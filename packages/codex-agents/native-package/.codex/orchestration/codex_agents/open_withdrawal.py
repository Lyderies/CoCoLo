"""Slice B3: `account.withdraw_open`, the wire contract (DESIGN-0.4.0-RS-B.md draft 2 §8, draft 3 §4).

A run opened at the account but never claimed asks the account to end its binding now instead of at its 24-hour
expiry. The request names the run (handle, attempt, run id, the open's inputs digest), the generation of the
binding's register row it expects (1 while held) and why. The account's authority is the enrolled root, the caller
scope of the run's host class and the open's own stored receipt (account_host, the ledger's `withdraw_open`); the
custody this module builds is only the request's statement of which run it is for, and must equal the payload.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re

from .learning_contracts import canonical_json, parse_bounded_json

# Why a run's open is withdrawn (fixed: never free text; stored only in the receipt evidence).
WITHDRAW_REASONS = ('refused_before_claim', 'cancelled_before_claim', 'failed_before_claim', 'registered_idle',
                    'service_stopped')
# The account's refusals of a withdrawal (besides account_not_authorized and the transport's own).
WITHDRAW_ERROR_CODES = frozenset({
    'withdraw_invalid',  # not a withdrawal request
    'withdraw_unavailable',  # no pending, unclaimed open of this run (claimed, or ended some other way)
    'withdraw_stale',  # a different request after the binding's transition (or naming another generation)
    'withdraw_generation_required',  # a new transition with no generation (an older client)
})
MAX_WITHDRAW_CUSTODY_BYTES = 1_024
_HANDLE = re.compile(r'hrn_[0-9a-f]{48}')
_RUN = re.compile(r'run:[0-9a-f]{64}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_MAX_INTEGER = (1 << 63) - 1


class WithdrawError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in WITHDRAW_ERROR_CODES:
            reason_code = 'withdraw_invalid'
        self.reason_code = reason_code
        super().__init__(reason_code)


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class WithdrawOpenInput:
    run_handle: str
    attempt: int
    run_id: str
    inputs_digest: str
    generation: int | None  # None only from an older client: the account refuses a new transition then
    reason: str

    def __post_init__(self):
        if (not _matches(self.run_handle, _HANDLE) or type(self.attempt) is not int
                or not 1 <= self.attempt <= _MAX_INTEGER or not _matches(self.run_id, _RUN)
                or not _matches(self.inputs_digest, _DIGEST)
                or (self.generation is not None and (type(self.generation) is not int
                                                     or not 1 <= self.generation <= _MAX_INTEGER))
                or type(self.reason) is not str or self.reason not in WITHDRAW_REASONS):
            raise WithdrawError('withdraw_invalid')

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_withdraw_open(payload) -> WithdrawOpenInput:
    invalid = False
    try:
        if type(payload) is not dict or set(payload) != set(WithdrawOpenInput.__dataclass_fields__):
            raise ValueError()
        value = WithdrawOpenInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise WithdrawError('withdraw_invalid')
    return value


def checked_withdraw_reply(value, request) -> dict:
    """The account's answer for exactly this request's run (W4e: a reply naming another handle is rejected)."""
    invalid = False
    try:
        if (type(request) is not WithdrawOpenInput or type(value) is not dict
                or set(value) != {'state', 'run_handle', 'receipt', 'known'} or value['state'] != 'withdrawn'
                or value['run_handle'] != request.run_handle or not _matches(value['receipt'], _DIGEST)
                or type(value['known']) is not bool):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise WithdrawError('withdraw_invalid')
    return dict(value)


def withdrawal_request_id(run_handle: str, attempt: int) -> str:
    """Fixed per attempt, so every retry (from this service or its successor) is the same request."""
    return 'withdraw-' + hashlib.sha256(canonical_json({'run_handle': run_handle, 'attempt': attempt})).hexdigest()[:32]


def withdrawal_custody(withdrawal: WithdrawOpenInput) -> bytes:
    """The custody a withdrawal is sent with: which run it is for (not a proof; see the module note)."""
    if type(withdrawal) is not WithdrawOpenInput:
        raise WithdrawError('withdraw_invalid')
    return canonical_json({'purpose': 'withdraw', 'run_handle': withdrawal.run_handle, 'attempt': withdrawal.attempt,
                           'run_id': withdrawal.run_id, 'inputs_digest': withdrawal.inputs_digest})


def custody_matches(raw, withdrawal: WithdrawOpenInput) -> bool:
    """Whether the enrolled custody is a withdrawal's and names exactly this payload's run."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_WITHDRAW_CUSTODY_BYTES:
            return False
        value = parse_bounded_json(raw, max_bytes=MAX_WITHDRAW_CUSTODY_BYTES)
        return type(value) is dict and raw == canonical_json(value) and raw == withdrawal_custody(withdrawal)
    except Exception:
        return False
