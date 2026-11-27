"""Account-owner snapshot of an actual original host open, before claim.

This creates no admission, registration, claim, clock window or execution.
Pending-lease MAC possession is not native identity or current authority.
"""
import os
import threading

from .account_claim_contracts import ClaimError, _DIGEST, matches
from .account_run_provenance import _record
from .claim_storage import capture_storage_evidence, verify_pending_storage_evidence
from .host_open_contracts import OpenProvenanceInput, decode_open_provenance_input, seal_open_provenance
from .learning_contracts import content_digest, external_id, keyed_digest
from .sql_read_budget import SQLReadError, SQLReadLimits
from .account_open_contracts import OpenError
from .account_original_open import managed_open_metadata, original_orchestration_digest


def attest_open_provenance(ledger, project_id, *, runtime_digest, caller_scope, request,
                           storage_binding, storage_evidence, work_limits):
    origin = getattr(ledger, '_connection_origin', None)
    if (type(origin) is not tuple or len(origin) != 2 or origin[0] != os.getpid()
            or origin[1] is not threading.current_thread() or not matches(caller_scope, _DIGEST)
            or type(request) is not OpenProvenanceInput or type(work_limits) is not SQLReadLimits):
        raise ClaimError('claim_execution_invalid')
    request = decode_open_provenance_input(request.payload())
    limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms, work_limits.max_value_bytes)
    expected, raw = capture_storage_evidence(storage_binding, storage_evidence)
    key = ledger.state.digest_key()
    lease = verify_pending_storage_evidence(key, expected, raw)
    if (expected.project_id != project_id or expected.runtime_digest != runtime_digest
            or expected.run_handle != request.run_handle):
        raise ClaimError('claim_execution_invalid')
    try:
        with ledger._held():
            connection = ledger._connection
            if connection.in_transaction:
                raise ClaimError('claim_execution_unavailable')
            connection.execute('BEGIN')
            try:
                return connection.transaction_read_call(limits, lambda: _snapshot(ledger, request, lease))
            finally:
                connection.execute('ROLLBACK')
    except SQLReadError as error:
        if passes_busy(error):
            raise
        raise ClaimError('claim_execution_unavailable') from None


def _snapshot(ledger, request, lease):
    connection, key, expected = ledger._connection, ledger.state.digest_key(), lease.binding
    row = connection.execute(
        'SELECT h.*,i.runtime_digest,i.provider,p.status AS project_status '
        'FROM host_run_bindings h JOIN launch_inputs i ON i.project_id=h.project_id '
        'AND i.handle_digest=h.handle_digest AND i.run_id=h.run_id '
        'JOIN projects p ON p.project_id=h.project_id '
        'WHERE h.project_id=? AND h.handle_digest=?',
        (expected.project_id, keyed_digest(key, request.run_handle)),
    ).fetchone()
    if row is None:
        raise ClaimError('claim_execution_unavailable')
    try:
        managed = managed_open_metadata(ledger, expected)
        orchestration_digest = original_orchestration_digest(ledger, expected) if managed is None else managed['orchestration_digest']
    except OpenError as error:
        raise ClaimError('claim_execution_invalid' if error.reason_code == 'open_conflict' else 'claim_execution_unavailable') from None
    if (row['project_status'] != 'active' or row['runtime_digest'] != expected.runtime_digest or row['provider'] not in {'codex', 'claude'}
            or row['client_version'] != 'bridge-v1' or row['run_id'] != expected.run_id
            or row['assignment_digest'] != lease.assignment_digest
            or orchestration_digest != keyed_digest(key, expected.orchestration_handle)):
        raise ClaimError('claim_execution_invalid')
    records = {}
    for kind, code in (('admission_decision', None), ('event', 'subagent_started'), ('verification_binding', None)):
        if managed is not None:
            value = managed[{'admission_decision': 'admission', 'event': 'start_event',
                             'verification_binding': 'verification_binding'}[kind]]
            if kind == 'verification_binding' and request.worker_outcome_digest is None:
                if value is not None:
                    raise ClaimError('claim_execution_invalid')
            elif value is None:
                raise ClaimError('claim_execution_unavailable')
            records[kind] = value
            continue
        sql = ('SELECT record_id,content,content_digest,source FROM records WHERE project_id=? AND record_type=? '
               "AND CASE WHEN json_valid(CAST(content AS TEXT)) THEN json_extract(CAST(content AS TEXT),'$.run_id') END=?")
        parameters = (expected.project_id, kind, expected.run_id)
        if code is not None:
            sql += " AND source='host' AND json_extract(CAST(content AS TEXT),'$.event_code')=?"
            parameters += (code,)
        found = connection.execute(sql + ' LIMIT 2', parameters).fetchall()
        if kind == 'verification_binding' and request.worker_outcome_digest is None:
            if found:
                raise ClaimError('claim_execution_invalid')
            records[kind] = None
        else:
            if len(found) != 1:
                raise ClaimError('claim_execution_unavailable')
            records[kind] = _record(found[0], kind, 'host' if code else 'lifecycle', expected.run_id)
    admission, event, verification = (records[k] for k in ('admission_decision', 'event', 'verification_binding'))
    if verification is not None and verification['record_id'] != 'verification_binding:' + content_digest(
            {name: value for name, value in verification.items() if name != 'record_id'}):
        raise ClaimError('claim_execution_invalid')
    if (admission['decision'] != 'pass' or admission['provider'] != row['provider']
            or event['record_id'] != external_id(key, 'evt', f'{expected.run_id}\0{request.assignment_id}\0started')
            or event['assignment_id'] != request.assignment_id or event['status'] != 'started'
            or event['configuration_digest'] != row['configuration_digest']
            or event['observed_at_ms'] != row['created_at_ms']
            or event['observed_at_ms'] != row['observation_started_at_ms']
            or event['task_slice'].split('-')[0] != event['task_family']
            or (verification is not None and (verification['worker_outcome_digest'] != request.worker_outcome_digest
                                             or verification['observed_at_ms'] != row['created_at_ms']))):
        raise ClaimError('claim_execution_invalid')
    return seal_open_provenance(key, request, {
        'binding': expected.as_data(), 'lease_digest': lease.lease_digest, 'assignment_id': request.assignment_id,
        **{name: row[name] for name in ('assignment_digest', 'task_digest', 'route_digest', 'configuration_digest')},
        'provider': admission['provider'], 'task_family': event['task_family'], 'client_version': row['client_version'],
        'admission_record_id': admission['record_id'], 'admission_content_digest': content_digest(admission),
        'admission_observed_at_ms': admission['observed_at_ms'], 'start_record_id': event['record_id'],
        'start_content_digest': content_digest(event), 'opened_at_ms': event['observed_at_ms'],
        'observation_deadline_at_ms': row['observation_deadline_at_ms'],
        'worker_outcome_digest': None if verification is None else verification['worker_outcome_digest'],
        'verification_record_id': None if verification is None else verification['record_id'],
        'verification_content_digest': None if verification is None else content_digest(verification),
    })


def passes_busy(error) -> bool:
    """M4b E1a-2: a busy ledger passes through to the reply as busy, not as claim_execution_unavailable."""
    return isinstance(error, SQLReadError) and error.reason_code == 'sql_read_busy'
