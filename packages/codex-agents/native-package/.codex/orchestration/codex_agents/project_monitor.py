"""Trusted local attempt control, separate from persistence and native authority.

This registry connects exact already-authorized bindings to local timer decisions.
It is not an RPC handler, caller authenticator, admission/launch authority or process
terminator. A native adapter must validate activity and actual terminal/cleanup
observations first. No model payload, provider timestamp, writer or I/O callback is
accepted. The owner must tick independently and retain the detached hard watchdog.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .history_contracts import AttemptBinding, HistoryError
from .liveness_clock import MAX_CLOCK_NS, LivenessClock, LivenessClockError, LivenessLimits, LivenessSnapshot
from .service_protocol import ServiceProtocolError, ServiceRequest, ServiceTarget

MAX_TRACKED_ATTEMPTS = 4096
MONITOR_REASON_CODES = frozenset({
    "monitor_invalid_input", "monitor_wrong_scope", "monitor_attempt_not_found",
    "monitor_binding_conflict", "monitor_capacity_exceeded", "monitor_wrong_owner",
})
_BINDING_FIELDS = tuple(AttemptBinding.__dataclass_fields__)
_SCOPE_FIELDS = ("project_id", "primary_session_id", "runtime_digest",
                 "supervisor_instance_id", "supervisor_generation")
_LIMIT_FIELDS = ("startup_ns", "quiet_ns", "stale_ns", "silence_ns")


class ProjectMonitorError(RuntimeError):
    """Fixed codes without rejected values, paths or native event content."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in MONITOR_REASON_CODES:
            reason_code = "monitor_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SupervisorScope:
    """Internal owner binding; constructing it proves no native caller identity."""

    project_id: str
    primary_session_id: str
    runtime_digest: str
    supervisor_instance_id: str
    supervisor_generation: int

    def __post_init__(self) -> None:
        invalid = False
        try:
            # Reuse the pure control codec's strict scalar contract, without
            # creating an endpoint, request permission or external credential.
            ServiceRequest(
                1, "project.status", "scope_validation", self.project_id,
                self.primary_session_id, self.supervisor_instance_id,
                self.supervisor_generation, self.runtime_digest,
                ServiceTarget("orc_" + "0" * 48, None, None),
            )
        except ServiceProtocolError:
            invalid = True
        if invalid:
            raise ProjectMonitorError("monitor_invalid_input")


@dataclass(frozen=True)
class AttemptStatus:
    """Local metadata, never a durable result or account-capacity receipt.

    Liveness ages freeze at a stop/terminal decision. Publication freshness must
    be supplied separately by the actual transport/monitor, not inferred here.
    """

    binding: AttemptBinding
    liveness: LivenessSnapshot | None
    stop_reason: str | None
    terminal_observed: bool
    cleanup_confirmed: bool

    @property
    def health(self) -> str:
        return "unavailable" if self.liveness is None else self.liveness.health

    @property
    def stop_requested(self) -> bool:
        return self.stop_reason is not None


@dataclass
class _Attempt:
    binding: tuple
    deadline: int
    limits: tuple[int, int, int, int]
    clock: LivenessClock
    monitor_failed: bool = False
    terminal_observed: bool = False
    cleanup_confirmed: bool = False


def _capture_binding(binding: AttemptBinding) -> AttemptBinding:
    if type(binding) is not AttemptBinding:
        raise ProjectMonitorError("monitor_invalid_input")
    invalid = False
    try:
        result = AttemptBinding(**{field: getattr(binding, field) for field in _BINDING_FIELDS})
    except (HistoryError, AttributeError):
        invalid = True
    if invalid:
        raise ProjectMonitorError("monitor_invalid_input")
    return result


class ProjectMonitor:
    """One process/thread's bounded attempt registry; no shared/global lock.

    Register after upstream authorization and before native launch so the startup
    clock covers launch. Registration is not launch permission. Entries remain
    for this registry's lifetime: no eviction, automatic
    relaunch, forgotten cancellation or reuse of a run/attempt with new bounds.
    All methods are trusted owner calls, not operations exposed to native clients.
    """

    def __init__(
        self, scope: SupervisorScope, limits: LivenessLimits, *, max_tracked_attempts: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (type(scope) is not SupervisorScope or type(limits) is not LivenessLimits
                or type(max_tracked_attempts) is not int
                or not 1 <= max_tracked_attempts <= MAX_TRACKED_ATTEMPTS or not callable(clock_ns)):
            raise ProjectMonitorError("monitor_invalid_input")
        invalid = False
        try:
            scope = SupervisorScope(**{field: getattr(scope, field) for field in _SCOPE_FIELDS})
            limits = LivenessLimits(**{field: getattr(limits, field) for field in _LIMIT_FIELDS})
        except (AttributeError, LivenessClockError, ProjectMonitorError):
            invalid = True
        if invalid:
            raise ProjectMonitorError("monitor_invalid_input")
        self._scope = tuple(getattr(scope, field) for field in _SCOPE_FIELDS)
        self._limits = limits
        self._maximum = max_tracked_attempts
        self._clock = clock_ns
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._attempts: dict[tuple[str, int], _Attempt] = {}

    def _owner(self) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise ProjectMonitorError("monitor_wrong_owner")

    def _binding(self, binding: AttemptBinding) -> AttemptBinding:
        self._owner()
        binding = _capture_binding(binding)
        if tuple(getattr(binding, field) for field in _SCOPE_FIELDS) != self._scope:
            raise ProjectMonitorError("monitor_wrong_scope")
        return binding

    def _entry(self, binding: AttemptBinding) -> _Attempt:
        binding = self._binding(binding)
        entry = self._attempts.get((binding.run_handle, binding.attempt))
        if entry is None:
            raise ProjectMonitorError("monitor_attempt_not_found")
        if tuple(getattr(binding, field) for field in _BINDING_FIELDS) != entry.binding:
            raise ProjectMonitorError("monitor_binding_conflict")
        return entry

    @staticmethod
    def _observe(entry: _Attempt, operation: str) -> AttemptStatus:
        snapshot = None
        if not entry.monitor_failed:
            try:
                snapshot = getattr(entry.clock, operation)()
            except LivenessClockError as exc:
                if exc.reason_code != "liveness_invalid_clock":
                    raise
                entry.monitor_failed = True
        # Never retain or return a healthy snapshot after clock failure. Cleanup
        # must still be requested; the detached watchdog remains independent.
        return AttemptStatus(
            AttemptBinding(**dict(zip(_BINDING_FIELDS, entry.binding, strict=True))),
            snapshot, "monitor_unavailable" if entry.monitor_failed else snapshot.stop_reason,
            entry.terminal_observed, entry.cleanup_confirmed,
        )

    def register(
        self, binding: AttemptBinding, *, hard_deadline_ns: int,
        limits: LivenessLimits | None = None,
    ) -> AttemptStatus:
        """Capture one immutable timer; an identical retry cannot rearm it."""
        binding = self._binding(binding)
        if type(hard_deadline_ns) is not int or not 0 <= hard_deadline_ns <= MAX_CLOCK_NS:
            raise ProjectMonitorError("monitor_invalid_input")
        invalid = False
        try:
            if limits is None:
                limits = self._limits
            if type(limits) is not LivenessLimits:
                raise LivenessClockError("liveness_invalid_limits")
            requested = LivenessLimits(**{field: getattr(limits, field) for field in _LIMIT_FIELDS})
            effective = self._limits.narrowed(requested)
        except (AttributeError, LivenessClockError):
            invalid = True
        if invalid:
            raise ProjectMonitorError("monitor_invalid_input")
        values = tuple(getattr(binding, field) for field in _BINDING_FIELDS)
        durations = tuple(getattr(effective, field) for field in _LIMIT_FIELDS)
        key = (binding.run_handle, binding.attempt)
        entry = self._attempts.get(key)
        if entry is not None:
            if (entry.binding, entry.deadline, entry.limits) != (values, hard_deadline_ns, durations):
                raise ProjectMonitorError("monitor_binding_conflict")
            return self._observe(entry, "poll")
        if len(self._attempts) >= self._maximum:
            raise ProjectMonitorError("monitor_capacity_exceeded")
        timer = LivenessClock(effective, hard_deadline_ns=hard_deadline_ns, clock_ns=self._clock)
        entry = _Attempt(values, hard_deadline_ns, durations, timer)
        self._attempts[key] = entry
        return self._observe(entry, "poll")

    def status(self, binding: AttemptBinding) -> AttemptStatus:
        return self._observe(self._entry(binding), "poll")

    def startup(self, binding: AttemptBinding) -> AttemptStatus:
        """Observe trusted, already validated and attributed native startup."""
        return self._observe(self._entry(binding), "startup")

    def activity(self, binding: AttemptBinding) -> AttemptStatus:
        """Observe trusted qualifying work; no raw event/content is accepted."""
        return self._observe(self._entry(binding), "activity")

    def cancel(self, binding: AttemptBinding) -> AttemptStatus:
        """Request an authorized stop, without asserting terminal or cleanup."""
        return self._observe(self._entry(binding), "cancel")

    def observe_terminal(self, binding: AttemptBinding) -> AttemptStatus:
        """Record actual owner-validated terminal observation, even after stop.

        This is not a model's completion assertion, result commit or account
        release. A late terminal preserves the original timer/cancellation stop.
        """
        entry = self._entry(binding)
        status = self._observe(entry, "finish")
        entry.terminal_observed = True
        return AttemptStatus(status.binding, status.liveness, status.stop_reason, True, status.cleanup_confirmed)

    def observe_cleanup(self, binding: AttemptBinding) -> AttemptStatus:
        """Record actual tracked-tree cleanup already confirmed by trusted code.

        No client-supplied bool, signal/PID or cancellation request is evidence.
        This does not commit a result or return/reissue an account launch claim.
        """
        entry = self._entry(binding)
        status = self._observe(entry, "poll")
        entry.cleanup_confirmed = True
        return AttemptStatus(status.binding, status.liveness, status.stop_reason, status.terminal_observed, True)

    def tick(self) -> tuple[AttemptStatus, ...]:
        """Poll every bounded entry without persistence/auth/publication work.

        A bad clock on one attempt does not prevent polling the others. This
        internal tuple is not an unbounded or paginated external status reply.
        """
        self._owner()
        return tuple(self._observe(entry, "poll") for entry in self._attempts.values())

    def pending_stops(self) -> tuple[AttemptStatus, ...]:
        """Repeated stop observations remain pending until actual cleanup."""
        return tuple(status for status in self.tick() if status.stop_requested and not status.cleanup_confirmed)
