"""Streamed project backups (format 3): one private SQLite file per project partition.

Release 0.3.0 built a project's backup as one canonical JSON document in memory
and refused any over 128 MiB, so a busy project could never be upgraded or
moved. Format 3 copies the partition row by row into its own SQLite file with
the isolated-restore schema, so the file is the restored copy: its checks
(schema, manifest, counts, integrity, foreign keys, coverage, records) and the
comparison with the live partition read both sides row by row, in the same
order, and never hold more than one batch. The ledger still registers the
file's SHA-256, and the file is checked again after it is read.

Format 2 (JSON) backups made by earlier releases stay readable through
learning_store._verify_backup_envelope; nothing writes them any more.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

FORMAT = "cocolo-project-backup"
FORMAT_VERSION = 3
SUFFIX = ".sqlite3"
BATCH_ROWS = 512
# Bytes a row takes beyond its values, for sizing before any write (SQLite
# record header, page and index share); a backup's size is planned, not measured.
ROW_OVERHEAD_BYTES = 64
SCHEMA_OVERHEAD_BYTES = 1_048_576
_MANIFEST_SCHEMA = (
    "CREATE TABLE cocolo_backup_manifest(format TEXT NOT NULL,format_version INTEGER NOT NULL,"
    "ledger_schema_version INTEGER NOT NULL,project_id TEXT NOT NULL,manifest BLOB NOT NULL)"
)
_REFERENCE: list[set[tuple[str, str, str]]] = []


def _store():
    from . import learning_store

    return learning_store


def _error(message: str) -> Exception:
    return _store().LedgerError(message)


def _columns(schema_version: int, table: str) -> list[str]:
    return list(_store()._backup_expected_columns(schema_version, table))


def estimate_bytes(connection: sqlite3.Connection, project_id: str, schema_version: int) -> int:
    """Planned size of one project's backup, from SQL totals; no row is read into memory."""
    store = _store()
    total = SCHEMA_OVERHEAD_BYTES
    for table in store._backup_tables(schema_version):
        columns = _columns(schema_version, table)
        if not columns:
            continue
        lengths = "+".join(f"IFNULL(length({column}),0)" for column in columns)
        count, size = connection.execute(
            f"SELECT COUNT(*),IFNULL(SUM({lengths}),0) FROM {table} WHERE project_id=?", (project_id,)
        ).fetchone()
        total += int(size) + int(count) * ROW_OVERHEAD_BYTES
    return total


def write(connection: sqlite3.Connection, path: Path, project_id: str, schema_version: int) -> dict[str, Any]:
    """Copy one partition into the empty owner-only file at `path`; returns its manifest.

    The caller created `path` (0600, exclusive) and renames it into place only
    after this returns and the file verifies. On any failure it removes it.
    """
    store = _store()
    project = connection.execute(
        f"SELECT {','.join(store._BACKUP_PROJECT_COLUMNS)} FROM projects WHERE project_id=? AND status='active'",
        (project_id,),
    ).fetchone()
    if project is None:
        raise _error("project binding is not active")
    target = sqlite3.connect(str(path), isolation_level=None)
    try:
        # A partial file is discarded on any failure, so it needs no journal.
        target.execute("PRAGMA journal_mode=OFF")
        target.execute("PRAGMA synchronous=OFF")
        target.execute("PRAGMA foreign_keys=ON")
        target.executescript(store._BACKUP_RESTORE_SCHEMA + ";\n" + _MANIFEST_SCHEMA + ";")
        target.execute("BEGIN")
        target.execute(
            f"INSERT INTO projects({','.join(store._BACKUP_PROJECT_COLUMNS)}) "
            f"VALUES({','.join('?' for _ in store._BACKUP_PROJECT_COLUMNS)})", tuple(project))
        columns_manifest, counts = {}, {}
        for table in store._backup_tables(schema_version):
            columns = _columns(schema_version, table)
            columns_manifest[table], counts[table] = columns, 0
            if not columns:
                continue
            insert = (f"INSERT INTO {table}(project_id,{','.join(columns)}) "
                      f"VALUES({','.join('?' for _ in range(len(columns) + 1))})")
            cursor = connection.execute(f"SELECT {','.join(columns)} FROM {table} WHERE project_id=?", (project_id,))
            try:
                while batch := cursor.fetchmany(BATCH_ROWS):
                    try:
                        target.executemany(insert, [(project_id, *row) for row in batch])
                    except sqlite3.IntegrityError:
                        raise _error("isolated project backup restoration rejected a row") from None
                    counts[table] += len(batch)
            finally:
                cursor.close()
        manifest = {"columns": columns_manifest, "counts": counts}
        target.execute("INSERT INTO cocolo_backup_manifest VALUES(?,?,?,?,?)", (
            FORMAT, FORMAT_VERSION, schema_version, project_id, _canonical(manifest)))
        target.execute("COMMIT")
    finally:
        target.close()
    return manifest


def _canonical(value: Any) -> bytes:
    from .learning_contracts import canonical_json

    return canonical_json(value)


def file_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1_048_576):
        digest.update(chunk)
    return digest.hexdigest()


def _reference_objects() -> set[tuple[str, str, str]]:
    """Every schema object a format-3 file may hold: the restore schema and the manifest."""
    if not _REFERENCE:
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(_store()._BACKUP_RESTORE_SCHEMA + ";\n" + _MANIFEST_SCHEMA + ";")
            _REFERENCE.append(_objects(reference))
        finally:
            reference.close()
    return _REFERENCE[0]


def _objects(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {(str(kind), str(name), " ".join(str(sql or "").split())) for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'")}


def open_backup(path: Path) -> sqlite3.Connection:
    """Read-only; the caller has checked the file's identity and digest."""
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise _error("project backup is invalid") from None
    return connection


def _order(backup: sqlite3.Connection, table: str, columns: list[str]) -> list[str]:
    """A total order both sides share: the primary key, then every other column."""
    keyed = sorted((row[5], row[1]) for row in backup.execute(f"PRAGMA table_info({table})") if row[5])
    order = [name for _, name in keyed if name in columns]
    return order + [name for name in columns if name not in order]


def _same(left: tuple, right: tuple) -> bool:
    return len(left) == len(right) and all(
        type(a) is type(b) and a == b for a, b in zip(left, right))


def verify(path: Path, *, project_id: str, schema_version: int | None = None,
           compare: sqlite3.Connection | None = None, exclude_file_id: str | None = None) -> dict[str, Any]:
    """Check a format-3 file completely; with `compare`, also that it equals the live partition.

    `exclude_file_id` leaves the backup's own registration out of the live side,
    which did not exist when the copy was written.
    """
    store = _store()
    from .learning_contracts import ContractError, content_digest, validate_learning_record

    backup = open_backup(path)
    try:
        try:
            if _objects(backup) != _reference_objects():
                raise _error("project backup schema is invalid")
            rows = backup.execute("SELECT format,format_version,ledger_schema_version,project_id,manifest "
                                  "FROM cocolo_backup_manifest").fetchall()
            if len(rows) != 1:
                raise _error("project backup manifest is invalid")
            kind, version, ledger_version, owner, raw = rows[0]
            if (kind != FORMAT or type(version) is not int or version != FORMAT_VERSION or type(ledger_version) is not int
                    or ledger_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13} or owner != project_id
                    or (schema_version is not None and ledger_version != schema_version)
                    or type(raw) is not bytes):
                raise _error("project backup manifest is invalid")
            manifest = json.loads(raw)
            tables = list(store._backup_tables(ledger_version))
            if (not isinstance(manifest, dict) or set(manifest) != {"columns", "counts"}
                    or _canonical(manifest) != raw
                    or set(manifest["columns"]) != set(tables) or set(manifest["counts"]) != set(tables)):
                raise _error("project backup table manifest is invalid")
            projects = backup.execute(
                f"SELECT {','.join(store._BACKUP_PROJECT_COLUMNS)} FROM projects").fetchall()
            if len(projects) != 1 or projects[0][0] != project_id:
                raise _error("project backup project row is invalid")
            for table in tables:
                columns, count = manifest["columns"][table], manifest["counts"][table]
                if columns != _columns(ledger_version, table) or type(count) is not int or count < 0:
                    raise _error("project backup count or column manifest is invalid")
                if not columns:
                    if count:
                        raise _error("project backup absent table has rows")
                    continue
                total, own = backup.execute(
                    f"SELECT COUNT(*),SUM(project_id=?) FROM {table}", (project_id,)).fetchone()
                if total != count or (own or 0) != count:
                    raise _error("project backup count or column manifest is invalid")
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _error("isolated project backup integrity check failed")
            if backup.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise _error("isolated project backup foreign-key check failed")
            store._check_backup_coverage(backup, ledger_version)
            cursor = backup.execute("SELECT content,content_digest FROM records WHERE project_id=?", (project_id,))
            try:
                while batch := cursor.fetchmany(BATCH_ROWS):
                    for content, digest in batch:
                        try:
                            record = validate_learning_record(json.loads(bytes(content)))
                        except (ContractError, json.JSONDecodeError, TypeError, ValueError):
                            raise _error("project backup contains a corrupt learning record") from None
                        if content_digest(record) != digest:
                            raise _error("project backup contains a corrupt learning record")
            finally:
                cursor.close()
            if compare is not None:
                _compare(compare, backup, project_id, ledger_version, manifest, exclude_file_id)
        except sqlite3.Error:
            raise _error("project backup is invalid") from None
        except (ValueError, TypeError, KeyError):
            raise _error("project backup manifest is invalid") from None
    finally:
        backup.close()
    return {"ledger_schema_version": ledger_version, "counts": dict(manifest["counts"]),
            "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def _compare(live: sqlite3.Connection, backup: sqlite3.Connection, project_id: str, schema_version: int,
             manifest: dict[str, Any], exclude_file_id: str | None) -> None:
    store = _store()
    names = ",".join(store._BACKUP_PROJECT_COLUMNS)
    current = live.execute(f"SELECT {names} FROM projects WHERE project_id=? AND status='active'",
                           (project_id,)).fetchone()
    kept = backup.execute(f"SELECT {names} FROM projects").fetchone()
    if current is None or not _same(tuple(current), tuple(kept)):
        raise _error("project backup does not match the current records")
    for table in store._backup_tables(schema_version):
        columns = manifest["columns"][table]
        if not columns:
            continue
        order = ",".join(_order(backup, table, columns))
        select = f"SELECT {','.join(columns)} FROM {table} WHERE project_id=?"
        excluded = table == "stored_files" and exclude_file_id is not None
        left = live.execute(select + (" AND file_id<>?" if excluded else "") + f" ORDER BY {order}",
                            (project_id, exclude_file_id) if excluded else (project_id,))
        right = backup.execute(select + f" ORDER BY {order}", (project_id,))
        try:
            while True:
                a, b = left.fetchmany(BATCH_ROWS), right.fetchmany(BATCH_ROWS)
                if len(a) != len(b) or not all(_same(tuple(x), tuple(y)) for x, y in zip(a, b)):
                    raise _error("project backup does not match the current records")
                if not a:
                    break
        finally:
            left.close()
            right.close()


def table_rows(backup: sqlite3.Connection, table: str, columns: list[str], *, project_id: str,
               order: str) -> Iterator[tuple]:
    """One table's rows from an open, verified backup, in `order`, one batch at a time."""
    cursor = backup.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE project_id=? ORDER BY {order}", (project_id,))
    try:
        while batch := cursor.fetchmany(BATCH_ROWS):
            yield from batch
    finally:
        cursor.close()
