"""Closed local status/cancel codec and client; no persistence or launch work.

The caller supplies an already trusted endpoint and scoped labels. Neither a
socket path nor these labels authenticate a native primary. Responses describe
the live monitor only, not a durable cancellation, outcome or account receipt.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError
from .learning_contracts import parse_bounded_json
from .service_protocol import (
    MAX_GENERATION, PROTOCOL_VERSION, ServiceProtocolError, ServiceRequest, ServiceTarget,
    canonical_request_bytes, decode_service_request, service_request_digest,
)
from .service_transport import MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

MAX_CONTROL_FRAME_BYTES = 16_384
MAX_CONTROL_ATTEMPTS = 4096
_MAX_MS = (MAX_GENERATION + 999_999) // 1_000_000
_SCOPE_FIELDS = ("project_id", "primary_session_id", "supervisor_instance_id", "supervisor_generation", "runtime_digest")
_RESPONSE_FIELDS = {"protocol_version", "request_id", "request_digest", "scope", "target",
                    "ready", "observed_at_ns", "result", "reason_code"}
HEALTH_STATES = frozenset({"starting", "active", "quiet", "stale", "stopping", "terminal", "unavailable", "cleaned"})
STOP_REASONS = frozenset({"cancel_requested", "startup_silence", "work_silence", "absolute_deadline", "monitor_unavailable"})
CONTROL_REASON_CODES = frozenset({
    "control_invalid_input", "control_invalid_request", "control_request_conflict", "control_invalid_response",
    "control_not_authorized", "control_unavailable", "control_wrong_owner", "control_attempt_not_found",
    "control_wrong_scope", "control_binding_conflict",
})
_LIVENESS_FIELDS = {"health", "elapsed_ms", "silence_age_ms", "startup_remaining_ms", "silence_remaining_ms",
                    "hard_deadline_remaining_ms", "activity_events", "last_activity_category", "stop_reason", "terminal"}
_ATTEMPT_FIELDS = {"kind", "health", "liveness", "liveness_frozen", "stop_requested", "stop_reason",
                   "terminal_observed", "cleanup_confirmed", "cancellation_state", "durable"}
_SUMMARY_FIELDS = {"kind", "tracked_attempts", "health_counts", "stop_requested", "terminal_observed",
                   "cleanup_confirmed", "durable"}


class ControlError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in CONTROL_REASON_CODES:
            reason_code = "control_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _integer(value, maximum=MAX_GENERATION) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _closed(value, fields) -> bool:
    return type(value) is dict and value.keys() == fields


@dataclass(frozen=True)
class ControlScope:
    project_id: str
    primary_session_id: str
    supervisor_instance_id: str
    supervisor_generation: int
    runtime_digest: str

    def __post_init__(self) -> None:
        scope_data(self)


def scope_data(scope: ControlScope) -> dict:
    invalid = False
    try:
        if type(scope) is not ControlScope:
            raise ControlError("control_invalid_input")
        values = {name: getattr(scope, name) for name in _SCOPE_FIELDS}
        ServiceRequest(PROTOCOL_VERSION, "project.status", "scope-validation", **values,
                       target=ServiceTarget("orc_" + "0" * 48, None, None))
    except (ControlError, ServiceProtocolError, AttributeError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ControlError("control_invalid_input")
    return values


def target_data(target: ServiceTarget) -> dict:
    if type(target) is not ServiceTarget:
        raise ControlError("control_invalid_input")
    return {name: getattr(target, name) for name in ("orchestration_handle", "run_handle", "attempt")}


def control_request_id(request: ServiceRequest) -> str:
    """Bind the complete operation/scope/target without retaining receipt aliases."""
    canonical_request_bytes(request)
    base = ServiceRequest(PROTOCOL_VERSION, request.operation, "control-identity",
                          **{name: getattr(request, name) for name in _SCOPE_FIELDS}, target=request.target)
    return "ctl_" + hashlib.sha256(canonical_request_bytes(base)).hexdigest()


def control_request(scope: ControlScope, operation: str, target: ServiceTarget) -> ServiceRequest:
    invalid = False
    try:
        request = ServiceRequest(PROTOCOL_VERSION, operation, "control-identity", **scope_data(scope), target=target)
        request = ServiceRequest(PROTOCOL_VERSION, operation, control_request_id(request), **scope_data(scope), target=target)
    except (ControlError, ServiceProtocolError, AttributeError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ControlError("control_invalid_request")
    return request


def encode_control_request(request: ServiceRequest) -> bytes:
    reason = None
    try:
        encoded = canonical_request_bytes(request)
        if len(encoded) + 1 > MAX_CONTROL_FRAME_BYTES:
            raise ControlError("control_invalid_request")
        if request.request_id != control_request_id(request):
            raise ControlError("control_request_conflict")
    except ControlError as exc:
        reason = exc.reason_code
    except (ServiceProtocolError, AttributeError, TypeError, ValueError, OverflowError, RecursionError):
        reason = "control_invalid_request"
    if reason is not None:
        raise ControlError(reason)
    return encoded


def decode_control_request(raw: bytes) -> ServiceRequest:
    reason = None
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_CONTROL_FRAME_BYTES:
            raise ControlError("control_invalid_request")
        request = decode_service_request(raw)
        encode_control_request(request)
    except ControlError as exc:
        reason = exc.reason_code
    except (ServiceProtocolError, TypeError, ValueError, OverflowError, RecursionError):
        reason = "control_invalid_request"
    if reason is not None:
        raise ControlError(reason)
    return request


def _validate_liveness(value: dict) -> None:
    if (
        not _closed(value, _LIVENESS_FIELDS) or type(value["health"]) is not str
        or value["health"] not in HEALTH_STATES - {"cleaned", "unavailable"}
        or not all(_integer(value[name], _MAX_MS) for name in ("elapsed_ms", "hard_deadline_remaining_ms"))
        or not all(value[name] is None or _integer(value[name], _MAX_MS)
                   for name in ("silence_age_ms", "startup_remaining_ms", "silence_remaining_ms"))
        or not _integer(value["activity_events"], (1 << 31) - 1)
        or type(value["terminal"]) is not bool
        or (value["stop_reason"] is not None and (type(value["stop_reason"]) is not str
                                                 or value["stop_reason"] not in STOP_REASONS - {"monitor_unavailable"}))
    ):
        raise ControlError("control_invalid_response")
    starting = value["silence_age_ms"] is None
    if (
        (value["startup_remaining_ms"] is not None) != starting
        or (value["silence_remaining_ms"] is None) != starting
        or (starting and (value["last_activity_category"] is not None or value["activity_events"] != 0))
        or (not starting and (type(value["last_activity_category"]) is not str
                             or value["last_activity_category"] not in {"startup", "qualifying_work"}))
        or (not starting and ((value["last_activity_category"] == "startup" and value["activity_events"] != 0)
                              or (value["last_activity_category"] == "qualifying_work" and value["activity_events"] == 0)))
        or (value["health"] == "stopping") != (value["stop_reason"] is not None)
        or (value["health"] == "terminal") != value["terminal"]
        or (value["health"] == "starting" and not starting)
        or (value["health"] in {"active", "quiet", "stale"} and starting)
    ):
        raise ControlError("control_invalid_response")


def _validate_result(request: ServiceRequest, result: dict) -> None:
    if request.target.run_handle is None:
        if (
            not _closed(result, _SUMMARY_FIELDS) or result["kind"] != "orchestration" or result["durable"] is not False
            or not _integer(result["tracked_attempts"], MAX_CONTROL_ATTEMPTS)
            or not _closed(result["health_counts"], HEALTH_STATES)
            or not all(_integer(count, result["tracked_attempts"]) for count in result["health_counts"].values())
            or sum(result["health_counts"].values()) != result["tracked_attempts"]
            or not all(_integer(result[name], result["tracked_attempts"])
                       for name in ("stop_requested", "terminal_observed", "cleanup_confirmed"))
            or result["health_counts"]["cleaned"] != result["cleanup_confirmed"]
        ):
            raise ControlError("control_invalid_response")
        counts = result["health_counts"]
        required_stops = counts["stopping"] + counts["unavailable"]
        stop_capacity = required_stops + counts["cleaned"]
        # Cleanup may precede terminal observation, and a first stop preserves
        # its health after a later terminal. Live health can carry neither fact.
        if (not required_stops <= result["stop_requested"] <= stop_capacity
                or not counts["terminal"] <= result["terminal_observed"] <= counts["terminal"] + stop_capacity):
            raise ControlError("control_invalid_response")
        return
    if (
        not _closed(result, _ATTEMPT_FIELDS) or result["kind"] != "attempt" or result["durable"] is not False
        or type(result["health"]) is not str or result["health"] not in HEALTH_STATES
        or not all(type(result[name]) is bool for name in
                   ("liveness_frozen", "stop_requested", "terminal_observed", "cleanup_confirmed"))
        or (result["stop_reason"] is not None and (type(result["stop_reason"]) is not str
                                                  or result["stop_reason"] not in STOP_REASONS))
        or result["stop_requested"] != (result["stop_reason"] is not None)
    ):
        raise ControlError("control_invalid_response")
    liveness = result["liveness"]
    if liveness is None:
        if result["health"] != ("cleaned" if result["cleanup_confirmed"] else "unavailable") or result["liveness_frozen"]:
            raise ControlError("control_invalid_response")
        if not result["cleanup_confirmed"] and result["stop_reason"] != "monitor_unavailable":
            raise ControlError("control_invalid_response")
    else:
        _validate_liveness(liveness)
        if (result["cleanup_confirmed"] or result["health"] != liveness["health"]
                or result["stop_reason"] != liveness["stop_reason"]
                or result["liveness_frozen"] != (liveness["stop_reason"] is not None or liveness["terminal"])
                or (liveness["terminal"] and not result["terminal_observed"])
                or (result["terminal_observed"] and not (liveness["terminal"] or result["stop_requested"]))):
            raise ControlError("control_invalid_response")
    expected = ("cleanup_confirmed" if result["cleanup_confirmed"] else "stop_requested" if result["stop_requested"]
                else "terminal_observed" if result["terminal_observed"] else "running")
    if result["cancellation_state"] != expected or (request.operation == "project.cancel" and expected == "running"):
        raise ControlError("control_invalid_response")


@dataclass(frozen=True)
class ControlView:
    observed_at_ns: int
    result: dict


def decode_control_response(raw: bytes, request: ServiceRequest) -> ControlView:
    invalid = False
    try:
        encode_control_request(request)
        if type(raw) is not bytes or len(raw) + 1 > MAX_CONTROL_FRAME_BYTES:
            raise ControlError("control_invalid_response")
        value = parse_bounded_json(raw, max_bytes=MAX_CONTROL_FRAME_BYTES - 1)
        if (
            not _closed(value, _RESPONSE_FIELDS) or type(value["protocol_version"]) is not int
            or value["protocol_version"] != PROTOCOL_VERSION or value["request_id"] != request.request_id
            or value["request_digest"] != service_request_digest(request)
            or not _closed(value["scope"], set(_SCOPE_FIELDS))
            or scope_data(ControlScope(**value["scope"])) != {name: getattr(request, name) for name in _SCOPE_FIELDS}
            or not _closed(value["target"], {"orchestration_handle", "run_handle", "attempt"})
            or target_data(ServiceTarget(**value["target"])) != target_data(request.target)
            or type(value["ready"]) is not bool
        ):
            raise ControlError("control_invalid_response")
        if value["ready"]:
            if value["reason_code"] is not None or not _integer(value["observed_at_ns"]):
                raise ControlError("control_invalid_response")
            _validate_result(request, value["result"])
        elif (value["result"] is not None or value["observed_at_ns"] is not None
              or type(value["reason_code"]) is not str or value["reason_code"] not in CONTROL_REASON_CODES):
            raise ControlError("control_invalid_response")
    except (ControlError, ServiceProtocolError, ContractError, AttributeError, KeyError, TypeError, ValueError,
            OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise ControlError("control_invalid_response")
    if not value["ready"]:
        raise ControlError(value["reason_code"])
    return ControlView(value["observed_at_ns"], value["result"])


class ControlClient:
    """One local observation per request; no resend, persistence or owner start.

    Caller and trusted owner must use the same host monotonic clock domain.
    Publication within the request interval is checked separately from frozen
    liveness ages. A failed/lost cancel reply does not prove no stop was requested.
    Explicit retry uses the same scoped business ID, never launches model work.
    """

    def __init__(self, endpoint: Path, scope: ControlScope, *, timeout: float, clock_ns=time.monotonic_ns) -> None:
        if (not isinstance(endpoint, Path) or not endpoint.is_absolute()
                or type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS or not callable(clock_ns)):
            raise ControlError("control_invalid_input")
        self._scope = ControlScope(**scope_data(scope))
        self._endpoint, self._timeout, self._clock = endpoint, timeout, clock_ns

    def _now(self) -> int:
        failed = False
        try:
            value = self._clock()
            failed = not _integer(value)
        except Exception:
            failed = True
        if failed:
            raise ControlError("control_unavailable")
        return value

    def _call(self, operation: str, target: ServiceTarget) -> ControlView:
        request = decode_control_request(encode_control_request(control_request(self._scope, operation, target)))
        started = self._now()
        failed = False
        try:
            raw = exchange_frame(self._endpoint, encode_control_request(request), timeout=self._timeout,
                                 request_limit=MAX_CONTROL_FRAME_BYTES, response_limit=MAX_CONTROL_FRAME_BYTES)
        except ServiceTransportError:
            failed = True
        if failed:
            raise ControlError("control_unavailable")
        view = decode_control_response(raw, request)
        finished = self._now()
        if not started <= view.observed_at_ns <= finished or finished - started > self._timeout * 1_000_000_000:
            raise ControlError("control_unavailable")
        return view

    def status(self, target: ServiceTarget) -> ControlView:
        return self._call("project.status", target)

    def cancel(self, target: ServiceTarget) -> ControlView:
        return self._call("project.cancel", target)
