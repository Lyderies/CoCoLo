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
from .process_tree import ProcessTree

_POPEN = subprocess.Popen
MAX_OWNED_EXECUTIONS = 64
EXECUTION_STOP_REASONS = frozenset({
    "cancel_requested", "startup_silence", "work_silence", "absolute_deadline", "monitor_unavailable",
})


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

    def begin_cleanup(self):
        self._registry.begin_cleanup(self)

    def stop_requested(self):
        return self._registry.stop_requested(self)

    def stop_reason(self):
        return self._registry.stop_reason(self)

    def activity_enabled(self):
        return self._registry.activity_enabled(self)

    def publish_activity(self, category):
        return self._registry.publish_activity(self, category)


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


class OwnedExecutionRegistry:
    def __init__(self):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._issued = weakref.WeakKeyDictionary()
        self._pending = {}
        self._keys = set()
        self._supervisors = weakref.WeakKeyDictionary()

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
            if entry.state not in {"cleaned", "released"}:
                entry.stop_requested = True
                entry.stop_reason = entry.stop_reason or reason
                if entry.activity is not None:
                    entry.activity.events.clear()
                self._signal_owned_group(entry)
            return self._supervisor_view(entry)

    def enable_activity(self, handle, clock_ns):
        with self._lock:
            entry = self._supervisor_entry(handle)
            if entry.activity is not None:
                if entry.activity.clock is not clock_ns:
                    raise ExecutionCustodyError()
                return
            if entry.state != "issued" or entry.thread.ident is not None:
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

    def _begin_cleanup(self, entry):
        if not entry.cleanup_started:
            try:
                if entry.transferred:
                    # Stop before relinquishing the unreaped group to the cleanup
                    # executor. A blocked tracker cannot delay this first stop.
                    self._signal_owned_group(entry)
            finally:
                entry.cleanup_started = True

    def begin_cleanup(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if entry.state == "launching":
                entry.state = "unknown"
            self._begin_cleanup(entry)

    def _abort_entry(self, entry):
        try:
            with self._lock:
                self._begin_cleanup(entry)
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
                    or not tree.identified or process.pid != tree.root or tree.root_birth is None):
                raise ExecutionCustodyError()
            entry.process, entry.tree, entry.birth = process, tree, tree.root_birth
            entry.state = "active"

    def spawn(self, ticket, arguments, **options):
        # The fixed runner supplies arguments/options. A caller cannot substitute
        # a Popen-like object or nominate an unrelated already-owned process.
        if options.get("start_new_session") is not True:
            raise ExecutionCustodyError()
        entry = self.enter(ticket)
        process = None
        try:
            process = _POPEN(arguments, **options)
            with self._lock:
                # Retain the actual process before any fallible metadata work.
                entry.process = process
                if entry.stop_requested:
                    self._signal_owned_group(entry)
                entry.tree = ProcessTree(process.pid)
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
            if (self._pending.get(ticket) is not entry or entry.state not in {"active", "unknown"}
                    or not entry.cleanup_started
                    or process is not entry.process or tree is not entry.tree or tree.root_birth != entry.birth):
                raise ExecutionCustodyError()
            entry.state = "unknown"
        # Kernel observations are outside the metadata lock and outside the
        # local timer/event-reader path. Failure/interrupt leaves unknown custody.
        confirmed = tree.cleanup_after_owned_wait(process)
        with self._lock:
            if confirmed:
                entry.state = "cleaned"
            return confirmed

    def key(self, ticket, *, cleaned=False):
        with self._lock:
            entry = self._entry(ticket, issuer=True)
            if cleaned and (self._pending.get(ticket) is not entry or entry.state != "cleaned"):
                raise ExecutionCustodyError()
            return entry.key

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
