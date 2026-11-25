"""E1b-3/4/5/6/7: bounded custody and fair scheduling for a future ledger owner.

Trusted same-process callers submit whole operations; the owner calls
execute_next(). This module creates no owner thread, opens no ledger and grants
no authority. Project labels select scheduling shares, not permissions. Existing
collector/account paths are deliberately not wired here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import re
import threading
import time
from typing import Any

from .ledger_waits import LEDGER_REQUEST_WAIT_MS
from .service_queue import MAX_PENDING_ITEMS, MAX_PROJECTS, ProjectFairQueue, QueueLimits

_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_BUSY_REASONS = frozenset({"account_busy", "open_account_busy", "operation_rejected"})
MAX_REFUSAL_CALLERS = 16  # Includes the reserved aggregate bucket, "other".


@dataclass(frozen=True)
class LedgerQueueLimits:
    requests: int
    projects: int
    per_project: int
    max_wait_ns: int

    def __post_init__(self) -> None:
        if (
            type(self.requests) is not int or not 1 <= self.requests <= MAX_PENDING_ITEMS
            or type(self.projects) is not int or not 1 <= self.projects <= MAX_PROJECTS
            or type(self.per_project) is not int or not 1 <= self.per_project <= MAX_PENDING_ITEMS
            or self.projects * self.per_project > MAX_PENDING_ITEMS
            or type(self.max_wait_ns) is not int
            or not 1 <= self.max_wait_ns <= LEDGER_REQUEST_WAIT_MS * 1_000_000
        ):
            raise ValueError("invalid ledger queue limits")


# Provisional conservative sizes, pending E0b-v2; not measured production values.
DEFAULT_LEDGER_QUEUE_LIMITS = LedgerQueueLimits(16, 8, 2, LEDGER_REQUEST_WAIT_MS * 1_000_000)


class LedgerQueueBusy(RuntimeError):
    """The existing busy answer selected by the trusted calling adapter."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class LedgerRequestHandle:
    """Caller-owned result and handled acknowledgement; not a publication grant.

    Only wait/abandon and observation of handled are supported. In particular,
    callers must not set/clear the acknowledgement or mutate private fields.
    Completed handles are not retained by the queue.
    """

    def __init__(self, queue_key: object, project: str, operation: Callable[[], Any],
                 caller: str, busy_reason: str, deadline_ns: int) -> None:
        self._queue_key = queue_key
        self._identity = self
        self._project = project
        self._operation: Callable[[], Any] | None = operation
        self._caller = caller
        self._busy_reason = busy_reason
        self._deadline_ns = deadline_ns
        self._state = "queued"
        self._abandoned = False
        self._refusal: str | None = None
        self._response: Any = None
        self._failed = False
        self._ready = threading.Event()
        self.handled = threading.Event()


class LedgerOwnerQueue:
    """Finite queued/running custody with FIFO per project and fair turns.

    Limits count retained callback references, not arbitrary payload bytes.
    Inputs and caller labels must come from trusted internal adapters. No lock
    spans an operation or completion wait. A running operation is never canceled
    by caller expiry/abandonment; queued abandoned/expired work is skipped by the
    owner and retains its slot until that acknowledgement.
    """

    def __init__(self, limits: LedgerQueueLimits = DEFAULT_LEDGER_QUEUE_LIMITS, *,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if type(limits) is not LedgerQueueLimits or not callable(clock_ns):
            raise ValueError("invalid ledger queue configuration")
        limits.__post_init__()
        self._limits = limits
        self._clock_ns = clock_ns
        self._pid = os.getpid()
        self._key = object()
        self._lock = threading.Lock()
        self._work = threading.Condition(self._lock)
        self._drained = threading.Event()
        self._drained.set()
        self._closed = False
        self._pending = ProjectFairQueue(QueueLimits(limits.projects, limits.per_project, 8))
        self._jobs: dict[bytes, LedgerRequestHandle] = {}
        self._projects: dict[str, int] = {}
        self._refusals: dict[str, dict[str, int]] = {}
        self._serial = 0
        self._owner: threading.Thread | None = None
        self._executing = False

    def _guard(self) -> None:
        if self._pid != os.getpid():
            raise RuntimeError("ledger queue wrong process owner")

    def _handle(self, handle: LedgerRequestHandle) -> LedgerRequestHandle:
        if (type(handle) is not LedgerRequestHandle or handle._queue_key is not self._key
                or handle._identity is not handle):
            raise ValueError("foreign ledger request handle")
        return handle

    def _count(self, caller: str, cause: str) -> None:
        if (caller not in self._refusals and caller != "other"
                and len(self._refusals) - ("other" in self._refusals) >= MAX_REFUSAL_CALLERS - 1):
            caller = "other"
        counts = self._refusals.setdefault(caller, {})
        counts[cause] = counts.get(cause, 0) + 1

    def _refuse(self, handle: LedgerRequestHandle, cause: str) -> None:
        if handle._refusal is None:
            handle._refusal = cause
            self._count(handle._caller, cause)
        handle._abandoned = True
        handle._response = None
        if handle._state == "queued":
            handle._operation = None
        handle._ready.set()

    def offer(self, project_id: str, operation: Callable[[], Any], *, caller: str,
              busy_reason: str, deadline_ns: int) -> LedgerRequestHandle:
        """Admit immediately or raise a named, counted busy refusal.

        The effective deadline is min(caller deadline, admission + max_wait).
        Callback authority and payload size must be checked by the future adapter.
        """
        self._guard()
        if (type(project_id) is not str or _PROJECT.fullmatch(project_id) is None
                or not callable(operation) or type(caller) is not str or not caller
                or len(caller) > 128 or type(busy_reason) is not str or busy_reason not in _BUSY_REASONS
                or type(deadline_ns) is not int or deadline_ns <= 0):
            raise ValueError("invalid ledger queue request")
        with self._lock:
            now = self._clock_ns()
            cause = None
            if self._closed:
                cause = "closed"
            elif deadline_ns <= now:
                cause = "deadline"
            elif len(self._jobs) >= self._limits.requests:
                cause = "queue_full"
            elif self._projects.get(project_id, 0) >= self._limits.per_project:
                cause = "project_full"
            elif project_id not in self._projects and len(self._projects) >= self._limits.projects:
                cause = "project_limit"
            if cause is not None:
                self._count(caller, cause)
                raise LedgerQueueBusy(busy_reason)
            handle = LedgerRequestHandle(self._key, project_id, operation, caller, busy_reason,
                                        min(deadline_ns, now + self._limits.max_wait_ns))
            # Fixed-size scheduling tokens avoid retaining callbacks in two queues.
            # Reuse tokens only once no earlier request remains in custody.
            if not self._jobs:
                self._serial = 0
            self._serial += 1
            token = self._serial.to_bytes(8, "big")
            self._pending.offer(project_id, token)
            self._jobs[token] = handle
            self._projects[project_id] = self._projects.get(project_id, 0) + 1
            self._drained.clear()
            self._work.notify()
            return handle

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if type(timeout) not in (int, float) or not 0 <= timeout <= threading.TIMEOUT_MAX:
            raise ValueError("invalid ledger queue timeout")

    def wait_for_work(self, timeout: float) -> bool:
        """Wait without polling; closure/timeout returns False. Does not bind owner."""
        self._guard()
        self._validate_timeout(timeout)
        with self._work:
            self._work.wait_for(
                lambda: self._closed or self._pending.snapshot().pending > 0, timeout)
            return not self._closed and self._pending.snapshot().pending > 0

    def _acknowledge(self, token: bytes, handle: LedgerRequestHandle) -> None:
        """Publish completion and release custody; caller holds the queue lock."""
        handle._state = "handled"
        del self._jobs[token]
        remaining = self._projects[handle._project] - 1
        if remaining:
            self._projects[handle._project] = remaining
        else:
            del self._projects[handle._project]
        handle._ready.set()
        handle.handled.set()
        if not self._jobs:
            self._drained.set()

    def _close_pending(self) -> None:
        """Refuse at most limits.requests queued entries, with the lock held."""
        while (item := self._pending.take_nowait()) is not None:
            handle = self._jobs[item.payload]
            self._refuse(handle, "closed")
            self._acknowledge(item.payload, handle)

    def close(self) -> None:
        """Stop admission without waiting; finish running work before queued refusals.

        Idempotent and bounded by retained capacity. Does not bind the owner.
        A blocked callback remains the owner's responsibility; use drain(timeout)
        to await its completion within a bounded caller wait.
        """
        self._guard()
        with self._lock:
            self._closed = True
            self._work.notify_all()
            if not self._executing:
                self._close_pending()

    def drain(self, timeout: float) -> bool:
        """Close and wait up to timeout seconds for every retained acknowledgement."""
        self._guard()
        self._validate_timeout(timeout)
        self.close()
        return self._drained.wait(timeout)

    def execute_next(self) -> bool:
        """Owner-only nonblocking pump; True means one request was handled.

        First call, including an empty call, permanently binds the owner thread.
        Operation Exceptions become plain error results; BaseExceptions propagate
        only here, after releasing custody and acknowledging completion.
        """
        self._guard()
        with self._lock:
            thread = threading.current_thread()
            if self._owner is None:
                self._owner = thread
            if self._owner is not thread or self._executing:
                raise RuntimeError("ledger queue wrong or reentrant owner")
            item = self._pending.take_nowait()
            if item is None:
                return False
            handle = self._jobs[item.payload]
            self._executing = True
            if self._clock_ns() >= handle._deadline_ns:
                self._refuse(handle, "deadline")
            operation = None if handle._abandoned else handle._operation
            handle._operation = None
            handle._state = "running"
        response = None
        completed = False
        try:
            if operation is not None:
                response = operation()
            completed = True
        except Exception:
            # Never retain an exception/traceback or raise it on a second thread.
            pass
        finally:
            with self._lock:
                if self._clock_ns() >= handle._deadline_ns:
                    self._refuse(handle, "deadline")
                if not handle._abandoned:
                    handle._response = response
                    handle._failed = not completed
                self._executing = False
                self._acknowledge(item.payload, handle)
                if self._closed:
                    self._close_pending()
        return True

    def wait(self, handle: LedgerRequestHandle) -> Any:
        """Wait at most the request's remaining deadline, then return or refuse."""
        self._guard()
        with self._lock:
            handle = self._handle(handle)
            remaining = handle._deadline_ns - self._clock_ns()
            if remaining <= 0:
                self._refuse(handle, "deadline")
            elif handle._abandoned:
                self._refuse(handle, "abandoned")
            if handle._refusal is not None:
                raise LedgerQueueBusy(handle._busy_reason)
        # Event.wait is the only completion wait, with the caller's remaining budget.
        ready = handle._ready.wait(remaining / 1_000_000_000)
        with self._lock:
            if not ready or self._clock_ns() >= handle._deadline_ns:
                self._refuse(handle, "deadline")
            elif handle._abandoned:
                self._refuse(handle, "abandoned")
            if handle._refusal is not None:
                raise LedgerQueueBusy(handle._busy_reason)
            if handle._failed:
                return {"ok": False, "reason_code": "operation_rejected"}
            return handle._response

    def abandon(self, handle: LedgerRequestHandle) -> None:
        """Discard caller interest; never release queued or running custody."""
        self._guard()
        with self._lock:
            handle = self._handle(handle)
            handle._abandoned = True
            handle._response = None
            if handle._state == "queued":
                handle._operation = None
            handle._ready.set()

    def snapshot(self) -> dict[str, Any]:
        """Counters only; independent of a running operation and safe to mutate."""
        self._guard()
        with self._lock:
            return {"retained": len(self._jobs), "projects": len(self._projects),
                    "maximum": self._limits.requests,
                    "refusals": {caller: dict(counts) for caller, counts in self._refusals.items()}}
