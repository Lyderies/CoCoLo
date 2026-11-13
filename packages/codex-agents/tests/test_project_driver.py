"""A bridged Codex run driven by the project host from open to released capacity.

The collector, the account services and the project host run as threads of
this process, so the fake provider clients the bridge tests trust are the ones
they resolve. Everything else is the product path: the in-process CLI, the
hosts' enrollment, the run driver and the original owners.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests.learning_support import attest_provider
from tests.test_bridge import (
    CLEAN, PARENT_GRANT, RATE_LIMIT, CliTestCase, cli, git, init_frame, null_filled, result, result_frame,
)

from codex_agents import client_runtime, collector_runtime, learning_store, project_driver
from codex_agents.account_host import AccountServiceHost
from codex_agents.collector_client import CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_store import CollectorServer, LearningLedger
from codex_agents.liveness_clock import LivenessLimits
from codex_agents.project_service import serve_project
from codex_agents.service_enrollment import capability_path

SECOND = 1_000_000_000


class DriverHarness(CliTestCase):
    """In-process collector, account and project hosts; the CLI and run driver are the product."""
    liveness = None  # The installed provisional policy unless a test narrows it.
    primary = 'codex'  # Which primary host class drives the CLI.

    def setUp(self):
        super().setUp()
        if self.primary == 'claude':
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop('CODEX_THREAD_ID', None)
            os.environ.pop('CODEX_SESSION_ID', None)
            os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='claude-primary')
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
        self.threads = [threading.Thread(target=self.collector, name='test-collector')]
        self.threads[0].start()
        self.wait_for(lambda: capability_path(self.paths, None, 'account').exists())
        self.start_project_host(self.project)

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

    def start_project_host(self, project):
        def serve():
            try:
                report = serve_project(self.state.root, project, stopped=self.stop.is_set, bundle_root=self.bundle,
                                       **({} if self.liveness is None else {'liveness': self.liveness}))
                if not report.get('ready'):
                    self.failures.append('project host refused: %r' % (report,))
            except BaseException as error:
                self.failures.append(repr(error))

        thread = threading.Thread(target=serve, name='test-project-host-' + project.name)
        self.threads.append(thread)
        thread.start()
        self.wait_for(lambda: runtime_hint_path(self.paths, project).exists())

    def cli_for(self, project, command, *arguments, expected=0):
        stdout = io.StringIO()
        argv = ['cli.py', command, '--state-root', str(self.state.root), '--project-root', str(project), *arguments]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle), \
                contextlib.redirect_stdout(stdout):
            code = cli.main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def stop_services(self):
        self.stop.set()
        for thread in reversed(self.threads):
            thread.join(20)
            self.assertFalse(thread.is_alive(), thread.name + ' did not stop')
        self.assertEqual([], self.failures)
        workers = {'account-observation', 'signature-fixed-worker', 'codex-agents-collector', 'account-ingress'}
        self.assertEqual([], [t.name for t in threading.enumerate() if t.name in workers and t.is_alive()])

    def open_run(self, project=None):
        project = project or self.project
        name = project.name + '-'
        task = self.write(name + 'task.json', self.task())
        route = self.cli_for(project, 'learning-route', '--task', task)
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write(name + 'assignment.json', assigned),
                   '--parent-grant', self.write(name + 'grant.json', PARENT_GRANT))
        route_path = self.write(name + 'route.json', route)
        orchestration = self.cli_for(project, 'learning-open-orchestration')
        opened = self.cli_for(project, 'learning-open-host-run', *context, '--route', route_path,
                              '--orchestration-handle', orchestration['orchestration_handle'])
        return context, route_path, orchestration['orchestration_handle'], opened['run_handle']

    def separate_dispatch(self, context, route_path, run_handle, project=None):
        """A second primary process waits on dispatch while this one inspects the run."""
        env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
            env.pop(name, None)
        if self.primary == 'claude':
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='claude-primary')
        else:
            env['CODEX_THREAD_ID'] = 'driver-primary'
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), 'dispatch',
            '--state-root', str(self.state.root), '--project-root', str(project or self.project), *context,
            '--route', route_path, '--run-handle', run_handle], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process


class ProjectDriverTests(DriverHarness):
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
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('learning-cancel-run', *target, expected=2)
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

    def test_only_the_opening_host_class_can_prepare_and_dispatch_its_run(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('host_client_mismatch', foreign['reason_code'])
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])
        # Nothing was claimed for the refused host; the owner can still run it once.
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        self.assertEqual(1, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))

    def test_a_slow_enrolled_driver_sender_cannot_stall_the_host(self):
        from codex_agents.client_runtime import read_runtime_hint
        from codex_agents.service_enrollment import enrollment_preamble, proof_directory, read_capability

        _, control, _ = read_runtime_hint(self.paths, self.project)
        driver = control.parent / 'run.sock'
        frame = b'{"attempt":1,"inputs_digest":"' + b'0' * 64 + b'","operation":"run.prepare","protocol":1,"run_handle":"hrn_x"}'
        line = enrollment_preamble(read_capability(self.paths, self.project, 'host'), 'host', 'codex-host-v1', 1, frame,
                                   proof_directory(driver), context=b'{}')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(driver))
            slow.sendall(line + b'\n' + frame[:10])  # Enrolled, then the request trickles in.
            started = time.monotonic()
            opened = self.cli('learning-open-orchestration')
            self.assertEqual('active', opened['status'])
            self.assertLess(time.monotonic() - started, 2)
            slow.sendall(frame[10:20])
            slow.settimeout(5)
            self.assertEqual(b'', slow.recv(1))  # Dropped at its absolute deadline, unanswered.

    def test_an_undispatched_run_is_abandoned_only_by_its_host_class_and_never_launches(self):
        context, route_path, orchestration, run_handle = self.open_run()
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                               '--reason', 'owner_cancelled', expected=2)
        self.assertEqual('host_client_mismatch', foreign['reason_code'])
        abandoned = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                             '--reason', 'owner_cancelled')
        self.assertEqual(('abandoned', 'never_claimed'), (abandoned['classification'], abandoned['capacity_state']))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_registering_another_project_during_a_run_does_not_disturb_it(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        # A new project registers while the child runs; registration still takes the
        # legacy maintenance path, which stops and restarts the collector.
        newcomer = Path(self.temporary.name) / 'newcomer'
        newcomer.mkdir()
        git(newcomer, 'init', '-q')
        registered = self.cli_for(newcomer, 'learning-register')
        self.assertRegex(registered['project_id'], '^prj_[0-9a-f]{32}$')
        observed = self.cli_for(newcomer, 'provider-observe', '--provider', 'codex')
        self.assertEqual('provider_identity', observed['records'][0]['record_type'])
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual(('running', None), (status['cancellation_state'], status['stop_reason']))
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('busy-observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('busy-result.json', dispatched['result']), '--observation', observation)
        # Terminal acceptance and capacity release reached the restarted account services.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=60)


class ClaudePrimaryDriverTests(ProjectDriverTests):
    """Path B: a Claude primary bridges the same Codex child through the same host."""
    primary = 'claude'


class ClaudeChildHarness(DriverHarness):
    """Path C: a Codex primary bridges a Claude child (`claude -p`) through the same host."""

    def admit(self, provider):
        # Before the services start: the account is also attested for Claude and
        # the project routes its children to Claude.
        fingerprint = super().admit(provider)
        if provider == 'codex':
            super().admit('claude')
            config = self.project / '.codex/orchestration/provider-routing.json'
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({'version': 1, 'providers': {'codex': {'enabled': False},
                                          'claude': {'enabled': True}}, 'codex_primary_may_bridge_to_claude': True}))
            git(self.project, 'add', '.')
            git(self.project, 'commit', '-qm', 'Bridge Claude children')
        return fingerprint

    def open_claude_run(self):
        context, route_path, orchestration, run_handle = self.open_run()
        route = json.loads(Path(route_path).read_text())
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        return context, route_path, orchestration, run_handle

    def dispatch_claude(self, context, route_path, run_handle):
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            return self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)



class ClaudeChildDriverTests(ClaudeChildHarness):
    def test_the_host_runs_a_claude_child_and_its_rate_limit_reaches_the_account(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Reading.'}]}},
            RATE_LIMIT, result_frame()])
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        report = dispatched['bridge_report']
        self.assertEqual(('result', 'claude', 'enforced'), (report['outcome'], report['provider'], report['read_containment']))
        self.assertEqual((1, False), (report['rate_limit_events'], report['rate_limit_record_failed']))
        self.assertGreaterEqual(report['rate_limit_records_kept'], 1)
        argv = [i for i in self.invocations('claude') if '-p' in i['argv']]
        self.assertEqual(1, len(argv))
        self.assertEqual('Read,Glob,Grep', argv[0]['argv'][argv[0]['argv'].index('--tools') + 1])
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)

    def test_a_paid_usage_event_stops_the_claude_child_at_once(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed'}}
        self.frames('claude', [init_frame(), paid, {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(('paid_usage_observed', 'failed'),
                         (dispatched['bridge_report']['outcome'], dispatched['bridge_report']['terminal_state']))
        self.assertGreater(dispatched['bridge_report']['rate_limit_records_kept'], 0)
        # The account kept the restriction, so the next Claude child on this account is refused.
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])


class ClaudeSilenceCutoffTests(ClaudeChildHarness):
    liveness = LivenessLimits(5 * SECOND, SECOND, 2 * SECOND, 3 * SECOND)

    def test_the_host_stops_a_claude_child_that_goes_silent_after_init(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Starting.'}]}},
            {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)  # Cut off by silence, not by the child's own end.
        self.assertEqual(('work_silence', 'timed_out'),
                         (dispatched['bridge_report']['outcome'], dispatched['bridge_report']['terminal_state']))
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual('work_silence', status['stop_reason'])


class SilenceCutoffTests(DriverHarness):
    # Trusted startup policy, shortened so the silence cutoff happens within the test.
    liveness = LivenessLimits(5 * SECOND, SECOND, 2 * SECOND, 3 * SECOND)

    def test_the_host_stops_a_silent_child_without_any_request(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'silent-child'},
                              {'type': 'turn.started'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        started = time.monotonic()
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        seen = set()
        while process.poll() is None and time.monotonic() - started < 25:
            status = self.cli('learning-run-status', *target)
            seen.add(status['health'])
            time.sleep(.2)
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # Cut off by silence, not by the child's own end.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('timed_out', dispatched['bridge_report']['terminal_state'])
        final = self.cli('learning-run-status', *target)
        self.assertEqual('work_silence', final['stop_reason'])
        self.assertTrue({'quiet', 'stale'} & seen, seen)


class TwoProjectTests(DriverHarness):
    def admit(self, provider):
        # A second registered, admitted project with its own host, sharing the account.
        self.second = Path(self.temporary.name) / 'second'
        self.second.mkdir()
        (self.second / 'source.txt').write_text('second marker\n')
        git(self.second, 'init', '-q')
        git(self.second, 'config', 'user.email', 'tests@example.invalid')
        git(self.second, 'config', 'user.name', 'Bridge Tests')
        git(self.second, 'add', '.')
        git(self.second, 'commit', '-qm', 'second')
        fingerprint = super().admit(provider)
        second = self.ledger.register_project(self.second, now_ms=1)
        identity = self.ledger.observe_provider(second, provider, project_root=self.tools, environ=CLEAN)[0]
        attest_provider(self.ledger, second, self.owner_key, provider, identity['account_fingerprint'])
        return fingerprint

    def test_two_projects_run_children_at_once_and_cancelling_one_leaves_the_other(self):
        self.start_project_host(self.second)
        first = self.open_run(self.project)
        second = self.open_run(self.second)
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'overlapping-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        processes = [self.separate_dispatch(first[0], first[1], first[3], self.project),
                     self.separate_dispatch(second[0], second[1], second[3], self.second)]
        self.wait_for(lambda: len([i for i in self.invocations('codex') if 'exec' in i['argv']]) == 2, seconds=40)
        targets = [('--orchestration-handle', run[2], '--run-handle', run[3]) for run in (first, second)]
        for project, target in zip((self.project, self.second), targets):
            status = self.cli_for(project, 'learning-run-status', *target)
            self.assertEqual('running', status['cancellation_state'])
        # Neither project can see the other's run.
        foreign = self.cli_for(self.second, 'learning-run-status', *targets[0], expected=2)
        self.assertIn(foreign['reason_code'], {'control_not_authorized', 'control_attempt_not_found', 'control_wrong_scope'})

        cancelled = self.cli_for(self.project, 'learning-cancel-run', *targets[0])
        self.assertEqual('cancel_requested', cancelled['stop_reason'])
        output, errors = processes[0].communicate(timeout=30)
        self.assertEqual(0, processes[0].returncode, (output + errors).decode(errors='replace'))
        self.assertEqual('cancelled', json.loads(output)['bridge_report']['terminal_state'])
        # The other project's child is still running, untouched.
        self.assertIsNone(processes[1].poll())
        other = self.cli_for(self.second, 'learning-run-status', *targets[1])
        self.assertEqual(('running', None), (other['cancellation_state'], other['stop_reason']))
        self.cli_for(self.second, 'learning-cancel-run', *targets[1])
        output, errors = processes[1].communicate(timeout=30)
        self.assertEqual(0, processes[1].returncode, (output + errors).decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
