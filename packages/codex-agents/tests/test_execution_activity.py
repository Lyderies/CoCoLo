from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from tests.test_liveness_clock import FakeClock, SECOND
from codex_agents.execution_activity import ActivityBuffer, ActivityUnavailable
from codex_agents.liveness_clock import MAX_RECEIVED_EVENTS, LivenessClock, LivenessClockError, LivenessLimits


class ActivityReceiptTests(unittest.TestCase):
    def make(self):
        clock = FakeClock()
        return clock, ActivityBuffer(clock), LivenessClock(
            LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND),
            hard_deadline_ns=60 * SECOND, clock_ns=clock,
        )

    def test_delayed_batch_uses_actual_receipts_and_does_not_false_cutoff_between_records(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(9)
        buffer.publish("activity")
        clock.at(18)
        buffer.publish("activity")
        clock.at(25)
        batch = buffer.receive()
        self.assertEqual(25 * SECOND, batch.observed_ns)
        status = timer.received(batch.events)
        self.assertEqual(("stale", 7000, 2, None),
                         (status.health, status.silence_age_ms, status.activity_events, status.stop_reason))
        clock.at(28)
        self.assertEqual("work_silence", timer.received(buffer.receive().events).stop_reason)

    def test_old_activity_is_not_renewed_at_consumption_time(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(1)
        buffer.publish("activity")
        clock.at(12)
        stopped = timer.received(buffer.receive().events)
        self.assertEqual(("work_silence", 11000), (stopped.stop_reason, stopped.silence_age_ms))
        clock.at(13)
        buffer.publish("activity")
        self.assertEqual(stopped, timer.received(buffer.receive().events))

    def test_event_at_cutoff_cannot_revive_and_hard_deadline_wins_a_tie(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(10)
        buffer.publish("activity")
        self.assertEqual("work_silence", timer.received(buffer.receive().events).stop_reason)
        clock = FakeClock()
        timer = LivenessClock(LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND),
                              hard_deadline_ns=10 * SECOND, clock_ns=clock)
        clock.at(10)
        self.assertEqual("absolute_deadline", timer.received((("startup", 0), ("activity", 10 * SECOND))).stop_reason)

    def test_replayed_out_of_order_future_or_untyped_receipts_fail_closed(self):
        for events in ((("activity", 1),), (("startup", True),), (("startup", 2 * SECOND),),
                       (("startup", 0), ("activity", 1), ("activity", 0)),
                       (("PRIVATE", 0),), [("startup", 0)]):
            with self.subTest(events=events):
                clock, _, timer = self.make()
                clock.at(1)
                with self.assertRaises(LivenessClockError):
                    timer.received(events)
                with self.assertRaises(LivenessClockError):
                    timer.poll()

    def test_watermark_orders_later_publications_and_rejects_clock_rollback(self):
        clock, buffer, _ = self.make()
        clock.at(5)
        self.assertEqual((), buffer.receive().events)
        clock.at(4)
        with self.assertRaisesRegex(ActivityUnavailable, "^monitor_unavailable$"):
            buffer.publish("activity")
        clock.at(6)
        self.assertTrue(buffer.receive().failed)

    def test_receipts_older_than_a_previous_watermark_and_oversized_batches_fail_closed(self):
        for events in ((("activity", 4 * SECOND),), (("activity", 5 * SECOND),) * (MAX_RECEIVED_EVENTS + 1)):
            with self.subTest(size=len(events)):
                clock, _, timer = self.make()
                timer.startup()
                clock.at(5)
                timer.poll()
                clock.at(6)
                with self.assertRaisesRegex(LivenessClockError, "^liveness_invalid_clock$"):
                    timer.received(events)

    def test_overflow_and_clock_failure_never_drop_old_events_to_claim_health(self):
        clock, buffer, _ = self.make()
        for _ in range(MAX_RECEIVED_EVENTS):
            buffer.publish("activity")
        with self.assertRaises(ActivityUnavailable):
            buffer.publish("activity")
        self.assertEqual(MAX_RECEIVED_EVENTS, len(buffer.events))
        failure = buffer.receive()
        self.assertTrue(failure.failed)
        self.assertEqual("activity_capacity_exceeded", failure.failure_code)
        self.assertEqual([], buffer.events)
        for failure in (ValueError("PRIVATE-CLOCK"), KeyboardInterrupt("PRIVATE-INTERRUPT")):
            buffer = ActivityBuffer(lambda: (_ for _ in ()).throw(failure))
            if isinstance(failure, Exception):
                observed = buffer.receive()
                self.assertTrue(observed.failed)
                self.assertEqual("activity_clock_unavailable", observed.failure_code)
            else:
                with self.assertRaises(KeyboardInterrupt) as caught:
                    buffer.receive()
                self.assertIs(failure, caught.exception)
            self.assertTrue(buffer.failed)

    def test_failure_codes_are_closed_and_the_first_failure_cannot_be_relabelled(self):
        buffer = ActivityBuffer()
        buffer.fail("PRIVATE-UNAPPROVED-REASON")
        buffer.fail("activity_clock_unavailable")
        batch = buffer.receive()
        self.assertEqual("activity_invalid", batch.failure_code)
        self.assertNotIn("PRIVATE", repr(batch))

    def test_reserved_completion_keeps_its_receipt_time_and_wins_later_cancel(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(9)
        buffer.complete()
        clock.at(25)
        status = timer.received(buffer.receive().events)
        self.assertTrue(status.terminal)
        self.assertIsNone(status.stop_reason)
        self.assertEqual(9000, status.elapsed_ms)
        self.assertEqual(status, timer.cancel())
        buffer.publish("activity")
        self.assertEqual((), buffer.receive().events)
        with self.assertRaises(ActivityUnavailable):
            buffer.complete()

    def test_reserved_completion_fits_a_full_activity_queue_but_cannot_beat_an_elapsed_timer(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        for _ in range(MAX_RECEIVED_EVENTS - 1):
            buffer.publish("activity")
        clock.at(9)
        buffer.complete()
        self.assertEqual(MAX_RECEIVED_EVENTS + 1, len(buffer.receive().events))
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(10)
        buffer.complete()
        stopped = timer.received(buffer.receive().events)
        self.assertFalse(stopped.terminal)
        self.assertEqual("work_silence", stopped.stop_reason)

    def test_reserved_stop_preserves_earlier_work_and_precedes_later_completion(self):
        clock, buffer, timer = self.make()
        buffer.publish("startup")
        clock.at(9)
        buffer.publish("activity")
        clock.at(12)
        buffer.stop("cancel_requested")
        clock.at(20)
        buffer.complete()
        clock.at(25)
        stopped = timer.received(buffer.receive().events)
        self.assertEqual("cancel_requested", stopped.stop_reason)
        self.assertEqual(12000, stopped.elapsed_ms)
        self.assertFalse(stopped.terminal)


if __name__ == "__main__":
    unittest.main()
