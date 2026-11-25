"""RS-B-4 real driver, preparation pool, bounded writer lane and journal; no sockets."""
from contextlib import closing
import json
import sqlite3
import threading
import time
from pathlib import Path
import unittest
from unittest import mock

from tests import test_bridge as fixtures
from tests.test_project_runtime import limits
from tests.test_b6_unattended_close import KEY, CLIENT
from codex_agents import project_driver
from codex_agents.history_contracts import HistoryError
from codex_agents.launch_journal import instance_stopped
from codex_agents.open_scope_contracts import open_inputs_digest
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope
from codex_agents.project_history import ProjectHistory
from codex_agents.project_runtime import ProjectRuntime
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding

requirements = __import__('test_framework').requirements
WHY = 'Actual owner, writer, preparation and journal boundaries; account replies are independent of the memory contract.'


class MemoryLaneTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.scope = OrchestrationScope(self.fixture.project_id, 'primary', project_driver.COLLECTOR_RUNTIME_DIGEST,
                                        'supervisor', 1)
        self.directory = self.fixture.state.root / 'projects' / self.scope.project_id / 'history'
        self.directory.mkdir(mode=0o700, parents=True)
        with closing(self.factory()) as store:
            self.opened = store._orchestration_owner().apply(OrchestrationRequest(1, 'orchestration.open', 'open',
                self.scope, {}), client_version=CLIENT, before_commit=lambda: None)['current']['snapshot']
        self.runtime = ProjectRuntime(self.scope, self.factory, limits())
        self.now = time.monotonic()
        self.driver = project_driver.RunDriver(self.fixture.state, self.fixture.project, self.fixture.bundle,
            self.scope, self.runtime, clock=lambda: self.now)
        self.addCleanup(self.stop)
        self.until(lambda: self.driver._memory_start.state == 'done')
        self.route = self.fixture.bridged_route('codex')
        self.assigned = self.fixture.assignment_for(self.route)
        self.key = ('hrn_' + 'b' * 48, 1)
        self.digest = open_inputs_digest(self.assigned, self.fixture.task(), self.route, fixtures.PARENT_GRANT)
        self.payload = {'purpose': 'register', 'orchestration_handle': self.opened['orchestration_handle'],
            'run_id': 'run:' + 'a'*64, 'host_client': CLIENT, 'assignment': self.assigned,
            'task': self.fixture.task(), 'route': self.route, 'parent_grant': fixtures.PARENT_GRANT}

    def factory(self):
        return ProjectHistory(self.directory, self.scope.project_id, result_key=KEY,
            orchestration_limits=OrchestrationLimits(8, 12),
            result_capacity=ResultCapacity(8, 4*MAX_RESULT_BODY_BYTES, 60_000, 120_000))

    def until(self, predicate, *, drive=True):
        end = time.monotonic()+10
        while not predicate():
            if not self.runtime._closed:
                self.runtime.run_once()
            if drive:
                self.driver.step()
            self.assertLess(time.monotonic(), end, 'memory lifecycle progress expired')
            time.sleep(.002)

    def rows(self):
        with closing(sqlite3.connect(self.directory / 'history.sqlite3')) as db:
            db.row_factory = sqlite3.Row
            return {r['kind']: dict(r) for r in db.execute('SELECT * FROM project_holds WHERE identity=?',
                (self.key[0]+':1',))}

    def register(self):
        self.driver._register(self.key, self.digest, self.payload, CLIENT)
        self.until(lambda: self.driver._runs[self.key].reporting is not None and
                   self.driver._runs[self.key].reporting.state == 'done')
        return self.driver._runs[self.key]

    def stop(self):
        if not self.driver._closed:
            self.driver.close()
        self.runtime.request_close()
        self.until(self.runtime.threads_stopped, drive=False)
        if self.driver._journal._lock_fd is not None:
            self.driver.settle_on_exit()

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M1_writer_commit_waits_before_submit_and_retire_receipt_follows_actual_retire(self):
        run = self.register()
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        from codex_agents import memory_holds
        original = memory_holds.take_preparation
        def blocked(*args):
            entered.set()
            self.assertTrue(resume.wait(8))
            return original(*args)
        with mock.patch.object(memory_holds, 'take_preparation', blocked), \
                mock.patch.object(self.driver._preparation, 'submit', wraps=self.driver._preparation.submit) as submit:
            self.driver._prepare(self.key, self.digest, {'purpose':'prepare', 'expected_export_commit':None}, CLIENT)
            self.assertTrue(entered.wait(5))
            for _ in range(10):
                self.driver._memory.step()
            submit.assert_not_called()
            self.assertEqual(0, self.driver._preparation.capacity()['jobs'])
            resume.set()
            self.until(lambda: run.prepared is not None)
            self.assertEqual('held', self.rows()['preparation']['state'])
        self.driver._give_back(run)
        self.assertEqual('held', self.rows()['preparation']['state'])
        self.until(lambda: not self.driver._giving_back)
        self.until(lambda: self.rows()['preparation']['state']=='released')
        self.assertEqual(0, self.driver._preparation.capacity()['jobs'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_L4_L6_ten_refused_fences_handoff_detaches_real_lane_and_same_service_lists(self):
        run = self.register()
        self.driver._prepare(self.key, self.digest, {'purpose':'prepare', 'expected_export_commit':None}, CLIENT)
        # Drive only preparation here: no provider observation or claim is needed for an armed lease.
        self.until(lambda: run.prepared is not None)
        self.until(lambda: self.driver._preparation.snapshot(run.prepared)['state']=='ready', drive=False)
        run.prepared_inputs = self.driver._preparation.prepared(run.prepared)
        run.lease = ResultLeaseBinding(**self.scope.as_data(), orchestration_handle=self.opened['orchestration_handle'],
            run_handle=self.key[0], attempt=1, run_id=run.run_id, claim_request_id='claim', claim_request_digest='d'*64)
        lane = self.runtime.persistence
        run.persistence = lane.prepare_lease(run.lease, run.prepared_inputs.worker)
        self.until(lambda: lane.snapshot(run.persistence)['state']=='reserved', drive=False)
        lane.arm(run.persistence)
        self.until(lambda: lane.snapshot(run.persistence)['state']=='claim_pending', drive=False)
        run.state, run.claim_sent = 'failed', True
        for _ in range(project_driver.MAX_FENCE_ATTEMPTS):
            self.driver._fence_later(run)
        self.assertIsNone(run.fence_at)
        self.assertEqual(1, len(lane._jobs))  # Handoff queued; detach has not happened.
        self.until(lambda: run.persistence is None)
        self.assertEqual(0, len(lane._jobs))
        self.assertEqual(0, len(self.runtime.pool._jobs))
        self.until(lambda: self.rows()['driver_entry']['state']=='released')
        rows = self.rows()
        self.assertEqual(('released','never_launch_local'), (rows['preparation']['state'],rows['preparation']['release_kind']))
        self.assertEqual(('held','armed'), (rows['lease']['state'],rows['lease']['lease_case']))
        self.assertEqual('sweep',json.loads(rows['lease']['evidence'])['instance'])
        request = lane.earlier_leases()
        self.until(lambda: request.state!='queued',drive=False)
        self.assertEqual([run.lease], [entry[0] for entry in request.result])
        self.assertEqual('handed_over',rows['driver_entry']['release_kind'])

    def armed(self):
        run = self.register()
        self.driver._prepare(self.key, self.digest, {'purpose':'prepare', 'expected_export_commit':None}, CLIENT)
        self.until(lambda: run.prepared is not None)
        self.until(lambda: self.driver._preparation.snapshot(run.prepared)['state']=='ready', drive=False)
        run.prepared_inputs = self.driver._preparation.prepared(run.prepared)
        run.lease = ResultLeaseBinding(**self.scope.as_data(), orchestration_handle=self.opened['orchestration_handle'],
            run_handle=self.key[0], attempt=1, run_id=run.run_id, claim_request_id='claim', claim_request_digest='d'*64)
        lane = self.runtime.persistence
        run.persistence = lane.prepare_lease(run.lease, run.prepared_inputs.worker)
        self.until(lambda: lane.snapshot(run.persistence)['state']=='reserved', drive=False)
        lane.arm(run.persistence)
        self.until(lambda: lane.snapshot(run.persistence)['state']=='claim_pending', drive=False)
        return run, lane

    def closure(self, lease):
        from codex_agents.claim_denial import denial_closure, DENIAL_RECORD_TYPE
        denial = {'state':'denied', 'receipt_digest':'e'*64, 'run_id':lease.binding.run_id,
            'bridge_run_id':None, 'deadline_at_ms':None, 'reason_code':'claim_not_open',
            'recorded_at_ms':lease.created_at_ms, 'known':True}
        return denial_closure(KEY, lease.binding, {'lease_digest':lease.lease_digest,
            'assignment_digest':lease.assignment_digest, 'denial':denial,
            'record_id':DENIAL_RECORD_TYPE+':'+'f'*64, 'observed_at_ms':lease.created_at_ms}, known=False)

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M6_denied_closed_run_retires_without_another_client(self):
        from codex_agents.learning_contracts import canonical_json
        run, lane = self.armed()
        lane.settle_denial(run.persistence,canonical_json(self.closure(lane.lease(run.persistence))))
        self.until(lambda: lane.snapshot(run.persistence)['state']=='denied',drive=False)
        run.claim_sent, run.state = True, 'settling'
        self.until(lambda: self.key not in self.driver._runs)
        self.until(lambda: self.rows()['driver_entry']['state']=='released')
        self.assertEqual('retired',self.rows()['preparation']['release_kind'])
        self.assertEqual('run_closed',self.driver._unheld_reply(self.key[0],CLIENT)['reason_code'])
        self.assertEqual(0,self.driver._preparation.capacity()['jobs'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_L6_same_service_fences_handed_over_lease_once(self):
        run, lane = self.armed()
        closure = self.closure(lane.lease(run.persistence))
        run.claim_sent, run.state = True, 'failed'
        answer = {'receipt':closure['denial'], 'closure':closure}
        with mock.patch.object(self.driver,'_fence_earlier_call',return_value=answer) as fence:
            for _ in range(project_driver.MAX_FENCE_ATTEMPTS):
                self.driver._fence_later(run)
            self.until(lambda: run.persistence is None)
            self.until(lambda: self.key[0] in self.driver._fenced)
            self.assertEqual(1,fence.call_count)
        self.assertEqual(('released','fenced'),(self.rows()['lease']['state'],self.rows()['lease']['release_kind']))
        self.until(lambda: self.rows()['driver_entry']['state']=='released')
        self.assertEqual(0,len(self.runtime.pool._jobs))
        self.assertEqual(0,len(lane._jobs))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M7_faulted_writer_sends_one_memory_withdrawal(self):
        lane = self.runtime.persistence
        def fault(request):
            raise RuntimeError('fixture writer fault')
        lane._earlier = fault
        lane.memory_hold('recover',self.driver._journal.directory,self.driver._journal.instance)
        self.until(lambda: not lane._writer.is_alive(),drive=False)
        self.assertTrue(lane.faulted())
        with mock.patch.object(self.driver,'_withdraw_call',return_value={'state':'withdrawn'}) as withdraw:
            self.driver._register(self.key,self.digest,self.payload,CLIENT)
            self.until(lambda: self.key not in self.driver._runs)
            for _ in range(5):
                self.driver.step()
            self.assertEqual(1,withdraw.call_count)
        self.assertNotIn('driver_entry',self.rows())

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M5_graceful_stop_retires_rows_before_unlock(self):
        self.register()
        original = self.driver._journal.release
        def release():
            self.assertFalse(instance_stopped(self.driver._journal.directory,self.driver._journal.instance))
            self.assertEqual('retired',self.rows()['driver_entry']['release_kind'])
            self.assertEqual({},self.driver._runs)
            original()
        with mock.patch.object(self.driver._journal,'release',release):
            self.stop()
        self.assertEqual('released',self.rows()['driver_entry']['state'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M7_refused_registration_drops_entry_and_keeps_withdrawal_outbox(self):
        # Refuse in the actual registration transaction after the independent binding duty commits.
        from codex_agents import memory_holds
        def refused(history,*args):
            raise HistoryError('history_orchestration_state')
        with mock.patch.object(memory_holds,'registration_hold',refused), \
                mock.patch.object(self.driver,'_withdraw_call',side_effect=RuntimeError('offline')):
            self.driver._register(self.key,self.digest,self.payload,CLIENT)
            self.until(lambda: self.key not in self.driver._runs)
        self.assertNotIn('driver_entry',self.rows())
        self.assertEqual('releasing',self.rows()['open_binding']['state'])
        self.assertEqual('run_closed',self.driver._unheld_reply(self.key[0],CLIENT)['reason_code'])


if __name__ == '__main__':
    unittest.main()
