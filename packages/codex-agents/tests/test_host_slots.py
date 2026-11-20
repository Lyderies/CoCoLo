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
from pathlib import Path
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
