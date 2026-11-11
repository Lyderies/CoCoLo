"""Original project owners assembled for a trusted native bootstrap.

This component supplies no listener, enrollment, routing hint, permission or
native identity proof. A separately verified frontend exclusively transfers
connected sockets and independently authorized contexts. Every authorizer
defaults to denial; scope labels and callback results are not that proof.
"""
from dataclasses import dataclass, replace
import os
import threading
import time

from .bridge_liveness import BridgeLivenessOwner
from .codex_worker import CodexWorkerLimits, CodexWorkerPool
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .control_loop import ControlLoop, ControlLoopLimits
from .control_service import ControlService
from .history_contracts import HistoryError, valid_integer
from .liveness_clock import LivenessLimits
from .orchestration_api import OrchestrationScope
from .owned_execution import OwnedExecutionRegistry
from .prepared_dispatch import PreparedDispatchService
from .project_ingress import ProjectIngress, ProjectIngressLimits
from .project_monitor import MAX_TRACKED_ATTEMPTS, ProjectMonitor, SupervisorScope
from .project_request_queue import ProjectRequestLimits
from .result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceLane


@dataclass(frozen=True)
class ProjectRuntimeLimits:
    """Explicit trusted startup budgets, never values from a client request."""
    max_tracked_attempts: int
    max_persistence_entries: int
    persistence_bytes: int
    liveness: LivenessLimits
    control: ControlLoopLimits
    workers: CodexWorkerLimits
    requests: ProjectRequestLimits
    ingress: ProjectIngressLimits

    def __post_init__(self):
        if (not valid_integer(self.max_tracked_attempts, minimum=1, maximum=MAX_TRACKED_ATTEMPTS)
                or not valid_integer(self.max_persistence_entries, minimum=1, maximum=64)
                or not valid_integer(self.persistence_bytes, minimum=1, maximum=512*1_048_576)):
            raise HistoryError('history_invalid_input')
        for value, kind in ((self.liveness, LivenessLimits), (self.control, ControlLoopLimits),
                            (self.workers, CodexWorkerLimits), (self.requests, ProjectRequestLimits),
                            (self.ingress, ProjectIngressLimits)):
            if type(value) is not kind:
                raise HistoryError('history_invalid_input')
            value.__post_init__()
        if self.persistence_bytes < self.max_persistence_entries*MAX_PERSISTENCE_ENTRY_BYTES + self.requests.retained_bytes:
            raise HistoryError('history_invalid_input')


class ProjectRuntime:
    def __init__(self, scope, open_history, limits, *, control_authorizer=None, ingress_authorizer=None,
                 history_authorizer=None, result_authorizer=None, orchestration_authorizer=None,
                 clock_ns=time.monotonic_ns):
        if (type(scope) is not OrchestrationScope or type(limits) is not ProjectRuntimeLimits
                or scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST or not callable(open_history)
                or not callable(clock_ns) or any(v is not None and not callable(v) for v in
                    (control_authorizer, ingress_authorizer, history_authorizer, result_authorizer, orchestration_authorizer))):
            raise HistoryError('history_invalid_input')
        scope = OrchestrationScope(**scope.as_data())
        limits = replace(limits, **{name: replace(getattr(limits, name)) for name in
                                   ('liveness', 'control', 'workers', 'requests', 'ingress')})
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._closed = self._running = False
        self.control = self.loop = self.owner = self.pool = self.persistence = self.ingress = self.dispatch = None
        try:
            self.monitor = ProjectMonitor(SupervisorScope(**scope.as_data()), limits.liveness,
                max_tracked_attempts=limits.max_tracked_attempts, clock_ns=clock_ns)
            self.control = ControlService(self.monitor, authorizer=control_authorizer, clock_ns=clock_ns)
            self.loop = ControlLoop(self.monitor, self.control, limits.control)
            self.owner = BridgeLivenessOwner(self.loop)
            self.pool = CodexWorkerPool(OwnedExecutionRegistry(), limits.workers)
            self.persistence = ResultPersistenceLane(self.owner, self.pool, open_history,
                max_entries=limits.max_persistence_entries, retained_bytes=limits.persistence_bytes,
                request_limits=limits.requests, history_authorizer=history_authorizer,
                result_authorizer=result_authorizer, orchestration_authorizer=orchestration_authorizer,
                bootstrap_history=True)
            self.ingress = ProjectIngress(self.persistence, limits.ingress, authorizer=ingress_authorizer)
            self.ingress.bootstrap()
        except BaseException:
            try:
                self.request_close()
            except BaseException:
                pass  # All started resources were offered shutdown; preserve startup's original failure.
            raise

    def _owner(self, *, closing=False):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError('history_wrong_owner')
        if self._running or (self._closed and not closing):
            raise HistoryError('history_service_unavailable')

    def attach_project(self, connection, context):
        """Accept exclusive native-frontend custody, never JSON authorization."""
        self._owner()
        self.ingress.attach(connection, context)

    def attach_control(self, connection, context):
        self._owner()
        self.loop.attach(connection, context)

    def bind_dispatch(self, *, max_requests, steps_per_cycle, authorizer=None):
        self._owner()
        service = PreparedDispatchService(self.owner, self.pool, self.persistence,
            max_requests=max_requests, steps_per_cycle=steps_per_cycle, authorizer=authorizer)
        self.control.bind_dispatch(service)
        self.dispatch = service
        return service

    def run_once(self):
        self._owner()
        self._running = True
        try:
            self.owner.run_once()
            if self.dispatch is not None:
                # Original owner operations must run outside owner.run_once().
                self.dispatch.drive()
        finally:
            self._running = False

    def request_close(self):
        """Stop intake and request local shutdown; never wait or release account capacity."""
        self._owner(closing=True)
        if self._closed:
            return
        self._closed = True
        failure = None
        for component in (self.ingress, self.pool, self.persistence, self.control, self.loop):
            if component is not None:
                try:
                    component.close()
                except BaseException as error:
                    if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                        failure = error
        if failure is not None:
            raise failure

    def threads_stopped(self):
        """Observe owned thread exit only, not physical cleanup or account release."""
        self._owner(closing=True)
        return (self._closed and (self.ingress is None or not self.ingress._thread.is_alive())
            and (self.persistence is None or not self.persistence._writer.is_alive())
            and (self.pool is None or all(not slot.thread.is_alive() for slot in self.pool._slots)))
