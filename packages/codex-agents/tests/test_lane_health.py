"""S1-H1a: the account lanes' published health notes (DESIGN-0.4.0-S1-H1a.md, revisions 2-6).

Unit stage: the notes, tokens, failure records and held episodes, with no account service.
"""
import json
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import lane_health


class LaneHealthTests(unittest.TestCase):
    def setUp(self):
        self.lane = lane_health.Lane('signature')
        self.owner, self.worker = self.lane.slot('owner'), self.lane.slot('worker')

    def test_H1a_2_a_job_published_by_worker_and_owner_is_counted_once(self):
        token = lane_health.new_token()
        self.worker.publish(kept=frozenset({token}))
        self.assertEqual(1, self.lane.view()['kept'])
        self.owner.publish(kept=frozenset({token}), busy=2, queued=1)  # the owner acknowledges it
        view = self.lane.view()
        self.assertEqual((1, 2, 1), (view['kept'], view['busy'], view['queued']))
        self.worker.publish(kept=frozenset())
        self.assertEqual(1, self.lane.view()['kept'])

    def test_H1a_2_normal_work_is_busy_or_queued_never_kept(self):
        self.owner.publish(busy=3, queued=4)
        view = self.lane.view()
        self.assertEqual((0, 3, 4, False), (view['kept'], view['busy'], view['queued'], view['faulted']))

    def test_H1a_2_any_faulted_or_dead_slot_makes_the_lane_faulted(self):
        self.worker.publish(faulted=True)
        self.assertTrue(self.lane.view()['faulted'])
        other = lane_health.Lane('probe_worker')
        other.slot('worker').publish(dead=True)
        self.assertTrue(other.view()['faulted'])

    def test_H1a_1_the_newest_failure_is_kept_by_its_sequence_and_survives_later_work(self):
        self.lane.note_failure(self.worker, 'signature_worker_failed', ValueError('a secret message /x/y'))
        self.lane.note_failure(self.owner, 'claim_owner_failed', KeyError('another secret'))
        self.worker.publish(busy=1)  # an ordinary transition republishes the first slot
        self.worker.publish(busy=0, kept=frozenset())  # and its work completes and is removed
        failure = self.lane.view()['last_failure']
        self.assertEqual(('claim_owner_failed', 'KeyError'), (failure['cause'], failure['type']))
        self.assertIs(int, type(failure['at_ms']))
        self.assertNotIn('secret', json.dumps(self.lane.view()))

    def test_H1a_1_failures_without_an_exception_and_odd_type_names_are_fixed_markers(self):
        self.lane.note_failure(self.owner, 'reply_mismatch', None)
        self.assertEqual('invariant', self.lane.view()['last_failure']['type'])
        odd = type('Odd-Name With Spaces', (Exception,), {})
        self.lane.note_failure(self.owner, 'observation_worker_failed', odd())
        self.assertEqual('other', self.lane.view()['last_failure']['type'])

    def test_H1a_1_recording_a_failure_never_raises(self):
        with mock.patch.object(lane_health, '_wall_ms', side_effect=OSError('clock')):
            self.lane.note_failure(self.owner, 'probe_step_failed', RuntimeError())
        with mock.patch.object(lane_health, 'new_token', side_effect=RuntimeError('counter')):
            self.lane.note_failure(self.owner, 'probe_step_failed', RuntimeError())
        self.lane.note_failure(self.owner, 'not a fixed cause!', RuntimeError())  # dropped, no raise
        self.assertNotEqual('not a fixed cause!', (self.lane.view()['last_failure'] or {}).get('cause'))

    def test_H1a_2_one_held_episode_keeps_its_onset_until_every_contributor_has_left(self):
        clock = [1000]
        with mock.patch.object(lane_health, '_wall_ms', lambda: clock[0]):
            self.lane.enter_held()   # A at 1000
            clock[0] = 2000
            self.lane.enter_held()   # B at 2000
            self.assertEqual(1000, self.lane.view()['held_since_ms'])
            clock[0] = 500           # the wall clock steps back
            self.lane.leave_held()   # A leaves first
            self.assertEqual(1000, self.lane.view()['held_since_ms'])
            self.lane.leave_held()   # B leaves
            self.assertIsNone(self.lane.view()['held_since_ms'])
            clock[0] = 3000
            self.lane.enter_held()   # a new episode
            self.assertEqual(3000, self.lane.view()['held_since_ms'])

    def test_H1a_5_reading_takes_no_lock_and_changes_nothing(self):
        token = lane_health.new_token()
        self.owner.publish(kept=frozenset({token}), busy=1)
        self.lane.enter_held()
        before = self.lane.view()
        with self.lane._health_lock:  # a writer mid-update must not stop a reader
            result = []
            reader = threading.Thread(target=lambda: result.append(self.lane.view()))
            reader.start()
            reader.join(2)
        self.assertEqual([before], result)
        self.assertEqual(before, self.lane.view())

    def test_C1_publishing_never_raises(self):
        with mock.patch.object(lane_health, 'replace', side_effect=MemoryError()):
            self.owner.publish(busy=1)  # dropped, not raised
        self.assertEqual(0, self.lane.view()['busy'])

    def test_SD3_1_an_interruption_inside_instrumentation_is_never_swallowed(self):
        """O10 SD3-1: a stop signal's SystemExit (or Ctrl-C's KeyboardInterrupt) raised while a note is
        written must reach the caller; only an ordinary error is dropped (C1 as restated)."""
        def failing(error):
            return mock.Mock(side_effect=error)
        cases = {
            'safely': lambda error: lane_health.safely(failing(error)),
            'Slot.publish': lambda error: self._with(lane_health, 'replace', error, lambda: self.owner.publish(busy=1)),
            'note_failure': lambda error: self._with(lane_health, '_wall_ms', error,
                                                     lambda: self.lane.note_failure(self.owner, 'probe_step_failed', None)),
            'note_failure_named': lambda error: self._with(lane_health, '_wall_ms', error,
                                                           lambda: self.lane.note_failure_named(self.owner, 'probe_step_failed', None)),
            'enter_held': lambda error: self._with(lane_health, '_wall_ms', error, self.lane.enter_held),
            'leave_held': lambda error: self._with(lane_health, '_Episode', error, self.lane.leave_held),
        }
        for name, call in cases.items():
            for interruption in (SystemExit(0), KeyboardInterrupt()):
                with self.subTest(name=name, interruption=type(interruption).__name__):
                    with self.assertRaises(type(interruption)):
                        call(interruption)
            with self.subTest(name=name, error='ValueError'):
                call(ValueError('dropped'))  # an ordinary error is still dropped, never raised

    def _with(self, module, attribute, error, call):
        with mock.patch.object(module, attribute, side_effect=error):
            call()

    def test_C5_a_job_kept_is_never_also_busy(self):
        token = lane_health.new_token()
        self.owner.publish(busy_tokens=frozenset({token}))  # the owner still counts it busy
        self.worker.publish(kept=frozenset({token}))         # its worker has kept it
        view = self.lane.view()
        self.assertEqual((1, 0), (view['kept'], view['busy']))

    def test_tokens_are_unique_across_threads(self):
        seen, lock = [], threading.Lock()

        def take():
            for _ in range(500):
                token = lane_health.new_token()
                with lock:
                    seen.append(token)
        threads = [threading.Thread(target=take) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == '__main__':
    unittest.main()
