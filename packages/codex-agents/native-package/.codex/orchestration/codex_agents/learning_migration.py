"""Move one project's learning history from the legacy ledger to its project store.

Offline owner work: the caller holds the ledger with the collector stopped and
has opened the project store itself, so the project's own service is not
running. Nothing here kills or waits for a run; a ledger that is not drained is
refused by the staging step and the project simply stays where it was.

Order, so that a failure at any point can be run again:

1. stage: a sealed backup of the project's ledger partition is copied, byte for
   byte, into the project store (records, owner signatures, QA rows and the full
   original image), and compared with the ledger again before it counts;
2. retire: the ledger stops accepting learning records for the project, and notes
   that the copy still has to be switched on;
3. activate: the project store starts reading the staged records.

Between 1 and 2 nothing has changed for anyone. Between 2 and 3 the project
learns from its live records only, and running the migration again finishes it.
Account rows, restrictions, claims and capacity stay in the ledger untouched.
Signatures are kept as bytes; the ledger verified them when it admitted the
records and they are not verified again here.

Rollback restores the ledger as the learning store. It is refused once learning
records were written after the cutover (`history_learning_reconciliation`).
"""

from __future__ import annotations

import contextlib

from .history_contracts import HistoryError
from .learning_source_transfer import stage_source
from .project_learning_activation import (
    activate, activation, deactivate, derived_records, discard_import, staged_imports,
)
from .sql_read_budget import SQLReadLimits

BACKUP_LIFETIME_MS = 24 * 60 * 60 * 1000
WORK_LIMITS = SQLReadLimits(100_000, 1000, 2 * 1_048_576)


class MigrationError(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _report(state, history, ledger, project_id):
    record = activation(history)
    return {"state": state, "project_id": project_id, "ledger_retired": ledger.learning_moved(project_id),
            "import_id": None if record is None else record["import_id"],
            "imported_records": 0 if record is None else record["record_count"],
            "imported_qa": 0 if record is None else record["qa_count"]}


def migration_status(ledger, history, project_id):
    moved, record = ledger.learning_moved(project_id), activation(history)
    if record is not None and not moved:
        raise MigrationError("migration_inconsistent")
    if moved:
        pending = ledger.learning_activation_pending(project_id) or (
            record is None and bool(staged_imports(history)))
        return _report("pending" if pending else "complete", history, ledger, project_id)
    return _report("pending" if ledger.has_learning_history(project_id) else "not_needed",
                   history, ledger, project_id)


def migrate_project(ledger, history, project_id, *, now_ms):
    """Run or finish the move; returns the same report as `migration_status`."""
    try:
        moved, record = ledger.learning_moved(project_id), activation(history)
        if record is not None and not moved:
            raise MigrationError("migration_inconsistent")
        if moved:
            if record is None:
                # Retired but not yet activated: finish with the staged import, if there is one.
                imports = staged_imports(history)
                if len(imports) > 1:
                    raise MigrationError("migration_inconsistent")
                if imports:
                    activate(history, imports[0], now_ms=now_ms)
            ledger.clear_learning_activation_pending(project_id)
            return _report("complete", history, ledger, project_id)
        for stale in staged_imports(history):
            discard_import(history, stale)  # An attempt that never retired the ledger.
        if not ledger.has_learning_history(project_id):
            ledger.set_learning_moved(project_id, True)
            return _report("complete", history, ledger, project_id)
        name = f"migration_{now_ms}"
        ledger.create_sanitized_backup(project_id, file_id=name, expires_at_ms=now_ms + BACKUP_LIFETIME_MS)
        import_id = name.replace("_", "-")
        receipt = stage_source(ledger, history, project_id, name, import_id=import_id, work_limits=WORK_LIMITS)
        if any(receipt[part]["state"] != "staged" for part in ("source", "records", "qa")):
            raise MigrationError("migration_incomplete")
        ledger.set_learning_moved(project_id, True, activation_pending=True)
        activate(history, import_id, now_ms=now_ms)
        ledger.clear_learning_activation_pending(project_id)
        return _report("complete", history, ledger, project_id)
    except HistoryError as error:
        raise MigrationError(_REASONS.get(error.reason_code, "migration_unavailable")) from None


def rollback_project(ledger, history, project_id):
    """Make the ledger the project's learning store again; the staged import is kept.

    Refused once the project store holds learning the ledger would not have:
    records written after an import was switched on, or, for a project that
    moved with nothing to import, any derived learning record at all.
    """
    try:
        record = activation(history)
        if record is not None:
            deactivate(history, expected=record)
        elif ledger.learning_moved(project_id) and derived_records(history):
            raise HistoryError("history_learning_reconciliation")
        ledger.set_learning_moved(project_id, False)
        return _report("rolled_back", history, ledger, project_id)
    except HistoryError as error:
        raise MigrationError(_REASONS.get(error.reason_code, "migration_unavailable")) from None


_REASONS = {
    # The whole ledger must be idle for the copy to be exact.
    "history_unavailable": "migration_not_drained",
    "history_conflict": "migration_source_changed",
    "history_learning_capacity": "migration_too_large",
    "history_learning_reconciliation": "migration_reconciliation_required",
    "history_writer_busy": "migration_project_service_running",
    "history_not_authorized": "migration_not_authorized",
    "history_incompatible": "migration_incompatible",
}

# O10 HS-3/HS-9: a learning migration that fails during a service start is kept for learning-status, one
# owner-only file per project (no update can lose another project's), opened without blocking, with
# reasons from a fixed list only, never a path or message. Not the shared client-health file: earlier
# versions read it and refuse codes they do not know.
START_FAILURES = "learning-migration-failures"
START_REASONS = frozenset({  # O10 HS-12: every fixed reason the migration and its command give
    "migration_failed", "migration_not_drained", "migration_project_service_running", "migration_source_changed",
    "migration_reconciliation_required", "migration_too_large", "migration_incompatible", "migration_unavailable",
    "migration_inconsistent", "migration_incomplete", "migration_not_authorized",
})


def _start_failure_path(state, project_root):
    from .collector_paths import _canonical_project_root
    from .learning_contracts import keyed_digest
    digest = keyed_digest(state.digest_key(), "migration-start\0" + str(_canonical_project_root(project_root)))
    return state.root / START_FAILURES / f"{digest}.json"


@contextlib.contextmanager
def _start_failure_lock(state, project_root):
    """O10 HS-14: one per-project lock for recording and clearing, taken without waiting; yields whether
    it is held. Never blocks a start."""
    import fcntl
    import os
    descriptor = None
    try:
        path = _start_failure_path(state, project_root).with_suffix(".lock")
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except Exception:
        held = False
    try:
        yield held
    finally:
        if descriptor is not None:
            os.close(descriptor)


def record_start_failure(state, project_root, reason) -> None:
    """Best effort: never stops, and never blocks, the start it describes."""
    with _start_failure_lock(state, project_root) as held:
        if held:
            _record_start_failure(state, project_root, reason)


def _record_start_failure(state, project_root, reason) -> None:
    import json
    import os
    import secrets
    import time
    try:
        path = _start_failure_path(state, project_root)
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        info = os.lstat(path.parent)
        import stat
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(json.dumps({"reason_code": reason if type(reason) is str and reason in START_REASONS
                                         else "migration_failed",
                                         "at_ms": time.time_ns() // 1_000_000}, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except Exception:
        pass


def clear_start_failure(state, project_root, *, since_ms=None) -> None:
    """O10 HS-13: a successful start clears only a failure kept before it began (`since_ms`), never a
    newer one another start kept meanwhile."""
    import os
    with _start_failure_lock(state, project_root) as held:
        if not held:
            return  # HS-14: another start is keeping a failure now; leave it
        try:
            if since_ms is not None:
                kept = start_failure(state, project_root)
                if kept is not None and kept["at_ms"] >= since_ms:
                    return
            path = _start_failure_path(state, project_root)
            if not path.is_symlink():
                os.unlink(path)
        except Exception:
            pass


def start_failure(state, project_root):
    """This project's last start-time migration failure, or None. Never blocks (a FIFO in the way is
    refused, not read)."""
    import json
    import os
    import stat
    try:
        descriptor = os.open(_start_failure_path(state, project_root),
                             os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except Exception:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_size > 4096):  # HS-17: one link only
            return None
        entry = json.loads(os.read(descriptor, 4096))
    except Exception:
        return None
    finally:
        os.close(descriptor)
    if (type(entry) is not dict or set(entry) != {"reason_code", "at_ms"} or type(entry["reason_code"]) is not str
            or entry["reason_code"] not in START_REASONS or type(entry["at_ms"]) is not int):
        return None
    return dict(entry)
