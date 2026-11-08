"""Bounded local receipt metadata; callers hold their execution registry lock.

No native frame, text, item ID or provider timestamp is retained here. The clock
must be the trusted bounded monotonic source shared with the project monitor.
Neither this buffer nor its immutable batches establish native authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from .liveness_clock import MAX_CLOCK_NS, MAX_RECEIVED_EVENTS


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
        if type(category) is not str or category not in {"startup", "activity"}:
            self.fail("activity_invalid")
            raise ActivityUnavailable()
        if len(self.events) >= MAX_RECEIVED_EVENTS:
            self.fail("activity_capacity_exceeded")
            raise ActivityUnavailable()
        self.events.append((category, self._sample()))

    def receive(self):
        # The clock sample and drain share the publisher's external lock. An
        # event published after this watermark cannot predate its timer poll.
        try:
            observed = self._sample()
        except ActivityUnavailable:
            self.events.clear()
            return ActivityBatch((), None, True, self.failure_code)
        batch = ActivityBatch(tuple(self.events), observed, False, None)
        self.events.clear()
        return batch
