"""S1-H1a: the project's account slot counts for learning-status (DESIGN-0.4.0-S1-H1a.md, revision 4-6).

Read only, never waiting on the ledger lock, under a fixed SQL work budget.
"""
import threading
import time
import unittest
from unittest import mock

import tests.test_learning_store as fixture_module
from codex_agents import learning_store


class AccountSlotHealthTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.LearningStoreTests("test_registration_and_scoped_reads_are_partitioned")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.ledger, self.project = self.fixture.ledger, self.fixture.project_a_id

    def test_a_fresh_project_has_no_slot_held(self):
        self.assertEqual({'available': True, 'runs_past_deadline': 0, 'runs_held_without_deadline': 0,
                          'probes_held': 0, 'probes_past_deadline': 0, 'probe_deadlines': 'wall_clock'},
                         self.ledger.account_slot_health(self.project))

    def test_a_held_ledger_lock_is_never_waited_on(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(10)
        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        self.assertEqual({'available': False, 'reason': 'ledger_busy'}, self.ledger.account_slot_health(self.project))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_a_spent_budget_is_unavailable_never_a_partial_count_and_later_sql_works(self):
        with mock.patch.object(learning_store, 'SLOT_HEALTH_STEPS', 1):
            self.assertEqual({'available': False, 'reason': 'budget_spent'},
                             self.ledger.account_slot_health(self.project))
        self.assertTrue(self.ledger.account_slot_health(self.project)['available'])
        self.assertEqual(1, self.ledger._connection.execute('SELECT 1').fetchone()[0])

    def test_reading_changes_nothing(self):
        before = self.ledger._connection.total_changes
        self.ledger.account_slot_health(self.project)
        self.assertEqual(before, self.ledger._connection.total_changes)


if __name__ == '__main__':
    unittest.main()
