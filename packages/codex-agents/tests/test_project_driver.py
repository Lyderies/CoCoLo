"""A bridged Codex run driven by the project host from open to released capacity.

The collector, the account services and the project host run as threads of
this process, so the fake provider clients the bridge tests trust are the ones
they resolve. Everything else is the product path: the in-process CLI, the
hosts' enrollment, the run driver and the original owners.
"""
import base64
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
from tests.service_harness import ServiceHarness
from tests.test_bridge import (
    CLEAN, PARENT_GRANT, RATE_LIMIT, CliTestCase, cli, git, init_frame, null_filled, result, result_frame,
)

from codex_agents import codex_worker, learning_store, project_driver
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.rate_limit_contracts import RateLimitError
from codex_agents.liveness_clock import LivenessLimits

SECOND = 1_000_000_000


class DriverHarness(ServiceHarness, CliTestCase):
    """In-process collector, account and project hosts; the CLI and run driver are the product."""
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
        self.start_services()

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

    def test_routing_and_preflight_never_open_the_ledger_or_pause_the_collector(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'running-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        task = self.write('other-task.json', {**self.task(), 'id': 'task-other'})
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')):
            route = self.cli('learning-route', '--task', task)
            assigned = {**self.assignment_for(route), 'assignment_id': 'other', 'task_id': 'task-other'}
            other = ('--task', task, '--assignment', self.write('other-assignment.json', assigned),
                     '--parent-grant', self.write('grant.json', PARENT_GRANT))
            self.assertEqual(assigned, self.cli('validate-assignment', *other))
            self.assertTrue(self.cli('result-preflight', *other)['ready'])
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        process.communicate(timeout=30)

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
        # A new project registers, observes and completes the owner's signed billing
        # attestation while the child runs, without ever pausing the collector.
        newcomer = Path(self.temporary.name) / 'newcomer'
        newcomer.mkdir()
        git(newcomer, 'init', '-q')
        with mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')), \
                mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            registered = self.cli_for(newcomer, 'learning-register')
            self.assertRegex(registered['project_id'], '^prj_[0-9a-f]{32}$')
            observed = self.cli_for(newcomer, 'provider-observe', '--provider', 'codex')
            identity = observed['records'][0]
            self.assertEqual('provider_identity', identity['record_type'])
            grant = self.write('newcomer-grant.json', {
                'version': 1, 'provider': 'codex', 'account_fingerprint': identity['account_fingerprint'],
                'no_paid_overage': True, 'no_usage_credits': True, 'issued_at': 2})
            unsigned = self.cli_for(newcomer, 'learning-record-billing-attestation', '--provider', 'codex',
                                    '--grant', grant)
            from tests.learning_support import sign_owner_payload
            signature = sign_owner_payload(self.owner_key, base64.b64decode(unsigned['signing_payload_b64']),
                                           'newcomer-attestation')
            signed = self.cli_for(newcomer, 'learning-record-billing-attestation', '--provider', 'codex',
                                  '--grant', grant, '--signature', str(signature))
            self.assertTrue(signed['ready'])
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

    def test_learning_migration_never_takes_the_collector_from_under_a_run(self):
        from codex_agents.collector_client import CollectorClient
        from codex_agents.collector_contracts import CollectorError

        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        client = CollectorClient(self.state.socket, timeout=5)
        self.assertEqual({'moved': False, 'has_learning': False, 'drained': False},
                         client.learning_migration(self.project))
        with self.assertRaises(CollectorError) as busy:
            client.shutdown(only_if_drained=True)
        self.assertEqual('collector_busy', busy.exception.code)
        # The owner's command reports and refuses; it stops nothing and opens no store.
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            self.assertEqual({'ready': True, 'state': 'not_needed', 'can_start_now': False},
                             self.cli('learning-migrate-project', '--status'))
            refused = self.cli_for(self.project, 'learning-migrate-project', expected=2)
        self.assertEqual('migration_not_drained', refused['reason_code'])
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
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=60)
        self.assertTrue(client.learning_migration(self.project)['drained'])
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual((1, 1, 0), (lifecycle['host_runs_opened'], lifecycle['host_runs_terminal'],
                                     lifecycle['host_runs_pending']))


class HostRestartTests(DriverHarness):
    """The project host stops mid-run and starts again: nothing is claimed that is not known."""

    def test_after_a_host_restart_the_run_is_reported_unavailable_and_the_primary_can_continue(self):
        # The project host the harness started is replaced by one this test can stop alone.
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertIn(self.cli('learning-run-status', *target)['health'], {'starting', 'active', 'quiet'})
        self.restart_host()
        process.communicate(timeout=60)
        # The new host does not know the old run: its state is unavailable, never a claimed outcome.
        after = self.cli('learning-run-status', *target, expected=2)
        self.assertFalse(after['ready'])
        self.assertNotIn('health', after)
        # The primary carries on: a new run in the same orchestration routes, opens and runs.
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        task = self.write('next-task.json', {**self.task(), 'id': 'task-next'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next', 'task_id': 'task-next'}
        nxt = ('--task', task, '--assignment', self.write('next-assignment.json', assigned),
               '--parent-grant', self.write('grant.json', PARENT_GRANT))
        nxt_route = self.write('next-route.json', route)
        opened = self.cli('learning-open-host-run', *nxt, '--route', nxt_route, '--orchestration-handle', orchestration)
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled({**result(), 'assignment_id': 'next'})))
        dispatched = self.cli('dispatch', *nxt, '--route', nxt_route, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])

    def restart_host(self):
        """Stop the running project host and start a fresh one for the same project."""
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(20)
        self.assertFalse(host.is_alive())
        self.threads.remove(host)
        self.start_project_host(self.project)

    def setUp(self):
        self._host_stop = threading.Event()
        original = DriverHarness.start_project_host

        def first(harness, project, stop=None):
            return original(harness, project, stop or self._host_stop)

        with mock.patch.object(DriverHarness, 'start_project_host', first):
            super().setUp()


class HostVerificationTests(DriverHarness):
    """A verifier of a host-driven worker is opened, prepared and run through the same host."""

    def worker(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('worker-result.json', dispatched['result']),
                            '--observation', observation)
        return orchestration, dispatched['result'], recorded['host_outcome_digest']

    def verifier(self, orchestration, digest, **changes):
        task = {**self.task(), 'id': 'verify-1', 'objective': 'Independently verify the research result',
                'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': digest,
                'owned_paths': ['source.txt'], **changes}
        task_path = self.write('verifier-task.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = self.assignment_for(route, assignment_id='verify-assignment-1', task_id='verify-1',
                                       objective=task['objective'], context=[digest], owned_paths=['source.txt'],
                                       verification_required=route['verification_required'])
        context = ('--task', task_path, '--assignment', self.write('verifier-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        return route, assigned, context, self.write('verifier-route.json', route)

    def verified(self):
        orchestration, worker_result, digest = self.worker()
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        self.assertEqual(('codex', 'bridge'), (route['provider'], route['dispatch']))
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        (self.tools / 'codex-last-message.json').write_text(
            json.dumps(null_filled({**result(), 'assignment_id': assigned['assignment_id']})))
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        invocation = [i for i in self.invocations('codex') if 'exec' in i['argv']][-1]
        self.assertEqual(['.orchestration-input/worker-result.json', 'source.txt'], invocation['files'])
        placed = invocation['reserved_inputs']['worker-result.json']
        self.assertEqual(worker_result, json.loads(placed['content']))
        self.assertEqual('0o400', placed['mode'])  # Read-only, and the prompt names it.
        self.assertIn('.orchestration-input/worker-result.json', invocation['argv'][-1])
        observation = self.write('verifier-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('verifier-result.json', dispatched['result']),
                            '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        return digest, recorded['host_outcome_digest'], assigned, dispatched['result']

    def test_a_verifier_gets_the_recorded_worker_result_through_the_host(self):
        self.verified()

    def test_operational_qa_of_a_host_worker_and_verifier_goes_through_the_project(self):
        worker, verifier, verifier_assignment, verifier_result = self.verified()
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        self.assertTrue(checks['checks_passed'])
        files = {'--evaluator-assignment': self.write('eval-assignment.json', json.loads(
                     Path(self.temporary.name, 'project-assignment.json').read_text())),
                 '--evaluator-result': str(Path(self.temporary.name, 'worker-result.json')),
                 '--verifier-assignment': self.write('ver-assignment.json', verifier_assignment),
                 '--verifier-result': self.write('ver-result.json', verifier_result)}
        qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                      '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                      *[item for pair in files.items() for item in pair])
        self.assertTrue(qa['ready'])
        self.assertIn('accepted', json.dumps(qa))
        # A verifier assignment that is not the recorded one (here relabelled) is refused.
        relabelled = {**files, '--verifier-assignment': self.write('relabelled.json', {
            **verifier_assignment, 'role': 'another_verification_worker'})}
        refused = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                           '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                           *[item for pair in relabelled.items() for item in pair], expected=2)
        self.assertFalse(refused['ready'])
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('legacy ledger')):
            again = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                             '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                             *[item for pair in files.items() for item in pair])
        # A repeat is the same committed QA, now reported as known; no legacy ledger is opened.
        self.assertEqual((False, True), (qa['known'], again['known']))
        same = {'known', 'learning_cycle'}  # Each call runs its own learning cycle.
        self.assertEqual({k: v for k, v in qa.items() if k not in same},
                         {k: v for k, v in again.items() if k not in same})
        self.assertEqual('available', qa['learning_cycle']['summary_state'])

    def test_accepted_reconnaissance_of_the_same_task_is_trusted_routing_evidence(self):
        base = self.task()
        original_worker = self.worker

        def reconnaissance():
            # The worker's task does not yet know how broad the work is.
            with mock.patch.object(self, 'task', return_value={**base, 'context_breadth': 'unknown'}):
                return original_worker()

        with mock.patch.object(self, 'worker', reconnaissance):
            worker, verifier, verifier_assignment, verifier_result = self.verified()
        hard = {**base, 'operation': 'end_to_end', 'complexity': 'extreme', 'ambiguity': 'high',
                'context_breadth': 'cross_system', 'routing_evidence_refs': ['outcome:' + worker]}
        hard_path = self.write('hard-task.json', hard)
        plain = self.cli('learning-route', '--task', self.write('plain-task.json', {**hard, 'routing_evidence_refs': []}))
        # Recorded but not yet through QA: cited evidence is refused, not ignored.
        with mock.patch.object(cli, '_learning_ledger', side_effect=AssertionError('ledger path')):
            early = self.cli('learning-route', '--task', hard_path, expected=2)
        self.assertFalse(early['ready'])
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                      '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                      '--evaluator-assignment', self.write('eval-assignment.json', json.loads(
                          Path(self.temporary.name, 'project-assignment.json').read_text())),
                      '--evaluator-result', str(Path(self.temporary.name, 'worker-result.json')),
                      '--verifier-assignment', self.write('ver-assignment.json', verifier_assignment),
                      '--verifier-result', self.write('ver-result.json', verifier_result))
        self.assertTrue(qa['ready'])
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')):
            routed = self.cli('learning-route', '--task', hard_path)
            self.assertIn('hard_end_to_end_evidence', json.dumps(routed))
            self.assertNotIn('hard_end_to_end_evidence', json.dumps(plain))
            # The same evidence cited for another task, or for another family, is refused.
            for change in ({'id': 'another-task'}, {'family': 'coding'}):
                with self.subTest(change=change):
                    other = self.cli('learning-route', '--task', self.write('other-task.json', {**hard, **change}),
                                     expected=2)
                    self.assertFalse(other['ready'])
        # Evidence this service does not hold is left to the ledger, never assumed.
        unknown = self.write('unknown-task.json', {**hard, 'routing_evidence_refs': ['outcome:' + 'f' * 64]})
        with mock.patch.object(cli, '_learning_ledger', side_effect=learning_store.LedgerError("routing evidence is not QA accepted")):
            self.assertFalse(self.cli('learning-route', '--task', unknown, expected=2)['ready'])

    def recorded_worker(self, orchestration, task_id):
        """One more Codex worker in the same orchestration, dispatched and recorded."""
        task = {**self.task(), 'id': task_id}
        task_path = self.write(task_id + '.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = {**self.assignment_for(route), 'assignment_id': task_id + '-assignment', 'task_id': task_id}
        context = ('--task', task_path, '--assignment', self.write(task_id + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(task_id + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        worker_result = {**result(), 'assignment_id': assigned['assignment_id']}
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(worker_result)))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        observation = self.write(task_id + '-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(task_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return {'digest': recorded['host_outcome_digest'], 'assignment': assigned, 'result': dispatched['result']}

    def bound_verifier(self, orchestration, task_id, worker, *, context_digest=None, expected=0):
        task = {**self.task(), 'id': task_id, 'objective': 'Independently verify the research result',
                'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': worker['digest'],
                'owned_paths': ['source.txt']}
        task_path = self.write(task_id + '.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = self.assignment_for(route, assignment_id=task_id + '-assignment', task_id=task_id,
                                       objective=task['objective'], context=[context_digest or worker['digest']],
                                       owned_paths=['source.txt'], verification_required=False)
        context = ('--task', task_path, '--assignment', self.write(task_id + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(task_id + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration, expected=expected)
        if expected:
            return opened
        verifier_result = {**result(), 'assignment_id': assigned['assignment_id']}
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(verifier_result)))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        observation = self.write(task_id + '-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(task_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return {'digest': recorded['host_outcome_digest'], 'assignment': assigned, 'result': dispatched['result']}

    def checks(self, worker, verifier, expected=0):
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            return self.cli('learning-run-operational-checks', '--host-outcome-digest', worker['digest'],
                            '--verifier-host-outcome-digest', verifier['digest'], expected=expected)

    def record_qa(self, worker, verifier, receipt, expected=0):
        files = {'--evaluator-assignment': self.write('qa-worker-assignment.json', worker['assignment']),
                 '--evaluator-result': self.write('qa-worker-result.json', worker['result']),
                 '--verifier-assignment': self.write('qa-verifier-assignment.json', verifier['assignment']),
                 '--verifier-result': self.write('qa-verifier-result.json', verifier['result'])}
        return self.cli('learning-record-operational-qa', '--host-outcome-digest', worker['digest'],
                        '--verifier-host-outcome-digest', verifier['digest'], '--check-receipt', receipt,
                        *[item for pair in files.items() for item in pair], expected=expected)

    def test_qa_binds_the_verifier_to_the_outcome_it_was_opened_for(self):
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        worker_a = self.recorded_worker(orchestration, 'task-a')
        worker_b = self.recorded_worker(orchestration, 'task-b')
        # Bound to A, but its assignment names B: refused before anything opens.
        refused = self.bound_verifier(orchestration, 'verify-x', worker_a, context_digest=worker_b['digest'],
                                      expected=2)
        self.assertFalse(refused['ready'])
        verifier_a = self.bound_verifier(orchestration, 'verify-a', worker_a)
        # The verifier opened for A cannot stand in for B's verification.
        self.assertFalse(self.checks(worker_b, verifier_a, expected=2)['ready'])
        checks = self.checks(worker_a, verifier_a)
        self.assertTrue(checks['checks_passed'])
        # Nor can A's receipt be spent on B's QA.
        self.assertFalse(self.record_qa(worker_b, verifier_a, checks['check_receipt'], expected=2)['ready'])
        self.assertTrue(self.record_qa(worker_a, verifier_a, checks['check_receipt'])['ready'])

    def test_a_verifier_bound_to_the_wrong_provider_family_or_orchestration_is_refused(self):
        orchestration, _, digest = self.worker()
        for name, changes in {'provider': {'verification_for_provider': 'claude'},
                              'family': {'verification_for_family': 'coding'}}.items():
            with self.subTest(wrong=name):
                route, assigned, context, route_path = self.verifier(orchestration, digest, **changes)
                refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                                   '--orchestration-handle', orchestration, expected=2)
                self.assertNotEqual('admitted', refused.get('admission'))
        other = self.cli('learning-open-orchestration')['orchestration_handle']
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                           '--orchestration-handle', other, expected=2)
        self.assertNotEqual('admitted', refused.get('admission'))
        self.assertFalse([i for i in self.invocations('codex') if 'exec' in i['argv']][1:])  # Only the worker ran.

    def test_a_verifier_of_an_unknown_or_unrecorded_outcome_is_refused(self):
        orchestration, _, digest = self.worker()
        route, assigned, context, route_path = self.verifier(orchestration, 'f' * 64)
        refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                           '--orchestration-handle', orchestration, expected=2)
        self.assertNotEqual('admitted', refused.get('admission'))


class HostClaudeVerificationTests(HostVerificationTests):
    """The verifier of a Codex worker is a bridged Claude child, for a Claude primary."""
    primary = 'claude'

    def admit(self, provider):
        fingerprint = super().admit(provider)
        if provider == 'codex':
            super().admit('claude')
            from tests.test_bridge import FIXTURES_ROOT
            name = 'claude_agents_verification_worker'  # The routed Claude verifier's installed agent.
            (self.bundle / f'.claude/agents/{name}.md').write_text(
                (FIXTURES_ROOT / f'claude/{name}.md').read_text().replace('\n---\n', '\nmaxTurns: 8\n---\n', 1))
        return fingerprint

    def verified(self):
        orchestration, worker_result, digest = self.worker()  # Routed to Codex: Claude is not enabled yet.
        config = self.project / '.codex/orchestration/provider-routing.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({'version': 1, 'providers': {'codex': {'enabled': True},
                                      'claude': {'enabled': True}}, 'codex_primary_may_bridge_to_claude': True}))
        git(self.project, 'add', '.')
        git(self.project, 'commit', '-qm', 'Both providers')
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        self.frames('claude', [init_frame(), RATE_LIMIT, result_frame(json.dumps(
            {**result(), 'assignment_id': assigned['assignment_id']}))])
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        invocation = [i for i in self.invocations('claude') if '-p' in i['argv']][-1]
        self.assertIn('.orchestration-input/worker-result.json', invocation['files'])
        self.assertEqual(worker_result, json.loads(invocation['reserved_inputs']['worker-result.json']['content']))
        observation = self.write('verifier-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('verifier-result.json', dispatched['result']),
                            '--observation', observation)
        return digest, recorded['host_outcome_digest'], assigned, dispatched['result']


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
        recorders, original = [], project_driver._RateLimitRecorder.__init__

        def remember(recorder, *args):
            recorders.append(recorder)
            return original(recorder, *args)

        with mock.patch.object(project_driver._RateLimitRecorder, '__init__', remember):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
        report = dispatched['bridge_report']
        self.assertEqual(('result', 'claude', 'enforced'), (report['outcome'], report['provider'], report['read_containment']))
        self.assertEqual({'exit_code', 'result_frame_seen', 'result_subtype', 'error_code'},
                         set(report['client_diagnostics']))
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
        # The run is finished: the account takes no further rate-limit event for it.
        with self.assertRaises(RateLimitError):
            recorders[0].record_claude_rate_limit_event(
                self.project_id, run_handle=run_handle, rate_limit_info=RATE_LIMIT['rate_limit_info'])

    def test_a_running_claude_child_reports_live_status_and_stops_on_cancellation(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        # The child starts and does some work, then goes quiet well inside every bound.
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Reading.'}]}},
            {'sleep': 30}, result_frame()])
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('claude') if '-p' in i['argv']], seconds=30)
        self.wait_for(lambda: self.cli('learning-run-status', *target)['health'] in {'active', 'quiet'}, seconds=20)
        status = self.cli('learning-run-status', *target)
        self.assertEqual(('attempt', 'running'), (status['kind'], status['cancellation_state']))
        self.assertIsNone(status['stop_reason'])
        started = time.monotonic()
        cancelled = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (cancelled['stop_reason'], cancelled['stop_requested']))
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # The child never emits another event.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        self.assertEqual('cancelled', json.loads(output)['bridge_report']['terminal_state'])
        self.assertEqual('cancel_requested', self.cli('learning-run-status', *target)['stop_reason'])

    def test_a_paid_usage_event_stops_the_claude_child_at_once(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed'}}
        # Mid-message, with partial streaming on: the stop is still immediate.
        self.frames('claude', [init_frame(),
                               {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
                                   'id': 'msg_1', 'model': 'claude-opus-5-5'}}},
                               {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': 0, 'delta': {
                                   'type': 'text_delta', 'text': 'Working'}}},
                               paid, {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)
        argv = [i for i in self.invocations('claude') if '-p' in i['argv']][-1]['argv']
        self.assertIn('--include-partial-messages', argv)
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


class ClaudePrimaryClaudeChildTests(ClaudeChildDriverTests):
    """Path D: a Claude primary's Claude child is a separate `claude -p` process run by the same host."""
    primary = 'claude'


class ClaudeOnlyVerificationTests(ClaudeChildHarness):
    """With Codex disabled, a Claude worker's verifier is Claude too, which QA does not accept."""

    def admit(self, provider):
        fingerprint = super().admit(provider)
        if provider == 'codex':
            from tests.test_bridge import FIXTURES_ROOT
            name = 'claude_agents_verification_worker'
            (self.bundle / f'.claude/agents/{name}.md').write_text(
                (FIXTURES_ROOT / f'claude/{name}.md').read_text().replace('\n---\n', '\nmaxTurns: 8\n---\n', 1))
        return fingerprint

    def run_claude(self, context, route_path, orchestration, assignment_id):
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.frames('claude', [init_frame(), RATE_LIMIT, result_frame(json.dumps(
            {**result(), 'assignment_id': assignment_id}))])
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write(assignment_id + '-observation.json', {
            'run_handle': opened['run_handle'],
            'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(assignment_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return recorded['host_outcome_digest']

    def test_a_same_provider_claude_verifier_is_refused_by_qa(self):
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write('assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        worker = self.run_claude(context, self.write('route.json', route), orchestration, assigned['assignment_id'])
        verify = {**self.task(), 'id': 'verify-1', 'objective': 'Independently verify the research result',
                  'family': 'verification', 'verification_for_family': 'research',
                  'verification_for_provider': 'claude', 'verification_for_outcome': worker,
                  'owned_paths': ['source.txt']}
        verify_path = self.write('verify-task.json', verify)
        verifier_route = self.cli('learning-route', '--task', verify_path)
        self.assertEqual(('claude', 'bridge', 'claude_agents_verification_worker'),
                         (verifier_route['provider'], verifier_route['dispatch'], verifier_route['role']))
        verifier_assigned = self.assignment_for(
            verifier_route, assignment_id='verify-assignment-1', task_id='verify-1', objective=verify['objective'],
            context=[worker], owned_paths=['source.txt'], verification_required=False)
        verifier_context = ('--task', verify_path,
                            '--assignment', self.write('verify-assignment.json', verifier_assigned),
                            '--parent-grant', self.write('grant.json', PARENT_GRANT))
        verifier = self.run_claude(verifier_context, self.write('verify-route.json', verifier_route), orchestration,
                                   'verify-assignment-1')
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            refused = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                               '--verifier-host-outcome-digest', verifier, expected=2)
        self.assertEqual('operational QA admission rejected', refused['error'])


class ClaudeRestrictionDeliveryTests(ClaudeChildHarness):
    def test_an_undelivered_paid_event_reaches_the_account_before_the_run_finishes(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed', 'padding': 'x' * 10_000}}
        self.frames('claude', [init_frame(), paid, {'sleep': 30}, result_frame()])
        original, calls = AccountClient.record_rate_limit_event, []

        def unreachable_once(client, *args, **options):
            calls.append(options['request_id'])
            if len(calls) == 1:
                raise AccountError('account_unavailable')
            return original(client, *args, **options)

        with mock.patch.object(AccountClient, 'record_rate_limit_event', unreachable_once):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
            self.assertEqual('rate_limit_event_invalid', dispatched['bridge_report']['outcome'])
            self.wait_for(lambda: ('driver', 'reporting', 'terminal') in self.stopped_runs, seconds=30)
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0], calls[1])  # The same request, so the account keeps it once.
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])

    def held_paid_event(self, limits, *, unreachable_first):
        """A paid event that is not accepted live still reaches the account before the terminal."""
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed'}}
        self.frames('claude', [init_frame(), paid, {'sleep': 30}, result_frame()])
        original, calls = AccountClient.record_rate_limit_event, []

        def record(client, *args, **options):
            calls.append(options['request_id'])
            if unreachable_first and len(calls) == 1:
                raise AccountError('account_unavailable')
            return original(client, *args, **options)

        with mock.patch.multiple(project_driver, **limits), \
                mock.patch.object(AccountClient, 'record_rate_limit_event', record):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
            self.assertEqual('rate_limit_event_invalid', dispatched['bridge_report']['outcome'])
            self.wait_for(lambda: ('driver', 'reporting', 'terminal') in self.stopped_runs, seconds=30)
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])
        return calls

    def test_an_event_past_the_run_cap_is_held_and_delivered(self):
        calls = self.held_paid_event({'MAX_RUN_RATE_LIMIT_EVENTS': 0}, unreachable_first=False)
        self.assertEqual(1, len(calls))  # Not sent live; delivered once before the terminal.

    def test_events_past_the_pending_limit_are_folded_and_delivered(self):
        calls = self.held_paid_event({'MAX_PENDING_RATE_LIMITS': 0}, unreachable_first=True)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[1].endswith('-merged'))

    def test_a_client_changed_after_the_account_observed_it_is_never_launched(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), result_frame()])
        with mock.patch.object(codex_worker, '_client_digest', return_value='0' * 64):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('dispatch_unavailable', refused['reason_code'])
        self.assertFalse([i for i in self.invocations('claude') if '-p' in i['argv']])


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
