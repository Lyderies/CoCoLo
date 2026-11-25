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
# B-tree pages are not filled completely: a quarter more covers it.
PAGE_SLACK_DIVISOR = 4
_MANIFEST_SCHEMA = (
    "CREATE TABLE cocolo_backup_manifest(format TEXT NOT NULL,format_version INTEGER NOT NULL,"
    "ledger_schema_version INTEGER NOT NULL,project_id TEXT NOT NULL,manifest BLOB NOT NULL)"
)
_REFERENCE: dict[int, set[tuple[str, str, str]]] = {}
# O0b RS-A (round 1 F3): the tables each ledger version added to a format-3 file. A file holds exactly its own
# version's objects, so a file an earlier release wrote still verifies (its indexes go with their tables).
_ADDED_IN = {15: ("run_holds",)}


def _file_schema(schema_version: int) -> str:
    """The format-3 file schema for one ledger version: the restore schema without later versions' tables."""
    later = [table for version, tables in _ADDED_IN.items() if schema_version < version for table in tables]
    schema = _store()._BACKUP_RESTORE_SCHEMA
    if schema_version < 16:
        schema = schema.replace(",probe_id TEXT", "")
    return (schema + ";\n" + _MANIFEST_SCHEMA + ";\n"
            + "".join(f"DROP TABLE {table};\n" for table in later))


def check_inventory(connection: sqlite3.Connection) -> None:
    """Refuse a ledger this backup cannot represent exactly, before anything is written (decision 249).

    Every table the ledger holds is a backed-up table, the projects table, a known machine-local table, another
    known non-partition table (sqlite_ internal tables aside), and each of those but the backed-up ones has exactly
    its own columns, generated and hidden columns included (table_xinfo). The owner script's own check
    (scripts/backup_installed_project.py) is the same rule; a test pins the two definitions equal.
    """
    store = _store()

    def named(table):
        return [(str(row[1]), int(row[6])) for row in sorted(
            connection.execute(f"PRAGMA table_xinfo({table})").fetchall(), key=lambda row: row[0])]
    found = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
             if not str(row[0]).startswith("sqlite_")}
    backed = set(store._BACKUP_TABLE_COLUMNS)  # any version's backed-up table is known
    if found - backed - {"projects"} - set(store.LOCAL_TABLE_COLUMNS) - set(store.OTHER_TABLE_COLUMNS):
        raise _error("project backup cannot represent the ledger's tables exactly")
    for table, expected in {**store.LOCAL_TABLE_COLUMNS, **store.OTHER_TABLE_COLUMNS}.items():
        if table not in found:
            continue
        columns = named(table)
        if tuple(name for name, _ in columns) != expected or any(hidden for _, hidden in columns):
            raise _error(f"project backup cannot represent {table} exactly")
    projects = named("projects")
    names = {name for name, _ in projects}
    if store.LOCAL_PROJECT_COLUMNS <= names:
        names -= store.LOCAL_PROJECT_COLUMNS
    if names != set(store._BACKUP_PROJECT_COLUMNS) or any(hidden for _, hidden in projects):
        raise _error("project backup cannot represent projects exactly")
    for table in sorted(found & backed):
        if any(hidden for _, hidden in named(table)):
            raise _error(f"project backup cannot represent {table} exactly")


def _store():
    from . import learning_store

    return learning_store


def _error(message: str) -> Exception:
    return _store().LedgerError(message)


def _columns(schema_version: int, table: str) -> list[str]:
    return list(_store()._backup_expected_columns(schema_version, table))


def estimate_bytes(connection: sqlite3.Connection, project_id: str, schema_version: int) -> int:
    """Planned size of one project's backup, from SQL totals; no row is read into memory.

    It counts every stored byte of each row (project_id included), once more for
    every index of the backup's schema that copies a column, the per-row
    overhead, and slack for partly filled pages, so it is not smaller than the
    file. The file's real size is checked again after it is written.
    """
    store = _store()
    total = SCHEMA_OVERHEAD_BYTES
    for table in store._backup_tables(schema_version):
        columns = _columns(schema_version, table)
        if not columns:
            continue
        stored = ["project_id", *columns]
        weights = {name: 1 + _index_copies(table).get(name, 0) for name in stored}
        lengths = "+".join(f"{weights[name]}*IFNULL(length(CAST({name} AS BLOB)),0)" for name in stored)
        count, size = connection.execute(
            f"SELECT COUNT(*),IFNULL(SUM({lengths}),0) FROM {table} WHERE project_id=?", (project_id,)
        ).fetchone()
        indexes = 1 + sum(1 for _ in _index_names(table))
        total += int(size) + int(count) * ROW_OVERHEAD_BYTES * indexes
    return total + total // PAGE_SLACK_DIVISOR


_INDEXES: dict[str, dict[str, int]] = {}


def _index_names(table: str) -> list[str]:
    return [name for name, _ in _index_columns(table)]


_INDEX_COLUMNS: dict[str, list[tuple[str, list[str]]]] = {}


def _index_columns(table: str) -> list[tuple[str, list[str]]]:
    if table in _INDEX_COLUMNS:
        return _INDEX_COLUMNS[table]
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_store()._BACKUP_RESTORE_SCHEMA + ";")
        _INDEX_COLUMNS[table] = [
            (row[1], [info[2] for info in reference.execute(f"PRAGMA index_info({row[1]!r})")])
            for row in reference.execute(f"PRAGMA index_list({table})")]
        return _INDEX_COLUMNS[table]
    finally:
        reference.close()


def _index_copies(table: str) -> dict[str, int]:
    """For each column, how many of the backup schema's indexes on `table` copy it."""
    if table not in _INDEXES:
        copies: dict[str, int] = {}
        for _, names in _index_columns(table):
            for name in names:
                copies[name] = copies.get(name, 0) + 1
        _INDEXES[table] = copies
    return _INDEXES[table]


def write(connection: sqlite3.Connection, path: Path, project_id: str, schema_version: int, *,
          limit_bytes: int | None = None) -> dict[str, Any]:
    """Copy one partition into the empty owner-only file at `path`; returns its manifest.

    The caller created `path` (0600, exclusive) and renames it into place only
    after this returns and the file verifies. On any failure it removes it.
    With `limit_bytes`, the file's size is checked after every batch, so an
    oversized partition is refused before it fills the disk.
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
        target.executescript(_file_schema(schema_version))
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
                    if limit_bytes is not None and os.stat(path).st_size > limit_bytes:
                        raise _store().LedgerCapacityError(projects=1, needed_bytes=os.stat(path).st_size,
                                                           limit_bytes=limit_bytes)
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


def _reference_objects(schema_version: int) -> set[tuple[str, str, str]]:
    """Every schema object a format-3 file of this ledger version holds: its restore schema and the manifest."""
    if schema_version not in _REFERENCE:
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(_file_schema(schema_version))
            _REFERENCE[schema_version] = _objects(reference)
        finally:
            reference.close()
    return _REFERENCE[schema_version]


def _objects(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {(str(kind), str(name), " ".join(str(sql or "").split())) for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'")}


def store_versions() -> frozenset[int]:
    return frozenset({5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17})


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
            # The manifest says which version's objects the file must hold exactly (round 1 F3).
            try:
                rows = backup.execute("SELECT format,format_version,ledger_schema_version,project_id,manifest "
                                      "FROM cocolo_backup_manifest").fetchall()
            except sqlite3.Error:
                raise _error("project backup schema is invalid") from None
            if (len(rows) != 1 or type(rows[0][2]) is not int or rows[0][2] not in store_versions()
                    or _objects(backup) != _reference_objects(rows[0][2])):
                raise _error("project backup schema is invalid")
            if len(rows) != 1:
                raise _error("project backup manifest is invalid")
            kind, version, ledger_version, owner, raw = rows[0]
            if (kind != FORMAT or type(version) is not int or version != FORMAT_VERSION or type(ledger_version) is not int
                    or ledger_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17} or owner != project_id
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
                            record = validate_learning_record(json.loads(bytes(content)), stored=True)
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
