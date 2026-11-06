"""Local monitor service seam: no enabled native listener or persistence path.

The native front end must authenticate and authorize current owning-primary
context before this owner-thread work. Its supplied authorizer must be bounded,
using already checked in-memory native state; slow verification belongs outside
the timer/control loop. Dataclasses and request labels are not that proof.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .control_api import (
    HEALTH_STATES, MAX_CONTROL_FRAME_BYTES, ControlError, ControlScope, decode_control_request,
    decode_control_response, scope_data, target_data,
)
from .learning_contracts import canonical_json
from .project_monitor import AttemptStatus, ProjectMonitor, ProjectMonitorError
from .service_protocol import OPERATIONS, PROTOCOL_VERSION, ServiceRequest, ServiceTarget, service_request_digest


@dataclass(frozen=True)
class ControlAuthorization:
    """Trusted adapter output, not a JSON principal or new native capability."""

    scope: ControlScope
    target: ServiceTarget
    request_digest: str
    allowed_operations: frozenset[str]

    def __post_init__(self) -> None:
        invalid = False
        try:
            scope_data(self.scope)
            ServiceRequest(PROTOCOL_VERSION, "project.status", "authorization-validation",
                           **scope_data(self.scope), target=self.target)
            invalid = (
                type(self.request_digest) is not str or len(self.request_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.request_digest)
                or type(self.allowed_operations) is not frozenset or not self.allowed_operations
                or any(type(operation) is not str for operation in self.allowed_operations)
                or not self.allowed_operations <= OPERATIONS
            )
        except Exception:
            invalid = True
        if invalid:
            raise ControlError("control_not_authorized")


def _attempt(status: AttemptStatus) -> dict:
    liveness = None if status.liveness is None else asdict(status.liveness)
    cancellation = ("cleanup_confirmed" if status.cleanup_confirmed else "stop_requested" if status.stop_requested
                    else "terminal_observed" if status.terminal_observed else "running")
    return {"kind": "attempt", "health": status.health, "liveness": liveness,
            "liveness_frozen": liveness is not None and (status.stop_requested or liveness["terminal"]),
            "stop_requested": status.stop_requested, "stop_reason": status.stop_reason,
            "terminal_observed": status.terminal_observed, "cleanup_confirmed": status.cleanup_confirmed,
            "cancellation_state": cancellation, "durable": False}


class ControlService:
    def __init__(self, monitor: ProjectMonitor, *,
                 authorizer: Callable[[ServiceRequest, object], ControlAuthorization | None] | None = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if type(monitor) is not ProjectMonitor or (authorizer is not None and not callable(authorizer)) or not callable(clock_ns):
            raise ControlError("control_invalid_input")
        foreign = False
        try:
            monitor._owner()
        except ProjectMonitorError:
            foreign = True
        if foreign:
            raise ControlError("control_wrong_owner")
        self._scope = ControlScope(**asdict(monitor.scope()))
        self._monitor, self._authorizer, self._clock = monitor, authorizer, clock_ns
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._closed = False
        self._last_published = None
        self._clock_failed = False
        self._sampling_clock = False

    def _authorize(self, request: ServiceRequest, context: object, digest: str) -> None:
        if {name: getattr(request, name) for name in scope_data(self._scope)} != scope_data(self._scope) or self._authorizer is None:
            raise ControlError("control_not_authorized")
        authorization = self._authorizer(request, context)
        if type(authorization) is not ControlAuthorization:
            raise ControlError("control_not_authorized")
        authorization.__post_init__()
        if (scope_data(authorization.scope) != scope_data(self._scope)
                or target_data(authorization.target) != target_data(request.target)
                or authorization.request_digest != digest or service_request_digest(request) != digest
                or request.operation not in authorization.allowed_operations):
            raise ControlError("control_not_authorized")

    @staticmethod
    def _reply(request=None, digest=None, *, observed=None, result=None, reason=None) -> bytes:
        return canonical_json({
            "protocol_version": PROTOCOL_VERSION, "request_id": request.request_id if request else None,
            "request_digest": digest, "scope": {name: getattr(request, name) for name in
                ("project_id", "primary_session_id", "supervisor_instance_id", "supervisor_generation", "runtime_digest")} if request else None,
            "target": target_data(request.target) if request else None, "ready": reason is None,
            "observed_at_ns": observed, "result": result, "reason_code": reason,
        })

    def _publish_time(self) -> int:
        if self._clock_failed or self._sampling_clock:
            self._clock_failed = True
            raise ControlError("control_unavailable")
        self._sampling_clock = True
        try:
            try:
                value = self._clock()
            except Exception:
                failed = True
            else:
                failed = (self._clock_failed or type(value) is not int or not 0 <= value <= (1 << 63) - 1
                          or (self._last_published is not None and value < self._last_published))
            if failed:
                self._clock_failed = True
                raise ControlError("control_unavailable")
            self._last_published = value
            return value
        except BaseException:
            self._clock_failed = True
            raise
        finally:
            self._sampling_clock = False

    def handle(self, raw: bytes, transport_context: object = None) -> bytes:
        request = None
        digest = None
        try:
            if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
                raise ControlError("control_wrong_owner")
            request = decode_control_request(raw)
            digest = service_request_digest(request)
            self._authorize(request, transport_context, digest)
            if request.target.run_handle is None:
                statuses = self._monitor.orchestration_status(request.target.orchestration_handle)
                counts = {health: 0 for health in HEALTH_STATES}
                for status in statuses:
                    counts[status.health] += 1
                result = {"kind": "orchestration", "tracked_attempts": len(statuses), "health_counts": counts,
                          "stop_requested": sum(status.stop_requested for status in statuses),
                          "terminal_observed": sum(status.terminal_observed for status in statuses),
                          "cleanup_confirmed": sum(status.cleanup_confirmed for status in statuses), "durable": False}
            else:
                binding = self._monitor.lookup(request.target)
                result = _attempt(self._monitor.cancel(binding) if request.operation == "project.cancel"
                                  else self._monitor.status(binding))
            observed = self._publish_time()
            reply = self._reply(request, digest, observed=observed, result=result)
            if len(reply) + 1 > MAX_CONTROL_FRAME_BYTES:
                raise ControlError("control_unavailable")
            decode_control_response(reply, request)
            return reply
        except ControlError as exc:
            reason = exc.reason_code
        except ProjectMonitorError as exc:
            reason = {
                "monitor_wrong_owner": "control_wrong_owner", "monitor_wrong_scope": "control_wrong_scope",
                "monitor_attempt_not_found": "control_attempt_not_found", "monitor_binding_conflict": "control_binding_conflict",
            }.get(exc.reason_code, "control_unavailable")
        except Exception:
            reason = "control_unavailable"
        # Re-decode bounded original labels if an adapter mutated the request.
        # Foreign-owner refusal still occurs before decoding or routing input.
        if reason != "control_wrong_owner":
            try:
                request = decode_control_request(raw)
                digest = service_request_digest(request)
            except ControlError:
                request = None
                digest = None
        return self._reply(request, digest, reason=reason)

    def close(self) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise ControlError("control_wrong_owner")
        # The monitor and tracked executor remain responsible for local safety;
        # closing this facade neither disables timers nor confirms cleanup.
        self._closed = True
