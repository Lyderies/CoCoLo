"""RSF-S3: real process death at SQLite commit boundaries on disposable stores."""
from contextlib import closing, ExitStack
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from codex_agents import learning_store, project_history, store_fence
from codex_agents.learning_store import LearningLedger, LearningState, LedgerError
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope
from tests.learning_support import attest_provider, event_record, generate_test_owner_key
from tests.test_project_history import PROJECT_A, binding
from tests import test_result_leases as leases
from tests.project_schema_fixtures import legacy_project_holds_layout

COUNT = 130
NOW = 1000
KEY = b'r' * 32
CAPACITY = ResultCapacity(512, 512 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
ACCOUNT_CLASSES = ('run_slot', 'binding', 'probe')
PROJECT_CLASSES = ('lease', 'reservation', 'context', 'result_custody', 'orchestration')


def database(root, kind):
    return root / ('state/learning.sqlite3' if kind == 'account' else 'history/history.sqlite3')


def open_owner(root, kind, *, authorized=True):
    if kind == 'account':
        return LearningLedger(LearningState(root / 'state'), allow_schema_upgrade=authorized)
    return project_history.ProjectHistory(root / 'history', PROJECT_A, result_key=KEY,
        result_capacity=CAPACITY, result_clock_ms=lambda: NOW,
        allow_legacy_orchestration_reference=True, allow_schema_upgrade=authorized)


def worker(root, kind, phase, target, threshold):
    """Observe actual owner commits, with no production fault hook or mocked commit."""
    real = store_fence.FencedConnection._fenced
    publication = None
    validated = set()
    class RetainedOwnerFinished(Exception):
        pass
    def progress_rows(connection):
        if sqlite3.Connection.execute(connection,
                "SELECT 1 FROM sqlite_master WHERE name='hold_migration_progress'").fetchone():
            return [tuple(row) for row in sqlite3.Connection.execute(connection,
                'SELECT class,cursor,done FROM hold_migration_progress ORDER BY class')]
        return []
    def watched(connection, sql, operation):
        nonlocal publication
        commit = sql.strip().upper().rstrip(';') in ('COMMIT', 'END')
        dropping = sql.strip().upper() == 'DROP TABLE HOLD_MIGRATION_PROGRESS'
        progress = progress_rows(connection) if commit or dropping else []
        if dropping:
            publication = progress
        hit = commit and (publication is not None if target == 'publication' else
                          any(name == target and cursor == threshold for name, cursor, _ in progress))
        def stop():
            epoch = sqlite3.Connection.execute(connection,
                'SELECT epoch FROM runtime_takeover WHERE singleton=1').fetchone()[0]
            print(json.dumps({'boundary': phase, 'progress': progress, 'publication': publication,
                              'validated': sorted(validated), 'epoch': epoch, 'pid': os.getpid()}), flush=True)
            if phase != 'retain':
                sys.stdin.readline()  # parent SIGKILLs this exact process
                return
            # Retain a real successor's connection after its lock is lost, then
            # let a third owner take over. No StoreFence method is mocked.
            if sys.stdin.readline().strip() != 'release':
                raise RuntimeError('expected release command')
            fcntl.flock(connection._write_fence.descriptor, fcntl.LOCK_UN)
            print(json.dumps({'released': True}), flush=True)
            if sys.stdin.readline().strip() != 'commit':
                raise RuntimeError('expected commit command')
            epoch_refused = False
            try:
                connection._write_fence.check(connection, full=False)
            except (LedgerError, HistoryError):
                epoch_refused = True
            # Trusted native staging exercises the actual COMMIT check even
            # though fenced BEGIN would already refuse this stale capability.
            sqlite3.Connection.execute(connection, 'BEGIN IMMEDIATE')
            table = 'run_holds' if kind == 'account' else 'project_holds'
            adopted = sqlite3.Connection.execute(connection,
                f'UPDATE {table} SET generation=generation+1 WHERE rowid=(SELECT MIN(rowid) FROM {table})').rowcount
            cursor = sqlite3.Connection.execute(connection,
                'UPDATE hold_migration_progress SET cursor=?,done=0 WHERE class=?', (threshold + 1, target)).rowcount
            try:
                connection.commit()
            except (LedgerError, HistoryError) as error:
                print(json.dumps({'refused': str(error), 'epoch_refused': epoch_refused,
                    'staged': [adopted, cursor], 'in_transaction': connection.in_transaction}), flush=True)
            else:
                print(json.dumps({'committed': True}), flush=True)
            raise RetainedOwnerFinished()
        if hit and phase == 'before':
            stop()
        result = real(connection, sql, operation)
        if hit and phase in ('after', 'retain'):
            stop()
        return result
    def observe_validator(name, original):
        def checked(*args, **kwargs):
            result = original(*args, **kwargs)
            connection = args[0] if isinstance(args[0], sqlite3.Connection) else args[0]._connection
            progress = progress_rows(connection)
            if result is not False and progress and all(done for _, _, done in progress):
                validated.add(name)
            return result
        return checked
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(store_fence.FencedConnection, '_fenced', watched))
        stack.enter_context(mock.patch.object(LearningLedger, '_clock_ms', return_value=NOW))
        stack.enter_context(mock.patch.object(LearningLedger, '_capacity_clock_reference', return_value=10))
        validators = [(learning_store, name) for name in ('check_run_holds_consistency', 'check_cleanup_coverage',
            'check_observation_coverage', 'check_billing_probe_coverage', 'check_admission_evidence_coverage')]
        validators.append((LearningLedger, '_assert_launch_schema_locked'))
        if kind == 'project':
            validators = ((project_history.ProjectHistory, name) for name in
                          ('_check_schema_identity', '_check_project_holds'))
        for owner_type, name in validators:
            stack.enter_context(mock.patch.object(owner_type, name,
                observe_validator(name, getattr(owner_type, name))))
        try:
            owner = open_owner(Path(root), kind)
            owner.close()
        except RetainedOwnerFinished:
            return
    print(json.dumps({'completed': True}), flush=True)


class ResumableMigrationTests(unittest.TestCase):
    __why_not_scenario__ = 'Isolated persistence owners and exact subprocess deaths; no installed services.'
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rsfs3-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def snapshot(self, root, kind):
        with closing(sqlite3.connect(database(root, kind))) as db:
            return {name: sorted([tuple(row) for row in db.execute('SELECT * FROM ' + name)], key=repr)
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                if name != 'runtime_takeover'}

    def account_fixture(self, *, lost=False):
        root = self.root / 'account-source'
        root.mkdir()
        project_root = root / 'project'
        project_root.mkdir()
        state = LearningState(root / 'state')
        private_key, public_key = generate_test_owner_key(root)
        state.install_owner_public_key(public_key)
        owner = LearningLedger(state)
        self.addCleanup(owner.close)
        project = owner.register_project(project_root, now_ms=1)
        owner.append(project, event_record('evt_preserved', observed_at_ms=NOW), producer='hook')
        attest_provider(owner, project, private_key, 'claude', 'a' * 64)
        db = owner._connection
        with owner._account_transaction():
            for n in range(COUNT):
                digest, run = f'{n+1:064x}', f'run-{n}'
                db.execute('INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,'
                    'task_digest,route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,'
                    'binding_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (project,digest,run,'a'*64,'b'*64,'c'*64,'d'*64,'legacy',1,999999,'pending'))
                db.execute('UPDATE host_run_bindings SET observation_started_at_ms=1,last_progress_at_ms=1,'
                    'deadline_observed=0 WHERE project_id=? AND handle_digest=?', (project,digest))
                if lost and n == 0:
                    db.execute("UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                        'WHERE project_id=? AND handle_digest=?', (project,digest))
                db.execute("INSERT INTO launch_claims(project_id,handle_digest,run_id,kind,claimed_at_ms) "
                    "VALUES(?,?,?,'legacy_fenced',1)", (project,digest,run))
                db.execute("INSERT INTO active_run_capacity(project_id,handle_digest,run_id,kind,claimed_at_ms) "
                    "VALUES(?,?,?,'legacy_unknown',1)", (project,digest,run))
                request = f'probe-{n}'
                db.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                    (project,request,'a'*64,1,'b'*64))
                db.execute('INSERT INTO account_billing_probe_requests(project_id,request_key,parameters_digest,'
                    'runtime_digest,caller_scope,created_at_ms,deadline_at_ms,state,state_seal) '
                    "VALUES(?,?,?,?,?,?,?,'reserved',?)", (project,request,'a'*64,'b'*64,'c'*64,1,999999,'d'*64))
        # Produce a genuine pre-register layout and a verified schema-14 backup.
        db._write_fence = None
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'run_holds_%'").fetchall():
            db.execute('DROP TRIGGER ' + name)
        db.execute('DROP TABLE run_holds')
        db.execute('ALTER TABLE records DROP COLUMN probe_id')
        db.execute('DROP TABLE runtime_takeover')
        db.execute('PRAGMA user_version=14')
        owner._fence.epoch = None
        owner._fence.legacy = True
        db._write_fence = owner._fence
        owner._write_upgrade_backup(project, 14, time.time_ns() // 1_000_000)
        owner.close()
        return root

    def project_fixture(self):
        root = self.root / 'project-source'
        root.mkdir()
        fixture = leases.ResultLeaseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture.capacity = CAPACITY
        fixture.reopen()
        for n in range(1, COUNT + 1):
            fixture.reserve(n)
            fixture.store.arm_result_lease(leases.lease_binding(n))
            fixture.bind(n)
        # Retain actual sealed publication/record bytes as well as the source leases.
        fixture.fixture.commit(1)
        fixture.store.append_record(canonical_json(event_record('evt_preserved')), source='hook', ingested_at_ms=NOW)
        fixture.store._orchestration_limits = OrchestrationLimits(512, 12)
        scope = OrchestrationScope(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1)
        for n in range(COUNT):
            fixture.store._orchestration_owner().apply(
                OrchestrationRequest(1, 'orchestration.open', f'open-{n}', scope, {}),
                client_version='codex-host-v1', before_commit=lambda: None)
        legacy_project_holds_layout(fixture.store)
        fixture.store._connection.execute('PRAGMA user_version=17')
        fixture.store.close()
        shutil.copytree(fixture.fixture.directory, root / 'history')
        return root

    def clone(self, source, name):
        destination = self.root / name
        shutil.copytree(source, destination)
        return destination

    def reopen(self, root, kind, *, now=NOW, monotonic=10):
        with mock.patch.object(LearningLedger, '_clock_ms', return_value=now), \
                mock.patch.object(LearningLedger, '_capacity_clock_reference', return_value=monotonic):
            owner = open_owner(root, kind)
            owner.close()

    def start_worker(self, root, kind, phase, target, threshold):
        env = {key: value for key, value in os.environ.items() if not key.startswith(('CODEX_', 'PYTHON'))}
        env['PYTHONPATH'] = os.pathsep.join([str(Path(__file__).parents[1]),
            str(Path(__file__).parents[1] / 'native-package/.codex/orchestration'),
            str(Path(__file__).parents[3] / 'scripts')])
        code = 'from tests.test_rsf_s3_resumable import worker; import sys; worker(*sys.argv[1:5],int(sys.argv[5]))'
        return subprocess.Popen([sys.executable, '-B', '-c', code, str(root), kind, phase, target, str(threshold)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def read_worker(self, child):
        readable, _, _ = select.select([child.stdout], [], [], 15)
        self.assertTrue(readable, 'migration never reached the requested commit boundary')
        line = child.stdout.readline()
        if not line:
            self.fail('migration subprocess exited: ' + child.stderr.read()[-2000:])
        return json.loads(line)

    def kill_at(self, root, kind, phase, target, threshold):
        child = self.start_worker(root, kind, phase, target, threshold)
        try:
            message = self.read_worker(child)
            self.assertEqual(phase, message.get('boundary'), message)
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(-signal.SIGKILL, child.returncode)
            return message
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def interrupted_equivalence(self, kind, classes):
        source = self.account_fixture() if kind == 'account' else self.project_fixture()
        original = self.snapshot(source, kind)
        uninterrupted = self.clone(source, 'uninterrupted')
        self.reopen(uninterrupted, kind)
        expected = self.snapshot(uninterrupted, kind)
        self.assertNotIn('hold_migration_progress', expected)
        for target in classes:
            for phase in ('before', 'after'):
                with self.subTest(kind=kind, target=target, phase=phase):
                    root = self.clone(source, target + '-' + phase)
                    self.kill_at(root, kind, phase, target, 64)
                    partial = self.snapshot(root, kind)
                    if phase == 'after':
                        progress = partial['hold_migration_progress']
                        reference = 10 if kind == 'account' else None
                        self.assertIn((kind, target, 64, 0, NOW, reference), progress)
                        table = ('run_holds' if kind == 'account' else
                                 'project_result_contexts' if target == 'context' else 'project_holds')
                        adopted = partial[table]
                        if table == 'run_holds':
                            adopted = [row for row in adopted if row[0] == target]
                        elif table == 'project_holds':
                            adopted = [row for row in adopted if row[1] == target]
                        self.assertEqual(64, len(adopted))
                        with self.assertRaises((LedgerError, HistoryError)):
                            open_owner(root, kind, authorized=False)
                        self.kill_at(root, kind, 'before', target, 128)
                        self.assertIn((kind, target, 64, 0, NOW, reference),
                            self.snapshot(root, kind)['hold_migration_progress'])
                    elif target == classes[0]:
                        self.assertEqual(original, partial)
                    self.reopen(root, kind)
                    self.assertEqual(expected, self.snapshot(root, kind))
                    self.reopen(root, kind)
                    self.assertEqual(expected, self.snapshot(root, kind))
                    if kind == 'project':
                        self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))
        for table in ('records', 'owner_attestations', 'launch_claims', 'result_reservations'):
            if table in original:
                if table == 'owner_attestations':
                    self.assertTrue(original[table])  # real detached SSH signature, not an empty preservation check
                if kind == 'account' and table == 'records':
                    self.assertEqual(original[table], [row[:-1] for row in expected[table]])
                    self.assertTrue(all(row[-1] is None for row in expected[table]))
                else:
                    self.assertEqual(original[table], expected[table])

    @requirements('RSF-S3-1', 'RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_account_killed_before_and_after_each_class_commit_reopens_twice_identically(self):
        self.interrupted_equivalence('account', ACCOUNT_CLASSES)

    @requirements('RSF-S3-1', 'RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_project_killed_before_and_after_each_class_commit_reopens_twice_identically(self):
        self.interrupted_equivalence('project', PROJECT_CLASSES)

    def final_boundary_equivalence(self, kind, classes):
        source = self.account_fixture() if kind == 'account' else self.project_fixture()
        uninterrupted = self.clone(source, 'final-uninterrupted')
        self.reopen(uninterrupted, kind)
        expected = self.snapshot(uninterrupted, kind)
        self.assertNotIn('hold_migration_progress', expected)
        if kind == 'account':
            self.assertTrue(expected['owner_attestations'])
        else:
            self.assertTrue(expected['records'])
            self.assertTrue(expected['project_orchestrations'])
        for target in (*classes, 'publication'):
            for phase in ('before', 'after'):
                with self.subTest(kind=kind, target=target, phase=phase):
                    root = self.clone(source, 'final-' + target + '-' + phase)
                    message = self.kill_at(root, kind, phase, target, COUNT)
                    partial = self.snapshot(root, kind)
                    if target == 'publication':
                        self.assertTrue(all(done == 1 for _, _, done in message['publication']))
                        self.assertEqual(len(classes), len(message['publication']))
                        validators = ({'check_run_holds_consistency', 'check_cleanup_coverage',
                            'check_observation_coverage', 'check_billing_probe_coverage',
                            'check_admission_evidence_coverage', '_assert_launch_schema_locked'} if kind == 'account' else
                            {'_check_schema_identity', '_check_project_holds'})
                        self.assertEqual(validators, set(message['validated']))
                        self.assertEqual(phase == 'before', 'hold_migration_progress' in partial)
                    else:
                        # With 130 sources the last batch has two rows, and its
                        # cursor and done=1 are written in that same transaction.
                        self.assertIn([target, COUNT, 1], message['progress'])
                        cursor, done = (COUNT - 2, 0) if phase == 'before' else (COUNT, 1)
                        reference = 10 if kind == 'account' else None
                        self.assertIn((kind, target, cursor, done, NOW, reference),
                                      partial['hold_migration_progress'])
                    if 'hold_migration_progress' in partial:
                        with self.assertRaises((LedgerError, HistoryError)):
                            open_owner(root, kind, authorized=False)
                    self.reopen(root, kind)
                    self.assertEqual(expected, self.snapshot(root, kind))
                    self.reopen(root, kind)
                    self.assertEqual(expected, self.snapshot(root, kind))
                    if kind == 'project':
                        self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))

    @requirements('RSF-S3-1', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_account_last_short_done_and_publication_commits_reopen_twice_identically(self):
        self.final_boundary_equivalence('account', ACCOUNT_CLASSES)

    @requirements('RSF-S3-1', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_project_last_short_done_and_publication_commits_reopen_twice_identically(self):
        self.final_boundary_equivalence('project', PROJECT_CLASSES)

    @requirements('RSF-S3-2', 'RSF-S3-4')
    def test_real_successor_after_sigkill_increments_epoch_and_stale_batch_cannot_commit(self):
        for kind, target in (('account', ACCOUNT_CLASSES[0]), ('project', PROJECT_CLASSES[0])):
            with self.subTest(kind=kind):
                source = self.account_fixture() if kind == 'account' else self.project_fixture()
                uninterrupted = self.clone(source, kind + '-takeover-uninterrupted')
                self.reopen(uninterrupted, kind)
                expected = self.snapshot(uninterrupted, kind)
                first = self.kill_at(source, kind, 'after', target, 64)
                stale = self.start_worker(source, kind, 'retain', target, 128)
                try:
                    second = self.read_worker(stale)
                    self.assertEqual('retain', second.get('boundary'), second)
                    self.assertNotEqual(first['pid'], second['pid'])
                    self.assertNotEqual(os.getpid(), second['pid'])
                    self.assertEqual(first['epoch'] + 1, second['epoch'])
                    stale.stdin.write('release\n')
                    stale.stdin.flush()
                    self.assertEqual({'released': True}, self.read_worker(stale))
                    third = self.kill_at(source, kind, 'after', target, COUNT)
                    self.assertNotEqual(second['pid'], third['pid'])
                    self.assertEqual(second['epoch'] + 1, third['epoch'])
                    before = self.snapshot(source, kind)
                    stale.stdin.write('commit\n')
                    stale.stdin.flush()
                    refused = self.read_worker(stale)
                    self.assertIn('Run: ./setup.sh --update', refused.get('refused', ''), refused)
                    self.assertTrue(refused['epoch_refused'])
                    self.assertEqual([1, 1], refused['staged'])
                    self.assertFalse(refused['in_transaction'])
                    stale.wait(timeout=5)
                    self.assertEqual(0, stale.returncode, stale.stderr.read())
                    self.assertEqual(before, self.snapshot(source, kind))
                finally:
                    if stale.poll() is None:
                        stale.kill()
                    stale.communicate(timeout=5)
                self.reopen(source, kind)
                self.assertEqual(expected, self.snapshot(source, kind))
                self.reopen(source, kind)
                self.assertEqual(expected, self.snapshot(source, kind))

    @requirements('RSF-S3-3')
    def test_migration_context_is_reset_after_adoption_and_validation_exceptions(self):
        for kind in ('account', 'project'):
            source = self.account_fixture() if kind == 'account' else self.project_fixture()
            for stage in ('adoption', 'validation'):
                with self.subTest(kind=kind, stage=stage):
                    root = self.clone(source, kind + '-context-' + stage)
                    captured = []
                    module = learning_store if kind == 'account' else project_history.project_holds_schema
                    name = (('install_run_holds_schema' if kind == 'account' else 'adopt_project_holds')
                            if stage == 'adoption' else
                            ('check_run_holds_consistency' if kind == 'account' else 'register_consistent'))
                    original = getattr(module, name)
                    def fail(connection, *args, **kwargs):
                        if getattr(connection, '_hold_migration', None) is not None:
                            captured.append(connection)
                            if stage == 'validation':
                                return False
                            raise ValueError('adoption failure')
                        return original(connection, *args, **kwargs)
                    with mock.patch.object(module, name, fail), \
                            self.assertRaises((ValueError, LedgerError, HistoryError)):
                        self.reopen(root, kind)
                    self.assertTrue(captured)
                    for connection in captured:
                        self.assertIsNone(connection._hold_migration)
                    kept = []
                    if kind == 'project' and stage == 'validation':
                        self.assertIn('hold_migration_progress', self.snapshot(root, kind))
                        self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-22.*.attempt'))))
                        kept = [(path, path.read_bytes()) for path in
                                (root / 'history').glob('history.sqlite3.before-22.*')]
                        self.assertEqual(1, len(kept))
                    self.reopen(root, kind)
                    for path, before_bytes in kept:
                        self.assertEqual(before_bytes, path.read_bytes())

    @requirements('RSF-S3-3')
    def test_pending_refusal_gives_plain_next_step_and_update_command(self):
        for kind, target in (('account', ACCOUNT_CLASSES[0]), ('project', PROJECT_CLASSES[0])):
            with self.subTest(kind=kind):
                root = self.account_fixture() if kind == 'account' else self.project_fixture()
                self.kill_at(root, kind, 'after', target, 64)
                before = self.snapshot(root, kind)
                with self.assertRaises((LedgerError, HistoryError)) as refused:
                    open_owner(root, kind, authorized=False)
                self.assertIn('pending hold migration', str(refused.exception).lower())
                self.assertIn('Resume the drained upgrade', str(refused.exception))
                self.assertIn('Run: ./setup.sh --update', str(refused.exception))
                self.assertEqual(before, self.snapshot(root, kind))

    @requirements('RSF-S3-3')
    def test_project_resume_refuses_missing_kept_before_copy_without_writes(self):
        source = self.project_fixture()
        self.kill_at(source, 'project', 'after', PROJECT_CLASSES[0], 64)
        for missing in ('copy', 'marker', 'regular_file'):
            with self.subTest(missing=missing):
                root = self.clone(source, 'missing-' + missing)
                directory = root / 'history'
                marker, = directory.glob('.history.sqlite3.upgrade-22.*.attempt')
                copy = directory / json.loads(marker.read_text())['copy']
                recovery_bytes = copy.read_bytes()
                # A different copy cannot stand in for the original attempt's.
                decoy = directory / 'history.sqlite3.before-22.1'
                decoy.write_bytes(recovery_bytes)
                decoy.chmod(0o600)
                if missing == 'marker':
                    marker.unlink()
                else:
                    copy.unlink()
                    if missing == 'regular_file':
                        copy.symlink_to(decoy.name)
                before = self.snapshot(root, 'project')
                with closing(sqlite3.connect(database(root, 'project'))) as db:
                    epoch = db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0]
                with self.assertRaises(HistoryError) as refused:
                    self.reopen(root, 'project')
                self.assertIn('kept project before-copy', str(refused.exception))
                self.assertIn('Restore', str(refused.exception))
                self.assertIn('Run: ./setup.sh --update', str(refused.exception))
                self.assertEqual(before, self.snapshot(root, 'project'))
                with closing(sqlite3.connect(database(root, 'project'))) as db:
                    self.assertEqual(epoch, db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])
                if missing == 'marker':
                    original_marker, = (source / 'history').glob('.history.sqlite3.upgrade-22.*.attempt')
                    shutil.copy2(original_marker, marker)
                else:
                    if copy.is_symlink():
                        copy.unlink()
                    copy.write_bytes(recovery_bytes)
                    copy.chmod(0o600)
                self.reopen(root, 'project')
                self.assertNotIn('hold_migration_progress', self.snapshot(root, 'project'))

    @requirements('RSF-S3-1', 'RSF-S3-4')
    def test_resumed_classification_keeps_original_wall_and_monotonic_evidence(self):
        source = self.account_fixture(lost=True)
        uninterrupted = self.clone(source, 'clock-uninterrupted')
        self.reopen(uninterrupted, 'account')
        expected = self.snapshot(uninterrupted, 'account')
        self.kill_at(source, 'account', 'after', 'run_slot', 64)
        self.reopen(source, 'account', now=NOW + 50, monotonic=99)
        self.assertEqual(expected, self.snapshot(source, 'account'))
        self.reopen(source, 'account', now=NOW + 100, monotonic=199)
        self.assertEqual(expected, self.snapshot(source, 'account'))

    @requirements('RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4')
    def test_revoked_batch_and_failed_validation_keep_progress_unpublished(self):
        for kind in ('account', 'project'):
            source = self.account_fixture() if kind == 'account' else self.project_fixture()
            target = ACCOUNT_CLASSES[0] if kind == 'account' else PROJECT_CLASSES[0]
            self.kill_at(source, kind, 'after', target, 64)
            before = self.snapshot(source, kind)
            real = store_fence.StoreFence.check
            def revoke(fence, connection, *, full=True):
                if connection.in_transaction and sqlite3.Connection.execute(connection,
                        'SELECT MAX(cursor) FROM hold_migration_progress').fetchone()[0] > 64:
                    sqlite3.Connection.execute(connection, 'UPDATE runtime_takeover SET epoch=epoch+1')
                return real(fence, connection, full=full)
            with mock.patch.object(store_fence.StoreFence, 'check', revoke), \
                    self.assertRaises((LedgerError, HistoryError)):
                self.reopen(source, kind)
            self.assertEqual(before, self.snapshot(source, kind))
            validator = ('codex_agents.learning_store.check_run_holds_consistency' if kind == 'account'
                         else 'codex_agents.project_holds_schema.register_consistent')
            with mock.patch(validator, return_value=False), self.assertRaises((LedgerError, HistoryError)):
                self.reopen(source, kind)
            self.assertIn('hold_migration_progress', self.snapshot(source, kind))
            self.reopen(source, kind)
            self.assertNotIn('hold_migration_progress', self.snapshot(source, kind))
