"""Move one project's learning history from the legacy ledger to its project store.

Offline owner work: the caller holds the ledger with the collector stopped and
has opened the project store itself, so the project's own service is not
running. Nothing here kills or waits for a run; a ledger that is not drained is
refused by the staging step and the project simply stays where it was.

Order, so that a failure at any point can be run again:

1. stage: a sealed backup of the project's ledger partition is copied, byte for
   byte, into the project store (records, owner signatures, QA rows and the full
   original image), and compared with the ledger again before it counts;
2. retire: the ledger stops accepting learning records for the project;
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

from .history_contracts import HistoryError
from .learning_source_transfer import stage_source
from .project_learning_activation import activate, activation, deactivate, discard_import, staged_imports
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
        pending = record is None and bool(staged_imports(history))
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
        ledger.set_learning_moved(project_id, True)
        activate(history, import_id, now_ms=now_ms)
        return _report("complete", history, ledger, project_id)
    except HistoryError as error:
        raise MigrationError(_REASONS.get(error.reason_code, "migration_unavailable")) from None


def rollback_project(ledger, history, project_id):
    """Make the ledger the project's learning store again; the staged import is kept."""
    try:
        record = activation(history)
        if record is not None:
            deactivate(history, expected=record)
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
