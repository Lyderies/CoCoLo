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
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from .contracts import ContractError
from .history_contracts import (
    MAX_SEQUENCE, AttemptBinding, HistoryEntry, HistoryError, TerminalDelivery,
    _DIGEST, _ID, _PROJECT, matches, valid_integer,
)
from .learning_contracts import (
    MAX_RECORD_BYTES, SOURCES, canonical_json, parse_bounded_json, validate_learning_record,
)

_APPLICATION_ID = 0x43444831
_SCHEMA_VERSION = 1
_RECORD_TYPES = frozenset({"event", "dispatch_intent", "dispatch_result", "outcome", "verification_binding"})
MAX_DELIVERY_PAGE_BYTES = 16_384
MAX_BINDING_BYTES = 4096
MAX_HISTORY_ROW_BYTES = 2 * MAX_RECORD_BYTES
_SCHEMA = """
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
_COLUMNS = {
    "history_identity": ("project_id", "next_source_sequence", "next_delivery_sequence"),
    "records": ("project_id", "record_id", "record_type", "source", "source_sequence",
                "observed_at_ms", "ingested_at_ms", "content", "content_digest", "late"),
    "terminal_outbox": ("project_id", "claim_id", "delivery_sequence", "run_handle", "attempt", "claim_digest", "binding", "record_id",
                        "record_content_digest", "result_digest", "receipt_digest"),
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
    This first store supports run/event records only, not legacy layout migration.
    """

    def __init__(self, directory: Path, project_id: str) -> None:
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._closed = False
        self._connection = None
        self._directory_fd = self._writer_fd = None
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
        elif (
            version != _SCHEMA_VERSION or application != _APPLICATION_ID
            or {" ".join(row[2].split()).casefold() for row in objects}
            != {" ".join(statement.split()).casefold() for statement in _SCHEMA.split(";") if statement.strip()}
        ):
            raise HistoryError("history_incompatible")
        for name, columns in _COLUMNS.items():
            actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({name})"))
            if actual != columns:
                raise HistoryError("history_incompatible")
        rows = connection.execute("SELECT project_id FROM history_identity LIMIT 2").fetchall()
        if len(rows) != 1 or rows[0][0] != self.project_id:
            raise HistoryError("history_incompatible")

    def close(self) -> None:
        # An inherited instance must not close/unlock the original owner's resources.
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        if self._closed:
            return
        self._closed = True
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
