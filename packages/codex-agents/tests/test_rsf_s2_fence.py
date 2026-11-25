"""RSF-S2: real historical executable and paused/offline persistence owners."""
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from test_framework import requirements
from codex_agents import project_history
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_store import LearningLedger, LearningState, LedgerError
from tests.learning_support import event_record
from codex_agents.learning_contracts import canonical_json

PROJECT = 'prj_' + '1' * 32
HERE = Path(__file__).parent
RUNTIME = HERE.parent / 'native-package/.codex/orchestration'


class FenceTests(unittest.TestCase):
    __why_not_scenario__ = "Real SQLite owners and real predecessor processes on isolated directories."
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='s2', dir=os.environ.get('TMPDIR', '/tmp'))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = LearningState(self.base / 's')
        self.directory = self.base / 'h'
        self.directory.mkdir(mode=0o700)

    def owners(self):
        ledger = LearningLedger(self.state)
        history = project_history.ProjectHistory(self.directory, PROJECT)
        self.addCleanup(ledger.close)
        self.addCleanup(history.close)
        return ledger, history

    def epoch(self, owner):
        return tuple(owner._connection.execute(
            'SELECT epoch,old_digest,new_digest FROM runtime_takeover WHERE singleton=1').fetchone())

    @requirements('RSF-S2-2', 'RSF-S2-6')
    def test_epoch_advances_and_signed_records_are_unchanged(self):
        ledger, history = self.owners()
        first = self.epoch(ledger), self.epoch(history)
        rows = [list(o._connection.execute('SELECT * FROM records')) for o in (ledger, history)]
        ledger.close()
        history.close()
        ledger, history = self.owners()
        for index, owner in enumerate((ledger, history)):
            current = self.epoch(owner)
            self.assertEqual(first[index][0] + 1, current[0])
            self.assertEqual(first[index][2], current[1])
            self.assertEqual(rows[index], list(owner._connection.execute('SELECT * FROM records')))

    @requirements('RSF-S2-1', 'RSF-S2-5')
    def test_lost_or_replaced_lifetime_lock_refuses_admissions(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                case = FenceTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                ledger, history = case.owners()
                for owner, descriptor, path, mutation in (
                    (ledger, ledger._writer_descriptor, case.state.lock,
                     lambda: ledger.register_project(case.directory)),
                    (history, history._writer_fd, case.directory / 'history.lock',
                     lambda: history._connection.execute('UPDATE history_identity SET next_source_sequence=2')),
                ):
                    if replace:
                        path.rename(path.with_suffix('.retired'))
                        path.touch(mode=0o600)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    before = owner._connection.total_changes
                    with self.assertRaisesRegex((LedgerError, HistoryError), r'\./setup\.sh --update'):
                        mutation()
                    self.assertEqual(before, owner._connection.total_changes)
                ledger.close()
                history.close()
                # A replaced lock path is safe only for this isolated fixture's next case.

    @requirements('RSF-S2-3', 'RSF-S2-5')
    def test_epoch_gate_covers_connection_cursor_batch_script_and_maintenance(self):
        ledger, history = self.owners()
        for owner, database, sql in (
            (ledger, self.state.database, "UPDATE projects SET created_at_ms=created_at_ms+1"),
            (history, self.directory / 'history.sqlite3', 'UPDATE history_identity SET next_source_sequence=2'),
        ):
            self.epoch(owner)  # fail red before executing fixture changes on an unfenced store
            with sqlite3.connect(database) as other:
                other.execute('UPDATE runtime_takeover SET epoch=epoch+1')
            connection = owner._connection
            before = connection.total_changes
            for mutation in (
                lambda: connection.execute(sql),
                lambda: connection.cursor().execute(sql),
                lambda: connection.execute('SELECT 1').execute(sql),
                lambda: connection.execute('SELECT 1').executemany(sql, [(), ()]),
                lambda: connection.executemany(sql, [(), ()]),
                lambda: connection.executescript(sql + ';'),
                lambda: connection.execute('VACUUM'),
                lambda: connection.execute('PRAGMA journal_mode=DELETE'),
                lambda: connection.execute('BEGIN IMMEDIATE'),
            ):
                with self.subTest(owner=type(owner).__name__, mutation=mutation):
                    with self.assertRaisesRegex((LedgerError, HistoryError), r'\./setup\.sh --update'):
                        mutation()
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(before, connection.total_changes)

    @requirements('RSF-S2-3')
    def test_commit_rechecks_epoch_and_rolls_back(self):
        ledger, history = self.owners()
        for owner, sql in ((ledger, 'DELETE FROM projects'),
                           (history, 'UPDATE history_identity SET next_source_sequence=2')):
            epoch = self.epoch(owner)
            connection = owner._connection
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(sql)
            # Trusted native fault injection inside the transaction: a missing commit check would persist 2.
            sqlite3.Connection.execute(connection, 'UPDATE runtime_takeover SET epoch=epoch+1')
            with self.assertRaises((LedgerError, HistoryError)):
                connection.commit()
            connection.rollback()
            self.assertEqual(epoch, self.epoch(owner))
        self.assertEqual(1, history._connection.execute('SELECT next_source_sequence FROM history_identity').fetchone()[0])

    @requirements('RSF-S2-1', 'RSF-S2-3')
    def test_paused_ledger_writer_cannot_resume_after_successor(self):
        ledger = LearningLedger(self.state)
        self.addCleanup(ledger.close)
        ready, resume = threading.Event(), threading.Event()
        errors = []
        def write():
            ready.set()
            resume.wait(5)
            try:
                ledger.register_project(self.directory)
            except BaseException as error:
                errors.append(error)
        writer = threading.Thread(target=write)
        writer.start()
        self.assertTrue(ready.wait(5))
        # Simulate a lost lock while the paused process retains its old connection.
        fcntl.flock(ledger._writer_descriptor, fcntl.LOCK_UN)
        successor = LearningLedger(self.state)
        self.addCleanup(successor.close)
        resume.set()
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(1, len(errors), errors)
        self.assertIsInstance(errors[0], LedgerError)
        self.assertEqual(0, successor._connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0])

    @requirements('RSF-S2-1', 'RSF-S2-5')
    def test_unsafe_hard_link_lock_is_refused_before_writing_an_owner_stamp(self):
        retained = self.base / 'retained-private-file'
        retained.write_bytes(b'original bytes must survive')
        retained.chmod(0o600)
        os.link(retained, self.state.lock)
        with self.assertRaisesRegex(LedgerError, r'\./setup\.sh --update'):
            LearningLedger(self.state)
        self.assertEqual(b'original bytes must survive', retained.read_bytes())

    @requirements('RSF-S2-1', 'RSF-S2-3')
    def test_another_lock_holder_is_not_mistaken_for_our_lifetime_ownership(self):
        ledger, history = self.owners()
        for owner, descriptor, path, sql in (
            (ledger, ledger._writer_descriptor, self.state.lock, 'DELETE FROM projects'),
            (history, history._writer_fd, self.directory / 'history.lock',
             'UPDATE history_identity SET next_source_sequence=99'),
        ):
            other = os.open(path, os.O_RDWR)
            try:
                owner._connection.execute('BEGIN IMMEDIATE')
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # No epoch changed yet: proof of somebody's lock is not proof of ours.
                with self.subTest(owner=type(owner).__name__), self.assertRaises((LedgerError, HistoryError)):
                    owner._connection.execute(sql)
            finally:
                owner._connection.rollback()
                os.close(other)

    @requirements('RSF-S2-2', 'RSF-S2-6')
    def test_epoch_is_inside_the_schema_step_before_each_legacy_adoption_and_rolls_back_with_it(self):
        from codex_agents import project_holds_schema, run_holds_schema
        class Cut(RuntimeError):
            pass
        for kind in ('ledger', 'history'):
            for cut in (True, False):
                with self.subTest(kind=kind, cut=cut):
                    case = FenceTests()
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    if kind == 'ledger':
                        old = LearningLedger(case.state)
                        old.close()
                        database, prior = case.state.database, 14
                        with sqlite3.connect(database) as connection:
                            for object_kind, name, _ in run_holds_schema.RUN_HOLDS_OBJECTS:
                                if object_kind == 'trigger':
                                    connection.execute('DROP TRIGGER IF EXISTS ' + name)
                            connection.execute('DROP TABLE run_holds')
                            connection.execute('DROP TABLE runtime_takeover')
                            connection.execute('ALTER TABLE records DROP COLUMN probe_id')
                            connection.execute('PRAGMA user_version=14')
                        original = LearningLedger._initialize_run_holds_locked
                        def adopt(owner):
                            observe(owner._connection)
                            original(owner)
                            if cut:
                                raise Cut()
                        patch = mock.patch.object(LearningLedger, '_initialize_run_holds_locked', adopt)
                        def open_owner():
                            return LearningLedger(case.state, allow_schema_upgrade=True)
                    else:
                        database, prior = case.directory / 'history.sqlite3', 17
                        with sqlite3.connect(database) as connection:
                            connection.executescript(project_history._SCHEMA_V17)
                            connection.execute('INSERT INTO history_identity VALUES(?,1,1)', (PROJECT,))
                            connection.execute('PRAGMA application_id=' + str(project_history._APPLICATION_ID))
                            connection.execute('PRAGMA user_version=17')
                        database.chmod(0o600)
                        original = project_holds_schema.adopt_project_holds
                        def adopt(connection, now):
                            observe(connection)
                            original(connection, now)
                            if cut:
                                raise Cut()
                        patch = mock.patch.object(project_holds_schema, 'adopt_project_holds', adopt)
                        def open_owner():
                            return project_history.ProjectHistory(case.directory, PROJECT, allow_schema_upgrade=True)
                    seen = []
                    def observe(connection):
                        self.assertTrue(connection.in_transaction)
                        row = tuple(connection.execute('SELECT epoch,old_digest,new_digest FROM runtime_takeover').fetchone())
                        self.assertEqual((1, None), row[:2])
                        self.assertRegex(row[2], '^[0-9a-f]{64}$')
                        seen.append(row)
                    with patch:
                        if cut:
                            with self.assertRaises(Cut):
                                open_owner()
                        else:
                            owner = open_owner()
                            self.assertEqual(seen[0], self.epoch(owner))
                            owner.close()
                    self.assertEqual(1, len(seen))
                    with sqlite3.connect(database) as connection:
                        self.assertEqual(prior if cut else (17 if kind == 'ledger' else 22),
                                         connection.execute('PRAGMA user_version').fetchone()[0])
                        self.assertEqual(0 if cut else 1, connection.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE name='runtime_takeover'").fetchone()[0])

    @requirements('RSF-S2-2', 'RSF-S2-6')
    def test_current_format_missing_epoch_refuses_without_recreating_it(self):
        ledger, history = self.owners()
        ledger.close()
        history.close()
        for kind, database in (('ledger', self.state.database), ('history', self.directory / 'history.sqlite3')):
            with sqlite3.connect(database) as connection:
                connection.execute('DELETE FROM runtime_takeover')
            with self.subTest(kind=kind), self.assertRaises((LedgerError, HistoryError)):
                if kind == 'ledger':
                    LearningLedger(self.state)
                else:
                    project_history.ProjectHistory(self.directory, PROJECT)
            with sqlite3.connect(database) as connection:
                self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM runtime_takeover').fetchone()[0])

    @requirements('RSF-S2-1', 'RSF-S2-3')
    def test_paused_project_process_cannot_resume_after_takeover(self):
        source = (
            f"import sys,fcntl; sys.path.insert(0,{str(RUNTIME)!r}); from pathlib import Path; "
            "from codex_agents.project_history import ProjectHistory; "
            f"owner=ProjectHistory(Path({str(self.directory)!r}),{PROJECT!r}); "
            "fcntl.flock(owner._writer_fd,fcntl.LOCK_UN); print('paused',flush=True); sys.stdin.readline(); "
            "\ntry: owner._connection.execute('UPDATE history_identity SET next_source_sequence=99')"
            "\nexcept Exception as error: print(str(error),flush=True)"
            "\nelse: print('STALE WRITE COMMITTED',flush=True)"
            "\nfinally: owner.close()"
        )
        process = subprocess.Popen([sys.executable, '-B', '-c', source], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertTrue(select.select([process.stdout], [], [], 15)[0])
        self.assertEqual('paused', process.stdout.readline().strip())
        successor = project_history.ProjectHistory(self.directory, PROJECT)
        self.addCleanup(successor.close)
        stdout, stderr = process.communicate('resume\n', timeout=15)
        self.assertEqual(0, process.returncode, stderr)
        self.assertIn('./setup.sh --update', stdout)
        self.assertNotIn('STALE WRITE COMMITTED', stdout)
        self.assertEqual(1, successor._connection.execute('SELECT next_source_sequence FROM history_identity').fetchone()[0])


class HistoricalExecutableTests(unittest.TestCase):
    epoch = FenceTests.epoch
    __why_not_scenario__ = FenceTests.__why_not_scenario__
    # Explicit methods only: do not inherit and repeat the store tests.
    def setUp(self):
        FenceTests.setUp(self)
        top = HERE.parents[2]
        archive = subprocess.run(['git', '-C', str(top), 'archive', '8d4a7ce3',
                                  'packages/codex-agents/native-package'], capture_output=True, check=True)
        self.old = self.base / 'old'
        self.old.mkdir()
        subprocess.run(['tar', '-x', '-C', str(self.old)], input=archive.stdout, check=True)
        self.old_runtime = self.old / 'packages/codex-agents/native-package/.codex/orchestration'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON'))}
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.env['TMPDIR'] = str(self.base)

    def old_command(self, kind, *, wait=False):
        setup = ("from codex_agents.learning_store import LearningState,LearningLedger; "
                 f"owner=LearningLedger(LearningState(Path({str(self.state.root)!r})))" if kind == 'ledger' else
                 "from codex_agents.project_history import ProjectHistory; "
                 f"owner=ProjectHistory(Path({str(self.directory)!r}),{PROJECT!r})")
        if wait:
            raw = canonical_json(event_record('evt_historical'))
            if kind == 'ledger':
                setup += f"; project=owner.register_project(Path({str(self.directory)!r})); owner.append(project,{event_record('evt_historical')!r},producer='hook')"
            else:
                setup += f"; owner.append_record({raw!r},source='hook',ingested_at_ms=1000)"
        return [sys.executable, '-B', '-c',
                f"import sys; sys.path.insert(0,{str(self.old_runtime)!r}); from pathlib import Path; "
                + setup + "; print('ready',flush=True); "
                + ("sys.stdin.readline(); owner.close()" if wait else "owner.close()")]

    @requirements('RSF-S2-1', 'RSF-S2-2', 'RSF-S2-4', 'RSF-S2-6')
    def test_real_0855_owners_block_migration_then_cannot_reopen(self):
        for kind in ('ledger', 'history'):
            with self.subTest(kind=kind):
                process = subprocess.Popen(self.old_command(kind, wait=True), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'old owner did not start')
                self.assertEqual('ready', process.stdout.readline().strip())
                database = self.state.database if kind == 'ledger' else self.directory / 'history.sqlite3'
                with sqlite3.connect(database) as check:
                    before = check.execute('PRAGMA user_version').fetchone()[0]
                with self.assertRaises((LedgerError, HistoryError)):
                    self.open_successor(kind)
                with sqlite3.connect(database) as check:
                    self.assertEqual(before, check.execute('PRAGMA user_version').fetchone()[0])
                    self.assertIsNone(check.execute("SELECT name FROM sqlite_master WHERE name='runtime_takeover'").fetchone())
                process.communicate('\n', timeout=15)
                self.assertEqual(0, process.returncode)
                with sqlite3.connect(database) as check:
                    records = check.execute('SELECT content,content_digest FROM records').fetchall()
                self.assertEqual(1, len(records))
                successor = self.open_successor(kind)
                self.assertEqual(1, self.epoch(successor)[0])
                self.assertEqual(records, [tuple(row) for row in successor._connection.execute('SELECT content,content_digest FROM records')])
                successor.close()
                refused = subprocess.run(self.old_command(kind), capture_output=True, text=True,
                                         timeout=15, env=self.env)
                self.assertNotEqual(0, refused.returncode, refused.stdout + refused.stderr)
                self.assertRegex(refused.stderr, 'newer|incompatible')

    def open_successor(self, kind):
        if kind == 'ledger':
            return LearningLedger(self.state, allow_schema_upgrade=True, backup_before_upgrade=True)
        return project_history.ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)

    @requirements('RSF-S2-4')
    def test_old_offline_cli_cannot_reopen_migrated_ledger(self):
        ledger = LearningLedger(self.state)
        ledger.close()
        before = self.state.database.read_bytes()
        # Give the genuine old offline CLI its own valid runtime authorization,
        # so it reaches the schema gate rather than stopping at missing authority.
        authorize = subprocess.run([sys.executable, '-B', '-c',
            f"import sys; sys.path.insert(0,{str(self.old_runtime)!r}); from pathlib import Path; "
            "from codex_agents.learning_store import LearningState,authorize_collector_runtime; "
            f"authorize_collector_runtime(LearningState(Path({str(self.state.root)!r})),Path({str(self.old_runtime / 'cli.py')!r}))"],
            capture_output=True, text=True, timeout=15, env=self.env)
        self.assertEqual(0, authorize.returncode, authorize.stderr)
        result = subprocess.run([sys.executable, '-B', str(self.old_runtime / 'cli.py'),
                                 'learning-backup-project', '--state-root', str(self.state.root),
                                 '--project-root', str(self.directory), '--file-id', 'old_backup'],
                                capture_output=True, text=True, timeout=20, env=self.env)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('newer', result.stdout + result.stderr)
        self.assertEqual(before, self.state.database.read_bytes())

