"""Socket-free fault-sensitivity checks for D4's publication proofs.

These exercise the actual D4 test methods with a narrowly injected regression. A proof
that accepts the regression fails here, including the original round-one false passes.
"""
import contextlib
import inspect
import json
import queue
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_publication_replay as store_tests
from tests import test_publication_replay_driver as driver_tests
from codex_agents import project_history, result_persistence
from codex_agents.learning_contracts import keyed_digest

WHY = 'D4 coverage regression witnesses run the real proof methods against isolated mutants without services or sockets.'


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class PublicationProofSensitivityTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def stopped_scenario(self, fault):
        """Run the real stopped-instance observer with socket-free service/dispatch substitutes."""
        case = driver_tests.StoppedInstanceOrderTests(methodName='runTest')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        case.tools = Path(directory.name)
        case.open_run = lambda: (None, None, 'orchestration', 'target')
        case.frames = lambda *args: None
        case.store_rows = lambda: dict(terminals=1, usage=1, material=0, states=['reserved'])
        process = mock.Mock()
        process.poll.return_value = 0
        case.separate_dispatch = lambda *args: process
        case.stop_without_settling = lambda: None
        case.counted = lambda: 0
        case.remaining = lambda: 2
        case.wait_for = lambda condition: self.assertTrue(condition())
        held = types.SimpleNamespace(binding=types.SimpleNamespace(claim_id='claim'), failed=threading.Event())
        held.failed.set()
        sweeps = []

        def sweep(driver):
            sweeps.append(1)
            if fault == 'empty_first' and len(sweeps) == 1:
                return
            case.store_rows = lambda: dict(terminals=1, usage=1, material=0, states=['committed'])
            values = [types.SimpleNamespace(run_handle='target', bridge_run_id='claim')]
            if fault == 'wrong_run':
                values.insert(0, types.SimpleNamespace(run_handle='other', bridge_run_id='claim'))
            if fault == 'wrong_claim':
                values.insert(0, types.SimpleNamespace(run_handle='target', bridge_run_id='other'))
            for value in values:
                try:
                    driver_tests.AccountClient.settle_lost_run(None, value)
                except AssertionError:
                    pass  # the real lost sweep also catches callback failures

        def expire(lane, request):
            request.state = 'done'

        def start_host():
            driver_tests.project_driver.RunDriver._sweep_lost(None)
            driver_tests.project_driver.RunDriver._expire_results(None)
            if fault == 'empty_first':
                driver_tests.project_driver.RunDriver._sweep_lost(None)

        case.start_host = start_host
        with mock.patch.object(driver_tests, 'HeldLiveCommit', lambda: contextlib.nullcontext(held)), \
                mock.patch.object(driver_tests.AccountClient, 'settle_lost_run', return_value='reply'), \
                mock.patch.object(driver_tests.project_driver.RunDriver, '_settle_lost', sweep), \
                mock.patch.object(driver_tests.project_driver.RunDriver, '_sweep_lost',
                                  lambda driver: driver_tests.project_driver.RunDriver._settle_lost(driver)), \
                mock.patch.object(driver_tests.result_persistence.ResultPersistenceLane, '_earlier', expire), \
                mock.patch.object(driver_tests.project_driver.RunDriver, '_expire_results',
                                  lambda driver: driver_tests.result_persistence.ResultPersistenceLane._earlier(
                                      None, types.SimpleNamespace(kind='expire'))):
            events, _, run = case.stranded_then_successor('sweep_first')
        case.check(events, run)
        return events

    def test_settlement_observer_retains_wrong_run_and_claim_calls(self):
        for fault in ('wrong_run', 'wrong_claim'):
            with self.subTest(fault=fault), self.assertRaises(AssertionError):
                self.stopped_scenario(fault)
        self.stopped_scenario(None)

    def test_sweep_first_observer_rejects_settlement_only_after_expiry(self):
        with self.assertRaises(AssertionError):
            self.stopped_scenario('empty_first')
        self.stopped_scenario(None)

    def test_driver_teardown_still_rejects_each_inherited_named_worker_leak(self):
        case = driver_tests.StoppedInstanceOrderTests(methodName='runTest')
        case.stop, case.threads, case.failures = threading.Event(), [], []
        case._host_stop = threading.Event()
        for name in ('account-observation', 'signature-fixed-worker', 'codex-agents-collector', 'account-ingress'):
            worker = types.SimpleNamespace(name=name, is_alive=lambda: True)
            with self.subTest(worker=name), mock.patch.object(driver_tests.threading, 'enumerate', return_value=[worker]), \
                    self.assertRaises(AssertionError):
                case.stop_services()
        with mock.patch.object(driver_tests.threading, 'enumerate', return_value=[]):
            case.stop_services()

    def test_registered_cleanup_obeys_the_shared_deadline_even_after_teardown_fails(self):
        case = driver_tests.StoppedInstanceOrderTests(methodName='runTest')
        case.stop, case.failures = threading.Event(), []
        case._host_stop = threading.Event()
        clock, joins = [59.5], []

        def join(timeout):
            joins.append(timeout)
            clock[0] += timeout  # emulate a thread that consumes the whole join timeout

        case.threads = [types.SimpleNamespace(name='stuck-host', join=join, is_alive=lambda: True)]
        with mock.patch.object(driver_tests, 'MODULE_END', 50), \
                mock.patch.object(driver_tests.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(AssertionError):
                case.stop_services()
            case._end_services(case.stop)  # registered by the inherited harness before teardown
        self.assertEqual([0.5, 0], joins)
        self.assertEqual(60, clock[0])
        self.assertTrue(case.stop.is_set())

    def test_lifetime_registry_keeps_both_D3_entries_and_D4_replay(self):
        registry = json.loads(Path(__file__).with_name('lifetime_caps.json').read_text())['sites']
        expected = {
            'project_result_contexts.py::context_rows': {
                'checks': 1,
                'kind': 'count',
                'ends': 'a page read inside repairs(): at most limit+1 context rows of one state after the cursor, one index range; rows leave as their retirement finishes (ended/expired); BOUNDED',
                'test': 'test_R2_a_truncated_report_says_so_and_its_continuation_reaches_every_remaining_entry'},
            'project_result_contexts.py::reservation_rows': {
                'checks': 1,
                'kind': 'count',
                'ends': 'a page read inside repairs(): at most limit+1 retiring reservations without a context row after the cursor; rows leave as their retirement finishes; BOUNDED',
                'test': 'test_reaudited_reservation_only_repairs_page_past_their_own_row_limit'},
        }
        for site, entry in expected.items():
            with self.subTest(site=site):
                self.assertEqual(entry, registry.get(site))
        self.assertIn('project_history.py::replay_candidates', registry)

    def test_P1_proof_detects_retention_recomputed_on_retry(self):
        case = self.fixture(store_tests.KeptPacketRetryTests)
        original = project_history.ProjectHistory.commit_result

        def recompute(history, content, binding, *args, **kwargs):
            row, value = history._result_row(binding)
            if row['prepared_digest'] is not None and value.state == 'reserved':
                history._connection.execute('UPDATE result_reservations SET retained_until_ms=? WHERE claim_id=?',
                                            (history._result_now() + history._result_capacity.retention_ms, binding.claim_id))
            return original(history, content, binding, *args, **kwargs)

        with mock.patch.object(project_history.ProjectHistory, 'commit_result', recompute), self.assertRaises(AssertionError):
            case.test_P1_each_transient_failure_is_retried_from_the_kept_packet_and_commits_once()

    def test_R6_proof_detects_acceptance_of_removed_supplied_usage(self):
        case = self.fixture(store_tests.ReplayEligibilityTests)
        original = project_history.ProjectHistory._verified_publication

        def accept_removed(history, binding, row, now, **options):
            import json
            stored = history._connection.execute('SELECT material FROM project_result_publications WHERE claim_id=?',
                                                (binding.claim_id,)).fetchone()
            if stored is not None and 'client_usage_record' not in json.loads(bytes(stored[0])):
                history._connection.execute('UPDATE result_reservations SET prepared_digest=? WHERE claim_id=?',
                                            (keyed_digest(store_tests.KEY, bytes(stored[0])), binding.claim_id))
                row = history._result_row(binding)[0]
            return original(history, binding, row, now, **options)

        with mock.patch.object(project_history.ProjectHistory, '_verified_publication', accept_removed), self.assertRaises(AssertionError):
            case.test_R6_an_altered_body_is_refused_and_an_absent_usage_record_is_a_replay_without_one()

    def test_X2_proof_detects_direct_replay_ignoring_the_release_gate(self):
        case = self.fixture(store_tests.ArbitrationTests)

        def verify_only(history, binding):
            row, _ = history._result_row(binding)
            return history._verified_publication(binding, row, history._expiry_now())

        with mock.patch.object(project_history.ProjectHistory, 'replay_publication', verify_only), self.assertRaises(AssertionError):
            case.test_X2_no_material_or_a_body_that_does_not_verify_is_unrecoverable_and_closes_the_gate()

    def test_L1_L2_proof_rejects_a_lifecycle_without_any_live_retry(self):
        case = driver_tests.LiveRetryLifecycleTests(methodName='runTest')
        case.live_packets = []
        report = {'bridge_report': {'outcome': 'result'}}
        case.opened = lambda _: ((), 'route', None, 'run', None, None)
        case.child_answers = lambda: None
        case.attempt = lambda *a: (0, report)
        case.dispatch_result = lambda *a: (0, report)
        case.executions = lambda: [1]
        case.rows = lambda _: (1, 1, 1)
        case.record = lambda *a: None
        case.wait_for = lambda *a, **kw: None
        case.store_rows = lambda: {'material': 0}
        case.stopped_runs = [('driver', 'retiring', 'released')]
        # The baseline helper accepted an unused expect_calls parameter.
        args = (contextlib.nullcontext, 2) if 'expect_calls' in inspect.signature(case.finish_through).parameters else (contextlib.nullcontext,)
        with self.assertRaises(AssertionError):
            case.finish_through(*args)

    def test_L1_L2_observer_sees_the_callback_cached_at_writer_startup(self):
        case = driver_tests.LiveRetryLifecycleTests(methodName='runTest')
        selected = object()
        pending = types.SimpleNamespace(selected=selected, observed_at_ms=123, state='queued', delivery=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock = threading.Lock()
        lane._ready = threading.Event()
        lane._bootstrap_history = False
        lane._requests = lane._store = None
        lane._closed, lane._fault = True, False
        lane._unanswered = {}
        lane._jobs = {'job': pending}
        lane._incoming = queue.Queue()
        for command in (('publish', 'job'), ('publish', 'job'), None):
            lane._incoming.put(command)

        def start_writer(harness):
            # The real writer caches its callbacks inside the harness's setUp,
            # before finish_through can install any of its dispatch-time patches.
            lane._run()

        def publish(lane, pending):
            pending.state = 'committed'

        with mock.patch.object(result_persistence.ResultPersistenceLane, '_publish', publish), \
                mock.patch.object(driver_tests.DriverHarness, 'setUp', start_writer):
            try:
                case.setUp()
            finally:
                case.doCleanups()  # unwind the observer before the outer publish stub
        self.assertFalse(lane._fault)
        self.assertEqual(2, len(getattr(case, 'live_packets', [])))
        for packet, observed in case.live_packets:
            self.assertIs(selected, packet)
            self.assertEqual(123, observed)

    def test_L3_proof_detects_equal_delivery_replacement_and_evidence_loss(self):
        case = driver_tests.LiveRetryLifecycleTests(methodName='runTest')
        original = result_persistence.ResultPersistenceLane._install_delivery

        def replace_equal(lane, pending, delivery, evidence):
            if pending.delivery is not None and pending.delivery == delivery:
                pending.delivery, pending.selected, pending.terminal_evidence = delivery, None, evidence
                return
            return original(lane, pending, delivery, evidence)

        with mock.patch.object(result_persistence.ResultPersistenceLane, '_install_delivery', replace_equal), self.assertRaises(AssertionError):
            case.test_L3_adopting_an_unequal_delivery_is_refused_and_an_equal_one_is_left_alone()
