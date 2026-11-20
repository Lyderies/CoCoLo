"""O10 HS: a project's own service upgrades its history store's format (PLAN HS-1 to HS-3).

Found on the control project after installing 0.8.33: the 0.4.0 service could not open a format-16
store, because only the learning migration may upgrade it, and that runs only before the move and
fails silently. Integration stage: real history stores in temporary folders.
"""
import argparse
import os
import sqlite3
import stat
import unittest
from unittest import mock

from tests.test_project_orchestration import KEY, PROJECT_A, ProjectOrchestrationTests

from codex_agents import project_service
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity


class HistoryServiceUpgradeTests(ProjectOrchestrationTests):
    def format_16_store(self):
        from tests.project_schema_fixtures import legacy_registered_run_layout
        self.opened()
        legacy_registered_run_layout(self.store)
        self.store._connection.execute('PRAGMA user_version=16')
        before = self.store._connection.execute('SELECT * FROM project_orchestration_checkpoints').fetchall()
        self.store.close()
        return before

    def service_history(self):
        return project_service.service_history(
            self.directory, PROJECT_A, result_key=KEY,
            result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
            result_clock_ms=lambda: self.now, orchestration_limits=self.limits)

    def version(self, path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute('PRAGMA user_version').fetchone()[0]
        finally:
            connection.close()

    def test_HS_1_the_service_upgrades_a_format_16_store_whether_or_not_learning_moved(self):
        before = self.format_16_store()
        self.store = self.service_history()
        self.assertEqual(17, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, self.store._connection.execute(
            'SELECT * FROM project_orchestration_checkpoints').fetchall())  # nothing lost
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])

    def test_HS_2_the_store_is_kept_as_it_was_before_the_upgrade_once(self):
        self.format_16_store()
        self.store = self.service_history()
        backup = self.directory / 'history.sqlite3.before-17'
        self.assertTrue(backup.is_file())
        self.assertEqual(16, self.version(backup))  # the store as it was
        self.assertEqual(0o600, stat.S_IMODE(os.stat(backup).st_mode))
        made = backup.stat().st_mtime_ns
        self.store.close()
        self.store = self.service_history()  # a later start: already upgraded, the copy is not remade
        self.assertEqual(made, backup.stat().st_mtime_ns)

    def test_HS_1_a_current_store_needs_no_copy(self):
        self.store.close()
        self.store = self.service_history()
        self.assertFalse((self.directory / 'history.sqlite3.before-17').exists())

    def test_HS_1_an_ordinary_open_still_refuses_a_format_16_store(self):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()
        self.store = self.service_history()


class StartMigrationFailureTests(unittest.TestCase):
    """HS-3: a learning migration that fails during a service start is recorded, and the service starts."""

    def start(self, migration):
        import cli
        client = mock.Mock()
        client.learning_migration.return_value = {'moved': False, 'has_learning': True, 'drained': True,
                                                  'activation_pending': False}
        args = argparse.Namespace(state_root=None, project_root='/tmp/project-hs3')
        with mock.patch.object(cli, 'other_version_host', return_value=None), \
                mock.patch.object(cli, 'project_host_serving', return_value=False), \
                mock.patch.object(cli, '_start_collector_quietly'), \
                mock.patch.object(cli, '_compatible_collector', return_value=client), \
                mock.patch.object(cli, 'CollectorPaths'), \
                mock.patch.object(cli, '_migrate_learning', side_effect=migration), \
                mock.patch.object(cli, 'record_start_failure') as recorded, \
                mock.patch.object(cli, 'clear_start_failure'), \
                mock.patch.object(cli, 'ensure_project_host', return_value=True) as started:
            cli._start_project_host(args)
        return recorded, started

    def test_HS_3_a_failing_migration_is_recorded_and_the_service_still_starts(self):
        recorded, started = self.start(RuntimeError('boom'))
        started.assert_called_once()
        self.assertEqual('migration_failed', recorded.call_args.args[2])

    def test_HS_3_a_refused_migration_is_recorded_with_its_reason(self):
        recorded, started = self.start(lambda args: {'ready': False, 'reason_code': 'migration_not_drained'})
        started.assert_called_once()
        self.assertEqual('migration_not_drained', recorded.call_args.args[2])

    def test_HS_3_the_failure_is_kept_owner_only_shown_and_cleared(self):
        import tempfile
        from pathlib import Path
        from tests.support import TEMP_ROOT
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.learning_migration import clear_start_failure, record_start_failure, start_failure
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        from codex_agents.learning_store import LearningState
        state = CollectorPaths(LearningState(Path(temporary.name) / 'state').root)  # a real state root and key
        project = Path(temporary.name) / 'project'
        project.mkdir()
        self.assertIsNone(start_failure(state, project))
        record_start_failure(state, project, 'migration_not_drained')
        self.assertEqual('migration_not_drained', start_failure(state, project)['reason_code'])
        record_start_failure(state, project, 'not a fixed reason /x')  # never a path or message
        self.assertEqual('migration_failed', start_failure(state, project)['reason_code'])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(state.root / 'learning-migration-failures.json').st_mode))
        clear_start_failure(state, project)
        self.assertIsNone(start_failure(state, project))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited orchestration tests run in their own module."""
    suite = unittest.TestSuite()
    for case in (HistoryServiceUpgradeTests, StartMigrationFailureTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
