"""Complete registered legacy rows, observed by their existing offline owner.

This private helper does not activate learning, authenticate native execution,
verify retained private artifacts, or authorize a writer cutover. Cooperative SQL
budgets bound each page's work, not physical memory or total offline wall time.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass

from .collector_paths import StateError, _assert_no_symlink_components, _check_owner_mode
from .history_contracts import HistoryError, _DIGEST, _PROJECT, matches
from .learning_contracts import canonical_json, keyed_digest
from .sql_read_budget import SQLReadConnection, SQLReadError, SQLReadLimits


@dataclass(frozen=True)
class SourceArchive:
    """The verified backup file an image was observed from: where, which file, how large.

    It grants nothing. A reader checks the file is still this one before and
    after each use, and the image is captured again, and compared, at the end.
    """
    path: object
    identity: tuple
    size: int

# The ledger schema the image is taken at. 14 added only the admission evidence
# index, which is not part of a project's backup, so the image's tables are 13's.
SCHEMA_VERSION = 14
# The registered backup is a streamed (format 3) SQLite file, read one batch at a
# time; this bounds its size, not memory. It matches the ledger's own limit.
MAX_BACKUP_BYTES = 16 * 1024 ** 3
PAGE_ROWS = 32
PAGE_BYTES = 256 * 1024
# Measured from the original schema. Identifiers never come from a request or
# backup. Every key has the fixed project prefix, supplied by the WHERE clause.
_KEYS = {
    "sessions": ("session_digest", "producer"),
    "run_provenance": ("run_id", "producer"),
    "records": ("record_id",),
    "record_links": ("from_record_id", "to_record_id", "relation"),
    "stored_files": ("file_id",),
    "collector_health": ("code",),
    "consumed_grants": ("grant_id",),
    "owner_attestations": ("subject_record_id",),
    "repair_receipts": ("receipt_id",),
    "launch_claims": ("handle_digest",),
    "launch_inputs": ("handle_digest",),
    "launch_observations": ("handle_digest",),
    "launch_receipts": ("scope_digest", "request_digest"),
    "active_run_capacity": ("handle_digest",),
    "capacity_completions": ("handle_digest",),
    "capacity_executions": ("handle_digest",),
    "account_cleanup_receipts": ("handle_digest",),
    "account_observation_origins": ("request_key",),
    "account_observation_requests": ("request_key",),
    "account_billing_probe_origins": ("request_key",),
    "account_billing_probe_requests": ("request_key",),
    "host_run_bindings": ("handle_digest",),
    "hook_ingestion": ("event_code",),
    "orchestrations": ("orchestration_id",),
    "orchestration_checkpoints": ("orchestration_id", "sequence"),
    "operational_qa": ("qa_id",),
    "operational_check_receipts": ("receipt_id",),
    "tombstones": ("deletion_id",),
    "account_open_receipts": ("handle_digest",),
    "account_open_requests": ("request_key",),
}


def _one(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private_file(info):
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_uid != os.getuid()):
        raise HistoryError("history_unsafe_path")


def _owner(ledger):
    from .learning_store import LearningLedger, LearningState

    if type(ledger) is not LearningLedger or type(ledger.state) is not LearningState:
        raise HistoryError("history_wrong_owner")
    if (type(ledger._connection) is not SQLReadConnection
            or ledger._connection_origin != (os.getpid(), threading.current_thread())):
        raise HistoryError("history_wrong_owner")
    connection = ledger._connection
    connection._read_owner()
    if ledger._allow_schema_upgrade is not True:
        raise HistoryError("history_not_authorized")
    if (ledger._collector_active is not False or ledger._readers() != 0
            or connection.in_transaction or connection._sql_scope is not None
            or connection._sql_foreign_progress):
        raise HistoryError("history_unavailable")
    descriptor = ledger._writer_descriptor
    if type(descriptor) is not int or descriptor < 0:
        raise HistoryError("history_unavailable")
    _assert_no_symlink_components(ledger.state.lock)
    _check_owner_mode(ledger.state.root, directory=True)
    _check_owner_mode(ledger.state.lock, directory=False)
    actual, expected = os.fstat(descriptor), ledger.state.lock.lstat()
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise HistoryError("history_unavailable")
    _private_file(actual)
    # The descriptor is the original supported owner's custody, not proof that
    # arbitrary same-user native code could not unlock or replace it.
    return (descriptor, actual.st_dev, actual.st_ino)


def _clock(ledger):
    now = ledger._clock_ms()
    if type(now) is not int or now < 0:
        raise HistoryError("history_unavailable")
    return now


def _registry(connection, project_id, file_id):
    if _one(connection, "SELECT 1 FROM projects WHERE project_id=? AND status='active'",
            (project_id,)) is None:
        raise HistoryError("history_conflict")
    # Length/type guards execute before conversion into Python values. No
    # unbounded stored path or digest is selected when those guards fail.
    row = _one(connection,
        "SELECT CASE WHEN typeof(relative_path)='text' AND "
        "length(CAST(relative_path AS BLOB)) BETWEEN 1 AND 4096 "
        "AND typeof(content_digest)='text' AND length(CAST(content_digest AS BLOB))=64 "
        "AND (expires_at_ms IS NULL OR typeof(expires_at_ms)='integer') THEN 1 ELSE 0 END "
        "FROM stored_files WHERE project_id=? AND file_id=? AND kind='backup'",
        (project_id, file_id))
    if row is None:
        raise HistoryError("history_not_found")
    if row[0] != 1:
        raise HistoryError("history_incompatible")
    row = _one(connection,
        "SELECT relative_path,content_digest,expires_at_ms FROM stored_files "
        "WHERE project_id=? AND file_id=? AND kind='backup'", (project_id, file_id))
    if row is None or not matches(row[1], _DIGEST):
        raise HistoryError("history_incompatible")
    return tuple(row)


def _drained(connection, now, monotonic_ns=None, *, children_only=False):
    # A run the project's own service drove settles by releasing its claim; the
    # ledger never consumes its binding. A claim more than the crash grace past
    # its hard deadline belongs to a run whose host is gone (the account already
    # stopped counting it): it and its binding never settle, and must not hold
    # every later migration back.
    from .active_run_capacity import crashed_claim

    #
    # `children_only` asks only whether a child may be running, for replacing the
    # collector or a project service: a child launches only after its claim, so a run
    # admitted but never claimed has none, and a run 0.2.x left never ran under either.
    # Otherwise an unclaimed run counts until its binding expires: no claim can be
    # admitted after that, so it can never record anything.
    monotonic_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    crashed, parameters = crashed_claim("a", now, monotonic_ns)
    if children_only:
        crashed = f"({crashed} OR a.kind='legacy_unknown')"
    if ((not children_only and _one(connection,
            "SELECT 1 FROM host_run_bindings h WHERE h.binding_state='pending' AND NOT EXISTS("
            "SELECT 1 FROM active_run_capacity a WHERE a.project_id=h.project_id "
            f"AND a.handle_digest=h.handle_digest AND (a.released_at_ms IS NOT NULL OR {crashed})) "
            "AND (h.expires_at_ms>? OR EXISTS(SELECT 1 FROM active_run_capacity c "
            "WHERE c.project_id=h.project_id AND c.handle_digest=h.handle_digest)) LIMIT 1",
            (*parameters, now)) is not None)
            or _one(connection,
                f"SELECT 1 FROM active_run_capacity a WHERE a.released_at_ms IS NULL AND NOT {crashed} LIMIT 1",
                parameters) is not None):
        raise HistoryError("history_unavailable")


def _schema(connection, store):
    if _one(connection, "PRAGMA user_version")[0] != SCHEMA_VERSION:
        raise HistoryError("history_incompatible")
    if tuple(_KEYS) != store._backup_tables(SCHEMA_VERSION):
        raise HistoryError("history_incompatible")
    cursor = connection.execute(
        "SELECT CASE WHEN length(CAST(name AS BLOB))<=160 THEN name ELSE NULL END "
        "FROM sqlite_master WHERE type='table' LIMIT 65")
    try:
        names = cursor.fetchmany(65)
    finally:
        cursor.close()
    # Global account metadata stays with its original owner; unknown tables
    # could hide an unrepresented project partition, so refuse this image.
    # admission_evidence holds only fields derived from the records it indexes.
    if {row[0] for row in names} != {*_KEYS, "projects", "additive_migrations",
                                   "owner_key_history", "sqlite_sequence", "admission_evidence"}:
        raise HistoryError("history_incompatible")
    for table in ("projects", *_KEYS):
        row = _one(connection, "SELECT type FROM sqlite_master WHERE name=?", (table,))
        if row is None or row[0] != "table":
            raise HistoryError("history_incompatible")
        cursor = connection.execute("PRAGMA table_info(" + table + ")")
        try:
            rows = cursor.fetchmany(65)
        finally:
            cursor.close()
        columns = (store._BACKUP_PROJECT_COLUMNS if table == "projects"
                   else ("project_id", *store._BACKUP_TABLE_COLUMNS[table]))
        keys = ("project_id",) if table == "projects" else ("project_id", *_KEYS[table])
        if (len(rows) > 64 or {r[1] for r in rows} != set(columns)
                or tuple(r[1] for r in sorted(rows, key=lambda r: r[5]) if r[5]) != keys):
            raise HistoryError("history_incompatible")


def _key(row, keys):
    values = tuple(row[name] for name in keys)
    if any(type(value) is not (int if name == "sequence" else str)
           for name, value in zip(keys, values)):
        raise HistoryError("history_incompatible")
    return values


def _page(connection, store, project_id, file_id, table, last):
    columns, keys = store._BACKUP_TABLE_COLUMNS[table], _KEYS[table]
    sql = "SELECT " + ",".join(columns) + " FROM " + table + " WHERE project_id=?"
    parameters = [project_id]
    if table == "stored_files":
        sql += " AND file_id<>?"
        parameters.append(file_id)
    if last is not None:
        if len(keys) == 1:
            sql += " AND " + keys[0] + ">?"
        else:
            sql += " AND (" + ",".join(keys) + ")>(" + ",".join("?" for _ in keys) + ")"
        parameters.extend(last)
    sql += " ORDER BY " + ",".join(keys) + " LIMIT 32"
    cursor = connection.execute(sql, parameters)
    rows, used = [], 0
    try:
        for raw in cursor:
            row = {name: store._backup_encode_value(raw[name]) for name in columns}
            size = len(canonical_json(row))
            if size > PAGE_BYTES:
                raise HistoryError("history_learning_capacity")
            if used + size > PAGE_BYTES:
                break
            _key(row, keys)
            rows.append(row)
            used += size
    finally:
        cursor.close()
    return rows


def _compare(connection, store, project_id, file_id, backup, limits, now):
    """Every live row equals the backup's, page by page, in key order; neither side is held."""
    from .project_backup import table_rows

    def project():
        _drained(connection, now)
        row = _one(connection, "SELECT " + ",".join(store._BACKUP_PROJECT_COLUMNS)
                   + " FROM projects WHERE project_id=? AND status='active'", (project_id,))
        return None if row is None else {
            name: store._backup_encode_value(row[name]) for name in store._BACKUP_PROJECT_COLUMNS}

    names = store._BACKUP_PROJECT_COLUMNS
    kept = backup.execute("SELECT " + ",".join(names) + " FROM projects").fetchone()
    kept = None if kept is None else {name: store._backup_encode_value(value) for name, value in zip(names, kept)}
    if canonical_json(connection.transaction_read_call(limits, project)) != canonical_json(kept):
        raise HistoryError("history_conflict")
    for table, keys in _KEYS.items():
        columns = store._BACKUP_TABLE_COLUMNS[table]
        expected = ({name: store._backup_encode_value(value) for name, value in zip(columns, row)}
                    for row in table_rows(backup, table, list(columns), project_id=project_id, order=",".join(keys)))
        try:
            last = None
            while True:
                rows = connection.transaction_read_call(limits,
                    lambda: _page(connection, store, project_id, file_id, table, last))
                if not rows:
                    if next(expected, None) is not None:
                        raise HistoryError("history_conflict")
                    break
                for row in rows:
                    wanted = next(expected, None)
                    if wanted is None or canonical_json(row) != canonical_json(wanted):
                        raise HistoryError("history_conflict")
                # Resume after the last CONSUMED row if the byte envelope stopped a
                # page early. A fetched-but-unconsumed row is read again next page.
                last = _key(rows[-1], keys)
        finally:
            expected.close()


def _open_backup(ledger, project_id, file_id, registration):
    path = ledger.state.backups / project_id / (file_id + ".sqlite3")
    if registration[0] != path.relative_to(ledger.state.root).as_posix():
        raise HistoryError("history_unsafe_path")
    _assert_no_symlink_components(path)
    for parent in (ledger.state.root, ledger.state.backups, path.parent):
        _check_owner_mode(parent, directory=True)
    _check_owner_mode(path, directory=False)
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_NONBLOCK", 0):
        raise HistoryError("history_unavailable")
    expected = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | flags)
    try:
        actual = os.fstat(descriptor)
        _private_file(actual)
        if _identity(actual) != _identity(expected):
            raise HistoryError("history_unsafe_path")
        parents = tuple((parent, parent.lstat().st_dev, parent.lstat().st_ino)
                        for parent in (ledger.state.root, ledger.state.backups, path.parent))
        return path, descriptor, _identity(actual), parents
    except BaseException:
        os.close(descriptor)
        raise


def _digest(descriptor):
    """The backup file's SHA-256, read in bounded chunks; its size is checked first."""
    info = os.fstat(descriptor)
    if not 0 < info.st_size <= MAX_BACKUP_BYTES:
        raise HistoryError("history_learning_capacity")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = os.read(descriptor, 1_048_576)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BACKUP_BYTES:
            raise HistoryError("history_learning_capacity")
        digest.update(chunk)
    if size != info.st_size:
        raise HistoryError("history_conflict")
    return digest.hexdigest()


def _capture(ledger, project_id, file_id, expected_domain_digest, limits):
    from . import learning_store as store

    _owner(ledger)
    with ledger._held():
        custody = _owner(ledger)
        connection = ledger._connection
        _schema(connection, store)
        changes, started = connection.total_changes, _clock(ledger)

        def registry():
            _drained(connection, started)
            return _registry(connection, project_id, file_id)

        registration = connection.read_call(limits, registry)
        expiry = registration[2]
        if expiry is not None and expiry <= started:
            raise HistoryError("history_result_expired")
        key = ledger.state.digest_key()
        if type(key) is not bytes or len(key) != 32:
            raise HistoryError("history_unavailable")
        domain = keyed_digest(key, b"project-learning-key-domain-v1\0" + project_id.encode("ascii"))
        if not hmac.compare_digest(domain, expected_domain_digest):
            raise HistoryError("history_conflict")
        path, descriptor, identity, parents = _open_backup(ledger, project_id, file_id, registration)
        try:
            backup_digest = _digest(descriptor)
            if not hmac.compare_digest(backup_digest, registration[1]):
                raise HistoryError("history_conflict")
            from . import project_backup
            try:
                verified = project_backup.verify(path, project_id=project_id, schema_version=SCHEMA_VERSION)
            except store.LedgerError:
                raise HistoryError("history_incompatible") from None
            counts = verified["counts"]
            if set(counts) != set(_KEYS) or any(type(counts[name]) is not int for name in _KEYS):
                raise HistoryError("history_incompatible")
            backup = project_backup.open_backup(path)
            try:
                if backup.execute("SELECT 1 FROM stored_files WHERE file_id=? LIMIT 1", (file_id,)).fetchone():
                    raise HistoryError("history_conflict")
                # Only bounded local SQLite/CPU work occurs under this read snapshot.
                # Key I/O stays outside it, including on failure.
                connection.execute("BEGIN").close()
                try:
                    _compare(connection, store, project_id, file_id, backup, limits, started)
                finally:
                    connection.rollback()
            except sqlite3.Error:
                raise HistoryError("history_incompatible") from None
            finally:
                backup.close()
            _schema(connection, store)
            if custody != _owner(ledger) or connection.total_changes != changes:
                raise HistoryError("history_conflict")
            if registration != connection.read_call(limits, registry):
                raise HistoryError("history_conflict")
            if not hmac.compare_digest(key, ledger.state.digest_key()):
                raise HistoryError("history_conflict")
            _assert_no_symlink_components(path)
            _check_owner_mode(path, directory=False)
            if _identity(os.fstat(descriptor)) != identity or _identity(path.lstat()) != identity:
                raise HistoryError("history_conflict")
            for parent, device, inode in parents:
                _check_owner_mode(parent, directory=True)
                info = parent.lstat()
                if (info.st_dev, info.st_ino) != (device, inode):
                    raise HistoryError("history_conflict")
            if _digest(descriptor) != backup_digest:
                raise HistoryError("history_conflict")
            finished = _clock(ledger)
            if finished < started:
                raise HistoryError("history_unavailable")
            if expiry is not None and expiry <= finished:
                raise HistoryError("history_result_expired")
            if (connection.total_changes != changes or custody != _owner(ledger)
                    or registration != connection.read_call(limits, registry)
                    or _identity(os.fstat(descriptor)) != identity
                    or _identity(path.lstat()) != identity):
                raise HistoryError("history_conflict")
            # The archived image is the backup file itself, so its digest is the file's.
            metadata = {"project_id": project_id, "ledger_schema_version": SCHEMA_VERSION,
                        "backup_digest": backup_digest, "manifest_digest": backup_digest,
                        "key_domain_digest": domain}
            return {**metadata, "source_identity_digest": keyed_digest(
                key, b"learning-source-image-v1\0" + canonical_json(metadata)),
                "archive": SourceArchive(path, identity, identity[5]),
                "counts": tuple((name, counts[name]) for name in _KEYS),
                "learning_ready": False}
        finally:
            os.close(descriptor)


def capture_source_image(ledger, project_id, file_id, *, expected_domain_digest, work_limits):
    """Observe a full registered current image; return no activation permission."""
    from .learning_store import LedgerError

    reason = "history_unavailable"
    try:
        if (not matches(project_id, _PROJECT) or not matches(expected_domain_digest, _DIGEST)
                or type(file_id) is not str or not 0 < len(file_id) <= 160
                or not file_id.replace("_", "").isalnum() or type(work_limits) is not SQLReadLimits):
            raise HistoryError("history_invalid_input")
        work_limits.__post_init__()
        return _capture(ledger, project_id, file_id, expected_domain_digest, work_limits)
    except HistoryError as error:
        reason = error.reason_code
    except SQLReadError as error:
        reason = ("history_wrong_owner" if error.reason_code == "sql_read_wrong_owner" else
                  "history_invalid_input" if error.reason_code == "sql_read_invalid_limits" else
                  "history_learning_capacity" if error.reason_code in {
                      "sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit"}
                  else "history_unavailable")
    except StateError:
        reason = "history_unsafe_path"
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError,
            LedgerError):
        pass
    # No raw SQL, payload, path or underlying exception chain crosses the boundary.
    raise HistoryError(reason)


def _same_file(archive):
    _assert_no_symlink_components(archive.path)
    if _identity(archive.path.lstat()) != archive.identity:
        raise HistoryError("history_conflict")


def archive_chunks(archive, size):
    """The archived backup's bytes, `size` at a time, from the file the image was observed in."""
    _same_file(archive)
    descriptor = os.open(archive.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if _identity(os.fstat(descriptor)) != archive.identity:
            raise HistoryError("history_conflict")
        read = 0
        while chunk := os.read(descriptor, size):
            read += len(chunk)
            yield chunk
        if read != archive.size or _identity(os.fstat(descriptor)) != archive.identity:
            raise HistoryError("history_conflict")
    finally:
        os.close(descriptor)


def archive_rows(archive, project_id, table, columns, order):
    """One table's rows from the archived backup, a batch at a time, in `order`."""
    from .project_backup import open_backup, table_rows

    _same_file(archive)
    backup = open_backup(archive.path)
    try:
        yield from table_rows(backup, table, list(columns), project_id=project_id, order=order)
    finally:
        backup.close()
    _same_file(archive)
