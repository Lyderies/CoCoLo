"""Runs an earlier version left in the records must not hold capacity, or the learning move, for ever.

Upgrading a 0.2.x ledger turns every historical run into an unreleased
`legacy_unknown` reservation (there is no proof its cleanup was confirmed). Those
rows had no deadline, so nothing could ever count them as finished: two of them
blocked every new child, and any of them blocked the move of the project's
learning history (`migration_not_drained`). A 0.2.x child ran inside its own CLI
command under a route deadline of at most six hours, so one claimed more than
LEGACY_RUN_MAX_MS ago cannot still be running; it stops counting, while its row
stays unreleased, exactly like a crashed claim of this version.
"""

import time
import unittest

from tests import test_learning_store as legacy
from tests.test_account_launch_claims import downgrade_ledger_to_six
from codex_agents import active_run_capacity, learning_store

HOUR = 60 * 60 * 1000


class LegacyCapacityUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.project_a_id

    def upgrade_with_runs(self, *ages_ms):
        ledger = self.fixture.ledger
        downgrade_ledger_to_six(ledger)
        now = time.time_ns() // 1_000_000
        for number, age in enumerate(ages_ms):
            ledger._connection.execute(
                "INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.project, f"{number:064x}", f"legacy-run-{number}", "b" * 64, "c" * 64, "d" * 64,
                 "e" * 64, "codex-host-v1", now - age, now - age + 90 * 24 * HOUR,
                 "consumed" if number % 2 else "pending")).close()
        ledger.close()
        self.fixture.ledger = learning_store.LearningLedger(
            self.fixture.state, client_capabilities=self.fixture.clients,
            allow_schema_upgrade=True, backup_before_upgrade=True)
        return self.fixture.ledger

    def counted(self, ledger):
        crashed, parameters = active_run_capacity.crashed_claim(
            "", ledger._clock_ms(), ledger._capacity_clock_reference())
        return ledger._connection.execute(
            f"SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL AND NOT {crashed}",
            parameters).fetchone()[0]

    def test_old_runs_from_an_earlier_version_neither_hold_capacity_nor_block_the_move(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, 3 * 24 * HOUR, 2 * learning_store.LEGACY_RUN_MAX_MS)
        rows = ledger._connection.execute(
            "SELECT COUNT(*) FROM active_run_capacity WHERE kind='legacy_unknown' AND released_at_ms IS NULL"
        ).fetchone()[0]
        self.assertEqual(3, rows)  # Kept, unreleased: no cleanup is invented.
        self.assertEqual(0, self.counted(ledger))
        self.assertTrue(ledger.drained())

    def test_a_recent_run_from_an_earlier_version_still_counts_until_it_cannot_be_running(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, HOUR)
        self.assertEqual(1, self.counted(ledger))
        self.assertFalse(ledger.drained())
        later = ledger._clock_ms() + learning_store.LEGACY_RUN_MAX_MS
        ledger._clock_ms = lambda: later
        self.assertEqual(0, self.counted(ledger))
        self.assertTrue(ledger.drained())

    def test_the_bound_covers_every_earlier_versions_longest_run(self):
        self.assertGreaterEqual(learning_store.LEGACY_RUN_MAX_MS,
                                6 * HOUR + active_run_capacity.CRASHED_CLAIM_GRACE_MS)


if __name__ == "__main__":
    unittest.main()
