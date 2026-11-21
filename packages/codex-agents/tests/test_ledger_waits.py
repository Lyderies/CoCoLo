"""M4b E1a-T3: one request shares one deadline across its waits (PLAN E1a-2, E1a-T3).

Unit stage: the wait helper with a fake clock.
"""
import unittest
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import ledger_waits


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class SharedDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch.object(ledger_waits, 'time', self.clock))

    def test_E1a_T3_outside_a_request_each_wait_has_the_per_wait_limit(self):
        self.assertEqual(0.75, ledger_waits.wait_seconds())
        self.clock.now += 10
        self.assertEqual(0.75, ledger_waits.wait_seconds())

    def test_E1a_T3_successive_waits_in_one_request_share_its_deadline(self):
        with ledger_waits.request():
            self.assertEqual(0.75, ledger_waits.wait_seconds())  # handle
            self.clock.now += 0.5
            self.assertAlmostEqual(0.3, ledger_waits.wait_seconds())  # begin: what is left, not 0.75
            self.clock.now += 0.4
            self.assertEqual(0.0, ledger_waits.wait_seconds())  # spent: refused at once, never negative

    def test_E1a_T3_each_request_gets_a_fresh_deadline_and_restores_the_one_before(self):
        with ledger_waits.request():
            self.clock.now += 0.7
            with ledger_waits.request():  # a new collector frame on this thread
                self.assertEqual(0.75, ledger_waits.wait_seconds())
            self.assertAlmostEqual(0.1, ledger_waits.wait_seconds())  # the outer one's is back
        self.assertEqual(0.75, ledger_waits.wait_seconds())  # nothing later inherits a deadline

    def test_E1a_T3_a_deadline_is_restored_even_when_the_request_raises(self):
        with self.assertRaises(RuntimeError), ledger_waits.request():
            raise RuntimeError()
        self.clock.now += 5
        self.assertEqual(0.75, ledger_waits.wait_seconds())

    def test_E1a_T3_a_smaller_caller_budget_wins(self):
        with ledger_waits.request():
            self.assertEqual(0.2, ledger_waits.wait_seconds(0.2))


if __name__ == '__main__':
    unittest.main()
