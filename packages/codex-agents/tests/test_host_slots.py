"""O0c M1, M3, M4, M7 (ANALYSIS-0.4.0-lifetime-caps.md Part 2): every way a run ends gives back every
host slot it took (the B0-6 invariant for the project host's in-memory slots).

Integration stage: the in-process collector, account and project services (DriverHarness). The project
host has three slots of each kind (project_service.MAX_ACTIVE_ATTEMPTS): preparations, workers, result
entries and monitor attempts. Each scenario reaches its failure exit through one injected fault, then
only the product's own steps run. A service that stops is started again only by the production path
(the next command's `ensure_project_host`), here in-process so the trusted fake clients still apply.
"""
import functools
import json
import subprocess
import time
from unittest import mock

from tests import service_harness
from tests.test_bridge import cli, null_filled, result
from tests.test_project_driver import DriverHarness

from codex_agents import codex_preparation, owned_execution, process_tree, project_service, result_persistence
from codex_agents.history_contracts import HistoryError
from codex_agents.project_host_client import project_host_serving


class HostSlotHarness(DriverHarness):
    def setUp(self):
        self.hosts, self.replacements = [], []
        original = project_service.ProjectServiceHost
        hosts = self.hosts

        class Recorded(original):
            def __init__(host, *arguments, **options):
                super().__init__(*arguments, **options)
                hosts.append(host)

        patcher = mock.patch.object(project_service, 'ProjectServiceHost', Recorded)
        patcher.start()
        self.addCleanup(patcher.stop)

        def ensure(state_root, orchestration_root, project_root, **options):
            # The production path: a command finds no service and starts one.
            if not project_host_serving(state_root, project_root):
                self.replacements.append(time.monotonic())
                self.start_project_host(self.project)
            return True

        patcher = mock.patch.object(cli, 'ensure_project_host', ensure)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def held(self, host=None):
        """What the current host holds: its runs and each kind of slot."""
        host = host or self.hosts[-1]
        driver, runtime = host._driver, host._runtime
        return {'runs': len(driver._runs), 'preparations': len(driver._preparation._jobs),
                'workers': len(runtime.pool._jobs), 'results': len(runtime.persistence._jobs),
                'executions': len(runtime.pool._registry._pending), 'monitor': len(runtime.monitor._attempts)}

    def account_slots(self):
        return self.collector_ledger._connection.execute(
            'SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0]

    def succeed(self):
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))

    def record(self, context, route_path, run_handle, dispatched):
        observation = {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]}
        terminal = dispatched['bridge_report'].get('terminal_state')
        if terminal is not None:
            observation['terminal_state'] = terminal
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('result.json', dispatched['result']),
                 '--observation', self.write('observation.json', observation))

    def dispatch(self, run):
        context, route_path, _, run_handle = run
        return self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)

    def released(self, count):
        """`count` runs have reached `released` (their slots retired) on the current host."""
        return len([s for s in self.stopped_runs if s == ('driver', 'retiring', 'released')]) >= count

    def wait_released(self, count):
        """Wait for `count` runs to reach `released`; on timeout, say where the runs stopped."""
        try:
            self.wait_for(lambda: self.released(count), seconds=30)
        except AssertionError:
            self.fail(f'{count} runs were not all released; the runs stopped at: '
                      f'{[s for s in self.stopped_runs if s[0] == "driver"][-8:]}; held: {self.held()}')

    def admitted_then_failed(self, run):
        """The claim is admitted, then the run fails before launch (the store's claim bind is refused)."""
        with mock.patch.object(result_persistence.ResultPersistenceLane, 'bind_claim',
                               side_effect=HistoryError('history_service_unavailable')):
            code, report = self.dispatch(run)
        self.assertEqual((2, 'run_failed'), (code, report.get('reason_code')), report)


class HostSlotLifecycleTests(HostSlotHarness):
    def test_M1_M3_M4_four_runs_end_on_their_failure_exits_then_a_fifth_dispatches_without_a_restart(self):
        """B0-6 invariant, host slots: with 3 slots, four runs end on these paths (cancel before spawn,
        spawn failure, admitted then failed, orphan preparation), then a fifth dispatches with no restart.
        On 0.8.38 the first three hold all three slots for the host's lifetime and the fourth is refused
        `preparation_full`."""
        self.succeed()
        baseline = self.held()
        self.assertEqual({'runs': 0, 'preparations': 0, 'workers': 0, 'results': 0, 'executions': 0,
                          'monitor': 0}, baseline)

        # 1. Cancelled after its claim, before its child is spawned.
        run = self.open_run()
        original_enter = owned_execution.OwnedExecutionRegistry.enter

        def cancelled_first(registry, ticket):
            with registry._lock:
                registry._request_stop(registry._entry(ticket), 'cancel_requested')
            return original_enter(registry, ticket)

        with mock.patch.object(owned_execution.OwnedExecutionRegistry, 'enter', cancelled_first):
            code, dispatched = self.dispatch(run)
        self.assertEqual(0, code, dispatched)
        self.assertEqual('cancelled', dispatched['bridge_report']['outcome'], dispatched)
        self.record(run[0], run[1], run[3], dispatched)
        self.wait_released(1)

        # 2. The process factory refuses: no child exists.
        class Refused(subprocess.Popen):
            def __init__(self, *arguments, **options):
                raise FileNotFoundError(2, 'No such file or directory')

        run = self.open_run()
        with mock.patch.object(owned_execution, '_POPEN', Refused):
            code, dispatched = self.dispatch(run)
        self.assertEqual(0, code, dispatched)
        self.assertEqual('launch_failed', dispatched['bridge_report']['outcome'], dispatched)
        self.record(run[0], run[1], run[3], dispatched)
        self.wait_released(2)

        # 3. Admitted, then failed before launch: settled at the host's replacement, holding meanwhile.
        self.admitted_then_failed(self.open_run())

        # 4. The run's own preparation cleanup fails once at retirement (the orphan path).
        run = self.open_run()
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        original_remove, failures = codex_preparation._remove, [1]

        def remove_fails_once(path):
            if failures:
                failures.pop()
                return False
            return original_remove(path)

        with mock.patch.object(codex_preparation, '_remove', remove_fails_once):
            self.record(run[0], run[1], run[3], dispatched)
            self.wait_for(lambda: self.released(3) and not failures, seconds=30)
        # Its preparation is given back and removed by the retried cleanup.
        self.wait_for(lambda: self.held()['preparations'] == 1, seconds=30)

        # 5. A fifth run dispatches and completes, with no restart of the service.
        run = self.open_run()
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        self.record(run[0], run[1], run[3], dispatched)
        self.wait_released(4)
        self.assertEqual([], self.replacements)
        # Only the admitted-then-failed run holds anything: its preparation, worker and result entry.
        self.wait_for(lambda: self.held() == {'runs': 1, 'preparations': 1, 'workers': 1, 'results': 1,
                                              'executions': 0, 'monitor': 0}, seconds=30)


class HostWarmUpStatusTests(HostSlotHarness):
    """LRS-7: learning-status shows whether the project service finished its start-up checks."""

    def test_LRS_7_a_service_that_finished_its_start_up_checks_says_ready(self):
        def warm():
            service = self.cli('learning-status').get('project_service') or {}
            return service.get('warm_up', {}).get('state') == 'ready'
        self.wait_for(warm, seconds=30)
        service = self.cli('learning-status')['project_service']
        self.assertEqual({'state', 'since_ms', 'failed_phase', 'last_failure_code'}, set(service['warm_up']))
        self.assertEqual((None, None), (service['warm_up']['failed_phase'], service['warm_up']['last_failure_code']))


class HostReplacementTests(HostSlotHarness):
    """A hold nothing here can free keeps its slot; the host is replaced once idle."""
    IDLE_SECONDS = 4

    def setUp(self):
        patcher = mock.patch.object(service_harness, 'serve_project', functools.partial(
            service_harness.serve_project, idle_stop_ns=self.IDLE_SECONDS * 1_000_000_000))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def host_thread(self):
        return [t for t in self.threads if t.name.startswith('test-project-host-')][-1]

    def test_M3_an_admitted_then_failed_run_is_settled_when_its_host_is_replaced_once_idle(self):
        self.succeed()
        self.admitted_then_failed(self.open_run())
        stuck = self.hosts[-1]
        self.assertEqual(1, self.account_slots())  # the account still counts the admitted claim
        thread = self.host_thread()
        # No command: once idle the host stops by itself, settling its own run on the kernel's evidence
        # (it never spawned), so the account no longer counts it.
        thread.join(self.IDLE_SECONDS + 30)
        self.assertFalse(thread.is_alive(), 'the host holding a stuck slot was never replaced')
        self.wait_for(lambda: self.account_slots() == 0, seconds=30)
        # The next command starts a fresh host by the production path, and a run completes there.
        run = self.open_run()
        self.assertIsNot(stuck, self.hosts[-1])
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)

    def test_M1_an_unconfirmed_cleanup_keeps_its_slot_says_so_and_the_host_is_replaced_once_idle(self):
        self.succeed()
        run = self.open_run()
        with mock.patch.object(process_tree.ProcessTree, 'cleanup_after_owned_wait', return_value=False):
            code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        self.record(run[0], run[1], run[3], dispatched)
        stuck, thread = self.hosts[-1], self.host_thread()
        self.wait_for(lambda: ('driver', 'acknowledging', 'cleanup_unconfirmed') in self.stopped_runs, seconds=30)
        # Its slot is kept: a child may still be alive, so nothing of it is freed.
        held = self.held(stuck)
        self.assertEqual((1, 1, 1, 1), (held['preparations'], held['workers'], held['results'], held['executions']))
        self.assertEqual(1, self.account_slots())
        # Status says so plainly while the host still serves.
        service = self.cli('learning-status')['project_service']
        self.assertEqual({'stuck_run_slots': 1, 'limit': 3, 'replaced_when_idle': True},
                         {name: service[name] for name in ('stuck_run_slots', 'limit', 'replaced_when_idle')})
        self.assertIn('replaced', service['summary'])
        self.assertTrue(thread.is_alive())
        # Once idle the host is replaced; its stopping settles the run on the kernel's evidence.
        thread.join(self.IDLE_SECONDS + 30)
        self.assertFalse(thread.is_alive(), 'the host holding an unconfirmed cleanup was never replaced')
        self.wait_for(lambda: self.account_slots() == 0, seconds=30)
        nxt = self.open_run()
        self.assertIsNot(stuck, self.hosts[-1])
        code, dispatched = self.dispatch(nxt)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)

    def test_D1_a_host_replaced_once_idle_with_a_recorded_worker_leaves_its_verifier_to_the_replacement(self):
        """D1 with O0c M3: before D1 a host with a stuck slot stopped once idle even while it kept recorded workers'
        originals in memory, so their verifiers could no longer be opened. Those originals are now in the project
        store, so the replacement opens, runs and records the verifier, and QA of the pair goes through."""
        from tests.test_project_driver import HostVerificationTests
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        worker = HostVerificationTests.recorded_worker(self, orchestration, 'task-before-replacement')
        self.succeed()
        self.admitted_then_failed(self.open_run())  # a slot nothing here can free
        stuck, thread = self.hosts[-1], self.host_thread()
        thread.join(self.IDLE_SECONDS + 30)
        self.assertFalse(thread.is_alive(), 'the host holding a stuck slot was never replaced')
        verifier = HostVerificationTests.bound_verifier(self, orchestration, 'verify-on-replacement', worker)
        self.assertIsNot(stuck, self.hosts[-1])
        checks = HostVerificationTests.checks(self, worker, verifier)
        self.assertTrue(checks['checks_passed'])
        self.assertTrue(HostVerificationTests.record_qa(self, worker, verifier, checks['check_receipt'])['ready'])
