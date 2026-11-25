"""B7a: project-local hold/custody counts and bounded, content-free D3 repairs.

Read only, on the history writer thread. Deadlines never imply an evidence-only
release. Memory holds report the durable driver and preparation lifecycle.
"""
from .holds_report_contracts import (HOLD_KINDS, UNTRACKED_KINDS, TIMER_KINDS, MAX_REPAIR_ENTRIES,
                                      DIRECTORY_REPAIR, OPERATOR_REPAIR, RETRY_REPAIR)
from .project_result_contexts import MAX_LIVE_CONTEXT_BYTES

MAX_REPAIR_PAGES = 5


def _holds(history):
    db, project = history._connection, history.project_id
    result = {kind: {'tracking': 'not_tracked' if kind in UNTRACKED_KINDS else 'tracked',
                     'held': 0, 'releasing': 0, 'past_due': 0 if kind in TIMER_KINDS else None,
                     'next_eligible': None, 'oldest_taken_at_ms': None} for kind in HOLD_KINDS}
    for kind, state, count, oldest in db.execute(
            "SELECT kind,state,COUNT(*),MIN(taken_at_ms) FROM project_holds INDEXED BY project_holds_due "
            "WHERE project_id=? AND state<>'released' GROUP BY kind,state", (project,)):
        if kind in UNTRACKED_KINDS:
            continue
        result[kind][state] = count
        if state == 'held':
            result[kind]['oldest_taken_at_ms'] = oldest
    # SH-4: an unfinished result has no timer release. A committed result's
    # retention applies even while its execution obligation still awaits release.
    now = history._hold_now()
    due, past = db.execute(
        "SELECT MIN(h.due_at_ms),COALESCE(SUM(h.due_at_ms<=?),0) "
        "FROM project_holds h INDEXED BY project_holds_due JOIN project_result_contexts c "
        "ON c.project_id=h.project_id AND c.run_handle=substr(h.identity,1,instr(h.identity,':')-1) "
        "AND c.attempt=CAST(substr(h.identity,instr(h.identity,':')+1) AS INTEGER) "
        "WHERE h.project_id=? AND h.state<>'released' AND h.state='held' AND h.kind='result_custody' "
        "AND h.due_at_ms IS NOT NULL AND (c.state IN ('committed','recorded') OR "
        "(c.state='legacy_no_context' AND EXISTS(SELECT 1 FROM result_reservations s "
        "WHERE s.project_id=c.project_id AND s.run_handle=c.run_handle AND s.attempt=c.attempt "
        "AND s.state='committed')))", (now, project)).fetchone()
    result['result_custody'].update(past_due=past, next_eligible=due)
    from .project_holds_schema import ORCHESTRATION_EXECUTION_HELD
    due, past = db.execute(
        "SELECT MIN(r.due_at_ms),COALESCE(SUM(r.due_at_ms<=?),0) FROM project_holds r "
        "JOIN project_orchestrations o ON o.project_id=r.project_id AND o.orchestration_handle=r.identity "
        "WHERE r.project_id=? AND r.kind='orchestration' AND r.state='held' "
        "AND r.due_at_ms IS NOT NULL AND NOT " + ORCHESTRATION_EXECUTION_HELD, (now,project)).fetchone()
    result['orchestration'].update(past_due=past,next_eligible=due)
    return result


def _ended_by_update(history, runtime_digest):
    if runtime_digest is None or history._result_key is None:
        return 0
    db, project = history._connection, history.project_id
    # A close can be interrupted for reasons other than an update. Require both
    # the custody release and a verified interrupted checkpoint of another runtime.
    rows = db.execute(
        "SELECT o.orchestration_handle,o.sequence,o.checkpoint_digest,COUNT(*) AS ended "
        "FROM project_result_contexts c JOIN project_holds h ON h.project_id=c.project_id "
        "AND h.kind='result_custody' AND h.identity=c.run_handle||':'||c.attempt "
        "JOIN result_reservations s ON s.project_id=c.project_id AND s.run_handle=c.run_handle AND s.attempt=c.attempt "
        "JOIN project_orchestrations o ON o.project_id=s.project_id AND o.orchestration_handle=s.orchestration_handle "
        "WHERE c.project_id=? AND c.outcome_digest IS NULL AND h.state='released' "
        "AND h.release_kind='orchestration_closed' AND NOT EXISTS(SELECT 1 FROM records r "
        "WHERE r.project_id=c.project_id AND r.record_type='host_result_admission' "
        "AND json_extract(CAST(r.content AS TEXT),'$.dispatch_result.bridge_run_id')=s.claim_id) "
        "GROUP BY o.orchestration_handle", (project,)).fetchall()
    owner = history._orchestration_owner()
    count = 0
    for row in rows:
        snapshot = owner._latest_snapshot(row)
        if (snapshot is not None and snapshot['status'] == 'interrupted'
                and snapshot['scope']['runtime_digest'] != runtime_digest):
            count += row['ended']
    return count


def _custody(history, runtime_digest):
    db, project = history._connection, history.project_id
    live, size, nearest, legacy = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(context_size),0),MIN(retained_until_ms),"
        "COALESCE(SUM(state='legacy_no_context'),0) FROM project_result_contexts "
        "INDEXED BY project_result_contexts_live WHERE project_id=? AND state NOT IN ('ended','expired')",
        (project,)).fetchone()
    awaiting = db.execute(
        "SELECT COUNT(*) FROM project_result_contexts c INDEXED BY project_result_contexts_live "
        "WHERE c.project_id=? AND c.state NOT IN ('ended','expired') AND c.state='recorded' "
        "AND EXISTS(SELECT 1 FROM records r WHERE r.project_id=c.project_id AND r.record_type='outcome' "
        "AND r.content_digest=c.outcome_digest "
        "AND COALESCE(json_extract(CAST(r.content AS TEXT),'$.task_slice'), '') NOT LIKE 'verification-%') "
        "AND NOT EXISTS(SELECT 1 FROM project_operational_qa q WHERE q.project_id=c.project_id "
        "AND q.worker_outcome_digest=c.outcome_digest) "
        "AND NOT EXISTS(SELECT 1 FROM records v WHERE v.project_id=c.project_id "
        "AND v.record_type='verification_binding' "
        "AND json_extract(CAST(v.content AS TEXT),'$.worker_outcome_digest')=c.outcome_digest)",
        (project,)).fetchone()[0]
    return {'live': live, 'context_bytes': size, 'max_live_context_bytes': MAX_LIVE_CONTEXT_BYTES,
            'retained_until_ms': nearest, 'awaiting_verifier': awaiting, 'legacy_no_context': legacy,
            'ended_by_update_unrecorded': _ended_by_update(history, runtime_digest)}


def _repairs(history):
    # D3's context repairs precede reservation-only entries, which can themselves
    # mix directories and retries. Bound both retained entries and pages read;
    # directories found within that window take priority over retries.
    needed, retrying = [], []
    cursor, total, more = None, 0, False
    for _ in range(MAX_REPAIR_PAGES):
        page = history.result_retirement_repairs(MAX_REPAIR_ENTRIES, cursor)
        total += len(page)
        for entry in page:
            if entry['reason'] == 'directory':
                target, plain = needed, DIRECTORY_REPAIR
            elif entry.get('operator_needed', False):
                target, plain = needed, OPERATOR_REPAIR
            else:
                target, plain = retrying, RETRY_REPAIR
            if len(target) <= MAX_REPAIR_ENTRIES:
                target.append(dict(plain))
        more = page.truncated
        if not more or len(needed) > MAX_REPAIR_ENTRIES:
            break
        cursor = page.cursor
    return {'entries': (needed + retrying)[:MAX_REPAIR_ENTRIES],
            'truncated': more or total > MAX_REPAIR_ENTRIES}


def holds_report(history, *, runtime_digest=None):
    history._require_owner(read_only=True)
    from .recovery_report import recovery_pending
    return {'project_holds': _holds(history), 'result_custody': _custody(history, runtime_digest),
            'result_retirement_repairs': _repairs(history), 'recovery_pending': recovery_pending(history)}
