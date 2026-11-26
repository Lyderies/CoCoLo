"""S14b: account detail retention, called only by collector maintenance.

Live and controlled evidence and replay/provenance fences are preserved.
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
COMPONENT_SQL = DETAIL_SQL+",'event'"
RETENTION_INDEX = (
    'CREATE INDEX IF NOT EXISTS records_run_retention ON records(expires_at_ms,project_id,source_sequence) '
    f'WHERE record_type IN ({DETAIL_SQL})'
)
# Persistent indexes change the stored layout, so only the consented upgrade flow
# (setup --upgrade-ledger) creates them; no open ever writes them. Without all of them maintenance does nothing and runs no SQL.
RETENTION_INDEX_NAMES = ('records_run_retention', 'record_links_retention_to', 'stored_files_retention_source',
                         'consumed_grants_retention_promotion', 'account_open_retention_run')
NEVER_DUE = 1 << 62
PROTECTION_INDEXES = (
    'CREATE INDEX IF NOT EXISTS record_links_retention_to ON record_links(project_id,to_record_id)',
    'CREATE INDEX IF NOT EXISTS stored_files_retention_source ON stored_files(project_id,source_record_id)',
    'CREATE INDEX IF NOT EXISTS consumed_grants_retention_promotion ON consumed_grants(project_id,promotion_record_id)',
    'CREATE INDEX IF NOT EXISTS account_open_retention_run ON account_open_requests(project_id,run_id)',
)


# 774 * 1825 * 15 = 21,188,250 entries. A 96-bit prefix gives birthday
# collision probability <= n(n-1)/2**97 < 2.84e-15 over that envelope.
# The prefix selects a candidate only: full digests in the summary prove identity.
HISTORY_PREFIX_BYTES = 12
HISTORY_INDEX_BATCH = 32
HISTORY_INDEX_SCHEMA = (
    'CREATE TEMP TABLE retention_members(project_ref INTEGER, digest BLOB, lookup_kind INTEGER, '
    'record_type INTEGER, summary_sequence INTEGER, '
    'PRIMARY KEY(project_ref,digest,lookup_kind,record_type)) WITHOUT ROWID'
)


def digest_prefix(digest):
    return bytes.fromhex(digest)[:HISTORY_PREFIX_BYTES]


def rebuilding_answer():
    # A transient lookup answer, never stored or used as learning evidence.
    return {'record_type': 'history_index_rebuilding', 'detail': 'history index rebuilding'}


def create_index_statements(db):
    """The five CREATE INDEX statements, nothing else: run inside the ledger 17 -> 18 schema step's transaction
    (decision 352), so they commit with the version write or roll back with it."""
    for sql in (RETENTION_INDEX, *PROTECTION_INDEXES):
        db.execute(sql)


def ensure_indexes(ledger):
    """Create the persistent retention indexes. Only the consented upgrade flow (setup --upgrade-ledger,
    after its whole-ledger copy, on the drained offline owner) may call this; no open ever does."""
    create_index_statements(ledger._connection)
    probe(ledger)


def probe(ledger):
    """Learn once (open, or after consented creation) whether retention can run and when it is next due.

    The idle sweep then compares two numbers in Python: no statement runs until a retainable
    record can have expired. Appends lower the due time; an empty page raises it.
    """
    db = ledger._connection
    present = {row[0] for row in db.execute(
        'SELECT name FROM sqlite_master WHERE type=? AND name IN (%s)' % ','.join('?'*len(RETENTION_INDEX_NAMES)),
        ('index', *RETENTION_INDEX_NAMES))}
    ledger._retention_ready = present == set(RETENTION_INDEX_NAMES)
    ledger._run_retention_due_ms = _next_due(db, -1) if ledger._retention_ready else NEVER_DUE
    ledger._run_retention_after = (-1, '', 0)


def _next_due(db, after_ms):
    row = db.execute(
        'SELECT expires_at_ms FROM records INDEXED BY records_run_retention '
        f'WHERE record_type IN ({DETAIL_SQL}) AND expires_at_ms>? ORDER BY expires_at_ms LIMIT 1',
        (after_ms,)).fetchone()
    return row[0] if row else NEVER_DUE


def note_append(ledger, record_type, expires_at_ms):
    if record_type in RETAINABLE_DETAIL_TYPES and expires_at_ms is not None:
        ledger._run_retention_due_ms = min(ledger._run_retention_due_ms, expires_at_ms)


def initialize(ledger):
    """Schedule derived state; no whole-history work runs while opening a ledger."""
    db = ledger._connection
    db.execute(HISTORY_INDEX_SCHEMA)
    ledger._history_index_after = ('', 0)
    ledger._history_index_rebuilding = db.execute(
        "SELECT 1 FROM records INDEXED BY records_type_project "
        "WHERE record_type='retention_summary' LIMIT 1").fetchone() is not None
    db.execute('CREATE TEMP TABLE retention_revision(n INTEGER)')
    db.execute('INSERT INTO retention_revision VALUES(0)')
    # Only mutations that can release protection invalidate the completed walk.
    for table in ('record_links', 'stored_files', 'owner_attestations', 'consumed_grants',
                  'account_open_requests', 'host_run_bindings', 'active_run_capacity', 'capacity_completions', 'projects'):
        for operation in ('DELETE', 'UPDATE'):
            db.execute(f'CREATE TEMP TRIGGER retention_{table}_{operation} AFTER {operation} ON main.{table} '
                       'BEGIN UPDATE retention_revision SET n=n+1; END')
    probe(ledger)


def rebuild_history_index(ledger):
    """Schedule a fresh rebuild. Maintenance owns its bounded, resumable pages."""
    with ledger._held():
        if ledger._readers() or ledger._connection.in_transaction:
            raise ValueError('history rebuild requires an idle owner')
        # Replacing the TEMP tree avoids a row-by-row DELETE of a large index.
        ledger._connection.execute('DROP TABLE retention_members')
        ledger._connection.execute(HISTORY_INDEX_SCHEMA)
        ledger._history_index_after = ('', 0)
        ledger._history_index_rebuilding = ledger._connection.execute(
            "SELECT 1 FROM records INDEXED BY records_type_project "
            "WHERE record_type='retention_summary' LIMIT 1").fetchone() is not None


def advance_history_index(ledger):
    """One indexed owner page; commit its entries before advancing the cursor."""
    db = ledger._connection
    project, after = ledger._history_index_after
    def next_project(project):
        row = db.execute(
            "SELECT project_id FROM records INDEXED BY records_type_project "
            "WHERE record_type='retention_summary' AND project_id>? ORDER BY project_id LIMIT 1",
            (project,)).fetchone()
        return row[0] if row else None
    def page():
        current, cursor = project, after
        if not current:
            current = next_project(current)
        if current is None:
            return None, [], False
        def rows():
            # records_type_project's implicit rowid suffix provides the seek;
            # do not use a tuple range that can rescan the entire project prefix.
            return db.execute(
                "SELECT p.rowid,r.rowid,r.source_sequence,r.content FROM records r "
                "INDEXED BY records_type_project JOIN projects p ON p.project_id=r.project_id "
                "WHERE r.record_type='retention_summary' AND r.project_id=? AND r.rowid>? "
                "ORDER BY r.rowid LIMIT ?", (current, cursor, HISTORY_INDEX_BATCH)).fetchall()
        found = rows()
        if not found:
            current, cursor = next_project(current), 0
            if current is None:
                return None, [], False
            found = rows()
        more = len(found) == HISTORY_INDEX_BATCH or next_project(current) is not None
        return current, found, more
    try:
        current, found, more = db.read_call(RUN_RETENTION_READ_LIMITS, page)
        with ledger._account_transaction():
            for project_ref, rowid, sequence, content in found:
                db.executemany('INSERT OR IGNORE INTO retention_members VALUES(?,?,?,?,?)',
                               _summary_members(project_ref, sequence, json.loads(bytes(content))))
    except SQLReadError:
        return False
    if found:
        ledger._history_index_after = (current, found[-1][1])
    ledger._history_index_rebuilding = more
    return True


# Append-only numeric vocabulary for the rebuildable index, independent of live writers.
HISTORY_TYPES = ('', 'admission_decision', 'dispatch_intent', 'event', 'outcome',
                 'provider_billing', 'provider_headroom', 'provider_identity')


def _summary_members(project_ref, sequence, summary):
    types = summary.get('record_id_types')
    if types is None:
        # Earlier summaries prove untyped membership; a mixed summary cannot
        # establish an individual member's type. Never guess a typed identity.
        kinds = list(summary['record_counts'])
        types = [kinds[0] if len(kinds) == 1 else ''] * len(summary['record_id_digests'])
    rows = [(project_ref, digest_prefix(digest), 0, HISTORY_TYPES.index(kind), sequence)
            for digest, kind in zip(summary['record_id_digests'], types)]
    # Legacy summaries sometimes included non-outcome content digests too. Only
    # typed new summaries, or all-outcome legacy summaries, prove this type.
    if 'record_id_types' in summary or set(summary['record_counts']) == {'outcome'}:
        rows.extend((project_ref, digest_prefix(digest), 1, HISTORY_TYPES.index('outcome'), sequence)
                    for digest in summary['record_content_digests'])
    return rows


def summary_matches(summary, target, *, by_digest=False, record_type=None):
    """A prefix (including a forced collision) never proves full membership."""
    if by_digest:
        return (record_type in (None, 'outcome')
                and ('record_id_types' in summary or set(summary['record_counts']) == {'outcome'})
                and target in summary['record_content_digests'])
    types = summary.get('record_id_types')
    if types is None:
        kinds = list(summary['record_counts'])
        types = [kinds[0] if len(kinds) == 1 else ''] * len(summary['record_id_digests'])
    return any(digest == target and (record_type is None or kind == record_type)
               for digest, kind in zip(summary['record_id_digests'], types))


def index_summary(ledger, project, summary):
    project_ref = ledger._connection.execute('SELECT rowid FROM projects WHERE project_id=?', (project,)).fetchone()[0]
    sequence = ledger._connection.execute('SELECT source_sequence FROM records WHERE project_id=? AND record_id=?',
                                          (project, summary['record_id'])).fetchone()[0]
    ledger._connection.executemany('INSERT OR IGNORE INTO retention_members VALUES(?,?,?,?,?)',
                                   _summary_members(project_ref, sequence, summary))


def initialize_import_history(history):
    """Imported summaries are inert, permanent history on the project owner."""
    db = history._connection
    db.execute(HISTORY_INDEX_SCHEMA)
    history._learning_history_cursors = {}
    # MAX_IMPORTS is two; only the indexed existence probe reads records here.
    for row in db.execute('SELECT import_id FROM project_learning_imports WHERE project_id=?',
                          (history.project_id,)).fetchall():
        if db.execute("SELECT 1 FROM project_learning_records WHERE project_id=? AND import_id=? "
                      "AND record_type='retention_summary' LIMIT 1", (history.project_id, row[0])).fetchone():
            history._learning_history_cursors[row[0]] = 0


def advance_import_history(history):
    """One page per project-owner maintenance turn, using the import type index."""
    pending = history._learning_history_cursors
    if not pending:
        return 0
    import_id = next(iter(pending))
    db = history._connection
    rows = db.execute(
        "SELECT source_sequence,content FROM project_learning_records "
        "WHERE project_id=? AND import_id=? AND record_type='retention_summary' AND source_sequence>? "
        "ORDER BY source_sequence LIMIT ?",
        (history.project_id, import_id, pending[import_id], HISTORY_INDEX_BATCH)).fetchall()
    with history._transaction():
        ref = db.execute('SELECT rowid FROM project_learning_imports WHERE project_id=? AND import_id=?',
                         (history.project_id, import_id)).fetchone()
        for sequence, content in rows:
            db.executemany('INSERT OR IGNORE INTO retention_members VALUES(?,?,?,?,?)',
                           _summary_members(ref[0], sequence, json.loads(bytes(content))))
    if len(rows) < HISTORY_INDEX_BATCH:
        del pending[import_id]
    else:
        pending[import_id] = rows[-1][0]
    return len(rows)


def import_history_detail(history, import_id, identifier, record_type, *, by_digest=False):
    if record_type is not None and record_type not in HISTORY_TYPES:
        return None
    target = identifier if by_digest else content_digest(identifier)
    try:
        full_digest = bytes.fromhex(target)
        if len(full_digest) != 32:
            return None
    except (ValueError, TypeError):
        return None
    target = full_digest.hex()
    kind = HISTORY_TYPES.index(record_type) if record_type is not None else None
    row = history._connection.execute(
        "SELECT r.content FROM retention_members m JOIN project_learning_records r "
        "ON r.project_id=? AND r.import_id=? AND r.source_sequence=m.summary_sequence "
        "WHERE m.project_ref=(SELECT rowid FROM project_learning_imports WHERE project_id=? AND import_id=?) "
        "AND m.digest=? AND m.lookup_kind=? AND (? IS NULL OR m.record_type=?) LIMIT 1",
        (history.project_id, import_id, history.project_id, import_id, digest_prefix(target),
         int(by_digest), kind, kind)).fetchone()
    if row:
        summary = json.loads(bytes(row[0]))
        if summary_matches(summary, target, by_digest=by_digest, record_type=record_type):
            return summary
    return rebuilding_answer() if import_id in history._learning_history_cursors else None


def _protected_ids(ledger, candidates, now):
    """Recheck all live authority under the same write lock as summary and deletion."""
    from .account_service import _ADMISSION_INPUT_SCOPE
    parameters = {'a': now}
    values = []
    for i, (row, record) in enumerate(candidates):
        values.append(f'(:p{i},:r{i},:run{i})')
        parameters.update({f'p{i}': row['project_id'], f'r{i}': row['record_id'], f'run{i}': record.get('run_id')})
    scope = _ADMISSION_INPUT_SCOPE.replace(':p', 'r.project_id')
    # An internal edge is ignored only for distinct expired members of the same
    # run. External edges and explicit self-links continue to pin full evidence.
    links = []
    for own, other in (('from_record_id', 'to_record_id'), ('to_record_id', 'from_record_id')):
        links.append('EXISTS(SELECT 1 FROM record_links l WHERE l.project_id=p.project_id '
                     f'AND l.{own}=p.record_id AND NOT EXISTS(SELECT 1 FROM page q '
                     f'WHERE q.project_id=p.project_id AND q.record_id=l.{other} '
                     'AND q.record_id<>p.record_id AND q.run_id=p.run_id))')
    sql = (
        'WITH RECURSIVE page(project_id,record_id,run_id) AS (VALUES '+','.join(values)+'), '
        'protected(project_id,record_id) AS ('
        'SELECT p.project_id,p.record_id FROM page p JOIN records r '
        'ON r.project_id=p.project_id AND r.record_id=p.record_id WHERE '
        "EXISTS(SELECT 1 FROM projects x WHERE x.project_id=p.project_id AND x.status<>'active') OR "
        + ' OR '.join(links) + ' OR '
        'EXISTS(SELECT 1 FROM stored_files f WHERE f.project_id=p.project_id AND f.source_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM owner_attestations a WHERE a.project_id=p.project_id AND a.subject_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM consumed_grants g WHERE g.project_id=p.project_id AND g.grant_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM consumed_grants g WHERE g.project_id=p.project_id AND g.promotion_record_id=p.record_id) OR '
        'EXISTS(SELECT 1 FROM capacity_completions c WHERE c.project_id=p.project_id AND c.terminal_record_id=p.record_id) OR '
        + scope + ' OR (p.run_id IS NOT NULL AND ('
        'EXISTS(SELECT 1 FROM account_open_requests q WHERE q.project_id=p.project_id AND q.run_id=p.run_id) OR '
        'EXISTS(SELECT 1 FROM host_run_bindings h WHERE h.project_id=p.project_id AND h.run_id=p.run_id) OR '
        'EXISTS(SELECT 1 FROM active_run_capacity c WHERE c.project_id=p.project_id AND c.run_id=p.run_id '
        'AND c.released_at_ms IS NULL))) '
        'UNION SELECT q.project_id,q.record_id FROM protected x JOIN record_links l ON l.project_id=x.project_id '
        'AND (l.from_record_id=x.record_id OR l.to_record_id=x.record_id) JOIN page q ON q.project_id=x.project_id '
        'AND (q.record_id=l.from_record_id OR q.record_id=l.to_record_id)) '
        'SELECT project_id,record_id FROM protected')
    return {(row[0], row[1]) for row in ledger._connection.execute(sql, parameters).fetchall()}


def _component(db, row, now, batch):
    """Bounded linked closure; oversize components stay full, never partly deleted."""
    return [dict(r) for r in db.execute(
        'WITH RECURSIVE members(record_id) AS (SELECT ? UNION '
        'SELECT r.record_id FROM members m JOIN record_links l ON l.project_id=? '
        'AND (l.from_record_id=m.record_id OR l.to_record_id=m.record_id) '
        'JOIN records r ON r.project_id=l.project_id AND '
        '(r.record_id=l.from_record_id OR r.record_id=l.to_record_id) '
        f'WHERE r.record_type IN ({COMPONENT_SQL}) AND (r.record_type<>\'event\' OR r.source=\'host\') '
        'AND r.expires_at_ms<=? AND r.ingested_at_ms<=? AND r.observed_at_ms<=? '
        "AND json_valid(CAST(r.content AS TEXT)) AND json_extract(CAST(r.content AS TEXT),'$.run_id')="
        "json_extract(CAST(? AS TEXT),'$.run_id') LIMIT ?) "
        'SELECT r.* FROM members m JOIN records r ON r.project_id=? AND r.record_id=m.record_id',
        (row['record_id'], row['project_id'], now, now-RUN_DETAIL_KEEP_MS, now-RUN_DETAIL_KEEP_MS,
         row['content'], batch+1, row['project_id'])).fetchall()]


def sweep(ledger, now, batch):
    if type(batch) is not int or not 1 <= batch <= RUN_RETENTION_BATCH:
        raise ValueError('run retention batch is outside its bound')
    db = ledger._connection
    with ledger._held():
        ledger._run_retention_more = False
        if ledger._readers() or db.in_transaction:
            return 0
        if ledger._history_index_rebuilding:
            completed_page = advance_history_index(ledger)
            ledger._run_retention_more = completed_page and ledger._history_index_rebuilding
            return 0
        if not ledger._retention_ready or now < ledger._run_retention_due_ms:
            return 0  # nothing can be due: no statement runs (the idle check stays one cheap read)
        revision = db.execute('SELECT n FROM retention_revision').fetchone()[0]
        # Keep the high-water mark on short pages. Only a changed dependency or
        # a timed admission-window review starts another walk; quiet pinned ledgers
        # do one indexed empty-tail check per full maintenance interval.
        if (revision != getattr(ledger, '_run_retention_revision', revision)
                or now >= getattr(ledger, '_run_retention_review_at', now+1)):
            ledger._run_retention_after = (-1, '', 0)
        if now >= getattr(ledger, '_run_retention_review_at', now+1):
            del ledger._run_retention_review_at
        ledger._run_retention_revision = revision
        after = getattr(ledger, '_run_retention_after', (-1, '', 0))
        def page():
            return [dict(row) for row in db.execute(
                'SELECT r.* FROM records r INDEXED BY records_run_retention '
                f"WHERE r.record_type IN ({DETAIL_SQL}) AND (r.record_type<>'event' OR r.source='host') "
                'AND r.expires_at_ms<=? AND (r.expires_at_ms,r.project_id,r.source_sequence)>(?,?,?) '
                'ORDER BY r.expires_at_ms,r.project_id,r.source_sequence LIMIT ?', (now, *after, batch)).fetchall()]
        try:
            rows = db.read_call(RUN_RETENTION_READ_LIMITS, page)
        except SQLReadError:
            return 0
        if not rows:
            if after == (-1, '', 0):  # a walk from the start found nothing expired
                ledger._run_retention_due_ms = _next_due(db, now)
            return 0
        candidates, visited, oversized = {}, [], set()
        def select():
            component_queries = 0
            for row in rows:
                key = (row['project_id'], row['record_id'])
                if key in candidates or key in oversized:
                    visited.append(row)
                    continue
                try:
                    run = json.loads(bytes(row['content'])).get('run_id')
                except (ValueError, TypeError, AttributeError, RecursionError):
                    visited.append(row)
                    continue
                if run is not None:
                    if component_queries == 12:
                        break
                    component = _component(db, row, now, batch)
                    component_queries += 1
                else:
                    component = [row]
                if len(component) > batch:
                    oversized.update((member['project_id'], member['record_id']) for member in component)
                    visited.append(row)
                    continue
                additions = {}
                for member in component:
                    if member['ingested_at_ms']+RUN_DETAIL_KEEP_MS > now or member['observed_at_ms']+RUN_DETAIL_KEEP_MS > now:
                        continue
                    try:
                        record = validate_learning_record(json.loads(bytes(member['content'])), stored=True)
                    except (ValueError, TypeError, KeyError, RecursionError):
                        continue
                    if (record['record_type'] != member['record_type'] or record['record_id'] != member['record_id']
                            or member['source'] not in SOURCES or content_digest(record) != member['content_digest']):
                        continue
                    additions[(member['project_id'], member['record_id'])] = (member, record)
                if len(candidates.keys() | additions.keys()) > batch:
                    break
                candidates.update(additions)
                visited.append(row)
        try:
            db.read_call(RUN_RETENTION_READ_LIMITS, select)
        except SQLReadError:
            return 0
        eligible = []
        if candidates:
            try:
                with ledger._account_transaction():
                    protected = db.read_in_current_mode(RUN_RETENTION_READ_LIMITS,
                                                        lambda: _protected_ids(ledger, list(candidates.values()), now))
                    eligible = [row for key, (row, _) in candidates.items() if key not in protected]
                    timed = any(key in protected and row['record_type'] in {'provider_headroom', 'provider_billing', 'admission_decision'}
                                for key, (row, _) in candidates.items())
                    groups = {}
                    for row in eligible:
                        groups.setdefault(row['project_id'], []).append(row)
                    for project, group in groups.items():
                        keys = [[r['record_id'], r['source_sequence'], r['content_digest']] for r in group]
                        members = sorted((content_digest(r['record_id']), r['record_type']) for r in group)
                        summary = {
                            'schema_version': 1, 'record_type': 'retention_summary',
                            'record_id': 'retsum-'+content_digest(keys), 'observed_at_ms': now, 'keep_days': 90,
                            'first_observed_at_ms': min(r['observed_at_ms'] for r in group),
                            'last_observed_at_ms': max(r['observed_at_ms'] for r in group),
                            'record_count': len(group), 'record_counts': dict(Counter(r['record_type'] for r in group)),
                            'record_id_digests': [digest for digest, _ in members],
                            'record_id_types': [kind for _, kind in members],
                            'record_content_digests': sorted({r['content_digest'] for r in group if r['record_type']=='outcome'}),
                            'outcome_counts': dict(Counter(json.loads(bytes(r['content']))['status'] for r in group if r['record_type']=='outcome')),
                            'source_counts': dict(Counter(r['source'] for r in group)),
                            'detail': 'Details older than 90 days; counts only.',
                        }
                        checked = validate_learning_record(summary)
                        ledger._validate_evidence_links_locked(project, checked)
                        ledger._append_checked_locked(project, checked, producer='lifecycle', timestamp=now)
                        db.executemany('DELETE FROM records WHERE project_id=? AND record_id=?',
                                       [(project, r['record_id']) for r in group])
            except SQLReadError:
                return 0
        # Neither failed transactions nor read-budget refusals advance the walk.
        if visited:
            last = visited[-1]
            ledger._run_retention_after = (last['expires_at_ms'], last['project_id'], last['source_sequence'])
        ledger._run_retention_more = len(rows) == batch or len(visited) < len(rows)
        ledger._run_retention_revision = db.execute('SELECT n FROM retention_revision').fetchone()[0]
        if candidates and timed and not hasattr(ledger, '_run_retention_review_at'):
            ledger._run_retention_review_at = now+86_400_000
        return len(eligible)
