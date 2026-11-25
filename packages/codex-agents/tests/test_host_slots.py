"""O0c M1, M3, M4, M7 (ANALYSIS-0.4.0-lifetime-caps.md Part 2): every way a run ends gives back every
host slot it took (the B0-6 invariant for the project host's in-memory slots).

Integration stage: the in-process collector, account and project services (DriverHarness). The project
host has three slots of each kind (project_service.MAX_ACTIVE_ATTEMPTS): preparations, workers, result
entries and monitor attempts. Each scenario reaches its failure exit through one injected fault, then
only the product's own steps run. A service that stops is started again only by the production path
(the next command's `ensure_project_host`), here in-process so the trusted fake clients still apply.
"""
import contextlib
import functools
import json
import sqlite3
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_framework import requirements

from tests import service_harness
from tests.test_bridge import PARENT_GRANT, cli, null_filled, result
from tests.test_project_driver import DriverHarness

from codex_agents import (client_runtime, codex_preparation, owned_execution, process_tree, project_driver,
                          project_service, result_persistence)
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
    @requirements('S13C-1', 'S13C-3', why_not_scenario='Test-speed requirement: these are existing tests whose seconds, interpreters or waits were changed; the requirement is how fast and how reliably they run, not a new outcome (S13C).')
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


class PreparationCleanupLifecycleTests(HostSlotHarness):
    @requirements('PREPCLEAN-1', 'PREPCLEAN-2', 'PREPCLEAN-3',
                  why_not_scenario='Canonical CLI/service-loop lifecycle: real folder, durable memory holds, status and automatic idle replacement boundary.')
    def test_PREPCLEAN_pending_cleanup_defers_idle_replacement_then_removes_and_replaces(self):
        self.succeed()
        run = self.open_run()
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        host = self.hosts[-1]
        thread = [t for t in self.threads if t.name.startswith('test-project-host-')][-1]
        failed_twelve, allow_thirteen, idle_checked = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(allow_thirteen.set)
        attempts, folders = [], []
        original_remove = codex_preparation.remove_private_directory
        original_retry = project_driver.RunDriver._retry_cleanup

        def remove(path):
            attempts.append(len(attempts) + 1)
            folders.append(path)
            if len(attempts) <= 12:
                if len(attempts) == 12:
                    failed_twelve.set()
                return  # Physical no-op: directory remains, despite no exception.
            return original_remove(path)

        def retry(driver, handle):
            if driver is host._driver and failed_twelve.is_set() and not allow_thirteen.is_set():
                return  # Parent-coordinated fault gate, not a production retry pause.
            return original_retry(driver, handle)

        original_idle, original_clock = host.idle_ns, host._clock_ns
        def idle():
            result = original_idle()
            if failed_twelve.is_set():
                idle_checked.set()
            return result

        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=remove), \
             mock.patch.object(project_driver.RunDriver, '_retry_cleanup', retry), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_SECONDS', .001), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_MAX_SECONDS', .002), \
             mock.patch.object(host, 'idle_ns', side_effect=idle):
            self.record(run[0], run[1], run[3], dispatched)
            self.assertTrue(failed_twelve.wait(10), 'cleanup did not reach twelve physical failures')
            self.wait_for(lambda: bool(host._driver._giving_back), seconds=5)
            folder = folders[0]
            self.assertTrue(all(path == folder for path in folders))
            # Move the host's idle clock past the production 15 minute deadline; no sleeping through it.
            idle_checked.clear()
            host._clock_ns = lambda: original_clock() + 2_000_000_000_000
            self.assertTrue(idle_checked.wait(5), 'production loop did not check the idle boundary')
            self.assertTrue(thread.is_alive(), 'pending folder cleanup lost its live custodian at idle replacement')
            self.assertEqual(12, len(attempts))
            self.assertTrue(folder.is_dir())
            self.assertEqual(1, self.held(host)['preparations'])
            pending = self.cli('learning-status')
            warning = ('A preparation folder could not be removed. The project service keeps retrying in the '
                       'background and holds its slot until removal is confirmed.')
            self.assertEqual([warning], pending['project_service']['warnings'])
            self.assertFalse(pending['project_service']['replaced_when_idle'])
            for kind in ('driver_entry', 'preparation'):
                self.assertEqual(1, pending['project_holds'][kind]['held'])
            history_path = self.state.root / 'projects' / self.project_id / 'history.sqlite3'
            with contextlib.closing(sqlite3.connect('file:' + str(history_path) + '?mode=ro', uri=True)) as db:
                evidence = db.execute("SELECT evidence FROM project_holds WHERE kind='preparation' AND state='held'").fetchone()[0]
                self.assertEqual('cleanup_unknown', json.loads(evidence)['reason'])
            host._clock_ns = original_clock
            allow_thirteen.set()
            self.wait_for(lambda: not folder.exists() and self.held(host)['preparations'] == 0, seconds=5)
            self.assertEqual(13, len(attempts))
            def cleared():
                report = self.cli('learning-status')
                return (report['project_service']['warnings'] == [] and
                        all(report['project_holds'][kind]['held'] == 0 for kind in ('preparation', 'driver_entry')))
            self.wait_for(cleared, seconds=5)
            # Removal proved and holds retired: ordinary idle replacement is now safe.
            host._clock_ns = lambda: original_clock() + 2_000_000_000_000
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'clean host did not stop at its idle boundary')
        run = self.open_run()
        self.assertIsNot(host, self.hosts[-1])
        self.assertFalse(folder.exists())
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)


    @requirements('PREPCLEAN-4', why_not_scenario='Canonical CLI/service-loop lifecycle: a permanently refused removal, an update that withdraws the host, and the successor service.')
    def test_PREPCLEAN_4_replacement_proceeds_and_the_successor_resumes_the_removal(self):
        self.succeed()
        run = self.open_run()
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        first = [t for t in self.threads if t.name.startswith('test-project-host-')][-1]
        allow, attempts = threading.Event(), []
        self.addCleanup(allow.set)
        original_remove = codex_preparation.remove_private_directory

        def remove(path):
            attempts.append(path)
            if allow.is_set():
                original_remove(path)

        history_path = self.state.root / 'projects' / self.project_id / 'history.sqlite3'

        def evidence():
            with contextlib.closing(sqlite3.connect('file:' + str(history_path) + '?mode=ro', uri=True)) as db:
                row = db.execute("SELECT state,evidence FROM project_holds WHERE kind='preparation' "
                                 "ORDER BY taken_at_ms DESC").fetchone()
            return None if row is None else (row[0], json.loads(row[1] or '{}'))

        warning = ('A preparation folder could not be removed. The project service keeps retrying in the '
                   'background and holds its slot until removal is confirmed.')
        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=remove), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_SECONDS', .001), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_MAX_SECONDS', .002):
            self.record(run[0], run[1], run[3], dispatched)
            self.wait_for(lambda: len(attempts) >= 3 and (evidence() or ('', {}))[1].get('removal') == 'pending',
                          seconds=10)
            folder = attempts[0]
            self.assertTrue(folder.is_dir())
            # An update withdraws the service's hint. A pending removal must not keep it alive.
            client_runtime.runtime_hint_path(self.paths, self.project).unlink()
            first.join(15)
            self.assertFalse(first.is_alive(), 'a pending folder removal kept a withdrawn service alive')
            self.assertTrue(folder.is_dir())
            state, facts = evidence()
            self.assertEqual(('released', 'pending', str(folder)), (state, facts['removal'], facts['workspace']))
            # The successor loads the durable record, warns, and keeps retrying with the same backoff.
            self.start_project_host(self.project)
            self.assertIsNot(first, self.threads[-1])
            self.wait_for(lambda: self.cli('learning-status')['project_service']['warnings'] == [warning],
                          seconds=15)
            seen = len(attempts)
            self.wait_for(lambda: len(attempts) >= seen + 3, seconds=10)
            self.assertTrue(folder.is_dir())
            self.assertEqual([warning], self.cli('learning-status')['project_service']['warnings'])
            allow.set()
            self.wait_for(lambda: not folder.exists(), seconds=10)
            self.wait_for(lambda: self.cli('learning-status')['project_service']['warnings'] == [], seconds=10)
            self.wait_for(lambda: (evidence() or ('', {}))[1].get('removal') == 'removed', seconds=10)
            self.assertNotIn('workspace', evidence()[1])


    @requirements('PREPCLEAN-4', why_not_scenario='Canonical service-loop lifecycle: an update withdraws a host whose folder removal is permanently refused.')
    def test_PREPCLEAN_4_a_refused_removal_never_keeps_a_withdrawn_host_alive(self):
        self.succeed()
        run = self.open_run()
        code, dispatched = self.dispatch(run)
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        host = self.hosts[-1]
        thread = [t for t in self.threads if t.name.startswith('test-project-host-')][-1]
        attempts = []
        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=attempts.append), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_SECONDS', .001), \
             mock.patch.object(project_driver, 'CLEANUP_RETRY_MAX_SECONDS', .002):
            self.record(run[0], run[1], run[3], dispatched)
            self.wait_for(lambda: len(attempts) >= 3 and bool(host._driver._giving_back), seconds=10)
            self.assertTrue(attempts[0].is_dir())
            client_runtime.runtime_hint_path(self.paths, self.project).unlink()
            thread.join(15)
            self.assertFalse(thread.is_alive(), 'a pending folder removal kept a withdrawn service alive')
            self.assertTrue(attempts[0].is_dir())  # the folder is still there: its removal stays pending


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


class DispatchSlotAdmissionTests(HostSlotHarness):
    def setUp(self):
        # Installed policy is fixed before the runtime binds its dispatch service.
        patcher = mock.patch.object(project_driver, 'DISPATCH_SLOTS', 2)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def open_arguments(self, name):
        task = self.write(name + '-task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        context = ('--task', task, '--assignment', self.write(name + '-assignment.json', self.assignment_for(route)),
                   '--parent-grant', self.write(name + '-grant.json', PARENT_GRANT))
        route_path = self.write(name + '-route.json', route)
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        return context, route_path, orchestration

    def open_with(self, arguments):
        context, route, orchestration = arguments
        return self.cli('learning-open-host-run', *context, '--route', route,
                        '--orchestration-handle', orchestration)

    @requirements('M11-1', 'M11-2', 'M11-3', why_not_scenario='Real CLI and service-loop lifecycle scenario.')
    def test_M11_busy_exhaustion_refuses_then_drains_replaces_and_dispatches_the_retried_run(self):
        self.succeed()
        host = self.hosts[-1]
        thread = [t for t in self.threads if t.name.startswith('test-project-host-')][-1]
        driver = host._driver
        self.assertEqual(2, driver._dispatch._maximum)
        arguments = self.open_arguments('first')
        opened = self.open_with(arguments)
        self.assertTrue(opened['project_host_registered'])
        first = (*arguments, opened['run_handle'])
        code, first_result = self.dispatch(first)
        self.assertEqual((0, 'result'), (code, first_result.get('bridge_report', {}).get('outcome')), first_result)

        # The second slot holds a real running child when the third open arrives.
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'm11-busy-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        arguments = self.open_arguments('second')
        opened = self.open_with(arguments)
        self.assertTrue(opened['project_host_registered'])
        second = (*arguments, opened['run_handle'])
        process = self.separate_dispatch(second[0], second[1], second[3])
        self.wait_for_launches(2, [process], seconds=30)
        target = ('--orchestration-handle', second[2], '--run-handle', second[3])
        self.assertEqual('running', self.cli('learning-run-status', *target)['cancellation_state'])
        self.assertEqual(2, len(driver._dispatch._jobs))
        self.assertTrue(driver.busy())
        self.assertFalse(driver.exhausted())
        self.assertTrue(thread.is_alive())

        arguments = self.open_arguments('third')
        original = client_runtime._run_driver
        registration_replies = []

        def observe(*args, **options):
            reply = original(*args, **options)
            if args[2] == 'run.register':
                registration_replies.append(reply)
            return reply

        def claims():
            return self.collector_ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0]
        self.assertEqual(2, claims())
        with mock.patch.object(client_runtime, '_run_driver', observe):
            refused = self.open_with(arguments)
        self.assertEqual([{'protocol': 1, 'state': 'refused', 'reason_code': 'history_service_unavailable'}],
                         registration_replies)
        self.assertFalse(refused['project_host_registered'])
        self.assertNotIn((refused['run_handle'], 1), driver._runs)
        history_path = self.state.root / 'projects' / self.project_id / 'history.sqlite3'
        with contextlib.closing(sqlite3.connect('file:' + str(history_path) + '?mode=ro', uri=True)) as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM project_registered_runs WHERE run_handle=?',
                                                  (refused['run_handle'],)).fetchone()[0])
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM result_leases WHERE run_handle=?',
                                                  (refused['run_handle'],)).fetchone()[0])
        self.assertEqual(2, claims())
        self.assertEqual(2, len(self.executions()))
        self.assertEqual([], self.replacements)
        self.cli('learning-status')
        self.assertTrue(thread.is_alive())

        # Record one result while the other child remains live and controllable.
        self.record(first[0], first[1], first[3], first_result)
        self.wait_released(1)
        self.assertTrue(driver.busy())
        self.assertTrue(thread.is_alive())
        self.assertIsNone(process.poll())
        self.assertEqual('running', self.cli('learning-run-status', *target)['cancellation_state'])
        cancelled = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (cancelled['stop_reason'], cancelled['stop_requested']))
        self.assertIn(cancelled['cancellation_state'], {'stop_requested', 'cleanup_confirmed'})
        output, errors = process.communicate(timeout=30)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('cancelled', dispatched['bridge_report']['terminal_state'])
        self.record(second[0], second[1], second[3], dispatched)
        self.wait_released(2)
        # The ordinary idle timeout is 15 minutes; exhaustion ends it promptly.
        thread.join(10)
        self.assertFalse(thread.is_alive(), 'exhausted service did not stop after drain')
        self.assertEqual([], self.replacements)
        self.assertEqual(0, self.account_slots())

        self.succeed()
        retried = self.open_with(arguments)
        self.assertEqual(refused['run_handle'], retried['run_handle'])
        self.assertTrue(retried['project_host_registered'])
        self.assertEqual(1, len(self.replacements))
        self.assertIsNot(host, self.hosts[-1])
        code, dispatched = self.dispatch((*arguments, retried['run_handle']))
        self.assertEqual((0, 'result'), (code, dispatched.get('bridge_report', {}).get('outcome')), dispatched)
        self.assertEqual(3, claims())
        self.assertEqual(3, len(self.executions()))
        self.record(arguments[0], arguments[1], retried['run_handle'], dispatched)
        self.wait_released(3)


class DispatchSlotRegistryTests(unittest.TestCase):
    @requirements('M11-4', why_not_scenario='Checks the evidence binding consumed by the lifetime-cap guard.')
    def test_M11_registration_limit_is_bound_to_the_busy_drain_replacement_scenario(self):
        entry = json.loads(Path(__file__).with_name('lifetime_caps.json').read_text())['sites'][
            'project_driver.py::_register']
        self.assertEqual('test_M11_busy_exhaustion_refuses_then_drains_replaces_and_dispatches_the_retried_run',
                         entry.get('test'))
        for word in ('refus', 'busy', 'drain', 'replac'):
            self.assertIn(word, entry['ends'].lower())


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
