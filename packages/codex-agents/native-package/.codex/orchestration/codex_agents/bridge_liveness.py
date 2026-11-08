"""Owner-side bridge control, independent of executor callbacks and persistence.

This is internal composition, not a listener or native caller enrollment. The
trusted frontend must authorize control connections and validate/attribute native
activity before updating ProjectMonitor. Real-client event qualification, closed
descendant containment and production client migration remain separate gates.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
import json
import weakref

from .control_api import ControlError
from .control_loop import ControlLoop
from .owned_execution import ExecutionSupervisor
from .project_monitor import _capture_binding


@dataclass(frozen=True, repr=False)
class CodexOwnerResult:
    """Immutable delivery data, not native/commit/cleanup/release authority."""
    packet: object
    stop_reason: str | None


class BridgeLivenessOwner:
    """One local control owner supervising bounded, separately owned executions.

    bind() accepts only a registry-issued handle retained by the original issuing
    thread after a one-time executor handoff. Timers, control I/O and the initial
    owned-group stop never wait for SQL, frame callbacks, process snapshots or
    worker joins. A stop request is not terminal/cleanup confirmation or an
    account-capacity release. Keep the existing detached absolute watchdog.
    """

    def __init__(self, loop: ControlLoop):
        if type(loop) is not ControlLoop:
            raise ControlError("control_invalid_input")
        loop._owner()
        self._loop = loop
        self._monitor = loop._monitor
        self._attempts = {}
        self._running = False
        self._results = weakref.WeakKeyDictionary()

    def _owner(self):
        self._loop._owner(closing=True)
        if self._running:
            raise ControlError("control_unavailable")

    def bind(self, binding, supervisor):
        """Capture the full registered binding and original absolute deadline.

        Labels alone and equal/copy-constructed handles cannot attach
        an execution. No replacement, eviction, timer rearm or relaunch occurs.
        """
        self._owner()
        self._loop._owner()
        binding = _capture_binding(binding)
        entry = self._monitor._entry(binding)
        if type(supervisor) is not ExecutionSupervisor:
            raise ControlError("control_invalid_input")
        if supervisor.target() != {
            "project_id": binding.project_id, "run_handle": binding.run_handle,
            "run_id": binding.run_id, "claim_id": binding.claim_id,
            "deadline_ns": entry.deadline,
        }:
            raise ControlError("control_invalid_input")
        key = (binding.run_handle, binding.attempt)
        previous = self._attempts.get(key)
        if previous is not None:
            if previous != (binding, supervisor):
                raise ControlError("control_invalid_input")
            return
        if len(self._attempts) >= self._monitor._maximum:
            raise ControlError("control_unavailable")
        self._monitor.bind_activity(binding, supervisor)
        self._attempts[key] = (binding, supervisor)

    def _synchronize(self):
        first_error = None
        for binding, supervisor in tuple(self._attempts.values()):
            try:
                status = self._monitor.status(binding)
                observed = (supervisor.request_stop(status.stop_reason) if status.stop_requested
                            else supervisor.snapshot())
                if (observed["worker_returned"] or (
                        observed["root_waited"] and not observed["completion_expected"])):
                    self._monitor.observe_terminal(binding)
                if observed["cleanup_confirmed"] and (
                        not observed["completion_expected"] or observed["worker_returned"]):
                    self._monitor.observe_cleanup(binding)
            except BaseException as error:
                # A failure for one attempt must not skip other latched stops.
                # Preserve the first interruption over later ordinary failures.
                if first_error is None or (isinstance(first_error, Exception)
                                           and not isinstance(error, Exception)):
                    first_error = error
        if first_error is not None:
            raise first_error

    def _worker(self, binding, pool, handle):
        from .codex_worker import CodexWorkerPool

        self._owner()
        binding = _capture_binding(binding)
        self._monitor._entry(binding)
        if type(pool) is not CodexWorkerPool:
            raise ControlError("control_invalid_input")
        supervisor = pool.supervisor(handle)
        if self._attempts.get((binding.run_handle, binding.attempt)) != (binding, supervisor):
            raise ControlError("control_invalid_input")
        return binding

    def result(self, binding, pool, handle):
        """Choose once from the actual fixed return and the serialized owner stop.

        Large result bytes are immutable references; only bounded metadata or a
        small fixed fence is encoded here. The persistence lane must separately
        authenticate this supervisor and bind the original assignment/claim.
        """
        from .bridge import _fenced_result
        from .codex_worker import CodexWorkerPacket
        from .learning_contracts import canonical_json

        binding = self._worker(binding, pool, handle)
        if handle in self._results:
            return self._results[handle]
        self._synchronize()
        packet = pool.packet(handle)
        if packet.error_code == "worker_result_discarded":
            raise ControlError("control_unavailable")
        if not pool.supervisor(handle).snapshot()["worker_returned"]:
            raise ControlError("control_unavailable")
        status = self._monitor.status(binding)
        if packet.body is not None and status.stop_reason is not None:
            reason = {"cancel_requested": "cancelled", "absolute_deadline": "timed_out"}.get(
                status.stop_reason, status.stop_reason)
            state = {"cancelled": "cancelled", "timed_out": "timed_out",
                     "startup_silence": "timed_out", "work_silence": "timed_out"}.get(reason, "failed")
            identity = packet.identity
            assignment = {"assignment_id": identity[0], "attempt": identity[1], "retry_of_assignment_id": identity[2]}
            body = canonical_json(_fenced_result(assignment, state, reason))
            report = json.loads(packet.report)
            report["outcome"] = reason
            validation_reason = packet.validation_reason if state == "failed" else None
            if validation_reason is None:
                report.pop("result_validation_reason_code", None)
            packet = CodexWorkerPacket(identity, body, canonical_json(report), True, packet.reviewed,
                                       state, validation_reason, None)
            pool.discard_candidate(handle)
        result = CodexOwnerResult(packet, status.stop_reason)
        self._results[handle] = result
        return result

    def discard_result(self, binding, pool, handle):
        """After durable delivery or orchestration closure, drop bytes only."""
        self._worker(binding, pool, handle)
        pool.discard_candidate(handle)
        self._results.pop(handle, None)

    def retire(self, binding, pool, handle):
        """Forget returned bytes only after the original execution was released."""
        self._worker(binding, pool, handle)
        pool.retire(handle)
        self._results.pop(handle, None)

    def run_once(self):
        """Drive bounded control I/O and apply latched stops on every exit path."""
        self._owner()
        self._running = True
        try:
            self._synchronize()
            return self._loop.run_once()
        finally:
            original = sys.exc_info()[1]
            try:
                self._synchronize()
            except BaseException:
                if original is not None and not isinstance(original, Exception):
                    raise original
                raise
            finally:
                self._running = False
