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
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .contracts import ContractError, validate_task
from .claim_storage import MAX_STORAGE_EVIDENCE_BYTES, encode_storage_evidence, lease_data, lease_receipt_digest, lease_state_seal
from .project_cleanup import CLEANUP_COLUMNS, CLEANUP_SCHEMA, CLEANUP_SCHEMA_V9, check_cleanup_reservation, reserve_cleanup
from .project_release_ack import ACK_COLUMNS, ACK_SCHEMA
from .execution_grant import MAX_EXECUTION_GRANT_BYTES, verify_execution_grant
from .history_contracts import (
    MAX_SEQUENCE, AttemptBinding, HistoryEntry, HistoryError, TerminalDelivery,
    _DIGEST, _ID, _PROJECT, matches, valid_integer,
)
from .host_result_contracts import HostResultReference, decode_host_result_reference
from .orchestration_api import OrchestrationLimits
from .orchestration_schema import ORCHESTRATION_SCHEMA, ORCHESTRATION_COLUMNS
from .project_qa_checks import CHECK_SCHEMA, CHECK_COLUMNS
from .project_qa_commit import QA_SCHEMA, QA_COLUMNS
from .project_learning_corpus import LEARNING_SCHEMA, LEARNING_COLUMNS
from .learning_contracts import (
    DISPATCH_RESULT_SCHEMA_VERSION, MAX_RECORD_BYTES, SOURCES, canonical_json, content_digest,
    keyed_digest, parse_bounded_json, validate_execution_configuration, validate_learning_record,
)
from .result_contracts import (
    MAX_ASSIGNMENT_CANONICAL_BYTES, MAX_RESULT_BODY_BYTES, MAX_RESULT_RETENTION_MS,
    CodexUsageObservation, ResultCapacity, ResultLease, ResultLeaseBinding, ResultObservation, ResultReservation,
    assignment_bytes, checked_result_body,
)

_APPLICATION_ID = 0x43444831
_SCHEMA_VERSION = 11
_RECORD_TYPES = frozenset({"event", "dispatch_intent", "dispatch_result", "outcome", "verification_binding",
                           "host_result_admission", "provider_usage", "observation_unavailable", "execution_provenance",
                           "host_open_provenance"})
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
_SCHEMA_V2 = _SCHEMA_V1 + _RESULT_SCHEMA
_LEASE_SCHEMA = """
CREATE TABLE result_leases(
 project_id TEXT NOT NULL, run_handle TEXT NOT NULL, attempt INTEGER NOT NULL,
 claim_request_id TEXT NOT NULL, binding BLOB NOT NULL, assignment_digest TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL, reserved_until_ms INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','claim_pending','bound','expired')),
 lease_digest TEXT NOT NULL, claim_id TEXT, claim_digest TEXT,
 hard_deadline_at_ms INTEGER, claim_binding_digest TEXT, state_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt), UNIQUE(project_id,claim_request_id), UNIQUE(project_id,claim_id),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id),
 FOREIGN KEY(project_id,claim_id) REFERENCES result_reservations(project_id,claim_id));
CREATE INDEX lease_expiry ON result_leases(project_id,state,reserved_until_ms);
"""
_SCHEMA_V3 = _SCHEMA_V2 + _LEASE_SCHEMA
_EXECUTION_SCHEMA = """
CREATE TABLE project_execution_consumptions(
 project_id TEXT NOT NULL, claim_id TEXT NOT NULL, registration_digest TEXT NOT NULL,
 grant BLOB NOT NULL, consumed_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,claim_id), UNIQUE(project_id,registration_digest),
 FOREIGN KEY(project_id,claim_id) REFERENCES result_reservations(project_id,claim_id));
"""
_SCHEMA_V4 = _SCHEMA_V3 + _EXECUTION_SCHEMA
_SCHEMA_V5 = _SCHEMA_V4 + ORCHESTRATION_SCHEMA
_HOST_OPEN_SCHEMA = """
CREATE TABLE project_host_opens(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
 binding BLOB NOT NULL,record_id TEXT NOT NULL,record_content_digest TEXT NOT NULL,
 source_sequence INTEGER NOT NULL,ingested_at_ms INTEGER NOT NULL,
 late INTEGER NOT NULL CHECK(late IN (0,1)),journal_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt),UNIQUE(project_id,record_id),
 FOREIGN KEY(project_id,run_handle,attempt) REFERENCES result_leases(project_id,run_handle,attempt),
 FOREIGN KEY(project_id,record_id) REFERENCES records(project_id,record_id));
"""
_SCHEMA_V6 = _SCHEMA_V5 + _HOST_OPEN_SCHEMA
_SCHEMA_V7 = _SCHEMA_V6 + CHECK_SCHEMA
_SCHEMA_V8 = _SCHEMA_V7 + QA_SCHEMA
_SCHEMA_V9 = _SCHEMA_V8.replace('state_digest TEXT NOT NULL,',
                            'state_digest TEXT NOT NULL, cleanup_reservation_digest TEXT,') + CLEANUP_SCHEMA_V9
_SCHEMA_V10 = _SCHEMA_V8.replace('state_digest TEXT NOT NULL,',
                            'state_digest TEXT NOT NULL, cleanup_reservation_digest TEXT,') + CLEANUP_SCHEMA + ACK_SCHEMA
_SCHEMA = _SCHEMA_V10 + LEARNING_SCHEMA
_COLUMNS = {
    **LEARNING_COLUMNS,
    **ACK_COLUMNS,
    **CLEANUP_COLUMNS,
    **ORCHESTRATION_COLUMNS,
    **CHECK_COLUMNS,
    **QA_COLUMNS,
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
    "result_leases": ("project_id", "run_handle", "attempt", "claim_request_id", "binding", "assignment_digest",
                      "created_at_ms", "reserved_until_ms", "state", "lease_digest", "claim_id", "claim_digest",
                      "hard_deadline_at_ms", "claim_binding_digest", "state_digest", "cleanup_reservation_digest"),
    "project_execution_consumptions": ("project_id", "claim_id", "registration_digest", "grant", "consumed_at_ms"),
    "project_host_opens": ("project_id", "run_handle", "attempt", "binding", "record_id", "record_content_digest",
                           "source_sequence", "ingested_at_ms", "late", "journal_seal"),
}


class _ExecutionProvenanceRejected(HistoryError):
    """Writer-local proof rejection, before either consumption row is written.

    Only this definite pre-write outcome permits replacing rejected proof in
    the original live lane. It grants no retry after an uncertain write.
    """

    def __init__(self):
        super().__init__('history_execution_not_ready')


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
        orchestration_limits: OrchestrationLimits | None = None,
        allow_legacy_orchestration_reference: bool = False,
    ) -> None:
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._closed = False
        self._connection = None
        self._directory_fd = self._writer_fd = None
        self._result_key = None
        self._result_capacity = None
        self._result_expiry_cursor: int | None = None
        self._lease_expiry_cursor: int | None = None
        self._result_clock = result_clock_ms if result_clock_ms is not None else (lambda: time.time_ns() // 1_000_000)
        if (
            type(allow_schema_upgrade) is not bool
            or type(allow_legacy_orchestration_reference) is not bool
            or (orchestration_limits is not None and type(orchestration_limits) is not OrchestrationLimits)
            or (result_key is None) != (result_capacity is None)
            or (result_key is not None and (type(result_key) is not bytes or len(result_key) != 32))
            or (result_capacity is not None and type(result_capacity) is not ResultCapacity)
            or (result_clock_ms is not None and not callable(result_clock_ms))
        ):
            raise HistoryError("history_invalid_input")
        self._allow_schema_upgrade = allow_schema_upgrade
        self._allow_legacy_orchestration_reference = allow_legacy_orchestration_reference
        self._orchestration_limits = (None if orchestration_limits is None else
                                      OrchestrationLimits(orchestration_limits.max_orchestrations, orchestration_limits.max_events))
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
        except BaseException:
            # Interrupted initialization must not strand the original writer
            # lock/connection. Preserve the original nonlocal interruption.
            with contextlib.suppress(Exception):
                self.close()
            raise
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

    def _orchestration_owner(self):
        # Lazy import keeps the protocol/client free of persistence imports.
        from .orchestration_store import OrchestrationOwner
        return OrchestrationOwner(self)

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
        elif (version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10} and application == _APPLICATION_ID
              and actual_schema == schema_set({1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3, 4: _SCHEMA_V4,
                                               5: _SCHEMA_V5, 6: _SCHEMA_V6, 7: _SCHEMA_V7, 8: _SCHEMA_V8,
                                               9: _SCHEMA_V9, 10: _SCHEMA_V10}[version])):
            if not self._allow_schema_upgrade:
                # Only the separately authorized, drained persistence-owner
                # upgrade path may opt in; ordinary API startup never does.
                raise HistoryError("history_incompatible")
            # Validate the complete old schema and project identity before any
            # fixed migration write. No unknown trigger/schema is ever executed.
            omitted = {*LEARNING_COLUMNS, *(ACK_COLUMNS if version < 10 else set()), *(CLEANUP_COLUMNS if version < 9 else set()), *(QA_COLUMNS if version < 8 else set()), *(CHECK_COLUMNS if version < 7 else set()), *({"project_host_opens"} if version < 6 else set()), *(ORCHESTRATION_COLUMNS if version < 5 else set()), *({"project_execution_consumptions"} if version < 4 else set()), *({"result_leases"} if version < 3 else set()),
                       *({"result_identity", "result_reservations"} if version == 1 else set())}
            old_columns = {name: (columns[:-1] if ((name == 'result_leases' and version < 9)
                                                 or (name == 'project_cleanup_reservations' and version == 9)) else columns)
                           for name, columns in _COLUMNS.items() if name not in omitted}
            self._check_schema_identity(old_columns)
            with self._transaction():
                additions = ((_RESULT_SCHEMA if version == 1 else "") + (_LEASE_SCHEMA if version < 3 else "")
                             + (_EXECUTION_SCHEMA if version < 4 else "")
                             + (ORCHESTRATION_SCHEMA if version < 5 else "")
                             + (_HOST_OPEN_SCHEMA if version < 6 else "")
                             + (CHECK_SCHEMA if version < 7 else "") + (QA_SCHEMA if version < 8 else ""))
                for statement in additions.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                if version < 9:
                    connection.execute('ALTER TABLE result_leases ADD COLUMN cleanup_reservation_digest TEXT')
                elif version == 9:
                    connection.execute('ALTER TABLE project_cleanup_reservations ADD COLUMN account_release_digest TEXT')
                for statement in ((CLEANUP_SCHEMA if version < 9 else '') + (ACK_SCHEMA if version < 10 else '')
                                  + LEARNING_SCHEMA).split(';'):
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
        if checked["record_type"] in {"dispatch_result", "host_result_admission", "provider_usage", "observation_unavailable", "execution_provenance", "host_open_provenance"}:
            # Closed business writers own terminal, usage and admission records.
            raise HistoryError("history_invalid_input")
        if checked['record_type'] == 'outcome' and source == 'verifier':
            raise HistoryError('history_invalid_input')
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
            leased = self._connection.execute("SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?",
                                              (self.project_id, binding.run_handle, binding.attempt)).fetchone()
            if leased is not None:
                value = self._lease(leased)
                if value.state != "bound":
                    raise HistoryError("history_result_not_ready")
                if value.claim_id != binding.claim_id or value.claim_digest != binding.claim_digest:
                    raise HistoryError("history_conflict")
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
        reason = "history_incompatible"
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
        except HistoryError as error:
            invalid = True
            if error.reason_code in {"history_unavailable", "history_result_unavailable"}:
                reason = error.reason_code
        except (ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            invalid = True
        if invalid:
            raise HistoryError(reason)
        return value

    def _result_row(self, binding: AttemptBinding):
        row = self._connection.execute("SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?",
                                       (self.project_id, binding.claim_id)).fetchone()
        if row is None:
            raise HistoryError("history_result_not_reserved")
        value = self._reservation(row)
        if value.binding != binding:
            raise HistoryError("history_conflict")
        leased = self._connection.execute("SELECT * FROM result_leases WHERE project_id=? AND claim_id=?",
                                          (self.project_id, binding.claim_id)).fetchone()
        if leased is not None:
            self._lease(leased)
        return row, value

    def _live_result(self, binding: AttemptBinding, now: int):
        row, value = self._result_row(binding)
        if value.state in {"retiring", "expired"} or now >= value.expires_at_ms:
            self._retire_result(binding)
            raise HistoryError("history_result_expired")
        return row, value

    def _lease_binding(self, binding):
        if type(binding) is not ResultLeaseBinding:
            raise HistoryError("history_invalid_input")
        binding = ResultLeaseBinding(**binding.as_data())
        if binding.project_id != self.project_id or len(canonical_json(binding.as_data())) > MAX_BINDING_BYTES:
            raise HistoryError("history_invalid_input")
        return binding

    def _lease_digest(self, binding, assignment_digest, created, until):
        return lease_receipt_digest(self._result_key, binding, assignment_digest, created, until)

    def _lease_seal(self, value):
        return lease_state_seal(self._result_key, value)

    def _claim_lease_digest(self, value, binding, deadline):
        return keyed_digest(self._result_key, canonical_json({
            "domain": "project-result-claim-binding-v1", "lease_digest": value.lease_digest,
            "binding": binding.as_data(), "hard_deadline_at_ms": deadline,
        }))

    def _lease(self, row):
        invalid = False
        try:
            encoded = self._stored_blob(row["binding"], maximum=MAX_BINDING_BYTES)
            binding = ResultLeaseBinding(**parse_bounded_json(encoded, max_bytes=MAX_BINDING_BYTES))
            value = ResultLease(binding, *[row[name] for name in (
                "assignment_digest", "created_at_ms", "reserved_until_ms", "state", "lease_digest",
                "claim_id", "claim_digest", "hard_deadline_at_ms", "claim_binding_digest", "cleanup_reservation_digest")])
            invalid = (
                encoded != canonical_json(binding.as_data()) or binding.project_id != self.project_id
                or row["project_id"] != self.project_id or row["run_handle"] != binding.run_handle
                or row["attempt"] != binding.attempt or row["claim_request_id"] != binding.claim_request_id
                or not secrets.compare_digest(value.lease_digest, self._lease_digest(
                    binding, value.assignment_digest, value.created_at_ms, value.reserved_until_ms))
                or not matches(row["state_digest"], _DIGEST)
                or not secrets.compare_digest(row["state_digest"], self._lease_seal(value))
            )
            if value.state == "bound":
                parent = self._connection.execute("SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?",
                                                  (self.project_id, value.claim_id)).fetchone()
                if parent is None:
                    raise HistoryError("history_incompatible")
                actual = self._reservation(parent)
                invalid |= (
                    not binding.matches_attempt(actual.binding) or actual.binding.claim_digest != value.claim_digest
                    or actual.assignment_digest != value.assignment_digest or actual.created_at_ms != value.created_at_ms
                    or actual.reserved_until_ms != value.reserved_until_ms
                    or actual.hard_deadline_at_ms != value.hard_deadline_at_ms
                    or not secrets.compare_digest(value.claim_binding_digest, self._claim_lease_digest(
                        value, actual.binding, actual.hard_deadline_at_ms))
                )
        except (HistoryError, ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            invalid = True
        if invalid:
            raise HistoryError("history_incompatible")
        check_cleanup_reservation(self, value)
        return value

    def _lease_row(self, binding):
        row = self._connection.execute("SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?",
                                       (self.project_id, binding.run_handle, binding.attempt)).fetchone()
        if row is None:
            raise HistoryError("history_result_not_reserved")
        value = self._lease(row)
        if value.binding != binding:
            raise HistoryError("history_conflict")
        return value

    def _set_lease(self, value):
        self._connection.execute(
            "UPDATE result_leases SET state=?,claim_id=?,claim_digest=?,hard_deadline_at_ms=?,"
            "claim_binding_digest=?,state_digest=? WHERE project_id=? AND run_handle=? AND attempt=?",
            (value.state, value.claim_id, value.claim_digest, value.hard_deadline_at_ms,
             value.claim_binding_digest, self._lease_seal(value), self.project_id,
             value.binding.run_handle, value.binding.attempt),
        )

    def _check_result_capacity(self):
        counts = self._connection.execute(
            "SELECT COUNT(*),SUM(CASE WHEN state!='expired' THEN 1 ELSE 0 END) "
            "FROM result_reservations WHERE project_id=?", (self.project_id,),
        ).fetchone()
        entries, charged = counts[0], counts[1] or 0
        # Verify each bounded lease before trusting mutable state to exclude a
        # tombstone or bound row. SQL labels alone cannot free reserved space.
        positions = self._connection.execute("SELECT rowid FROM result_leases WHERE project_id=? LIMIT ?",
                                             (self.project_id, self._result_capacity.max_entries + 1)).fetchall()
        if len(positions) > self._result_capacity.max_entries:
            raise HistoryError("history_result_capacity")
        for position in positions:
            row = self._connection.execute("SELECT * FROM result_leases WHERE rowid=?", (position[0],)).fetchone()
            value = self._lease(row)
            if value.state != "bound":
                entries += 1
                charged += value.state != "expired"
        if (entries >= self._result_capacity.max_entries
                or (charged + 1) * MAX_RESULT_BODY_BYTES > self._result_capacity.max_reserved_bytes):
            raise HistoryError("history_result_capacity")

    def _insert_result_reservation(self, value):
        binding = value.binding
        self._connection.execute(
            "INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,orchestration_handle,"
            "binding,assignment_digest,created_at_ms,hard_deadline_at_ms,reserved_until_ms,expires_at_ms,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.project_id, binding.claim_id, binding.run_handle, binding.attempt, binding.claim_digest,
             binding.orchestration_handle, canonical_json(binding.as_data()), value.assignment_digest,
             value.created_at_ms, value.hard_deadline_at_ms, value.reserved_until_ms, value.expires_at_ms, value.state),
        )

    @_operation
    def reserve_result_lease(self, binding: ResultLeaseBinding, assignment: dict) -> ResultLease:
        """Charge durable body capacity BEFORE consuming any account claim.

        This is storage metadata, not authentication/admission. The future fixed
        account adapter must check the actual protected lease and full request.
        No placeholder bridge/claim ID or result is created here.
        """
        binding = self._lease_binding(binding)
        encoded = assignment_bytes(assignment, binding)
        now = self._result_now()
        digest = keyed_digest(self._result_key, encoded)
        with self._transaction():
            previous = self._connection.execute("SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?",
                                                (self.project_id, binding.run_handle, binding.attempt)).fetchone()
            if previous is not None:
                value = self._lease(previous)
                if value.binding != binding or value.assignment_digest != digest:
                    raise HistoryError("history_conflict")
                return value  # Never refresh, reuse or revive a known lease.
            self._orchestration_owner().active_for(binding)
            if (self._connection.execute("SELECT 1 FROM result_reservations WHERE project_id=? AND run_handle=? AND attempt=?",
                                         (self.project_id, binding.run_handle, binding.attempt)).fetchone() is not None
                    or self._connection.execute("SELECT 1 FROM terminal_outbox WHERE project_id=? AND run_handle=? AND attempt=?",
                                                (self.project_id, binding.run_handle, binding.attempt)).fetchone() is not None):
                raise HistoryError("history_conflict")
            self._check_result_capacity()
            until = now + self._result_capacity.reservation_ms
            value = ResultLease(binding, digest, now, until, "reserved", self._lease_digest(binding, digest, now, until))
            self._connection.execute("INSERT INTO result_leases VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)",
                                     (self.project_id, binding.run_handle, binding.attempt, binding.claim_request_id,
                                      canonical_json(binding.as_data()), digest, now, until, value.state,
                                      value.lease_digest, self._lease_seal(value), None))
            value = reserve_cleanup(self, value)
            self._connection.execute('UPDATE result_leases SET cleanup_reservation_digest=?,state_digest=? '
                'WHERE project_id=? AND run_handle=? AND attempt=?',
                (value.cleanup_reservation_digest, self._lease_seal(value), self.project_id, binding.run_handle, binding.attempt))
        return value

    @_operation
    def record_host_open(self, binding, request, provenance, *, assignment, task, route, before_commit):
        from .project_host_open import record_host_open
        return record_host_open(self, binding, request, provenance, assignment=assignment,
                                 task=task, route=route, before_commit=before_commit)

    @_operation
    def result_lease(self, binding: ResultLeaseBinding) -> ResultLease:
        binding = self._lease_binding(binding)
        self._result_now()
        return self._lease_row(binding)

    @_operation
    def attest_result_lease(self, binding: ResultLeaseBinding) -> bytes:
        """Server-only fixed evidence for an actual armed protected-store lease.

        Native/IPC enrollment must prove who owns this project writer and the
        account requester. Bytes, HMACs and role/session labels alone do not.
        No body, key, assignment text, path or caller-selected seal is returned.
        """
        binding = self._lease_binding(binding)
        self._orchestration_owner().active_for(binding)
        now = self._result_now()
        value = self._lease_row(binding)
        if now >= value.reserved_until_ms:
            raise HistoryError("history_result_expired")
        return encode_storage_evidence(self._result_key, value)

    @_operation
    def attest_bound_result_lease(self, binding: ResultLeaseBinding) -> bytes:
        """Historical bound metadata only; it asserts no current launch readiness.

        Current account timing and local one-use consumption are checked
        separately. Authentication/enrollment of both service peers is mandatory.
        """
        binding = self._lease_binding(binding)
        if self._result_key is None:
            raise HistoryError("history_result_unavailable")
        value = self._lease_row(binding)
        if value.state != "bound":
            raise HistoryError("history_result_not_ready")
        raw = canonical_json({"protocol_version": 1, "lease": lease_data(value), "seal": self._lease_seal(value)})
        if len(raw) > MAX_STORAGE_EVIDENCE_BYTES:
            raise HistoryError("history_invalid_input")
        return raw

    @_operation
    def attest_pending_result_lease(self, binding: ResultLeaseBinding) -> bytes:
        """Historical armed metadata for reconciliation; no fresh timing grant."""
        binding = self._lease_binding(binding)
        if self._result_key is None:
            raise HistoryError('history_result_unavailable')
        return encode_storage_evidence(self._result_key, self._lease_row(binding))

    @_operation
    def settle_denied_result_lease(self, binding: ResultLeaseBinding, raw: bytes) -> ResultLease:
        """Retire only after an independently authenticated account no-launch fence.

        The MAC is an integrity/binding check, not proof of the native sender.
        Expired is the existing permanent unbound tombstone, not a new attempt.
        No local clock, caller cleanup flag or missing-row inference is used.
        """
        from .claim_denial import verify_denial_closure

        binding = self._lease_binding(binding)
        with self._transaction():
            value = self._lease_row(binding)
            invalid = False
            try:
                verify_denial_closure(self._result_key, value, raw)
            except Exception:
                invalid = True
            if invalid:
                raise HistoryError('history_invalid_input')
            if value.state not in {'claim_pending', 'expired'}:
                raise HistoryError('history_result_not_ready')
            if self._connection.execute('SELECT 1 FROM result_reservations WHERE project_id=? AND run_handle=? AND attempt=? LIMIT 1',
                                         (self.project_id, binding.run_handle, binding.attempt)).fetchone() is not None:
                raise HistoryError('history_conflict')
            if value.state == 'claim_pending':
                value = replace(value, state='expired')
                self._set_lease(value)
        return value

    @_operation
    def consume_execution_grant(self, binding: AttemptBinding, raw: bytes, *, task: dict, route: dict, provenance=None):
        """Persist one original registration BEFORE local process custody.

        This internal operation requires an independently authenticated account
        peer and project supervisor. A returned dataclass/MAC is not an execution
        ticket. Only the actual fixed writer return may authorize its local
        handoff. Lost replies/crashes retain consumption and never reissue it.
        """
        binding = self._result_binding(binding)
        now = self._result_now()
        try:
            grant = verify_execution_grant(self._result_key, binding, raw)
        except Exception:
            grant = None
        if grant is None:
            raise HistoryError("history_invalid_input")
        invalid = False
        try:
            invalid = (type(route) is not dict or grant.task_digest != keyed_digest(self._result_key, canonical_json(validate_task(task)))
                       or grant.route_digest != keyed_digest(self._result_key, canonical_json(route))
                       or grant.configuration_digest != content_digest(validate_execution_configuration(route["execution_configuration"])))
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError("history_conflict")
        with self._transaction():
            existing = self._connection.execute("SELECT * FROM project_execution_consumptions WHERE project_id=? "
                                                "AND (claim_id=? OR registration_digest=?) LIMIT 2",
                                                (self.project_id, binding.claim_id, grant.registration_digest)).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]["claim_id"] != binding.claim_id or existing[0]["registration_digest"] != grant.registration_digest:
                    raise HistoryError("history_conflict")
                prior = self._stored_blob(existing[0]["grant"], maximum=MAX_EXECUTION_GRANT_BYTES)
                if prior != raw or not valid_integer(existing[0]["consumed_at_ms"]):
                    raise HistoryError("history_incompatible")
                raise HistoryError("history_execution_consumed")
            self._orchestration_owner().active_for(binding)
            # No implicit retirement/file I/O or transaction nesting here. An
            # expired/body-prepared/retired result cannot admit a new process.
            _, reservation = self._result_row(binding)
            row = self._connection.execute("SELECT * FROM result_leases WHERE project_id=? AND claim_id=?",
                                           (self.project_id, binding.claim_id)).fetchone()
            lease = None if row is None else self._lease(row)
            if (lease is None or lease.state != "bound" or reservation.state != "reserved"
                    or reservation.prepared_at_ms is not None or now >= reservation.expires_at_ms
                    or grant.assignment_digest != reservation.assignment_digest or grant.lease_digest != lease.lease_digest
                    or grant.deadline_at_ms != reservation.hard_deadline_at_ms
                    or not lease.created_at_ms <= grant.claimed_at_ms <= now < grant.deadline_at_ms):
                raise HistoryError("history_execution_not_ready")
            clock = time.monotonic_ns()
            if type(clock) is not int or not grant.clock_reference_ns <= clock < grant.deadline_ns:
                raise HistoryError("history_execution_not_ready")
            record = self._new_execution_provenance(grant, task, route, provenance, now)
            if record is None and (self._orchestration_limits is not None or self._connection.execute(
                    'SELECT 1 FROM project_orchestrations WHERE project_id=? AND orchestration_handle=?',
                    (binding.project_id, binding.orchestration_handle)).fetchone() is not None):
                # The explicit pre-journal fixture mode cannot bypass fresh
                # provenance once an actual managed project lifecycle exists.
                raise _ExecutionProvenanceRejected()
            if record is not None:
                self._append_locked(record, canonical_json(record), 'host', now)
            self._connection.execute("INSERT INTO project_execution_consumptions VALUES(?,?,?,?,?)",
                                     (self.project_id, binding.claim_id, grant.registration_digest, raw, now))
        return grant

    def _new_execution_provenance(self, grant, task, route, provenance, now):
        if provenance is None and self._allow_legacy_orchestration_reference:
            return None  # Explicit isolated pre-provenance fixture only.
        from .run_provenance import decode_run_provenance_input, verify_run_provenance
        invalid = False
        try:
            facts = provenance['provenance']
            request = decode_run_provenance_input({name: facts[name] for name in ('registration', 'registration_digest', 'assignment_id')})
            checked = verify_run_provenance(self._result_key, request, grant, provenance)
            facts = checked['provenance']
            invalid = facts['task_family'] != task['family'] or facts['provider'] != route.get('provider', 'codex')
            if not invalid:
                from .project_host_open import verify_execution_open
                verify_execution_open(self, grant, checked, task=task, now=now)
        except Exception:
            invalid = True
        if invalid:
            raise _ExecutionProvenanceRejected()
        return validate_learning_record({
            'schema_version': 1, 'record_type': 'execution_provenance',
            'record_id': self._execution_provenance_id(grant), 'run_id': grant.binding.run_id,
            'registration_digest': grant.registration_digest, 'provenance': checked, 'observed_at_ms': now,
        })

    def _execution_provenance_id(self, grant):
        return 'execution_provenance:' + keyed_digest(self._result_key, canonical_json(grant.binding.as_data()))

    @_operation
    def verification_worker_reference(self, context, outcome_digest, *, before_reply):
        """Resolve the original retained worker dependency before verifier open."""
        from .project_worker_result import verification_worker_reference
        return verification_worker_reference(self, context, outcome_digest, before_reply=before_reply)

    @_operation
    def original_open_scope(self, context, *, before_reply):
        from .project_open_scope import original_open_scope
        return original_open_scope(self, context, before_reply=before_reply)

    @_operation
    def operational_qa_scope(self, context, host_outcome_digest, verifier_host_outcome_digest, *, before_reply):
        from .project_operational_qa import operational_qa_scope
        return operational_qa_scope(self, context, host_outcome_digest, verifier_host_outcome_digest,
                                   before_reply=before_reply)

    @_operation
    def issue_operational_check_receipt(self, context, host_outcome_digest, verifier_host_outcome_digest,
                                       report, *, request_id, before_commit):
        from .project_qa_checks import issue_check_receipt
        return issue_check_receipt(self, context, host_outcome_digest, verifier_host_outcome_digest,
                                   report, request_id=request_id, before_commit=before_commit)

    @_operation
    def commit_operational_qa(self, context, host_outcome_digest, verifier_host_outcome_digest,
                              check_receipt, *, before_commit):
        from .project_qa_commit import commit_operational_qa
        return commit_operational_qa(self, context, host_outcome_digest, verifier_host_outcome_digest,
                                     check_receipt, before_commit=before_commit)

    @_operation
    def operational_qa_details(self, context, host_outcome_digest, verifier_host_outcome_digest,
                               check_receipt, *, before_reply):
        from .project_qa_commit import operational_qa_details
        return operational_qa_details(self, context, host_outcome_digest, verifier_host_outcome_digest,
                                      check_receipt, before_reply=before_reply)

    @_operation
    def begin_learning_import(self, import_id, manifest, *, now_ms, before_commit):
        from .project_learning_corpus import begin_import
        return begin_import(self, import_id, manifest, now_ms=now_ms, before_commit=before_commit)

    @_operation
    def stage_learning_import_chunk(self, import_id, index, records, attestations, *, now_ms, before_commit):
        from .project_learning_corpus import stage_chunk
        return stage_chunk(self, import_id, index, records, attestations, now_ms=now_ms, before_commit=before_commit)

    @_operation
    def finish_learning_import(self, import_id, *, now_ms, before_commit):
        from .project_learning_corpus import finish_import
        return finish_import(self, import_id, now_ms=now_ms, before_commit=before_commit)

    @_operation
    def learning_import_records(self, import_id, *, after_source_sequence=0, limit=32):
        from .project_learning_corpus import import_records
        return import_records(self, import_id, after_source_sequence=after_source_sequence, limit=limit)

    @_operation
    def learning_import_attestations(self, import_id, *, after_subject_record_id="", limit=32):
        from .project_learning_corpus import import_attestations
        return import_attestations(self, import_id, after_subject_record_id=after_subject_record_id, limit=limit)

    @_operation
    def learning_key_domain_digest(self):
        from .project_learning_identity import key_domain_digest
        return key_domain_digest(self)

    @_operation
    def prepare_learning_case_records(self, references, *, expected_domain_digest):
        from .project_learning_identity import prepare_cases
        return prepare_cases(self, references, expected_domain_digest=expected_domain_digest)

    @_operation
    def prepare_learning_candidate_record(self, spec, references, *, expected_domain_digest):
        from .project_learning_identity import prepare_candidate
        return prepare_candidate(self, spec, references, expected_domain_digest=expected_domain_digest)

    def _execution_provenance(self, grant):
        identity = self._execution_provenance_id(grant)
        try:
            entry = self.record(identity)
        except HistoryError:
            raise HistoryError('history_execution_not_ready') from None
        from .run_provenance import decode_run_provenance_input, verify_run_provenance
        try:
            record = parse_bounded_json(entry.content)
            if (entry.source != 'host' or record['record_type'] != 'execution_provenance'
                    or record['run_id'] != grant.binding.run_id or record['registration_digest'] != grant.registration_digest):
                raise HistoryError('history_incompatible')
            facts = record['provenance']['provenance']
            request = decode_run_provenance_input({name: facts[name] for name in ('registration', 'registration_digest', 'assignment_id')})
            checked = verify_run_provenance(self._result_key, request, grant, record['provenance'])
            from .project_host_open import verify_execution_open
            verify_execution_open(self, grant, checked)
            return checked
        except Exception:
            raise HistoryError('history_incompatible') from None

    @_operation
    def arm_result_lease(self, binding: ResultLeaseBinding) -> ResultLease:
        """Durably mark claim-send uncertainty before sending the account request.

        Armed leases never expire/free capacity on a timeout or restart. Exact
        actual-claim binding or verified account denial settlement resolves them;
        missing/orphaned state remains charged, never a caller 'not sent' flag.
        """
        binding = self._lease_binding(binding)
        now = self._result_now()
        with self._transaction():
            value = self._lease_row(binding)
            if value.state in {"claim_pending", "bound"}:
                return value
            self._orchestration_owner().active_for(binding)
            if value.state != "reserved" or now >= value.reserved_until_ms:
                raise HistoryError("history_result_expired")
            value = replace(value, state="claim_pending")
            self._set_lease(value)
        return value

    @_operation
    def bind_result_lease(self, lease_binding: ResultLeaseBinding, binding: AttemptBinding,
                          assignment: dict, *, hard_deadline_at_ms: int) -> ResultReservation:
        """Bind storage to the exact actual claim after trusted account checking.

        Values are not that proof. The native/account adapter must authenticate
        the original request, issued receipt, complete scope and deadline first.
        The lease's creation/expiry and charge survive the atomic bind unchanged.
        """
        lease_binding = self._lease_binding(lease_binding)
        binding = self._result_binding(binding)
        if not lease_binding.matches_attempt(binding) or not valid_integer(hard_deadline_at_ms):
            raise HistoryError("history_conflict")
        digest = keyed_digest(self._result_key, assignment_bytes(assignment, binding))
        now = self._result_now()
        with self._transaction():
            value = self._lease_row(lease_binding)
            if value.assignment_digest != digest:
                raise HistoryError("history_conflict")
            if value.state == "bound":
                _, actual = self._result_row(binding)
                if actual.hard_deadline_at_ms != hard_deadline_at_ms:
                    raise HistoryError("history_conflict")
                return actual
            if value.state != "claim_pending":
                raise HistoryError("history_result_not_ready")
            if not now < hard_deadline_at_ms < value.reserved_until_ms:
                raise HistoryError("history_result_expired")
            actual = ResultReservation(binding, digest, value.created_at_ms, hard_deadline_at_ms,
                                       value.reserved_until_ms, value.reserved_until_ms, "reserved")
            if self._connection.execute("SELECT 1 FROM terminal_outbox WHERE project_id=? AND "
                                        "(claim_id=? OR (run_handle=? AND attempt=?) OR claim_digest=?)",
                                        (self.project_id, binding.claim_id, binding.run_handle,
                                         binding.attempt, binding.claim_digest)).fetchone() is not None:
                raise HistoryError("history_conflict")
            self._insert_result_reservation(actual)
            value = replace(value, state="bound", claim_id=binding.claim_id, claim_digest=binding.claim_digest,
                            hard_deadline_at_ms=hard_deadline_at_ms,
                            claim_binding_digest=self._claim_lease_digest(value, binding, hard_deadline_at_ms))
            self._set_lease(value)
        return actual

    @_operation
    def expire_result_leases(self, *, limit: int = 10) -> int:
        """Retire only unused leases; pending claims retain uncertain capacity."""
        now = self._result_now()
        if not valid_integer(limit, minimum=1, maximum=100):
            raise HistoryError("history_invalid_input")
        def candidates(after):
            sql = "SELECT rowid FROM result_leases WHERE project_id=? AND state='reserved' AND reserved_until_ms<=?"
            args = [self.project_id, now]
            if after is not None:
                sql += " AND rowid>?"
                args.append(after)
            return self._connection.execute(sql + " ORDER BY rowid LIMIT ?", (*args, limit)).fetchall()
        rows = candidates(self._lease_expiry_cursor)
        if not rows and self._lease_expiry_cursor is not None:
            self._lease_expiry_cursor = None
            rows = candidates(None)
        failure = None
        retired = 0
        for position in rows:
            self._lease_expiry_cursor = position[0]
            try:
                with self._transaction():
                    row = self._connection.execute("SELECT * FROM result_leases WHERE rowid=?", (position[0],)).fetchone()
                    value = self._lease(row)
                    self._set_lease(replace(value, state="expired"))
                retired += 1
            except HistoryError as error:
                failure = failure or error.reason_code
            except (sqlite3.Error, OSError):
                failure = failure or "history_unavailable"
        if failure is not None:
            raise HistoryError(failure)
        return retired

    @_operation
    def reserve_result(self, binding: AttemptBinding, assignment: dict, *, hard_deadline_at_ms: int) -> ResultReservation:
        """Reserve storage before launch; this creates no launch/admission authority.

        This known-claim reference path remains for existing internal callers.
        A service must use the pre-claim lease/arm/bind path to reserve durable
        space before account claim consumption; this method cannot do that.
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
            self._orchestration_owner().active_for(binding)
            if self._connection.execute("SELECT 1 FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?",
                                        (self.project_id, binding.run_handle, binding.attempt)).fetchone() is not None:
                raise HistoryError("history_conflict")
            if self._connection.execute("SELECT 1 FROM terminal_outbox WHERE project_id=? AND "
                                        "(claim_id=? OR (run_handle=? AND attempt=?) OR claim_digest=?)",
                                        (self.project_id, binding.claim_id, binding.run_handle,
                                         binding.attempt, binding.claim_digest)).fetchone() is not None:
                raise HistoryError("history_conflict")
            self._check_result_capacity()
            self._insert_result_reservation(value)
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
    def publish_result(
        self, binding: AttemptBinding, assignment: dict, body: bytes, observation: ResultObservation,
        *, client_usage: CodexUsageObservation | None = None,
    ) -> TerminalDelivery:
        """Construct exact terminal metadata inside the trusted digest-key owner.

        This is an internal business operation, not an enabled publication API.
        A trusted service must prove original assignment/executor-output custody,
        host facts and the complete pre-reserved claim before calling it. Normal
        primary/CLI/hook labels cannot authorize these facts. No key, digest or
        record ID is supplied by the caller. The fixed captured observation time
        and all terminal facts participate in the existing immutable prepared
        commit, so retry/restart cannot silently replace them or refresh privacy.
        Omitted client_usage retains the old internal reference request. Actual
        fixed-worker publication always supplies a captured observation, even
        when its measurement is unavailable; model result usage is never used.
        """
        binding = self._result_binding(binding)
        if type(observation) is not ResultObservation:
            raise HistoryError("history_invalid_input")
        captured = observation.as_data()
        encoded_assignment = assignment_bytes(assignment, binding)
        snapshot = parse_bounded_json(encoded_assignment, max_bytes=MAX_ASSIGNMENT_CANONICAL_BYTES)
        now = self._result_now()
        _, reservation = self._live_result(binding, now)
        if reservation.assignment_digest != keyed_digest(self._result_key, encoded_assignment):
            raise HistoryError("history_conflict")
        if not reservation.created_at_ms <= captured["observed_at_ms"] <= now:
            raise HistoryError("history_invalid_input")
        body = checked_result_body(body, snapshot, captured)
        material = {
            **captured, "schema_version": DISPATCH_RESULT_SCHEMA_VERSION,
            "record_type": "dispatch_result", "run_id": binding.run_id,
            "result_digest": keyed_digest(self._result_key, body),
        }
        # Keep the existing dispatch-result record ID rule: it hashes only safe
        # terminal metadata, including the owner's keyed private-body digest.
        record = {**material, "record_id": f"dispatch_result:{content_digest(material)}"}
        content = canonical_json(validate_learning_record(record))
        if client_usage is None:
            # Preserve the original reference call shape as well as its bytes.
            # Actual fixed-worker publication always supplies an observation.
            return self.commit_result(content, binding, snapshot, body)
        return self.commit_result(content, binding, snapshot, body, client_usage=client_usage)

    @_operation
    def commit_result(
        self, content: bytes, binding: AttemptBinding, assignment: dict, body: bytes,
        *, client_usage: CodexUsageObservation | None = None,
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
        usage_record = None
        if client_usage is not None:
            if type(client_usage) is not CodexUsageObservation:
                raise HistoryError("history_invalid_input")
            usage_record = self._client_usage_record(binding, snapshot, checked, client_usage)
        material = {
            "binding": binding.as_data(), "record": checked, "result_digest": digest,
            "assignment_digest": value.assignment_digest,
        }
        if usage_record is not None:
            material["client_usage_record"] = usage_record
        prepared = keyed_digest(self._result_key, canonical_json(material))
        if row["prepared_digest"] is not None and not secrets.compare_digest(row["prepared_digest"], prepared):
            raise HistoryError("history_conflict")
        if value.state == "committed":
            if self.read_result(binding) != body:
                raise HistoryError("history_conflict")
            terminal = self._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                                (self.project_id, binding.claim_id)).fetchone()
            if usage_record is not None:
                actual = self._client_usage_entry(binding, checked, snapshot["model"])
                if actual is None or actual.content != canonical_json(usage_record):
                    raise HistoryError("history_conflict")
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
            if usage_record is not None:
                self._append_locked(usage_record, canonical_json(usage_record), "provider", now)
            self._connection.execute(
                "UPDATE result_reservations SET state='committed',committed_at_ms=?,expires_at_ms=?,"
                "body_size=?,result_digest=?,terminal_record_id=? WHERE project_id=? AND claim_id=?",
                (now, value.retained_until_ms, len(body), digest, delivery.record_id,
                 self.project_id, binding.claim_id),
            )
        return delivery

    def _client_usage_id(self, binding):
        return "client_usage:" + keyed_digest(self._result_key, b"project-client-usage-v1\0" + canonical_json(binding.as_data()))

    def _client_usage_record(self, binding, assignment, terminal, observation):
        data = observation.as_data()
        if data["requested_model"] != assignment["model"] or terminal["bridge_run_id"] != binding.claim_id:
            raise HistoryError("history_conflict")
        record = {"schema_version": 1, "record_id": self._client_usage_id(binding), "provider": "codex",
                  "run_id": binding.run_id, "phase": "after", "observed_at_ms": terminal["observed_at_ms"]}
        if data["usage"] is None:
            record.update(record_type="observation_unavailable", reason_code="usage_unavailable")
        else:
            record.update(record_type="provider_usage", requested_model=data["requested_model"], models=[], helper_models=[],
                          cache_creation_input_tokens=None, **data["usage"])
        return self._checked_record(canonical_json(record), "provider", terminal["observed_at_ms"])

    def _client_usage_entry(self, binding, terminal, model):
        row = self._connection.execute("SELECT * FROM records WHERE project_id=? AND record_id=?",
                                        (self.project_id, self._client_usage_id(binding))).fetchone()
        if row is None:
            return None
        entry = self._entry(row)
        record = parse_bounded_json(entry.content)
        if (entry.source != "provider" or record["record_type"] not in {"provider_usage", "observation_unavailable"}
                or record["run_id"] != binding.run_id or record["provider"] != "codex" or record["phase"] != "after"
                or record["observed_at_ms"] != terminal["observed_at_ms"]
                or (record["record_type"] == "provider_usage" and record["requested_model"] != model)
                or (record["record_type"] == "observation_unavailable" and record["reason_code"] != "usage_unavailable")):
            raise HistoryError("history_conflict")
        return entry

    @staticmethod
    def _workflow_client_usage(entry):
        from .learning_adapters import empty_usage

        usage = empty_usage()
        if entry is None:
            return usage
        record = parse_bounded_json(entry.content)
        if record["record_type"] == "observation_unavailable":
            return usage
        inputs, cached = record["input_tokens"], record["cached_input_tokens"]
        outputs, reasoning = record["output_tokens"], record["reasoning_output_tokens"]
        if cached is not None and inputs is not None and cached > inputs:
            cached = None
        if reasoning is not None and outputs is not None and reasoning > outputs:
            reasoning = None
        return {**usage, "input_tokens": inputs, "cached_tokens": cached,
                "output_tokens": outputs, "reasoning_tokens": reasoning}

    @_operation
    def read_result(self, binding: AttemptBinding) -> bytes:
        binding = self._result_binding(binding)
        if self._orchestration_owner().closed_for(binding):
            raise HistoryError("history_orchestration_state")
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
        """Bounded fair cleanup; failures stay charged and do not starve peers.

        The owner-local cursor is scheduling state, not a durable receipt or a
        business identity. Durable retiring/expired state still controls reads
        and capacity. A restart can repeat cleanup, never revive an identity.
        """
        now = self._result_now()
        if not valid_integer(limit, minimum=1, maximum=100):
            raise HistoryError("history_invalid_input")
        def candidates(after):
            statement = (
                "SELECT rowid FROM result_reservations WHERE project_id=? AND state!='expired' "
                "AND (expires_at_ms<=? OR state='retiring' OR EXISTS(SELECT 1 FROM project_orchestrations AS owned "
                "WHERE owned.project_id=result_reservations.project_id "
                "AND owned.orchestration_handle=result_reservations.orchestration_handle))"
            )
            arguments = [self.project_id, now]
            if after is not None:
                statement += " AND rowid>?"
                arguments.append(after)
            return self._connection.execute(statement + " ORDER BY rowid LIMIT ?", (*arguments, limit)).fetchall()

        # Select only bounded scalar positions first. One oversized/corrupt
        # reservation must not make the entire batch fail before progression.
        rows = candidates(self._result_expiry_cursor)
        if not rows and self._result_expiry_cursor is not None:
            self._result_expiry_cursor = None
            rows = candidates(None)
        failure = None
        retired = 0
        for candidate in rows:
            position = candidate[0]
            if not valid_integer(position, minimum=-MAX_SEQUENCE - 1):
                raise HistoryError("history_incompatible")
            self._result_expiry_cursor = position
            try:
                row = self._connection.execute(
                    "SELECT * FROM result_reservations WHERE project_id=? AND rowid=?", (self.project_id, position),
                ).fetchone()
                if row is None:
                    raise HistoryError("history_incompatible")
                value = self._reservation(row)
                # SQL membership selects candidates only. The sealed current
                # lifecycle must prove closure before early body retirement.
                if (value.state != 'retiring' and now < value.expires_at_ms
                        and not self._orchestration_owner().closed_for(value.binding)):
                    continue
                self._retire_result(value.binding)
                retired += 1
            except HistoryError as exc:
                if failure is None:
                    failure = exc.reason_code
            except (sqlite3.Error, OSError):
                if failure is None:
                    failure = "history_unavailable"
        if failure is not None:
            # Preserve failure visibility, without chaining private file/SQL
            # details, after other selected rows have had their cleanup turn.
            raise HistoryError(failure)
        return retired

    @_operation
    def admit_host_result(self, reference: HostResultReference, *, assignment: dict, task: dict,
                          route: dict, client_capability: dict, before_commit: Callable[[], None]) -> dict:
        """Derive one non-accepted outcome from this writer's exact durable return.

        Original input and current primary authority are server-side obligations.
        The callback rechecks current authority after artifact I/O, before any
        mutation (and on known retries). No account store, new ticket, admission
        acceptance, repair, QA decision or cleanup release is created here.
        """
        from .host_result_contracts import HostResultContext
        from .learning_engine import build_host_result_records

        if type(reference) is not HostResultReference or not callable(before_commit):
            raise HistoryError("history_invalid_input")
        reference = decode_host_result_reference(reference.as_data())
        binding = self._result_binding(reference.binding)
        _, inputs = HostResultContext(binding, assignment, task, route, client_capability).snapshot()
        assignment, task, route, client_capability = (inputs[name] for name in (
            "assignment", "task", "route", "client_capability"))
        row = self._connection.execute(
            "SELECT * FROM project_execution_consumptions WHERE project_id=? AND claim_id=?",
            (self.project_id, binding.claim_id),
        ).fetchone()
        if row is None:
            raise HistoryError("history_execution_not_ready")
        invalid = False
        try:
            grant = verify_execution_grant(self._result_key, binding,
                                          self._stored_blob(row["grant"], maximum=MAX_EXECUTION_GRANT_BYTES))
            configuration = validate_execution_configuration(route["execution_configuration"])
            invalid = (
                row["registration_digest"] != grant.registration_digest
                or not valid_integer(row["consumed_at_ms"], minimum=grant.claimed_at_ms,
                                     maximum=grant.deadline_at_ms - 1)
                or grant.assignment_digest != keyed_digest(self._result_key, assignment_bytes(assignment, binding))
                or grant.task_digest != keyed_digest(self._result_key, canonical_json(validate_task(task)))
                or grant.route_digest != keyed_digest(self._result_key, canonical_json(route))
                or grant.configuration_digest != content_digest(configuration)
                or configuration["client_digest"] != content_digest(client_capability)
            )
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError("history_conflict")
        provenance = self._execution_provenance(grant)
        if provenance is not None:
            facts = provenance['provenance']
            if facts['task_family'] != task['family'] or facts['provider'] != route.get('provider', 'codex'):
                raise HistoryError('history_conflict')
        reservation_row, reservation = self._result_row(binding)
        outbox = self._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                          (self.project_id, binding.claim_id)).fetchone()
        if outbox is None or reservation.state not in {"committed", "expired"}:
            raise HistoryError("history_result_not_ready")
        delivery = self._delivery(outbox)
        terminal_entry = self.record(delivery.record_id)
        terminal = parse_bounded_json(terminal_entry.content)
        if (delivery.binding != binding or delivery.record_id != reference.terminal_record_id
                or delivery.record_content_digest != reference.terminal_content_digest
                or delivery.result_digest != reference.result_digest
                or reservation.assignment_digest != grant.assignment_digest
                or reservation.hard_deadline_at_ms != grant.deadline_at_ms
                or reservation.terminal_record_id != delivery.record_id
                or reservation.result_digest != delivery.result_digest
                or terminal_entry.source != "host" or terminal_entry.content_digest != delivery.record_content_digest
                or terminal["schema_version"] != DISPATCH_RESULT_SCHEMA_VERSION
                or terminal["bridge_run_id"] != binding.claim_id
                or terminal["observed_at_ms"] < grant.claimed_at_ms):
            raise HistoryError("history_conflict")
        scope_digest = keyed_digest(self._result_key, b"project-host-result-v1\0" + canonical_json(binding.as_data()))
        identity = "host_result_admission:" + scope_digest
        expected = {
            "schema_version": 1, "record_type": "host_result_admission", "record_id": identity,
            "run_id": binding.run_id, "binding_digest": scope_digest,
            "registration_digest": grant.registration_digest, "dispatch_result": terminal,
            "terminal_content_digest": terminal_entry.content_digest,
            "observed_at_ms": terminal["observed_at_ms"],
        }
        usage_entry = self._client_usage_entry(binding, terminal, assignment["model"])
        prepared_material = {"binding": binding.as_data(), "record": terminal, "result_digest": delivery.result_digest,
                             "assignment_digest": grant.assignment_digest}
        if usage_entry is not None:
            prepared_material["client_usage_record"] = parse_bounded_json(usage_entry.content)
        prepared = keyed_digest(self._result_key, canonical_json(prepared_material))
        if (not matches(reservation_row["prepared_digest"], _DIGEST)
                or not secrets.compare_digest(reservation_row["prepared_digest"], prepared)):
            # Absence is unknown only for an original legacy request with no
            # client companion. A lost or changed expected record is corruption,
            # never permission to downgrade measured usage to unavailable.
            raise HistoryError("history_conflict")
        if usage_entry is not None:
            expected.update(schema_version=2, usage_record_id=usage_entry.record_id,
                            usage_content_digest=usage_entry.content_digest)
        prior = self._connection.execute("SELECT * FROM records WHERE project_id=? AND record_id=?",
                                         (self.project_id, identity)).fetchone()
        if prior is not None:
            entry = self._entry(prior)
            receipt = parse_bounded_json(entry.content)
            if entry.source != "host" or any(receipt.get(name) != value for name, value in expected.items()):
                raise HistoryError("history_conflict")
            result = self._host_result_receipt(entry)
            before_commit()
            return result
        body = self.read_result(binding)
        checked_result_body(body, assignment, terminal)
        checked_result = parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)
        observation = {"run_id": binding.run_id, "client_version": "bridge-v1",
                       "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]}
        if terminal["terminal_state"] is not None:
            observation["terminal_state"] = terminal["terminal_state"]
        # The original result lane has no repair operation: one immutable actual
        # return is admitted, including its invalid-result fence. A caller cannot
        # smuggle a legacy repair or substitute its own body/observations here.
        event, outcome, _ = build_host_result_records(
            assignment=assignment, task=task, result=checked_result, observation=observation,
            host_evidence={"run_id": binding.run_id, "coverage": "lifecycle_partial",
                           "usage": self._workflow_client_usage(usage_entry),
                           "observed_at_ms": terminal["observed_at_ms"], "no_progress_events": 0,
                           "usage_source": "client_observed" if usage_entry is not None else "host_observed"},
            execution_configuration=configuration, digest_key=self._result_key, client_capability=client_capability,
        )
        receipt = {**expected, "event_record_id": event["record_id"], "event_content_digest": content_digest(event),
                   "outcome_record_id": outcome["record_id"], "host_outcome_digest": content_digest(outcome)}
        encoded = [canonical_json(value) for value in (event, outcome, receipt)]
        checked = [self._checked_record(value, "host", terminal["observed_at_ms"]) for value in encoded]
        before_commit()
        now = self._result_now()
        if (self.result_reservation(binding) != reservation or now >= reservation.expires_at_ms
                or now < terminal["observed_at_ms"]):
            raise HistoryError("history_result_expired")
        with self._transaction():
            for value, content in zip(checked, encoded):
                entry = self._append_locked(value, content, "host", now)
        return self._host_result_receipt(entry)

    def _host_result_receipt(self, entry: HistoryEntry) -> dict:
        receipt = parse_bounded_json(entry.content)
        usage_entry = None
        if receipt["schema_version"] == 2:
            usage_entry = self.record(receipt["usage_record_id"])
            usage = parse_bounded_json(usage_entry.content)
            if (usage_entry.source != "provider" or usage_entry.content_digest != receipt["usage_content_digest"]
                    or usage["record_type"] not in {"provider_usage", "observation_unavailable"}
                    or usage["run_id"] != receipt["run_id"] or usage["provider"] != "codex"
                    or usage["observed_at_ms"] != receipt["observed_at_ms"]):
                raise HistoryError("history_conflict")
        commits = {}
        for label, record_type, identity, digest in (
            ("event_commit", "event", receipt["event_record_id"], receipt["event_content_digest"]),
            ("outcome_commit", "outcome", receipt["outcome_record_id"], receipt["host_outcome_digest"]),
        ):
            linked = self.record(identity)
            value = parse_bounded_json(linked.content)
            if (linked.source != "host" or value["record_type"] != record_type
                    or value["run_id"] != receipt["run_id"] or linked.content_digest != digest):
                raise HistoryError("history_conflict")
            if record_type == "outcome" and usage_entry is not None:
                usage = parse_bounded_json(usage_entry.content)
                if (value["usage"] != self._workflow_client_usage(usage_entry)
                        or (usage["record_type"] == "provider_usage" and usage["requested_model"] != value["model"])):
                    raise HistoryError("history_conflict")
            commits[label] = self._host_result_metadata(linked)
        return {"admission": receipt, "admission_commit": self._host_result_metadata(entry), **commits}

    @staticmethod
    def _host_result_metadata(entry: HistoryEntry) -> dict:
        return {name: getattr(entry, name) for name in (
            "record_id", "source", "source_sequence", "observed_at_ms", "ingested_at_ms", "content_digest", "late",
        )}

    @_operation
    def attest_terminal_delivery(self, binding: AttemptBinding) -> bytes:
        """Export the actual consumed attempt's durable safe outbox metadata.

        No private result artifact is opened. Historical metadata survives body
        retirement and an acknowledgement; native peer/actor authentication is
        still required before the account accepts this bounded evidence.
        """
        from .terminal_delivery import encode_terminal_evidence

        binding = self._result_binding(binding)
        _, reservation = self._result_row(binding)
        consumed = self._connection.execute("SELECT grant FROM project_execution_consumptions WHERE project_id=? AND claim_id=?",
                                            (self.project_id, binding.claim_id)).fetchone()
        row = self._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                       (self.project_id, binding.claim_id)).fetchone()
        if consumed is None or row is None or reservation.state not in {'committed', 'expired'}:
            raise HistoryError('history_result_not_ready')
        invalid = False
        reason = 'history_incompatible'
        try:
            grant = verify_execution_grant(self._result_key, binding,
                                           self._stored_blob(consumed['grant'], maximum=MAX_EXECUTION_GRANT_BYTES))
            delivery = self._delivery(row)
            invalid = (delivery.binding != binding or reservation.terminal_record_id != delivery.record_id
                       or reservation.assignment_digest != grant.assignment_digest
                       or reservation.hard_deadline_at_ms != grant.deadline_at_ms)
            if not invalid:
                evidence = encode_terminal_evidence(self._result_key, grant, delivery, self.record(delivery.record_id).content)
        except HistoryError as error:
            invalid = True
            if error.reason_code in {'history_unavailable', 'history_result_unavailable'}:
                reason = error.reason_code
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError(reason)
        return evidence

    @_operation
    def acknowledge_account_terminal(self, binding: AttemptBinding, raw: bytes) -> None:
        """Verify the original account acceptance before acknowledging its outbox.

        This acknowledges terminal metadata only. It does not retire body storage,
        release execution custody or establish confirmed tree cleanup.
        The service must independently authenticate the original account peer;
        the shared-key MAC is a binding check, not native sender authority.
        """
        from .terminal_delivery import verify_terminal_evidence, verify_terminal_receipt

        binding = self._result_binding(binding)
        evidence_raw = self.attest_terminal_delivery(binding)
        lease = self._connection.execute('SELECT binding FROM result_leases WHERE project_id=? AND claim_id=?',
                                         (self.project_id, binding.claim_id)).fetchone()
        if lease is None:
            raise HistoryError('history_result_not_ready')
        invalid = False
        try:
            expected = ResultLeaseBinding(**parse_bounded_json(self._stored_blob(lease['binding'], maximum=MAX_BINDING_BYTES)))
            evidence = verify_terminal_evidence(self._result_key, expected, evidence_raw)
            receipt = verify_terminal_receipt(self._result_key, evidence.grant, evidence.delivery, raw)
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError('history_invalid_input')
        self.acknowledge_terminal(binding.claim_id, evidence.delivery.record_content_digest, receipt['receipt_digest'],
                                  runtime_digest=binding.runtime_digest)

    @_operation
    def issue_cleanup(self, binding, *, registry, custody):
        from .project_cleanup import issue_cleanup
        return issue_cleanup(self, self._lease_binding(binding), registry=registry, custody=custody)

    @_operation
    def cleanup_evidence(self, binding):
        from .project_cleanup import read_cleanup
        return read_cleanup(self, self._lease_binding(binding))

    @_operation
    def record_account_release(self, binding, raw, *, registry, custody):
        from .project_release_ack import record_account_release
        return record_account_release(self, self._lease_binding(binding), raw, registry=registry, custody=custody)

    @_operation
    def account_release_receipt(self, binding):
        from .project_release_ack import read_account_release
        return read_account_release(self, self._lease_binding(binding))

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
