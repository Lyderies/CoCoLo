"""Bounded frontend-to-original-project-writer requests, with no listener.

Only trusted startup/frontends call this adapter. A plain opaque context token
has no authority by itself: HistoryService independently authorizes each closed
business request. No SQL, callback selected by JSON, automatic retry or native
enrollment is supplied by this queue.

take() returns internal bytes to the trusted same-process frontend. That frontend
must independently revalidate native authority before actual socket publication;
this internal handoff is not a publication grant or an enabled native endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time

from .history_contracts import HistoryError
from .history_service import HistoryService
from .service_transport import MAX_FRAME_BYTES

MAX_REQUEST_SLOTS = 64
MAX_REQUEST_WAIT_NS = 60_000_000_000
REQUEST_SLOT_BYTES = 2 * MAX_FRAME_BYTES + 1024


@dataclass(frozen=True)
class ProjectRequestLimits:
    requests: int
    max_wait_ns: int

    def __post_init__(self):
        if (type(self.requests) is not int or not 1 <= self.requests <= MAX_REQUEST_SLOTS
                or type(self.max_wait_ns) is not int or not 1 <= self.max_wait_ns <= MAX_REQUEST_WAIT_NS):
            raise HistoryError("history_invalid_input")

    @property
    def retained_bytes(self):
        return self.requests * REQUEST_SLOT_BYTES


class ProjectRequestHandle:
    __slots__ = ()


@dataclass(repr=False)
class _Request:
    raw: bytes | None
    context: object
    deadline_ns: int
    state: str = "queued"
    response: bytes | None = None
    abandoned: bool = False


class ProjectRequestQueue:
    """Finite request custody; only execute() belongs on the project writer.

    Intake/take/abandon/close do no I/O, SQL or waiting for completion. Queued and
    executing abandoned requests retain their quota until the writer drains
    them, so disconnects cannot recycle slots around still-running work.
    """

    def __init__(self, limits: ProjectRequestLimits):
        if type(limits) is not ProjectRequestLimits:
            raise HistoryError("history_invalid_input")
        limits.__post_init__()
        self._limits = ProjectRequestLimits(limits.requests, limits.max_wait_ns)
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._jobs: dict[ProjectRequestHandle, _Request] = {}
        self._closed = False
        self._writer = None

    def _guard(self):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")

    def prepare(self, raw: bytes, context: object, *, deadline_ns: int):
        self._guard()
        now = time.monotonic_ns()
        if (type(raw) is not bytes or not raw or len(raw) + 1 > MAX_FRAME_BYTES or b"\n" in raw
                or (context is not None and type(context) is not object)
                or type(deadline_ns) is not int or not now < deadline_ns <= now + self._limits.max_wait_ns):
            raise HistoryError("history_invalid_input")
        with self._lock:
            if self._closed or len(self._jobs) >= self._limits.requests:
                raise HistoryError("history_service_unavailable")
            handle = ProjectRequestHandle()
            self._jobs[handle] = _Request(raw, context, deadline_ns)
            return handle

    def _job(self, handle):
        if type(handle) is not ProjectRequestHandle or handle not in self._jobs:
            raise HistoryError("history_not_authorized")
        return self._jobs[handle]

    def reject_unqueued(self, handle):
        """Only the containing writer lane may withdraw a failed enqueue."""
        self._guard()
        with self._lock:
            job = self._job(handle)
            if job.state != "queued":
                raise HistoryError("history_service_unavailable")
            del self._jobs[handle]

    def execute(self, handle, service: HistoryService):
        """Exactly one original writer handles a request; no lock spans work."""
        self._guard()
        if type(service) is not HistoryService:
            raise HistoryError("history_invalid_input")
        thread = threading.current_thread()
        # Bind writer identity from the actual service, not from request labels.
        if service._pid != self._pid or service._thread is not thread:
            raise HistoryError("history_wrong_owner")
        with self._lock:
            if self._writer is None:
                self._writer = (thread, service)
            if self._writer[0] is not thread or self._writer[1] is not service:
                raise HistoryError("history_wrong_owner")
            job = self._job(handle)
            if job.state != "queued":
                raise HistoryError("history_conflict")
            if self._closed or job.abandoned or time.monotonic_ns() >= job.deadline_ns:
                job.raw = job.context = None
                job.state = "unavailable"
                if job.abandoned or self._closed:
                    del self._jobs[handle]
                return
            job.state = "executing"
            raw, context = job.raw, job.context
        response = None
        failed = False
        try:
            response = service.handle(raw, context)
            if type(response) is not bytes or not response or len(response) + 1 > MAX_FRAME_BYTES or b"\n" in response:
                failed = True
        except Exception:
            failed = True
        except BaseException:
            with self._lock:
                job.raw = job.context = job.response = None
                job.state = "unavailable"
            raise
        with self._lock:
            job.raw = job.context = None
            if self._closed or job.abandoned:
                del self._jobs[handle]
                return
            if failed or service._closed or time.monotonic_ns() >= job.deadline_ns:
                job.state, job.response = "unavailable", None
            else:
                job.state, job.response = "ready", response

    def take(self, handle):
        self._guard()
        with self._lock:
            job = self._job(handle)
            if self._closed:
                raise HistoryError("history_service_unavailable")
            if job.abandoned:
                raise HistoryError("history_not_authorized")
            if time.monotonic_ns() >= job.deadline_ns:
                job.abandoned = True
                if job.state in {"queued", "executing"}:
                    if job.state == "queued":
                        job.raw = job.context = None
                else:
                    del self._jobs[handle]
                raise HistoryError("history_service_unavailable")
            if job.state in {"queued", "executing"}:
                return None
            del self._jobs[handle]
            if job.state != "ready" or time.monotonic_ns() >= job.deadline_ns:
                raise HistoryError("history_service_unavailable")
            return job.response

    def abandon(self, handle):
        self._guard()
        with self._lock:
            job = self._job(handle)
            job.abandoned = True
            if job.state in {"ready", "unavailable"}:
                del self._jobs[handle]
            elif job.state == "queued":
                job.raw = job.context = None

    def close(self):
        self._guard()
        with self._lock:
            self._closed = True
            for handle, job in list(self._jobs.items()):
                if job.state in {"queued", "executing"}:
                    job.abandoned = True
                    if job.state == "queued":
                        job.raw = job.context = None
                else:
                    del self._jobs[handle]

    def finish_writer(self, service):
        """Release retained frames only after this actual writer has stopped work."""
        self._guard()
        thread = threading.current_thread()
        if (type(service) is not HistoryService or service._pid != self._pid or service._thread is not thread):
            raise HistoryError("history_wrong_owner")
        with self._lock:
            if (self._writer is not None and (self._writer[0] is not thread or self._writer[1] is not service)
                    or any(job.state == "executing" for job in self._jobs.values())):
                raise HistoryError("history_wrong_owner")
            self._closed = True
            self._jobs.clear()

    def snapshot(self):
        self._guard()
        with self._lock:
            return {"retained": len(self._jobs), "maximum": self._limits.requests,
                    "retained_bytes_limit": self._limits.retained_bytes, "closed": self._closed}
