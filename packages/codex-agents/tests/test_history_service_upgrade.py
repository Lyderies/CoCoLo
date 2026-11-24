"""O10 HS: a project's own service upgrades its history store's format (PLAN HS-1 to HS-3).

Found on the control project after installing 0.8.33: the 0.4.0 service could not open a format-16
store, because only the learning migration may upgrade it, and that runs only before the move and
fails silently. Integration stage: real history stores in temporary folders.
"""
import argparse
import os
import sqlite3
import time
import sys
import stat
import unittest
from unittest import mock

from tests.test_project_orchestration import KEY, PROJECT_A, ProjectOrchestrationTests

from codex_agents import project_service
from codex_agents.project_history import ProjectHistory
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

    def copies(self):
        """HS-10: the copies made so far (a fresh one per upgrade attempt), never a temporary one."""
        # (requirement) SB: the copy is named for the format it upgrades to, now 20
        return sorted(path for path in self.directory.glob('history.sqlite3.before-21.*')  # B6: schema 21 (decision 131)
                      if not path.name.endswith('.partial'))

    def version(self, path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute('PRAGMA user_version').fetchone()[0]
        finally:
            connection.close()

    def test_HS_1_the_service_upgrades_a_format_16_store_whether_or_not_learning_moved(self):
        before = self.format_16_store()
        self.store = self.service_history()
        self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)
        self.assertEqual(before, self.store._connection.execute(
            'SELECT * FROM project_orchestration_checkpoints').fetchall())  # nothing lost
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])

    def test_HS_2_the_store_is_kept_as_it_was_before_the_upgrade(self):
        self.format_16_store()
        self.store = self.service_history()
        (backup,) = self.copies()
        self.assertEqual(16, self.version(backup))  # the store as it was
        self.assertEqual(0o600, stat.S_IMODE(os.stat(backup).st_mode))
        self.store.close()
        self.store = self.service_history()  # a later start: already upgraded, no new copy
        self.assertEqual([backup], self.copies())

    def copy_rows(self, path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute('SELECT * FROM project_orchestration_checkpoints').fetchall()
        finally:
            connection.close()

    def test_HS_5_the_offline_upgrade_keeps_the_copy_too(self):
        before = self.format_16_store()
        self.store = ProjectHistory(self.directory, PROJECT_A, result_key=KEY, allow_schema_upgrade=True,
                                    result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                                    result_clock_ms=lambda: self.now, orchestration_limits=self.limits)
        (copy,) = self.copies()
        self.assertEqual(16, self.version(copy))
        self.assertEqual([tuple(row) for row in before], self.copy_rows(copy))  # HS-7: the data, not just the file

    def test_HS_5_a_busy_store_is_refused_before_any_copy(self):
        import fcntl
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        lock = os.open(self.directory / 'history.lock', os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another writer holds the store
        with self.assertRaisesRegex(HistoryError, 'history_writer_busy'):
            self.service_history()
        self.assertEqual([], self.copies())
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.store = self.service_history()

    def test_HS_6_a_wrong_result_key_leaves_the_old_store_untouched(self):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            project_service.service_history(
                self.directory, PROJECT_A, result_key=b'w' * 32,
                result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                result_clock_ms=lambda: self.now, orchestration_limits=self.limits)
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))  # nothing written
        self.assertEqual([], self.copies())
        self.store = self.service_history()

    def test_HS_10_an_existing_copy_is_never_reused(self):
        before = self.format_16_store()
        # An impostor in a copy's place: version 16 and this project's id, but not the store (round 2).
        impostor = self.directory / 'history.sqlite3.before-21.1'  # (requirement) B6: format 21 (decision 131)
        connection = sqlite3.connect(impostor)
        connection.execute('CREATE TABLE history_identity(project_id TEXT)')
        connection.execute('INSERT INTO history_identity VALUES(?)', (PROJECT_A,))
        connection.execute('PRAGMA user_version=16')
        connection.commit()
        connection.close()
        os.chmod(impostor, 0o600)
        self.store = self.service_history()
        made = [path for path in self.copies() if path != impostor]
        self.assertEqual(1, len(made))  # a fresh copy, whatever was there
        self.assertEqual([tuple(row) for row in before], self.copy_rows(made[0]))

    def test_HS_10_a_copy_that_fails_leaves_nothing_and_upgrades_nothing(self):
        from codex_agents import project_history
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        real = sqlite3.connect

        def connect(path, *args, **kwargs):
            if '.partial' in str(path):
                raise KeyboardInterrupt  # any failure, not only an OSError
            return real(path, *args, **kwargs)
        with mock.patch.object(project_history.sqlite3, 'connect', connect):
            with self.assertRaises((HistoryError, KeyboardInterrupt)):
                self.service_history()
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))
        self.assertFalse(list(self.directory.glob('*.partial')))
        self.assertEqual([], self.copies())

    def test_HS_15_a_copy_that_fails_after_its_rename_leaves_nothing(self):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        real_fsync = os.fsync
        calls = []

        def fsync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:  # the folder's sync, after the copy is published
                raise OSError('disk full')
            return real_fsync(descriptor)
        with mock.patch.object(os, 'fsync', fsync):
            with self.assertRaises(HistoryError):
                self.service_history()
        self.assertEqual([], self.copies())
        self.assertFalse(list(self.directory.glob('*.partial')))
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))

    def test_HS_16_a_copy_never_replaces_an_existing_one(self):
        import time as time_module
        before = self.format_16_store()
        fixed = 1_700_000_000_000_000_000
        existing = self.directory / f'history.sqlite3.before-21.{fixed}'  # (requirement) B6: format 21 (decision 131)
        existing.write_bytes(b'keep me')
        with mock.patch.object(time_module, 'time_ns', return_value=fixed):
            self.store = self.service_history()
        self.assertEqual(b'keep me', existing.read_bytes())  # not replaced
        made = [path for path in self.copies() if path != existing]
        self.assertEqual(1, len(made))
        self.assertEqual([tuple(row) for row in before], self.copy_rows(made[0]))

    def test_HS_11_an_invalid_result_identity_is_refused_before_anything_is_written(self):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        connection = sqlite3.connect(self.directory / 'history.sqlite3')
        connection.execute("UPDATE result_identity SET last_observed_at_ms=-1")  # right key, bad field
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.service_history()
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))
        self.assertEqual([], self.copies())

    def refused_with(self, change):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        connection = sqlite3.connect(self.directory / 'history.sqlite3')
        connection.execute(change)
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.service_history()
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))
        self.assertEqual([], self.copies())

    def test_HS_11_another_project_identity_is_refused_before_anything_is_written(self):
        self.refused_with("UPDATE result_identity SET project_id='other-project'")

    def test_HS_11_a_malformed_key_check_is_refused_before_anything_is_written(self):
        self.refused_with("UPDATE result_identity SET key_check='not-a-digest'")

    def test_HS_11_two_identity_rows_are_refused_before_anything_is_written(self):
        self.refused_with("INSERT INTO result_identity SELECT 'other-project', key_check, last_observed_at_ms "
                          "FROM result_identity")

    def test_HS_18_a_copy_made_for_an_upgrade_that_fails_is_removed(self):
        from codex_agents import project_history
        self.format_16_store()
        with mock.patch.object(project_history, 'REGISTERED_RUN_SCHEMA', 'CREATE TABL broken(x)'):
            with self.assertRaises(Exception):
                self.service_history()
        self.assertEqual(16, self.version(self.directory / 'history.sqlite3'))  # nothing upgraded
        self.assertEqual([], self.copies())  # and no copy left for it
        self.store = self.service_history()  # a later attempt upgrades, with one copy
        self.assertEqual(1, len(self.copies()))

    def test_HS_1_a_current_store_needs_no_copy(self):
        self.store.close()
        self.store = self.service_history()
        self.assertEqual([], self.copies())

    def test_HS_1_an_ordinary_open_still_refuses_a_format_16_store(self):
        from codex_agents.history_contracts import HistoryError
        self.format_16_store()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()
        self.store = self.service_history()


class StartMigrationFailureTests(unittest.TestCase):
    """HS-3: a learning migration that fails during a service start is recorded, and the service starts."""

    def start(self, migration, record_error=None):
        import cli
        client = mock.Mock()
        client.learning_migration.return_value = {'moved': False, 'has_learning': True, 'drained': True,
                                                  'activation_pending': False}
        args = self.args = argparse.Namespace(state_root=None, project_root='/tmp/project-hs3')
        with mock.patch.object(cli, 'other_version_host', return_value=None), \
                mock.patch.object(cli, 'project_host_serving', return_value=False), \
                mock.patch.object(cli, '_start_collector_quietly'), \
                mock.patch.object(cli, '_compatible_collector', return_value=client), \
                mock.patch.object(cli, 'CollectorPaths'), \
                mock.patch.object(cli, '_migrate_learning', side_effect=migration), \
                mock.patch.object(cli, 'record_start_failure', side_effect=record_error) as recorded, \
                mock.patch.object(cli, 'clear_start_failure'), \
                mock.patch.object(cli, 'ensure_project_host', return_value=True) as started:
            cli._start_project_host(args)
        return recorded, started

    def test_HS_3_a_failing_migration_is_recorded_and_the_service_still_starts(self):
        recorded, started = self.start(RuntimeError('boom'))
        started.assert_called_once()
        self.assertEqual('migration_failed', recorded.call_args.args[2])
        self.assertEqual('migration_failed', self.args.learning_migration_failure)  # HS-8: in the reply too

    def test_HS_8_the_start_reply_carries_the_failure_even_if_keeping_it_fails(self):
        import cli
        self.start(RuntimeError('boom'), record_error=OSError('disk'))
        self.assertEqual('migration_failed', self.args.learning_migration_failure)
        self.assertEqual({'ready': True, 'serving': True, 'learning_migration_failure': 'migration_failed'},
                         cli._start_reply(True, 'migration_failed'))
        self.assertEqual({'ready': True, 'serving': True}, cli._start_reply(True, None))

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
        record_start_failure(state, project, 'some_other_reason')  # HS-9: a fixed list, not a pattern
        self.assertEqual('migration_failed', start_failure(state, project)['reason_code'])
        files = list((state.root / 'learning-migration-failures').glob('*.json'))  # HS-14 adds a .lock beside it
        self.assertEqual(1, len(files))  # HS-9: one file per project
        self.assertEqual(0o600, stat.S_IMODE(os.stat(files[0]).st_mode))
        other = Path(temporary.name) / 'other'
        other.mkdir()
        record_start_failure(state, other, 'migration_not_drained')
        clear_start_failure(state, project)
        self.assertIsNone(start_failure(state, project))
        self.assertEqual('migration_not_drained', start_failure(state, other)['reason_code'])  # not lost

    def state_and_project(self):
        import tempfile
        from pathlib import Path
        from tests.support import TEMP_ROOT
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.learning_store import LearningState
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        state = CollectorPaths(LearningState(Path(temporary.name) / 'state').root)
        project = Path(temporary.name) / 'project'
        project.mkdir()
        return state, project

    def test_HS_12_every_fixed_migration_reason_is_kept_and_a_malformed_one_reads_as_none(self):
        import json
        from codex_agents.learning_migration import _start_failure_path, record_start_failure, start_failure
        state, project = self.state_and_project()
        for reason in ('migration_inconsistent', 'migration_incomplete', 'migration_not_authorized'):
            record_start_failure(state, project, reason)
            self.assertEqual(reason, start_failure(state, project)['reason_code'])
        path = _start_failure_path(state, project)
        for bad in ([], {}, 3):
            path.write_text(json.dumps({'reason_code': bad, 'at_ms': 1}))
            os.chmod(path, 0o600)
            self.assertIsNone(start_failure(state, project))  # never raises

    def test_HS_13_clearing_never_erases_a_newer_failure(self):
        import time
        from codex_agents.learning_migration import clear_start_failure, record_start_failure, start_failure
        state, project = self.state_and_project()
        began = time.time_ns() // 1_000_000 - 1000  # this start began before the failure was kept
        record_start_failure(state, project, 'migration_not_drained')
        clear_start_failure(state, project, since_ms=began)
        self.assertEqual('migration_not_drained', start_failure(state, project)['reason_code'])
        clear_start_failure(state, project, since_ms=time.time_ns() // 1_000_000 + 1000)
        self.assertIsNone(start_failure(state, project))

    def test_HS_14_a_clear_that_cannot_take_the_lock_leaves_the_record(self):
        from codex_agents.learning_migration import (_start_failure_lock, clear_start_failure,
                                                     record_start_failure, start_failure)
        state, project = self.state_and_project()
        record_start_failure(state, project, 'migration_not_drained')
        with _start_failure_lock(state, project) as held:  # another start is keeping its failure now
            self.assertTrue(held)
            clear_start_failure(state, project, since_ms=1 << 60)
        self.assertEqual('migration_not_drained', start_failure(state, project)['reason_code'])
        clear_start_failure(state, project, since_ms=1 << 60)
        self.assertIsNone(start_failure(state, project))

    def test_HS_17_a_record_with_another_link_reads_as_none(self):
        from codex_agents.learning_migration import _start_failure_path, record_start_failure, start_failure
        state, project = self.state_and_project()
        record_start_failure(state, project, 'migration_not_drained')
        path = _start_failure_path(state, project)
        os.link(path, path.with_name('other-name.json'))
        self.assertIsNone(start_failure(state, project))

    def test_HS_4_a_service_that_ends_early_hands_its_reason_to_its_starter(self):
        from codex_agents import project_host_client
        state, project = self.state_and_project()
        entry = project / 'fake_host.py'
        entry.write_text('import json\nprint(json.dumps({"ready": False, "reason_code": "history_incompatible"}))\n')
        report = {}
        self.assertFalse(project_host_client._start(state, entry, project, lambda: False, 10, report=report))
        self.assertEqual('history_incompatible', report.get('reason_code'))

    def test_HS_19_reading_a_start_report_never_raises(self):
        from codex_agents.project_host_client import _ended_report
        state, project = self.state_and_project()
        path = project / 'report.json'
        for content in (b'\xff\xfe', b'[1]', b'[' * 100_000, b'{"reason_code": ["x"]}', b'null', b''):
            path.write_bytes(content)
            self.assertEqual({}, _ended_report(path))
        self.assertEqual({}, _ended_report(project / 'missing.json'))

    def test_HS_12_the_allowed_reasons_cover_every_fixed_migration_reason(self):
        import re
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents.learning_migration import START_REASONS
        sources = [(ORCHESTRATION_ROOT / 'codex_agents' / 'learning_migration.py').read_text(),
                   (ORCHESTRATION_ROOT / 'cli.py').read_text()]
        given = {reason for text in sources for reason in re.findall(r"""["'](migration_[a-z_]+)["']""", text)}
        self.assertLessEqual(given, START_REASONS)

    def test_HS_13_a_start_clears_with_its_own_start_time(self):
        import cli
        client = mock.Mock()
        client.learning_migration.return_value = {'moved': False, 'has_learning': True, 'drained': True,
                                                  'activation_pending': False}
        args = argparse.Namespace(state_root=None, project_root='/tmp/project-hs13')
        before = time.time_ns() // 1_000_000
        with mock.patch.object(cli, 'other_version_host', return_value=None), \
                mock.patch.object(cli, 'project_host_serving', return_value=False), \
                mock.patch.object(cli, '_start_collector_quietly'), \
                mock.patch.object(cli, '_compatible_collector', return_value=client), \
                mock.patch.object(cli, 'CollectorPaths'), \
                mock.patch.object(cli, '_migrate_learning', return_value={'ready': True}), \
                mock.patch.object(cli, 'record_start_failure'), \
                mock.patch.object(cli, 'clear_start_failure') as cleared, \
                mock.patch.object(cli, 'ensure_project_host', return_value=True):
            cli._start_project_host(args)
        since = cleared.call_args.kwargs['since_ms']
        self.assertTrue(before <= since <= time.time_ns() // 1_000_000)

    def test_HS_4_the_start_command_answers_the_service_reason_from_the_fixed_list(self):
        import contextlib
        import io
        import json
        import cli
        for given, answered in (('history_incompatible', 'history_incompatible'),
                                ('anything/else', 'project_service_unavailable')):
            def start(args, given=given):
                args.service_start_report = {'reason_code': given}
                return False
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['cli.py', 'learning-start-project-service',
                                                 '--project-root', '/tmp/project-hs4']), \
                    mock.patch.object(cli, '_start_project_host', side_effect=start), \
                    contextlib.redirect_stdout(output):
                code = cli.main()
            self.assertEqual(2, code)
            self.assertEqual(answered, json.loads(output.getvalue())['reason_code'])

    def test_HS_9_a_special_file_in_the_way_never_hangs_a_start(self):
        import tempfile
        import threading
        from pathlib import Path
        from tests.support import TEMP_ROOT
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.learning_store import LearningState
        from codex_agents.learning_migration import _start_failure_path, record_start_failure, start_failure
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        state = CollectorPaths(LearningState(Path(temporary.name) / 'state').root)
        project = Path(temporary.name) / 'project'
        project.mkdir()
        path = _start_failure_path(state, project)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.mkfifo(path, 0o600)  # a reader would block on this without O_NONBLOCK
        done = []
        thread = threading.Thread(target=lambda: done.append((start_failure(state, project),
                                                              record_start_failure(state, project, 'migration_failed'))),
                                  daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual([(None, None)], done)


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
