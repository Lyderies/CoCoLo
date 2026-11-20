"""Owner-local custody for actual fixed-runner process completions.

This is not native caller enrollment or an RPC capability. A ledger consumes
only its own registry's original ticket and retained Popen/tree association.
Constructed keys, tickets, flags and process metadata alone prove no authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import weakref

from .account_claim_contracts import _BRIDGE, _DIGEST, _HANDLE, _PROJECT, _RUN, matches
from .execution_activity import ActivityBuffer, ActivityUnavailable
from .liveness_clock import LIVENESS_STOP_REASONS
from .process_tree import RUN_TAG, RUN_TAG_VARIABLE, ProcessTree, process_record

_POPEN = subprocess.Popen
MAX_OWNED_EXECUTIONS = 64
_KEY_FIELDS = ('project_id', 'handle_digest', 'run_id', 'bridge_run_id')
EXECUTION_STOP_REASONS = LIVENESS_STOP_REASONS


def _abort_spawn(process, tree):
    """Stop only this factory's unreaped, new-session child after a handoff fault.

    The owned Popen still reserves the group ID until wait. This is best-effort
    containment, never sufficient evidence to release a capacity reservation.
    """
    try:
        if tree is not None:
            tree.terminate()
    finally:
        try:
            if process.returncode is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
        finally:
            try:
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()


class ExecutionCustodyError(RuntimeError):
    def __init__(self, reason="execution_custody_unavailable"):
        self.reason_code = reason if reason in {
            "execution_custody_unavailable", "execution_deadline_expired", "execution_cancel_requested",
        } else "execution_custody_unavailable"
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class OwnedExecutionKey:
    project_id: str
    handle_digest: str
    run_id: str
    bridge_run_id: str

    def __post_init__(self):
        if not all(matches(value, pattern) for value, pattern in (
            (self.project_id, _PROJECT), (self.handle_digest, _DIGEST),
            (self.run_id, _RUN), (self.bridge_run_id, _BRIDGE),
        )):
            raise ExecutionCustodyError()


class ExecutionTicket:
    __slots__ = ("_registry", "__weakref__")

    def __init__(self, registry):
        self._registry = registry

    def enter(self):
        self._registry.enter(self)

    def spawn(self, arguments, **options):
        return self._registry.spawn(self, arguments, **options)

    def deadline_ns(self):
        return self._registry.deadline_ns(self)

    def bind(self, process, tree):
        self._registry.bind(self, process, tree)

    def tree(self, process):
        return self._registry.tree(self, process)

    def finish(self, process, tree):
        return self._registry.finish(self, process, tree)

    def abort_handoff(self):
        self._registry.abort_handoff(self)

    def begin_cleanup(self, reason="run_ended"):
        self._registry.begin_cleanup(self, reason)

    def stop_requested(self):
        return self._registry.stop_requested(self)

    def stop_reason(self):
        return self._registry.stop_reason(self)

    def activity_enabled(self):
        return self._registry.activity_enabled(self)

    def publish_activity(self, category):
        return self._registry.publish_activity(self, category)

    def fail_activity(self):
        self._registry.fail_activity(self)

    def watched(self, pid):
        """The detached watchdog acknowledged tracking this execution's tree."""
        self._registry.watched(self, pid)

    def watchdog_marker(self):
        """(path, bridge run id) for the watchdog's cleanup confirmation, or None without a journal."""
        return self._registry.watchdog_marker(self)

    def watchdog_container(self):
        """C3: the container the detached watchdog stops once its service has died, or None."""
        return self._registry.watchdog_container(self)

    def _signal_stop(self, process):
        self._registry._signal_stop(self, process)


class ExecutionSupervisor:
    """Original owner's opaque stop/observation handle, never an RPC grant.

    Only handoff() issues a registered instance. No caller-selected PID,
    signal, process object or cleanup flag is accepted.
    """
    __slots__ = ("_registry", "__weakref__")

    def __init__(self, registry):
        self._registry = registry

    def target(self):
        return self._registry.supervisor_target(self)

    def snapshot(self):
        return self._registry.supervisor_snapshot(self)

    def request_stop(self, reason="cancel_requested"):
        return self._registry.supervisor_stop(self, reason)

    def enable_activity(self, *, clock_ns=time.monotonic_ns):
        self._registry.enable_activity(self, clock_ns)

    def receive_activity(self):
        return self._registry.receive_activity(self)


class ExecutionCleanup:
    """Opaque original cleanup reference for one selected native reader.

    Registry membership, original worker return and current retained custody
    are checked on every read. This is not serializable, native peer identity,
    closed-descendant containment proof or permission to release account slots.
    """
    __slots__ = ("_registry", "__weakref__")

    def __init__(self, registry):
        self._registry = registry


@dataclass(frozen=True, repr=False)
class _CleanupReference:
    entry: object
    ticket: object
    pool: object
    worker: object
    job: object
    slot: object
    issuer: object
    consumer: object
    key: object
    identity: tuple
    deadline_ns: int
    run_handle: str


@dataclass
class _Owned:
    key: OwnedExecutionKey
    thread: object
    created_ns: int
    deadline_ns: int
    state: str = "issued"
    process: object = None
    tree: object = None
    birth: str | None = None
    issuer_thread: object = None
    run_handle: str | None = None
    transferred: bool = False
    stop_requested: bool = False
    stop_reason: str | None = None
    stop_signal_attempted: bool = False
    stop_signal_sent: bool = False
    cleanup_started: bool = False
    activity: object = None
    worker_pool: object = None
    worker_handle: object = None
    worker_returned: bool = False
    # C2: the run's started container (`container_launch.StartedContainer`), and whether its stop
    # found it gone (None: not stopped yet).
    container: object = None
    container_gone: bool | None = None
    # C3: a stop of the container was asked for on the launcher's helper thread; and whether the
    # local process that streamed its output was confirmed clean when it finished (a later proof
    # that the container is gone then confirms the run clean).
    container_stop_requested: bool = False
    local_confirmed: bool = False
    # O0c M1: set before anything that could create a child (the container's start, the process
    # factory) and cleared only when the factory provably left none. With it unset when the worker
    # returns, the execution never launched: nothing is left to clean up.
    child_possible: bool = False


class OwnedExecutionRegistry:
    def __init__(self):
        # A project service's launch journal (`launch_journal.LaunchJournal`), or None.
        # With one, a child is created only after its `spawning` entry is on disk,
        # and its identity and its watchdog's are recorded as soon as they are known.
        self.launch_journal = None
        # C2: a `container_launch.ContainerLauncher` while containers are switched on, else None.
        # With one, the child runs in a container recorded before its claim; the local process
        # this registry owns streams the container's output and ends with it.
        self.container_launch = None
        # C3: the service is closing (its worker pool's close): a cancel is then recorded as
        # `service_close` on the run's container.
        self.is_closing = False
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._issued = weakref.WeakKeyDictionary()
        self._pending = {}
        self._keys = set()
        self._supervisors = weakref.WeakKeyDictionary()
        self._worker_pools = weakref.WeakSet()
        self._cleanups = weakref.WeakKeyDictionary()
        self._cleanup_by_ticket = weakref.WeakKeyDictionary()

    def _entry(self, ticket, *, issuer=False):
        if self._pid != os.getpid() or type(ticket) is not ExecutionTicket or ticket._registry is not self:
            raise ExecutionCustodyError()
        entry = self._issued.get(ticket)
        current = threading.current_thread()
        if entry is None or (entry.thread is not current and not (issuer and entry.issuer_thread is current)):
            raise ExecutionCustodyError()
        return entry

    def issue(self, key, *, deadline_ns, run_handle=None):
        if self._pid != os.getpid() or type(key) is not OwnedExecutionKey:
            raise ExecutionCustodyError()
        key.__post_init__()
        if run_handle is not None and not matches(run_handle, _HANDLE):
            raise ExecutionCustodyError()
        now = time.monotonic_ns()
        if type(deadline_ns) is not int or not 0 <= now < deadline_ns <= (1 << 63) - 1:
            raise ExecutionCustodyError()
        with self._lock:
            if len(self._pending) >= MAX_OWNED_EXECUTIONS or key in self._keys:
                raise ExecutionCustodyError()
            ticket = ExecutionTicket(self)
            entry = _Owned(key, threading.current_thread(), now, deadline_ns,
                           issuer_thread=threading.current_thread(), run_handle=run_handle)
            self._issued[ticket] = self._pending[ticket] = entry
            self._keys.add(key)
            return ticket

    def handoff(self, ticket, worker):
        """One explicit transfer before entry to one not-yet-started thread.

        The original owner retains stop/metadata authority, not spawn/bind
        authority. This transfers local custody only; it authenticates no native
        caller and cannot recreate a lost/restarted execution.
        """
        with self._lock:
            entry = self._entry(ticket)
            if (self._pending.get(ticket) is not entry or entry.state != "issued"
                    or entry.transferred or entry.run_handle is None
                    or type(worker) is not threading.Thread or worker.ident is not None or worker.is_alive()):
                raise ExecutionCustodyError()
            handle = ExecutionSupervisor(self)
            self._supervisors[handle] = entry
            entry.thread, entry.transferred = worker, True
            return handle

    def _supervisor_entry(self, handle):
        if (self._pid != os.getpid() or type(handle) is not ExecutionSupervisor
                or handle._registry is not self):
            raise ExecutionCustodyError()
        entry = self._supervisors.get(handle)
        if entry is None or entry.issuer_thread is not threading.current_thread():
            raise ExecutionCustodyError()
        return entry

    @staticmethod
    def _supervisor_view(entry):
        process = entry.process
        return {"state": entry.state, "process_returned": process is not None,
                "root_waited": entry.state in {"cleaned", "released"}
                or (process is not None and type(process.returncode) is int),
                "stop_requested": entry.stop_requested, "stop_signal_sent": entry.stop_signal_sent,
                "stop_reason": entry.stop_reason,
                "completion_expected": entry.worker_pool is not None,
                "worker_returned": entry.worker_returned,
                "cleanup_confirmed": entry.state in {"cleaned", "released"}}

    def supervisor_target(self, handle):
        with self._lock:
            entry = self._supervisor_entry(handle)
            return {"project_id": entry.key.project_id, "run_handle": entry.run_handle,
                    "run_id": entry.key.run_id, "claim_id": entry.key.bridge_run_id,
                    "deadline_ns": entry.deadline_ns}

    def supervisor_snapshot(self, handle):
        with self._lock:
            return self._supervisor_view(self._supervisor_entry(handle))

    @staticmethod
    def _signal_owned_group(entry):
        # Called under the registry lock. The factory required a new session;
        # this actual Popen is unreaped and reserves its PID/group. The executor
        # must revoke eligibility with begin_cleanup BEFORE any wait/reap.
        process = entry.process
        if (entry.cleanup_started or entry.stop_signal_attempted or process is None
                or type(process) is not _POPEN or process.returncode is not None):
            return
        entry.stop_signal_attempted = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # No cleanup claim; the executor/watchdog still owns recovery.
        else:
            entry.stop_signal_sent = True

    def supervisor_stop(self, handle, reason):
        with self._lock:
            if type(reason) is not str or reason not in EXECUTION_STOP_REASONS:
                raise ExecutionCustodyError()
            entry = self._supervisor_entry(handle)
            self._request_stop(entry, reason)
            return self._supervisor_view(entry)

    def _request_stop(self, entry, reason):
        if entry.state not in {"cleaned", "released"} or (entry.worker_pool is not None and not entry.worker_returned):
            entry.stop_requested = True
            entry.stop_reason = entry.stop_reason or reason
            try:
                if entry.activity is not None and not entry.worker_returned:
                    entry.activity.stop(entry.stop_reason)
            except ActivityUnavailable:
                pass  # Failed timing never skips the actual owned stop.
            finally:
                try:
                    self._request_container_stop(entry)
                finally:
                    self._signal_owned_group(entry)

    def closing(self):
        """C3: the service is closing; its runs' containers are stopped as `service_close`."""
        with self._lock:
            self.is_closing = True

    def _container_reason(self, entry, fallback):
        reason = entry.stop_reason or fallback
        return "service_close" if self.is_closing and reason == "cancel_requested" else reason

    def _request_container_stop(self, entry):
        """C3, under the lock: the run's container is stopped on the launcher's helper thread, once."""
        launcher = self.container_launch
        if entry.container is None or launcher is None or entry.container_stop_requested:
            return
        entry.container_stop_requested = True
        try:
            launcher.request_stop(entry.container, self._container_reason(entry, "cancel_requested"),
                                  on_gone=lambda: self._container_proven_gone(entry))
        except Exception:
            pass  # The cleanup's own stop still runs, and only the OS's answer confirms anything.

    def _container_proven_gone(self, entry):
        """C3: the OS says the run's container is gone (now or after a retried stop)."""
        with self._lock:
            entry.container_gone = True
            if entry.state == "unknown" and entry.local_confirmed:
                entry.state = "cleaned"

    def _signal_stop(self, ticket, process):
        """Whole-process signal guard only; never an RPC/primary capability.

        The actual active bridge record supplies both objects. Main-thread
        signal delivery may differ from the executor/issuer, but it can stop
        only this registry's exact retained pair, under its pre-wait guard.
        """
        with self._lock:
            if (self._pid != os.getpid() or type(ticket) is not ExecutionTicket
                    or ticket._registry is not self):
                raise ExecutionCustodyError()
            entry = self._issued.get(ticket)
            if entry is None or entry.process is not process or type(process) is not _POPEN:
                raise ExecutionCustodyError()
            self._request_stop(entry, "cancel_requested")

    def enable_activity(self, handle, clock_ns):
        with self._lock:
            entry = self._supervisor_entry(handle)
            if entry.activity is not None:
                if entry.activity.clock is not clock_ns:
                    raise ExecutionCustodyError()
                return
            if (entry.state != "issued" or (entry.thread.ident is not None
                    and (entry.worker_pool is None or not entry.worker_pool._reserved(entry.worker_handle)))):
                raise ExecutionCustodyError()
            try:
                entry.activity = ActivityBuffer(clock_ns)
            except ActivityUnavailable:
                raise ExecutionCustodyError() from None

    def activity_enabled(self, ticket):
        with self._lock:
            return self._entry(ticket).activity is not None

    def publish_activity(self, ticket, category):
        with self._lock:
            entry = self._entry(ticket)
            if entry.activity is None or entry.stop_requested or entry.cleanup_started:
                return False
            if entry.state != "active":
                raise ExecutionCustodyError()
            try:
                entry.activity.publish(category)
            except ActivityUnavailable:
                raise ExecutionCustodyError() from None
            return True

    def receive_activity(self, handle):
        with self._lock:
            entry = self._supervisor_entry(handle)
            if entry.activity is None:
                raise ExecutionCustodyError()
            return entry.activity.receive()

    def fail_activity(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if entry.activity is not None:
                entry.activity.fail()

    def _register_worker_pool(self, pool):
        from .codex_worker import CodexWorkerPool

        if type(pool) is not CodexWorkerPool or pool._registry is not self:
            raise ExecutionCustodyError()
        pool._owner()
        with self._lock:
            self._worker_pools.add(pool)

    def _handoff_worker(self, ticket, pool, handle):
        with self._lock:
            entry = self._entry(ticket)
            if pool not in self._worker_pools:
                raise ExecutionCustodyError()
            job = pool._job(handle)
            if (job.ticket is not ticket or not pool._reserved(handle)
                    or self._pending.get(ticket) is not entry or entry.state != "issued"
                    or entry.transferred or entry.run_handle is None):
                raise ExecutionCustodyError()
            supervisor = ExecutionSupervisor(self)
            self._supervisors[supervisor] = entry
            entry.thread, entry.transferred = job.slot.thread, True
            entry.worker_pool, entry.worker_handle = pool, handle
            job.supervisor = supervisor

    def _register_probe_worker_pool(self, pool):
        from .billing_probe_worker import BillingProbeWorkers
        if type(pool) is not BillingProbeWorkers or pool._registry is not self:
            raise ExecutionCustodyError()
        pool._owner()
        with self._lock:
            self._worker_pools.add(pool)

    def _complete_probe_worker(self, pool, handle):
        from .billing_probe_worker import BillingProbePacket, BillingProbeWorkers
        with self._lock:
            if type(pool) is not BillingProbeWorkers or pool not in self._worker_pools:
                raise ExecutionCustodyError()
            job = pool._jobs.get(handle)
            if job is None or job.state != 'finishing' or type(job.packet) is not BillingProbePacket:
                raise ExecutionCustodyError()
            entry = self._entry(job.ticket)
            if (entry.worker_pool is not pool or entry.worker_handle is not handle
                    or entry.worker_returned or entry.activity is not None
                    or job.snapshot is None or tuple(getattr(entry.key, n) for n in _KEY_FIELDS) != job.snapshot[5]
                    or entry.run_handle != job.snapshot[6] or entry.deadline_ns > job.snapshot[7]):
                raise ExecutionCustodyError()
            entry.worker_returned = True

    def _complete_worker(self, pool, handle):
        from .codex_worker import CodexWorkerPool
        with self._lock:
            if type(pool) is not CodexWorkerPool or pool not in self._worker_pools:
                raise ExecutionCustodyError()
            job = pool._jobs.get(handle)
            if job is None or job.state != "finishing" or job.packet is None:
                raise ExecutionCustodyError()
            entry = self._entry(job.ticket)
            if entry.worker_pool is not pool or entry.worker_handle is not handle or entry.worker_returned:
                raise ExecutionCustodyError()
            entry.worker_returned = True
            self._never_launched(entry)
            if entry.activity is None:
                raise ExecutionCustodyError()
            try:
                entry.activity.complete()
            except ActivityUnavailable:
                pass  # Actual return remains known; failed timing fences acceptance.

    @staticmethod
    def _never_launched(entry):
        """O0c M1, under the lock, once the fixed worker returned: an execution that provably never
        launched a child (cancelled or past its deadline before the spawn, refused before or by the
        process factory with no child left, or never entered) is clean, so its normal release frees
        every slot it took. Its ticket is no longer usable: only the returned worker held it. A
        container's start, or any doubt, leaves it unknown."""
        if (entry.worker_returned and entry.state in {"issued", "unknown"} and not entry.child_possible
                and entry.process is None and entry.tree is None and entry.container is None):
            entry.cleanup_started = True
            entry.state = "cleaned"

    def _begin_cleanup(self, entry):
        if not entry.cleanup_started:
            try:
                if entry.transferred:
                    # Stop before relinquishing the unreaped group to the cleanup
                    # executor. A blocked tracker cannot delay this first stop.
                    self._signal_owned_group(entry)
            finally:
                entry.cleanup_started = True

    def begin_cleanup(self, ticket, reason="run_ended"):
        with self._lock:
            entry = self._entry(ticket)
            if entry.state == "launching":
                entry.state = "unknown"
            self._begin_cleanup(entry)
        self._stop_container(entry, reason)

    def _stop_container(self, entry, reason="run_ended"):
        """C2/C3: stop the run's container (outside the lock) through the launcher's one stop action,
        joining a stop already asked for; whether the OS says it is gone. A later proof (a retried
        stop) arrives through `_container_proven_gone`."""
        with self._lock:
            started, launcher = entry.container, self.container_launch
            if started is None or entry.container_gone is not None:
                return
            entry.container_gone = False  # Not proven gone unless the stop says so.
            reason = self._container_reason(entry, reason)
        try:
            gone = launcher.stop(started, reason, on_gone=lambda: self._container_proven_gone(entry)) is True
        except Exception:
            gone = False
        with self._lock:
            entry.container_gone = entry.container_gone or gone

    def _abort_entry(self, entry):
        try:
            with self._lock:
                self._begin_cleanup(entry)
            self._stop_container(entry, "launch_refused")
        finally:
            original = sys.exc_info()[1]
            try:
                if entry.process is not None:
                    _abort_spawn(entry.process, entry.tree)
            except BaseException:
                if original is not None and not isinstance(original, Exception):
                    raise original
                raise

    def stop_requested(self, ticket):
        with self._lock:
            return self._entry(ticket).stop_requested

    def stop_reason(self, ticket):
        with self._lock:
            return self._entry(ticket).stop_reason

    def enter(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if self._pending.get(ticket) is not entry or entry.state != "issued":
                raise ExecutionCustodyError()
            if entry.stop_requested:
                entry.state = "unknown"
                raise ExecutionCustodyError("execution_cancel_requested")
            now = time.monotonic_ns()
            if not entry.created_ns <= now < entry.deadline_ns:
                entry.state = "unknown"
                raise ExecutionCustodyError("execution_deadline_expired" if now >= entry.deadline_ns else "execution_custody_unavailable")
            # Set before Popen. An interruption before registration stays
            # uncertain; it does not permit a second launch or free capacity.
            entry.state = "launching"
            return entry

    def deadline_ns(self, ticket):
        with self._lock:
            return self._entry(ticket).deadline_ns

    def bind(self, ticket, process, tree):
        with self._lock:
            entry = self._entry(ticket)
            if (self._pending.get(ticket) is not entry or entry.state != "launching"
                    or type(process) is not _POPEN or type(tree) is not ProcessTree
                    or process is not entry.process
                    or tree is not entry.tree
                    or not tree.identified or process.pid != tree.root or tree.root_birth is None
                    or type(tree.run_tag) is not str or RUN_TAG.fullmatch(tree.run_tag) is None):
                raise ExecutionCustodyError()
            entry.process, entry.tree, entry.birth = process, tree, tree.root_birth
            entry.state = "active"
        journal = self.launch_journal
        if journal is not None and entry.container is None:
            # Outside the lock. A failure stops the child before any output is read.
            try:
                journal.launched(entry.key.bridge_run_id, process.pid, entry.birth, tree.run_tag)
            except Exception:
                raise ExecutionCustodyError() from None

    def watchdog_marker(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if self._pending.get(ticket) is not entry or entry.state != "active":
                raise ExecutionCustodyError()
        journal = self.launch_journal
        if journal is None or entry.container is not None:
            return None  # A container's end is asked of the OS, not confirmed by the local watchdog.
        try:
            return str(journal.marker_path(entry.key.bridge_run_id)), entry.key.bridge_run_id
        except Exception:
            raise ExecutionCustodyError() from None

    def watchdog_container(self, ticket):
        """C3: the launcher's watchdog configuration for this run's container, or None."""
        with self._lock:
            entry = self._entry(ticket)
            if self._pending.get(ticket) is not entry or entry.state != "active":
                raise ExecutionCustodyError()
            started, launcher = entry.container, self.container_launch
        if started is None or launcher is None:
            return None
        try:
            return launcher.watchdog_config(started)
        except Exception:
            raise ExecutionCustodyError() from None

    def watched(self, ticket, pid):
        with self._lock:
            entry = self._entry(ticket)
            if (self._pending.get(ticket) is not entry or entry.state != "active" or type(pid) is not int
                    or entry.process is None or pid == entry.process.pid):
                raise ExecutionCustodyError()
        journal = self.launch_journal
        if journal is None or entry.container is not None:
            return
        # The watchdog is this process's unreaped child, so its pid is still its own.
        record = process_record(pid)
        if record is None:
            raise ExecutionCustodyError()
        try:
            journal.watched(entry.key.bridge_run_id, pid, record[2])
        except Exception:
            raise ExecutionCustodyError() from None

    def spawn(self, ticket, arguments, **options):
        # The fixed runner supplies arguments/options. A caller cannot substitute
        # a Popen-like object or nominate an unrelated already-owned process.
        if options.get("start_new_session") is not True:
            raise ExecutionCustodyError()
        entry = self.enter(ticket)
        process = None
        try:
            launcher = self.container_launch
            if launcher is not None:
                with self._lock:
                    entry.child_possible = True  # A container may exist from here on.
                # C2: the child runs in its container, created only after its admitted intent was
                # read back, with its deadline set on it; `started` is on disk before this returns.
                # The local process streams the container's output and ends with it.
                environment = options.get("env")
                run_tag = environment.get(RUN_TAG_VARIABLE) if type(environment) is dict else None
                if type(run_tag) is not str or RUN_TAG.fullmatch(run_tag) is None:
                    raise ExecutionCustodyError()
                try:
                    started = launcher.start(entry.key.bridge_run_id, arguments, workspace=options.get("cwd"),
                                             run_tag=run_tag, deadline_ns=entry.deadline_ns)
                except Exception:
                    raise ExecutionCustodyError() from None
                with self._lock:
                    entry.container = started
                    if entry.stop_requested:
                        self._request_container_stop(entry)  # C3: asked for while it was being created
                arguments = launcher.attach_arguments(started)
                options = {**options, "env": {**os.environ, RUN_TAG_VARIABLE: run_tag}}
            journal = self.launch_journal
            if journal is not None and launcher is None:
                # Durably on disk before the child exists: a run whose entry never
                # reached `spawning` provably never started one.
                try:
                    journal.spawning(entry.key.bridge_run_id)
                except Exception:
                    raise ExecutionCustodyError() from None
            process = self._create(entry, arguments, options)
            with self._lock:
                # Retain the actual process before any fallible metadata work.
                entry.process = process
                if entry.stop_requested:
                    self._signal_owned_group(entry)
                # The marker the fixed runner put in the child's environment, kept with its tree.
                environment = options.get("env")
                entry.tree = ProcessTree(process.pid, environment.get(RUN_TAG_VARIABLE)
                                         if type(environment) is dict else None)
                if (self._entry(ticket) is not entry or self._pending.get(ticket) is not entry
                        or entry.state != "launching"):
                    raise ExecutionCustodyError()
            return process
        except BaseException as original:
            with self._lock:
                entry.state = "unknown"
                if process is not None:
                    entry.process = process
            try:
                self._abort_entry(entry)
            except BaseException as cleanup_error:
                # Preserve the original nonlocal exit; an ordinary error
                # must not swallow a new interruption during cleanup either.
                if isinstance(original, Exception) and not isinstance(cleanup_error, Exception):
                    raise
            raise

    def _create(self, entry, arguments, options):
        """The process factory, keeping what it knows when it fails (O0c M1).

        A factory error with no child created (refused before the fork, or the fork failed), or with
        its child already reaped (Popen waits for a child whose exec failed before raising), leaves
        no child: the execution can then be released as never launched. A child created and not
        reaped is retained, so the abort path stops and reaps it; an interruption keeps the doubt.
        """
        with self._lock:
            entry.child_possible = True
        process = _POPEN.__new__(_POPEN)
        try:
            process.__init__(arguments, **options)
        except BaseException as error:
            created = process._child_created
            if created and process.returncode is None:
                with self._lock:
                    entry.process = process  # Stopped and reaped by the abort path.
            elif (created or isinstance(error, Exception)) and self.container_launch is None:
                with self._lock:
                    entry.child_possible = False
            raise
        return process

    def tree(self, ticket, process):
        with self._lock:
            entry = self._entry(ticket)
            if entry.state != "launching" or process is not entry.process or entry.tree is None:
                raise ExecutionCustodyError()
            return entry.tree

    def abort_handoff(self, ticket):
        # The caller may have been interrupted before assigning the factory's
        # return value. Recover only our actual retained child, never a PID or
        # caller-supplied process. This path cannot produce cleanup authority.
        with self._lock:
            entry = self._entry(ticket)
            if self._pending.get(ticket) is not entry or entry.state not in {"launching", "unknown"}:
                raise ExecutionCustodyError()
            entry.state = "unknown"
            process = entry.process
            if process is not None and type(process) is not _POPEN:
                raise ExecutionCustodyError()
        self._abort_entry(entry)

    def finish(self, ticket, process, tree):
        with self._lock:
            entry = self._entry(ticket)
            # O0c M1: a child stopped before its tree was bound (a stop or a refusal between the spawn
            # and the bind) is confirmed by the same kernel evidence, against the birth its own tree saw.
            birth = (entry.birth if entry.birth is not None or entry.state != "unknown" or entry.container is not None
                     or type(tree) is not ProcessTree or not tree.identified else tree.root_birth)
            if (self._pending.get(ticket) is not entry or entry.state not in {"active", "unknown"}
                    or not entry.cleanup_started or birth is None
                    or process is not entry.process or tree is not entry.tree or tree.root_birth != birth):
                raise ExecutionCustodyError()
            entry.state = "unknown"
        # Kernel observations are outside the metadata lock and outside the
        # local timer/event-reader path. Failure/interrupt leaves unknown custody.
        confirmed = tree.cleanup_after_owned_wait(process)
        if entry.container is not None:
            # C2: a run in a container is clean only once the OS says its container is gone.
            self._stop_container(entry)
        with self._lock:
            if entry.container is not None:
                # C3: a later proof that the container is gone confirms it then.
                entry.local_confirmed = confirmed is True
                confirmed = confirmed and entry.container_gone is True
            if confirmed:
                entry.state = "cleaned"
            return confirmed

    def key(self, ticket, *, cleaned=False):
        with self._lock:
            entry = self._entry(ticket, issuer=True)
            if cleaned and (self._pending.get(ticket) is not entry or entry.state != "cleaned"):
                raise ExecutionCustodyError()
            return entry.key

    def retain_cleanup(self, ticket, pool, worker, *, consumer):
        """Retain actual issuer custody before handing facts to its writer.

        No SQL, clock, callbacks, process inspection or waiting occurs here.
        The fixed worker must already have returned after registry cleanup.
        A different reader cannot adopt an existing retained reference.
        """
        with self._lock:
            entry = self._entry(ticket, issuer=True)
            current = threading.current_thread()
            if (entry.issuer_thread is not current or pool not in self._worker_pools
                    or (consumer is not current and type(consumer) is not threading.Thread)
                    or not consumer.is_alive()):
                raise ExecutionCustodyError()
            reference = self._cleanup_by_ticket.get(ticket)
            previous = None if reference is None else reference()
            if previous is not None:
                existing = self._cleanups.get(previous)
                if existing is None or existing.consumer is not consumer:
                    raise ExecutionCustodyError()
                self._cleanup_reference(previous)
                return previous
            if len(self._cleanups) >= MAX_OWNED_EXECUTIONS:
                raise ExecutionCustodyError()
            job = pool._jobs.get(worker)
            if job is None:
                raise ExecutionCustodyError()
            proof = ExecutionCleanup(self)
            self._cleanups[proof] = _CleanupReference(entry, ticket, pool, worker, job, job.slot,
                current, consumer, entry.key, tuple(getattr(entry.key, name) for name in _KEY_FIELDS), entry.deadline_ns, entry.run_handle)
            try:
                self._cleanup_reference(proof)
            except BaseException:
                self._cleanups.pop(proof, None)
                raise
            self._cleanup_by_ticket[ticket] = weakref.ref(proof)
            return proof

    def _cleanup_reference(self, proof):
        if (self._pid != os.getpid() or type(proof) is not ExecutionCleanup or proof._registry is not self):
            raise ExecutionCustodyError()
        reference = self._cleanups.get(proof)
        current = threading.current_thread()
        if (reference is None or current not in (reference.issuer, reference.consumer)
                or not reference.issuer.is_alive() or not reference.consumer.is_alive()):
            raise ExecutionCustodyError()
        entry, pool = reference.entry, reference.pool
        job = pool._jobs.get(reference.worker)
        if (self._issued.get(reference.ticket) is not entry or self._pending.get(reference.ticket) is not entry
                or entry.issuer_thread is not reference.issuer or entry.state != 'cleaned' or entry.worker_returned is not True
                or entry.worker_pool is not pool or entry.worker_handle is not reference.worker
                or pool not in self._worker_pools or pool._closed or job is not reference.job
                or job.state != 'returned' or job.ticket is not reference.ticket or job.slot is not reference.slot
                or job.slot.current is not reference.worker or job.slot.fault or not job.slot.thread.is_alive()
                or entry.key is not reference.key or tuple(getattr(entry.key, name) for name in _KEY_FIELDS) != reference.identity
                or entry.deadline_ns != reference.deadline_ns or entry.run_handle != reference.run_handle):
            raise ExecutionCustodyError()
        return reference

    def cleanup_snapshot(self, proof):
        """Fresh original facts only; a copied snapshot carries no custody."""
        with self._lock:
            reference = self._cleanup_reference(proof)
            return {'key': OwnedExecutionKey(*reference.identity), 'deadline_ns': reference.deadline_ns}

    def acknowledge_release(self, ticket):
        with self._lock:
            entry = self._entry(ticket, issuer=True)
            if entry.state == "released":
                return
            if self._pending.get(ticket) is not entry or entry.state != "cleaned":
                raise ExecutionCustodyError()
            self._pending.pop(ticket)
            self._keys.remove(entry.key)
            entry.process = entry.tree = None
            entry.state = "released"
            reference = self._cleanup_by_ticket.pop(ticket, None)
            proof = None if reference is None else reference()
            if proof is not None:
                self._cleanups.pop(proof, None)
