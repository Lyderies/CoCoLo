"""B4t takeover tests, part store (CI round 6 split of test_b4t_takeover, test alignment 126; classes moved verbatim; the shared journal harness stays in test_b4t_takeover)."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
import subprocess
import sys
import shutil
from contextlib import closing, ExitStack, contextmanager

import tests.support  # noqa: F401
from test_framework import requirements
from tests.test_b4t_takeover import A, B, C, run_entry, Operations


class StoreTests(unittest.TestCase):
    __why_not_scenario__ = 'Archived 0.8.66 business methods create authentic custody; disposable stores only.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-store-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        here = Path(__file__).parent
        packed = subprocess.run(['git', '-C', str(here.parents[2]), 'archive', 'afb6f610',
                                 'packages/codex-agents', 'scripts'], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(cls.archive / 'scripts')
        made = subprocess.run([sys.executable, '-B', str(here / 'b4t_fixture.py'), str(cls.archive),
                               str(cls.source), getattr(cls, 'fixture_mode', 'build')],
                              env=env, capture_output=True, text=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        cls.data = json.loads((cls.source / 'manifest.json').read_text())

    def setUp(self):
        from codex_agents.history_contracts import AttemptBinding
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', self.case / 'history')
        self.finished = AttemptBinding(**self.data['finished'])
        self.running = AttemptBinding(**self.data['running'])
        self.scope = ResultLeaseBinding(**self.data['running_scope'])
        self.key = bytes.fromhex(self.data['key'])
        self.store = None
        self.addCleanup(lambda: self.store.close() if self.store else None)
        self.intent = dict(takeover_id='1' * 32, project_id=self.data['project'], root_digest='d' * 64,
            predecessor_service=dict(version='0.8.66', digest=self.finished.runtime_digest,
                                     entry='old/project_host.py'),
            predecessor_collector=dict(version='0.8.66', digest=self.finished.runtime_digest),
            successor=dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST),
            runs=[dict(run_handle=self.running.run_handle, attempt=self.running.attempt,
                       bridge_run_id=self.running.claim_id)], phase='store_adopting', recorded_at_ms=self.data['now'])

    def open(self, *, takeover=True):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        if self.store:
            self.store.close()
        self.store = ProjectHistory(self.case / 'history', self.data['project'], result_key=self.key,
            result_capacity=ResultCapacity(256, 256 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * 86_400_000),
            result_clock_ms=lambda: self.data['now'], allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True, takeover=self.intent if takeover else None)
        return self.store

    def rows(self, name):
        return [tuple(row) for row in self.store._connection.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]

    def primary_record(self, *, corrupt=False):
        from codex_agents.hosts import CODEX_HOST_CLIENT
        from codex_agents.host_result_contracts import HostResultReference
        from codex_agents.service_record import ServiceRecordAuthority
        reservation = self.store.result_reservation(self.finished)
        terminal = self.store.record(reservation.terminal_record_id)
        reference = HostResultReference(self.finished, terminal.record_id,
                                        'f' * 64 if corrupt else terminal.content_digest, reservation.result_digest)
        original = self.store.stored_result_context(self.finished, host_class=CODEX_HOST_CLIENT)
        return self.store.admit_host_result(reference, assignment=original.assignment, task=original.task,
            route=original.route, client_capability=original.client_capability, before_commit=lambda: None,
            final_check=ServiceRecordAuthority(self.store._fence.digest, False))

    def predecessor_records(self, *, classify_only=False):
        (self.case / 'manifest.json').write_text(json.dumps(self.data))
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.project_history import ProjectHistory
from codex_agents.history_contracts import AttemptBinding
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
from codex_agents.service_record import ServiceRecordAuthority
root=Path(sys.argv[2]);data=json.loads((root/'manifest.json').read_text())
owner=ProjectHistory(root/'history',data['project'],result_key=bytes.fromhex(data['key']),
 result_capacity=ResultCapacity(256,256*MAX_RESULT_BODY_BYTES,1800000,7*86400000),
 result_clock_ms=lambda:data['now'],allow_legacy_orchestration_reference=True)
try:
 if sys.argv[3]=='classify':
  with owner._transaction(): owner._classify_legacy_custody(None)
 else:
  result=owner.admit_service_result(AttemptBinding(**data['finished']),
   final_check=ServiceRecordAuthority(owner._fence.digest,True))
  assert result['admission']['host_outcome_digest']
finally: owner.close()
'''
        done = subprocess.run([sys.executable, '-B', '-c', code, str(runtime), str(self.case),
                               'classify' if classify_only else 'record'],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(0, done.returncode, done.stderr)

    @requirements('RS-B-B4t-2')
    def test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store(self):
        # The archived fixture is first captured before publish; its old writer
        # commits a real result at the service-stop boundary, before adoption.
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.project_takeover import stopped_children
        from codex_agents.history_contracts import AttemptBinding
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        made = subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('b4t_fixture.py')),
            str(self.archive),str(self.case),'stop-result'],env={**os.environ,'PYTHONPATH':str(self.archive/'scripts')},
            capture_output=True,text=True,timeout=30)
        self.assertEqual(0,made.returncode,made.stderr)
        self.data=json.loads((self.case/'manifest.json').read_bytes())
        self.running=AttemptBinding(**self.data['running'])
        self.finished=self.running
        self.key=bytes.fromhex(self.data['key'])
        self.intent.update(project_id=self.data['project'],recorded_at_ms=self.data['now'],
                           runs=[dict(run_handle=self.running.run_handle,attempt=self.running.attempt,bridge_run_id=self.running.claim_id)])
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        with closing(sqlite3.connect(self.case/'history/history.sqlite3')) as db:
            self.assertIsNone(db.execute('SELECT committed_at_ms FROM result_reservations WHERE claim_id=?',
                                        (self.running.claim_id,)).fetchone()[0])
        # A real old writer publishes, through the same product methods as its
        # service. It is closed before the successor can take the lifetime lock.
        code = """import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.project_history import ProjectHistory
from codex_agents.history_contracts import AttemptBinding
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
from codex_agents.result_contracts import ResultObservation
root=Path(sys.argv[2]); data=json.loads((root/'manifest.json').read_text())
binding=AttemptBinding(**data['running'])
owner=ProjectHistory(root/'history',data['project'],result_key=bytes.fromhex(data['key']),
 result_capacity=ResultCapacity(256,256*MAX_RESULT_BODY_BYTES,1800000,7*86400000),
 result_clock_ms=lambda:data['now'],allow_legacy_orchestration_reference=True)
body=data['body'].encode()
try:
 owner.publish_result(binding,data['assignment'],body,ResultObservation(binding.claim_id,data['now'],False,True,None))
finally:owner.close()
"""
        (self.case/'manifest.json').write_text(json.dumps(self.data))
        done = subprocess.run([sys.executable,'-B','-c',code,str(runtime),str(self.case)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,done.returncode,done.stderr)
        self.open()
        self.assertIsNotNone(self.store.result_reservation(self.running).committed_at_ms)
        self.assertEqual(0, stopped_children(self.store, self.intent['runs']))
        ServiceRecordAuthority(self.store._fence.digest, True).check(self.store,self.running,'K1')
        self.assertIsNotNone(self.store.read_result(self.running))

    @requirements('RS-B-B4t-8')
    def test_nonempty_takeover_counts_are_produced_by_the_real_history_handler(self):
        from codex_agents.history_service import HistoryService,HistoryAuthorization
        from codex_agents.history_api import HistoryRequest,history_request_digest,encode_history_request,decode_history_response
        self.open()
        request=HistoryRequest(1,'history.takeover_report','takeover',self.data['project'],self.store._fence.digest,{})
        service=HistoryService(request.project_id,request.runtime_digest,lambda:self.store,close_history=False,
            authorizer=lambda offered,token:HistoryAuthorization(request.project_id,request.runtime_digest,
                history_request_digest(offered),'host',frozenset({'history.takeover_report'})))
        before=self.store._connection.total_changes
        with closing(service):
            reply=decode_history_response(service.handle(encode_history_request(request),object()),request)
        self.assertEqual((1,0),(reply['records'][0]['runs_listed'],reply['records'][0]['runs_unlisted']))
        self.assertEqual(before,self.store._connection.total_changes)

    @requirements('RS-B-B4t-8','RS-B-B4t-10')
    def test_B4t_A1_overflow_count_survives_the_journal(self):
        from codex_agents.takeover_journal import TakeoverJournal
        from codex_agents.project_takeover import takeover_report,takeover_lines
        from codex_agents.project_history import SETTLEMENT_REPLAY_WINDOW_MS
        journal=TakeoverJournal(self.case)
        intent=Operations(self.case).preflight()
        document=journal.create(intent,[run_entry(i) for i in range(70)])
        for _ in range(4):
            document=journal.reconcile(document,[run_entry(i) for i in range(70)])
        self.assertEqual((64,6),(len(document['runs']),document['runs_unlisted']))
        self.intent.update(runs=document['runs'],runs_unlisted=document['runs_unlisted'])
        self.open()
        row=self.rows('takeover_record')[0]
        self.assertEqual((64,6),row[-2:])
        from codex_agents.project_takeover import complete_takeovers
        complete_takeovers(self.store)
        self.assertEqual('complete',self.rows('takeover_record')[0][5])
        journal.remove()  # J14's product removal; status has no journal left.
        with mock.patch.object(TakeoverJournal,'read',side_effect=AssertionError('status depended on removed journal')):
            self.assertIn('70 runs ended by the update (6 of them not listed one by one)',
                          '\n'.join(takeover_lines(takeover_report(self.store))))
            self.store._purge_takeover(self.data['now']+SETTLEMENT_REPLAY_WINDOW_MS+1,0)
            self.assertEqual([],self.rows('takeover_runs'))
            self.assertIn('70 runs ended by the update (6 of them not listed one by one)',
                          '\n'.join(takeover_lines(takeover_report(self.store))))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-5')
    def test_B4t_2_committed_results_stay_recordable(self):
        from codex_agents.service_record import ServiceRecordAuthority
        self.open()
        before = self.store.read_result(self.finished)
        for _ in range(2):
            authority = ServiceRecordAuthority(self.store._fence.digest, True)
            authority.check(self.store, self.finished, 'K1')
            self.assertEqual(before, self.store.read_result(self.finished))
            self.open(takeover=False)
        recorded = self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertTrue(recorded['admission']['host_outcome_digest'])
        self.assertEqual(1, len(self.rows('project_result_recorders')))
        original_records = self.rows('records')
        self.open(takeover=False)
        self.assertIsNone(self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True)))
        self.assertEqual(original_records, self.rows('records'))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_predecessors_classification_is_not_the_successors_takeover_authority(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.predecessor_records(classify_only=True)
        self.open(takeover=False)
        before = self.rows('records'), self.rows('project_holds')
        with self.assertRaises(HistoryError) as refused:
            self.store.admit_service_result(self.finished,
                final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertEqual('history_unrecordable_other_runtime', refused.exception.reason_code)
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))
        self.open()
        self.assertTrue(self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True))['admission']['host_outcome_digest'])

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_taken_over_result_requires_its_own_takeover_authority(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        with self.store._transaction():
            row = self.store._connection.execute("SELECT evidence FROM project_holds WHERE kind='result_custody' "
                "AND identity=?", (f'{self.finished.run_handle}:{self.finished.attempt}',)).fetchone()
            evidence = json.loads(row[0])
            self.assertIn('takeover_id', evidence)
            del evidence['takeover_id']
            self.store._connection.execute("UPDATE project_holds SET evidence=? WHERE kind='result_custody' AND identity=?",
                (json.dumps(evidence), f'{self.finished.run_handle}:{self.finished.attempt}'))
        before = self.rows('records'), self.rows('project_holds')
        with self.assertRaises(HistoryError):
            self.store.admit_service_result(self.finished,
                final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_result_cannot_borrow_an_unrelated_or_noncurrent_takeover_record(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        from codex_agents.collector_client import PACKAGE_VERSION
        self.open()
        identity = f'{self.finished.run_handle}:{self.finished.attempt}'
        with self.store._transaction():
            self.store._connection.execute('INSERT INTO takeover_record(takeover_id,predecessor_digest,predecessor_version,successor_digest,successor_version,state,recorded_at_ms,runs_listed,runs_unlisted) VALUES(?,?,?,?,?,?,0,0,0)',
                ('6' * 32, '5' * 64, '0.8.60', '6' * 64, '0.8.61', 'adopted'))
            evidence = json.loads(self.store._connection.execute("SELECT evidence FROM project_holds "
                "WHERE kind='result_custody' AND identity=?", (identity,)).fetchone()[0])
            evidence['takeover_id'] = '6' * 32
            self.store._connection.execute("UPDATE project_holds SET evidence=? WHERE kind='result_custody' AND identity=?",
                                            (json.dumps(evidence), identity))
        for current in (False, True):
            with self.subTest(current=current):
                if current:
                    with self.store._transaction():
                        self.store._connection.execute('UPDATE takeover_record SET successor_digest=?,successor_version=? '
                            'WHERE takeover_id=?', (self.store._fence.digest, PACKAGE_VERSION, '6' * 32))
                before = self.rows('records'), self.rows('project_holds')
                with self.assertRaises(HistoryError):
                    self.store.admit_service_result(self.finished,
                        final_check=ServiceRecordAuthority(self.store._fence.digest, True))
                self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_tampered_takeover_run_is_refused_without_relabelling_another_run(self):
        from codex_agents.project_takeover import takeover_report
        self.open()
        with self.store._transaction():
            self.store._connection.execute('UPDATE takeover_runs SET run_handle=?', (self.finished.run_handle,))
        before = self.rows('project_holds'), self.rows('result_leases')
        report = takeover_report(self.store, run_handle=self.finished.run_handle)
        self.assertEqual(1, report['refused_unverifiable'])
        self.assertEqual((0, 0), (report['lost'], report['lost_held']))
        self.assertEqual(before, (self.rows('project_holds'), self.rows('result_leases')))

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_run_cannot_borrow_an_unrelated_takeover_id_for_its_annotation(self):
        from codex_agents.project_takeover import takeover_report
        self.open()
        with self.store._transaction():
            self.store._connection.execute('INSERT INTO takeover_record(takeover_id,predecessor_digest,predecessor_version,successor_digest,successor_version,state,recorded_at_ms,runs_listed,runs_unlisted) VALUES(?,?,?,?,?,?,0,0,0)',
                ('6' * 32, '5' * 64, '0.8.60', '6' * 64, '0.8.61', 'adopted'))
            self.store._connection.execute('UPDATE takeover_runs SET takeover_id=?', ('6' * 32,))
        before = self.rows('project_holds'), self.rows('result_leases')
        report = takeover_report(self.store, run_handle=self.running.run_handle)
        self.assertEqual(1, report['refused_unverifiable'])
        self.assertEqual([], report['records'])
        self.assertEqual((0, 0), (report['lost'], report['lost_held']))
        self.assertEqual(before, (self.rows('project_holds'), self.rows('result_leases')))

    @requirements('RS-B-B4t-3')
    def test_B4t_3_an_old_binary_cannot_reopen_either_store(self):
        self.open().close()
        self.store = None
        old_runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from codex_agents.project_history import ProjectHistory;"
                "ProjectHistory(Path(sys.argv[2]),sys.argv[3])")
        before = (self.case / 'history/history.sqlite3').read_bytes()
        done = subprocess.run([sys.executable, '-B', '-c', code, str(old_runtime), str(self.case / 'history'),
                               self.data['project']], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(0, done.returncode)
        self.assertRegex(done.stderr, 'newer|incompatible')
        self.assertEqual(before, (self.case / 'history/history.sqlite3').read_bytes())

    @requirements('RS-B-B4t-3')
    def test_B4t_3_injected_stale_owner_commits_nothing(self):
        from codex_agents.history_contracts import HistoryError
        import fcntl
        stale = self.open()
        fcntl.flock(stale._writer_fd, fcntl.LOCK_UN)
        self.store = None
        successor = self.open()
        self.addCleanup(stale.close)
        before = self.rows('records')
        with self.assertRaises(HistoryError):
            stale._connection.execute('DELETE FROM records')
        stale._connection.rollback()
        self.assertEqual(before, self.rows('records'))
        self.assertIsNotNone(successor._fence.epoch)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_real_predecessor_is_excluded_by_proven_exit(self):
        from codex_agents.history_contracts import HistoryError
        import select
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from codex_agents.project_history import ProjectHistory;"
                "s=ProjectHistory(Path(sys.argv[2]),sys.argv[3]);print('ready',flush=True);"
                "sys.stdin.readline();s.close()")
        p = subprocess.Popen([sys.executable, '-B', '-c', code, str(runtime), str(self.case / 'history'),
                              self.data['project']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.kill() if p.poll() is None else None)
        self.assertTrue(select.select([p.stdout], [], [], 10)[0])
        self.assertEqual('ready', p.stdout.readline().strip())
        with self.assertRaises(HistoryError):
            self.open()
        p.communicate('\n', timeout=10)
        self.assertEqual(0, p.returncode)
        self.open()

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once(self):
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.learning_contracts import canonical_json
        from codex_agents.lost_run_closure import lost_run_closure
        from codex_agents.project_takeover import takeover_report
        self.open()
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'lost-' + '2' * 64)
        lease = self.store.result_lease(self.scope)
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                 self.running.claim_id, lease.lease_digest).payload()
        outbox = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'lost-' + '2' * 64,
                                                    payload, runtime_digest=self.store._fence.digest)
        report = takeover_report(self.store)
        self.assertEqual(1, report['lost_held'])
        closure = canonical_json(lost_run_closure(self.key, self.scope, lease_digest=lease.lease_digest,
            assignment_digest=lease.assignment_digest, bridge_run_id=self.running.claim_id, release_kind='lost',
            released_at_ms=self.data['now'] + 5, request_digest=outbox['request_digest']))
        for _ in range(2):
            self.store.record_account_settlement(self.scope, closure)
        report = takeover_report(self.store)
        self.assertEqual((1, 0), (report['lost'], report['lost_held']))
        self.assertEqual(1, len(self.rows('takeover_runs')))

    @requirements('RS-B-B4t-8')
    def test_B4t_8_status_names_the_takeover(self):
        from codex_agents.project_takeover import takeover_report, takeover_lines
        self.open()
        report = takeover_report(self.store)
        lines = '\n'.join(takeover_lines(report))
        self.assertIn('Update hand-over', lines)
        self.assertIn('0.8.66', lines)
        self.assertIn('finished results kept', lines)
        self.assertNotIn(str(self.case), lines)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_post_stop_count_includes_an_unsettled_orphan_but_excludes_a_result_committed_before_stop(self):
        from codex_agents.project_takeover import stopped_children
        self.open()
        finished = dict(run_handle=self.finished.run_handle, attempt=self.finished.attempt,
                        bridge_run_id=self.finished.claim_id)
        self.assertEqual(1, stopped_children(self.store, [*self.intent['runs'], finished]))
        self.assertEqual(0, stopped_children(self.store, [finished]))
        forged = dict(self.intent['runs'][0], run_handle=self.finished.run_handle)
        self.assertEqual(0, stopped_children(self.store, [forged]))

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_T0_does_not_ignore_an_offline_predecessor_store_without_a_trusted_package(self):
        from codex_agents.learning_store import LearningState
        from codex_agents.collector_contracts import CollectorError
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.update_takeover import TakeoverError
        state = LearningState(self.case / 'state')
        state.key_path.write_bytes(self.key)
        project = self.case / 'project'
        project.mkdir()
        directory = state.root / 'projects' / self.data['project']
        directory.parent.mkdir(mode=0o700)
        shutil.copytree(self.source / 'history', directory)
        row = dict(project_id=self.data['project'], canonical_root=str(project), launch_copy=None, launch_digest=None)
        database = directory / 'history.sqlite3'
        before = database.read_bytes()
        with mock.patch('codex_agents.takeover_preflight._registry', return_value=(row, 18, {})), \
                mock.patch('codex_agents.takeover_preflight.CollectorClient._request', side_effect=CollectorError('down')), \
                mock.patch('codex_agents.project_host_client._hinted_host', return_value=None):
            with self.assertRaises(TakeoverError) as caught:
                read_plan(state, project)
        self.assertEqual('predecessor_unverified', caught.exception.code)
        self.assertEqual(before, database.read_bytes())
        self.assertFalse((state.root / 'update-takeover.json').exists())

    @requirements('RS-B-B4t-8')
    def test_status_wording_uses_fixed_outcomes_and_other_project_commands(self):
        from codex_agents.project_takeover import takeover_lines
        report = dict(records=[dict(predecessor_version='0.8.66', successor_version='0.8.67', state='complete')],
                      lost=1, lost_held=0, finished_kept=2, refused_unverifiable=0)
        account = dict(recorded_at_ms=0, other_projects=1, other_runs=1,
                       updates=[dict(version='0.8.66', children=1, update_message='Update command from B4m')])
        text = '\n'.join(takeover_lines(report, account=account))
        self.assertIn('1970-01-01 00:00 UTC, CoCoLo 0.8.66 to 0.8.67', text)
        self.assertIn('1 run ended by the update. 2 finished results kept.', text)
        self.assertIn('Runs ended by the update: 1, slot freed.', text)
        self.assertIn('Other projects still on an earlier version: 1.', text)
        self.assertIn('Update command from B4m', text)
        report['refused_unverifiable'] = 3
        self.assertIn('Kept unchanged, could not be verified: 3 records of CoCoLo 0.8.66. Nothing was freed.',
                      '\n'.join(takeover_lines(report)))

    @requirements('RS-B-B4t-4')
    def test_B4t_4_memory_holds_and_pending_removals_carry_over(self):
        # Adoption cannot manufacture memory-release evidence or remove retry duties.
        with closing(sqlite3.connect(self.case / 'history/history.sqlite3')) as db:
            before = db.execute("SELECT * FROM project_holds WHERE kind IN ('preparation','driver_entry')").fetchall()
        self.open()
        after = [tuple(row) for row in self.store._connection.execute(
            "SELECT * FROM project_holds WHERE kind IN ('preparation','driver_entry')")]
        self.assertEqual(before, after)

    @requirements('RS-B-B4t-4')
    def test_B4t_4_open_bindings_end_by_their_own_rules(self):
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.project_holds_schema import attempt_identity
        self.open()
        before = self.rows('result_leases')
        self.open()
        self.assertEqual(before, self.rows('result_leases'))
        self.assertEqual('bound', self.store.result_lease(self.scope).state)
        bound_id=attempt_identity(self.running.run_handle,self.running.attempt)
        pending=ResultLeaseBinding(**self.data['pending'])
        pending_id=attempt_identity(pending.run_handle,pending.attempt)
        def duty(identity):
            return dict(self.store._connection.execute("SELECT * FROM project_holds WHERE kind='open_binding' AND identity=?",(identity,)).fetchone())
        self.assertEqual(('released','claimed'),(duty(bound_id)['state'],duty(bound_id)['release_kind']))
        before=duty(pending_id)
        self.assertEqual('held',before['state'])
        self.store.withdraw_earlier_open_bindings('new-instance',project_id=self.data['project'],runtime_digest=self.store._fence.digest)
        self.assertEqual(before,duty(pending_id))
        self.assertEqual('legacy_open_inputs_missing',json.loads(before['evidence'])['reason'])
        self.test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once()
        self.assertEqual(('released','claimed'),(duty(bound_id)['state'],duty(bound_id)['release_kind']))
        # The account's durable open in flight is a different lifecycle: a new
        # request conflicts with its original key until the old window expires.
        from tests.test_account_original_open import OriginalAccountOpenPendingTests
        from codex_agents.account_open_contracts import OpenError
        from codex_agents.learning_store import LearningLedger
        case=OriginalAccountOpenPendingTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        original,_=case.preflight()
        rows=case.pending()
        state=case.ledger.state
        case.ledger.close()
        case.ledger=LearningLedger(state,allow_schema_upgrade=True,takeover=self.intent)
        self.addCleanup(case.ledger.close)
        self.assertEqual(rows,case.pending())
        with self.assertRaisesRegex(OpenError,'open_conflict'):
            case.preflight(request_id='successor-open')
        self.assertEqual(rows,case.pending())
        from codex_agents.learning_store import OPEN_REQUEST_RETENTION_MS
        case.ledger.sweep_account_retention(now_ms=original.observation_deadline_at_ms+OPEN_REQUEST_RETENTION_MS+1)
        self.assertEqual([],case.pending())

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_B4t_4_rollover_replay_across_the_takeover(self):
        self.open()
        before = self.rows('project_orchestrations')
        self.open()
        self.assertEqual(before, self.rows('project_orchestrations'))
        self.assertFalse(any(row[-1] == 'closed' for row in self.store._connection.execute(
            'SELECT * FROM project_orchestrations WHERE orchestration_handle=?',
            (self.finished.orchestration_handle,))))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_adopted_open_and_paused_orchestrations_are_not_closed_by_runtime_change(self):
        from codex_agents.orchestration_api import OrchestrationScope, SCOPE_FIELDS
        self.open()
        owner = self.store._orchestration_owner()
        current = OrchestrationScope(**{name: getattr(self.finished, name) for name in SCOPE_FIELDS})
        current = replace(current, runtime_digest=self.store._fence.digest)
        before = self.rows('project_orchestrations'), self.rows('project_holds')
        self.assertEqual(0, owner.end_earlier(current, 10))
        self.assertEqual(before, (self.rows('project_orchestrations'), self.rows('project_holds')))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-8', 'RS-B-B4t-10')
    def test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata(self):
        from codex_agents.project_takeover import complete_takeovers, takeover_report
        self.open()
        before = self.rows('result_leases'), self.rows('project_orchestrations'), self.rows('takeover_obligations')
        self.assertEqual('adopted', self.store._connection.execute('SELECT state FROM takeover_record').fetchone()[0])
        complete_takeovers(self.store)
        complete_takeovers(self.store)
        self.assertEqual('complete', self.store._connection.execute('SELECT state FROM takeover_record').fetchone()[0])
        self.assertEqual(before, (self.rows('result_leases'), self.rows('project_orchestrations'), self.rows('takeover_obligations')))
        report = takeover_report(self.store)
        self.assertEqual({'predecessor_version', 'successor_version', 'state', 'runs_listed', 'runs_unlisted'}, set(report['records'][0]))
        self.assertEqual((1, 0), (report['records'][0]['runs_listed'], report['records'][0]['runs_unlisted']))
        self.assertEqual('complete', report['records'][0]['state'])

    @requirements('RS-B-B4t-5')
    def test_B4t_5_no_double_record_in_any_order(self):
        from codex_agents.service_record import ServiceRecordAuthority
        for order in (('service', 'primary'), ('primary', 'service'), ('old', 'service', 'primary')):
            with self.subTest(order=order):
                if self.store is not None:
                    self.store.close()
                    self.store = None
                self.case = Path(tempfile.mkdtemp(dir=self.root))
                shutil.copytree(self.source / 'history', self.case / 'history')
                if order[0] == 'old':
                    self.predecessor_records()
                self.open()
                for operation in order:
                    if operation == 'service':
                        self.store.admit_service_result(self.finished,
                            final_check=ServiceRecordAuthority(self.store._fence.digest, True))
                    elif operation == 'primary':
                        self.primary_record()
                self.assertEqual(1, len(self.rows('project_result_recorders')))
                before = self.rows('records'), self.rows('project_result_recorders')
                for _ in range(2):
                    self.open()
                    self.primary_record()
                    self.assertIsNone(self.store.admit_service_result(self.finished,
                        final_check=ServiceRecordAuthority(self.store._fence.digest, True)))
                    self.assertEqual(before, (self.rows('records'), self.rows('project_result_recorders')))
                self.assertEqual(1, len(self.rows('takeover_record')))

    @requirements('RS-B-B4t-6')
    def test_B4t_6_primary_recheck_idempotent_across_takeover(self):
        from codex_agents.service_record import ServiceRecordAuthority
        self.open()
        before = self.rows('records')
        authority = ServiceRecordAuthority(self.store._fence.digest, False)
        for _ in range(3):
            authority.check(self.store, self.finished, 'K2')
        self.assertEqual(before, self.rows('records'))
        recorded = self.primary_record()
        rows = self.rows('records'), self.rows('project_result_recorders')
        for _ in range(3):
            self.assertEqual(recorded, self.primary_record())
            self.open(takeover=False)
        from codex_agents.history_contracts import HistoryError
        with self.assertRaises(HistoryError):
            self.primary_record(corrupt=True)
        self.assertEqual(rows, (self.rows('records'), self.rows('project_result_recorders')))

    @requirements('UH-5')
    def test_UH_5_a_run_still_held_at_adoption_that_later_goes_releasing_is_not_taken_for_unverified(self):
        """Review r1 HIGH 2 (decision 367): a valid listed run still `held` when B adopts, later `releasing` through
        the successor's own arbitration and outbox, has no obligation row; it is never 'unverified', its conflict is
        retried and, when late, it is reported `hold_overdue`."""
        import time
        from codex_agents import health_holds
        from codex_agents.health_monitor import CheckContext
        from codex_agents.lost_run_contracts import SettleLostInput
        self.open()
        state = "SELECT state FROM project_holds WHERE kind='reservation' AND identity=?"
        self.assertEqual('held', self.store._connection.execute(state, (self.running.claim_id,)).fetchone()[0])
        lease = self.store.result_lease(self.scope)
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'b4t-lost')
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                  self.running.claim_id, lease.lease_digest).payload()
        self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost', payload,
                                           runtime_digest=self.store._fence.digest)
        self.assertEqual('releasing', self.store._connection.execute(state, (self.running.claim_id,)).fetchone()[0])
        self.assertIs(False, self.store.takeover_unverified(self.running.run_handle, self.running.attempt,
                                                            self.running.claim_id))
        with self.store._transaction():
            self.store._connection.execute(
                "UPDATE project_holds SET due_at_ms=NULL,next_check_ms=9000000000000 WHERE NOT "
                "(kind='reservation' AND identity=?)", (self.running.claim_id,))
            self.store._connection.execute(
                "UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 WHERE kind='reservation' AND identity=?",
                (self.running.claim_id,))
        check = health_holds.HoldsCheck(None, sweep_interval_seconds=60)
        with closing(health_holds.open_reader(self.case / 'history/history.sqlite3')) as reader:
            found = {f.reason_code for f in check.check_project(
                reader, self.data['project'], CheckContext(time.monotonic() + 5, 61_001, 60_000), self.key)}
        self.assertIn('hold_overdue', found)
        self.assertNotIn('hold_unverified_takeover', found)

    @requirements('RS-B-B4t-5', 'RS-B-B4t-10')
    def test_B4t_5_cross_digest_outbox_replay(self):
        from codex_agents.lost_run_contracts import SettleLostInput
        self.open(takeover=False)
        lease = self.store.result_lease(self.scope)
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'b4t-lost')
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                 self.running.claim_id, lease.lease_digest).payload()
        from codex_agents.launch_journal import LaunchJournal
        journal = LaunchJournal(self.case / 'history/launches')
        journal.claimed(self.running.claim_id, run_handle=self.running.run_handle, attempt=self.running.attempt,
                        host_client='bridge-v1', lease=self.scope.as_data(), identity_digest='d' * 64,
                        claim_request_id=self.scope.claim_request_id,
                        storage_evidence=bytes.fromhex(self.data['running_storage_evidence']), workspace=None)
        journal.release()
        original = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost',
                                                      payload, runtime_digest=self.running.runtime_digest)
        self.open()
        offered = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost',
                                                     payload, runtime_digest=self.store._fence.digest)
        self.assertNotEqual(original['request_digest'], offered['request_digest'])
        self.assertEqual(original['request_digest'], self.store._connection.execute(
            "SELECT request_digest FROM project_holds WHERE kind='reservation' AND identity=?",
            (self.running.claim_id,)).fetchone()[0])
        self.assertEqual(1, len(self.rows('takeover_obligations')))
        self.assertEqual(self.running.runtime_digest, self.rows('takeover_obligations')[0][-1])

    @requirements('RS-B-B4t-9')
    def test_B4t_9_corrupt_receipts_context_and_outbox_are_refused(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        with self.store._transaction():
            self.store._connection.execute('UPDATE project_result_contexts SET context_digest=? WHERE run_handle=?',
                                           ('e' * 64, self.finished.run_handle))
        with self.assertRaises(HistoryError):
            ServiceRecordAuthority(self.store._fence.digest, True).check(self.store, self.finished, 'K1')
        self.assertEqual('committed', self.store.result_reservation(self.finished).state)

    @requirements('RS-B-B4t-9')
    def test_B4t_2_revoked_enrollment_refuses_recording(self):
        from types import SimpleNamespace
        from codex_agents.history_api import HistoryRequest, history_request_digest
        from codex_agents.host_result_contracts import HostResultReference
        from codex_agents.hosts import CODEX_HOST_CLIENT, CLAUDE_HOST_CLIENT
        from codex_agents.service_host import ProjectServiceHost
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        reservation = self.store.result_reservation(self.finished)
        terminal = self.store.record(reservation.terminal_record_id)
        reference = HostResultReference(self.finished, terminal.record_id, terminal.content_digest,
                                        reservation.result_digest)
        request = HistoryRequest(1, 'history.admit_host_result', self.finished.claim_id, self.store.project_id,
                                 self.store._fence.digest, reference.as_data())
        host = ProjectServiceHost.__new__(ProjectServiceHost)
        host._frontend = mock.Mock()
        lane = SimpleNamespace(stored_result_context=lambda binding, host_class:
                               self.store.stored_result_context(binding, host_class=host_class))
        def read_context(read, **kwargs):
            try:
                return read(lane)
            except HistoryError:
                return None
        host._stored = read_context
        enrolled = SimpleNamespace(role='host', host_client=CODEX_HOST_CLIENT,
                                   request_digest=history_request_digest(request))
        host._frontend.context.return_value = SimpleNamespace(kind='project', enrolled=enrolled)
        self.assertIsNotNone(host._authorize_history(request, 'current-token'))
        before = self.rows('records'), self.rows('project_holds'), self.rows('takeover_record')
        host._frontend.context.return_value = None
        self.assertIsNone(host._authorize_history(request, 'revoked-token'))
        enrolled.host_client = CLAUDE_HOST_CLIENT
        host._frontend.context.return_value = SimpleNamespace(kind='project', enrolled=enrolled)
        self.assertIsNone(host._authorize_history(request, 'changed-host-class'))
        from codex_agents.history_service import HistoryService
        from codex_agents.history_api import encode_history_request, decode_history_response
        enrolled.host_client = CODEX_HOST_CLIENT
        entry = SimpleNamespace(kind='project', enrolled=enrolled)
        checks = []
        def reauthorizer(offered, token):
            checks.append(token)
            host._frontend.context.return_value = entry if len(checks) == 1 else None
            return host._authorize_history(offered, token)
        service = HistoryService(self.store.project_id, self.store._fence.digest, lambda: self.store,
                                 authorizer=reauthorizer, close_history=False)
        self.addCleanup(service.close)
        with self.assertRaises(HistoryError) as revoked:
            decode_history_response(service.handle(encode_history_request(request), 'revoked-during-body-IO'), request)
        self.assertEqual('history_not_authorized', revoked.exception.reason_code)
        self.assertGreaterEqual(len(checks), 2)
        forged = replace(self.finished, runtime_digest='f' * 64)
        with self.assertRaises(HistoryError):
            ServiceRecordAuthority(self.store._fence.digest, True).check(self.store, forged, 'K1')
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds'), self.rows('takeover_record')))
        self.assertEqual('committed', self.store.result_reservation(self.finished).state)
        service._authorizer = host._authorize_history
        host._frontend.context.return_value = entry
        result = decode_history_response(service.handle(encode_history_request(request), 'fresh-current-token'), request)
        self.assertTrue(result['admission']['host_outcome_digest'])
        self.assertEqual(1, len(self.rows('project_result_recorders')))

    @requirements('RS-B-B4t-9')
    def test_B4t_machine_local_tables_never_travel(self):
        from codex_agents import learning_source_image, learning_store, project_history
        import inspect
        for module, tables in ((learning_source_image, ('runtime_floor', 'runtime_lineage')),
                               (learning_store, ('runtime_floor', 'runtime_lineage')),
                               (project_history, ('runtime_floor', 'takeover_record', 'takeover_runs',
                                                  'takeover_obligations'))):
            source = inspect.getsource(module)
            for name in tables:
                self.assertIn(name, source)


class ChainedOutboxTests(unittest.TestCase):
    __why_not_scenario__ = 'Two real archived claims, both owner stores and authenticated B4s closures; no live service.'
    fixture_mode = 'chain'
    setUpClass = classmethod(StoreTests.setUpClass.__func__)

    @contextmanager
    def build_identity(self, digest, version):
        from codex_agents import collector_client, project_history, project_takeover, learning_store, account_lost_run
        from codex_agents.runtime_lineage import release_versions
        releases = dict(release_versions(), **{B: '0.8.68'})
        releases[self.data['runtime_digest']] = self.data['version']
        with ExitStack() as stack:
            for module in (collector_client, project_history, project_takeover, learning_store, account_lost_run):
                stack.enter_context(mock.patch.object(module, 'COLLECTOR_RUNTIME_DIGEST', digest))
                if hasattr(module, 'PACKAGE_VERSION'):
                    stack.enter_context(mock.patch.object(module, 'PACKAGE_VERSION', version))
            stack.enter_context(mock.patch('codex_agents.runtime_lineage.release_versions', return_value=releases))
            yield

    def owners(self, root, predecessor, successor, number):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.learning_store import LearningState, LearningLedger
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        data = self.data
        intent = dict(takeover_id=str(number) * 32, project_id=data['project'], recorded_at_ms=data['now'],
            predecessor_collector=predecessor, predecessor_service=predecessor, successor=successor,
            runs=[dict(run_handle=r['binding']['run_handle'], attempt=r['binding']['attempt'],
                       bridge_run_id=r['binding']['claim_id']) for r in data['runs']])
        history = ProjectHistory(root / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
            result_clock_ms=lambda: data['now'], allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(history.close)
        ledger = LearningLedger(LearningState(root / 'state'), allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(ledger.close)
        return history, ledger

    def send(self, history, ledger, run, digest):
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.account_lost_run import settle_lost_run, lost_run_closure
        scope = ResultLeaseBinding(**run['scope'])
        offered = history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                 run['payload'], runtime_digest=digest)
        arguments = dict(runtime_digest=digest, caller_scope='a' * 64, settle=SettleLostInput(**run['payload']),
                         storage_binding=scope, storage_evidence=bytes.fromhex(run['pending']))
        reply = settle_lost_run(ledger, self.data['project'], **arguments)
        self.assertEqual('settled', reply['state'])
        raw = lost_run_closure(ledger, self.data['project'], request_digest=offered['request_digest'], **arguments)
        from codex_agents.learning_contracts import canonical_json
        return scope, canonical_json(raw)

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_corrupt_original_outbox_is_reported_unverifiable_and_kept(self):
        from codex_agents import learning_store
        from codex_agents.project_takeover import takeover_report
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.history_contracts import HistoryError
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        run = self.data['runs'][0]
        with closing(sqlite3.connect(root / 'history/history.sqlite3')) as db:
            db.execute("UPDATE project_holds SET request_digest=? WHERE kind='reservation' AND identity=?",
                       ('f' * 64, run['binding']['claim_id']))
            db.commit()
        with self.build_identity(B, '0.8.68'), \
                mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=self.data['now']), \
                mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                  return_value=self.data['clock_reference_ns']):
            control = Path(tempfile.mkdtemp(dir=self.root))
            shutil.copytree(self.source / 'history', control / 'history')
            shutil.copytree(self.source / 'state', control / 'state')
            intact, control_ledger = self.owners(control,
                dict(version=self.data['version'], digest=self.data['runtime_digest']), dict(version='0.8.68', digest=B), 3)
            baseline = takeover_report(intact)['refused_unverifiable']
            intact.close()
            control_ledger.close()
            history, _ = self.owners(root, dict(version=self.data['version'], digest=self.data['runtime_digest']),
                                     dict(version='0.8.68', digest=B), 3)
            before = [tuple(r) for r in history._connection.execute('SELECT * FROM project_holds ORDER BY rowid')]
            self.assertEqual(baseline + 1, takeover_report(history)['refused_unverifiable'])
            scope = ResultLeaseBinding(**run['scope'])
            with self.assertRaises(HistoryError):
                history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                run['payload'], runtime_digest=B)
            self.assertEqual(before, [tuple(r) for r in history._connection.execute('SELECT * FROM project_holds ORDER BY rowid')])

    def unverified_case(self, *, corrupt):
        """0.8.70 UH: the corrupt-original fixture above (or the intact one), adopted by B, with only the first run's
        holds made due so the holds check would look at them."""
        from codex_agents import learning_store
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        run = self.data['runs'][0]
        if corrupt:
            with closing(sqlite3.connect(root / 'history/history.sqlite3')) as db:
                db.execute("UPDATE project_holds SET request_digest=? WHERE kind='reservation' AND identity=?",
                           ('f' * 64, run['binding']['claim_id']))
                db.commit()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.build_identity(B, '0.8.68'))
        stack.enter_context(mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=self.data['now']))
        stack.enter_context(mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                              return_value=self.data['clock_reference_ns']))
        history, _ = self.owners(root, dict(version=self.data['version'], digest=self.data['runtime_digest']),
                                 dict(version='0.8.68', digest=B), 3)
        with history._transaction():
            history._connection.execute(
                "UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 WHERE state='releasing' AND "
                "((kind='reservation' AND identity=?) OR (kind='lease' AND identity=?))",
                (run['binding']['claim_id'], run['binding']['run_handle'] + ':' + str(run['binding']['attempt'])))
            history._connection.execute(
                "UPDATE project_holds SET due_at_ms=NULL,next_check_ms=9000000000000 WHERE NOT "
                "((kind='reservation' AND identity=?) OR (kind='lease' AND identity=?))",
                (run['binding']['claim_id'], run['binding']['run_handle'] + ':' + str(run['binding']['attempt'])))
        return history, root, run

    def holds_codes(self, root, now=61_001):
        import time
        from codex_agents import health_holds
        from codex_agents.health_monitor import CheckContext
        check = health_holds.HoldsCheck(None, sweep_interval_seconds=60)
        with closing(health_holds.open_reader(root / 'history/history.sqlite3')) as reader:
            found = check.check_project(reader, self.data['project'], CheckContext(time.monotonic() + 5, now, 60_000),
                                        bytes.fromhex(self.data['key']))
        return {f.reason_code: f for f in found}

    @requirements('UH-1', 'UH-2')
    def test_UH_a_hold_the_takeover_left_unverified_is_its_own_plain_finding_not_overdue(self):
        history, root, run = self.unverified_case(corrupt=True)
        self.assertEqual('releasing', history._connection.execute(
            "SELECT state FROM project_holds WHERE kind='reservation' AND identity=?",
            (run['binding']['claim_id'],)).fetchone()[0])
        self.assertEqual('unverified', history._connection.execute(
            "SELECT original_request_digest FROM takeover_obligations WHERE kind='reservation' AND identity=?",
            (run['binding']['claim_id'],)).fetchone()[0])
        found = self.holds_codes(root)
        self.assertNotIn('hold_overdue', found)
        self.assertIn('hold_unverified_takeover', found)
        text = found['hold_unverified_takeover'].text
        for words in ('could not be checked', 'harmless', 'no run slot', 'will not clear by itself'):
            self.assertIn(words, text)

    @requirements('UH-5')
    def test_UH_a_verified_takeover_hold_that_is_overdue_is_still_reported_overdue(self):
        history, root, run = self.unverified_case(corrupt=False)
        found = self.holds_codes(root)
        self.assertIn('hold_overdue', found)
        self.assertNotIn('hold_unverified_takeover', found)

    @requirements('UH-3')
    def test_UH_the_writer_says_which_runs_the_takeover_left_unverified(self):
        corrupt, _, run = self.unverified_case(corrupt=True)
        scope = run['scope']
        self.assertIs(True, corrupt.takeover_unverified(scope['run_handle'], scope['attempt'], run['binding']['claim_id']))
        other = self.data['runs'][1]
        self.assertIs(False, corrupt.takeover_unverified(other['scope']['run_handle'], other['scope']['attempt'],
                                                         other['binding']['claim_id']))
        intact, _, run = self.unverified_case(corrupt=False)
        self.assertIs(False, intact.takeover_unverified(scope['run_handle'], scope['attempt'], run['binding']['claim_id']))

    def reopen(self, root):
        """A later start of the same build: no takeover intent, the store as it was left."""
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        data = self.data
        history = ProjectHistory(root / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
            result_clock_ms=lambda: data['now'], allow_legacy_orchestration_reference=True)
        self.addCleanup(history.close)
        return history

    @requirements('UH-1', 'UH-2', 'UH-6')
    def test_UH_6_the_unverified_fact_written_at_adoption_survives_completion_maintenance_restart_and_a_copy(self):
        """Review r1 HIGH 1 (decision 367): the takeover records the hold it refused in its own adoption transaction;
        completing the journal, the ordinary maintenance pass, a restart and a copied (backed-up, restored or moved)
        store all keep it, so health keeps the plain finding and the writer keeps answering 'unverified'."""
        from codex_agents.project_takeover import complete_takeovers
        history, root, run = self.unverified_case(corrupt=True)
        claim, scope = run['binding']['claim_id'], run['scope']

        def still_unverified(store, where):
            self.assertIs(True, store.takeover_unverified(scope['run_handle'], scope['attempt'], claim))
            found = self.holds_codes(where)
            self.assertNotIn('hold_overdue', found)
            self.assertIn('hold_unverified_takeover', found)
        complete_takeovers(history)
        self.assertEqual({'complete'}, {row[0] for row in history._connection.execute('SELECT state FROM takeover_record')})
        still_unverified(history, root)
        history.expire_results(limit=10)
        still_unverified(history, root)
        history.close()
        restarted = self.reopen(root)
        still_unverified(restarted, root)
        restarted.close()
        copy = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(root / 'history', copy / 'history')
        still_unverified(self.reopen(copy), copy)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-5', 'RS-B-B4t-9', 'RS-B-B4t-10')
    def test_two_archived_outboxes_survive_A_B_C_with_one_effect_and_immutable_originals(self):
        from codex_agents import learning_store
        from codex_agents.history_contracts import HistoryError
        from codex_agents.result_contracts import ResultLeaseBinding
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        data = self.data
        def rows(owner, table):
            return [tuple(r) for r in owner._connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
        with mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=data['now']), \
                mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                  return_value=data['clock_reference_ns']):
            with self.build_identity(B, '0.8.68'):
                history, ledger = self.owners(root, dict(version=data['version'], digest=data['runtime_digest']),
                                              dict(version='0.8.68', digest=B), 3)
                self.assertEqual(2, len(rows(history, 'takeover_obligations')))
                obligations, bindings = rows(history, 'takeover_obligations'), rows(history, 'result_leases')
                originals = [tuple(r) for r in history._connection.execute(
                    "SELECT identity,request_id,request_digest FROM project_holds WHERE kind='reservation' ORDER BY identity")]
                scope, raw = self.send(history, ledger, data['runs'][0], B)
                history.record_account_settlement(scope, raw)
                self.send(history, ledger, data['runs'][1], B)  # accepted; reply never recorded by B
                releases = [tuple(r) for r in ledger._connection.execute(
                    'SELECT handle_digest,released_at_ms,release_digest FROM active_run_capacity ORDER BY handle_digest')]
                history.close()
                ledger.close()
            with self.build_identity(C, '0.8.69'):
                history, ledger = self.owners(root, dict(version='0.8.68', digest=B), dict(version='0.8.69', digest=C), 4)
                from codex_agents.project_takeover import takeover_report
                report = takeover_report(history)
                self.assertEqual((1, 1), (report['lost'], report['lost_held']))
                self.assertEqual(obligations, rows(history, 'takeover_obligations'))
                self.assertEqual(bindings, rows(history, 'result_leases'))
                self.assertEqual(originals, [tuple(r) for r in history._connection.execute(
                    "SELECT identity,request_id,request_digest FROM project_holds WHERE kind='reservation' ORDER BY identity")])
                run = data['runs'][1]
                # Missing either level's final edge never dispatches or frees custody.
                with history._transaction():
                    saved = history._connection.execute('SELECT * FROM takeover_record WHERE predecessor_digest=?', (B,)).fetchone()
                    history._connection.execute('UPDATE takeover_record SET predecessor_digest=? WHERE predecessor_digest=?',
                                                ('d' * 64, B))
                before = rows(history, 'project_holds')
                scope = ResultLeaseBinding(**run['scope'])
                with self.assertRaises(HistoryError):
                    history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                    run['payload'], runtime_digest=C)
                self.assertEqual(before, rows(history, 'project_holds'))
                with history._transaction():
                    history._connection.execute('UPDATE takeover_record SET predecessor_digest=? WHERE takeover_id=?',
                                                (B, saved['takeover_id']))
                from codex_agents.account_claim_contracts import ClaimError
                with ledger._account_transaction():
                    edge = ledger._connection.execute('SELECT * FROM runtime_lineage WHERE predecessor_digest=?', (B,)).fetchone()
                    ledger._connection.execute('DELETE FROM runtime_lineage WHERE predecessor_digest=?', (B,))
                count = ledger._connection.total_changes
                with self.assertRaises(ClaimError):
                    self.send(history, ledger, run, C)
                self.assertEqual(count, ledger._connection.total_changes)
                self.assertEqual(before, rows(history, 'project_holds'))
                with ledger._account_transaction():
                    ledger._connection.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,?,?,?)', tuple(edge))
                for _ in range(2):
                    scope, raw = self.send(history, ledger, run, C)
                    history.record_account_settlement(scope, raw)
                self.assertEqual(releases, [tuple(r) for r in ledger._connection.execute(
                    'SELECT handle_digest,released_at_ms,release_digest FROM active_run_capacity ORDER BY handle_digest')])
                self.assertEqual(2, len(rows(history, 'project_account_settlements')))
                self.assertEqual(obligations, rows(history, 'takeover_obligations'))


class MemoryTakeoverTests(unittest.TestCase):
    __why_not_scenario__ = 'A SIGKILLed archived 0.8.67 fixture leaves genuine memory holds and lock inodes.'
    build_identity = ChainedOutboxTests.build_identity
    fixture_mode = 'memory'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-memory-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        packed = subprocess.run(['git', '-C', str(Path(__file__).parents[3]), 'archive', 'ce81bd96',
                                 'packages/codex-agents', 'scripts'],
                                 capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)

    def setUp(self):
        import select
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(self.archive / 'scripts')
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('b4t_fixture.py')),
            str(self.archive), str(self.case), self.fixture_mode,
            'inside' if 'inside' in self._testMethodName else 'after'],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd='/')
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertTrue(select.select([child.stdout], [], [], 10)[0])
        ready = child.stdout.readline().strip()
        if ready != self.fixture_mode + '-custody-ready':
            child.kill()
            _, error = child.communicate(timeout=5)
            self.fail(ready + error)
        child.kill()
        child.communicate(timeout=5)
        self.assertLess(child.returncode, 0)
        self.data = json.loads((self.case / 'manifest.json').read_bytes())

    def open(self):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        from codex_agents.orchestration_api import OrchestrationLimits
        data = self.data
        old = dict(version=data['version'], digest=data['runtime_digest'])
        intent = dict(takeover_id='5' * 32, project_id=data['project'], recorded_at_ms=data['now'],
            predecessor_service=old, predecessor_collector=old, successor=dict(version='0.8.68', digest=B), runs=[])
        store = ProjectHistory(self.case / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_clock_ms=lambda: data['now'], orchestration_limits=OrchestrationLimits(8, 12),
            result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
            allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(store.close)
        return store

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_archived_memory_holds_recover_only_with_positive_inode_evidence_and_retry_the_removal(self):
        from codex_agents import memory_holds, codex_preparation, bridge
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            rows = history._connection.execute("SELECT kind,state,release_kind,evidence FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind").fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(tuple(r)[1:3] == ('released', 'service_stopped') for r in rows))
            pending = list(memory_holds.pending_removals(history, 'e' * 32))
            self.assertEqual(1, len(pending))
            identity, workspace = pending[0]
            with mock.patch.object(bridge, 'scratch_base', return_value=str(self.case.parent)), \
                    mock.patch.object(bridge, 'SCRATCH_CONTAINER', self.case.name):
                with mock.patch.object(codex_preparation, '_remove', return_value=False):
                    self.assertIs(False, codex_preparation.remove_recorded_workspace(workspace))
                self.assertEqual(pending, list(memory_holds.pending_removals(history, 'e' * 32)))
                self.assertTrue(Path(workspace).exists())
                self.assertIs(True, codex_preparation.remove_recorded_workspace(workspace))
            memory_holds.removal_proven(history, identity)
            self.assertEqual([], list(memory_holds.pending_removals(history, 'e' * 32)))
            self.assertFalse(Path(workspace).exists())
            before = history._connection.total_changes
            memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            self.assertEqual(before, history._connection.total_changes)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-9')
    def test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds(self):
        from codex_agents import memory_holds
        (self.case / 'launches' / (self.data['instance'] + '.lock')).unlink()
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            before = [tuple(r) for r in history._connection.execute("SELECT * FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind")]
            self.assertEqual(2, len(before))
            for _ in range(2):
                memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            after = [tuple(r) for r in history._connection.execute("SELECT * FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind")]
            self.assertEqual([row[:-1] for row in before], [row[:-1] for row in after])
            for prior, current in zip(before, after):
                facts, observed = json.loads(prior[-1]), json.loads(current[-1])
                self.assertTrue(all(observed.get(name) == value for name, value in facts.items() if name != 'reason'))
            self.assertIn('instance_unproven', after[0][-1])
            self.assertEqual(2, history._connection.execute("SELECT COUNT(*) FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') AND state='held'").fetchone()[0])


class RolloverTakeoverTests(unittest.TestCase):
    __why_not_scenario__ = 'SIGKILL inside/after an archived U1 transaction, then current authenticated lifecycle handler.'
    fixture_mode = 'rollover'
    setUpClass = classmethod(MemoryTakeoverTests.setUpClass.__func__)
    setUp = MemoryTakeoverTests.setUp
    open = MemoryTakeoverTests.open
    build_identity = ChainedOutboxTests.build_identity

    def replay(self, committed):
        from codex_agents.orchestration_api import (OrchestrationScope, OrchestrationRequest,
            encode_orchestration_request, decode_orchestration_response)
        from codex_agents.orchestration_service import OrchestrationService
        from codex_agents.history_contracts import HistoryError
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            self.assertEqual(2 if committed else 1, history._connection.execute(
                'SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
            scope = OrchestrationScope(**self.data['scope'])
            request = OrchestrationRequest(1, 'orchestration.rollover', 'handoff', scope, self.data['payload'])
            token = object()
            from types import SimpleNamespace
            from codex_agents.service_host import ProjectServiceHost
            from codex_agents.project_takeover import adopted_orchestration_scope
            host = ProjectServiceHost.__new__(ProjectServiceHost)
            host._scope = replace(scope, runtime_digest=B)
            host._frontend = mock.Mock()
            enrolled = SimpleNamespace(role='host', host_client='codex-host-v1')
            entry = SimpleNamespace(kind='project', enrolled=enrolled)
            lane = SimpleNamespace(adopted_orchestration_scope=lambda original, handle, client:
                                   adopted_orchestration_scope(history, original, handle, client))
            host._stored = lambda read: read(lane)
            def authorize(offered, context):
                host._frontend.context.return_value = entry if context is token else None
                return host._authorize_orchestration(offered, context)
            service = OrchestrationService(history.project_id, B, lambda: history,
                                           authorizer=authorize, close_history=False)
            self.addCleanup(service.close)
            encoded = encode_orchestration_request(request)
            first = decode_orchestration_response(service.handle(encoded, token), request)
            self.assertEqual(committed, first['known'])
            self.assertTrue(first['rolled_over'])
            again = decode_orchestration_response(service.handle(encoded, token), request)
            self.assertTrue(again['known'])
            self.assertEqual(first['current'], again['current'])
            self.assertEqual(2, history._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
            self.assertEqual(1, history._orchestration_owner()._open_count())
            before = history._connection.total_changes
            with self.assertRaises(HistoryError):
                decode_orchestration_response(service.handle(encoded, object()), request)
            self.assertEqual(before, history._connection.total_changes)
            with history._transaction():
                history._connection.execute('UPDATE takeover_record SET predecessor_digest=?', ('d' * 64,))
            before = history._connection.total_changes
            with self.assertRaises(HistoryError):
                decode_orchestration_response(service.handle(encoded, token), request)
            self.assertEqual(before, history._connection.total_changes)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_rollover_killed_inside_its_transaction_replays_both_ends_after_takeover(self):
        self.replay(False)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_rollover_killed_after_commit_returns_the_same_successor_after_takeover(self):
        self.replay(True)


class AccountLineageTests(unittest.TestCase):
    __why_not_scenario__ = 'Archived admitted custody and real current ledger functions; native enrollment is separate.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-account-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        here = Path(__file__).parent
        packed = subprocess.run(['git', '-C', str(here.parents[2]), 'archive', 'afb6f610',
                                 'packages/codex-agents', 'scripts'], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(cls.archive / 'scripts')
        made = subprocess.run([sys.executable, '-B', str(here / 'b4t_fixture.py'), str(cls.archive),
                               str(cls.source), 'account'], env=env, capture_output=True, text=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        cls.data = json.loads((cls.source / 'manifest.json').read_bytes())

    def setUp(self):
        from codex_agents import learning_store, collector_client
        from codex_agents.result_contracts import ResultLeaseBinding
        self.expected = ResultLeaseBinding(**self.data['expected'])
        self.project = self.expected.project_id
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'state', self.case / 'state')
        for name, value in (('_clock_ms', self.data['now']), ('_capacity_clock_reference', self.data['clock_reference_ns'])):
            patch = mock.patch.object(learning_store.LearningLedger, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.state = learning_store.LearningState(self.case / 'state')
        self.intent = dict(takeover_id='2' * 32, project_id=self.project, recorded_at_ms=self.data['now'],
            predecessor_collector=dict(version='0.8.66', digest=self.expected.runtime_digest),
            predecessor_service=dict(version='0.8.66', digest=self.expected.runtime_digest),
            successor=dict(version=collector_client.PACKAGE_VERSION, digest=collector_client.COLLECTOR_RUNTIME_DIGEST))
        self.ledger = learning_store.LearningLedger(self.state, allow_schema_upgrade=True, takeover=self.intent)
        self.addCleanup(self.ledger.close)

    def snapshot(self):
        return {name: [tuple(row) for row in self.ledger._connection.execute('SELECT * FROM ' + name)]
                for (name,) in self.ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def terminal(self):
        from codex_agents.terminal_delivery import decode_terminal_submission
        return self.ledger._accept_project_terminal(self.project, runtime_digest=self.ledger._fence.digest,
            caller_scope=self.data['caller_scope'], submission=decode_terminal_submission(self.data['submission']),
            storage_binding=self.expected, storage_evidence=bytes.fromhex(self.data['bound']),
            terminal_evidence=bytes.fromhex(self.data['terminal']))

    @requirements('S14b-6')
    def test_S14b_6_takeover_adoption_keeps_the_retention_indexes_of_the_consented_step(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.learning_store import LearningLedger
        from codex_agents import run_retention
        names = ', '.join("'%s'" % n for n in run_retention.RETENTION_INDEX_NAMES)
        query = f"SELECT count(*) FROM sqlite_master WHERE name IN ({names})"
        # setUp's consented open performed the 17 -> 18 step, whose transaction created them (decision 352).
        self.assertEqual(5, self.ledger._connection.execute(query).fetchone()[0])
        root = self.case / 'P'
        root.mkdir()
        project_p = self.ledger.register_project(root)
        self.ledger.close()
        OwnerOperations(self.state, Path('/cli'), root, lambda: self.intent).adopt_ledger(
            dict(self.intent, project_id=project_p))
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.assertEqual(5, self.ledger._connection.execute(query).fetchone()[0])

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_B4t_4_another_projects_child_keeps_running(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.account_service import AccountOwnerViews
        from codex_agents.account_takeover import takeover_report
        from codex_agents.learning_store import LearningLedger
        child = subprocess.Popen([sys.executable,'-I','-B','-c',
                                  'import sys; print("ready",flush=True); sys.stdin.read()'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.addCleanup(lambda: child.communicate(timeout=5))
        self.assertEqual('ready',child.stdout.readline().strip())
        root = self.case/'P'
        root.mkdir()
        project_p = self.ledger.register_project(root)
        before = [tuple(r) for r in self.ledger._connection.execute('SELECT * FROM active_run_capacity WHERE project_id=?',(self.project,))]
        self.assertTrue(before)
        self.ledger.close()
        ops = OwnerOperations(self.state,Path('/cli'),root,lambda:self.intent)
        ops.adopt_ledger(dict(self.intent,project_id=project_p))
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        after=[tuple(r) for r in self.ledger._connection.execute('SELECT * FROM active_run_capacity WHERE project_id=?',(self.project,))]
        self.assertEqual(before,after)
        self.assertIsNone(child.poll())
        report=takeover_report(AccountOwnerViews(self.ledger),project_p,{})
        self.assertEqual((1,1),(report['other_projects'],report['other_runs']))
        self.assertEqual('0.8.66',report['updates'][0]['version'])
        self.assertEqual(1,report['updates'][0]['children'])
        # Q's own update can now settle exactly that old claim, once.
        self.test_successor_lost_settlement_and_closure_find_the_original_claim_once()
        self.assertIsNotNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity '
            'WHERE project_id=?',(self.project,)).fetchone()[0])
        child.stdin.close()
        child.stdin=None
        child.communicate(timeout=5)

    @requirements('RS-B-B4t-5', 'RS-B-B4t-9')
    def test_successor_terminal_uses_original_scope_and_capacity_row_and_missing_lineage_writes_nothing(self):
        from codex_agents.account_claim_contracts import ClaimError
        before = self.snapshot()
        with mock.patch('codex_agents.runtime_lineage.release_versions', return_value={}):
            with self.assertRaises(ClaimError) as caught:
                self.terminal()
        self.assertEqual('claim_terminal_invalid', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())
        first = self.terminal()
        self.assertTrue(first['receipt_digest'])
        counted = self.snapshot()
        second = self.terminal()
        self.assertTrue(second['known'])
        self.assertEqual(first['receipt_digest'], second['receipt_digest'])
        self.assertEqual(counted, self.snapshot())
        self.assertEqual(1, len(counted['capacity_completions']))

    @requirements('RS-B-B4t-4', 'RS-B-B4t-5', 'RS-B-B4t-9')
    def test_successor_lost_settlement_and_closure_find_the_original_claim_once(self):
        from codex_agents.account_lost_run import settle_lost_run, lost_run_closure
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.claim_storage import verify_pending_storage_evidence
        from codex_agents.history_contracts import AttemptBinding
        from codex_agents.account_claim_contracts import ClaimError
        lease = verify_pending_storage_evidence(self.state.digest_key(), self.expected, bytes.fromhex(self.data['pending']))
        binding = AttemptBinding(**self.data['binding'])
        settle = SettleLostInput(self.expected.run_handle, self.data['identity_digest'],
                                self.expected.claim_request_id, binding.claim_id, lease.lease_digest)
        arguments = dict(runtime_digest=self.ledger._fence.digest, caller_scope=self.data['caller_scope'],
                         settle=settle, storage_binding=self.expected, storage_evidence=bytes.fromhex(self.data['pending']))
        before = self.snapshot()
        with mock.patch('codex_agents.runtime_lineage.release_versions', return_value={}):
            for function, extras in ((settle_lost_run, {}), (lost_run_closure, {'request_digest': 'e' * 64})):
                with self.assertRaises(ClaimError) as caught:
                    function(self.ledger, self.project, **arguments, **extras)
                self.assertEqual('claim_cleanup_invalid', caught.exception.reason_code)
                self.assertEqual(before, self.snapshot())
        settled = settle_lost_run(self.ledger, self.project, **arguments)
        self.assertEqual('settled', settled['state'])
        counted = self.snapshot()
        self.assertTrue(settle_lost_run(self.ledger, self.project, **arguments)['known'])
        closure = lost_run_closure(self.ledger, self.project, **arguments, request_digest='e' * 64)
        self.assertEqual('lost', closure['release_kind'])
        self.assertEqual(counted, self.snapshot())

    @requirements('RS-B-B4t-9')
    def test_earlier_runtime_is_passed_only_at_the_four_approved_call_sites(self):
        from tests.support import ORCHESTRATION_ROOT
        sites = set()
        for path in (ORCHESTRATION_ROOT / 'codex_agents').glob('*.py'):
            source = path.read_text()
            if 'earlier_runtime' not in source:  # a keyword of that name must appear in the source to be passed
                continue
            tree = ast.parse(source)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and any(arg.arg == 'earlier_runtime' for arg in node.keywords):
                        sites.add((path.name, function.name))
                        self.assertTrue(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == 'successor_custody' for call in ast.walk(function)))
        self.assertEqual({('account_lost_run.py', '_known_claim'), ('account_cleanup.py', 'release_capacity'),
                          ('learning_store.py', '_settle_unlaunched'),
                          ('learning_store.py', '_accept_project_terminal')}, sites)


class FloorTests(unittest.TestCase):
    __why_not_scenario__ = 'Actual owner acquisitions at a lower build version must change zero database bytes.'

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_current_ledger_open_keeps_the_predecessors_capacity_coverage_refusal(self):
        from codex_agents import learning_store
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            project_root = root / 'project'
            project_root.mkdir()
            state = learning_store.LearningState(root / 'state')
            owner = learning_store.LearningLedger(state)
            project = owner.register_project(project_root)
            with owner._account_transaction():
                owner._connection.execute("INSERT INTO launch_claims VALUES(?,?,?,'claimed',?,?)",
                                           (project, 'a' * 64, 'lost-capacity-row', 1, 'brg_' + 'b' * 32))
            owner.close()
            before = state.database.read_bytes()
            with self.assertRaises(learning_store.LedgerError):
                with closing(learning_store.LearningLedger(state)):
                    pass
            self.assertEqual(before, state.database.read_bytes())

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_a_B4t_build_below_the_floor_cannot_advance_either_epoch(self):
        from codex_agents import collector_client, project_history, learning_store
        from codex_agents.history_contracts import HistoryError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            directory = root / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '9' * 32
            history = project_history.ProjectHistory(directory, project)
            self.assertEqual(23, history._connection.execute('PRAGMA user_version').fetchone()[0])
            history.close()
            state = learning_store.LearningState(root / 'state')
            ledger = learning_store.LearningLedger(state)
            self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])
            ledger.close()
            before = {path: path.read_bytes() for path in (directory / 'history.sqlite3', state.database)}
            with mock.patch.object(collector_client, 'PACKAGE_VERSION', '0.8.66'):
                with self.assertRaises(HistoryError):
                    project_history.ProjectHistory(directory, project)
                with self.assertRaises(learning_store.LedgerError):
                    learning_store.LearningLedger(state)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_a_store_written_by_this_build_refuses_0868_0869_0870_0871_0872_and_0873(self):
        # IP-G (decision 370, floor moved by the 0.8.74 bump): the takeover floor this build writes is its own PACKAGE_VERSION (0.8.74), so the
        # released 0.8.68, 0.8.69, 0.8.70, 0.8.71, 0.8.72 and 0.8.73 builds refuse a store that already carries the unverified marker.
        from codex_agents import collector_client, project_history
        from codex_agents.history_contracts import HistoryError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            directory = Path(folder).resolve() / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '8' * 32
            project_history.ProjectHistory(directory, project).close()
            history = project_history.ProjectHistory(directory, project)
            self.assertEqual([(1, '0.8.74')], [tuple(r) for r in history._connection.execute('SELECT * FROM runtime_floor')])
            history.close()
            before = (directory / 'history.sqlite3').read_bytes()
            for older in ('0.8.68', '0.8.69', '0.8.70', '0.8.71', '0.8.72', '0.8.73'):
                with mock.patch.object(collector_client, 'PACKAGE_VERSION', older):
                    with self.assertRaises(HistoryError):
                        project_history.ProjectHistory(directory, project)
            self.assertEqual(before, (directory / 'history.sqlite3').read_bytes())

    @requirements('RS-B-B4t-3')
    def test_archived_0866_and_0832_cannot_reopen_either_published_store(self):
        from codex_agents import project_history, learning_store
        with tempfile.TemporaryDirectory(prefix='b4t-old-', dir='/tmp') as folder:
            root = Path(folder).resolve()
            directory = root / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '8' * 32
            project_history.ProjectHistory(directory, project).close()
            state = learning_store.LearningState(root / 'state')
            learning_store.LearningLedger(state).close()
            top = Path(__file__).parents[3]
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
            for revision in ('afb6f610', 'v0.3.4'):
                archive = root / revision
                archive.mkdir()
                packed = subprocess.run(['git', '-C', str(top), 'archive', revision,
                                         'packages/codex-agents'], capture_output=True, check=True)
                subprocess.run(['tar', '-x', '-C', str(archive)], input=packed.stdout, check=True)
                runtime = archive / 'packages/codex-agents/native-package/.codex/orchestration'
                for kind in ('ledger', 'project'):
                    with self.subTest(revision=revision, kind=kind):
                        database = state.database if kind == 'ledger' else directory / 'history.sqlite3'
                        before = database.read_bytes()
                        code = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]=='ledger':
 from codex_agents.learning_store import LearningState,LearningLedger
 owner=LearningLedger(LearningState(Path(sys.argv[3])))
else:
 from codex_agents.project_history import ProjectHistory
 owner=ProjectHistory(Path(sys.argv[3]),sys.argv[4])
owner.close()
'''
                        done = subprocess.run([sys.executable, '-B', '-c', code, str(runtime), kind,
                            str(state.root if kind == 'ledger' else directory), project],
                            env=env, capture_output=True, text=True, timeout=15)
                        self.assertNotEqual(0, done.returncode)
                        self.assertRegex(done.stderr, 'newer|incompatible')
                        self.assertEqual(before, database.read_bytes())
