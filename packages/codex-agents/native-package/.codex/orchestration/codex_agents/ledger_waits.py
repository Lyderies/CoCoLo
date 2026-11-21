"""M4b E1a: how long anything waits for the ledger lock, and the refusals counted (PLAN E1a-1 to E1a-4;
DESIGN-0.4.0-M4b-E1a.md).

Every bounded wait for the ledger lock waits at most LEDGER_LOCK_WAIT_MS, and inside a request (an
account or collector request, set with `request()`) all of the request's waits share one
LEDGER_REQUEST_WAIT_MS deadline, under the shortest client timeout that reaches the ledger (1 s).
A wait that runs out is refused with today's busy refusal, counted here by the caller's name.
"""
from __future__ import annotations

import contextlib
import threading
import time
from threading import local as _ThreadLocal

LEDGER_LOCK_WAIT_MS = 750
LEDGER_REQUEST_WAIT_MS = 800

_request = _ThreadLocal()


def wait_seconds(cap_seconds: float | None = None) -> float:
    """The longest this wait may take: the per-wait limit, what is left of this thread's request
    deadline, and the caller's own budget (a read scope's), whichever is shortest."""
    wait = LEDGER_LOCK_WAIT_MS / 1000
    deadline = getattr(_request, "deadline", None)
    if deadline is not None:
        wait = min(wait, max(0.0, deadline - time.monotonic()))
    if cap_seconds is not None:
        wait = min(wait, cap_seconds)
    return wait


@contextlib.contextmanager
def request():
    """One request's shared deadline; restored on every return, so nothing later on this thread
    inherits it (revision 3, item 1)."""
    previous = getattr(_request, "deadline", None)
    _request.deadline = time.monotonic() + LEDGER_REQUEST_WAIT_MS / 1000
    try:
        yield
    finally:
        _request.deadline = previous


class LockWaits:
    """Refusals by caller. Its own lock, never the ledger's, so a snapshot can be read while the ledger
    lock is held (E1a-3)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def refused(self, label: str) -> None:
        with self._lock:
            self._counts[label] = self._counts.get(label, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
