"""Automatic runner/core continuation on the project writer's existing journal.

No provider process control: the external runner serializes admission at safe
points, persists its request identity, and replaces its session after this call.
Both journal ends and their hand-off are committed in one owner transaction.
"""
from .history_contracts import HistoryError
from .learning_contracts import canonical_json, keyed_digest
from .orchestration_api import OrchestrationRequest, orchestration_request_digest


def _capacity_due(owner, snapshot):
    from . import project_history, project_orchestration_report as report
    project, handle = owner._history.project_id, snapshot['orchestration_handle']
    limits = owner._history._orchestration_limits
    if limits is None:
        raise HistoryError('history_orchestration_unavailable')
    if snapshot['sequence'] >= limits.max_events - 1:
        return True
    registered = owner._db.execute(
        'SELECT COUNT(*) FROM project_registered_runs INDEXED BY project_registered_runs_orchestration '
        'WHERE project_id=? AND orchestration_handle=?', (project, handle)).fetchone()[0]
    if registered >= min(project_history.MAX_REGISTERED_RUNS, report.MAX_RUNS):
        return True
    opens = owner._db.execute(
        "SELECT COUNT(*) FROM project_host_opens WHERE project_id=? "
        "AND json_extract(CAST(binding AS TEXT),'$.orchestration_handle')=?", (project, handle)).fetchone()[0]
    if opens >= report.MAX_RUNS:
        return True
    mixed = owner._db.execute(
        "SELECT COUNT(*) FROM (SELECT run_id FROM project_registered_runs WHERE project_id=? "
        "AND orchestration_handle=? UNION SELECT json_extract(CAST(binding AS TEXT),'$.run_id') "
        "FROM project_host_opens WHERE project_id=? "
        "AND json_extract(CAST(binding AS TEXT),'$.orchestration_handle')=?)",
        (project, handle, project, handle)).fetchone()[0]
    return mixed >= report.MAX_RUNS


def _unchanged(current, *, waiting=False):
    return {'receipt': None, 'current': current, 'previous': None, 'handoff': None,
            'known': False, 'rolled_over': False, 'note': 'Waiting for active work.' if waiting else None}


def lifetime_call(owner, request, *, client_version, before_commit):
    payload, scope = request.payload, request.scope
    handle = payload['orchestration_handle']
    if request.operation == 'orchestration.packet':
        current = owner._current(scope, handle)
        if current['snapshot']['client_version'] != client_version:
            raise HistoryError('history_not_authorized')
        extras = owner._continuation(handle, current['snapshot']['sequence'])
        before_commit()
        return {'current': current, 'resume_state': extras.get('resume_state', {}), 'handoff': extras.get('handoff')}

    # Derived from identity, not parameters: changing the parameters cannot fork a replay.
    identity = keyed_digest(owner._key(), b'project-orchestration-handoff-v1\0' + canonical_json({
        'scope': scope.as_data(), 'request_id': request.request_id}))
    close_request = OrchestrationRequest(1, 'orchestration.close', 'rollover-close:' + identity, scope, {
        'orchestration_handle': handle, 'expected_sequence': payload['expected_sequence'],
        'status': 'completed', 'goal_tokens_used': None, 'goal_time_used_seconds': None})
    open_request = OrchestrationRequest(1, 'orchestration.open', 'rollover-open:' + identity, scope, {})
    close_key = keyed_digest(owner._key(), canonical_json({
        'scope_digest': owner._scope_digest(scope), 'request_id': close_request.request_id}))
    with owner._history._transaction():
        known = owner._db.execute('SELECT * FROM project_orchestration_requests WHERE project_id=? AND request_key=?',
                                  (scope.project_id, close_key)).fetchone()
        if known is not None:
            previous = owner._checkpoint(known['orchestration_handle'], known['sequence'],
                expected_request=(close_key, orchestration_request_digest(close_request)))
            extras = owner._continuation(handle, previous['snapshot']['sequence'])
            link = extras.get('handoff')
            if link is None or link['request_digest'] != orchestration_request_digest(request):
                raise HistoryError('history_conflict')
            # Ordinary replay validates both complete chains, original identities and host class.
            owner.apply(close_request, client_version=client_version, before_commit=lambda: None, _in_transaction=True)
            opened = owner.apply(open_request, client_version=client_version, before_commit=lambda: None, _in_transaction=True)
            before_commit()
            return {'previous': previous, **opened, 'handoff': link, 'known': True, 'rolled_over': True, 'note': None}
        current = owner._current(scope, handle)
        snapshot = current['snapshot']
        if snapshot['client_version'] != client_version:
            raise HistoryError('history_not_authorized')
        if snapshot['status'] not in {'active', 'paused'}:
            raise HistoryError('history_orchestration_state')
        if snapshot['sequence'] != payload['expected_sequence']:
            raise HistoryError('history_conflict')
        if payload['cause'] == 'capacity' and not _capacity_due(owner, snapshot):
            before_commit()
            return _unchanged(current)
        if owner._unfinished(scope, handle):
            before_commit()
            return _unchanged(current, waiting=True)
        next_handle = 'orc_' + keyed_digest(owner._key(), b'project-orchestration-successor-v1\0' + identity.encode())[:48]
        link = {'version': 1, 'handoff_id': identity, 'request_digest': orchestration_request_digest(request),
                'previous_handle': handle, 'next_handle': next_handle,
                **{name: payload[name] for name in ('session_id', 'next_session_id', 'cause')}}
        extras = owner._continuation(handle, snapshot['sequence'])
        extras['handoff'] = link
        previous = owner.apply(close_request, client_version=client_version, before_commit=lambda: None,
                               _in_transaction=True, _continuation=extras)['current']
        # Confirm the complete report is available before publishing either end. It keeps its own
        # independent bounds; a mixed row overflow must never be silently omitted from a hand-off.
        from .project_orchestration_report import orchestration_report
        orchestration_report(owner._history, snapshot['orchestration_id'])
        opened = owner.apply(open_request, client_version=client_version, before_commit=lambda: None,
                             _in_transaction=True, _continuation=extras, _handle=next_handle)
        before_commit()
        return {'previous': previous, **opened, 'handoff': link, 'rolled_over': True, 'note': None}
