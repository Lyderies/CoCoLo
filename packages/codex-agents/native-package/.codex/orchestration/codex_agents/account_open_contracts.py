"""Closed original account-open values, without persistence or native authority.

Only the account owner admits. Requests name protected original inputs; they
cannot provide assignments, provider facts, signatures, clocks or a passed flag.
Known replies are historical metadata and never renew an execution window.
"""
from dataclasses import dataclass

from .history_contracts import HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import ADMISSION_REASON_CODES, canonical_json, parse_bounded_json
from .open_scope_contracts import (MAX_OPEN_SCOPE_BYTES, MAX_OPEN_SCOPE_CONTEXT_BYTES, OpenScopeContext,
                                   checked_open_scope, decode_open_scope_request, open_inputs_digest)
from .orchestration_api import OrchestrationScope
from .account_claim_contracts import _HANDLE, _RUN

MAX_ACCOUNT_OPEN_CONTEXT_BYTES = MAX_OPEN_SCOPE_CONTEXT_BYTES + MAX_OPEN_SCOPE_BYTES + 1024
MAX_ACCOUNT_OPEN_RECEIPT_BYTES = 16_384
OPEN_REASON_CODES = frozenset({'open_invalid_request', 'open_not_authorized', 'open_unavailable',
                             'open_conflict', 'open_busy', 'open_preparation_failed', 'open_preparation_stale', 'open_cancelled',
                             # S1-U10: each cause of a refused open names itself; open_unavailable is only read
                             # from an earlier service.
                             'open_account_starting', 'open_account_closing', 'open_account_busy',
                             'open_service_not_running', 'open_service_other_version', 'open_service_unreachable',
                             'open_service_hint_refused',
                             'open_project_inactive', 'open_state_inconsistent', 'open_failed',
                             # OP-4: the project's opens in flight are at their limit (not transient: see
                             # refusal_steps); OP-2: a replay of an open purged past its retention.
                             'open_capacity_full', 'open_expired'})
# OP-1: how many of a project's open requests may be in flight (pending, not past their own deadline) at once.
# The account host's limit, and the one learning-status reports (OP-3).
OPEN_REQUESTS_IN_FLIGHT = 4


class OpenError(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code if type(reason_code) is str and reason_code in OPEN_REASON_CODES else 'open_invalid_request'
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class OpenOwnerLimits:
    """Trusted startup configuration, never selected by an open request. OP-1: only requests in flight are
    capped; a finished one is bounded by retention (account_original_open.purge_finished_opens), never by a
    count of every request kept, which 0.8.38 had (max_retained, a lifetime cap)."""
    max_pending: int
    max_context_bytes: int

    def __post_init__(self):
        if (not valid_integer(self.max_pending, minimum=1, maximum=64)
                or not valid_integer(self.max_context_bytes, minimum=1, maximum=MAX_ACCOUNT_OPEN_CONTEXT_BYTES)):
            raise OpenError('open_invalid_request')


@dataclass(frozen=True)
class AccountOpenInput:
    scope: OrchestrationScope
    orchestration_handle: str
    inputs_digest: str

    def __post_init__(self):
        self.payload()

    def payload(self):
        if type(self.scope) is not OrchestrationScope:
            raise OpenError('open_invalid_request')
        value = {'scope': self.scope.as_data(), 'orchestration_handle': self.orchestration_handle,
                 'inputs_digest': self.inputs_digest}
        try:
            decode_open_scope_request(value, self.scope.project_id, self.scope.runtime_digest)
        except HistoryError:
            raise OpenError('open_invalid_request') from None
        return value


def decode_account_open_input(value, project_id, runtime_digest):
    try:
        scope, handle, digest = decode_open_scope_request(value, project_id, runtime_digest)
    except HistoryError:
        raise OpenError('open_invalid_request') from None
    return AccountOpenInput(scope, handle, digest)


@dataclass(frozen=True, repr=False)
class AccountOpenContext:
    """Original input/evidence custody of an independently authenticated adapter.

    Native identity, current project/session/generation, live permission and
    revocation still require independent enforcement. This value and its MAC
    confer none of that authority and cannot be supplied in request JSON.
    """
    prospective: OpenScopeContext
    project_scope: dict

    def snapshot(self):
        invalid = False
        try:
            if type(self.prospective) is not OpenScopeContext or type(self.project_scope) is not dict:
                raise ValueError()
            original = self.prospective.snapshot()[1]
            digest = open_inputs_digest(*(original[n] for n in ('assignment', 'task', 'route', 'parent_grant')))
            proof = checked_open_scope(self.project_scope, self.prospective.scope,
                self.prospective.orchestration_handle, digest)
            raw = canonical_json({'prospective': original, 'project_scope': proof})
            if not 0 < len(raw) <= MAX_ACCOUNT_OPEN_CONTEXT_BYTES:
                raise ValueError()
            captured = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_OPEN_CONTEXT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise OpenError('open_not_authorized')
        return raw, captured

    @classmethod
    def from_snapshot(cls, value):
        return cls(OpenScopeContext.from_snapshot(value['prospective']), value['project_scope'])


def checked_account_open_reply(value, request):
    invalid = False
    try:
        if (type(request) is not AccountOpenInput or type(value) is not dict
                or value.keys() != {'state', 'run_handle', 'run_id', 'inputs_digest', 'scope_digest', 'receipt_digest',
                    'admission_record_id', 'admission_content_digest', 'reason_code', 'recorded_at_ms',
                    'observation_deadline_at_ms', 'known'}
                or value['state'] not in {'opened', 'denied'} or type(value['known']) is not bool
                or value['inputs_digest'] != request.inputs_digest
                or any(not matches(value[n], _DIGEST) for n in
                       ('inputs_digest', 'scope_digest', 'receipt_digest', 'admission_content_digest'))
                or not matches(value['admission_record_id'], _ID)
                or type(value['reason_code']) is not str or value['reason_code'] not in ADMISSION_REASON_CODES
                or not valid_integer(value['recorded_at_ms'])):
            raise ValueError()
        if value['state'] == 'opened':
            if (value['reason_code'] != 'admitted' or not matches(value['run_handle'], _HANDLE)
                    or not matches(value['run_id'], _RUN)
                    or not valid_integer(value['observation_deadline_at_ms'], minimum=value['recorded_at_ms'] + 1)):
                raise ValueError()
        elif (value['reason_code'] == 'admitted' or value['run_handle'] is not None or value['run_id'] is not None
                or value['observation_deadline_at_ms'] is not None):
            raise ValueError()
        raw = canonical_json(value)
        if len(raw) > MAX_ACCOUNT_OPEN_RECEIPT_BYTES:
            raise ValueError()
        captured = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_OPEN_RECEIPT_BYTES)
    except Exception:
        invalid = True
    if invalid:
        raise OpenError('open_invalid_request')
    return captured
