"""Account-owner settlement of a claimed run whose project service was lost.

The trusted account adapter supplies the run's lease and the project writer's
sealed storage evidence from the enrolled project peer, whose root must be
registered to the project. The account checks that this is the run's own
admitted claim, then in one transaction marks its host binding abandoned
(`service_lost`). From then on the run no longer counts against the machine
or account limits (`active_run_capacity.crashed_claim`). The capacity row
itself stays as it is: no terminal, cleanup or release receipt is invented,
and a released row is reported as released. Repeating the request returns
the same receipt; nothing is settled twice.
"""
from .account_claim_contracts import ClaimError, LaunchClaimInput
from .lost_run_contracts import LOST_RUN_REASON, SettleLostInput, decode_settle_lost, settlement_digest


def settle_lost_run(ledger, project_id, *, runtime_digest, caller_scope, settle, storage_binding, storage_evidence):
    from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest
    from .claim_storage import capture_storage_evidence, verify_pending_storage_evidence

    if type(settle) is not SettleLostInput:
        raise ClaimError('claim_cleanup_invalid')
    settle = decode_settle_lost(settle.payload())
    expected, raw = capture_storage_evidence(storage_binding, storage_evidence)
    if expected is None or raw is None:
        raise ClaimError('claim_storage_required')
    launch = LaunchClaimInput(settle.run_handle, settle.identity_digest)
    original = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.claim_launch', settle.claim_request_id,
                              project_id, runtime_digest, launch.payload())
    if (expected.project_id != project_id or expected.runtime_digest != runtime_digest
            or expected.run_handle != settle.run_handle or expected.claim_request_id != settle.claim_request_id
            or expected.claim_request_digest != account_request_digest(original)):
        raise ClaimError('claim_cleanup_invalid')
    key = ledger.state.digest_key()
    lease = verify_pending_storage_evidence(key, expected, raw)
    if lease.lease_digest != settle.lease_digest:
        raise ClaimError('claim_cleanup_invalid')
    context, known, _, _ = ledger._claim_preflight(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                  request_id=settle.claim_request_id, launch=launch,
                                                  storage_binding=expected)
    if (known is None or known['state'] != 'claimed' or known['bridge_run_id'] != settle.bridge_run_id
            or known['run_id'] != expected.run_id):
        raise ClaimError('claim_cleanup_unavailable')
    with ledger._account_transaction():
        current = ledger._known_claim_receipt_locked(project_id, context.scope, context.request, context.parameters)
        if current is None or current['receipt_digest'] != known['receipt_digest']:
            raise ClaimError('claim_cleanup_unavailable')
        row = ledger._connection.execute(
            'SELECT a.kind,a.run_id,a.bridge_run_id,a.released_at_ms,h.run_id AS binding_run_id,'
            'h.binding_state,h.abandoned_reason FROM active_run_capacity a '
            'JOIN host_run_bindings h ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest '
            'WHERE a.project_id=? AND a.handle_digest=?', (project_id, context.handle)).fetchone()
        if (row is None or row['kind'] != 'claimed' or row['run_id'] != expected.run_id
                or row['binding_run_id'] != expected.run_id or row['bridge_run_id'] != settle.bridge_run_id):
            raise ClaimError('claim_cleanup_unavailable')
        if row['binding_state'] == 'abandoned' and row['abandoned_reason'] == LOST_RUN_REASON:
            # Checked first: since O0b RS-A a lost settlement also releases the slot (as `lost`), and its
            # replay must still answer `settled`.
            state, first = 'settled', False
        elif row['released_at_ms'] is not None:
            state, first = 'released', False  # Its own service released it; nothing is left to settle.
        elif row['binding_state'] == 'pending':
            changed = ledger._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason=? "
                "WHERE project_id=? AND handle_digest=? AND binding_state='pending'",
                (LOST_RUN_REASON, project_id, context.handle)).rowcount
            if changed != 1:
                raise ClaimError('claim_cleanup_unavailable')
            # O0b RS-A: the slot is released as lost here, for good, in the same transaction.
            ledger._release_slot_locked(project_id, context.handle, 'lost', ledger._clock_ms(),
                                        ledger._capacity_clock_reference())
            state, first = 'settled', True
        else:
            raise ClaimError('claim_cleanup_unavailable')
    digest = settlement_digest(key, {'project_id': project_id, 'handle_digest': context.handle,
                                     'run_id': expected.run_id, 'bridge_run_id': settle.bridge_run_id,
                                     'lease_digest': settle.lease_digest, 'state': state})
    return {'state': state, 'run_handle': settle.run_handle, 'bridge_run_id': settle.bridge_run_id,
            'receipt_digest': digest, 'known': not first}
