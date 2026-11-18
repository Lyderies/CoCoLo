"""How near a project's store is to each of its limits (0.3.4 R9): counts and limits only.

Each live count follows the same rule its limit enforces, so the report and the refusal agree.
"""

from .history_contracts import HistoryError
from .orchestration_store import OrchestrationOwner
from .project_cleanup import MAX_CLEANUP_RESERVATIONS
from .project_qa_checks import MAX_CHECK_RECEIPTS

CAPACITY_NAMES = ('results', 'orchestrations', 'cleanup_reservations', 'operational_qa', 'qa_check_receipts')


def capacity_report(history):
    history._require_owner()
    db, project = history._connection, history.project_id
    results = db.execute("SELECT COUNT(*) FROM result_reservations WHERE project_id=? AND state!='expired'",
                         (project,)).fetchone()[0]
    for row in db.execute('SELECT * FROM result_leases WHERE project_id=?', (project,)).fetchall():
        try:
            results += history._lease(row).state not in {'bound', 'expired'}
        except HistoryError:
            results += 1  # Unverifiable counts as live, as in the limit.
    limits = history._orchestration_limits
    counts = {
        'results': (results, history._result_capacity.max_entries),
        'orchestrations': (OrchestrationOwner(history)._open_count(), None if limits is None else limits.max_orchestrations),
        'cleanup_reservations': (db.execute('SELECT COUNT(*) FROM project_cleanup_reservations').fetchone()[0],
                                 MAX_CLEANUP_RESERVATIONS),
        'operational_qa': (db.execute('SELECT COUNT(*) FROM project_operational_qa WHERE project_id=?',
                                      (project,)).fetchone()[0], MAX_CHECK_RECEIPTS),
        'qa_check_receipts': (db.execute('SELECT COUNT(*) FROM project_qa_check_receipts WHERE project_id=?',
                                         (project,)).fetchone()[0], MAX_CHECK_RECEIPTS),
    }
    return {name: {'live': live, 'limit': limit} for name, (live, limit) in counts.items()}
