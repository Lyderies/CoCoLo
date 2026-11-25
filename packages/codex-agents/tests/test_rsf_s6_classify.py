"""RSF-S6: one real pre-B3 mixed store, classification and fenced resumptions."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import select
import signal
from unittest import mock
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import tests.support  # noqa: F401
from test_framework import requirements
from codex_agents import project_history, project_holds_schema, service_record, store_fence
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.result_contracts import ResultCapacity, ResultLeaseBinding, MAX_RESULT_BODY_BYTES, ResultObservation
from codex_agents.learning_contracts import canonical_json
from codex_agents.lost_run_contracts import SettleLostInput
from codex_agents.lost_run_closure import lost_run_closure

HERE = Path(__file__).parent
TOP = HERE.parents[2]


class ClassificationTests(unittest.TestCase):
    __why_not_scenario__ = "Real archived runtime and disposable stores; no installed services or model launch."
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='rsfs6-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        archive = cls.root / 'old'
        archive.mkdir()
        packed = subprocess.run(['git', '-C', str(TOP), 'archive', 'f4e8cea896c09a3113354f23f746513733e7a8da',
            'packages/codex-agents', 'scripts'], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(archive)], input=packed.stdout, check=True)
        cls.archive = archive
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON'))}
        env['PYTHONPATH'] = str(archive / 'scripts')
        made = subprocess.run([sys.executable, '-B', str(HERE / 'rsf_s6_fixture.py'), str(archive), str(cls.source)],
            env=env, capture_output=True, text=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        cls.manifest = json.loads((cls.source / 'manifest.json').read_text())
        with closing(sqlite3.connect(cls.source / 'history/history.sqlite3')) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 18:
                raise AssertionError('fixture must be genuinely schema 18')

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', self.case / 'history')
        self.directory = self.case / 'history'
        self.data = self.manifest
        self.key = bytes.fromhex(self.data['key'])
        self.finished = AttemptBinding(**self.data['finished'])
        self.running = AttemptBinding(**self.data['running'])
        self.scope = ResultLeaseBinding(**self.data['running_scope'])
        self.pending = ResultLeaseBinding(**self.data['pending'])
        self.now = self.data['now']
        self.store = None
        self.addCleanup(lambda: self.store.close() if self.store else None)

    def upgrade(self):
        if self.store:
            self.store.close()
        self.store = project_history.ProjectHistory(self.directory, self.data['project'], result_key=self.key,
            result_capacity=ResultCapacity(256, 256 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * 86_400_000),
            result_clock_ms=lambda: self.now, allow_legacy_orchestration_reference=True, allow_schema_upgrade=True)
        return self.store

    def hold(self, kind, identity):
        row = self.store._connection.execute('SELECT * FROM project_holds WHERE kind=? AND identity=?',
                                              (kind, identity)).fetchone()
        return None if row is None else dict(row)

    def snapshot(self):
        db = self.store._connection
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'runtime_takeover'")}

    @requirements('RSF-S6-1', 'RSF-S6-2', 'RSF-S6-5')
    def test_mixed_store_backfills_orchestration_open_duty_and_preparation_without_guessing(self):
        self.upgrade()
        for handle in (self.finished.orchestration_handle, self.data['paused']):
            row = self.hold('orchestration', handle)
            self.assertEqual('held', row['state'])
            self.assertGreater(row['due_at_ms'], row['taken_at_ms'])
        self.assertIsNone(self.hold('orchestration', self.data['closed']))
        duty = self.hold('open_binding', project_holds_schema.attempt_identity(self.pending.run_handle, self.pending.attempt))
        self.assertIsNotNone(duty)
        self.assertEqual('held', duty['state'])
        evidence = json.loads(duty['evidence'])
        self.assertEqual('legacy_open_inputs_missing', evidence['reason'])
        self.assertTrue(evidence['next_step'])
        before_duty = dict(duty)
        self.assertEqual(0, self.store.withdraw_earlier_open_bindings('current-instance',
            project_id=self.data['project'], runtime_digest=self.store._fence.digest))
        self.assertEqual(before_duty, self.hold('open_binding', duty['identity']))
        self.assertEqual(self.pending.run_id, evidence['run_id'])
        done = self.hold('open_binding', project_holds_schema.attempt_identity(self.finished.run_handle, self.finished.attempt))
        self.assertEqual(('released', 'claimed'), (done['state'], done['release_kind']))
        prep = ResultLeaseBinding(**self.data['preparations'][0])
        row = self.hold('lease', project_holds_schema.attempt_identity(prep.run_handle, prep.attempt))
        self.assertEqual('held', row['state'])
        self.assertEqual('legacy_preparation_memory_lost', json.loads(row['evidence'])['reason'])
        self.assertTrue(json.loads(row['evidence'])['next_step'])
        from codex_agents.project_holds_report import holds_report
        report = holds_report(self.store)
        for kind in ('preparation', 'driver_entry'):
            self.assertEqual('tracked', report['project_holds'][kind]['tracking'])
            self.assertEqual(0, report['project_holds'][kind]['held'])
        before = self.snapshot()
        for _ in range(2):
            self.upgrade()
            self.assertEqual(before, self.snapshot())

    @requirements('RSF-S6-3', 'RSF-S6-6')
    def test_unrecoverable_running_attempt_waits_for_b4s_lost_and_refuses_late_publication(self):
        self.upgrade()
        row = self.hold('reservation', self.running.claim_id)
        evidence = json.loads(row['evidence'])
        self.assertEqual('legacy_attempt_needs_settlement', evidence['reason'])
        self.assertTrue(evidence['next_step'])
        self.assertEqual('held', row['state'])
        self.assertEqual('recovered', self.store.arbitrate_publication(self.finished.run_handle, self.finished.attempt, 'finished'))
        self.assertEqual('unrecoverable', self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 's6-lost'))
        self.late_refused()
        lease = self.store.result_lease(self.scope)
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                 self.running.claim_id, lease.lease_digest).payload()
        outbox = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 's6-lost', payload,
                                                    runtime_digest=self.store._fence.digest)
        closure = canonical_json(lost_run_closure(self.key, self.scope, lease_digest=lease.lease_digest,
            assignment_digest=lease.assignment_digest, bridge_run_id=self.running.claim_id, release_kind='lost',
            released_at_ms=self.now + 5, request_digest=outbox['request_digest']))
        self.assertEqual('lost', self.store.record_account_settlement(self.scope, closure)['release_kind'])
        self.assertEqual('lost', self.hold('reservation', self.running.claim_id)['release_kind'])
        self.assertTrue(self.store._attempt_settled(self.store.result_lease(self.scope)))
        from codex_agents.project_orchestration_report import orchestration_report
        from codex_agents.learning_contracts import external_id
        report = orchestration_report(self.store, external_id(self.key, 'orc', self.finished.orchestration_handle),
                                      runtime_digest=self.store._fence.digest)
        self.assertEqual(1, report['runs']['abandoned'])
        self.late_refused()
        self.assertTrue(self.store.record_account_settlement(self.scope, closure)['known'])
        self.assertEqual(self.data['body'].encode(), self.store.read_result(self.finished))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, self.key))
        self.upgrade()
        self.late_refused()

    def late_refused(self):
        before = self.snapshot()
        changes = self.store._connection.total_changes
        files = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.name.startswith(('.result-', '.context-'))}
        with self.assertRaises(HistoryError):
            self.store.publish_result(self.running, self.data['assignment'], self.data['body'].encode(),
                ResultObservation(self.running.claim_id, self.now, False, True, None))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(changes, self.store._connection.total_changes)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.directory.iterdir()
                                if p.name.startswith(('.result-', '.context-'))})

    @requirements('RSF-S6-2', 'RSF-S6-3', 'RSF-S6-4')
    def test_missing_legacy_inputs_keep_context_and_custody_without_fabrication(self):
        # Extend the SAME mixed source using the old bind operation, not row edits.
        (self.case / 'manifest.json').write_text(json.dumps(self.data))
        code = """import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity,ResultLeaseBinding,MAX_RESULT_BODY_BYTES,LEASE_SCOPE_FIELDS
from codex_agents.history_contracts import AttemptBinding
root=Path(sys.argv[2]); data=json.loads((root/'manifest.json').read_text())
scope=ResultLeaseBinding(**data['pending'])
owner=ProjectHistory(root/'history',data['project'],result_key=bytes.fromhex(data['key']),
 result_capacity=ResultCapacity(256,256*MAX_RESULT_BODY_BYTES,1800000,7*86400000),
 result_clock_ms=lambda:data['now'],allow_legacy_orchestration_reference=True)
binding=AttemptBinding(**{name:getattr(scope,name) for name in LEASE_SCOPE_FIELDS},
 claim_id='brg_'+'9'*32,claim_digest='9'*64)
owner.bind_result_lease(scope,binding,data['assignment'],hard_deadline_at_ms=data['now']+1000)
owner.close()
"""
        result = subprocess.run([sys.executable, '-B', '-c', code,
            str(self.archive / 'packages/codex-agents/native-package/.codex/orchestration'), str(self.case)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.upgrade()
        identity = project_holds_schema.attempt_identity(self.pending.run_handle, self.pending.attempt)
        context = self.store._connection.execute('SELECT state,context_digest,context_size,host_class '
            'FROM project_result_contexts WHERE run_handle=?', (self.pending.run_handle,)).fetchone()
        self.assertEqual(('legacy_no_context', None, 0, None), tuple(context))
        row = self.hold('result_custody', identity)
        self.assertEqual('held', row['state'])
        self.assertEqual('legacy_result_inputs_missing', json.loads(row['evidence'])['reason'])
        self.assertTrue(json.loads(row['evidence'])['next_step'])
        self.assertEqual('held', self.hold('reservation', 'brg_' + '9' * 32)['state'])
        before = self.snapshot()
        self.upgrade()
        self.assertEqual(before, self.snapshot())

    @requirements('RSF-S6-4')
    def test_other_runtime_guidance_keeps_custody_and_names_update_recovery(self):
        from codex_agents.refusal_steps import next_step
        step = next_step('history_unrecordable_other_runtime')
        self.assertIn('update', step)
        self.assertNotIn('start that version', step)

    @requirements('RSF-S6-4')
    def test_finished_result_remains_recordable_only_by_current_fenced_runtime_after_two_reopens(self):
        self.upgrade()
        old_bytes = self.data['body'].encode()
        self.assertEqual(old_bytes, self.store.read_result(self.finished))
        historical = [tuple(row) for row in self.store._connection.execute('SELECT * FROM records ORDER BY rowid')]
        for _ in range(2):
            self.upgrade()
        authority = service_record.ServiceRecordAuthority(self.store._fence.digest, True)
        self.assertNotEqual(self.finished.runtime_digest, authority.runtime_digest)
        before = self.snapshot()
        with self.assertRaisesRegex(HistoryError, 'history_unrecordable_other_runtime'):
            self.store.admit_service_result(self.finished, final_check=service_record.ServiceRecordAuthority('f' * 64, True))
        self.assertEqual(before, self.snapshot())
        answer = self.store.admit_service_result(self.finished, final_check=authority)
        self.assertTrue(answer['admission']['host_outcome_digest'])
        self.assertEqual(old_bytes, self.store.read_result(self.finished))
        self.assertEqual(historical, [tuple(row) for row in self.store._connection.execute(
            'SELECT * FROM records ORDER BY rowid LIMIT ?', (len(historical),))])


    @requirements('RSF-S6-1', 'RSF-S6-2', 'RSF-S6-4', 'RSF-S6-5', 'RSF-S6-6')
    def test_each_class_commit_survives_sigkill_and_two_reopens_identically(self):
        self.upgrade()
        expected = self.snapshot()
        self.store.close()
        for target, cursor in (('lease', 64), ('reservation', 2), ('context', 2),
                               ('result_custody', 2), ('orchestration', 3)):
            for phase in ('before', 'after'):
                with self.subTest(target=target, phase=phase):
                    case = Path(tempfile.mkdtemp(dir=self.root))
                    shutil.copytree(self.source / 'history', case / 'history')
                    (case / 'manifest.json').write_text(json.dumps(self.data))
                    code = ('from tests.test_rsf_s6_classify import migration_worker; '
                            'import sys; migration_worker(*sys.argv[1:])')
                    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(case), target, str(cursor), phase],
                        cwd=HERE.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env={**os.environ, 'PYTHONPATH': str(TOP / 'scripts')})
                    try:
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'boundary not reached')
                        message = child.stdout.readline()
                        self.assertTrue(message, child.stderr.read() if child.poll() is not None else 'no boundary')
                        self.assertEqual(phase, json.loads(message)['boundary'])
                        child.kill()
                        child.wait(timeout=5)
                        self.assertEqual(-signal.SIGKILL, child.returncode)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.communicate(timeout=5)
                    with closing(sqlite3.connect(case / 'history/history.sqlite3')) as db:
                        if target == 'lease' and phase == 'before':
                            # S3's first uncommitted checkpoint restores the original schema.
                            self.assertEqual(18, db.execute('PRAGMA user_version').fetchone()[0])
                            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='hold_migration_progress'").fetchone())
                        else:
                            progress = db.execute('SELECT cursor FROM hold_migration_progress WHERE class=?', (target,)).fetchone()
                            self.assertEqual(cursor if phase == 'after' else None, progress[0])
                    self.directory = case / 'history'
                    for _ in range(2):
                        self.upgrade()
                        self.assertEqual(expected, self.snapshot())

    @requirements('RSF-S6-6')
    def test_revoked_epoch_rolls_back_classification_and_its_cursor(self):
        real = store_fence.FencedConnection._fenced
        def revoke(connection, sql, operation):
            if sql.strip().upper() == 'COMMIT' and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='hold_migration_progress'").fetchone():
                row = connection.execute("SELECT cursor FROM hold_migration_progress WHERE class='reservation'").fetchone()
                if row is not None and row[0] == 2:
                    sqlite3.Connection.execute(connection, 'UPDATE runtime_takeover SET epoch=epoch+1')
            return real(connection, sql, operation)
        with mock.patch.object(store_fence.FencedConnection, '_fenced', revoke):
            with self.assertRaises(HistoryError):
                self.upgrade()
        with closing(sqlite3.connect(self.directory / 'history.sqlite3')) as db:
            self.assertEqual((None, 0), db.execute(
                "SELECT cursor,done FROM hold_migration_progress WHERE class='reservation'").fetchone())
            self.assertIsNone(db.execute("SELECT evidence FROM project_holds WHERE kind='reservation' AND identity=?",
                                        (self.running.claim_id,)).fetchone()[0])
            self.assertEqual(1, db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])
        self.upgrade()
        self.assertEqual('legacy_attempt_needs_settlement',
                         json.loads(self.hold('reservation', self.running.claim_id)['evidence'])['reason'])


def migration_worker(root, target, cursor, phase):
    root = Path(root)
    data = json.loads((root / 'manifest.json').read_text())
    real = store_fence.FencedConnection._fenced
    reached = False
    def watch(connection, sql, operation):
        nonlocal reached
        commit = sql.strip().upper() == 'COMMIT'
        if commit and not reached and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='hold_migration_progress'").fetchone():
            row = connection.execute('SELECT cursor FROM hold_migration_progress WHERE class=?', (target,)).fetchone()
            if row is not None and row[0] == int(cursor):
                reached = True
                if phase == 'after':
                    result = real(connection, sql, operation)
                print(json.dumps({'boundary': phase}), flush=True)
                sys.stdin.readline()  # parent kills this exact isolated process
                if phase == 'after':
                    return result
        return real(connection, sql, operation)
    with mock.patch.object(store_fence.FencedConnection, '_fenced', watch):
        project_history.ProjectHistory(root / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_capacity=ResultCapacity(256, 256 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * 86_400_000),
            result_clock_ms=lambda: data['now'], allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True).close()

