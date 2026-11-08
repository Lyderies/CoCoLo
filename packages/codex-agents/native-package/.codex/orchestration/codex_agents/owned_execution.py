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
import threading
import time
import weakref

from .account_claim_contracts import _BRIDGE, _DIGEST, _PROJECT, _RUN, matches
from .process_tree import ProcessTree

_POPEN = subprocess.Popen
MAX_OWNED_EXECUTIONS = 64


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
        self.reason_code = reason if reason in {"execution_custody_unavailable", "execution_deadline_expired"} else "execution_custody_unavailable"
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


class OwnedExecutionRegistry:
    def __init__(self):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._issued = weakref.WeakKeyDictionary()
        self._pending = {}
        self._keys = set()

    def _entry(self, ticket):
        if self._pid != os.getpid() or type(ticket) is not ExecutionTicket or ticket._registry is not self:
            raise ExecutionCustodyError()
        entry = self._issued.get(ticket)
        if entry is None or entry.thread is not threading.current_thread():
            raise ExecutionCustodyError()
        return entry

    def issue(self, key, *, deadline_ns):
        if self._pid != os.getpid() or type(key) is not OwnedExecutionKey:
            raise ExecutionCustodyError()
        key.__post_init__()
        now = time.monotonic_ns()
        if type(deadline_ns) is not int or not 0 <= now < deadline_ns <= (1 << 63) - 1:
            raise ExecutionCustodyError()
        with self._lock:
            if len(self._pending) >= MAX_OWNED_EXECUTIONS or key in self._keys:
                raise ExecutionCustodyError()
            ticket = ExecutionTicket(self)
            entry = _Owned(key, threading.current_thread(), now, deadline_ns)
            self._issued[ticket] = self._pending[ticket] = entry
            self._keys.add(key)
            return ticket

    def enter(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if self._pending.get(ticket) is not entry or entry.state != "issued":
                raise ExecutionCustodyError()
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
                entry.tree = ProcessTree(process.pid)
                if (self._entry(ticket) is not entry or self._pending.get(ticket) is not entry
                        or entry.state != "launching"):
                    raise ExecutionCustodyError()
            return process
        except BaseException as original:
            entry.state = "unknown"
            if process is not None:
                entry.process = process
                try:
                    _abort_spawn(process, entry.tree)
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

    def finish(self, ticket, process, tree):
        with self._lock:
            entry = self._entry(ticket)
            if (self._pending.get(ticket) is not entry or entry.state not in {"active", "unknown"}
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
            entry = self._entry(ticket)
            if cleaned and (self._pending.get(ticket) is not entry or entry.state != "cleaned"):
                raise ExecutionCustodyError()
            return entry.key

    def acknowledge_release(self, ticket):
        with self._lock:
            entry = self._entry(ticket)
            if entry.state == "released":
                return
            if self._pending.get(ticket) is not entry or entry.state != "cleaned":
                raise ExecutionCustodyError()
            self._pending.pop(ticket)
            self._keys.remove(entry.key)
            entry.process = entry.tree = None
            entry.state = "released"
