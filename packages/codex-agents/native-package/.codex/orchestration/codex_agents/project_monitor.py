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
    "monitor_unavailable",
})
_BINDING_FIELDS = tuple(AttemptBinding.__dataclass_fields__)
_SCOPE_FIELDS = ("project_id", "primary_session_id", "runtime_digest",
                 "supervisor_instance_id", "supervisor_generation")
_LIMIT_FIELDS = ("startup_ns", "quiet_ns", "stale_ns", "silence_ns")
_SNAPSHOT_FIELDS = tuple(LivenessSnapshot.__dataclass_fields__)


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
    Known cleanup reports cleaned health with no live liveness snapshot. The
    first actual terminal still classifies the original bounds after cleanup;
    neither cleanup nor a null stop reason proves a successful/accepted result.
    """

    binding: AttemptBinding
    liveness: LivenessSnapshot | None
    stop_reason: str | None
    terminal_observed: bool
    cleanup_confirmed: bool

    @property
    def health(self) -> str:
        if self.cleanup_confirmed:
            return "cleaned"
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
    decision: tuple | None = None
    stop_reason: str | None = None
    terminal_observed: bool = False
    cleanup_confirmed: bool = False
    activity_source: object = None
    monitor_failed: bool = False


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
        self._last_clock_sample: int | None = None
        self._clock_failed = False
        self._sampling_clock = False
        self._source_sample = None
        self._loading_activity = False
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

    def scope(self) -> SupervisorScope:
        """Return captured labels to trusted control code, never caller proof."""
        self._owner()
        return SupervisorScope(**dict(zip(_SCOPE_FIELDS, self._scope, strict=True)))

    def lookup(self, target: ServiceTarget) -> AttemptBinding:
        """Resolve an exact already registered target after service authorization."""
        self._owner()
        invalid = False
        try:
            if type(target) is not ServiceTarget:
                raise ServiceProtocolError("invalid_target")
            target = ServiceTarget(target.orchestration_handle, target.run_handle, target.attempt)
            if target.run_handle is None:
                raise ServiceProtocolError("invalid_target")
        except (ServiceProtocolError, AttributeError):
            invalid = True
        if invalid:
            raise ProjectMonitorError("monitor_invalid_input")
        entry = self._attempts.get((target.run_handle, target.attempt))
        if entry is None:
            raise ProjectMonitorError("monitor_attempt_not_found")
        binding = self._binding(AttemptBinding(**dict(zip(_BINDING_FIELDS, entry.binding, strict=True))))
        if binding.orchestration_handle != target.orchestration_handle:
            raise ProjectMonitorError("monitor_wrong_scope")
        return binding

    def _sample_clock(self) -> int:
        """One shared failure latch and monotonic order for every attempt.

        Preserve nonlocal interrupts. Their failure latch must cover other
        attempts too, including registrations made after the failed sample.
        """
        self._owner()
        if self._clock_failed or self._sampling_clock:
            self._clock_failed = True
            raise LivenessClockError("liveness_invalid_clock")
        self._sampling_clock = True
        try:
            try:
                value = self._clock() if self._source_sample is None else self._source_sample
            except Exception:
                invalid = True
            else:
                invalid = (
                    self._clock_failed or type(value) is not int or not 0 <= value <= MAX_CLOCK_NS
                    or (self._last_clock_sample is not None and value < self._last_clock_sample)
                )
            if invalid:
                self._clock_failed = True
                raise LivenessClockError("liveness_invalid_clock")
            self._last_clock_sample = value
            return value
        except BaseException:
            self._clock_failed = True
            raise
        finally:
            self._sampling_clock = False

    def bind_activity(self, binding: AttemptBinding, source) -> None:
        """Attach an actual original-owner receiver before its worker starts.

        The buffer clock is the same trusted bounded source as this monitor.
        Internal custody labels/handles are not native enrollment or RPC grants.
        """
        from .owned_execution import ExecutionSupervisor

        entry = self._entry(binding)
        if type(source) is not ExecutionSupervisor:
            raise ProjectMonitorError("monitor_invalid_input")
        if source.target() != {
            "project_id": binding.project_id, "run_handle": binding.run_handle,
            "run_id": binding.run_id, "claim_id": binding.claim_id, "deadline_ns": entry.deadline,
        }:
            raise ProjectMonitorError("monitor_binding_conflict")
        if entry.activity_source is not None:
            if entry.activity_source is not source:
                raise ProjectMonitorError("monitor_binding_conflict")
            return
        if entry.decision is not None or entry.cleanup_confirmed or entry.monitor_failed or self._clock_failed:
            raise ProjectMonitorError("monitor_unavailable")
        source.enable_activity(clock_ns=self._clock)
        entry.activity_source = source

    def _observe(
        self, entry: _Attempt, operation: str, *, final_cleanup_poll: bool = False,
        final_terminal_poll: bool = False,
    ) -> AttemptStatus:
        self._owner()
        if self._loading_activity:
            self._clock_failed = True
            raise LivenessClockError("liveness_invalid_clock")
        if (entry.activity_source is None or entry.decision is not None or entry.monitor_failed or self._clock_failed
                or (entry.cleanup_confirmed and not (final_cleanup_poll or final_terminal_poll))):
            return self._observe_clock(entry, operation, final_cleanup_poll=final_cleanup_poll,
                                       final_terminal_poll=final_terminal_poll)
        self._loading_activity = True
        try:
            batch = entry.activity_source.receive_activity()
            if batch.failed or self._clock_failed:
                if batch.failure_code == "activity_clock_unavailable":
                    self._clock_failed = True
                raise LivenessClockError("liveness_invalid_clock")
            self._source_sample = batch.observed_ns
            entry.clock.received(batch.events)
            return self._observe_clock(entry, operation, final_cleanup_poll=final_cleanup_poll,
                                       final_terminal_poll=final_terminal_poll)
        except Exception:
            # Broken/overflowed receipt streams never become a healthy label.
            entry.monitor_failed = True
            entry.stop_reason = entry.stop_reason or "monitor_unavailable"
            return self._observe_clock(entry, "poll", final_cleanup_poll=final_cleanup_poll,
                                       final_terminal_poll=final_terminal_poll)
        except BaseException:
            self._clock_failed = True
            entry.stop_reason = entry.stop_reason or "monitor_unavailable"
            raise
        finally:
            self._source_sample = None
            self._loading_activity = False

    def _observe_clock(
        self, entry: _Attempt, operation: str, *, final_cleanup_poll: bool = False,
        final_terminal_poll: bool = False,
    ) -> AttemptStatus:
        self._owner()
        snapshot = None
        if entry.decision is not None:
            # Cache scalar values, never an outward mutable dataclass reference.
            snapshot = LivenessSnapshot(**dict(zip(_SNAPSHOT_FIELDS, entry.decision, strict=True)))
        elif entry.cleanup_confirmed and not (final_cleanup_poll or final_terminal_poll):
            # No later observation can reactivate a confirmed cleaned tree.
            pass
        elif self._clock_failed or entry.monitor_failed:
            entry.stop_reason = entry.stop_reason or "monitor_unavailable"
        else:
            try:
                snapshot = getattr(entry.clock, operation)()
            except LivenessClockError as exc:
                if exc.reason_code != "liveness_invalid_clock":
                    raise
                self._clock_failed = True
                entry.stop_reason = entry.stop_reason or "monitor_unavailable"
            except BaseException:
                if self._clock_failed:
                    entry.stop_reason = entry.stop_reason or "monitor_unavailable"
                raise
            if snapshot is not None:
                if snapshot.stop_reason is not None:
                    entry.stop_reason = snapshot.stop_reason
                if snapshot.stop_requested or snapshot.terminal:
                    entry.decision = tuple(getattr(snapshot, field) for field in _SNAPSHOT_FIELDS)
        # Keep historical terminal/stop decisions, never a healthy snapshot
        # after shared-clock failure. Known cleanup has its own final health;
        # it does not fabricate a completed result or a cancellation reason.
        return AttemptStatus(
            AttemptBinding(**dict(zip(_BINDING_FIELDS, entry.binding, strict=True))),
            None if entry.cleanup_confirmed else snapshot, entry.stop_reason,
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
        if self._clock_failed:
            raise ProjectMonitorError("monitor_unavailable")
        if len(self._attempts) >= self._maximum:
            raise ProjectMonitorError("monitor_capacity_exceeded")
        unavailable = False
        try:
            timer = LivenessClock(effective, hard_deadline_ns=hard_deadline_ns, clock_ns=self._sample_clock)
        except LivenessClockError as exc:
            if exc.reason_code != "liveness_invalid_clock":
                raise
            unavailable = True
        if unavailable:
            raise ProjectMonitorError("monitor_unavailable")
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
        first = not entry.terminal_observed
        entry.terminal_observed = True
        # Cleanup ends live activity, not the original result deadline. The
        # first actual terminal still classifies that bound if no decision was
        # already latched. Later terminal repeats never read the clock again.
        return self._observe(entry, "finish", final_terminal_poll=first)

    def observe_cleanup(self, binding: AttemptBinding) -> AttemptStatus:
        """Record actual tracked-tree cleanup already confirmed by trusted code.

        No client-supplied bool, signal/PID or cancellation request is evidence.
        This does not commit a result or return/reissue an account launch claim.
        """
        entry = self._entry(binding)
        first = not entry.cleanup_confirmed
        entry.cleanup_confirmed = True
        return self._observe(entry, "poll", final_cleanup_poll=first)

    def _poll_entries(self, entries: tuple[_Attempt, ...]) -> tuple[AttemptStatus, ...]:
        failed_before = self._clock_failed
        statuses = tuple(self._observe(entry, "poll") for entry in entries)
        if self._clock_failed and not failed_before:
            return tuple(self._observe(entry, "poll") for entry in entries)
        return statuses

    def orchestration_status(self, orchestration_handle: str) -> tuple[AttemptStatus, ...]:
        """Bounded internal observations for one already authorized orchestration."""
        self._owner()
        invalid = False
        try:
            ServiceTarget(orchestration_handle, None, None)
        except ServiceProtocolError:
            invalid = True
        if invalid:
            raise ProjectMonitorError("monitor_invalid_input")
        entries = tuple(entry for entry in self._attempts.values()
                        if entry.binding[_BINDING_FIELDS.index("orchestration_handle")] == orchestration_handle)
        return self._poll_entries(entries)

    def tick(self) -> tuple[AttemptStatus, ...]:
        """Poll every bounded entry without persistence/auth/publication work.

        Shared-clock failure produces unavailable metadata for other undecided
        attempts too. Nonlocal interrupts propagate with that failure latched. This
        internal tuple is not an unbounded or paginated external status reply.
        """
        self._owner()
        # A later failed sample invalidates earlier undecided rows in this same
        # batch; rebuild without reads, preserving known decisions and cleanup.
        return self._poll_entries(tuple(self._attempts.values()))

    def pending_stops(self) -> tuple[AttemptStatus, ...]:
        """Repeated stop observations remain pending until actual cleanup."""
        return tuple(status for status in self.tick() if status.stop_requested and not status.cleanup_confirmed)

    def pending_cleanup(self) -> tuple[AttemptStatus, ...]:
        """Include normal terminal observations without inventing cancellation."""
        return tuple(status for status in self.tick()
                     if (status.stop_requested or status.terminal_observed) and not status.cleanup_confirmed)
