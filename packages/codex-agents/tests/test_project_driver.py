"""A bridged Codex run driven by the project host from open to released capacity.

The collector, the account services and the project host run as threads of
this process, so the fake provider clients the bridge tests trust are the ones
they resolve. Everything else is the product path: the in-process CLI, the
hosts' enrollment, the run driver and the original owners.
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests.test_bridge import PARENT_GRANT, CliTestCase, null_filled, result

from codex_agents import client_runtime, collector_runtime, learning_store, project_driver
from codex_agents.account_host import AccountServiceHost
from codex_agents.collector_client import CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_store import CollectorServer, LearningLedger
from codex_agents.project_service import serve_project
from codex_agents.service_enrollment import capability_path


class ProjectDriverTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.admit('codex')
        self.ledger.close()
        self.paths = CollectorPaths(self.state.root)
        self.stop, self.restart = threading.Event(), threading.Event()
        self.restart.set()
        self.failures = []
        # Legacy routing still stops the collector for maintenance. Its restart is
        # this thread's in-process collector, so the trusted fake clients still apply.
        for module in (collector_runtime, learning_store):
            patcher = mock.patch.object(module, '_start_collector_locked', self.start_collector)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Where a run stopped, for the failure message; the driver itself keeps only a reason code.
        self.stopped_runs, original_fail = [], project_driver.RunDriver._fail

        def record(driver, run, reason):
            self.stopped_runs.append((run.state, reason))
            return original_fail(driver, run, reason)

        patcher = mock.patch.object(project_driver.RunDriver, '_fail', record)
        patcher.start()
        self.addCleanup(patcher.stop)
        original_exchange = client_runtime._run_driver

        def exchange(*args, **options):
            try:
                reply = original_exchange(*args, **options)
            except Exception as error:
                self.stopped_runs.append(('client', args[1], repr(error)))
                raise
            self.stopped_runs.append(('client', args[1], reply))
            return reply

        patcher = mock.patch.object(client_runtime, '_run_driver', exchange)
        patcher.start()
        self.addCleanup(patcher.stop)
        from codex_agents import dispatch_api
        original_call = dispatch_api.DispatchClient.call

        def call(client, operation, *args, **options):
            try:
                view = original_call(client, operation, *args, **options)
            except Exception as error:
                self.stopped_runs.append((operation, repr(error)))
                raise
            self.stopped_runs.append((operation, view.get('state')))
            return view

        patcher = mock.patch.object(dispatch_api.DispatchClient, 'call', call)
        patcher.start()
        self.addCleanup(patcher.stop)
        from codex_agents import prepared_dispatch
        original_advance = prepared_dispatch.PreparedDispatchService._advance

        def advance(service, job):
            before = job.state
            try:
                return original_advance(service, job)
            except Exception as error:
                self.stopped_runs.append(('dispatch-drive', before, repr(error)))
                raise

        patcher = mock.patch.object(prepared_dispatch.PreparedDispatchService, '_advance', advance)
        patcher.start()
        self.addCleanup(patcher.stop)
        original_driver_advance = project_driver.RunDriver._advance

        def driver_advance(driver, run):
            before = run.state
            try:
                return original_driver_advance(driver, run)
            finally:
                if run.state != before:
                    self.stopped_runs.append(('driver', before, run.state))

        patcher = mock.patch.object(project_driver.RunDriver, '_advance', driver_advance)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.threads = [threading.Thread(target=self.collector, name='test-collector'),
                        threading.Thread(target=self.project_host, name='test-project-host')]
        self.threads[0].start()
        self.wait_for(lambda: capability_path(self.paths, None, 'account').exists())
        self.threads[1].start()
        self.wait_for(lambda: runtime_hint_path(self.paths, self.project).exists())

    def tearDown(self):
        # Before the base class removes the state these services own.
        try:
            self.stop_services()
        finally:
            super().tearDown()

    def wait_for(self, condition, seconds=20):
        end = time.monotonic() + seconds
        while not condition():
            if self.failures:
                self.fail(self.failures[0])
            self.assertLess(time.monotonic(), end, 'service did not become ready')
            time.sleep(.01)

    def start_collector(self, state, cli_path, *, project_root, timeout=1.0):
        self.restart.set()
        end = time.monotonic() + 10
        while True:
            try:
                return CollectorClient(self.paths.socket).health()
            except CollectorError:
                if time.monotonic() >= end:
                    raise
                time.sleep(.01)

    def collector(self):
        """collector_host.py's loop, restarted in this thread whenever maintenance asks."""
        try:
            while not self.stop.is_set():
                if not self.restart.wait(.05):
                    continue
                self.restart.clear()
                ledger = LearningLedger(self.state, client_capabilities=self.clients)
                server, account = CollectorServer(ledger), None
                try:
                    server.start()
                    account = AccountServiceHost(ledger, self.paths)
                    while not self.stop.is_set() and not server.wait(.002):
                        account.serve_once()
                finally:
                    try:
                        if account is not None:
                            account.close()
                            end = time.monotonic() + 10
                            while not account.drained() and time.monotonic() < end:
                                time.sleep(.01)
                            if not account.drained():
                                self.failures.append('account services did not drain')
                    finally:
                        server.stop()
                        ledger.close()
        except BaseException as error:  # Surface a service failure in the test thread.
            self.failures.append(repr(error))

    def project_host(self):
        try:
            report = serve_project(self.state.root, self.project, stopped=self.stop.is_set, bundle_root=self.bundle)
            if not report.get('ready'):
                self.failures.append('project host refused: %r' % (report,))
        except BaseException as error:
            self.failures.append(repr(error))

    def stop_services(self):
        self.stop.set()
        for thread in reversed(self.threads):
            thread.join(20)
            self.assertFalse(thread.is_alive(), thread.name + ' did not stop')
        self.assertEqual([], self.failures)
        workers = {'account-observation', 'signature-fixed-worker', 'codex-agents-collector', 'account-ingress'}
        self.assertEqual([], [t.name for t in threading.enumerate() if t.name in workers and t.is_alive()])

    def test_the_host_prepares_claims_dispatches_and_releases_a_bridged_codex_run(self):
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        self.assertEqual(('codex', 'bridge'), (route['provider'], route['dispatch']))
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write('assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write('route.json', route)
        orchestration = self.cli('learning-open-orchestration')
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration['orchestration_handle'])
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        argv = [i for i in self.invocations('codex') if 'exec' in i['argv']][0]['argv']
        self.assertIn('--ignore-rules', argv)
        self.assertEqual('read-only', argv[argv.index('-s') + 1])
        self.assertEqual(1, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))
        self.assertEqual('retained', dispatched['bridge_report']['capacity_state'])

        observation = self.write('observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        made_up = {**dispatched['result'], 'summary': 'A verdict the child never gave.'}
        refused = self.cli('learning-record-host-result', *context, '--route', route_path,
                           '--result', self.write('made-up.json', made_up), '--observation', observation, expected=2)
        self.assertEqual('history_conflict', refused['reason_code'])
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        self.assertEqual(route['model'], recorded['outcome']['model'])
        # Recording lets the host finish: terminal accepted, cleanup confirmed, capacity released.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual([], [entry for entry in self.stopped_runs if len(entry) == 2 and entry[0] in
                              {'recording_open', 'claiming', 'registering', 'terminal', 'releasing'}])

    def open_run(self):
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write('assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write('route.json', route)
        orchestration = self.cli('learning-open-orchestration')
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration['orchestration_handle'])
        return context, route_path, orchestration['orchestration_handle'], opened['run_handle']

    def separate_dispatch(self, context, route_path, run_handle):
        """A second primary process waits on dispatch while this one inspects the run."""
        env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        env['CODEX_THREAD_ID'] = 'driver-primary'
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), 'dispatch',
            '--state-root', str(self.state.root), '--project-root', str(self.project), *context,
            '--route', route_path, '--run-handle', run_handle], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_a_running_child_reports_live_status_and_stops_on_cancellation(self):
        context, route_path, orchestration, run_handle = self.open_run()
        # The child starts, then goes quiet well inside every bound.
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'quiet-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)

        status = self.cli('learning-run-status', *target)
        self.assertEqual(('attempt', 'running', False), (status['kind'], status['cancellation_state'], status['durable']))
        self.assertIn(status['health'], {'starting', 'active'})
        self.assertIsNone(status['stop_reason'])
        # Another host class cannot inspect or stop this run.
        os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
        os.environ.pop('CODEX_THREAD_ID'), os.environ.pop('CODEX_SESSION_ID', None)
        try:
            foreign = self.cli('learning-cancel-run', *target, expected=2)
        finally:
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
                os.environ.pop(name, None)
            os.environ.update(CODEX_THREAD_ID='tests-thread', CODEX_SESSION_ID='tests-session')
        self.assertEqual('control_not_authorized', foreign['reason_code'])

        started = time.monotonic()
        cancelled = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (cancelled['stop_reason'], cancelled['stop_requested']))
        self.assertIn(cancelled['cancellation_state'], {'stop_requested', 'cleanup_confirmed'})
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # Long before the child's own 30-second sleep ends.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('cancelled', dispatched['bridge_report']['terminal_state'])
        settled = self.cli('learning-run-status', *target)
        self.assertTrue(settled['stop_requested'])
        self.assertEqual('cancel_requested', settled['stop_reason'])


if __name__ == '__main__':
    unittest.main()
