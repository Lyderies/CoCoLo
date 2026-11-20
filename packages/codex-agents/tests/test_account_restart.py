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

    def test_C3_kept_work_outside_the_stuck_lane_means_wait(self):
        notes = lanes(ingress=lane(faulted=True), billing=lane(kept=1))
        decision = self.decide(notes)
        self.assertEqual(('wait', {'billing': 1}), (decision['action'], decision['held']['lanes']))

    def test_H1c_2_kept_work_that_makes_it_stuck_does_not_count_as_running(self):
        notes = lanes(probe_worker=lane(kept=1, held_since_ms=NOW - 20 * MINUTE),
                      billing=lane(kept=1))
        self.assertEqual('restart', self.decide(notes)['action'])

    def test_H1c_5_C4_at_most_one_self_restart_an_hour_by_the_last_restart_kept(self):
        # C4 (review 040-s1-h1c): `previous` is the last self-restart (account-restart.json), not the
        # latest exit record, so an ordinary exit in between never resets the hour.
        notes = lanes(ingress=lane(faulted=True))
        self.assertEqual('suppressed', self.decide(notes, previous={'at_ms': NOW - 30 * MINUTE})['action'])
        self.assertEqual('suppressed', self.decide(notes, previous={'at_ms': NOW - 60 * MINUTE + 1})['action'])
        self.assertEqual('restart', self.decide(notes, previous={'at_ms': NOW - 61 * MINUTE})['action'])
        self.assertEqual('restart', self.decide(notes, previous=None)['action'])

    def test_missing_or_malformed_notes_never_restart(self):
        for notes in (None, {}, {'ingress': 'x'}, lanes(ingress={'faulted': True})):
            with self.subTest(notes=notes):
                self.assertEqual('ok', self.decide(notes)['action'])


class RestartRecordTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_C4_C5_the_last_self_restart_is_kept_owner_only_and_read_back(self):
        import os
        self.assertIsNone(account_restart.last_restart(self.root))
        self.assertTrue(account_restart.record_restart(self.root, lane='ingress', cause='ingress_cleanup_failed',
                                                       now_ms=NOW))
        self.assertEqual({'at_ms': NOW, 'lane': 'ingress', 'cause': 'ingress_cleanup_failed'},
                         account_restart.last_restart(self.root))
        self.assertEqual(0o600, os.stat(self.root / 'account-restart.json').st_mode & 0o777)

    def test_C4_a_restart_that_cannot_be_recorded_does_not_happen(self):
        from unittest import mock
        with mock.patch.object(account_restart.os, 'replace', side_effect=OSError('full')):
            self.assertFalse(account_restart.record_restart(self.root, lane='ingress', cause='x_failed', now_ms=NOW))
        self.assertIsNone(account_restart.last_restart(self.root))

    def test_a_malformed_record_reads_as_none(self):
        (self.root / 'account-restart.json').write_text('{"at_ms": "soon"}')
        self.assertIsNone(account_restart.last_restart(self.root))


class RestartCheckTests(unittest.TestCase):
    def test_H1c_6_unreadable_notes_never_restart_and_an_unreadable_ledger_waits(self):
        faulted = lanes(ingress=lane(faulted=True))

        def broken():
            raise RuntimeError('unreadable')
        self.assertEqual('ok', account_restart.check(broken, lambda: IDLE, None, now_ms=NOW)['action'])
        self.assertEqual('wait', account_restart.check(lambda: faulted, broken, None, now_ms=NOW)['action'])
        self.assertEqual('restart', account_restart.check(lambda: faulted, lambda: IDLE, None, now_ms=NOW)['action'])


if __name__ == '__main__':
    unittest.main()
