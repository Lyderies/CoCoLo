"""Fixed account-owner read of original registered-run provenance.

No admission/registration/launch is performed. Native service identity and
protected publication are independent gates; MAC-bound metadata is not proof
of a native caller, process cleanup, capacity release or current active state.
"""
import os
import secrets
import threading

from .account_claim_contracts import ClaimError, LaunchClaimInput
from .claim_storage import capture_storage_evidence, verify_bound_storage_evidence
from .execution_grant import sign_execution_grant
from .history_contracts import AttemptBinding
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, content_digest, external_id, keyed_digest, parse_bounded_json, validate_learning_record
from .result_contracts import LEASE_SCOPE_FIELDS
from .run_provenance import RunProvenanceInput, decode_run_provenance_input, seal_run_provenance
from .sql_read_budget import SQLReadError, SQLReadLimits


def attest_run_provenance(ledger, project_id, *, runtime_digest, caller_scope, request,
                         storage_binding, storage_evidence, work_limits):
    origin = getattr(ledger, '_connection_origin', None)
    if (type(origin) is not tuple or len(origin) != 2 or origin[0] != os.getpid()
            or origin[1] is not threading.current_thread()):
        raise ClaimError('claim_execution_invalid')
    if type(request) is not RunProvenanceInput or type(work_limits) is not SQLReadLimits:
        raise ClaimError('claim_execution_invalid')
    request = decode_run_provenance_input(request.payload())
    work_limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms, work_limits.max_value_bytes)
    expected, raw = capture_storage_evidence(storage_binding, storage_evidence)
    key = ledger.state.digest_key()
    lease = verify_bound_storage_evidence(key, expected, raw)
    registration = request.registration
    if (expected.project_id != project_id or expected.runtime_digest != runtime_digest
            or expected.run_handle != registration.run_handle or expected.claim_request_id != registration.claim_request_id
            or lease.claim_id != registration.bridge_run_id or lease.claim_digest != registration.claim_digest):
        raise ClaimError('claim_execution_invalid')
    from .account_api import AccountRequest, account_request_digest

    launch = LaunchClaimInput(registration.run_handle, registration.identity_digest)
    original = AccountRequest(1, 'account.claim_launch', registration.claim_request_id, project_id, runtime_digest, launch.payload())
    if expected.claim_request_digest != account_request_digest(original):
        raise ClaimError('claim_execution_invalid')
    # Known receipt lookup preserves its original clock/window and caller scope.
    # This fixed path never calls register_execution to create a missing row.
    try:
        context, known, _, _ = ledger._claim_preflight(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                      request_id=registration.claim_request_id, launch=launch,
                                                      work_limits=work_limits)
        if (known is None or known['state'] != 'claimed' or known['bridge_run_id'] != registration.bridge_run_id
                or known['receipt_digest'] != registration.claim_digest):
            raise ClaimError('claim_execution_unavailable')
        with ledger._lock:
            connection = ledger._connection
            if connection.in_transaction:
                raise ClaimError('claim_execution_unavailable')
            connection.execute('BEGIN')
            try:
                value = connection.transaction_read_call(work_limits, lambda: _snapshot(ledger, context, request, lease, expected))
            finally:
                connection.execute('ROLLBACK')
        return value
    except SQLReadError:
        raise ClaimError('claim_execution_unavailable') from None


def _record(row, record_type, source, run_id):
    if row is None:
        raise ClaimError('claim_execution_unavailable')
    invalid = False
    try:
        raw = row['content']
        if type(raw) is not bytes or len(raw) > MAX_RECORD_BYTES:
            raise ValueError()
        record = validate_learning_record(parse_bounded_json(raw))
        invalid = (type(record['schema_version']) is not int
                   or record['record_type'] != record_type or row['source'] != source or record['run_id'] != run_id
                   or record['record_id'] != row['record_id'] or canonical_json(record) != raw
                   or content_digest(record) != row['content_digest'])
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return record


def _snapshot(ledger, context, request, lease, expected):
    connection, key = ledger._connection, ledger.state.digest_key()
    registration = request.registration
    known = ledger._known_claim_receipt_locked(context.project_id, context.scope, context.request, context.parameters)
    if known is None or known['state'] != 'claimed' or known['receipt_digest'] != registration.claim_digest:
        raise ClaimError('claim_execution_unavailable')
    row = connection.execute(
        'SELECT a.*,h.assignment_digest,h.task_digest,h.route_digest,h.configuration_digest,h.client_version,'
        'h.native_assignment_id,h.created_at_ms AS opened_at_ms,h.observation_started_at_ms,h.observation_deadline_at_ms,'
        'o.handle_digest AS orchestration_digest,e.owner_digest AS registered_digest '
        'FROM active_run_capacity a JOIN host_run_bindings h '
        'ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest AND h.run_id=a.run_id '
        'JOIN orchestrations o ON o.project_id=h.project_id AND o.orchestration_id=h.orchestration_id '
        'JOIN capacity_executions e ON e.project_id=a.project_id AND e.handle_digest=a.handle_digest '
        'AND e.run_id=a.run_id AND e.bridge_run_id=a.bridge_run_id '
        'WHERE a.project_id=? AND a.handle_digest=? AND a.bridge_run_id=?',
        (context.project_id, context.handle, registration.bridge_run_id),
    ).fetchone()
    if row is None:
        raise ClaimError('claim_execution_unavailable')
    if (row['kind'] != 'claimed' or row['runtime_digest'] != context.runtime_digest or row['provider'] != 'codex'
            or row['run_id'] != expected.run_id or row['client_version'] != 'bridge-v1'
            or row['assignment_digest'] != lease.assignment_digest or row['deadline_at_ms'] != lease.hard_deadline_at_ms
            or row['orchestration_digest'] != keyed_digest(key, expected.orchestration_handle)
            or row['native_assignment_id'] != external_id(key, 'asg', registration.bridge_run_id)):
        raise ClaimError('claim_execution_invalid')
    binding = AttemptBinding(**{name: getattr(expected, name) for name in LEASE_SCOPE_FIELDS},
                             claim_id=registration.bridge_run_id, claim_digest=registration.claim_digest)
    claim_binding = keyed_digest(key, canonical_json({'domain': 'project-result-claim-binding-v1',
        'lease_digest': lease.lease_digest, 'binding': binding.as_data(), 'hard_deadline_at_ms': lease.hard_deadline_at_ms}))
    if not secrets.compare_digest(claim_binding, lease.claim_binding_digest):
        raise ClaimError('claim_execution_invalid')
    grant = sign_execution_grant(key, binding=binding, handle_digest=context.handle,
        assignment_digest=row['assignment_digest'], task_digest=row['task_digest'], route_digest=row['route_digest'],
        configuration_digest=row['configuration_digest'], lease_digest=lease.lease_digest,
        clock_owner_digest=row['clock_owner_digest'], clock_reference_ns=row['clock_reference_ns'],
        deadline_ns=row['execution_deadline_ns'], claimed_at_ms=row['claimed_at_ms'], deadline_at_ms=row['deadline_at_ms'])
    if (row['registered_digest'] != grant.registration_digest or request.registration_digest != grant.registration_digest):
        raise ClaimError('claim_execution_conflict')
    # LIMIT2 bounds retained candidate memory. The owner SQL work budget also
    # bounds the JSON predicate scan; overflow refuses rather than omitting rows.
    decisions = connection.execute(
        "SELECT record_id,content,content_digest,source FROM records WHERE project_id=? AND record_type='admission_decision' "
        "AND json_extract(CAST(content AS TEXT),'$.run_id')=? LIMIT 2", (context.project_id, expected.run_id),
    ).fetchall()
    if len(decisions) != 1:
        raise ClaimError('claim_execution_unavailable')
    admission = _record(decisions[0], 'admission_decision', 'lifecycle', expected.run_id)
    start_id = external_id(key, 'evt', f'{expected.run_id}\0{request.assignment_id}\0started')
    # Never let the requested assignment select one of several host starts.
    # Both the original run's start and admission must be unique in this same
    # budgeted snapshot; ambiguous history is not reconstructed or preferred.
    starts = connection.execute(
        "SELECT record_id,content,content_digest,source FROM records WHERE project_id=? AND record_type='event' "
        "AND source='host' AND json_extract(CAST(content AS TEXT),'$.run_id')=? "
        "AND json_extract(CAST(content AS TEXT),'$.event_code')='subagent_started' LIMIT 2",
        (context.project_id, expected.run_id),
    ).fetchall()
    if len(starts) != 1:
        raise ClaimError('claim_execution_unavailable')
    event = _record(starts[0], 'event', 'host', expected.run_id)
    if (admission['decision'] != 'pass' or admission['provider'] != row['provider']
            or event['record_id'] != start_id or event['event_code'] != 'subagent_started' or event['status'] != 'started'
            or event['assignment_id'] != request.assignment_id or event['configuration_digest'] != row['configuration_digest']
            or event['task_slice'].split('-')[0] != event['task_family']
            or event['observed_at_ms'] != row['opened_at_ms'] or event['observed_at_ms'] != row['observation_started_at_ms']):
        raise ClaimError('claim_execution_invalid')
    return seal_run_provenance(key, {
        'binding': binding.as_data(), 'registration': registration.payload(), 'assignment_id': request.assignment_id,
        **{name: getattr(grant, name) for name in ('registration_digest', 'assignment_digest', 'task_digest',
                                                 'route_digest', 'configuration_digest', 'lease_digest')},
        'provider': admission['provider'], 'task_family': event['task_family'], 'client_version': row['client_version'],
        'registered_assignment_id': row['native_assignment_id'], 'admission_record_id': admission['record_id'],
        'admission_content_digest': decisions[0]['content_digest'], 'admission_observed_at_ms': admission['observed_at_ms'],
        'start_record_id': event['record_id'], 'start_content_digest': content_digest(event),
        'started_at_ms': event['observed_at_ms'], 'observation_deadline_at_ms': row['observation_deadline_at_ms'],
    })
