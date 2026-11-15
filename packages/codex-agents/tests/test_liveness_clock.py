from __future__ import annotations

import json
import threading
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401

from codex_agents import liveness_clock
from codex_agents.liveness_clock import (
    MAX_CLOCK_NS, LivenessClock, LivenessClockError, LivenessLimits,
)

SECOND = 1_000_000_000


class FakeClock:
    def __init__(self, value=0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def at(self, seconds, *, extra_ns=0):
        self.value = seconds * SECOND + extra_ns


class LivenessClockTests(unittest.TestCase):
    def make_clock(self, *, deadline=100, limits=None):
        source = FakeClock()
        timer = LivenessClock(limits or LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND),
                              hard_deadline_ns=deadline * SECOND, clock_ns=source)
        return source, timer

    def test_startup_timeout_at_exact_boundary_without_output(self):
        source, timer = self.make_clock()
        self.assertEqual("starting", timer.poll().health)
        source.at(18, extra_ns=-1)
        before = timer.poll()
        self.assertEqual(("starting", 1, None),
                         (before.health, before.startup_remaining_ms, before.silence_age_ms))
        source.at(18)
        stopped = timer.poll()
        self.assertEqual(("stopping", "startup_silence", True, False),
                         (stopped.health, stopped.stop_reason, stopped.stop_requested, stopped.terminal))

    def test_startup_then_active_quiet_stale_and_work_cutoff(self):
        source, timer = self.make_clock()
        source.at(3)
        started = timer.startup()
        self.assertEqual(("active", 0, None, 0, "startup"),
                         (started.health, started.silence_age_ms, started.startup_remaining_ms,
                          started.activity_events, started.last_activity_category))
        for seconds, extra, expected in ((5, -1, "active"), (5, 0, "quiet"),
                                          (8, -1, "quiet"), (8, 0, "stale"),
                                          (13, -1, "stale"), (13, 0, "stopping")):
            with self.subTest(seconds=seconds, extra_ns=extra):
                source.at(seconds, extra_ns=extra)
                snapshot = timer.poll()
                self.assertEqual(expected, snapshot.health)
        self.assertEqual("work_silence", snapshot.stop_reason)

    def test_qualifying_work_recovers_stale_and_restarts_only_silence(self):
        source, timer = self.make_clock(deadline=20)
        timer.startup()
        source.at(8)
        self.assertEqual("stale", timer.poll().health)
        recovered = timer.activity()
        self.assertEqual(("active", 0, 1, "qualifying_work", 12_000),
                         (recovered.health, recovered.silence_age_ms, recovered.activity_events,
                          recovered.last_activity_category, recovered.hard_deadline_remaining_ms))
        for seconds, health in ((10, "quiet"), (13, "stale"), (18, "stopping")):
            source.at(seconds)
            self.assertEqual(health, timer.poll().health)
        self.assertEqual("work_silence", timer.poll().stop_reason)

    def test_repeated_startup_and_polling_are_not_qualifying_work(self):
        source, timer = self.make_clock()
        timer.startup()
        for seconds in range(1, 10):
            source.at(seconds)
            timer.poll()
            snapshot = timer.startup()
            self.assertEqual(0, snapshot.activity_events)
            self.assertEqual(seconds * 1000, snapshot.silence_age_ms)
        source.at(10)
        self.assertEqual("work_silence", timer.startup().stop_reason)

    def test_work_before_startup_cannot_bypass_the_startup_deadline(self):
        source, timer = self.make_clock()
        source.at(5)
        with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_transition"):
            timer.activity()
        self.assertEqual(("starting", 13_000, 0),
                         (timer.poll().health, timer.poll().startup_remaining_ms, timer.poll().activity_events))
        source.at(18)
        self.assertEqual("startup_silence", timer.activity().stop_reason)

    def test_earliest_elapsed_timer_wins_even_when_both_are_overdue(self):
        for startup, deadline, expected in ((False, 5, "absolute_deadline"),
                                            (False, 20, "startup_silence"),
                                            (True, 5, "absolute_deadline"),
                                            (True, 20, "work_silence")):
            with self.subTest(startup=startup, deadline=deadline):
                source, timer = self.make_clock(deadline=deadline)
                if startup:
                    timer.startup()
                source.at(30)
                self.assertEqual(expected, timer.poll().stop_reason)

    def test_hard_deadline_wins_both_timer_ties(self):
        for startup, deadline in ((False, 18), (True, 10)):
            with self.subTest(startup=startup):
                source, timer = self.make_clock(deadline=deadline)
                if startup:
                    timer.startup()
                source.at(deadline)
                self.assertEqual("absolute_deadline", timer.poll().stop_reason)

    def test_callbacks_at_cutoff_cannot_refresh_cancel_or_complete_the_run(self):
        for startup, cutoff, expected in ((False, 18, "startup_silence"), (True, 10, "work_silence")):
            for method in ("startup", "activity", "cancel", "finish"):
                with self.subTest(startup=startup, method=method):
                    source, timer = self.make_clock()
                    if startup:
                        timer.startup()
                    source.at(cutoff)
                    snapshot = getattr(timer, method)()
                    self.assertEqual((expected, False, 0),
                                     (snapshot.stop_reason, snapshot.terminal, snapshot.activity_events))

    def test_activity_one_nanosecond_before_cutoff_is_allowed(self):
        source, timer = self.make_clock()
        timer.startup()
        source.at(10, extra_ns=-1)
        self.assertEqual("active", timer.activity().health)
        source.at(10)
        self.assertEqual("active", timer.poll().health)
        source.at(20, extra_ns=-1)
        self.assertEqual("work_silence", timer.poll().stop_reason)

    def test_activity_never_extends_the_original_hard_deadline(self):
        source, timer = self.make_clock(deadline=7)
        timer.startup()
        for seconds in range(1, 7):
            source.at(seconds)
            snapshot = timer.activity()
            self.assertEqual((7 - seconds) * 1000, snapshot.hard_deadline_remaining_ms)
        source.at(7)
        self.assertEqual("absolute_deadline", timer.activity().stop_reason)

    def test_cancellation_is_requested_not_confirmed_cleanup_and_is_idempotent(self):
        source, timer = self.make_clock()
        timer.startup()
        source.at(9)
        cancelled = timer.cancel()
        self.assertEqual(("stopping", "cancel_requested", True, False),
                         (cancelled.health, cancelled.stop_reason, cancelled.stop_requested, cancelled.terminal))
        for method in ("poll", "startup", "activity", "cancel", "finish"):
            source.at(100)
            self.assertEqual(cancelled, getattr(timer, method)())

    def test_first_serialized_completion_or_cancel_wins_before_cutoff(self):
        for first, second in (("finish", "cancel"), ("cancel", "finish")):
            with self.subTest(first=first):
                source, timer = self.make_clock()
                timer.startup()
                source.at(3)
                decided = getattr(timer, first)()
                self.assertEqual(decided, getattr(timer, second)())
                self.assertEqual(first == "finish", decided.terminal)
                self.assertEqual(first == "cancel", decided.stop_requested)

    def test_authoritative_early_terminal_freezes_metadata_and_cannot_reactivate(self):
        source, timer = self.make_clock()
        source.at(4)
        terminal = timer.finish()
        self.assertEqual(("terminal", True, False, 4000),
                         (terminal.health, terminal.terminal, terminal.stop_requested, terminal.elapsed_ms))
        source.at(1000)
        for method in ("poll", "startup", "activity", "cancel", "finish"):
            self.assertEqual(terminal, getattr(timer, method)())

    def test_deadline_already_elapsed_on_creation_is_never_extended(self):
        source = FakeClock(20 * SECOND)
        timer = LivenessClock(LivenessLimits(18, 2, 5, 10), hard_deadline_ns=19 * SECOND, clock_ns=source)
        self.assertEqual(("absolute_deadline", 0),
                         (timer.poll().stop_reason, timer.poll().hard_deadline_remaining_ms))

    def test_clock_failure_or_regression_latches_unavailable_without_exception_content(self):
        for invalid in (True, -1, 1.5, "CANARY-private", None, MAX_CLOCK_NS + 1,
                        RuntimeError("CANARY-private"), 0):
            with self.subTest(kind=type(invalid).__name__):
                source, timer = self.make_clock()
                source.at(1)
                timer.poll()
                source.value = invalid
                with self.assertRaisesRegex(LivenessClockError, "^liveness_invalid_clock$") as caught:
                    timer.poll()
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn("CANARY", str(caught.exception))
                source.at(2)
                with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_clock"):
                    timer.startup()

    def test_invalid_clock_at_creation_has_no_healthy_state(self):
        for value in (False, -1, float("nan"), "CANARY", None, MAX_CLOCK_NS + 1, OSError("CANARY")):
            with self.subTest(kind=type(value).__name__), \
                    self.assertRaisesRegex(LivenessClockError, "liveness_invalid_clock") as caught:
                LivenessClock(LivenessLimits(18, 2, 5, 10), hard_deadline_ns=100, clock_ns=FakeClock(value))
            self.assertIsNone(caught.exception.__context__)

    def test_nonlocal_clock_interrupts_latch_failure_without_swallowing_interrupt(self):
        for failure in (KeyboardInterrupt("CANARY"), SystemExit("CANARY"), GeneratorExit("CANARY")):
            with self.subTest(kind=type(failure).__name__):
                samples = iter((0, 0, failure, SECOND))

                def sample():
                    value = next(samples)
                    if isinstance(value, BaseException):
                        raise value
                    return value

                timer = LivenessClock(LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND),
                                      hard_deadline_ns=100 * SECOND, clock_ns=sample)
                timer.startup()
                with self.assertRaises(type(failure)):
                    timer.poll()
                with self.assertRaisesRegex(LivenessClockError, "^liveness_invalid_clock$") as caught:
                    timer.poll()
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_caller_policy_object_changes_cannot_extend_an_existing_clock(self):
        for startup in (False, True):
            with self.subTest(startup=startup):
                limits = LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND)
                source, timer = self.make_clock(limits=limits, deadline=1000)
                if startup:
                    timer.startup()
                # A frozen dataclass is not an immutable storage boundary. This
                # caller-owned object must no longer control an existing attempt.
                for field, value in (("startup_ns", 180 * SECOND), ("quiet_ns", 20 * SECOND),
                                     ("stale_ns", 50 * SECOND), ("silence_ns", 100 * SECOND)):
                    object.__setattr__(limits, field, value)
                source.at(10 if startup else 18)
                self.assertEqual("work_silence" if startup else "startup_silence", timer.poll().stop_reason)

    def test_limits_are_strict_ordered_frozen_and_can_only_narrow(self):
        limits = LivenessLimits(18, 2, 5, 10)
        fields = ("startup_ns", "quiet_ns", "stale_ns", "silence_ns")
        for field in fields:
            for value in (True, False, 0, -1, 1.5, "CANARY", None, MAX_CLOCK_NS + 1):
                with self.subTest(field=field, kind=type(value).__name__), \
                        self.assertRaisesRegex(LivenessClockError, "liveness_invalid_limits"):
                    replace(limits, **{field: value})
            with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_limits"):
                limits.narrowed(replace(limits, **{field: getattr(limits, field) + 1}))
        for values in ((18, 5, 5, 10), (18, 2, 10, 10), (18, 6, 5, 10)):
            with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_limits"):
                LivenessLimits(*values)
        narrower = LivenessLimits(17, 1, 4, 9)
        self.assertEqual(narrower, limits.narrowed(narrower))
        with self.assertRaises(FrozenInstanceError):
            limits.silence_ns = 100
        with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_limits"):
            limits.narrowed({"silence_ns": 1})

    def test_narrowing_captures_values_before_caller_changes_requested_policy(self):
        installed = LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND)
        requested = LivenessLimits(17 * SECOND, SECOND, 4 * SECOND, 9 * SECOND)
        effective = installed.narrowed(requested)
        object.__setattr__(requested, "silence_ns", 100 * SECOND)
        source, timer = self.make_clock(limits=effective)
        timer.startup()
        source.at(9)
        self.assertEqual("work_silence", timer.poll().stop_reason)

    def test_invalid_constructor_inputs_have_fixed_reasons(self):
        for deadline in (True, -1, None, 1.0, MAX_CLOCK_NS + 1, "CANARY"):
            with self.subTest(deadline=deadline), \
                    self.assertRaisesRegex(LivenessClockError, "liveness_invalid_deadline") as caught:
                LivenessClock(LivenessLimits(18, 2, 5, 10), hard_deadline_ns=deadline)
            self.assertIsNone(caught.exception.__context__)
        with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_limits"):
            LivenessClock({}, hard_deadline_ns=100)
        with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_clock"):
            LivenessClock(LivenessLimits(18, 2, 5, 10), hard_deadline_ns=100, clock_ns=None)

    def test_foreign_threads_cannot_read_or_mutate_or_sample_the_clock(self):
        class EqualThread(threading.Thread):
            __hash__ = object.__hash__

            def __eq__(self, other):
                return True

        for thread_type in (threading.Thread, EqualThread):
            with self.subTest(thread_type=thread_type.__name__):
                source, timer = self.make_clock()
                calls = source.calls
                results = []

                def operate():
                    for method in ("poll", "startup", "activity", "cancel", "finish"):
                        try:
                            getattr(timer, method)()
                        except LivenessClockError as exc:
                            results.append(exc.reason_code)

                worker = thread_type(target=operate)
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(["liveness_wrong_owner"] * 5, results)
                self.assertEqual(calls, source.calls)
                self.assertEqual("starting", timer.poll().health)

    def test_foreign_process_origin_refuses_before_clock_access(self):
        source, timer = self.make_clock()
        calls = source.calls
        with mock.patch.object(liveness_clock, "os", SimpleNamespace(getpid=lambda: 0)), \
                self.assertRaisesRegex(LivenessClockError, "liveness_wrong_owner"):
            timer.poll()
        self.assertEqual(calls, source.calls)
        self.assertEqual("starting", timer.poll().health)

    def test_counts_and_snapshot_bytes_stay_bounded_without_retaining_payload(self):
        source, timer = self.make_clock(deadline=MAX_CLOCK_NS // SECOND)
        first = timer.startup()
        with mock.patch.object(liveness_clock, "MAX_ACTIVITY_EVENTS", 2):
            for _ in range(10):
                snapshot = timer.activity()
        self.assertEqual(2, snapshot.activity_events)
        self.assertEqual(0, first.activity_events)
        with self.assertRaises(FrozenInstanceError):
            snapshot.health = "active"
        source.value = MAX_CLOCK_NS
        snapshot = timer.poll()
        encoded = json.dumps(asdict(snapshot)).encode()
        self.assertLess(len(encoded), 1024)
        self.assertEqual("work_silence", snapshot.stop_reason)
        self.assertEqual({"qualifying_work"}, {snapshot.last_activity_category})


if __name__ == "__main__":
    unittest.main()
