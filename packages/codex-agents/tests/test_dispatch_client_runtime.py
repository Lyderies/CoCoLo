"""Actual consumed grants, process execution and guarded CLI, synthetic native peers."""
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from tests import test_project_execution_processes as fixtures
from tests.test_bridge import PARENT_GRANT
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.account_api import AccountRequest
from codex_agents.terminal_delivery import TerminalSubmissionInput
from codex_agents.cleanup_contracts import verify_cleanup_evidence
from codex_agents.control_api import ControlClient, ControlScope
from codex_agents.dispatch_api import DispatchClient, DispatchError
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.service_protocol import ServiceTarget
from codex_agents.result_read_api import ResultReadClient
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS


class DispatchClientRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedProjectExecutionProcessTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bridge, self.base = self.fixture.bridge, self.fixture.base
        self.clients = []
        self.addCleanup(self.stop_clients)

    def stop_clients(self):
        for process in self.clients:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def start(self, *, hold_result_publish=False, hold_result_read=False, await_release=False):
        child = self.fixture.start(self.fixture.fixture.scope, self.fixture.fixture.request,
            self.fixture.fixture.directory, 'rpc', with_provenance=True, client_dispatch=True,
            hold_result_publish=hold_result_publish, hold_result_read=hold_result_read, await_release=await_release)
        self.assertFalse(child['dispatch_ready']['launched'])
        self.assertEqual(0, child['dispatch_ready']['tickets'])
        return child

    def control_scope(self, child):
        return ControlScope(**{n: asdict(child['scope'])[n] for n in
            ('project_id', 'primary_session_id', 'supervisor_instance_id', 'supervisor_generation', 'runtime_digest')})

    def test_actual_dispatch_metadata_is_idempotent_and_uses_original_writer(self):
        child = self.start()
        ready, scope = child['dispatch_ready'], self.control_scope(child)
        client = DispatchClient(ready['dispatch_endpoint'], ready['result_endpoint'], scope, timeout=2)
        with self.assertRaisesRegex(DispatchError, 'dispatch_not_found'):
            client.call('dispatch.status', child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'])
        with self.assertRaisesRegex(DispatchError, 'dispatch_conflict'):
            client.call('dispatch.submit', child['scope'].run_handle, child['scope'].attempt, 'f'*64)
        first = client.call('dispatch.submit', child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'])
        self.assertFalse(first['known'])
        self.fixture.read(child, 'started')
        known = client.call('dispatch.submit', child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'])
        self.assertTrue(known['known'])
        self.assertEqual(first['binding'], known['binding'])
        self.assertTrue(self.fixture.control(child, 'project.cancel').result['stop_requested'])
        done = self.fixture.accept_terminal(child)
        self.assertEqual(['launch'], done['launches'])
        self.assertEqual(1, done['ticket_count'])
        published = client.call('dispatch.status', child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'])
        self.assertEqual('published', published['state'])
        self.assertEqual('retained', published['report']['capacity_state'])
        self.fixture.send(child, {'client_done': True})
        self.assertEqual(0, child['process'].wait(timeout=10))

    def launch_cli(self, child, *, install_hint=True):
        ready, scope = child['dispatch_ready'], self.control_scope(child)
        state = CollectorPaths(self.bridge.state.root)
        project_root = self.bridge.project
        path = runtime_hint_path(state, project_root)
        if install_hint:
            path.parent.mkdir(mode=0o700)
            path.write_bytes(canonical_json({'schema_version': 1,
                'project_root_digest': keyed_digest(state.digest_key(), str(project_root.resolve())),
                'scope': asdict(scope), 'dispatch_endpoint': ready['dispatch_endpoint'], 'result_endpoint': ready['result_endpoint']}))
            path.chmod(0o600)
        arguments = []
        for flag, value in (('task', self.bridge.task()), ('route', self.base.fixture.route),
                            ('assignment', self.base.fixture.assignment), ('parent-grant', PARENT_GRANT)):
            target = self.bridge.state.root / (flag + '.json')
            target.write_bytes(canonical_json(value))
            arguments.extend(['--'+flag, str(target)])
        environment = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID'):
            environment.pop(marker, None)
        environment['CODEX_THREAD_ID'] = 'fixture-dispatch-primary'
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.dispatch_cli_fixture', 'dispatch',
            '--project-root', str(project_root), '--state-root', str(state.root), '--run-handle', child['scope'].run_handle,
            *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.clients.append(process)
        return process

    def test_fresh_normal_cli_dispatch_never_opens_database_or_takes_collector(self):
        child = self.start(hold_result_publish=True)
        process = self.launch_cli(child)
        self.fixture.read(child, 'started')
        self.fixture.control(child, 'project.cancel')
        self.fixture.read(child, 'writer_stalled')
        scope = self.control_scope(child)
        control = ControlClient(Path(child['dispatch_ready']['dispatch_endpoint']), scope, timeout=1)
        target = ServiceTarget(child['scope'].orchestration_handle, child['scope'].run_handle, child['scope'].attempt)
        started = time.monotonic()
        self.assertTrue(control.status(target).result['stop_requested'])
        self.assertTrue(control.cancel(target).result['stop_requested'])
        self.assertLess(time.monotonic()-started, 2)
        self.assertIsNone(process.poll(), 'client returned before the original writer committed')
        self.fixture.send(child, {'release_writer': True})
        done = self.fixture.accept_terminal(child)
        output, errors = process.communicate(timeout=12)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        result = json.loads(output)
        self.assertTrue(result['ready'])
        self.assertEqual('cancelled', result['result']['execution_state'])
        self.assertEqual(child['receipt']['bridge_run_id'], result['bridge_report']['bridge_run_id'])
        self.assertEqual(1, done['ticket_count'])
        self.fixture.send(child, {'client_done': True})
        self.assertEqual(0, child['process'].wait(timeout=10))

    def test_missing_runtime_never_falls_back_to_client_persistence(self):
        child = self.start()
        process = self.launch_cli(child, install_hint=False)
        output, errors = process.communicate(timeout=10)
        self.assertEqual(2, process.returncode, errors.decode(errors='replace'))
        self.assertEqual('dispatch_unavailable', json.loads(output)['reason_code'])
        self.assertFalse((child['root']/'launches').exists())
        # There is no launched child; terminate this disposable waiting fixture.
        child['process'].terminate()
        child['process'].wait(timeout=10)

    def test_actual_account_release_retires_dispatch_payload_and_replay_cannot_launch(self):
        child = self.start(await_release=True)
        process = self.launch_cli(child)
        self.fixture.read(child, 'started')
        self.fixture.control(child, 'project.cancel')
        done = self.fixture.accept_terminal(child)
        output, errors = process.communicate(timeout=12)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        self.assertEqual('retained', json.loads(output)['bridge_report']['capacity_state'])
        raw = bytes.fromhex(done['cleanup_evidence'])
        cleanup = verify_cleanup_evidence(self.fixture.fixture.key, child['scope'], raw)
        self.fixture.contexts[child['context']]['cleanup_evidence'] = raw
        delivery = cleanup.delivery
        submission = TerminalSubmissionInput(child['registration'], child['registration_digest'],
            delivery.record_id, delivery.record_content_digest, delivery.result_digest)
        request = AccountRequest(1, 'account.release_capacity', 'dispatch-original-release',
            child['scope'].project_id, COLLECTOR_RUNTIME_DIGEST, submission.payload())
        receipt = self.fixture.account(request, child['context'])
        self.assertEqual('released', receipt['state'])
        self.assertEqual({**receipt, 'known': True}, self.fixture.account(request, child['context']))
        self.fixture.send(child, receipt)
        retired = self.fixture.read(child, 'release_received')
        self.assertTrue(retired['local_execution_released'])
        self.assertTrue(retired['local_persistence_retired'])
        self.assertTrue(retired['local_worker_retired'])
        self.assertTrue(retired['dispatch_payload_dropped'])
        self.assertEqual(1, retired['dispatch_lifetime_slots'])
        self.assertEqual(['running', 'published', 'acknowledged', 'cleanup_ready', 'account_reply_only',
            'writer_reply_unknown', 'release_acknowledged', 'release_finished', 'persistence_retired'],
            retired['dispatch_retirement_refusals'])
        ready = child['dispatch_ready']
        client = DispatchClient(ready['dispatch_endpoint'], ready['result_endpoint'], self.control_scope(child), timeout=2)
        for operation in ('dispatch.status', 'dispatch.submit'):
            view = client.call(operation, child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'])
            self.assertEqual('retired', view['state'])
            self.assertTrue(view['known'])
            self.assertIsNone(view['delivery'])
            self.assertIsNone(view['report'])
        with self.assertRaisesRegex(DispatchError, 'dispatch_unavailable'):
            client.dispatch(child['scope'].run_handle, child['scope'].attempt, ready['inputs_digest'], wait_seconds=2)
        self.assertEqual(['launch'], (child['root']/'launches').read_text().splitlines())
        self.fixture.send(child, {'client_done': True})
        self.assertEqual(0, child['process'].wait(timeout=10))

    def test_active_child_can_be_inspected_and_cancelled_while_result_reader_stalls_writer(self):
        child = self.start(hold_result_read=True)
        process = self.launch_cli(child)
        self.fixture.read(child, 'started')
        scope = child['scope']
        binding = AttemptBinding(**{name: getattr(scope, name) for name in LEASE_SCOPE_FIELDS},
                                 claim_id=child['receipt']['bridge_run_id'],
                                 claim_digest=child['receipt']['receipt_digest'])
        reader = ResultReadClient(Path(child['dispatch_ready']['result_endpoint']), timeout=3)
        control = ControlClient(Path(child['dispatch_ready']['dispatch_endpoint']), self.control_scope(child), timeout=1)
        target = ServiceTarget(scope.orchestration_handle, scope.run_handle, scope.attempt)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(reader.read_chunk, binding, 'f'*64)
            self.assertEqual('read_result', self.fixture.read(child, 'writer_stalled')['operation'])
            started = time.monotonic()
            status = control.status(target).result
            self.assertFalse(status['terminal_observed'])
            self.assertFalse(status['stop_requested'])
            self.assertTrue(control.cancel(target).result['stop_requested'])
            self.assertTrue(control.status(target).result['stop_requested'])
            self.assertLess(time.monotonic()-started, 2)
            self.assertFalse(pending.done(), 'writer did not remain stalled during control requests')
            self.assertIsNone(process.poll(), 'CLI returned before durable result publication')
            self.fixture.send(child, {'release_writer': True})
            with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
                pending.result(timeout=4)
        done = self.fixture.accept_terminal(child)
        output, errors = process.communicate(timeout=12)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        result = json.loads(output)
        self.assertEqual('cancelled', result['result']['execution_state'])
        self.assertEqual(['launch'], done['launches'])
        self.assertEqual(1, done['ticket_count'])
        self.fixture.send(child, {'client_done': True})
        self.assertEqual(0, child['process'].wait(timeout=10))
