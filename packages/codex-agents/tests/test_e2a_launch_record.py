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
    COLLECTOR_RUNTIME_DIGEST, CollectorClient, CollectorError, CollectorServer, LedgerError, ProjectNotRegisteredError,
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
        self.assertEqual(18, connection.execute('PRAGMA user_version').fetchone()[0])
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
        self.assertEqual(18, reopened._connection.execute('PRAGMA user_version').fetchone()[0])
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
        # Decision 176: the fixture copy is not at the trusted copy root, so it is refused and nothing is recorded.
        with self.assertRaises(CollectorError):
            client.record_launch(root, copy, DIGEST)
        with self.ledger._lock:
            self.assertIsNone(self.ledger.launch_record(self.fixture.project_a_id))
        bad = json.loads(exchange_frame(self.fixture.state.socket, canonical_json(
            {**old, 'launch_digest': 'e' * 64, 'runtime_digest': COLLECTOR_RUNTIME_DIGEST}),
            timeout=2.0, request_limit=65_536, response_limit=16_384))
        self.assertFalse(bad['ok'])
        with self.ledger._lock:
            self.assertIsNone(self.ledger.launch_record(self.fixture.project_a_id))


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



class LaunchAuthorityTests(StoreBase):
    """Review round 1 (HIGH launch authority, MED collector status): the collector records a launch target only for
    the owner and only a copy it verifies itself; launch status authorizes its subject before any lookup."""

    def setUp(self):
        super().setUp()
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    def send(self, request):
        return json.loads(exchange_frame(self.fixture.state.socket, canonical_json(request), timeout=2.0,
                                         request_limit=65_536, response_limit=16_384))

    @requirements('E2a-3', why_not_scenario='Crosses the real collector socket in process; no project service is needed.')
    def test_E2a_3_record_launch_needs_the_owner_and_a_copy_the_collector_verifies(self):
        root, project = self.fixture.project_b, self.fixture.project_b_id
        copy = self.a_copy(root)
        # A caller with only the socket (no owner proof) cannot redirect another project's launch record.
        reply = self.send({'protocol': 1, 'op': 'record_launch', 'project_root': str(root), 'launch_copy': copy,
                           'launch_digest': DIGEST, 'runtime_digest': COLLECTOR_RUNTIME_DIGEST})
        self.assertFalse(reply['ok'])
        with self.ledger._lock:
            self.assertIsNone(self.ledger.launch_record(project))
        # The owner's own request for a well-formed copy that does not verify at the trusted copy root is refused.
        with self.assertRaises(learning_store_errors()):
            CollectorClient(self.fixture.state.socket, timeout=2).record_launch(root, copy, DIGEST)
        with self.ledger._lock:
            self.assertIsNone(self.ledger.launch_record(project))

    @requirements('E2a-11', why_not_scenario='Crosses the real collector socket in process; no project service is needed.')
    def test_E2a_11_launch_status_authorizes_its_subject_before_any_lookup(self):
        self.ledger.record_launch(self.fixture.project_a_id, self.a_copy(self.fixture.project_a), DIGEST)
        unknown = Path(self.fixture.temporary.name) / 'unregistered'
        unknown.mkdir()
        replies = [self.send({'protocol': 1, 'op': 'launch_status', 'project_root': str(root)})
                   for root in (self.fixture.project_a, self.fixture.project_b, unknown)]
        self.assertEqual(1, len({canonical_json(reply) for reply in replies}), replies)
        self.assertFalse(replies[0]['ok'])
        # The owner's own status still reads it.
        client = CollectorClient(self.fixture.state.socket, timeout=2)
        self.assertEqual((True, False), (client.launch_recorded(self.fixture.project_a),
                                         client.launch_recorded(self.fixture.project_b)))


def learning_store_errors():
    from codex_agents.collector_contracts import CollectorError
    return CollectorError


class PinOrderTests(CopyBase):
    """Review round 1 (HIGH copy pins): a stable per-project lock, the new copy pinned before the record commits,
    the old pin cleared only after it, and replacement and removal honouring pins."""

    def marks(self, copy):
        pins = Path(copy).parent / '.pins'
        return sorted(name for name in os.listdir(pins) if name.endswith('.registered')) if pins.is_dir() else []

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_setup_pins_the_new_copy_before_the_record_and_clears_the_old_pin_after(self):
        copy = Path(self.install()['runtime_copy'])
        older = self.sibling('a' * 64)
        mark_registered_copy(older)  # the copy the ledger's record names now
        seen = []

        def record(target, digest):
            seen.append(self.marks(copy))
        self.assertEqual('recorded', self.install(record_launch=record)['launch_record'])
        self.assertEqual([sorted([older.name + '.registered', copy.name + '.registered'])], seen)
        self.assertEqual([copy.name + '.registered'], self.marks(copy))
        # A record that fails leaves the earlier pin, and no pin on the copy it did not record.
        older = self.sibling('b' * 64)
        mark_registered_copy(older)

        def refuse(target, digest):
            raise HostCommandError('collector unavailable')
        self.assertEqual([older.name + '.registered'], self.marks(copy))
        self.assertEqual('not_recorded', self.install(record_launch=refuse)['launch_record'])
        self.assertEqual([older.name + '.registered'], self.marks(copy))
        self.assertTrue(older.is_dir())

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_a_copy_pinned_while_a_prune_runs_is_never_removed(self):
        copy = Path(self.install()['runtime_copy'])
        older = self.sibling('a' * 64)
        real, marker = host_commands._registered, []

        def registered(container, name):
            found = real(container, name)
            if name == older.name and not marker:
                def mark():
                    try:
                        mark_registered_copy(older)
                        marker.append('marked')
                    except HostCommandError:
                        marker.append('refused')
                worker = threading.Thread(target=mark)
                marker.append(worker)
                worker.start()
                worker.join(0.5)
            return found
        with mock.patch.object(host_commands, '_registered', registered):
            prune_runtime_copies(self.project, copy, self.environ)
        marker[0].join(5)
        pinned = older.name + '.registered' in self.marks(copy)
        self.assertFalse(pinned and not older.is_dir(), 'a pinned copy was removed')

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_a_pin_on_a_lock_file_a_replacement_dropped_never_lets_a_second_replacement_in(self):
        copy = Path(self.install()['runtime_copy'])
        stray = copy / '.codex' / 'orchestration' / 'stray.pyc'
        stray.write_bytes(b'')  # a problem: setup replaces the copy
        real, state = host_commands.fcntl.flock, {'busy': False, 'done': False}

        def flock(descriptor, operation):
            if operation & host_commands.fcntl.LOCK_SH and not state['busy'] and not state['done']:
                state['busy'] = True
                try:
                    self.install()  # a whole replacement between the launcher's open and its lock
                finally:
                    state['busy'], state['done'] = False, True
            return real(descriptor, operation)
        pinned = None
        with mock.patch.object(host_commands.fcntl, 'flock', flock):
            try:
                pinned = pin_runtime_copy(copy)
                pinned.__enter__()
            except HostCommandError:
                pinned = None
        try:
            (copy / '.codex' / 'orchestration' / 'stray.pyc').write_bytes(b'')
            replaced = True
            try:
                self.install()
            except HostCommandError:
                replaced = False
            self.assertFalse(pinned is not None and replaced, 'a copy held in use was replaced')
        finally:
            if pinned is not None:
                pinned.__exit__(None, None, None)

    @requirements('E2a-4', why_not_scenario='Filesystem pins and locks on a real protected copy; no service process.')
    def test_E2a_4_owner_removal_keeps_the_registered_copy(self):
        copy = Path(self.install(record_launch=lambda target, digest: None)['runtime_copy'])
        removed = remove_rule(self.project, self.environ)
        self.assertNotIn(str(copy.parent), removed)
        self.assertTrue(copy.is_dir())


class SetupOfflineTests(CopyBase):
    """Review round 1 (MED offline setup): with no collector running, setup records under exclusive owner
    maintenance, directly in the ledger."""

    @requirements('E2a-3', why_not_scenario='The real CLI recorder, owner maintenance and ledger, with no collector.')
    def test_E2a_3_with_no_collector_running_setup_records_in_the_ledger(self):
        import argparse
        import cli
        fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        project_id = fixture.ledger.register_project(self.project)
        fixture.ledger.close()
        args = argparse.Namespace(state_root=fixture.state_root, project_root=self.project)
        self.assertEqual('recorded', host_commands.record_setup_launch(self.project, cli._launch_recorder(args),
                                                                       self.environ))
        fixture.ledger = store_tests.LearningLedger(fixture.state, client_capabilities=fixture.clients)
        copy = expected_copy(self.codex_home, self.project)
        self.assertEqual((str(copy), copy.name), fixture.ledger.launch_record(project_id))


class FinalFixTests(CopyBase):
    """Primary decision 179 (the one final fix): concurrent setups, a registered copy's replacement, a lost
    record_launch reply, and the owner proof's wrong key and replay over the collector wire."""

    def marks(self, copy):
        pins = Path(copy).parent / '.pins'
        return sorted(name for name in os.listdir(pins) if name.endswith('.registered')) if pins.is_dir() else []

    def collector(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    @requirements('E2a-4', why_not_scenario='Two setup record steps on real threads over a real protected copy folder.')
    def test_E2a_4_concurrent_setups_never_clear_the_mark_of_the_record_that_won(self):
        copy = Path(self.install()['runtime_copy'])
        first, second = self.sibling('a' * 64), self.sibling('b' * 64)
        ledger, first_recorded, second_done = {}, threading.Event(), threading.Event()

        def record_first(target, digest):
            ledger['copy'] = target  # committed; its cleanup comes after the other setup ran (or waited)
            first_recorded.set()
            second_done.wait(1.0)

        def record_second(target, digest):
            ledger['copy'] = target
        with mock.patch.object(host_commands, 'launch_target_problem', lambda *args: None):
            worker = threading.Thread(target=host_commands._record_target,
                                      args=(first, self.project, record_first, self.environ))
            worker.start()
            self.assertTrue(first_recorded.wait(5))
            other = threading.Thread(target=lambda: (host_commands._record_target(
                second, self.project, record_second, self.environ), second_done.set()))
            other.start()
            worker.join(10)
            other.join(10)
        self.assertEqual(second, ledger['copy'])
        self.assertEqual([second.name + '.registered'], self.marks(copy))

    @requirements('E2a-4', why_not_scenario='Setup over a real protected copy; no service process.')
    def test_E2a_4_setup_never_replaces_the_registered_copy_that_verifies(self):
        copy = Path(self.install(record_launch=lambda target, digest: None)['runtime_copy'])
        inode = copy.stat().st_ino
        elsewhere = self.base / 'another-clone'
        elsewhere.mkdir()
        self.assertEqual('unchanged', self.install(clone_root=elsewhere)['runtime_copy_status'])
        self.assertEqual(inode, copy.stat().st_ino)
        self.assertIsNone(launch_target_problem(copy, self.project, self.environ))

    @requirements('E2a-4', why_not_scenario='The real CLI recorder, the real collector socket and a real copy folder.')
    def test_E2a_4_a_lost_record_reply_keeps_the_new_copy_pinned(self):
        import argparse
        import cli
        from codex_agents import collector_client
        from codex_agents.service_transport import ServiceTransportError
        self.collector()
        project_id = self.ledger.register_project(self.project)
        copy, _status = host_commands.install_runtime_copy(self.project, self.environ)
        older = self.sibling('a' * 64)
        mark_registered_copy(older)  # the copy an earlier record named
        real = collector_client.exchange_frame

        def lost(path, frame, **options):
            raw = real(path, frame, **options)
            if b'"op":"record_launch"' in frame:
                raise ServiceTransportError('transport_unavailable')  # committed; the reply never arrived
            return raw
        args = argparse.Namespace(state_root=self.fixture.state_root, project_root=self.project)
        environment = {**{k: v for k, v in os.environ.items() if k != 'CODEX_HOME'}, **self.environ}
        with mock.patch.object(collector_client, 'exchange_frame', lost), \
                mock.patch.dict(os.environ, environment, clear=True):
            host_commands.record_setup_launch(self.project, cli._launch_recorder(args), self.environ)
        with self.ledger._lock:
            self.assertEqual((str(copy), copy.name), self.ledger.launch_record(project_id))
        self.assertIn(copy.name + '.registered', self.marks(copy))
        prune_runtime_copies(self.project, older, self.environ)
        self.assertTrue(copy.is_dir(), 'the copy the record names was pruned')


class FinalWireTests(StoreBase):
    """Primary decision 179 item 8: the owner proof's wrong key and replay, over the collector wire."""

    def collector(self):
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    @requirements('E2a-3', why_not_scenario='Crosses the real collector socket in process; no project service is needed.')
    def test_E2a_3_record_launch_and_launch_status_refuse_a_wrong_key_and_a_replayed_proof(self):
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.hosts import OWNER_CLIENT
        from codex_agents.service_enrollment import collector_owner_paths, collector_owner_preamble, enrollment_preamble
        self.collector()
        root, project = self.fixture.project_a, self.fixture.project_a_id
        paths = CollectorPaths(self.fixture.state_root)
        _key, proofs = collector_owner_paths(paths)

        def send(request):
            return json.loads(exchange_frame(self.fixture.state.socket, canonical_json(request), timeout=5.0,
                                             request_limit=65_536, response_limit=16_384))

        def record(copy, line=None, key=None):
            body = {'op': 'record_launch', 'project_root': str(Path(root).resolve()), 'launch_copy': str(copy),
                    'launch_digest': Path(copy).name}
            if line is None:
                message = canonical_json(body)
                line = (collector_owner_preamble(paths, message, body['project_root']) if key is None else
                        enrollment_preamble(key, 'account', OWNER_CLIENT, 1, message, proofs,
                                            subject=body['project_root'])).decode('ascii')
            return send({'protocol': 1, **body, 'owner': line, 'runtime_digest': COLLECTOR_RUNTIME_DIGEST}), line
        first, second = self.a_copy(root, 'a' * 64), self.a_copy(root, 'b' * 64)
        with mock.patch.object(host_commands, 'launch_target_problem', lambda *args: None):
            wrong, _line = record(first, key=os.urandom(32))
            self.assertEqual((False, 'operation_rejected'), (wrong['ok'], wrong.get('code')))
            with self.ledger._lock:
                self.assertIsNone(self.ledger.launch_record(project))
            accepted, line = record(first)
            self.assertTrue(accepted['ok'], accepted)
            self.assertTrue(record(second)[0]['ok'])
            replayed, _line = record(first, line=line)  # the first request's own proof, sent again
            self.assertEqual((False, 'operation_rejected'), (replayed['ok'], replayed.get('code')))
            with self.ledger._lock:
                self.assertEqual((second, 'b' * 64), self.ledger.launch_record(project))
        status = {'op': 'launch_status', 'project_root': str(Path(root).resolve())}
        message = canonical_json(status)
        wrong = send({'protocol': 1, **status, 'owner': enrollment_preamble(
            os.urandom(32), 'account', OWNER_CLIENT, 1, message, proofs, subject=status['project_root']).decode()})
        self.assertEqual((False, 'operation_rejected'), (wrong['ok'], wrong.get('code')))
        line = collector_owner_preamble(paths, message, status['project_root']).decode()
        self.assertEqual({'ok': True, 'recorded': True}, send({'protocol': 1, **status, 'owner': line}))
        replayed = send({'protocol': 1, **status, 'owner': line})
        self.assertEqual((False, 'operation_rejected'), (replayed['ok'], replayed.get('code')))


if __name__ == '__main__':
    unittest.main()
