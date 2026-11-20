"""O10 SD2: a collector's shutdown ends within its bound, whatever any step does (PLAN SD2-1 to SD2-3).

The first stop step starts a timer. If the shutdown has not finished when the bound passes, the exit
record names the step it was stuck in and the process ends at once. Ending this way is safe for the
ledger: every write is a committed or rolled-back transaction, so the next open recovers it, as after
any crash.
"""
from __future__ import annotations

import math
import os
import signal
import threading

from .collector_exit import SHUTDOWN_STEPS, write_exit

__all__ = ("SHUTDOWN_STEPS", "ShutdownWatchdog", "SHUTDOWN_BOUND_SECONDS")

# Inside the 20 s its callers allow (SD), with room for the process to be reaped.
SHUTDOWN_BOUND_SECONDS = 15
# SD2-1 (detail): the kernel's backstop, for when a call in C keeps the timer from running.
HARD_STOP_SECONDS = SHUTDOWN_BOUND_SECONDS + 3


class ShutdownWatchdog:
    FORCED_EXIT_CODE = 3

    def __init__(self, state_root, *, started_at_ms, bound_seconds=SHUTDOWN_BOUND_SECONDS,
                 hard_stop_seconds=HARD_STOP_SECONDS, exit=os._exit):
        self._root, self._started_at_ms, self._bound, self._exit = state_root, started_at_ms, bound_seconds, exit
        self._hard_stop = hard_stop_seconds
        self._lock = threading.Lock()
        self._current = None
        self._finished = False
        self._timer = None

    def step(self, name: str) -> None:
        """The shutdown is now in `name`; the first step starts the bound."""
        with self._lock:
            self._current = name if name in SHUTDOWN_STEPS else None
            if self._timer is None and not self._finished:
                self._timer = threading.Timer(self._bound, self._expired)
                self._timer.daemon = True
                self._timer.start()
                if self._hard_stop is not None:
                    # SIGALRM at its default action ends the process in the kernel, whatever the
                    # interpreter is doing; it stays armed through the final print and flush.
                    signal.signal(signal.SIGALRM, signal.SIG_DFL)
                    signal.alarm(max(1, math.ceil(self._hard_stop)))

    def done(self) -> None:
        """The steps finished: the timer is cancelled; the kernel alarm stays until the process ends."""
        with self._lock:
            self._finished = True
            if self._timer is not None:
                self._timer.cancel()

    def _expired(self) -> None:
        with self._lock:
            if self._finished:
                return
            stalled = self._current
        try:
            write_exit(self._root, started_at_ms=self._started_at_ms, cause="stop_forced", error=None,
                       stalled_in=stalled)
        finally:
            self._exit(self.FORCED_EXIT_CODE)
