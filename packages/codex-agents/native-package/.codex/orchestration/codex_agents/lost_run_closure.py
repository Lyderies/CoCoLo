"""B4s (RS-B-B4-1): the account's closure of a lost or released run, for the project store; metadata only.

`account.settle_lost_run` stops counting a lost run at the account; its reply carries nothing the project can
verify, and its field set is fixed (an older project service reads it). This separate operation answers a run the
account has settled (`lost`) or that its own service released before its acknowledgement reached the project
(`released_unacknowledged`) with a closure keyed with the account's digest key: the project writer's result key is the
same key (`<state root>/digest.key`; a run is only claimed after the account verified the project's sealed lease with
it), so the writer verifies the closure and records it (`ProjectHistory.record_account_settlement`). The MAC binds
these data to the key holder; it does not authenticate a sender (claim_denial.py), and the account sends a closure
only to the run's own enrolled project role. The closure names the outbox request's digest, so a closure fetched for
another request is told apart. Built from the account's stored rows, it is the same bytes on every ask.

B5a also answers a recorded `crashed` slot, after checking durable process evidence (or a never-spawned
admission). The project writer independently requires the matching proof before its first register release.
"""
import secrets

from .account_claim_contracts import _BRIDGE, _DIGEST, ClaimError, matches
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json
from .result_contracts import ResultLeaseBinding

CLOSURE_OPERATION = 'account.lost_run_closure'
# One closure fits one account frame (16 KiB) with its envelope; a real one is about 1 KiB.
MAX_LOST_RUN_CLOSURE_BYTES = 8_192
# Account release state -> the kind the project records; B5a's crashed state additionally requires process proof.
RELEASE_KINDS = {'settled': 'lost', 'released': 'released_unacknowledged', 'crashed': 'crashed'}
_FIELDS = frozenset({'state', 'binding', 'lease_digest', 'assignment_digest', 'bridge_run_id', 'release_kind',
                     'released_at_ms', 'request_digest', 'receipt_digest'})
_DOMAIN = 'account-lost-run-closure-v1'


def _receipt_digest(key, material):
    return keyed_digest(key, canonical_json({'domain': _DOMAIN, **material}))


def checked_lost_run_closure(value, request=None):
    """The closure's shape (and, with the request's `SettleLostInput`, that it is this run's); ClaimError otherwise."""
    invalid = False
    try:
        if (type(value) is not dict or set(value) != _FIELDS or value['state'] != 'lost_run_closed'
                or value['release_kind'] not in RELEASE_KINDS.values()
                or type(value['released_at_ms']) is not int or not 0 <= value['released_at_ms'] <= (1 << 63) - 1
                or not matches(value['bridge_run_id'], _BRIDGE)
                or any(not matches(value[n], _DIGEST) for n in ('lease_digest', 'assignment_digest', 'request_digest',
                                                               'receipt_digest'))):
            raise ValueError()
        binding = ResultLeaseBinding(**value['binding'])
        if request is not None and (binding.run_handle != request.run_handle
                                    or binding.claim_request_id != request.claim_request_id
                                    or value['lease_digest'] != request.lease_digest
                                    or value['bridge_run_id'] != request.bridge_run_id):
            raise ValueError()
        result = {**value, 'binding': binding.as_data()}
        if len(canonical_json(result)) > MAX_LOST_RUN_CLOSURE_BYTES:
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_cleanup_invalid')
    return result


def lost_run_closure(key, binding, *, lease_digest, assignment_digest, bridge_run_id, release_kind, released_at_ms,
                     request_digest):
    """The closure the account sends for this run (and the one a test builds with a store's result key)."""
    if type(binding) is not ResultLeaseBinding:
        raise ClaimError('claim_cleanup_invalid')
    material = {'state': 'lost_run_closed', 'binding': binding.as_data(), 'lease_digest': lease_digest,
                'assignment_digest': assignment_digest, 'bridge_run_id': bridge_run_id, 'release_kind': release_kind,
                'released_at_ms': released_at_ms, 'request_digest': request_digest}
    return checked_lost_run_closure({**material, 'receipt_digest': _receipt_digest(key, material)})


def verify_lost_run_closure(key, raw, *, project_id):
    """The project writer's check: canonical bytes within the limit, this project's, keyed with this key."""
    invalid = False
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_LOST_RUN_CLOSURE_BYTES:
            raise ValueError()
        value = checked_lost_run_closure(parse_bounded_json(raw, max_bytes=MAX_LOST_RUN_CLOSURE_BYTES))
        material = {k: v for k, v in value.items() if k != 'receipt_digest'}
        invalid = (raw != canonical_json(value) or value['binding']['project_id'] != project_id
                   or not secrets.compare_digest(value['receipt_digest'], _receipt_digest(key, material)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_cleanup_invalid')
    return value
