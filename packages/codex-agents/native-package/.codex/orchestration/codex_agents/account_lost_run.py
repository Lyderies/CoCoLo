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
from .learning_contracts import canonical_json, content_digest
from .lost_run_contracts import LOST_RUN_REASON, SettleLostInput, decode_settle_lost, settlement_digest


def _tombstone_evidence(run_id, settle):
    """B4s r1: what the binding's tombstone keeps of a lost settlement (its receipt is this evidence's digest)."""
    return {'d': 'lost-v1', 'r': run_id, 'b': settle.bridge_run_id, 'l': settle.lease_digest}


def _known_claim(ledger, project_id, runtime_digest, caller_scope, settle, launch, expected):
    """The run's stored claim receipt, with the original-open integrity checks of `_claim_preflight`. B4s r1 (HIGH 1):
    once OP-2 purged the run's open past its retention, those checks have no rows left to read (the binding and the
    open receipt are gone); the receipt is then read alone, keyed as stored, and only for a run whose binding
    tombstone remains (`_run_state` then requires the replay evidence its lost settlement kept)."""
    from types import SimpleNamespace

    from .learning_contracts import keyed_digest

    try:
        context, known, _, _ = ledger._claim_preflight(project_id, runtime_digest=runtime_digest,
                                                      caller_scope=caller_scope, request_id=settle.claim_request_id,
                                                      launch=launch, storage_binding=expected)
        return context, known
    except ClaimError as error:
        if error.reason_code != 'claim_storage_invalid':
            raise
    key = ledger.state.digest_key()
    context = SimpleNamespace(
        handle=keyed_digest(key, launch.run_handle),
        scope=keyed_digest(key, canonical_json({'project': project_id, 'runtime': runtime_digest,
                                                'caller': caller_scope})),
        request=keyed_digest(key, settle.claim_request_id), parameters=keyed_digest(key, canonical_json(launch.payload())))
    with ledger._held():
        project = ledger._connection.execute('SELECT status FROM projects WHERE project_id=?', (project_id,)).fetchone()
        purged = (ledger._connection.execute('SELECT 1 FROM host_run_bindings WHERE project_id=? AND handle_digest=?',
                                             (project_id, context.handle)).fetchone() is None
                  and ledger._connection.execute(
                      "SELECT 1 FROM run_holds WHERE project_id=? AND kind='binding' AND identity=? AND state='released'",
                      (project_id, context.handle)).fetchone() is not None)
        if project is None or project[0] != 'active' or not purged:
            raise ClaimError('claim_storage_invalid')
        known = ledger._known_claim_receipt_locked(project_id, context.scope, context.request, context.parameters)
    return context, known


def _run_state(ledger, project_id, handle, run_id, settle):
    """Inside the account transaction: this run's capacity row, its binding (or, once the open was purged past its
    retention, the binding's tombstone) and its slot's recorded release kind. Refused unless it is this claimed run."""
    row = ledger._connection.execute(
        'SELECT a.kind,a.run_id,a.bridge_run_id,a.released_at_ms,h.run_id AS binding_run_id,'
        'h.binding_state,h.abandoned_reason FROM active_run_capacity a '
        'LEFT JOIN host_run_bindings h ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest '
        'WHERE a.project_id=? AND a.handle_digest=?', (project_id, handle)).fetchone()
    if (row is None or row['kind'] != 'claimed' or row['run_id'] != run_id
            or row['bridge_run_id'] != settle.bridge_run_id):
        raise ClaimError('claim_cleanup_unavailable')
    holds = {r['kind']: r for r in ledger._connection.execute(
        "SELECT kind,state,release_kind,receipt,evidence FROM run_holds WHERE project_id=? AND identity=? "
        "AND kind IN ('run_slot','binding')", (project_id, handle))}
    slot = holds.get('run_slot')
    state = {'released_at_ms': row['released_at_ms'], 'binding_state': row['binding_state'],
             'slot_kind': None if slot is None or slot['state'] != 'released' else slot['release_kind']}
    if row['binding_state'] is not None:
        if row['binding_run_id'] != run_id:
            raise ClaimError('claim_cleanup_unavailable')
        state['lost'] = row['binding_state'] == 'abandoned' and row['abandoned_reason'] == LOST_RUN_REASON
        return state
    # OP-2 purged the binding with its open: only its released register row is left. A lost settlement is answered
    # from it only with the replay evidence the settlement kept there, for this run, claim and lease.
    tombstone = holds.get('binding')
    if tombstone is None or tombstone['state'] != 'released' or row['released_at_ms'] is None:
        raise ClaimError('claim_cleanup_unavailable')
    if tombstone['release_kind'] == LOST_RUN_REASON:
        evidence = _tombstone_evidence(run_id, settle)
        if (tombstone['evidence'] != canonical_json(evidence).decode()
                or tombstone['receipt'] != content_digest(evidence)):
            raise ClaimError('claim_cleanup_unavailable')  # settled before the tombstone kept its evidence
        state['lost'] = True
    else:
        state['lost'] = False
    return state


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
    context, known = _known_claim(ledger, project_id, runtime_digest, caller_scope, settle, launch, expected)
    if (known is None or known['state'] != 'claimed' or known['bridge_run_id'] != settle.bridge_run_id
            or known['run_id'] != expected.run_id):
        raise ClaimError('claim_cleanup_unavailable')
    with ledger._account_transaction():
        current = ledger._known_claim_receipt_locked(project_id, context.scope, context.request, context.parameters)
        if current is None or current['receipt_digest'] != known['receipt_digest']:
            raise ClaimError('claim_cleanup_unavailable')
        row = _run_state(ledger, project_id, context.handle, expected.run_id, settle)
        if row['lost']:
            # Checked first: since O0b RS-A a lost settlement also releases the slot (as `lost`), and its
            # replay must still answer `settled` (B4s r1: also once the binding was purged, from its tombstone).
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
            # B4s r1 (HIGH 1): the binding's register row (a tombstone once the open is purged) keeps the replay
            # evidence, so the identical request is still answered, and its closure given, after the purge.
            evidence = _tombstone_evidence(expected.run_id, settle)
            if ledger._connection.execute(
                    "UPDATE run_holds SET receipt=?,evidence=? WHERE project_id=? AND kind='binding' AND identity=? "
                    "AND state='released' AND release_kind=?",
                    (content_digest(evidence), canonical_json(evidence).decode(), project_id, context.handle,
                     LOST_RUN_REASON)).rowcount != 1:
                raise ClaimError('claim_cleanup_unavailable')
            state, first = 'settled', True
        else:
            raise ClaimError('claim_cleanup_unavailable')
    digest = settlement_digest(key, {'project_id': project_id, 'handle_digest': context.handle,
                                     'run_id': expected.run_id, 'bridge_run_id': settle.bridge_run_id,
                                     'lease_digest': settle.lease_digest, 'state': state})
    return {'state': state, 'run_handle': settle.run_handle, 'bridge_run_id': settle.bridge_run_id,
            'receipt_digest': digest, 'known': not first}


def lost_run_closure(ledger, project_id, *, runtime_digest, caller_scope, settle, storage_binding, storage_evidence,
                     request_digest):
    """B4s (RS-B-B4-1): the closure of a run `settle_lost_run` settled (`lost`) or found released by its own service
    (`released_unacknowledged`), for the project writer to verify with its result key. The same custody checks as
    the settlement; nothing is written, and the closure is built from the stored rows (the same bytes on every ask).
    A run neither settled nor released has none (`claim_cleanup_unavailable`)."""
    from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest
    from .claim_storage import capture_storage_evidence, verify_pending_storage_evidence
    from .lost_run_closure import lost_run_closure as closure
    from .account_claim_contracts import _DIGEST, matches

    if type(settle) is not SettleLostInput or not matches(request_digest, _DIGEST):
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
    context, known = _known_claim(ledger, project_id, runtime_digest, caller_scope, settle, launch, expected)
    if (known is None or known['state'] != 'claimed' or known['bridge_run_id'] != settle.bridge_run_id
            or known['run_id'] != expected.run_id):
        raise ClaimError('claim_cleanup_unavailable')
    with ledger._account_transaction():
        row = _run_state(ledger, project_id, context.handle, expected.run_id, settle)
    if row['released_at_ms'] is None:
        raise ClaimError('claim_cleanup_unavailable')
    # As settle_lost_run answers: a lost settlement first (it also released the slot as `lost`), else its own
    # release (`completed`). The slot's recorded release kind decides. B5a: a `crashed` slot also needs this
    # lease's durable process proof; the timed account decision alone still has no project closure.
    if row['lost'] and row['slot_kind'] == 'lost':
        state = 'settled'
    elif not row['lost'] and row['slot_kind'] == 'completed':
        state = 'released'
    elif row['slot_kind'] == 'crashed':
        from .launch_journal import instance_stopped, process_settlement_evidence
        directory = ledger.state.root / 'projects' / project_id / 'launches'
        proof = process_settlement_evidence(directory, expected, settle.bridge_run_id,
                                            identity_digest=settle.identity_digest,
                                            allow_unbound=lease.state == 'claim_pending')
        if proof is None:
            raise ClaimError('claim_cleanup_unavailable')
        # Historical pending evidence alone does not fence a live service's next spawn.
        # A settled tombstone cannot spawn; a claimed admission needs its owner's stopped lock.
        if (proof['child'] is None and proof['state'] != 'settled'
                and not instance_stopped(directory, proof['instance'])):
            raise ClaimError('claim_cleanup_unavailable')
        state = 'crashed'
    else:
        raise ClaimError('claim_cleanup_unavailable')
    from .lost_run_closure import RELEASE_KINDS
    return closure(key, expected, lease_digest=lease.lease_digest, assignment_digest=lease.assignment_digest,
                   bridge_run_id=settle.bridge_run_id, release_kind=RELEASE_KINDS[state],
                   released_at_ms=int(row['released_at_ms']), request_digest=request_digest)
