"""Local monotonic liveness decisions for one trusted supervisor event loop.

This is not an event decoder, caller authenticator, process terminator or enabled
monitor. The owner must validate native run/model/tool/billing facts before calling
startup() or activity(), and supply installed bounds and the original deadline.
There is no persistence, RPC, status callback or provider timestamp in this core.
Real-client event qualification and calibration remain separate gates.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

MAX_CLOCK_NS = (1 << 63) - 1
MAX_ACTIVITY_EVENTS = (1 << 31) - 1
MAX_RECEIVED_EVENTS = 256
NS_PER_MS = 1_000_000
CLOCK_REASON_CODES = frozenset({
    "liveness_invalid_limits", "liveness_invalid_deadline", "liveness_invalid_clock",
    "liveness_wrong_owner", "liveness_invalid_transition",
})


class LivenessClockError(RuntimeError):
    """Fixed metadata only; never clock exceptions or rejected values."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in CLOCK_REASON_CODES:
            reason_code = "liveness_invalid_clock"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_CLOCK_NS


@dataclass(frozen=True)
class LivenessLimits:
    """Explicit installed durations; construction alone supplies no authority.

    No defaults are selected here. Nanoseconds avoid hidden timestamp conversion
    and let the owner derive all clocks from the same monotonic source.
    """

    startup_ns: int
    quiet_ns: int
    stale_ns: int
    silence_ns: int

    def __post_init__(self) -> None:
        if (
            not all(_integer(value, minimum=1) for value in
                    (self.startup_ns, self.quiet_ns, self.stale_ns, self.silence_ns))
            or not self.quiet_ns < self.stale_ns < self.silence_ns
        ):
            raise LivenessClockError("liveness_invalid_limits")

    def narrowed(self, requested: LivenessLimits) -> LivenessLimits:
        """Accept only a valid, no-wider set; the hard deadline is not a field."""
        if type(requested) is not LivenessLimits:
            raise LivenessClockError("liveness_invalid_limits")
        requested = LivenessLimits(requested.startup_ns, requested.quiet_ns,
                                   requested.stale_ns, requested.silence_ns)
        if any(getattr(requested, field) > getattr(self, field) for field in
               ("startup_ns", "quiet_ns", "stale_ns", "silence_ns")):
            raise LivenessClockError("liveness_invalid_limits")
        return requested


@dataclass(frozen=True)
class LivenessSnapshot:
    """Bounded internal metadata, not an execution or cleanup receipt.

    Once stop or terminal is observed, ages freeze at that decision. Monitor
    publication freshness and later OS cleanup must be reported separately.
    """

    health: str
    elapsed_ms: int
    silence_age_ms: int | None
    startup_remaining_ms: int | None
    silence_remaining_ms: int | None
    hard_deadline_remaining_ms: int
    activity_events: int
    last_activity_category: str | None
    stop_reason: str | None
    terminal: bool

    @property
    def stop_requested(self) -> bool:
        return self.stop_reason is not None


class LivenessClock:
    """One owner-thread clock; callbacks are serialized observation points.

    Evaluate the earliest elapsed timer before each callback. The hard deadline
    wins a timer tie. Before a timer elapses, the first serialized cancellation or
    authoritative completion wins. Later callbacks never replace that decision.
    Cancellation only requests a stop; this class never confirms process cleanup.
    """

    def __init__(
        self, limits: LivenessLimits, *, hard_deadline_ns: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(limits) is not LivenessLimits:
            raise LivenessClockError("liveness_invalid_limits")
        # Retain validated values, not a caller's policy object. Even frozen
        # dataclasses can be changed through Python's object mutation escape.
        limits = LivenessLimits(limits.startup_ns, limits.quiet_ns, limits.stale_ns, limits.silence_ns)
        if not _integer(hard_deadline_ns):
            raise LivenessClockError("liveness_invalid_deadline")
        if not callable(clock_ns):
            raise LivenessClockError("liveness_invalid_clock")
        self._limits = limits
        self._deadline = hard_deadline_ns
        self._clock = clock_ns
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._last_sample: int | None = None
        self._clock_failed = False
        self._last_activity: int | None = None
        self._activity_events = 0
        self._last_category: str | None = None
        self._stop_reason: str | None = None
        self._decision_at: int | None = None
        self._terminal = False
        self._started = self._sample()

    def _owner(self) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise LivenessClockError("liveness_wrong_owner")

    def _sample(self) -> int:
        self._owner()
        if self._clock_failed:
            raise LivenessClockError("liveness_invalid_clock")
        # Preserve nonlocal interrupts without allowing a later healthy sample.
        # Also refuse a clock callback that re-enters this clock.
        self._clock_failed = True
        try:
            value = self._clock()
        except Exception:
            invalid = True
        else:
            invalid = not _integer(value) or (self._last_sample is not None and value < self._last_sample)
        if invalid:
            # Recovery cannot restore an active label after losing the clock.
            # The owner reports monitoring unavailable and retains the watchdog.
            raise LivenessClockError("liveness_invalid_clock")
        self._last_sample = value
        self._clock_failed = False
        return value

    def _evaluate(self) -> int:
        self._owner()
        if self._decision_at is not None:
            return self._decision_at
        return self._evaluate_at(self._sample())

    def _evaluate_at(self, now: int) -> int:
        if self._decision_at is not None:
            return self._decision_at
        silence_base = self._started if self._last_activity is None else self._last_activity
        duration = self._limits.startup_ns if self._last_activity is None else self._limits.silence_ns
        reason = "startup_silence" if self._last_activity is None else "work_silence"
        # Python integer addition is bounded here by two validated 63-bit values.
        due, _, reason = min((self._deadline, 0, "absolute_deadline"),
                             (silence_base + duration, 1, reason))
        if now >= due:
            self._stop_reason, self._decision_at = reason, now
        return now

    def received(self, events: tuple[tuple[str, int], ...]) -> LivenessSnapshot:
        """Apply one trusted, ordered receipt batch before its atomic watermark.

        The clock source for this call must return the watermark sampled under
        the actual local publisher's lock. These are not provider timestamps or
        a wire API. Sampling time after replaying each old record would either
        renew old work or falsely cut off a healthy queued sequence.
        """
        self._owner()
        if self._decision_at is not None:
            return self._snapshot(self._decision_at)
        previous = self._last_sample
        now = self._sample()
        valid = type(events) is tuple and len(events) <= MAX_RECEIVED_EVENTS
        last = previous
        if valid:
            for event in events:
                if (type(event) is not tuple or len(event) != 2
                        or type(event[0]) is not str or event[0] not in {"startup", "activity"}
                        or not _integer(event[1]) or not self._started <= event[1] <= now
                        or (last is not None and event[1] < last)):
                    valid = False
                    break
                last = event[1]
        if not valid:
            self._clock_failed = True
            raise LivenessClockError("liveness_invalid_clock")
        for category, received in events:
            self._evaluate_at(received)
            if self._decision_at is not None:
                break
            if category == "startup":
                if self._last_activity is None:
                    self._last_activity, self._last_category = received, "startup"
            elif self._last_activity is None:
                self._clock_failed = True
                raise LivenessClockError("liveness_invalid_transition")
            else:
                self._last_activity, self._last_category = received, "qualifying_work"
                self._activity_events = min(MAX_ACTIVITY_EVENTS, self._activity_events + 1)
        return self._snapshot(self._evaluate_at(now))

    @staticmethod
    def _remaining_ms(due: int, now: int) -> int:
        # A positive remainder must not be displayed as an already elapsed bound.
        return (max(0, due - now) + NS_PER_MS - 1) // NS_PER_MS

    def _snapshot(self, now: int) -> LivenessSnapshot:
        age = None if self._last_activity is None else now - self._last_activity
        if self._stop_reason is not None:
            health = "stopping"
        elif self._terminal:
            health = "terminal"
        elif age is None:
            health = "starting"
        elif age >= self._limits.stale_ns:
            health = "stale"
        elif age >= self._limits.quiet_ns:
            health = "quiet"
        else:
            health = "active"
        return LivenessSnapshot(
            health, (now - self._started) // NS_PER_MS,
            None if age is None else age // NS_PER_MS,
            self._remaining_ms(self._started + self._limits.startup_ns, now) if age is None else None,
            None if age is None else self._remaining_ms(self._last_activity + self._limits.silence_ns, now),
            self._remaining_ms(self._deadline, now), self._activity_events, self._last_category,
            self._stop_reason, self._terminal,
        )

    def poll(self) -> LivenessSnapshot:
        """Evaluate timers; polling, bytes, keepalives and metadata are not work."""
        return self._snapshot(self._evaluate())

    def startup(self) -> LivenessSnapshot:
        """Observe the first validated startup; repeats never refresh silence."""
        now = self._evaluate()
        if self._decision_at is None and self._last_activity is None:
            self._last_activity, self._last_category = now, "startup"
        return self._snapshot(now)

    def activity(self) -> LivenessSnapshot:
        """Observe already validated, run-bound work; no native payload is accepted."""
        now = self._evaluate()
        if self._decision_at is None:
            if self._last_activity is None:
                raise LivenessClockError("liveness_invalid_transition")
            self._last_activity, self._last_category = now, "qualifying_work"
            self._activity_events = min(MAX_ACTIVITY_EVENTS, self._activity_events + 1)
        return self._snapshot(now)

    def cancel(self) -> LivenessSnapshot:
        """Request cancellation after owner authorization; never acknowledge cleanup."""
        now = self._evaluate()
        if self._decision_at is None:
            self._stop_reason, self._decision_at = "cancel_requested", now
        return self._snapshot(now)

    def finish(self) -> LivenessSnapshot:
        """Observe an authoritative terminal; a late completion cannot undo a stop.

        The owner supplies this after its terminal checks, never from a model's
        completion assertion. It supplies no process-cleanup evidence to this core.
        """
        now = self._evaluate()
        if self._decision_at is None:
            self._terminal, self._decision_at = True, now
        return self._snapshot(now)
