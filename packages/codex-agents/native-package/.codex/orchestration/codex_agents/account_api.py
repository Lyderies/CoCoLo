"""Closed account metadata contracts/client; no persistence or native authority.

Read IDs correlate fresh views. Claim IDs retrieve durable known metadata, never
execution permission. Endpoint identity and caller enrollment remain mandatory
gates. No startup, automatic retries or SQLite fallback are performed here.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from .reply_shapes import reply_fields_match
from .contracts import ContractError
from .learning_contracts import canonical_json, parse_bounded_json
from .owner_key_contracts import checked_public_key
from .service_transport import MAX_TIMEOUT_SECONDS, SERVICE_REFUSALS, ServiceTransportError, exchange_frame
from .account_claim_contracts import CLAIM_ERROR_CODES, ClaimError, LaunchClaimInput, checked_claim_receipt, decode_claim_input
from .capacity_status_contracts import CapacityStatusInput, checked_capacity_status, decode_capacity_status_input
from .execution_grant import ExecutionRegistrationInput, checked_execution_reply, decode_execution_registration
from .terminal_delivery import checked_terminal_receipt, decode_terminal_submission
from .release_contracts import checked_release_receipt
from .claim_denial import (
    CloseDeniedInput, FenceClaimInput, checked_denial_closure, checked_fence_reply, decode_close_denied, decode_fence_claim,
)
from .lost_run_contracts import SettleLostInput, checked_lost_reply, decode_settle_lost
from .run_provenance import RunProvenanceInput, checked_run_provenance, decode_run_provenance_input
from .host_open_contracts import OpenProvenanceInput, checked_open_provenance, decode_open_provenance_input
from .admission_inputs import ADMISSION_CAPTURE_PUBLIC_REASONS
from .account_open_contracts import AccountOpenInput, OpenError, OPEN_REASON_CODES, checked_account_open_reply, decode_account_open_input
from .provider_observation_contracts import (
    OBSERVATION_REASONS, ObservationError, ObservationInput, checked_observation_reply, decode_observation_input,
)
from .observation_failure_contracts import (
    ObservationFailureInput, checked_observation_failure_reply, decode_observation_failure,
)
from .rate_limit_contracts import (
    RateLimitError, RateLimitEventInput, checked_rate_limit_reply, decode_rate_limit_event,
)
from .billing_probe_contracts import (
    BILLING_PROBE_REASONS, BillingProbeError, checked_billing_probe_reply, decode_billing_probe_input,
)
from .open_withdrawal import (
    WITHDRAW_ERROR_CODES, WithdrawError, WithdrawOpenInput, checked_withdraw_reply, decode_withdraw_open,
)

ACCOUNT_PROTOCOL_VERSION = 1
MAX_ACCOUNT_FRAME_BYTES = 16_384
MAX_ACCOUNT_INTEGER = (1 << 63) - 1
ACCOUNT_READ_OPERATIONS = frozenset({
    "account.health", "account.project", "account.owner_key", "account.owner_public_key", "account.capacity_status",
    "account.provider_state_snapshot",
})
ACCOUNT_OPERATIONS = ACCOUNT_READ_OPERATIONS | {
    "account.claim_launch", "account.register_execution", "account.submit_terminal", "account.release_capacity", "account.close_denied_attempt",
    "account.fence_claim", "account.settle_lost_run", "account.run_provenance", "account.open_provenance",
    'account.open_host_run', 'account.observe_provider',
    'account.record_observation_unavailable', 'account.record_rate_limit_event',
    'account.billing_probe',
    # B3 (RS-B draft 2 §8): a never-claimed run's open, withdrawn by its project's service or its opening host.
    'account.withdraw_open',
}
ACCOUNT_REASON_CODES = frozenset({
    "account_invalid_request", "account_request_too_large", "account_invalid_response",
    "account_not_authorized", "account_wrong_owner", "account_unavailable",
    "account_incompatible", "account_project_not_found",
    # The account closed the connection without replying: it may have restarted (0.3.3, C11).
    "account_closed_without_reply",
    # The account's connection limit is reached (an attach refusal, said as service_refused_busy on the wire).
    "account_busy",
}) | SERVICE_REFUSALS | CLAIM_ERROR_CODES | OPEN_REASON_CODES | OBSERVATION_REASONS | BILLING_PROBE_REASONS | ADMISSION_CAPTURE_PUBLIC_REASONS | WITHDRAW_ERROR_CODES
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_REQUEST_FIELDS = {"protocol_version", "operation", "request_id", "project_id", "runtime_digest", "payload"}
_RESPONSE_FIELDS = {"protocol_version", "request_id", "project_id", "runtime_digest",
                    "request_digest", "ready", "result", "reason_code"}


class AccountError(RuntimeError):
    """Only a fixed public reason, never OS/SQL/request content."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in ACCOUNT_REASON_CODES:
            reason_code = "account_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


def matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_ACCOUNT_INTEGER


@dataclass(frozen=True)
class AccountRequest:
    protocol_version: int
    operation: str
    request_id: str
    project_id: str
    runtime_digest: str
    payload: dict

    def __post_init__(self) -> None:
        encode_account_request(self)


def _request_data(request: AccountRequest) -> dict:
    if type(request) is not AccountRequest:
        raise AccountError("account_invalid_request")
    return {name: getattr(request, name) for name in _REQUEST_FIELDS}


def _checked_request(data: object) -> None:
    if (
        type(data) is not dict or set(data) != _REQUEST_FIELDS
        or type(data["protocol_version"]) is not int or data["protocol_version"] != ACCOUNT_PROTOCOL_VERSION
        or type(data["operation"]) is not str or data["operation"] not in ACCOUNT_OPERATIONS
        or not matches(data["request_id"], _ID) or not matches(data["project_id"], _PROJECT)
        or not matches(data["runtime_digest"], _DIGEST) or type(data["payload"]) is not dict
    ):
        raise AccountError("account_invalid_request")
    if data['operation'] == 'account.billing_probe':
        try:
            decode_billing_probe_input(data['payload'])
        except BillingProbeError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] in {'account.observe_provider', 'account.record_observation_unavailable'}:
        try:
            (decode_observation_input if data['operation'] == 'account.observe_provider' else
             decode_observation_failure)(data['payload'])
        except ObservationError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.record_rate_limit_event':
        try:
            decode_rate_limit_event(data['payload'])
        except RateLimitError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.open_host_run':
        try:
            decode_account_open_input(data['payload'], data['project_id'], data['runtime_digest'])
        except OpenError:
            raise AccountError('account_invalid_request') from None
    elif data["operation"] == "account.claim_launch":
        try:
            decode_claim_input(data["payload"])
        except ClaimError:
            raise AccountError("account_invalid_request") from None
    elif data["operation"] == "account.register_execution":
        try:
            decode_execution_registration(data["payload"])
        except ClaimError:
            raise AccountError("account_invalid_request") from None
    elif data['operation'] == 'account.run_provenance':
        try:
            decode_run_provenance_input(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.open_provenance':
        try:
            decode_open_provenance_input(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data["operation"] == "account.capacity_status":
        try:
            decode_capacity_status_input(data["payload"])
        except ValueError:
            raise AccountError("account_invalid_request") from None
    elif data["operation"] in {"account.submit_terminal", "account.release_capacity"}:
        try:
            decode_terminal_submission(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.close_denied_attempt':
        try:
            decode_close_denied(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.fence_claim':
        try:
            decode_fence_claim(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.settle_lost_run':
        try:
            decode_settle_lost(data['payload'])
        except ClaimError:
            raise AccountError('account_invalid_request') from None
    elif data['operation'] == 'account.withdraw_open':
        try:
            decode_withdraw_open(data['payload'])
        except WithdrawError:
            raise AccountError('account_invalid_request') from None
    elif data["payload"]:
        raise AccountError("account_invalid_request")


def encode_account_request(request: AccountRequest) -> bytes:
    data = _request_data(request)
    _checked_request(data)
    raw = canonical_json(data)
    if len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
        raise AccountError("account_request_too_large")
    return raw


def decode_account_request(raw: bytes) -> AccountRequest:
    if type(raw) is not bytes:
        raise AccountError("account_invalid_request")
    if len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
        raise AccountError("account_request_too_large")
    try:
        data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_FRAME_BYTES - 1)
        _checked_request(data)
        return AccountRequest(**data)
    except AccountError as exc:
        reason = exc.reason_code
    except (ContractError, TypeError, ValueError, RecursionError):
        reason = "account_invalid_request"
    raise AccountError(reason)


def account_request_digest(request: AccountRequest) -> str:
    return hashlib.sha256(encode_account_request(request)).hexdigest()


def _checked_result(result: object, request: AccountRequest) -> dict:
    if not reply_fields_match(request.operation, result):
        raise AccountError("account_invalid_response")
    if request.operation == 'account.billing_probe':
        try:
            return checked_billing_probe_reply(result)
        except BillingProbeError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.record_observation_unavailable':
        try:
            return checked_observation_failure_reply(result, decode_observation_failure(request.payload))
        except ObservationError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.record_rate_limit_event':
        try:
            return checked_rate_limit_reply(result)
        except RateLimitError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.observe_provider':
        try:
            return checked_observation_reply(result, decode_observation_input(request.payload))
        except ObservationError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.open_host_run':
        try:
            return checked_account_open_reply(result, decode_account_open_input(request.payload, request.project_id, request.runtime_digest))
        except OpenError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == "account.claim_launch":
        try:
            return checked_claim_receipt(result)
        except ClaimError:
            raise AccountError("account_invalid_response") from None
    elif request.operation == "account.register_execution":
        try:
            return checked_execution_reply(result)
        except ClaimError:
            raise AccountError("account_invalid_response") from None
    elif request.operation == 'account.run_provenance':
        try:
            return checked_run_provenance(result, decode_run_provenance_input(request.payload),
                                           request.project_id, request.runtime_digest)
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.open_provenance':
        try:
            return checked_open_provenance(result, decode_open_provenance_input(request.payload),
                                            request.project_id, request.runtime_digest)
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == "account.capacity_status":
        try:
            return checked_capacity_status(result)
        except ValueError:
            raise AccountError("account_invalid_response") from None
    elif request.operation == 'account.submit_terminal':
        try:
            checked = checked_terminal_receipt(result, decode_terminal_submission(request.payload))
            if checked['binding']['project_id'] != request.project_id or checked['binding']['runtime_digest'] != request.runtime_digest:
                raise ClaimError('claim_terminal_invalid')
            return checked
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.release_capacity':
        try:
            checked = checked_release_receipt(result, decode_terminal_submission(request.payload))
            if checked['binding']['project_id'] != request.project_id or checked['binding']['runtime_digest'] != request.runtime_digest:
                raise ClaimError('claim_cleanup_invalid')
            return checked
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.fence_claim':
        try:
            fence = decode_fence_claim(request.payload)
            checked = checked_fence_reply(result, fence)
            # A sweep's fence (no identity) closes an earlier version's request under that version's
            # runtime (0.3.3, C3f); the history checks the closure against the lease itself.
            if checked['closure'] is not None and (
                    checked['closure']['binding']['project_id'] != request.project_id
                    or (checked['closure']['binding']['runtime_digest'] != request.runtime_digest
                        and fence.identity_digest is not None)):
                raise ClaimError('claim_denial_invalid')
            return checked
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.settle_lost_run':
        try:
            return checked_lost_reply(result, decode_settle_lost(request.payload))
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.withdraw_open':
        try:
            return checked_withdraw_reply(result, decode_withdraw_open(request.payload))
        except WithdrawError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == 'account.close_denied_attempt':
        try:
            checked = checked_denial_closure(result, decode_close_denied(request.payload))
            if checked['binding']['project_id'] != request.project_id or checked['binding']['runtime_digest'] != request.runtime_digest:
                raise ClaimError('claim_denial_invalid')
            return checked
        except ClaimError:
            raise AccountError('account_invalid_response') from None
    elif request.operation == "account.provider_state_snapshot":
        from .admission_metadata import ProviderStateSnapshot
        try:
            checked = ProviderStateSnapshot(result["project_id"], result["evaluated_at_ms"], result["providers"])
            if (result != checked.audit() or result["project_id"] != request.project_id
                    or type(result["evaluated_at_ms"]) is not int
                    or set(result["providers"]) != {"codex", "claude"}
                    or any(v["state"] not in {"unknown", "ample", "constrained", "exhausted"}
                           for v in result["providers"].values())):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AccountError("account_invalid_response") from None
    elif request.operation == "account.health":
        if set(result) != {"transport_alive", "database_checked"} or result["transport_alive"] is not True or result["database_checked"] is not False:
            raise AccountError("account_invalid_response")
    elif request.operation == "account.project":
        if (
            set(result) != {"project_id", "status", "created_at_ms"}
            or result["project_id"] != request.project_id or result["status"] != "active"
            or not integer(result["created_at_ms"])
        ):
            raise AccountError("account_invalid_response")
    else:
        owner_fields = {"state", "fingerprint", "sequence", "replacements", "last_replaced_at_ms"}
        if request.operation == "account.owner_public_key":
            owner_fields.add("public_key")
        if (
            set(result) != owner_fields
            or type(result["state"]) is not str or result["state"] not in {"ok", "mismatch", "not_configured"}
            or not integer(result["sequence"]) or not integer(result["replacements"])
            or result["replacements"] > result["sequence"]
            or (result["fingerprint"] is not None and not matches(result["fingerprint"], _FINGERPRINT))
            or (result["last_replaced_at_ms"] is not None and not integer(result["last_replaced_at_ms"]))
            or (result["replacements"] == 0) != (result["last_replaced_at_ms"] is None)
            or (result["sequence"] == 0) != (result["fingerprint"] is None)
            or (result["state"] == "ok" and result["sequence"] == 0)
            or (result["state"] == "not_configured" and result["sequence"] != 0)
        ):
            raise AccountError("account_invalid_response")
        if request.operation == "account.owner_public_key":
            if result["state"] == "ok":
                invalid = False
                try:
                    _, fingerprint = checked_public_key(result["public_key"])
                    invalid = fingerprint != result["fingerprint"]
                except ValueError:
                    invalid = True
                if invalid:
                    raise AccountError("account_invalid_response")
            elif result["public_key"] is not None:
                raise AccountError("account_invalid_response")
    return result


def decode_account_response(raw: bytes, request: AccountRequest) -> dict:
    if type(raw) is bytes and raw == b"":
        raise AccountError("account_closed_without_reply")
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
            raise AccountError("account_invalid_response")
        data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_FRAME_BYTES - 1)
        if (
            set(data) != _RESPONSE_FIELDS or type(data["protocol_version"]) is not int
            or data["protocol_version"] != ACCOUNT_PROTOCOL_VERSION
            or data["request_id"] != request.request_id or data["project_id"] != request.project_id
            or data["runtime_digest"] != request.runtime_digest
            or data["request_digest"] != account_request_digest(request) or type(data["ready"]) is not bool
        ):
            raise AccountError("account_invalid_response")
        if data["ready"]:
            if data["reason_code"] is not None:
                raise AccountError("account_invalid_response")
            return _checked_result(data["result"], request)
        if data["result"] is not None or type(data["reason_code"]) is not str or data["reason_code"] not in ACCOUNT_REASON_CODES:
            raise AccountError("account_invalid_response")
        reason = data["reason_code"]
    except (AccountError, ContractError, TypeError, ValueError, RecursionError):
        reason = "account_invalid_response"
    raise AccountError(reason)


class AccountClient:
    """One exchange per operation; no database fallback, startup or auto retry.

    Claim replies describe durable state, never an executable launch grant.
    A lost reply stays ambiguous; this client cannot start or resend model work.
    """

    def __init__(self, socket_path: Path, project_id: str, runtime_digest: str, *, timeout: float = 1.0) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or type(timeout) not in {int, float} or not 0 < timeout <= MAX_TIMEOUT_SECONDS
            or not math.isfinite(timeout)
        ):
            raise AccountError("account_invalid_request")
        self._path, self._project, self._runtime, self._timeout = socket_path, project_id, runtime_digest, timeout

    def _read(self, operation: str, request_id: str | None) -> dict:
        identity = "read-" + secrets.token_hex(16) if request_id is None else request_id
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, operation, identity, self._project, self._runtime, {})
        raw = encode_account_request(request)
        sent = decode_account_request(raw)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else "account_unavailable"
        else:
            failed = None
        if failed:
            raise AccountError(failed)  # R7b: a service's own refusal reason is kept
        return decode_account_response(reply, sent)

    def health(self, *, request_id: str | None = None) -> dict:
        return self._read("account.health", request_id)

    def provider_state_snapshot(self, *, request_id: str | None = None):
        from .admission_metadata import ProviderStateSnapshot
        value = self._read("account.provider_state_snapshot", request_id)
        return ProviderStateSnapshot(value["project_id"], value["evaluated_at_ms"], value["providers"])

    def project(self, *, request_id: str | None = None) -> dict:
        return self._read("account.project", request_id)

    def owner_key(self, *, request_id: str | None = None) -> dict:
        return self._read("account.owner_key", request_id)

    def owner_public_key(self, *, request_id: str | None = None) -> dict:
        return self._read("account.owner_public_key", request_id)

    def billing_probe(self, *, request_id):
        """One original fixed probe request, never an automatic retry/reprobe."""
        request = AccountRequest(1, 'account.billing_probe', request_id, self._project, self._runtime, {})
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(reply, request)

    def observe_provider(self, provider, *, request_id, run_handle=None, phase=None):
        """One explicit non-spending request; a known reply is historical metadata."""
        try:
            payload = ObservationInput(provider, run_handle, phase).payload()
        except ObservationError:
            raise AccountError('account_invalid_request') from None
        request = AccountRequest(1, 'account.observe_provider', request_id, self._project, self._runtime, payload)
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(reply, request)

    def record_observation_unavailable(self, provider, *, run_handle, phase, reason_code, request_id):
        """One explicit fixed-metadata request; never probe or retry a model."""
        try:
            payload = ObservationFailureInput(provider, run_handle, phase, reason_code).payload()
        except ObservationError:
            raise AccountError('account_invalid_request') from None
        request = AccountRequest(1, 'account.record_observation_unavailable', request_id, self._project, self._runtime, payload)
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(reply, request)

    def record_rate_limit_event(self, run_handle, rate_limit_info, *, request_id):
        """One event of a registered run; a lost reply is never resent as success."""
        try:
            payload = RateLimitEventInput(run_handle, rate_limit_info).payload()
        except RateLimitError:
            raise AccountError('account_invalid_request') from None
        request = AccountRequest(1, 'account.record_rate_limit_event', request_id, self._project, self._runtime, payload)
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(reply, request)

    def claim_launch(self, run_handle: str, identity_digest: str, *, request_id: str) -> dict:
        try:
            payload = LaunchClaimInput(run_handle, identity_digest).payload()
        except ClaimError:
            raise AccountError("account_invalid_request") from None
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.claim_launch", request_id,
                                 self._project, self._runtime, payload)
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else "account_unavailable"
        else:
            failed = None
        if failed:
            raise AccountError(failed)  # R7b: a service's own refusal reason is kept
        return decode_account_response(reply, request)

    def open_host_run(self, opening: AccountOpenInput, *, request_id: str) -> dict:
        """One original-open exchange; returned metadata is not execution authority."""
        if type(opening) is not AccountOpenInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(1, 'account.open_host_run', request_id, self._project, self._runtime, opening.payload())
        except OpenError:
            raise AccountError('account_invalid_request') from None
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 request_limit=MAX_ACCOUNT_FRAME_BYTES, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(raw, request)

    def register_execution(self, run_handle, bridge_run_id, claim_digest, claim_request_id, identity_digest, *, request_id):
        try:
            payload = ExecutionRegistrationInput(run_handle, bridge_run_id, claim_digest,
                                                 claim_request_id, identity_digest).payload()
        except ClaimError:
            raise AccountError("account_invalid_request") from None
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.register_execution", request_id,
                                 self._project, self._runtime, payload)
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else "account_unavailable"
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def run_provenance(self, provenance_request, *, request_id):
        """Read original registered metadata once; never retry or register work."""
        if type(provenance_request) is not RunProvenanceInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(1, 'account.run_provenance', request_id, self._project, self._runtime,
                                     provenance_request.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 request_limit=MAX_ACCOUNT_FRAME_BYTES, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def open_provenance(self, provenance_request, *, request_id):
        """Read original pre-claim metadata once; never open or admit a run."""
        if type(provenance_request) is not OpenProvenanceInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(1, 'account.open_provenance', request_id, self._project, self._runtime,
                                     provenance_request.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 request_limit=MAX_ACCOUNT_FRAME_BYTES, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(raw, request)

    def release_capacity(self, submission, *, request_id):
        """One bounded exchange; proof comes from the enrolled project peer."""
        from .terminal_delivery import TerminalSubmissionInput
        if type(submission) is not TerminalSubmissionInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.release_capacity', request_id,
                                     self._project, self._runtime, submission.payload())
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 request_limit=MAX_ACCOUNT_FRAME_BYTES, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            raise AccountError(error.reason_code if error.reason_code in SERVICE_REFUSALS
                               else 'account_unavailable') from None
        return decode_account_response(raw, request)

    def submit_terminal(self, submission, *, request_id):
        """One bounded metadata exchange, never cleanup or capacity release."""
        from .terminal_delivery import TerminalSubmissionInput

        if type(submission) is not TerminalSubmissionInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.submit_terminal', request_id,
                                     self._project, self._runtime, submission.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def close_denied_attempt(self, closure, *, request_id):
        """One explicit permanent no-launch closure, never a claim retry."""
        if type(closure) is not CloseDeniedInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.close_denied_attempt', request_id,
                                     self._project, self._runtime, closure.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def fence_claim(self, fence, *, request_id):
        """Close the run's own claim request for good, unless the account already admitted it."""
        if type(fence) is not FenceClaimInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.fence_claim', request_id,
                                     self._project, self._runtime, fence.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def settle_lost_run(self, settle, *, request_id):
        """Settle a claimed run whose service was lost; the project checked its kernel evidence first."""
        if type(settle) is not SettleLostInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.settle_lost_run', request_id,
                                     self._project, self._runtime, settle.payload())
        except ClaimError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def withdraw_open(self, withdrawal, *, request_id):
        """End a never-claimed run's open at the account (B3); an identical retry gets the stored receipt."""
        if type(withdrawal) is not WithdrawOpenInput:
            raise AccountError('account_invalid_request')
        try:
            request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open', request_id,
                                     self._project, self._runtime, withdrawal.payload())
        except WithdrawError:
            raise AccountError('account_invalid_request') from None
        failed = False
        try:
            raw = exchange_frame(self._path, encode_account_request(request), timeout=self._timeout,
                                 response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:  # R7b: a service's own refusal reason is kept
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else 'account_unavailable'
        if failed:
            raise AccountError(failed)
        return decode_account_response(raw, request)

    def capacity_status(self, run_handle: str, bridge_run_id: str, *, request_id: str | None = None) -> dict:
        """Read the current reservation/receipt, including after a lost reply.

        Unknown or held never permits relaunch or frees a slot. A released
        receipt is owner metadata, not fresh process cleanup evidence. Each
        explicit call makes one exchange, with no database fallback or retry.
        """
        try:
            payload = CapacityStatusInput(run_handle, bridge_run_id).payload()
        except ValueError:
            raise AccountError("account_invalid_request") from None
        identity = "read-" + secrets.token_hex(16) if request_id is None else request_id
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.capacity_status", identity,
                                 self._project, self._runtime, payload)
        raw = encode_account_request(request)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError as error:
            failed = error.reason_code if error.reason_code in SERVICE_REFUSALS else "account_unavailable"
        else:
            failed = None
        if failed:
            raise AccountError(failed)  # R7b: a service's own refusal reason is kept
        return decode_account_response(reply, request)
