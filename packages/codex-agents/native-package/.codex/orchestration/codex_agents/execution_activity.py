"""Bounded local receipt metadata; callers hold their execution registry lock.

No native frame, text, item ID or provider timestamp is retained here. The clock
must be the trusted bounded monotonic source shared with the project monitor.
Neither this buffer nor its immutable batches establish native authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from .liveness_clock import LIVENESS_STOP_REASONS, MAX_CLOCK_NS, MAX_RECEIVED_EVENTS


class ActivityUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("monitor_unavailable")


@dataclass(frozen=True)
class ActivityBatch:
    events: tuple[tuple[str, int], ...]
    observed_ns: int | None
    failed: bool
    failure_code: str | None


class ActivityBuffer:
    def __init__(self, clock_ns=time.monotonic_ns):
        if not callable(clock_ns):
            raise ActivityUnavailable()
        self.clock = clock_ns
        self.events = []
        self.last_sample = None
        self.failed = False
        self.failure_code = None
        self.sampling = False
        self.completion_ns = None
        self.completion_seen = False
        self.stop_event = None
        self.stop_seen = False

    def fail(self, code="activity_invalid"):
        self.failed = True
        if type(code) is not str or code not in {
            "activity_clock_unavailable", "activity_capacity_exceeded", "activity_invalid",
        }:
            code = "activity_invalid"
        self.failure_code = self.failure_code or code

    def _sample(self):
        if self.failed or self.sampling:
            self.fail("activity_clock_unavailable")
            raise ActivityUnavailable()
        self.sampling = True
        try:
            try:
                value = self.clock()
            except Exception:
                raise ActivityUnavailable() from None
            if (self.failed or type(value) is not int or not 0 <= value <= MAX_CLOCK_NS
                    or (self.last_sample is not None and value < self.last_sample)):
                raise ActivityUnavailable()
            self.last_sample = value
            return value
        except BaseException:
            self.fail("activity_clock_unavailable")
            raise
        finally:
            self.sampling = False

    def publish(self, category):
        if self.completion_seen or self.stop_seen:
            return  # A late producer cannot invalidate or renew actual completion.
        if type(category) is not str or category not in {"startup", "activity"}:
            self.fail("activity_invalid")
            raise ActivityUnavailable()
        if len(self.events) >= MAX_RECEIVED_EVENTS:
            self.fail("activity_capacity_exceeded")
            raise ActivityUnavailable()
        self.events.append((category, self._sample()))

    def complete(self):
        """Reserved, once-only lane used only by the actual fixed worker loop."""
        if self.completion_seen:
            raise ActivityUnavailable()
        self.completion_seen = True
        self.completion_ns = self._sample()

    def stop(self, reason):
        if self.completion_seen or self.stop_seen:
            return
        if type(reason) is not str or reason not in LIVENESS_STOP_REASONS:
            self.fail()
            raise ActivityUnavailable()
        self.stop_seen = True
        self.stop_event = (reason, self._sample())

    def receive(self):
        # The clock sample and drain share the publisher's external lock. An
        # event published after this watermark cannot predate its timer poll.
        try:
            observed = self._sample()
        except ActivityUnavailable:
            self.events.clear()
            return ActivityBatch((), None, True, self.failure_code)
        events = tuple(self.events)
        if self.stop_event is not None:
            events += (self.stop_event,)
            self.stop_event = None
        if self.completion_ns is not None:
            events += (("terminal", self.completion_ns),)
            self.completion_ns = None
        batch = ActivityBatch(events, observed, False, None)
        self.events.clear()
        return batch
