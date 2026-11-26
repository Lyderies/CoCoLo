"""S14b-6 (decision 349): the retention indexes belong to the consented records-upgrade step, on every path."""
import sqlite3
import unittest
from contextlib import closing

from test_framework import requirements
from tests import test_learning_cli as legacy_cli
from tests.test_run_retention import DAY, START, identity, store_product_row
from tests.test_account_launch_claims import downgrade_ledger_to_six
from codex_agents import run_retention
from codex_agents.learning_store import LearningLedger, LearningState

NAMES = ', '.join("'%s'" % n for n in run_retention.RETENTION_INDEX_NAMES)


def index_count(path):
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        return db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({NAMES})").fetchone()[0]


@requirements(why_not_scenario='Real cli subprocesses on a synthetic state; no provider is started.')
class UpgradeCreatesRetentionIndexesTests(unittest.TestCase):
    def setUp(self):
        self.cli = legacy_cli.LearningCliTests(methodName='runTest')
        self.cli.setUp()
        self.addCleanup(self.cli.tearDown)
        self.state_root = self.cli.state_root
        self.database = LearningState(self.state_root, create=False).database
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        store_product_row(ledger, self.cli.project_id, identity('old'))
        self.assertEqual(0, index_count(self.database))  # a plain open made none
        downgrade_ledger_to_six(ledger)
        ledger.close()

    @requirements('S14b-6')
    def test_cli_upgrade_ledger_creates_the_five_indexes_and_a_sweep_removes_a_record(self):
        # (d) the un-upgraded ledger stays without indexes until the owner approves.
        self.cli._run('learning-register', expected=2)
        self.assertEqual(0, index_count(self.database))
        self.cli._run('learning-register', '--upgrade-ledger')
        self.assertEqual(5, index_count(self.database))
        self.cli._stop_collector()
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            self.assertGreaterEqual(ledger.sweep_run_retention(now_ms=START + 91 * DAY), 1)
            self.assertIsNone(ledger._connection.execute(
                "SELECT 1 FROM records WHERE record_id='old'").fetchone())
        finally:
            ledger.close()

    @requirements('S14b-6')
    def test_registration_with_a_running_collector_keeps_the_indexes_of_the_consented_step(self):
        self.cli._run('learning-register', '--upgrade-ledger')  # leaves the collector running
        again = self.cli._run('learning-register', '--upgrade-ledger')  # the running-collector early return
        self.assertIsNone(again['ledger_upgraded_from'])
        self.assertTrue(again['active'])
        self.assertEqual(5, index_count(self.database))


if __name__ == '__main__':
    unittest.main()
