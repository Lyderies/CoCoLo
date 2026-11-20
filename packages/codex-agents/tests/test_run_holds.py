"""O0b run supervisor, stage RS-A: the register and the account's own holds (DESIGN-0.4.0-RS-A.md, RS-A-1 to
RS-A-8; written before the code, each fails first).

Integration stage: a real ledger with a claimed run (the test_active_run_capacity fixture); clocks are
patched, never slept.
"""
import unittest
from unittest import mock

from tests.test_active_run_capacity import ActiveRunCapacityTests

from codex_agents import learning_store
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.active_run_capacity import ActiveRunLimits


class RunHoldTests(ActiveRunCapacityTests):
    def counted(self, account, now, monotonic):
        """Whether the machine's one slot is taken, as admission sees it."""
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(1, 1)), \
                mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic), \
                self.ledger._account_transaction():
            try:
                self.ledger._check_active_capacity_locked("codex", account)
            except ClaimError:
                return True
            return False

    def sweep(self, now, monotonic):
        with mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic):
            return self.ledger.release_due_account_holds(limit=64)

    def test_RS_A_3_a_slot_freed_by_a_reboot_never_counts_again_as_uptime_grows(self):
        """CR-1: today the reboot inference frees the slot only while uptime is below the claim's old
        reference; once uptime passes it, the slot counts again with no clock change. Written once, for good."""
        self.case.claim()
        row = self.rows()[0]
        account = row["account_fingerprint"]
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        rebooted = row["clock_reference_ns"] - 1           # uptime below the claim's reference
        later = row["clock_reference_ns"] + 1               # uptime grown past it, still before the deadline
        self.assertFalse(self.counted(account, now, rebooted))  # today's rule frees it
        self.assertEqual(1, self.sweep(now, rebooted))      # the supervisor writes the decision
        released = self.rows()[0]
        self.assertIsNotNone(released["released_at_ms"])
        self.assertEqual("crashed", released["release_kind"])
        self.assertFalse(self.counted(account, now, later))  # and it never counts again

    def test_RS_A_3_a_live_slot_is_never_released_by_the_sweep(self):
        self.case.claim()
        row = self.rows()[0]
        self.assertEqual(0, self.sweep(row["deadline_at_ms"] - 1, row["clock_reference_ns"] + 1))
        self.assertIsNone(self.rows()[0]["released_at_ms"])
        self.assertTrue(self.counted(row["account_fingerprint"], row["deadline_at_ms"] - 1,
                                     row["clock_reference_ns"] + 1))

    def test_RS_A_1_each_account_hold_has_its_register_row(self):
        self.case.claim()
        holds = [dict(r) for r in self.ledger._connection.execute(
            "SELECT kind, state FROM run_holds ORDER BY kind")]
        self.assertIn({"kind": "run_slot", "state": "held"}, holds)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited capacity tests run in test_active_run_capacity."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(RunHoldTests):
        if name in RunHoldTests.__dict__:
            suite.addTest(RunHoldTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
