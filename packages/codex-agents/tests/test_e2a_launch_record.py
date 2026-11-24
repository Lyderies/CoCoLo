"""RS-E E2a-1..4: the setup-bound launch record in the collector ledger, its launch-target check and copy pinning.

Real ledgers, a real installed package and its protected copy, and the real collector over its socket (short TMPDIR).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import test_learning_store as store_tests
from tests.support import SHORT_TMP, TEMP_ROOT
from tests.test_host_commands import PYTHON, expected_copy, install_package

from codex_agents import host_commands
from codex_agents.host_commands import (
    BINDING_RELATIVE, HostCommandError, install_rule, launch_target_problem, mark_registered_copy, pin_runtime_copy,
    prune_runtime_copies, remove_rule,
)
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import (
    COLLECTOR_RUNTIME_DIGEST, CollectorClient, CollectorServer, LedgerError, ProjectNotRegisteredError,
    _snapshot_project_partition, _verify_project_backup_body,
)
from codex_agents.service_transport import exchange_frame

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements  # noqa: E402

DIGEST = 'c' * 64


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger

    def project_row(self, project_id):
        return dict(self.ledger._connection.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone())

    def a_copy(self, root, digest=DIGEST):
        return str(Path('/var/empty/owner/.codex/agent-orchestration') / host_commands.project_digest(Path(root).resolve())
                   / digest)


class LedgerSchemaTests(StoreBase):
    @requirements('E2a-1', why_not_scenario='Ledger schema and write rules on a real ledger; no process boundary.')
    def test_E2a_1_additive_columns_and_wake_table_keep_the_schema_version(self):
        connection = self.ledger._connection
        self.assertEqual(16, connection.execute('PRAGMA user_version').fetchone()[0])
        columns = {row[1] for row in connection.execute('PRAGMA table_info(projects)')}
        self.assertLessEqual({'launch_copy', 'launch_digest'}, columns)
        wake = {row[1] for row in connection.execute('PRAGMA table_info(project_wake)')}
        self.assertEqual({'project_id', 'allocated_epoch', 'epoch', 'seq', 'state', 'next_due_at_ms', 'body_digest',
                          'due_at_ms', 'refused', 'updated_at_ms'}, wake)
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='project_wake_due'").fetchone())
        # Reopening is idempotent and keeps the version.
        self.ledger.close()
        reopened = store_tests.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.fixture.ledger = reopened
        self.assertEqual(16, reopened._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(columns, {row[1] for row in reopened._connection.execute('PRAGMA table_info(projects)')})

    @requirements('E2a-1', why_not_scenario='Ledger schema and write rules on a real ledger; no process boundary.')
    def test_E2a_1_backup_keeps_its_format_and_restore_has_no_launch_record(self):
        project = self.fixture.project_a_id
        self.ledger.record_launch(project, self.a_copy(self.fixture.project_a), DIGEST)
        body = _snapshot_project_partition(self.ledger._connection, project, ledger_schema_version=16)
        self.assertEqual({'project_id', 'canonical_root', 'root_digest', 'status', 'created_at_ms', 'deleted_at_ms'},
                         set(body['project']))
        self.assertNotIn('project_wake', body['tables'])
        _verify_project_backup_body(body)  # the restore schema takes exactly this row

    @requirements('E2a-2', why_not_scenario='Ledger schema and write rules on a real ledger; no process boundary.')
    def test_E2a_2_registration_names_its_columns_and_writes_no_launch_record(self):
        root = Path(self.fixture.temporary.name) / 'project-c'
        root.mkdir()
        project = self.ledger.register_project(root, now_ms=5)
        row = self.project_row(project)
        self.assertEqual((None, None, 'active', 5), (row['launch_copy'], row['launch_digest'], row['status'],
                                                     row['created_at_ms']))
        self.assertIsNone(self.ledger._connection.execute('SELECT 1 FROM project_wake WHERE project_id=?',
                                                          (project,)).fetchone())
        source = Path(store_tests.LearningLedger.register_project.__code__.co_filename).read_text()
        self.assertNotIn('INSERT INTO projects VALUES', source)

    @requirements('E2a-3', why_not_scenario='Ledger schema and write rules on a real ledger; no process boundary.')
    def test_E2a_3_ledger_records_only_a_well_formed_launch_record(self):
        project, root = self.fixture.project_a_id, self.fixture.project_a
        good = self.a_copy(root)
        before = self.project_row(project)
        for copy, digest in ((good, 'C' * 64), (good, 'c' * 63), ('relative/' + DIGEST, DIGEST),
                             (self.a_copy(root, 'd' * 64), DIGEST),
                             (self.a_copy(self.fixture.project_b), DIGEST),
                             (str(Path(good).parent.parent / 'elsewhere' / DIGEST), DIGEST)):
            with self.subTest(copy=copy, digest=digest), self.assertRaises(LedgerError):
                self.ledger.record_launch(project, copy, digest)
        self.assertEqual(before, self.project_row(project))
        with self.assertRaises(ProjectNotRegisteredError):
            self.ledger.record_launch('prj_' + '0' * 32, good, DIGEST)
        self.ledger.record_launch(project, good, DIGEST)
        self.assertEqual((good, DIGEST), self.ledger.launch_record(project))
        self.assertIsNone(self.ledger.launch_record(self.fixture.project_b_id))


class CollectorRecordTests(StoreBase):
    def setUp(self):
        super().setUp()
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    @requirements('E2a-3', why_not_scenario='Crosses the real collector socket in process; no project service is needed.')
    def test_E2a_3_record_launch_over_the_collector_wire_and_its_runtime_gate(self):
        root = self.fixture.project_a
        copy = self.a_copy(root)
        old = {'protocol': 1, 'op': 'record_launch', 'project_root': str(root), 'launch_copy': copy,
               'launch_digest': DIGEST}
        with self.ledger._lock:
            changes = self.ledger._connection.total_changes
        reply = json.loads(exchange_frame(self.fixture.state.socket, canonical_json({**old, 'runtime_digest': '0' * 64}),
                                          timeout=2.0, request_limit=65_536, response_limit=16_384))
        self.assertEqual('runtime_upgrade_required', reply['code'])
        with self.ledger._lock:
            self.assertEqual(changes, self.ledger._connection.total_changes)
        client = CollectorClient(self.fixture.state.socket, timeout=2)
        client.record_launch(root, copy, DIGEST)
        with self.ledger._lock:
            self.assertEqual((copy, DIGEST), self.ledger.launch_record(self.fixture.project_a_id))
        bad = json.loads(exchange_frame(self.fixture.state.socket, canonical_json(
            {**old, 'launch_digest': 'e' * 64, 'runtime_digest': COLLECTOR_RUNTIME_DIGEST}),
            timeout=2.0, request_limit=65_536, response_limit=16_384))
        self.assertFalse(bad['ok'])
        with self.ledger._lock:
            self.assertEqual((copy, DIGEST), self.ledger.launch_record(self.fixture.project_a_id))


class CopyBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        patcher = mock.patch.object(host_commands, 'temp_roots', lambda: set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = install_package(self.base / 'project')
        self.home = Path(os.path.realpath(self.base)) / 'home'
        self.home.mkdir()
        self.codex_home = self.home / '.codex'
        self.environ = {'HOME': str(self.home)}
        account = mock.patch.object(host_commands, 'account_home', lambda: self.home)
        account.start()
        self.addCleanup(account.stop)

    def install(self, **kwargs):
        return install_rule(self.project, self.environ, PYTHON, **kwargs)

    def sibling(self, name):
        """An older copy of the same project, as an earlier install left it."""
        copy = expected_copy(self.codex_home, self.project)
        older = copy.parent / name
        shutil.copytree(copy, older)
        return older


class LaunchTargetTests(CopyBase):
    @requirements('E2a-3', why_not_scenario='Launch-target validation over a real installed package and protected copy.')
    def test_E2a_3_a_valid_copy_is_a_launch_target_whatever_clone_ran_setup(self):
        elsewhere = self.base / 'some-clone'
        elsewhere.mkdir()
        copy = Path(self.install(clone_root=elsewhere)['runtime_copy'])
        self.assertIsNone(launch_target_problem(copy, self.project, self.environ))
        self.assertEqual(str(elsewhere.resolve()), json.loads((copy / BINDING_RELATIVE).read_text())['clone_root'])

    @requirements('E2a-3', why_not_scenario='Launch-target validation over a real installed package and protected copy.')
    def test_E2a_3_a_copy_bound_elsewhere_or_changed_is_not_a_launch_target(self):
        copy = Path(self.install()['runtime_copy'])
        other = install_package(self.base / 'other')
        self.assertIsNotNone(launch_target_problem(copy, other, self.environ))  # bound to another project
        moved = self.base / 'moved' / copy.name
        shutil.copytree(copy, moved)
        self.assertIsNotNone(launch_target_problem(moved, self.project, self.environ))  # not where setup puts it
        self.assertIsNotNone(launch_target_problem(copy.parent / ('f' * 64), self.project, self.environ))  # missing
        target = copy / '.codex' / 'orchestration' / 'project_host.py'
        os.chmod(target, 0o600)
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        self.assertIsNotNone(launch_target_problem(copy, self.project, self.environ))  # bytes changed

    @requirements('E2a-3', why_not_scenario='Launch-target validation over a real installed package and protected copy.')
    def test_E2a_3_setup_records_the_launch_target_through_the_collector(self):
        recorded = []
        installed = self.install(record_launch=lambda copy, digest: recorded.append((copy, digest)))
        copy = Path(installed['runtime_copy'])
        self.assertEqual([(copy, copy.name)], recorded)
        self.assertEqual('recorded', installed['launch_record'])
        self.assertTrue((copy.parent / '.pins' / (copy.name + '.registered')).is_file())

        def refuse(copy, digest):
            raise HostCommandError('collector unavailable')
        again = self.install(record_launch=refuse)
        self.assertEqual('not_recorded', again['launch_record'])
        # Without a recorder (the API as before), the report keeps its earlier keys.
        self.assertEqual({'codex_rule', 'runtime_copy', 'runtime_copy_status'}, set(self.install()))


class PinningTests(CopyBase):
    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_prune_skips_the_registered_and_in_use_copies(self):
        copy = Path(self.install()['runtime_copy'])
        registered, in_use, loose = self.sibling('a' * 64), self.sibling('b' * 64), self.sibling('d' * 64)
        mark_registered_copy(registered)
        with pin_runtime_copy(in_use):
            removed = prune_runtime_copies(self.project, copy, self.environ)
        self.assertEqual([str(loose)], removed)
        self.assertTrue(registered.is_dir() and in_use.is_dir() and copy.is_dir())
        # Marking another copy registered moves the mark: the earlier one may go.
        mark_registered_copy(copy)
        self.assertEqual(sorted([str(registered), str(in_use)]), sorted(prune_runtime_copies(self.project, copy,
                                                                                             self.environ)))

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_replacement_and_removal_skip_a_copy_in_use(self):
        copy = Path(self.install()['runtime_copy'])
        (copy / '.codex' / 'orchestration' / 'stray.pyc').write_bytes(b'')  # a problem: setup would replace it
        with pin_runtime_copy(copy):
            with self.assertRaises(HostCommandError):
                self.install()
            self.assertTrue((copy / '.codex' / 'orchestration' / 'stray.pyc').exists())  # left in place
            removed = remove_rule(self.project, self.environ)
            self.assertNotIn(str(copy.parent), removed)
            self.assertTrue(copy.is_dir())
        removed = remove_rule(self.project, self.environ)
        self.assertIn(str(copy.parent), removed)
        self.assertFalse(copy.parent.exists())

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_a_pin_waits_for_no_one_and_fails_while_a_prune_holds_the_copy(self):
        copy = Path(self.install()['runtime_copy'])
        older = self.sibling('a' * 64)
        entered, release = threading.Event(), threading.Event()
        real = host_commands.remove_tree_at

        def slow(container, name):
            entered.set()
            release.wait(5)
            return real(container, name)
        with mock.patch.object(host_commands, 'remove_tree_at', slow):
            worker = threading.Thread(target=prune_runtime_copies, args=(self.project, copy, self.environ))
            worker.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaises(HostCommandError):
                with pin_runtime_copy(older):
                    pass
            release.set()
            worker.join(5)
        self.assertFalse(older.exists())
        with pin_runtime_copy(copy):
            with pin_runtime_copy(copy):  # shared: two launchers may hold it
                pass


class SetupRecordTests(CopyBase):
    """Decision 171: setup's registration step records the launch target for every project, Codex or not."""

    def setUp(self):
        super().setUp()
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    def register(self):
        import cli
        stdout = io.StringIO()
        environment = {**{k: v for k, v in os.environ.items() if k != 'CODEX_HOME'}, **self.environ,
                       'XDG_STATE_HOME': str(self.home / '.local' / 'state')}
        with mock.patch.object(sys, 'argv', ['cli.py', 'learning-register', '--state-root',
                                             str(self.fixture.state_root), '--project-root', str(self.project)]), \
                mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(stdout):
            code = cli.main()
        self.assertEqual(0, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    @requirements('E2a-10', why_not_scenario='The real CLI registration step against the real collector socket, in process so the account home can be a test folder.')
    def test_E2a_10_registration_records_the_launch_target_without_the_codex_rule(self):
        report = self.register()
        copy = expected_copy(self.codex_home, self.project)
        self.assertEqual('recorded', report['launch_record'])
        with self.ledger._lock:
            self.assertEqual((str(copy), copy.name), self.ledger.launch_record(report['project_id']))
        self.assertTrue((copy.parent / '.pins' / (copy.name + '.registered')).is_file())
        self.assertFalse((self.codex_home / 'rules').exists())  # no Codex rule was written
        self.assertEqual('recorded', self.register()['launch_record'])  # an update run records it again

    @requirements('E2a-11', why_not_scenario='learning-status over the real collector socket; no project service is needed.')
    def test_E2a_11_status_says_run_setup_when_no_launch_record_exists(self):
        from codex_agents.client_runtime import status_client
        project = self.register()['project_id']
        recorded = status_client(self.fixture.state_root, self.project)
        self.assertIsNone(recorded['recovery'])
        # A project registered without setup's record (as a restored one is) is told to run setup.
        missing = status_client(self.fixture.state_root, self.fixture.project_a)
        self.assertEqual('recovery pending: run setup to record the install', missing['recovery'])
        with self.ledger._lock:
            self.ledger._connection.execute("UPDATE projects SET launch_copy=NULL,launch_digest=NULL WHERE project_id=?",
                                            (project,))
        self.assertEqual('recovery pending: run setup to record the install',
                         status_client(self.fixture.state_root, self.project)['recovery'])


if __name__ == '__main__':
    unittest.main()
