"""Connection-owning project history and atomic terminal outbox.

Only a persistence owner may instantiate this class. It is not an external API,
does not authenticate a caller, and cannot admit, launch or release account work.
There is no LearningState, digest-key creation or account authority in this store.
The installed CLI does not use it until the service/enrollment/migration gates pass.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .contracts import ContractError
from .history_contracts import (
    MAX_SEQUENCE, AttemptBinding, HistoryEntry, HistoryError, TerminalDelivery,
    _DIGEST, _ID, _PROJECT, matches, valid_integer,
)
from .learning_contracts import (
    MAX_RECORD_BYTES, SOURCES, canonical_json, keyed_digest, parse_bounded_json, validate_learning_record,
)
from .result_contracts import (
    MAX_ASSIGNMENT_CANONICAL_BYTES, MAX_RESULT_BODY_BYTES, MAX_RESULT_RETENTION_MS,
    ResultCapacity, ResultReservation, assignment_bytes, checked_result_body,
)

_APPLICATION_ID = 0x43444831
_SCHEMA_VERSION = 2
_RECORD_TYPES = frozenset({"event", "dispatch_intent", "dispatch_result", "outcome", "verification_binding"})
MAX_DELIVERY_PAGE_BYTES = 16_384
MAX_BINDING_BYTES = 4096
MAX_HISTORY_ROW_BYTES = 2 * MAX_RECORD_BYTES
_SCHEMA_V1 = """
CREATE TABLE history_identity(
 project_id TEXT PRIMARY KEY, next_source_sequence INTEGER NOT NULL,
 next_delivery_sequence INTEGER NOT NULL);
CREATE TABLE records(
 project_id TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL,
 source TEXT NOT NULL, source_sequence INTEGER NOT NULL, observed_at_ms INTEGER NOT NULL,
 ingested_at_ms INTEGER NOT NULL, content BLOB NOT NULL, content_digest TEXT NOT NULL,
 late INTEGER NOT NULL CHECK(late IN (0,1)),
 PRIMARY KEY(project_id,record_id), UNIQUE(project_id,source_sequence),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE TABLE terminal_outbox(
 project_id TEXT NOT NULL, claim_id TEXT NOT NULL, delivery_sequence INTEGER NOT NULL,
 run_handle TEXT NOT NULL, attempt INTEGER NOT NULL, claim_digest TEXT NOT NULL,
 binding BLOB NOT NULL, record_id TEXT NOT NULL, record_content_digest TEXT NOT NULL,
 result_digest TEXT NOT NULL, receipt_digest TEXT,
 PRIMARY KEY(project_id,claim_id), UNIQUE(project_id,delivery_sequence), UNIQUE(project_id,record_id),
 UNIQUE(project_id,run_handle,attempt), UNIQUE(project_id,claim_digest),
 FOREIGN KEY(project_id,record_id) REFERENCES records(project_id,record_id));
CREATE INDEX records_observed ON records(project_id,observed_at_ms);
"""
_RESULT_SCHEMA = """
CREATE TABLE result_identity(
 project_id TEXT PRIMARY KEY, key_check TEXT NOT NULL, last_observed_at_ms INTEGER NOT NULL,
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE TABLE result_reservations(
 project_id TEXT NOT NULL, claim_id TEXT NOT NULL, run_handle TEXT NOT NULL,
 attempt INTEGER NOT NULL, claim_digest TEXT NOT NULL, orchestration_handle TEXT NOT NULL,
 binding BLOB NOT NULL, assignment_digest TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL, hard_deadline_at_ms INTEGER NOT NULL,
 reserved_until_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','committed','retiring','expired')),
 prepared_digest TEXT, prepared_at_ms INTEGER, retained_until_ms INTEGER,
 committed_at_ms INTEGER, body_size INTEGER,
 result_digest TEXT, terminal_record_id TEXT,
 PRIMARY KEY(project_id,claim_id), UNIQUE(project_id,run_handle,attempt),
 UNIQUE(project_id,claim_digest),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id),
 FOREIGN KEY(project_id,terminal_record_id) REFERENCES records(project_id,record_id));
CREATE INDEX result_expiry ON result_reservations(project_id,expires_at_ms);
"""
_SCHEMA = _SCHEMA_V1 + _RESULT_SCHEMA
_COLUMNS = {
    "history_identity": ("project_id", "next_source_sequence", "next_delivery_sequence"),
    "records": ("project_id", "record_id", "record_type", "source", "source_sequence",
                "observed_at_ms", "ingested_at_ms", "content", "content_digest", "late"),
    "terminal_outbox": ("project_id", "claim_id", "delivery_sequence", "run_handle", "attempt", "claim_digest", "binding", "record_id",
                        "record_content_digest", "result_digest", "receipt_digest"),
    "result_identity": ("project_id", "key_check", "last_observed_at_ms"),
    "result_reservations": ("project_id", "claim_id", "run_handle", "attempt", "claim_digest", "orchestration_handle",
                            "binding", "assignment_digest", "created_at_ms", "hard_deadline_at_ms", "reserved_until_ms",
                            "expires_at_ms", "state", "prepared_digest", "prepared_at_ms", "retained_until_ms",
                            "committed_at_ms", "body_size", "result_digest",
                            "terminal_record_id"),
}


def _operation(function):
    @functools.wraps(function)
    def guarded(self, *args, **kwargs):
        self._require_owner()
        failure = None
        try:
            return function(self, *args, **kwargs)
        except sqlite3.IntegrityError:
            failure = "history_conflict"
        except (sqlite3.Error, OSError):
            failure = "history_unavailable"
        if failure is not None:
            # Do not chain SQL, paths or rejected values into public diagnostics.
            raise HistoryError(failure)
    return guarded


class ProjectHistory:
    """One project, process, thread and writer; every mutation is a short transaction.

    The trusted launcher supplies an existing protected directory and project ID.
    Methods accept already authorized internal work, never a model request. Canonical
    records remain unchanged; sequence/ingestion metadata live in separate columns.
    Result artifacts require an explicitly injected owner-held digest key and
    finite policy. No ordinary client is migrated or authorized by this class.
    """

    def __init__(
        self, directory: Path, project_id: str, *, result_key: bytes | None = None,
        result_capacity: ResultCapacity | None = None, result_clock_ms: Callable[[], int] | None = None,
        allow_schema_upgrade: bool = False,
    ) -> None:
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._closed = False
        self._connection = None
        self._directory_fd = self._writer_fd = None
        self._result_key = None
        self._result_capacity = None
        self._result_clock = result_clock_ms if result_clock_ms is not None else (lambda: time.time_ns() // 1_000_000)
        if (
            type(allow_schema_upgrade) is not bool
            or (result_key is None) != (result_capacity is None)
            or (result_key is not None and (type(result_key) is not bytes or len(result_key) != 32))
            or (result_capacity is not None and type(result_capacity) is not ResultCapacity)
            or (result_clock_ms is not None and not callable(result_clock_ms))
        ):
            raise HistoryError("history_invalid_input")
        self._allow_schema_upgrade = allow_schema_upgrade
        if result_capacity is not None:
            invalid_capacity = False
            try:
                self._result_capacity = ResultCapacity(result_capacity.max_entries, result_capacity.max_reserved_bytes,
                                                       result_capacity.reservation_ms, result_capacity.retention_ms)
            except (HistoryError, AttributeError, TypeError, ValueError, OverflowError):
                invalid_capacity = True
            if invalid_capacity:
                raise HistoryError("history_invalid_input")
            self._result_key = result_key
        if (
            not callable(getattr(sqlite3.Connection, "setlimit", None))
            or type(getattr(sqlite3, "SQLITE_LIMIT_LENGTH", None)) is not int
        ):
            raise HistoryError("history_incompatible")
        if not matches(project_id, _PROJECT) or not isinstance(directory, Path) or not directory.is_absolute():
            raise HistoryError("history_invalid_input")
        self.project_id = project_id
        failure = None
        try:
            if directory.resolve(strict=True) != directory:
                raise HistoryError("history_unsafe_path")
            self._directory_fd = self._open_directory(directory)
            self._writer_fd = self._owned_file("history.lock")
            try:
                fcntl.flock(self._writer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HistoryError("history_writer_busy") from None
            database_fd = self._owned_file("history.sqlite3")
            os.close(database_fd)
            for name in ("history.sqlite3-wal", "history.sqlite3-shm", "history.sqlite3-journal"):
                try:
                    self._check_owned(os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False))
                except FileNotFoundError:
                    pass
            self._connection = sqlite3.connect(directory / "history.sqlite3", timeout=0.25, isolation_level=None)
            # Bound corrupted TEXT/BLOB rows in SQLite before Python receives
            # them. A valid record plus its fixed metadata fits this ceiling.
            self._connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_HISTORY_ROW_BYTES)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=250")
            self._initialize()
            if self._result_key is not None:
                self._initialize_results()
            if self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise HistoryError("history_unavailable")
        except HistoryError as exc:
            failure = exc.reason_code
        except (OSError, sqlite3.Error):
            failure = "history_unavailable"
        if failure is not None:
            self.close()
            raise HistoryError(failure)

    @staticmethod
    def _open_directory(directory: Path) -> int:
        """Check POSIX ancestry before SQLite's path open.

        A root/owner sticky directory (such as /private/tmp) protects entries
        owned by root/owner; every next component is checked for that ownership.
        The trusted launcher must additionally establish effective protection,
        including ACLs, and keep state outside every admitted model's writable
        roots. Mode checks cannot isolate arbitrary unsandboxed owner code.
        """
        descriptor = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in directory.parts[1:]:
                parent = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (0, os.getuid())
                    or (stat.S_IMODE(parent.st_mode) & 0o022 and not parent.st_mode & stat.S_ISVTX)
                ):
                    raise HistoryError("history_unsafe_path")
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            ProjectHistory._check_owned(os.fstat(descriptor), directory=True)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _check_owned(info, *, directory: bool = False) -> None:
        if (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
        ):
            raise HistoryError("history_unsafe_path")

    def _owned_file(self, name: str) -> int:
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
        try:
            self._check_owned(os.fstat(descriptor))
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _require_owner(self) -> None:
        if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")

    @contextmanager
    def _transaction(self):
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("ROLLBACK")
            raise

    def _initialize(self) -> None:
        connection = self._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        objects = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        actual_schema = {" ".join(row[2].split()).casefold() for row in objects}
        def schema_set(schema):
            return {" ".join(statement.split()).casefold() for statement in schema.split(";") if statement.strip()}
        if not objects and version == 0 and application == 0:
            # executescript is used only for this fixed, empty schema, never requests.
            connection.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
            try:
                connection.execute("INSERT INTO history_identity VALUES(?,1,1)", (self.project_id,))
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        elif version == 1 and application == _APPLICATION_ID and actual_schema == schema_set(_SCHEMA_V1):
            if not self._allow_schema_upgrade:
                # Only the separately authorized, drained persistence-owner
                # upgrade path may opt in; ordinary API startup never does.
                raise HistoryError("history_incompatible")
            # Validate the complete old schema and project identity before any
            # fixed migration write. No unknown trigger/schema is ever executed.
            self._check_schema_identity({name: columns for name, columns in _COLUMNS.items()
                                         if name not in {"result_identity", "result_reservations"}})
            with self._transaction():
                for statement in _RESULT_SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        elif (
            version != _SCHEMA_VERSION or application != _APPLICATION_ID
            or actual_schema != schema_set(_SCHEMA)
        ):
            raise HistoryError("history_incompatible")
        self._check_schema_identity(_COLUMNS)

    def _check_schema_identity(self, tables: dict) -> None:
        for name, columns in tables.items():
            actual = tuple(row[1] for row in self._connection.execute(f"PRAGMA table_info({name})"))
            if actual != columns:
                raise HistoryError("history_incompatible")
        rows = self._connection.execute("SELECT project_id FROM history_identity LIMIT 2").fetchall()
        if len(rows) != 1 or rows[0][0] != self.project_id:
            raise HistoryError("history_incompatible")

    def _initialize_results(self) -> None:
        check = keyed_digest(self._result_key, "project-result-key-v1:" + self.project_id)
        with self._transaction():
            row = self._connection.execute("SELECT * FROM result_identity LIMIT 2").fetchall()
            if not row:
                self._connection.execute("INSERT INTO result_identity VALUES(?,?,0)", (self.project_id, check))
            elif (len(row) != 1 or row[0]["project_id"] != self.project_id
                  or not matches(row[0]["key_check"], _DIGEST)
                  or not secrets.compare_digest(row[0]["key_check"], check)
                  or not valid_integer(row[0]["last_observed_at_ms"])):
                raise HistoryError("history_incompatible")

    def close(self) -> None:
        # An inherited instance must not close/unlock the original owner's resources.
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        if self._closed:
            return
        self._closed = True
        self._result_key = None
        if self._connection is not None:
            with contextlib.suppress(sqlite3.Error):
                self._connection.close()
        for descriptor in (self._writer_fd, self._directory_fd):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    @staticmethod
    def _checked_record(content: bytes, source: str, ingested_at_ms: int) -> dict:
        if (
            type(content) is not bytes or not 0 < len(content) <= MAX_RECORD_BYTES
            or type(source) is not str or source not in SOURCES or not valid_integer(ingested_at_ms)
        ):
            raise HistoryError("history_invalid_input")
        invalid = False
        try:
            parsed = parse_bounded_json(content)
            record_type = parsed.get("record_type")
            # Strict new writes do not change the legacy reader's canonical
            # bytes or equality semantics. Legacy import is a separate gate.
            if (
                type(record_type) is not str or record_type not in _RECORD_TYPES
                or type(parsed.get("schema_version")) is not int
            ):
                raise HistoryError("history_invalid_input")
            checked = validate_learning_record(parsed)
            if (
                canonical_json(checked) != content
                or not valid_integer(checked.get("observed_at_ms"))
            ):
                invalid = True
        except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
            invalid = True
        if invalid:
            raise HistoryError("history_invalid_input")
        return checked

    def _entry(self, row) -> HistoryEntry:
        invalid = False
        try:
            content = self._stored_blob(row["content"], maximum=MAX_RECORD_BYTES)
            checked = self._checked_record(content, row["source"], row["ingested_at_ms"])
            invalid = (
                row["project_id"] != self.project_id or row["record_id"] != checked["record_id"]
                or row["record_type"] != checked["record_type"]
                or row["observed_at_ms"] != checked["observed_at_ms"]
                or not valid_integer(row["source_sequence"], minimum=1, maximum=MAX_SEQUENCE - 1)
                or type(row["late"]) is not int or row["late"] not in (0, 1)
                or row["content_digest"] != hashlib.sha256(content).hexdigest()
            )
        except (HistoryError, TypeError, ValueError, OverflowError):
            invalid = True
        if invalid:
            raise HistoryError("history_incompatible")
        return HistoryEntry(
            row["project_id"], row["record_id"], row["source"], row["source_sequence"],
            row["observed_at_ms"], row["ingested_at_ms"], content,
            row["content_digest"], bool(row["late"]),
        )

    def _append_locked(self, checked: dict, content: bytes, source: str, timestamp: int) -> HistoryEntry:
        digest = hashlib.sha256(content).hexdigest()
        row = self._connection.execute(
            "SELECT * FROM records WHERE project_id=? AND record_id=?", (self.project_id, checked["record_id"]),
        ).fetchone()
        if row is not None:
            entry = self._entry(row)
            if entry.content_digest != digest or entry.content != content or entry.source != source:
                raise HistoryError("history_conflict")
            return entry
        sequence = self._next_sequence(terminal=False)
        maximum = self._connection.execute(
            "SELECT COALESCE(MAX(observed_at_ms),0) FROM records WHERE project_id=?", (self.project_id,),
        ).fetchone()[0]
        if not valid_integer(maximum):
            raise HistoryError("history_incompatible")
        late = checked["observed_at_ms"] < maximum
        self._connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.project_id, checked["record_id"], checked["record_type"], source, sequence,
             checked["observed_at_ms"], timestamp, content, digest, int(late)),
        )
        self._connection.execute("UPDATE history_identity SET next_source_sequence=?", (sequence + 1,))
        return HistoryEntry(self.project_id, checked["record_id"], source, sequence,
                            checked["observed_at_ms"], timestamp, content, digest, late)

    @_operation
    def append_record(self, content: bytes, *, source: str, ingested_at_ms: int) -> HistoryEntry:
        checked = self._checked_record(content, source, ingested_at_ms)
        if checked["record_type"] == "dispatch_result":
            # A terminal record must never bypass its atomic delivery outbox.
            raise HistoryError("history_invalid_input")
        with self._transaction():
            return self._append_locked(checked, content, source, ingested_at_ms)

    @_operation
    def record(self, record_id: str) -> HistoryEntry:
        if not matches(record_id, _ID):
            raise HistoryError("history_invalid_input")
        row = self._connection.execute(
            "SELECT * FROM records WHERE project_id=? AND record_id=?", (self.project_id, record_id),
        ).fetchone()
        if row is None:
            raise HistoryError("history_not_found")
        return self._entry(row)

    @staticmethod
    def _stored_blob(value: object, *, maximum: int) -> bytes:
        # bytes(INTEGER) allocates that many bytes; bytes(TEXT) raises TypeError.
        # SQLite BLOB affinity does not enforce the storage class. Never coerce.
        if type(value) is not bytes or not 0 < len(value) <= maximum:
            raise HistoryError("history_incompatible")
        return value

    def _next_sequence(self, *, terminal: bool) -> int:
        statement = (
            "SELECT next_delivery_sequence FROM history_identity WHERE project_id=?" if terminal
            else "SELECT next_source_sequence FROM history_identity WHERE project_id=?"
        )
        row = self._connection.execute(statement, (self.project_id,)).fetchone()
        if row is None:
            raise HistoryError("history_incompatible")
        if type(row[0]) is int and row[0] == MAX_SEQUENCE:
            raise HistoryError("history_sequence_exhausted")
        if not valid_integer(row[0], minimum=1, maximum=MAX_SEQUENCE - 1):
            raise HistoryError("history_incompatible")
        return row[0]

    def _delivery(self, row) -> TerminalDelivery:
        invalid = False
        reason = "history_incompatible"
        try:
            encoded = self._stored_blob(row["binding"], maximum=MAX_BINDING_BYTES)
            binding = AttemptBinding(**parse_bounded_json(encoded))
            entry = self.record(row["record_id"])
            checked = parse_bounded_json(entry.content)
            delivery = TerminalDelivery(row["delivery_sequence"], binding, row["record_id"],
                                        row["record_content_digest"], row["result_digest"])
            invalid = (
                encoded != canonical_json(binding.as_data())
                or row["project_id"] != self.project_id or binding.project_id != self.project_id
                or row["claim_id"] != binding.claim_id or row["claim_digest"] != binding.claim_digest
                or row["run_handle"] != binding.run_handle or row["attempt"] != binding.attempt
                or entry.source != "host" or entry.content_digest != delivery.record_content_digest
                or checked["record_type"] != "dispatch_result" or checked["run_id"] != binding.run_id
                or checked["result_digest"] != delivery.result_digest
                or (row["receipt_digest"] is not None and not matches(row["receipt_digest"], _DIGEST))
            )
        except HistoryError as exc:
            invalid = True
            if exc.reason_code == "history_unavailable":
                reason = "history_unavailable"
        except (ContractError, KeyError, TypeError, ValueError, RecursionError, OverflowError):
            invalid = True
        if invalid:
            raise HistoryError(reason)
        return delivery

    def _insert_terminal(self, delivery: TerminalDelivery, binding: bytes) -> None:
        self._connection.execute(
            "INSERT INTO terminal_outbox VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (self.project_id, delivery.binding.claim_id, delivery.delivery_sequence,
             delivery.binding.run_handle, delivery.binding.attempt, delivery.binding.claim_digest, binding,
             delivery.record_id, delivery.record_content_digest, delivery.result_digest),
        )

    @_operation
    def commit_terminal(self, content: bytes, binding: AttemptBinding, *, ingested_at_ms: int) -> TerminalDelivery:
        checked = self._checked_record(content, "host", ingested_at_ms)
        if type(binding) is not AttemptBinding:
            raise HistoryError("history_invalid_input")
        encoded_binding = canonical_json(binding.as_data())
        if (
            binding.project_id != self.project_id or checked["record_type"] != "dispatch_result"
            or checked["run_id"] != binding.run_id
        ):
            raise HistoryError("history_invalid_input")
        digest = hashlib.sha256(content).hexdigest()
        with self._transaction():
            reserved = self._connection.execute(
                "SELECT * FROM result_reservations WHERE project_id=? AND "
                "(claim_id=? OR (run_handle=? AND attempt=?) OR claim_digest=?) LIMIT 4",
                (self.project_id, binding.claim_id, binding.run_handle, binding.attempt, binding.claim_digest),
            ).fetchall()
            if reserved:
                if len(reserved) != 1:
                    raise HistoryError("history_conflict")
                reservation = self._reservation(reserved[0])
                if reservation.binding != binding:
                    raise HistoryError("history_conflict")
                if reservation.terminal_record_id is None:
                    raise HistoryError("history_result_not_ready")
            return self._commit_terminal_locked(content, checked, binding, encoded_binding, digest, ingested_at_ms)

    def _commit_terminal_locked(self, content, checked, binding, encoded_binding, digest, ingested_at_ms):
        existing = self._connection.execute(
            "SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
            (self.project_id, binding.claim_id),
        ).fetchone()
        if existing is not None:
            delivery = self._delivery(existing)
            if canonical_json(delivery.binding.as_data()) != encoded_binding or delivery.record_content_digest != digest:
                raise HistoryError("history_conflict")
            return delivery
        sequence = self._next_sequence(terminal=True)
        entry = self._append_locked(checked, content, "host", ingested_at_ms)
        delivery = TerminalDelivery(sequence, binding, entry.record_id, entry.content_digest, checked["result_digest"])
        self._insert_terminal(delivery, encoded_binding)
        self._connection.execute("UPDATE history_identity SET next_delivery_sequence=?", (sequence + 1,))
        return delivery

    def _result_now(self) -> int:
        if self._result_key is None or self._result_capacity is None:
            raise HistoryError("history_result_unavailable")
        clock_failed = False
        try:
            now = self._result_clock()
        except Exception:
            clock_failed = True
        if clock_failed:
            raise HistoryError("history_result_unavailable")
        if not valid_integer(now, maximum=MAX_SEQUENCE - MAX_RESULT_RETENTION_MS):
            raise HistoryError("history_result_unavailable")
        row = self._connection.execute("SELECT * FROM result_identity WHERE project_id=?", (self.project_id,)).fetchone()
        check = keyed_digest(self._result_key, "project-result-key-v1:" + self.project_id)
        if (row is None or not matches(row["key_check"], _DIGEST)
                or not secrets.compare_digest(row["key_check"], check)
                or not valid_integer(row["last_observed_at_ms"])):
            raise HistoryError("history_incompatible")
        if now < row["last_observed_at_ms"]:
            raise HistoryError("history_result_unavailable")
        # Persist the maximum trusted time before fallible I/O. A restart or
        # backward wall-clock adjustment cannot resurrect an expired result.
        self._connection.execute("UPDATE result_identity SET last_observed_at_ms=? WHERE project_id=?", (now, self.project_id))
        return now

    def _result_binding(self, binding: AttemptBinding) -> AttemptBinding:
        if type(binding) is not AttemptBinding:
            raise HistoryError("history_invalid_input")
        captured = AttemptBinding(**binding.as_data())
        if captured.project_id != self.project_id or len(canonical_json(captured.as_data())) > MAX_BINDING_BYTES:
            raise HistoryError("history_invalid_input")
        return captured

    def _reservation(self, row) -> ResultReservation:
        invalid = False
        try:
            encoded = self._stored_blob(row["binding"], maximum=MAX_BINDING_BYTES)
            binding = AttemptBinding(**parse_bounded_json(encoded))
            value = ResultReservation(binding, row["assignment_digest"], row["created_at_ms"], row["hard_deadline_at_ms"],
                                      row["reserved_until_ms"], row["expires_at_ms"], row["state"],
                                      row["prepared_at_ms"], row["retained_until_ms"], row["committed_at_ms"],
                                      row["body_size"], row["result_digest"], row["terminal_record_id"])
            invalid = (
                encoded != canonical_json(binding.as_data()) or row["project_id"] != self.project_id
                or binding.project_id != self.project_id or row["claim_id"] != binding.claim_id
                or row["run_handle"] != binding.run_handle or row["attempt"] != binding.attempt
                or row["claim_digest"] != binding.claim_digest or row["orchestration_handle"] != binding.orchestration_handle
                or (row["prepared_digest"] is not None and not matches(row["prepared_digest"], _DIGEST))
                or ((row["prepared_digest"] is None) != (value.prepared_at_ms is None))
                or (value.committed_at_ms is not None and row["prepared_digest"] is None)
            )
            if value.committed_at_ms is not None:
                terminal = self._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                                    (self.project_id, binding.claim_id)).fetchone()
                if terminal is None:
                    invalid = True
                else:
                    delivery = self._delivery(terminal)
                    invalid |= (delivery.binding != binding or delivery.result_digest != value.result_digest
                                or delivery.record_id != value.terminal_record_id)
        except (HistoryError, ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            invalid = True
        if invalid:
            raise HistoryError("history_incompatible")
        return value

    def _result_row(self, binding: AttemptBinding):
        row = self._connection.execute("SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?",
                                       (self.project_id, binding.claim_id)).fetchone()
        if row is None:
            raise HistoryError("history_result_not_reserved")
        value = self._reservation(row)
        if value.binding != binding:
            raise HistoryError("history_conflict")
        return row, value

    def _live_result(self, binding: AttemptBinding, now: int):
        row, value = self._result_row(binding)
        if value.state in {"retiring", "expired"} or now >= value.expires_at_ms:
            self._retire_result(binding)
            raise HistoryError("history_result_expired")
        return row, value

    @_operation
    def reserve_result(self, binding: AttemptBinding, assignment: dict, *, hard_deadline_at_ms: int) -> ResultReservation:
        """Reserve storage before launch; this creates no launch/admission authority.

        The service must independently prove the exact assignment, actual deadline
        and complete claim before this call, and reserve before consuming launch.
        """
        binding = self._result_binding(binding)
        encoded_assignment = assignment_bytes(assignment, binding)
        now = self._result_now()
        if not valid_integer(hard_deadline_at_ms):
            raise HistoryError("history_invalid_input")
        digest = keyed_digest(self._result_key, encoded_assignment)
        existing = self._connection.execute("SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?",
                                            (self.project_id, binding.claim_id)).fetchone()
        if existing is not None:
            _, value = self._live_result(binding, now)
            if value.assignment_digest != digest or value.hard_deadline_at_ms != hard_deadline_at_ms:
                raise HistoryError("history_conflict")
            return value
        if not now < hard_deadline_at_ms < now + self._result_capacity.reservation_ms:
            raise HistoryError("history_result_capacity")
        value = ResultReservation(binding, digest, now, hard_deadline_at_ms,
                                  now + self._result_capacity.reservation_ms,
                                  now + self._result_capacity.reservation_ms, "reserved")
        with self._transaction():
            if self._connection.execute("SELECT 1 FROM terminal_outbox WHERE project_id=? AND "
                                        "(claim_id=? OR (run_handle=? AND attempt=?) OR claim_digest=?)",
                                        (self.project_id, binding.claim_id, binding.run_handle,
                                         binding.attempt, binding.claim_digest)).fetchone() is not None:
                raise HistoryError("history_conflict")
            counts = self._connection.execute(
                "SELECT COUNT(*),SUM(CASE WHEN state!='expired' THEN 1 ELSE 0 END) "
                "FROM result_reservations WHERE project_id=?", (self.project_id,),
            ).fetchone()
            if (counts[0] >= self._result_capacity.max_entries
                    or ((counts[1] or 0) + 1) * MAX_RESULT_BODY_BYTES > self._result_capacity.max_reserved_bytes):
                raise HistoryError("history_result_capacity")
            self._connection.execute(
                "INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,orchestration_handle,"
                "binding,assignment_digest,created_at_ms,hard_deadline_at_ms,reserved_until_ms,expires_at_ms,state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.project_id, binding.claim_id, binding.run_handle, binding.attempt, binding.claim_digest,
                 binding.orchestration_handle, canonical_json(binding.as_data()), digest, now, hard_deadline_at_ms,
                 value.reserved_until_ms, value.expires_at_ms, value.state),
            )
        return value

    def _result_names(self, binding: AttemptBinding) -> tuple[str, str]:
        identity = keyed_digest(self._result_key, b"project-result-artifact-v1\0" + canonical_json(binding.as_data()))
        return f".result-{identity}.partial", f".result-{identity}.json"

    @staticmethod
    def _result_file_info(info, *, links: tuple[int, ...] = (1,)) -> None:
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or not stat.S_ISREG(info.st_mode) or info.st_nlink not in links
                or not 0 <= info.st_size <= MAX_RESULT_BODY_BYTES):
            raise HistoryError("history_unsafe_path")

    def _recover_result_link(self, binding: AttemptBinding) -> None:
        partial, final = self._result_names(binding)
        try:
            left = os.stat(partial, dir_fd=self._directory_fd, follow_symlinks=False)
            right = os.stat(final, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        self._result_file_info(left, links=(2,))
        self._result_file_info(right, links=(2,))
        if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
            raise HistoryError("history_unsafe_path")
        # Only the exact two fixed names of our interrupted atomic publication
        # may have two links. An unrelated hard link is always refused.
        os.unlink(partial, dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)

    def _read_result_file(self, binding: AttemptBinding, digest: str) -> bytes:
        self._recover_result_link(binding)
        _, name = self._result_names(binding)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
        except FileNotFoundError:
            raise HistoryError("history_result_unavailable") from None
        with os.fdopen(fd, "rb") as stream:
            self._result_file_info(os.fstat(stream.fileno()))
            body = stream.read(MAX_RESULT_BODY_BYTES + 1)
            self._result_file_info(os.fstat(stream.fileno()))
        if (not 0 < len(body) <= MAX_RESULT_BODY_BYTES
                or not secrets.compare_digest(keyed_digest(self._result_key, body), digest)):
            raise HistoryError("history_result_unavailable")
        return body

    def _write_result_file(self, binding: AttemptBinding, body: bytes, digest: str) -> None:
        partial, final = self._result_names(binding)
        self._recover_result_link(binding)
        try:
            os.stat(final, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if self._read_result_file(binding, digest) != body:
                raise HistoryError("history_conflict")
            os.fsync(self._directory_fd)
            return
        try:
            info = os.stat(partial, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self._result_file_info(info)
            os.unlink(partial, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
        try:
            os.fchmod(fd, 0o600)
            self._result_file_info(os.fstat(fd))
            pending = memoryview(body)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError("result write failed")
                pending = pending[written:]
            self._result_file_info(os.fstat(fd))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(partial, final, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd, follow_symlinks=False)
        os.unlink(partial, dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)

    @_operation
    def commit_result(
        self, content: bytes, binding: AttemptBinding, assignment: dict, body: bytes,
    ) -> TerminalDelivery:
        """Keep validated bytes before returning terminal metadata; never relaunch.

        A prepared immutable request is durable before artifact I/O. File-before-
        transaction crashes retain the reservation and exact retry identity. The
        result reference, terminal record and outbox commit in one transaction.
        """
        binding = self._result_binding(binding)
        encoded_assignment = assignment_bytes(assignment, binding)
        snapshot = parse_bounded_json(encoded_assignment, max_bytes=MAX_ASSIGNMENT_CANONICAL_BYTES)
        now = self._result_now()
        checked = self._checked_record(content, "host", now)
        if checked["record_type"] != "dispatch_result" or checked["run_id"] != binding.run_id:
            raise HistoryError("history_invalid_input")
        body = checked_result_body(body, snapshot, checked)
        digest = keyed_digest(self._result_key, body)
        if not secrets.compare_digest(digest, checked["result_digest"]):
            raise HistoryError("history_invalid_input")
        row, value = self._live_result(binding, now)
        if value.assignment_digest != keyed_digest(self._result_key, encoded_assignment):
            raise HistoryError("history_conflict")
        prepared = keyed_digest(self._result_key, canonical_json({
            "binding": binding.as_data(), "record": checked, "result_digest": digest,
            "assignment_digest": value.assignment_digest,
        }))
        if row["prepared_digest"] is not None and not secrets.compare_digest(row["prepared_digest"], prepared):
            raise HistoryError("history_conflict")
        if value.state == "committed":
            if self.read_result(binding) != body:
                raise HistoryError("history_conflict")
            terminal = self._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                                (self.project_id, binding.claim_id)).fetchone()
            return self._delivery(terminal)
        if row["prepared_digest"] is None:
            with self._transaction():
                # Retention begins before the first possible private body write,
                # even if publication/metadata commit fails. Retry and changed
                # deployment policy cannot extend these original timestamps.
                self._connection.execute(
                    "UPDATE result_reservations SET prepared_digest=?,prepared_at_ms=?,retained_until_ms=?,"
                    "expires_at_ms=MIN(reserved_until_ms,?) WHERE project_id=? AND claim_id=?",
                    (prepared, now, now + self._result_capacity.retention_ms,
                     now + self._result_capacity.retention_ms, self.project_id, binding.claim_id),
                )
        self._write_result_file(binding, body, digest)
        now = self._result_now()
        row, value = self._live_result(binding, now)
        if row["prepared_digest"] != prepared or value.state != "reserved":
            raise HistoryError("history_conflict")
        with self._transaction():
            delivery = self._commit_terminal_locked(content, checked, binding, canonical_json(binding.as_data()),
                                                    hashlib.sha256(content).hexdigest(), now)
            self._connection.execute(
                "UPDATE result_reservations SET state='committed',committed_at_ms=?,expires_at_ms=?,"
                "body_size=?,result_digest=?,terminal_record_id=? WHERE project_id=? AND claim_id=?",
                (now, value.retained_until_ms, len(body), digest, delivery.record_id,
                 self.project_id, binding.claim_id),
            )
        return delivery

    @_operation
    def read_result(self, binding: AttemptBinding) -> bytes:
        binding = self._result_binding(binding)
        _, value = self._live_result(binding, self._result_now())
        if value.state != "committed":
            raise HistoryError("history_result_not_ready")
        body = self._read_result_file(binding, value.result_digest)
        _, current = self._live_result(binding, self._result_now())
        if len(body) != current.body_size:
            raise HistoryError("history_incompatible")
        return body

    @_operation
    def result_reservation(self, binding: AttemptBinding) -> ResultReservation:
        binding = self._result_binding(binding)
        now = self._result_now()
        _, value = self._result_row(binding)
        if value.state == "retiring" or (value.state != "expired" and now >= value.expires_at_ms):
            self._retire_result(binding)
            _, value = self._result_row(binding)
        return value

    def _retire_result(self, binding: AttemptBinding) -> None:
        _, value = self._result_row(binding)
        if value.state == "expired":
            return
        with self._transaction():
            self._connection.execute("UPDATE result_reservations SET state='retiring' WHERE project_id=? AND claim_id=?",
                                     (self.project_id, binding.claim_id))
        self._recover_result_link(binding)
        for name in self._result_names(binding):
            try:
                info = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            self._result_file_info(info)
            os.unlink(name, dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)
        with self._transaction():
            self._connection.execute("UPDATE result_reservations SET state='expired' WHERE project_id=? AND claim_id=?",
                                     (self.project_id, binding.claim_id))

    @_operation
    def retire_result(self, binding: AttemptBinding) -> None:
        """Trusted lifecycle owner only, including actual orchestration close.

        Tombstones survive deletion. This releases only proven deleted body
        capacity, never an account claim or an opportunity to execute again.
        """
        binding = self._result_binding(binding)
        self._result_now()
        self._retire_result(binding)

    @_operation
    def expire_results(self, *, limit: int = 10) -> int:
        now = self._result_now()
        if not valid_integer(limit, minimum=1, maximum=100):
            raise HistoryError("history_invalid_input")
        rows = self._connection.execute(
            "SELECT * FROM result_reservations WHERE project_id=? AND state!='expired' "
            "AND (expires_at_ms<=? OR state='retiring') ORDER BY created_at_ms,claim_id LIMIT ?",
            (self.project_id, now, limit),
        ).fetchall()
        for row in rows:
            self._retire_result(self._reservation(row).binding)
        return len(rows)

    @_operation
    def pending_terminals(self, *, after_sequence: int = 0, limit: int = 10) -> tuple[TerminalDelivery, ...]:
        if not valid_integer(after_sequence) or not valid_integer(limit, minimum=1, maximum=100):
            raise HistoryError("history_invalid_input")
        rows = self._connection.execute(
            "SELECT * FROM terminal_outbox WHERE project_id=? AND receipt_digest IS NULL "
            "AND delivery_sequence>? ORDER BY delivery_sequence LIMIT ?",
            (self.project_id, after_sequence, limit),
        )
        page = []
        for row in rows:
            delivery = self._delivery(row)
            if len(canonical_json([item.as_data() for item in (*page, delivery)])) + 1 > MAX_DELIVERY_PAGE_BYTES:
                break
            page.append(delivery)
        return tuple(page)

    @_operation
    def acknowledge_terminal(
        self, claim_id: str, record_content_digest: str, receipt_digest: str, *, runtime_digest: str,
    ) -> None:
        """Persist an already verified account receipt, not a caller's claimed receipt.

        The service must verify account/claim/result binding before this internal
        call. Ordinary ACKs cannot retire an older runtime's delivery; recovery
        needs a separate, explicitly authorized protocol. This store cannot release
        account capacity or authenticate a receipt.
        """
        if not matches(claim_id, _ID) or not all(
            matches(value, _DIGEST) for value in (record_content_digest, receipt_digest, runtime_digest)
        ):
            raise HistoryError("history_invalid_input")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                (self.project_id, claim_id),
            ).fetchone()
            if row is None:
                raise HistoryError("history_not_found")
            delivery = self._delivery(row)
            if delivery.binding.runtime_digest != runtime_digest:
                raise HistoryError("history_not_authorized")
            if row["record_content_digest"] != record_content_digest or row["receipt_digest"] not in (None, receipt_digest):
                raise HistoryError("history_conflict")
            self._connection.execute(
                "UPDATE terminal_outbox SET receipt_digest=? WHERE project_id=? AND claim_id=?",
                (receipt_digest, self.project_id, claim_id),
            )
