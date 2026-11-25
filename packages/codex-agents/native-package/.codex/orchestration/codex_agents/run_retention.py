"""S14b: account detail retention, called only by collector maintenance.

Controlled outcomes and replay/provenance rows are deliberately outside this sweep.
An indexed page is bounded BEFORE protection checks, so permanent evidence
cannot make a pass scan an entire ledger. The in-memory cursor is only an
optimisation; deletion and immutable summaries are the durable checkpoint.
"""
from __future__ import annotations

import json
from collections import Counter

from .learning_contracts import content_digest, validate_learning_record, RETAINABLE_DETAIL_TYPES, SOURCES
from .sql_read_budget import SQLReadError, SQLReadLimits

RUN_RETENTION_BATCH = 32
RUN_DETAIL_KEEP_MS = 90 * 86_400_000
RUN_RETENTION_READ_LIMITS = SQLReadLimits(2048, 25, 4*1_048_576)
DETAIL_SQL = ','.join("'"+kind+"'" for kind in sorted(RETAINABLE_DETAIL_TYPES))
RETENTION_INDEX = (
    'CREATE INDEX IF NOT EXISTS records_run_retention ON records(expires_at_ms,project_id,source_sequence) '
    f'WHERE record_type IN ({DETAIL_SQL})'
)
PROTECTION_INDEXES = (
    'CREATE INDEX IF NOT EXISTS record_links_retention_to ON record_links(project_id,to_record_id)',
    'CREATE INDEX IF NOT EXISTS stored_files_retention_source ON stored_files(project_id,source_record_id)',
    'CREATE INDEX IF NOT EXISTS consumed_grants_retention_promotion ON consumed_grants(project_id,promotion_record_id)',
    'CREATE INDEX IF NOT EXISTS account_open_retention_run ON account_open_requests(project_id,run_id)',
)


def _protected_ids(ledger, candidates, now):
    """One SELECT for the bounded page, within the shared 16-cursor limit."""
    from .account_service import _ADMISSION_INPUT_SCOPE
    parameters = {'a': now}
    values = []
    for i, (row, record) in enumerate(candidates):
        values.append(f'(:p{i},:r{i},:run{i})')
        parameters.update({f'p{i}': row['project_id'], f'r{i}': row['record_id'], f'run{i}': record.get('run_id')})
    # Each row is tested in its own project scope. Account-wide Claude/window
    # rules inside the existing scope keep their original cross-project meaning.
    scope = _ADMISSION_INPUT_SCOPE.replace(':p', 'r.project_id')
    sql = (
        'WITH page(project_id,record_id,run_id) AS (VALUES '+','.join(values)+') '
        'SELECT p.project_id,p.record_id FROM page p CROSS JOIN records r '
        'ON r.project_id=p.project_id AND r.record_id=p.record_id WHERE '
        'EXISTS(SELECT 1 FROM projects x WHERE x.project_id=p.project_id AND x.status<>\'active\') OR '
        'EXISTS(SELECT 1 FROM record_links l WHERE l.project_id=p.project_id AND l.from_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM record_links l WHERE l.project_id=p.project_id AND l.to_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM stored_files f WHERE f.project_id=p.project_id AND f.source_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM owner_attestations a WHERE a.project_id=p.project_id AND a.subject_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM consumed_grants g WHERE g.project_id=p.project_id AND g.grant_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM consumed_grants g WHERE g.project_id=p.project_id AND g.promotion_record_id=p.record_id) OR '
        + scope + ' OR (p.run_id IS NOT NULL AND ('
        'EXISTS(SELECT 1 FROM account_open_requests q WHERE q.project_id=p.project_id AND q.run_id=p.run_id) OR '
        'EXISTS(SELECT 1 FROM host_run_bindings h WHERE h.project_id=p.project_id AND h.run_id=p.run_id) OR '
        'EXISTS(SELECT 1 FROM active_run_capacity c WHERE c.project_id=p.project_id AND c.run_id=p.run_id '
        'AND c.released_at_ms IS NULL)))')
    return {(row[0], row[1]) for row in ledger._connection.execute(sql, parameters).fetchall()}


def sweep(ledger, now, batch):
    if type(batch) is not int or not 1 <= batch <= RUN_RETENTION_BATCH:
        raise ValueError('run retention batch is outside its bound')
    db = ledger._connection
    with ledger._held():
        ledger._run_retention_more = False
        if ledger._readers() or db.in_transaction:
            return 0
        after = getattr(ledger, '_run_retention_after', (-1, '', 0))
        def page():
            return [dict(row) for row in db.execute(
                'SELECT r.* FROM records r INDEXED BY records_run_retention '
                f'WHERE r.record_type IN ({DETAIL_SQL}) AND r.expires_at_ms<=? '
                'AND (r.expires_at_ms,r.project_id,r.source_sequence)>(?,?,?) '
                'ORDER BY r.expires_at_ms,r.project_id,r.source_sequence LIMIT ?',
                (now, *after, batch)).fetchall()]
        try:
            rows = db.read_call(RUN_RETENTION_READ_LIMITS, page)
        except SQLReadError:
            return 0  # cooperative budget refusal is never a run's refusal
        candidates, eligible = [], []
        def select():
            for row in rows:
                if row['ingested_at_ms']+RUN_DETAIL_KEEP_MS > now:
                    continue
                try:
                    record = validate_learning_record(json.loads(bytes(row['content'])), stored=True)
                except (ValueError, TypeError, KeyError):
                    continue  # malformed detail stays readable by older tools
                if (record['record_type'] != row['record_type'] or record['record_id'] != row['record_id']
                        or not row['observed_at_ms'] <= now-RUN_DETAIL_KEEP_MS):
                    continue
                if row['source'] not in SOURCES or content_digest(record) != row['content_digest']:
                    continue
                candidates.append((row, record))
            if candidates:
                protected = _protected_ids(ledger, candidates, now)
                eligible.extend(row for row, _ in candidates if (row['project_id'], row['record_id']) not in protected)
        try:
            db.read_call(RUN_RETENTION_READ_LIMITS, select)
        except SQLReadError:
            eligible = []
        groups = {}
        for row in eligible:
            groups.setdefault(row['project_id'], []).append(row)
        # No cursor advancement until the entire transaction succeeds. A crash
        # re-reads the same rows; no summary can be counted twice.
        if eligible:
            with ledger._account_transaction():
                for project, group in groups.items():
                    keys = [[r['record_id'], r['source_sequence'], r['content_digest']] for r in group]
                    summary = {
                        'schema_version': 1, 'record_type': 'retention_summary',
                        'record_id': 'retsum-'+content_digest(keys),
                        'observed_at_ms': now, 'keep_days': 90,
                        'first_observed_at_ms': min(r['observed_at_ms'] for r in group),
                        'last_observed_at_ms': max(r['observed_at_ms'] for r in group),
                        'record_count': len(group),
                        'record_counts': dict(Counter(r['record_type'] for r in group)),
                        'record_id_digests': sorted({content_digest(r['record_id']) for r in group}),
                        # Typed digest lookup exists for outcomes only. Other
                        # detail keeps its ID digest without duplicating an
                        # unused content-digest index in permanent summaries.
                        'record_content_digests': sorted({r['content_digest'] for r in group if r['record_type'] == 'outcome'}),
                        'outcome_counts': dict(Counter(json.loads(bytes(r['content']))['status']
                                                       for r in group if r['record_type'] == 'outcome')),
                        'source_counts': dict(Counter(r['source'] for r in group)),
                        'detail': 'Details older than 90 days; counts only.',
                    }
                    ledger._append_checked_locked(project, validate_learning_record(summary), producer='lifecycle', timestamp=now)
                    db.executemany('DELETE FROM records WHERE project_id=? AND record_id=?',
                                   [(project, r['record_id']) for r in group])
        ledger._run_retention_after = ((rows[-1]['expires_at_ms'], rows[-1]['project_id'], rows[-1]['source_sequence'])
                                       if len(rows) == batch else (-1, '', 0))
        ledger._run_retention_more = len(rows) == batch
        return len(eligible)
