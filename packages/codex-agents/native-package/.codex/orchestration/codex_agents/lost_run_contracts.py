"""Settling a claimed run whose project service was lost; metadata only.

A later service of the same project sends this, with the run's lease and the
project writer's sealed storage evidence as its custody, once the launch
journal shows that the run never started a child or that the child, its group
and session, and its watchdog are gone. The account then stops counting the
run against the active-run limits, for good and once. Nothing here proves
that evidence; the project service checked it before asking.
"""
from dataclasses import asdict, dataclass

from .account_claim_contracts import _BRIDGE, _DIGEST, _HANDLE, _REQUEST_ID, ClaimError, LaunchClaimInput, matches
from .active_run_capacity import LOST_RUN_REASON as LOST_RUN_REASON  # The binding's fixed abandonment reason.
from .learning_contracts import canonical_json, keyed_digest

_REPLY_FIELDS = {'state', 'run_handle', 'bridge_run_id', 'receipt_digest', 'known'}


@dataclass(frozen=True)
class SettleLostInput:
    run_handle: str
    identity_digest: str
    claim_request_id: str
    bridge_run_id: str
    lease_digest: str

    def __post_init__(self):
        try:
            LaunchClaimInput(self.run_handle, self.identity_digest)
        except ClaimError:
            raise ClaimError('claim_cleanup_invalid') from None
        if (not matches(self.claim_request_id, _REQUEST_ID) or not matches(self.bridge_run_id, _BRIDGE)
                or not matches(self.lease_digest, _DIGEST)):
            raise ClaimError('claim_cleanup_invalid')

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_settle_lost(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        value = SettleLostInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_cleanup_invalid')
    return value


def settlement_digest(key, material):
    """The same digest for the same run and outcome, so a lost reply is answered alike."""
    return keyed_digest(key, canonical_json({'domain': 'account-lost-run-settlement-v1', **material}))


def checked_lost_reply(value, request):
    """'settled' (it no longer counts) or 'released' (its own service released it first)."""
    invalid = False
    try:
        if (type(value) is not dict or set(value) != _REPLY_FIELDS or type(request) is not SettleLostInput
                or value['state'] not in {'settled', 'released'} or type(value['known']) is not bool
                or not matches(value['run_handle'], _HANDLE) or value['run_handle'] != request.run_handle
                or value['bridge_run_id'] != request.bridge_run_id or not matches(value['receipt_digest'], _DIGEST)):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_cleanup_invalid')
    return dict(value)
