"""O10 SD2: a collector's shutdown ends within its bound, whatever any step does (PLAN SD2-1 to SD2-3).

The first stop step starts a timer. If the shutdown has not finished when the bound passes, the exit
record names the step it was stuck in and the process ends at once. Ending this way is safe for the
ledger: every write is a committed or rolled-back transaction, so the next open recovers it, as after
any crash.

O10 SD3-3: the first step can be `signal_received`, taken inside the collector's SIGTERM/SIGINT
handler, so the bound also covers a stop whose exception was lost before any shutdown step ran.
What a step does is therefore kept safe to do from that handler (which Python runs on the main
thread between two bytecodes, possibly while that thread is inside any other code):
- no thread is started there: `prepare()` starts the waiting thread beforehand;
- no lock the main thread can hold is taken there: only a plain attribute write, a read of an
  Event's flag, `Event.set()` on an Event only this watchdog's thread waits on, and two signal calls
  (`signal.signal` and `signal.alarm`, allowed on the main thread);
- the bound and the kernel alarm are armed once, by the first step; later steps only rename it.
"""
from __future__ import annotations

import math
import os
import signal
import threading
import time

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
        self._root, self._bound, self._exit = state_root, bound_seconds, exit
        # Set once the collector has started (SD3: the watchdog exists before that).
        self.started_at_ms = started_at_ms
        self._hard_stop = hard_stop_seconds
        self._current = None
        self._began = None  # monotonic time of the first step
        self._armed = threading.Event()  # waited on only by this watchdog's thread
        self._finished = threading.Event()
        self._thread = None

    def prepare(self) -> None:
        """Start the waiting thread now, so that a later step (from a signal handler) starts none."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="shutdown-watchdog", daemon=True)
            self._thread.start()

    def step(self, name: str) -> None:
        """The shutdown is now in `name`; the first step starts the bound. Safe in a signal handler
        once prepared (module docstring)."""
        self._current = name if name in SHUTDOWN_STEPS else None
        if self._began is not None or self._finished.is_set():
            return
        self._began = time.monotonic()
        if self._hard_stop is not None:
            # SIGALRM at its default action ends the process in the kernel, whatever the
            # interpreter is doing; it stays armed through the final print and flush.
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(max(1, math.ceil(self._hard_stop)))
        self.prepare()  # already started where it matters (SD3-3); started here otherwise
        self._armed.set()

    def done(self) -> None:
        """The steps finished (or the process ends another way): the timer stops; the kernel alarm
        stays until the process ends."""
        self._finished.set()
        self._armed.set()  # wakes the thread so it ends

    def _run(self) -> None:
        self._armed.wait()
        if self._finished.is_set():
            return
        if self._finished.wait(max(0.0, self._began + self._bound - time.monotonic())):
            return
        self._expired()

    def _expired(self) -> None:
        if self._finished.is_set():
            return
        try:
            write_exit(self._root, started_at_ms=self.started_at_ms, cause="stop_forced", error=None,
                       stalled_in=self._current)
        finally:
            self._exit(self.FORCED_EXIT_CODE)
