"""Content-free B4t diagnostics through the existing account owner connection."""
from pathlib import Path

PAGE_SIZE = 8
MAX_REPORT_BYTES = 14_336


def takeover_report(views, project_id, payload):
    from .account_api import AccountError
    from .collector_client import COLLECTOR_RUNTIME_DIGEST
    from .compatibility_message import compatibility_message
    from .learning_contracts import canonical_json
    from .runtime_lineage import release_versions
    from .active_run_capacity import crashed_claim
    views._guard()
    ledger = views._ledger
    offset, predecessor = payload.get('offset', 0), payload.get('predecessor_version')
    with ledger._held():
        views._project_locked(project_id)
        crashed, arguments = crashed_claim('a', ledger._clock_ms(), ledger._capacity_clock_reference())
        condition = (" FROM active_run_capacity a JOIN projects p ON p.project_id=a.project_id "
                     "WHERE p.status='active' AND a.project_id<>? AND a.kind='claimed' "
                     "AND a.runtime_digest<>? AND a.released_at_ms IS NULL AND NOT " + crashed)
        args = (project_id, COLLECTOR_RUNTIME_DIGEST, *arguments)
        projects, runs = ledger._connection.execute(
            'SELECT COUNT(DISTINCT a.project_id),COUNT(*)' + condition, args).fetchone()
        rows = ledger._connection.execute('SELECT p.canonical_root,a.runtime_digest,COUNT(*)' + condition
            + ' GROUP BY a.project_id,a.runtime_digest ORDER BY a.project_id,a.runtime_digest LIMIT ? OFFSET ?',
            (*args, PAGE_SIZE + 1, offset)).fetchall()
        recorded = ledger._connection.execute('SELECT MIN(recorded_at_ms) FROM runtime_lineage '
            'WHERE predecessor_version=? AND successor_digest=?', (predecessor, COLLECTOR_RUNTIME_DIGEST)).fetchone()[0]
    result = dict(recorded_at_ms=recorded, other_projects=projects, other_runs=runs, updates=[], next_offset=None)
    from .runtime_lineage import lineage_status
    with ledger._held():
        message = lineage_status(ledger._connection)
    if message is not None:
        result['lineage_message'] = message
    versions = release_versions()
    for root, digest, children in rows[:PAGE_SIZE]:
        version = versions.get(digest)
        update = dict(version=version, children=children,
                      update_message=compatibility_message(version, project_root=Path(root)))
        proposed = dict(result, updates=[*result['updates'], update])
        if len(canonical_json(proposed)) > MAX_REPORT_BYTES:
            if not result['updates']:
                raise AccountError('account_incompatible')
            break
        result['updates'].append(update)
    if len(result['updates']) < len(rows):
        result['next_offset'] = offset + len(result['updates'])
    return result
