"""Account-owner original-open request custody, before admission and claim.

Only this owner touches SQL. Original clocks survive queued signature work and
explicit retry; neither pending nor known metadata authorizes execution. Native
caller/current project/input custody is an independent server-adapter obligation.
"""
from dataclasses import dataclass, replace
import os
import threading

from .account_open_contracts import (AccountOpenContext, AccountOpenInput, OpenError, OpenOwnerLimits,
                                     MAX_ACCOUNT_OPEN_RECEIPT_BYTES, checked_account_open_reply)
from .contracts import routing_task_slice, validate_assignment, validate_task
from .history_contracts import _DIGEST, _ID, matches, valid_integer
from .history_contracts import AttemptBinding
from .result_contracts import ResultLeaseBinding
from .orchestration_api import OrchestrationScope, SCOPE_FIELDS
from .learning_contracts import (canonical_json, content_digest, external_id, keyed_digest, parse_bounded_json,
                                  provider_for_model, validate_execution_configuration, validate_learning_record)
from .learning_adapters import empty_usage
from .open_scope_contracts import open_inputs_digest, verify_open_scope
from .sql_read_budget import SQLReadError, SQLReadLimits

_REQUEST_DOMAIN = b'account-original-open-request-v1\0'
_RECEIPT_DOMAIN = b'account-original-open-receipt-v1\0'
_REQUEST_FIELDS = ('project_id', 'request_key', 'original_key', 'parameters_digest', 'handle_digest', 'run_id',
                   'state', 'opened_at_ms', 'observation_deadline_at_ms', 'receipt_digest')
_HOST_FIELDS = ('project_id', 'handle_digest', 'run_id', 'assignment_digest', 'task_digest', 'task_id_digest',
                'route_digest', 'configuration_digest', 'client_version', 'created_at_ms', 'expires_at_ms',
                'orchestration_id', 'observation_started_at_ms', 'observation_deadline_at_ms', 'agent_type', 'history_owner')

BRIDGE_PROVIDERS = frozenset({'codex', 'claude'})


@dataclass(frozen=True, repr=False)
class OpenExecution:
    assignment_id: str
    model: str
    effort: str
    role: str
    task_family: str
    task_slice: str
    topology: str
    configuration_digest: str
    assignment_digest: str
    task_digest: str
    task_id_digest: str
    route_digest: str
    orchestration_id: str
    orchestration_digest: str
    worker_outcome_digest: str | None
    worker_expires_at_ms: int | None
    agent_type: str
    hard_timeout_seconds: int


@dataclass(frozen=True, repr=False)
class OriginalOpenRequest:
    origin: object
    project_id: str
    runtime_digest: str
    caller_scope: str
    request_id: str
    wire_input: AccountOpenInput
    context_bytes: bytes
    scope_digest: str
    request_key: str
    original_key: str
    parameters_digest: str
    run_handle: str
    handle_digest: str
    run_id: str
    observation_seconds: int
    execution: OpenExecution
    opened_at_ms: int | None = None
    observation_deadline_at_ms: int | None = None
    journal_seal: str | None = None


# S1-U10: the stored open journal, receipt and admission checks below answer open_state_inconsistent;
# a misuse or wiring fault open_failed; a write already in progress open_account_busy.


def _owner(ledger):
    origin = getattr(ledger, '_connection_origin', None)
    if (type(origin) is not tuple or len(origin) != 2 or origin[0] != os.getpid()
            or origin[1] is not threading.current_thread()):
        raise OpenError('open_failed')
    ledger._connection._read_owner()


def _capture(ledger, project_id, runtime_digest, caller_scope, request_id, request, context, limits):
    _owner(ledger)
    if (type(request) is not AccountOpenInput or type(context) is not AccountOpenContext
            or type(limits) is not OpenOwnerLimits or not matches(caller_scope, _DIGEST)
            or not matches(request_id, _ID) or request.scope.project_id != project_id
            or request.scope.runtime_digest != runtime_digest):
        raise OpenError('open_invalid_request')
    limits.__post_init__()
    raw, captured = context.snapshot()
    if len(raw) > limits.max_context_bytes:
        raise OpenError('open_busy')
    context = AccountOpenContext.from_snapshot(captured)
    original = captured['prospective']
    expected = AccountOpenInput(context.prospective.scope, context.prospective.orchestration_handle,
        open_inputs_digest(*(original[n] for n in ('assignment', 'task', 'route', 'parent_grant'))))
    if expected != request:
        raise OpenError('open_not_authorized')
    key = ledger.state.digest_key()
    try:
        proof = verify_open_scope(key, request.scope, request.orchestration_handle, request.inputs_digest, captured['project_scope'])
        task = validate_task(original['task'])
        assignment = validate_assignment(original['assignment'], task=task, route=original['route'], parent_grant=original['parent_grant'])
        route = original['route']
        configuration = validate_execution_configuration(route['execution_configuration'])
        seconds = route.get('observation_deadline_seconds', assignment['bounds']['timeout_seconds'])
        facts = proof['facts']
        # A bridged child is a read-only Codex or Claude child of the provider its model names.
        provider = route.get('provider', 'codex')
        if (provider not in BRIDGE_PROVIDERS or route.get('dispatch') != 'bridge'
                or route.get('stage') not in {'execution', 'reconnaissance'}
                or route.get('sandbox_mode') != 'read-only' or provider_for_model(assignment['model']) != provider
                or any(not entry.endswith(':read') for entry in assignment['permissions']['filesystem'])
                or assignment['permissions']['mcps'] or facts['assignment_id'] != assignment['assignment_id']
                or facts['attempt'] != assignment['attempt'] or facts['task_family'] != task['family']
                or facts['configuration_digest'] != content_digest(configuration)
                or any(facts[name+'_digest'] != keyed_digest(key, canonical_json(original[name]))
                       for name in ('assignment', 'task', 'route', 'parent_grant'))
                or any(configuration[n] != assignment[n] for n in ('model', 'effort', 'role', 'verification_required'))):
            raise ValueError()
        if not valid_integer(seconds, minimum=1):
            raise ValueError()
        worker = facts['worker']
        worker_digest = None if original['worker'] is None else keyed_digest(key,
            b'project-open-worker-input-v1\0'+canonical_json(original['worker']))
        if ((worker is None) != (task.get('verification_for_outcome') is None)
                or (worker is None) != (original['worker'] is None) or facts['worker_context_digest'] != worker_digest
                or (worker is not None and (worker['host_outcome_digest'] != task['verification_for_outcome']
                    or worker['task_family'] != task['verification_for_family']
                    or worker['provider'] != task['verification_for_provider']
                    or assignment['role'] != provider + '_agents_verification_worker'
                    or assignment['context'] != [worker['host_outcome_digest']]))):
            raise ValueError()
    except Exception:
        raise OpenError('open_not_authorized') from None
    scope = keyed_digest(key, b'account-original-open-scope-v1\0'+canonical_json({
        'scope': request.scope.as_data(), 'orchestration_digest': keyed_digest(key, request.orchestration_handle),
        'caller_scope': caller_scope}))
    request_key = keyed_digest(key, b'account-original-open-id-v1\0'+canonical_json({'scope_digest': scope, 'request_id': request_id}))
    original_key = keyed_digest(key, b'account-original-open-assignment-v1\0'+canonical_json({
        'scope_digest': scope, 'task_id': task['id'], 'stage': route['stage'], 'attempt': assignment['attempt']}))
    parameters = keyed_digest(key, b'account-original-open-parameters-v1\0'+canonical_json(original))
    # The original opaque handle is recoverable without persisting a bearer token.
    handle = 'hrn_'+keyed_digest(key, b'account-original-open-handle-v1\0'+request_key.encode())[:48]
    execution = OpenExecution(*(assignment[n] for n in ('assignment_id', 'model', 'effort', 'role')),
        task['family'], routing_task_slice(task), configuration['topology'], facts['configuration_digest'],
        facts['assignment_digest'], facts['task_digest'], keyed_digest(key, task['id']), facts['route_digest'],
        facts['orchestration_id'], keyed_digest(key, request.orchestration_handle), task.get('verification_for_outcome'),
        None if facts['worker'] is None else facts['worker']['expires_at_ms'],
        route.get('agent_type', assignment['role']),
        min(route['bounds']['timeout_seconds'], assignment['bounds']['timeout_seconds']))
    return OriginalOpenRequest(ledger._attestation_origin, project_id, runtime_digest, caller_scope, request_id,
        request, raw, scope, request_key, original_key, parameters, handle, keyed_digest(key, handle), external_id(key, 'run', handle), seconds, execution)


def _provider(request):
    """The admitted child's provider, from the model the original open validated."""
    return provider_for_model(request.execution.model)


def start_metadata(ledger, request, opened):
    e = request.execution
    return validate_learning_record({'schema_version': 1, 'record_type': 'event',
        'record_id': external_id(ledger.state.digest_key(), 'evt', f'{request.run_id}\0{e.assignment_id}\0started'),
        'run_id': request.run_id, 'turn_id': None, 'assignment_id': e.assignment_id, 'parent_run_id': None,
        'observed_at_ms': opened, 'event_code': 'subagent_started', 'model': e.model, 'effort': e.effort,
        'role': e.role, 'task_family': e.task_family, 'task_slice': e.task_slice, 'topology': e.topology,
        'configuration_digest': e.configuration_digest, 'status': 'started', 'stop_reason': None,
        'tool_digest': None, 'item_digest': None, 'coverage': 'lifecycle_partial', 'usage': empty_usage()})


def verification_metadata(request, opened):
    if request.execution.worker_outcome_digest is None:
        return None
    data = {'schema_version': 1, 'record_type': 'verification_binding', 'run_id': request.run_id,
        'worker_outcome_digest': request.execution.worker_outcome_digest, 'observed_at_ms': opened}
    return validate_learning_record({**data, 'record_id': 'verification_binding:'+content_digest(data)})


def _journal_seal(key, data):
    return keyed_digest(key, _REQUEST_DOMAIN+canonical_json({n: data[n] for n in _REQUEST_FIELDS}))


def request_journal(ledger, request):
    row = ledger._connection.execute('SELECT * FROM account_open_requests WHERE project_id=? AND request_key=?',
        (request.project_id, request.request_key)).fetchone()
    if row is None:
        return None
    data = dict(row)
    if any(data[n] != getattr(request, n) for n in ('project_id', 'request_key', 'original_key', 'parameters_digest', 'handle_digest', 'run_id')):
        raise OpenError('open_conflict')  # the same request identity with different inputs
    if (type(data['state']) is not str or data['state'] not in {'pending', 'committed'}
            or not valid_integer(data['opened_at_ms'])
            or not valid_integer(data['observation_deadline_at_ms'], minimum=data['opened_at_ms']+1)
            or (data['receipt_digest'] is None) != (data['state'] == 'pending')
            or (data['receipt_digest'] is not None and not matches(data['receipt_digest'], _DIGEST))
            or not matches(data['request_seal'], _DIGEST)
            or data['request_seal'] != _journal_seal(ledger.state.digest_key(), data)):
        raise OpenError('open_state_inconsistent')  # S1-U10e: the stored journal does not verify
    return data


def _receipt(ledger, request, journal):
    header = ledger._connection.execute('SELECT project_id,handle_digest,run_id,request_key,scope_digest,parameters_digest,'
        'length(content) AS content_bytes,content_seal FROM account_open_receipts WHERE project_id=? AND handle_digest=?',
        (request.project_id, request.handle_digest)).fetchone()
    if header is None:
        if journal is not None and journal['state'] == 'committed':
            raise OpenError('open_state_inconsistent')
        return None
    if journal is None or journal['state'] != 'committed':
        raise OpenError('open_state_inconsistent')
    if any(header[n] != getattr(request, n) for n in ('project_id', 'handle_digest', 'request_key', 'scope_digest', 'parameters_digest')):
        raise OpenError('open_conflict')
    if (not valid_integer(header['content_bytes'], minimum=1, maximum=MAX_ACCOUNT_OPEN_RECEIPT_BYTES)
            or not matches(header['content_seal'], _DIGEST) or header['content_seal'] != journal['receipt_digest']):
        raise OpenError('open_state_inconsistent')
    raw = bytes(ledger._connection.execute('SELECT content FROM account_open_receipts WHERE project_id=? AND handle_digest=?',
        (request.project_id, request.handle_digest)).fetchone()[0])
    if keyed_digest(ledger.state.digest_key(), _RECEIPT_DOMAIN+raw) != header['content_seal']:
        raise OpenError('open_state_inconsistent')
    data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_OPEN_RECEIPT_BYTES)
    if (type(data) is not dict or data.keys() != {'schema_version', 'scope_digest', 'parameters_digest', 'original_key',
            'view', 'host', 'launch', 'admission', 'scope', 'orchestration_digest', 'start_event', 'verification_binding'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1 or canonical_json(data) != raw
            or any(data[n] != getattr(request, n) for n in ('scope_digest', 'parameters_digest', 'original_key'))
            or type(data['view']) is not dict or 'run_handle' in data['view'] or 'known' in data['view'] or 'receipt_digest' in data['view']):
        raise OpenError('open_conflict')
    if (canonical_json(data['scope']) != canonical_json(request.wire_input.scope.as_data())
            or data['orchestration_digest'] != request.execution.orchestration_digest):
        raise OpenError('open_conflict')
    view = checked_account_open_reply({**data['view'], 'receipt_digest': header['content_seal'], 'known': True,
        'run_handle': request.run_handle if data['view'].get('state') == 'opened' else None}, request.wire_input)
    if (view['scope_digest'] != request.scope_digest or view['recorded_at_ms'] != journal['opened_at_ms']
            or header['run_id'] != view['run_id']):
        raise OpenError('open_conflict')
    binding = ledger._connection.execute('SELECT '+','.join(_HOST_FIELDS)+' FROM host_run_bindings WHERE project_id=? AND handle_digest=?',
        (request.project_id, request.handle_digest)).fetchone()
    if view['state'] == 'opened':
        if (binding is None or type(data['host']) is not dict or data['host'].keys() != set(_HOST_FIELDS)
                or canonical_json(dict(binding)) != canonical_json(data['host']) or binding['history_owner'] != 'project'
                or view['run_id'] != request.run_id or view['observation_deadline_at_ms'] != journal['observation_deadline_at_ms']):
            raise OpenError('open_state_inconsistent')
        if (data['start_event'] != start_metadata(ledger, request, journal['opened_at_ms'])
                or data['verification_binding'] != verification_metadata(request, journal['opened_at_ms'])):
            raise OpenError('open_conflict')
        inputs = ledger._connection.execute('SELECT run_id,runtime_digest,provider,model,effort,hard_timeout_seconds '
            'FROM launch_inputs WHERE project_id=? AND handle_digest=?', (request.project_id, request.handle_digest)).fetchone()
        provenance = ledger._connection.execute("SELECT client_version,session_digest,created_at_ms FROM run_provenance "
            "WHERE project_id=? AND run_id=? AND producer='host'", (request.project_id, request.run_id)).fetchone()
        session = ledger._connection.execute("SELECT created_at_ms FROM sessions WHERE project_id=? AND session_digest=? AND producer='host'",
            (request.project_id, request.handle_digest)).fetchone()
        if (inputs is None or tuple(inputs) != (request.run_id, request.runtime_digest, _provider(request),
                request.execution.model, request.execution.effort, request.execution.hard_timeout_seconds)
                or canonical_json(dict(inputs)) != canonical_json(data['launch'])
                or provenance is None or tuple(provenance) != ('bridge-v1', request.handle_digest, journal['opened_at_ms'])
                or session is None or session[0] != journal['opened_at_ms']):
            raise OpenError('open_state_inconsistent')
    elif binding is not None or data['host'] is not None or data['launch'] is not None:
        raise OpenError('open_conflict')
    elif data['start_event'] is not None or data['verification_binding'] is not None:
        raise OpenError('open_conflict')
    admission = validate_learning_record(data['admission'])
    if (admission['record_type'] != 'admission_decision' or admission['provider'] != _provider(request)
            or admission['record_id'] != view['admission_record_id'] or content_digest(admission) != view['admission_content_digest']
            or admission['reason_code'] != view['reason_code'] or admission['run_id'] != view['run_id']
            or (admission['decision'] == 'pass') != (view['state'] == 'opened')):
        raise OpenError('open_state_inconsistent')  # the stored receipt and its admission disagree
    stored = ledger._connection.execute('SELECT source,content_digest,content FROM records WHERE project_id=? AND record_id=?',
        (request.project_id, view['admission_record_id'])).fetchone()
    if (stored is None or stored['source'] != 'lifecycle' or stored['content_digest'] != view['admission_content_digest']
            or bytes(stored['content']) != canonical_json(admission)):
        raise OpenError('open_state_inconsistent')
    return view


def current_open_request(ledger, request):
    _owner(ledger)
    if type(request) is not OriginalOpenRequest or request.origin is not ledger._attestation_origin:
        raise OpenError('open_not_authorized')
    project = ledger._connection.execute('SELECT status FROM projects WHERE project_id=?', (request.project_id,)).fetchone()
    if project is None or project[0] != 'active':
        raise OpenError('open_project_inactive')
    journal = request_journal(ledger, request)
    known = _receipt(ledger, request, journal)
    if known is not None:
        return journal, known
    # Neither another request ID nor another assignment label can reopen the
    # same original task stage/attempt. A verified reconnaissance promotion has
    # its own routed stage, rather than resetting the original task identity.
    existing = ledger._connection.execute('SELECT request_key FROM account_open_requests WHERE project_id=? AND original_key=?',
        (request.project_id, request.original_key)).fetchone()
    if existing is not None and existing[0] != request.request_key:
        raise OpenError('open_conflict')
    for table in ('host_run_bindings', 'launch_inputs', 'launch_claims', 'active_run_capacity'):
        if ledger._connection.execute('SELECT 1 FROM '+table+' WHERE project_id=? AND handle_digest=? LIMIT 1',
                (request.project_id, request.handle_digest)).fetchone() is not None:
            raise OpenError('open_state_inconsistent')
    if ledger._connection.execute('SELECT 1 FROM run_provenance WHERE project_id=? AND run_id=? LIMIT 1',
            (request.project_id, request.run_id)).fetchone() is not None:
        raise OpenError('open_state_inconsistent')
    if ledger._connection.execute("SELECT 1 FROM records WHERE project_id=? AND record_type IN "
            "('admission_decision','event','verification_binding') AND json_extract(CAST(content AS TEXT),'$.run_id')=? LIMIT 1",
            (request.project_id, request.run_id)).fetchone() is not None:
        raise OpenError('open_state_inconsistent')
    return journal, None


def preflight_open(ledger, project_id, *, runtime_digest, caller_scope, request_id, request, context,
                    limits, work_limits, before_commit):
    if type(work_limits) is not SQLReadLimits or not callable(before_commit):
        raise OpenError('open_invalid_request')
    captured = _capture(ledger, project_id, runtime_digest, caller_scope, request_id, request, context, limits)
    before_commit()
    _owner(ledger)
    try:
        with ledger._held():
            if ledger._connection.in_transaction:
                raise OpenError('open_failed')  # nesting is a misuse; contention waits elsewhere
            journal, known = ledger._connection.read_call(work_limits, lambda: current_open_request(ledger, captured))
            if known is not None:
                before_commit()
                _owner(ledger)
                if ledger._connection.read_call(work_limits, lambda: current_open_request(ledger, captured)) != (journal, known):
                    raise OpenError('open_conflict')
                return captured, known
            with ledger._account_transaction():
                journal, known = ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, captured))
                if known is not None:
                    before_commit()
                    if ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, captured)) != (journal, known):
                        raise OpenError('open_conflict')
                    return captured, known
                if journal is None:
                    positions = ledger._connection.transaction_read_call(work_limits, lambda: ledger._connection.execute(
                        'SELECT state FROM account_open_requests WHERE project_id=? LIMIT ?',
                        (project_id, limits.max_retained+1)).fetchall())
                    if (len(positions) >= limits.max_retained
                            or sum(row[0] == 'pending' for row in positions) >= limits.max_pending):
                        raise OpenError('open_busy')
                    opened = ledger._clock_ms()
                    seconds = captured.observation_seconds
                    created = ledger._connection.transaction_read_call(work_limits, lambda: ledger._connection.execute(
                        'SELECT created_at_ms FROM projects WHERE project_id=?', (project_id,)).fetchone())
                    if (created is None or not valid_integer(created[0]) or not valid_integer(opened, minimum=created[0])
                            or not valid_integer(seconds, minimum=1)):
                        raise OpenError('open_state_inconsistent')
                    deadline = opened+seconds*1000
                    if not valid_integer(deadline, minimum=opened+1):
                        raise OpenError('open_state_inconsistent')
                    journal = {n: getattr(captured, n) for n in ('project_id', 'request_key', 'original_key', 'parameters_digest', 'handle_digest', 'run_id')}
                    journal.update(state='pending', opened_at_ms=opened, observation_deadline_at_ms=deadline, receipt_digest=None)
                    journal['request_seal'] = _journal_seal(ledger.state.digest_key(), journal)
                    ledger._connection.execute('INSERT INTO account_open_requests('+','.join((*_REQUEST_FIELDS, 'request_seal'))
                        +') VALUES('+','.join('?' for _ in range(len(_REQUEST_FIELDS)+1))+')',
                        tuple(journal[n] for n in (*_REQUEST_FIELDS, 'request_seal')))
                before_commit()
                _owner(ledger)
                if ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, captured)) != (journal, None):
                    raise OpenError('open_conflict')
            return replace(captured, opened_at_ms=journal['opened_at_ms'],
                observation_deadline_at_ms=journal['observation_deadline_at_ms'], journal_seal=journal['request_seal']), None
    except SQLReadError as error:  # S1-U10b
        raise OpenError('open_account_busy' if error.reason_code == 'sql_read_busy' else 'open_failed') from None


def _returned_admission(ledger, request, pool, handle):
    from .signature_worker_pool import SignatureWorkerPool
    intake = getattr(ledger, '_signature_job_intake', None)
    if (type(pool) is not SignatureWorkerPool or intake is None or pool._intake is not intake
            or getattr(intake, '_signature_worker_pool', None) is not pool or intake._views._ledger is not ledger):
        raise OpenError('open_failed')
    pool._owner()
    result = pool.result(handle)
    inputs = intake.returned_inputs(handle)
    snapshot, work_limits = intake.returned_admission(handle)
    if (inputs.origin is not ledger._attestation_origin or inputs.project_id != request.project_id
            or snapshot.origin is not intake._views._admission_capture_origin or snapshot.project_id != request.project_id
            or snapshot.provider != _provider(request) or inputs.provider != _provider(request)):
        raise OpenError('open_failed')
    return intake, result, inputs, snapshot, work_limits


def _fresh_admission(ledger, request, intake, snapshot, work_limits, *, appended=None):
    from .admission_inputs import AdmissionCaptureLimits
    from .claim_admission import same_admission_inputs
    signer = ledger._current_signer_bytes()
    policy = intake._views._claim_policy(_provider(request))
    limits = intake._limits
    current = ledger._connection.transaction_read_call(work_limits, lambda: intake._views._capture_admission_rows_locked(
        request.project_id, _provider(request), policy, AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer, snapshot.anchor_ms))
    expected = snapshot
    if appended is not None:
        originals = [row for row in snapshot.rows if row[1] == request.project_id and row[2] == appended['record_id']]
        rows = [row for row in current.rows if row[1] == request.project_id and row[2] == appended['record_id']]
        if (len(rows) != 1 or rows[0][4] != 'admission_decision' or rows[0][5] != canonical_json(appended)
                or rows[0][6] != content_digest(appended) or rows[0][7] != 'lifecycle'
                or (originals and originals != rows)):
            raise OpenError('open_preparation_stale')
        if not originals:
            expected = replace(snapshot, rows=tuple(sorted((*snapshot.rows, rows[0]), key=lambda row: row[0])))
    if not same_admission_inputs(expected, current):
        raise OpenError('open_preparation_stale')
    now = current.captured_at_ms
    if (not valid_integer(now, minimum=request.opened_at_ms)
            or now >= request.observation_deadline_at_ms
            or (request.execution.worker_expires_at_ms is not None and now >= request.execution.worker_expires_at_ms)):
        raise OpenError('open_preparation_stale')
    return now


def finish_open_from_signature_job(ledger, request, pool, handle, *, before_commit):
    """Commit only an actual returned registered worker, never caller verdicts.

    No model is launched and no shared execution slot is released. Original
    input custody, native caller/current permission and protected publication
    remain independent requirements of the trusted account-service adapter.
    """
    from .admission_metadata import API_KEY_VARIABLES
    from .claim_admission import PreparedClaimAdmission, admission_signature_rows
    from .account_open_admission import open_admission_inputs, evaluate_original_open
    from .learning_store import BINDING_HELD, HOST_RUN_TTL_MS, MAX_PENDING_HOST_RUNS
    _owner(ledger)
    if (type(request) is not OriginalOpenRequest or request.origin is not ledger._attestation_origin
            or request.journal_seal is None or not callable(before_commit)):
        raise OpenError('open_not_authorized')
    intake, result, inputs, snapshot, work_limits = _returned_admission(ledger, request, pool, handle)
    before_commit()
    _owner(ledger)
    try:
        with ledger._held():
            if ledger._connection.in_transaction:
                raise OpenError('open_failed')  # nesting is a misuse; contention waits elsewhere
            journal, known = ledger._connection.read_call(work_limits, lambda: current_open_request(ledger, request))
            if known is not None:
                before_commit()
                if ledger._connection.read_call(work_limits, lambda: current_open_request(ledger, request)) != (journal, known):
                    raise OpenError('open_conflict')
                return known
        if result.reason != 'signature_execution_completed':
            raise OpenError('open_preparation_failed')
        if (type(result.prepared) is not PreparedClaimAdmission or result.prepared.snapshot is not snapshot
                or snapshot.owner != inputs.owner or snapshot.signer != inputs.signer
                or inputs.rows != admission_signature_rows(snapshot)):
            raise OpenError('open_preparation_failed')
        verified = frozenset(inputs.rows[index][0] for index in result.indices)
        prepared = open_admission_inputs(result.prepared, verified)
        # Parse and validate start/dependency metadata before the writer begins.
        start = start_metadata(ledger, request, request.opened_at_ms)
        verification = verification_metadata(request, request.opened_at_ms)
        before_commit()
        _owner(ledger)
        with ledger._account_transaction():
            journal, known = ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, request))
            if known is not None:
                before_commit()
                if ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, request)) != (journal, known):
                    raise OpenError('open_conflict')
                return known
            if (journal is None or journal['state'] != 'pending' or journal['request_seal'] != request.journal_seal
                    or journal['opened_at_ms'] != request.opened_at_ms
                    or journal['observation_deadline_at_ms'] != request.observation_deadline_at_ms):
                raise OpenError('open_state_inconsistent')
            now = _fresh_admission(ledger, request, intake, snapshot, work_limits)
            # Only presence markers are copied. The daemon environment is an
            # additional refusal guard, never native child/key-free proof.
            guard = {name: 'present' for name in API_KEY_VARIABLES if os.environ.get(name)}
            decision = evaluate_original_open(prepared, timestamp=now, guard_environment=guard)
            passed = decision['decision'] == 'pass'
            if passed:
                pending = ledger._connection.transaction_read_call(work_limits, lambda: ledger._connection.execute(
                    # O0b B0-1 (narrowed): only live bindings count, pending and not past their expiry, so
                    # expired ones never fill a project for good.
                    "SELECT h.handle_digest FROM host_run_bindings h WHERE h.project_id=? AND h.binding_state='pending' "
                    f"AND h.expires_at_ms>? AND {BINDING_HELD} LIMIT ?",
                    (request.project_id, now, MAX_PENDING_HOST_RUNS)).fetchall())
                if len(pending) >= MAX_PENDING_HOST_RUNS:
                    raise OpenError('open_busy')
                expires = request.opened_at_ms+HOST_RUN_TTL_MS
                if not valid_integer(expires, minimum=now+1):
                    raise OpenError('open_preparation_stale')
                e = request.execution
                ledger._connection.execute('INSERT INTO host_run_bindings('
                    'project_id,handle_digest,run_id,assignment_digest,task_digest,task_id_digest,route_digest,configuration_digest,'
                    'client_version,created_at_ms,expires_at_ms,orchestration_id,agent_type,binding_state,'
                    'observation_started_at_ms,observation_deadline_at_ms,last_progress_at_ms,deadline_observed,history_owner) '
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,0,'project')",
                    (request.project_id, request.handle_digest, request.run_id, e.assignment_digest, e.task_digest, e.task_id_digest,
                     e.route_digest, e.configuration_digest, 'bridge-v1', request.opened_at_ms, expires, e.orchestration_id,
                     e.agent_type, request.opened_at_ms, request.observation_deadline_at_ms, request.opened_at_ms))
                ledger._connection.execute('INSERT INTO launch_inputs VALUES(?,?,?,?,?,?,?,?)',
                    (request.project_id, request.handle_digest, request.run_id, request.runtime_digest, _provider(request), e.model, e.effort, e.hard_timeout_seconds))
                ledger._connection.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?,?)',
                    (request.project_id, request.handle_digest, 'host', request.opened_at_ms))
                ledger._connection.execute('INSERT INTO run_provenance VALUES(?,?,?,?,?,?)',
                    (request.project_id, request.run_id, 'host', 'bridge-v1', request.handle_digest, request.opened_at_ms))
            admission = ledger._store_admission_locked(request.project_id, decision,
                run_id=request.run_id if passed else None, timestamp=now)
            view = {'state': 'opened' if passed else 'denied', 'run_id': request.run_id if passed else None,
                'inputs_digest': request.wire_input.inputs_digest, 'scope_digest': request.scope_digest,
                'admission_record_id': admission['record_id'], 'admission_content_digest': content_digest(admission),
                'reason_code': decision['reason_code'], 'recorded_at_ms': request.opened_at_ms,
                'observation_deadline_at_ms': request.observation_deadline_at_ms if passed else None}
            host = None if not passed else dict(ledger._connection.execute('SELECT '+','.join(_HOST_FIELDS)
                +' FROM host_run_bindings WHERE project_id=? AND handle_digest=?', (request.project_id, request.handle_digest)).fetchone())
            launch = None if not passed else dict(ledger._connection.execute(
                'SELECT run_id,runtime_digest,provider,model,effort,hard_timeout_seconds FROM launch_inputs '
                'WHERE project_id=? AND handle_digest=?', (request.project_id, request.handle_digest)).fetchone())
            data = {'schema_version': 1, 'scope_digest': request.scope_digest, 'parameters_digest': request.parameters_digest,
                'original_key': request.original_key, 'view': view, 'host': host, 'launch': launch, 'admission': admission,
                'scope': request.wire_input.scope.as_data(), 'orchestration_digest': request.execution.orchestration_digest,
                'start_event': start if passed else None, 'verification_binding': verification if passed else None}
            raw = canonical_json(data)
            if not 0 < len(raw) <= MAX_ACCOUNT_OPEN_RECEIPT_BYTES:
                raise OpenError('open_failed')
            seal = keyed_digest(ledger.state.digest_key(), _RECEIPT_DOMAIN+raw)
            ledger._connection.execute('INSERT INTO account_open_receipts VALUES(?,?,?,?,?,?,?,?)',
                (request.project_id, request.handle_digest, request.run_id if passed else None, request.request_key,
                 request.scope_digest, request.parameters_digest, raw, seal))
            final = {**journal, 'state': 'committed', 'receipt_digest': seal}
            final['request_seal'] = _journal_seal(ledger.state.digest_key(), final)
            changed = ledger._connection.execute("UPDATE account_open_requests SET state='committed',receipt_digest=?,request_seal=? "
                "WHERE project_id=? AND request_key=? AND state='pending' AND request_seal=?",
                (seal, final['request_seal'], request.project_id, request.request_key, request.journal_seal)).rowcount
            if changed != 1:
                raise OpenError('open_conflict')
            before_commit()
            _owner(ledger)
            _fresh_admission(ledger, request, intake, snapshot, work_limits, appended=admission)
            if {name: 'present' for name in API_KEY_VARIABLES if os.environ.get(name)} != guard:
                raise OpenError('open_preparation_stale')
            current, reply = ledger._connection.transaction_read_call(work_limits, lambda: current_open_request(ledger, request))
            if current != final or reply is None:
                raise OpenError('open_conflict')
        return {**reply, 'known': False}
    except SQLReadError as error:  # S1-U10b: a busy database is the account's; anything else failed
        raise OpenError('open_account_busy' if error.reason_code == 'sql_read_busy' else 'open_failed') from None


def managed_open_metadata(ledger, expected):
    try:
        return _managed_open_metadata(ledger, expected)
    except OpenError:
        raise
    except Exception:
        raise OpenError('open_conflict') from None


def _managed_open_metadata(ledger, expected):
    """Read original managed facts against an INDEPENDENT expected lease scope.

    None means an explicitly legacy host binding, never a missing managed
    receipt. The caller still verifies its actual storage lease/grant and native
    authority. No caller chooses a record, source mode or replacement history.
    """
    _owner(ledger)
    if type(expected) not in (ResultLeaseBinding, AttemptBinding):
        raise OpenError('open_invalid_request')
    expected.as_data()
    key = ledger.state.digest_key()
    handle = keyed_digest(key, expected.run_handle)
    host = ledger._connection.execute('SELECT '+','.join(_HOST_FIELDS)+' FROM host_run_bindings WHERE project_id=? AND handle_digest=?',
        (expected.project_id, handle)).fetchone()
    receipt = ledger._connection.execute('SELECT request_key,scope_digest,parameters_digest,run_id,length(content) AS size,content_seal '
        'FROM account_open_receipts WHERE project_id=? AND handle_digest=?', (expected.project_id, handle)).fetchone()
    journal = ledger._connection.execute('SELECT * FROM account_open_requests WHERE project_id=? AND handle_digest=?',
        (expected.project_id, handle)).fetchone()
    if host is not None and host['history_owner'] == 'legacy':
        if receipt is not None or journal is not None:
            raise OpenError('open_conflict')
        return None
    if host is None or host['history_owner'] != 'project' or receipt is None or journal is None:
        raise OpenError('open_state_inconsistent')
    journal = dict(journal)
    if (journal['project_id'] != expected.project_id or journal['handle_digest'] != handle
            or journal['state'] != 'committed' or journal['run_id'] != expected.run_id
            or not matches(journal['request_seal'], _DIGEST) or journal['request_seal'] != _journal_seal(key, journal)
            or any(receipt[n] != journal[n] for n in ('request_key', 'parameters_digest', 'run_id'))
            or not valid_integer(receipt['size'], minimum=1, maximum=MAX_ACCOUNT_OPEN_RECEIPT_BYTES)
            or not matches(receipt['content_seal'], _DIGEST) or receipt['content_seal'] != journal['receipt_digest']):
        raise OpenError('open_state_inconsistent')  # the stored journal and receipt do not verify together
    original_handle = 'hrn_'+keyed_digest(key, b'account-original-open-handle-v1\0'+journal['request_key'].encode())[:48]
    if original_handle != expected.run_handle or external_id(key, 'run', original_handle) != expected.run_id:
        raise OpenError('open_conflict')
    raw = bytes(ledger._connection.execute('SELECT content FROM account_open_receipts WHERE project_id=? AND handle_digest=?',
        (expected.project_id, handle)).fetchone()[0])
    if keyed_digest(key, _RECEIPT_DOMAIN+raw) != receipt['content_seal']:
        raise OpenError('open_conflict')
    data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_OPEN_RECEIPT_BYTES)
    fields = {'schema_version', 'scope_digest', 'parameters_digest', 'original_key', 'view', 'host', 'launch', 'admission',
              'scope', 'orchestration_digest', 'start_event', 'verification_binding'}
    if (type(data) is not dict or data.keys() != fields or type(data['schema_version']) is not int or data['schema_version'] != 1
            or canonical_json(data) != raw or canonical_json(data['host']) != canonical_json(dict(host))
            or data['scope_digest'] != receipt['scope_digest'] or data['parameters_digest'] != journal['parameters_digest']
            or data['original_key'] != journal['original_key'] or type(data['scope']) is not dict):
        raise OpenError('open_conflict')
    scope = OrchestrationScope(**data['scope'])
    actual_scope = OrchestrationScope(**{n: getattr(expected, n) for n in SCOPE_FIELDS})
    if (scope != actual_scope or data['orchestration_digest'] != keyed_digest(key, expected.orchestration_handle)
            or host['orchestration_id'] != external_id(key, 'orc', expected.orchestration_handle)
            or host['run_id'] != expected.run_id or host['created_at_ms'] != journal['opened_at_ms']
            or host['observation_started_at_ms'] != journal['opened_at_ms']
            or host['observation_deadline_at_ms'] != journal['observation_deadline_at_ms'] or host['client_version'] != 'bridge-v1'):
        raise OpenError('open_conflict')
    view = data['view']
    if type(view) is not dict or 'run_handle' in view or 'known' in view or 'receipt_digest' in view:
        raise OpenError('open_conflict')
    request = AccountOpenInput(actual_scope, expected.orchestration_handle, view.get('inputs_digest'))
    checked = checked_account_open_reply({**view, 'run_handle': expected.run_handle, 'known': True,
                                          'receipt_digest': receipt['content_seal']}, request)
    if (checked['state'] != 'opened' or checked['run_id'] != expected.run_id
            or checked['recorded_at_ms'] != journal['opened_at_ms']
            or checked['observation_deadline_at_ms'] != journal['observation_deadline_at_ms']):
        raise OpenError('open_conflict')
    admission, event = validate_learning_record(data['admission']), validate_learning_record(data['start_event'])
    inputs = ledger._connection.execute('SELECT run_id,runtime_digest,provider,model,effort,hard_timeout_seconds '
        'FROM launch_inputs WHERE project_id=? AND handle_digest=?', (expected.project_id, handle)).fetchone()
    provenance = ledger._connection.execute("SELECT client_version,session_digest,created_at_ms FROM run_provenance "
        "WHERE project_id=? AND run_id=? AND producer='host'", (expected.project_id, expected.run_id)).fetchone()
    session = ledger._connection.execute("SELECT created_at_ms FROM sessions WHERE project_id=? AND session_digest=? AND producer='host'",
        (expected.project_id, handle)).fetchone()
    if (inputs is None or canonical_json(dict(inputs)) != canonical_json(data['launch'])
            or inputs['run_id'] != expected.run_id or inputs['runtime_digest'] != expected.runtime_digest
            or inputs['provider'] not in BRIDGE_PROVIDERS or provider_for_model(inputs['model']) != inputs['provider']
            or provenance is None
            or tuple(provenance) != ('bridge-v1', handle, journal['opened_at_ms'])
            or session is None or session[0] != journal['opened_at_ms']
            or admission['record_type'] != 'admission_decision' or admission['decision'] != 'pass' or admission['provider'] != inputs['provider']
            or admission['run_id'] != expected.run_id or admission['record_id'] != view['admission_record_id']
            or content_digest(admission) != view['admission_content_digest']
            or event['record_type'] != 'event' or event['run_id'] != expected.run_id
            or event['event_code'] != 'subagent_started' or event['status'] != 'started'
            or event['configuration_digest'] != host['configuration_digest'] or event['observed_at_ms'] != journal['opened_at_ms']
            or event['model'] != inputs['model'] or event['effort'] != inputs['effort']
            or event['record_id'] != external_id(key, 'evt', f"{expected.run_id}\0{event['assignment_id']}\0started")
            or event['task_slice'].split('-')[0] != event['task_family']):
        raise OpenError('open_state_inconsistent')  # stored launch facts disagree with the receipt
    stored = ledger._connection.execute('SELECT source,content,content_digest FROM records WHERE project_id=? AND record_id=?',
        (expected.project_id, admission['record_id'])).fetchone()
    if stored is None or tuple(stored) != ('lifecycle', canonical_json(admission), content_digest(admission)):
        raise OpenError('open_state_inconsistent')
    verification = data['verification_binding']
    if verification is not None:
        verification = validate_learning_record(verification)
        if (verification['record_type'] != 'verification_binding' or verification['run_id'] != expected.run_id
                or verification['observed_at_ms'] != journal['opened_at_ms']
                or verification['record_id'] != 'verification_binding:'+content_digest({n: v for n, v in verification.items() if n != 'record_id'})):
            raise OpenError('open_conflict')
    return data


def original_orchestration_digest(ledger, expected):
    data = managed_open_metadata(ledger, expected)
    if data is not None:
        return data['orchestration_digest']
    row = ledger._connection.execute('SELECT o.handle_digest FROM host_run_bindings h JOIN orchestrations o '
        'ON o.project_id=h.project_id AND o.orchestration_id=h.orchestration_id WHERE h.project_id=? AND h.handle_digest=?',
        (expected.project_id, keyed_digest(ledger.state.digest_key(), expected.run_handle))).fetchone()
    if row is None:
        raise OpenError('open_state_inconsistent')
    return row[0]
