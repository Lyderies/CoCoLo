"""Owner-side bridge control, independent of executor callbacks and persistence.

This is internal composition, not a listener or native caller enrollment. The
trusted frontend must authorize control connections and validate/attribute native
activity before updating ProjectMonitor. Real-client event qualification, closed
descendant containment and production client migration remain separate gates.
"""
from __future__ import annotations

import sys

from .control_api import ControlError
from .control_loop import ControlLoop
from .owned_execution import ExecutionSupervisor
from .project_monitor import _capture_binding


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
        self._attempts[key] = (binding, supervisor)

    def _synchronize(self):
        first_error = None
        for binding, supervisor in tuple(self._attempts.values()):
            try:
                status = self._monitor.status(binding)
                observed = (supervisor.request_stop(status.stop_reason) if status.stop_requested
                            else supervisor.snapshot())
                if observed["root_waited"]:
                    self._monitor.observe_terminal(binding)
                if observed["cleanup_confirmed"]:
                    self._monitor.observe_cleanup(binding)
            except BaseException as error:
                # A failure for one attempt must not skip other latched stops.
                # Preserve the first interruption over later ordinary failures.
                if first_error is None or (isinstance(first_error, Exception)
                                           and not isinstance(error, Exception)):
                    first_error = error
        if first_error is not None:
            raise first_error

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
