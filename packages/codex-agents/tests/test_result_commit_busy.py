"""M4b E1a-T1: a result commit refused by a busy ledger leaves nothing behind and can be retried (PLAN E1a-2,
E1a-T1; review 040-m4b-e1a-code-2).

Integration stage: the real ledger and an admitted, bound host run (the test_synchronized_races fixture).
"""
import threading
import unittest

from tests.test_synchronized_races import DuplicateHostResultRaceTests

from codex_agents.learning_contracts import keyed_digest
from codex_agents.sql_read_budget import SQLReadError


class ResultCommitBusyTests(DuplicateHostResultRaceTests):
    def hold_lock(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(30)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        return release, thread

    def binding_state(self, handle):
        digest = keyed_digest(self.case.state.digest_key(), handle)
        return self.ledger._connection.execute(
            "SELECT binding_state FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone()[0]

    def test_E1a_T1_a_refused_commit_removes_its_new_file_consumes_nothing_and_a_retry_succeeds(self):
        handle, arguments = self.opened(0)
        files = self.kept_files()
        stamp = self.case.clock + 2
        release, thread = self.hold_lock()
        with self.assertRaises(SQLReadError) as refused:
            self.ledger.commit_host_result(self.project, now_ms=stamp, **arguments)
        self.assertEqual("sql_read_busy", refused.exception.reason_code)
        release.set()
        thread.join(5)
        self.assertEqual(files, self.kept_files())  # the file this attempt made is gone
        self.assertNotEqual("terminal", self.binding_state(handle))  # nothing consumed
        self.assertFalse(self.ledger._connection.in_transaction)
        committed = self.ledger.commit_host_result(self.project, now_ms=stamp, **arguments)
        self.assertEqual(sorted([*files, "host_result_" + committed["host_outcome_digest"] + ".json"]),
                         self.kept_files())
        self.assertEqual("terminal", self.binding_state(handle))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited race tests run in test_synchronized_races."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ResultCommitBusyTests):
        if name in ResultCommitBusyTests.__dict__:
            suite.addTest(ResultCommitBusyTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
