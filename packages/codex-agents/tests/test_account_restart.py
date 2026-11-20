"""S1-H1c: the account restarts itself only when stuck and nothing is running (PLAN H1c-1 to H1c-6)."""
import unittest

import tests.support  # noqa: F401
from codex_agents import account_restart

MINUTE = 60_000
NOW = 1_800_000_000_000


def lane(**changes):
    return {'faulted': False, 'kept': 0, 'busy': 0, 'queued': 0, 'last_failure': None, 'held_since_ms': None,
            **changes}


def lanes(**changes):
    notes = {name: lane() for name in ('signature', 'claims', 'observations', 'ingress', 'billing', 'probe_worker')}
    notes.update(changes)
    return notes


IDLE = {'run_slots': [], 'probes': []}


class RestartDecisionTests(unittest.TestCase):
    def decide(self, notes, idle=IDLE, previous=None):
        return account_restart.decide(notes, idle, previous, now_ms=NOW)

    def test_H1c_1_a_healthy_account_is_left_alone(self):
        self.assertEqual('ok', self.decide(lanes())['action'])

    def test_H1c_1_3_a_faulted_lane_with_nothing_running_restarts_with_its_cause(self):
        notes = lanes(ingress=lane(faulted=True, last_failure={'cause': 'ingress_cleanup_failed', 'type': 'OSError',
                                                              'at_ms': NOW - MINUTE}))
        decision = self.decide(notes)
        self.assertEqual(('restart', 'ingress', 'ingress_cleanup_failed'),
                         (decision['action'], decision['lane'], decision['cause']))

    def test_H1c_1_a_probe_worker_held_over_ten_minutes_is_stuck_and_under_it_is_not(self):
        held = lanes(probe_worker=lane(kept=1, held_since_ms=NOW - 11 * MINUTE))
        self.assertEqual(('restart', 'probe_worker', 'probe_worker_held'),
                         tuple(self.decide(held)[k] for k in ('action', 'lane', 'cause')))
        recent = lanes(probe_worker=lane(kept=1, held_since_ms=NOW - 9 * MINUTE))
        self.assertEqual('ok', self.decide(recent)['action'])

    def test_H1c_2_4_anything_running_means_wait_and_names_what_is_held(self):
        faulted = lanes(claims=lane(faulted=True))
        for idle, held in (({'run_slots': ['project-a'], 'probes': []}, {'run_slots': ['project-a']}),
                           ({'run_slots': [], 'probes': ['project-b']}, {'probes': ['project-b']})):
            with self.subTest(idle=idle):
                decision = self.decide(faulted, idle)
                self.assertEqual('wait', decision['action'])
                self.assertEqual(held, {k: v for k, v in decision['held'].items() if v})
        busy = lanes(claims=lane(faulted=True), observations=lane(busy=1))
        decision = self.decide(busy)
        self.assertEqual(('wait', {'observations': 1}), (decision['action'], decision['held']['lanes']))

    def test_H1c_2_kept_work_that_makes_it_stuck_does_not_count_as_running(self):
        notes = lanes(probe_worker=lane(kept=1, held_since_ms=NOW - 20 * MINUTE),
                      billing=lane(kept=1))
        self.assertEqual('restart', self.decide(notes)['action'])

    def test_H1c_5_at_most_one_self_restart_an_hour(self):
        notes = lanes(ingress=lane(faulted=True))
        recent = {'cause': 'account_restart', 'stopped_at_ms': NOW - 30 * MINUTE}
        self.assertEqual('suppressed', self.decide(notes, previous=recent)['action'])
        older = {'cause': 'account_restart', 'stopped_at_ms': NOW - 61 * MINUTE}
        self.assertEqual('restart', self.decide(notes, previous=older)['action'])
        other = {'cause': 'signal', 'stopped_at_ms': NOW - MINUTE}
        self.assertEqual('restart', self.decide(notes, previous=other)['action'])

    def test_missing_or_malformed_notes_never_restart(self):
        for notes in (None, {}, {'ingress': 'x'}, lanes(ingress={'faulted': True})):
            with self.subTest(notes=notes):
                self.assertEqual('ok', self.decide(notes)['action'])


if __name__ == '__main__':
    unittest.main()
