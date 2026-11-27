"""S14b-6 (decision 349): the retention indexes belong to the consented records-upgrade step, on every path."""
import sqlite3
import unittest
from contextlib import closing

from test_framework import requirements
from tests import learning_cli_support as legacy_cli
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
        self.cli = legacy_cli.LearningCliBase(methodName='runTest')
        self.cli.setUp()
        self.addCleanup(self.cli.tearDown)
        self.state_root = self.cli.state_root
        self.database = LearningState(self.state_root, create=False).database
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        store_product_row(ledger, self.cli.project_id, identity('old'))
        self.assertEqual(5, index_count(self.database))  # created with the fresh schema-18 ledger
        for name in run_retention.RETENTION_INDEX_NAMES:  # then made like a ledger that lacks them (0.8.67 had none)
            ledger._connection.execute(f'DROP INDEX {name}')
        self.assertEqual(0, index_count(self.database))
        ledger.close()

    def downgrade(self):
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        downgrade_ledger_to_six(ledger)
        ledger.close()

    @requirements('S14b-6')
    def test_cli_upgrade_ledger_creates_the_five_indexes_and_a_sweep_removes_a_record(self):
        self.downgrade()
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
        self.downgrade()
        self.cli._run('learning-register', '--upgrade-ledger')  # leaves the collector running
        again = self.cli._run('learning-register', '--upgrade-ledger')  # the running-collector early return
        self.assertIsNone(again['ledger_upgraded_from'])
        self.assertTrue(again['active'])
        self.assertEqual(5, index_count(self.database))

    def set_version(self, version):
        with closing(sqlite3.connect(self.database)) as db:  # a 0.8.66/0.8.67 ledger: B4t tables kept, version set back
            db.execute(f'PRAGMA user_version={version}')

    @requirements('S14b-6')
    def test_offline_register_upgrade_ledger_performs_17_to_18_with_the_indexes(self):
        self.set_version(17)
        self.cli._run('learning-register', '--upgrade-ledger')
        self.cli._stop_collector()
        self.assertEqual((18, 5), (self.user_version(), index_count(self.database)))

    @requirements('S14b-6')
    def test_a_schema_16_ledger_migrated_through_17_gets_the_indexes(self):
        self.set_version(16)
        self.cli._run('learning-register', '--upgrade-ledger')
        self.cli._stop_collector()
        self.assertEqual((18, 5), (self.user_version(), index_count(self.database)))

    @requirements('S14b-6')
    def test_a_failure_on_the_third_index_leaves_17_with_no_index_and_a_retry_gets_all_five(self):
        from unittest import mock
        self.set_version(17)
        state = LearningState(self.state_root, create=False)
        broken = (run_retention.PROTECTION_INDEXES[0], 'CREATE INDEX no_such_index ON no_such_table(x)',
                  *run_retention.PROTECTION_INDEXES[1:])
        with mock.patch.object(run_retention, 'PROTECTION_INDEXES', broken):
            with self.assertRaises(Exception):
                LearningLedger(state, allow_schema_upgrade=True)
        self.assertEqual((17, 0), (self.user_version(), index_count(self.database)))
        LearningLedger(state, allow_schema_upgrade=True).close()
        self.assertEqual((18, 5), (self.user_version(), index_count(self.database)))

    def committed_probes(self, count):
        """`count` finished probe requests: more than one hold-migration batch (64) of hold-source rows."""
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            for number in range(count):
                key = f'committed-{number:04d}'.ljust(64, '0')
                db.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                           (self.cli.project_id, key, 'p' * 64, 1, 's' * 64))
                db.execute('INSERT INTO account_billing_probe_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.cli.project_id, key, 'p' * 64, 'r' * 64, 'c' * 64, 1, 2, 'committed', None, None, None,
                     '[]', 's' * 64))
            db.commit()

    def state_of(self):
        with closing(sqlite3.connect(f'file:{self.database}?mode=ro', uri=True)) as db:
            pending = db.execute("SELECT 1 FROM sqlite_master WHERE name='hold_migration_progress'").fetchone()
            return db.execute('PRAGMA user_version').fetchone()[0], pending is not None, index_count(self.database)

    @requirements('S14b-6')
    def test_a_chunked_hold_migration_never_leaves_18_not_pending_without_the_five_indexes(self):
        from unittest import mock
        self.committed_probes(65)
        self.set_version(17)
        state = LearningState(self.state_root, create=False)
        broken = (run_retention.PROTECTION_INDEXES[0], 'CREATE INDEX no_such_index ON no_such_table(x)',
                  *run_retention.PROTECTION_INDEXES[1:])
        with mock.patch.object(run_retention, 'PROTECTION_INDEXES', broken):
            with self.assertRaises(Exception):
                LearningLedger(state, allow_schema_upgrade=True)
        version, pending, count = self.state_of()
        self.assertFalse(version == 18 and not pending and count != 5, (version, pending, count))
        LearningLedger(state, allow_schema_upgrade=True).close()
        self.assertEqual((18, False, 5), self.state_of())

    @requirements('S14b-6')
    def test_schema_16_without_consent_goes_no_further_than_17_and_creates_no_index(self):
        self.set_version(16)
        state = LearningState(self.state_root, create=False)
        try:
            LearningLedger(state, allow_schema_upgrade=False).close()
        except Exception:
            pass  # a refusal is as good as stopping at 17: what matters is the state left behind
        version, _, count = self.state_of()
        self.assertLessEqual(version, 17)
        self.assertEqual(0, count)
        LearningLedger(state, allow_schema_upgrade=True).close()
        self.assertEqual((18, False, 5), self.state_of())

    @requirements('S14b-6')
    def test_learning_migrate_status_on_an_offline_schema_17_ledger_changes_nothing(self):
        import subprocess
        import sys
        from tests.support import ORCHESTRATION_ROOT
        self.set_version(17)
        subprocess.run([sys.executable, str(ORCHESTRATION_ROOT / 'cli.py'), 'learning-migrate-project', '--status',
                        '--state-root', str(self.state_root), '--project-root', str(self.cli.project)],
                       text=True, capture_output=True, env=self.cli._environment(), check=False)
        self.assertEqual((17, 0), (self.user_version(), index_count(self.database)))

    @requirements('S14b-6')
    def test_a_fresh_ledger_is_created_with_the_five_indexes_and_retention_is_ready(self):
        import tempfile
        from pathlib import Path
        from tests.support import TEMP_ROOT
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            state = LearningState(Path(folder) / 'fresh')
            ledger = LearningLedger(state)
            project = ledger.register_project(Path(folder))
            store_product_row(ledger, project, identity('old'))
            ledger.close()
            self.assertEqual(5, index_count(state.database))  # schema 18 includes them from its creation
            reopened = LearningLedger(state)
            try:
                self.assertTrue(reopened._retention_ready)
                self.assertLess(reopened._run_retention_due_ms, run_retention.NEVER_DUE)
                self.assertEqual(1, reopened.sweep_run_retention(now_ms=START + 91 * DAY))
            finally:
                reopened.close()

    @requirements('S14b-6')
    def test_learning_migrate_that_performs_the_schema_step_creates_the_indexes(self):
        import subprocess
        import sys
        from tests.support import ORCHESTRATION_ROOT
        with closing(sqlite3.connect(self.database)) as db:  # as a 0.8.67 ledger: B4t tables kept, version set back
            db.execute('PRAGMA user_version=17')
        self.assertEqual(17, self.user_version())
        done = subprocess.run([sys.executable, str(ORCHESTRATION_ROOT / 'cli.py'), 'learning-migrate-project',
                               '--state-root', str(self.state_root), '--project-root', str(self.cli.project)],
                              text=True, capture_output=True, env=self.cli._environment(), check=False)
        self.assertEqual(18, self.user_version(), done.stdout + done.stderr)  # the open performed the 17 -> 18 step
        self.assertEqual(5, index_count(self.database), done.stdout + done.stderr)

    def user_version(self):
        with closing(sqlite3.connect(f'file:{self.database}?mode=ro', uri=True)) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]


if __name__ == '__main__':
    unittest.main()
