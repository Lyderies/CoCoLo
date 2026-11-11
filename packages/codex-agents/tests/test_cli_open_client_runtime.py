"""Fresh CLI -> original project/account APIs; native enrollment is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
import socket
import subprocess
import sys
import time
import unittest

from tests import test_dispatch_client_runtime as dispatch_fixtures
from tests.test_bridge import PARENT_GRANT

from codex_agents.account_api import decode_account_response, encode_account_request
from codex_agents.account_ingress import AccountIngress, AccountIngressLimits
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.control_api import ControlScope
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.service_transport import exchange_frame


class CliOpenClientRuntimeTests(unittest.TestCase):
    setUp = dispatch_fixtures.DispatchClientRuntimeTests.setUp
    stop_clients = dispatch_fixtures.DispatchClientRuntimeTests.stop_clients
    control_scope = dispatch_fixtures.DispatchClientRuntimeTests.control_scope
    launch_cli = dispatch_fixtures.DispatchClientRuntimeTests.launch_cli

    def listen_account(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.account_path))
        self.account_path.chmod(0o600)
        listener.listen(4)
        listener.setblocking(False)
        return listener

    def pump_account(self):
        for _ in range(4):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                break
            # Deliberately synthetic native peer, independent of request JSON.
            self.api.attach(connection, self.native_context)
        self.fixture.base.stamp = time.time_ns()//1_000_000
        self.api.pump_owner()

    def wait_api(self, predicate):
        end = time.monotonic()+12
        while not predicate():
            self.pump_account()
            self.assertLess(time.monotonic(), end, 'bounded original account progress expired')
            time.sleep(.002)

    def close_account(self):
        self.stop_clients()
        self.listener.close()
        self.api.request_close()
        end = time.monotonic()+10
        while not self.api.drain():
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        self.executor.shutdown(wait=True)

    def original_account_call(self, request, context):
        self.assertIs(self.native_context, context)
        future = self.executor.submit(exchange_frame, self.account_path, encode_account_request(request), timeout=10)
        self.wait_api(future.done)
        return decode_account_response(future.result(), request)

    def hint(self, child, original, *, version):
        state = CollectorPaths(self.bridge.state.root)
        path = runtime_hint_path(state, self.bridge.project)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        scope = ControlScope(**asdict(original.prospective.scope))
        value = {'schema_version': version,
            'project_root_digest': keyed_digest(state.digest_key(), str(self.bridge.project.resolve())),
            'scope': asdict(scope), 'dispatch_endpoint': child['prepared']['dispatch_endpoint'],
            'result_endpoint': child['prepared']['result_endpoint']}
        if version == 2:
            value['account_endpoint'] = str(self.account_path)
        path.write_bytes(canonical_json(value))
        path.chmod(0o600)

    def invoke_open(self, original, *, assigned=None, host=None, metered_backend=False):
        host = self.parent_host if host is None else host
        args = []
        values = {'task': self.bridge.task(), 'route': self.base.fixture.route,
            'assignment': self.base.fixture.assignment if assigned is None else assigned, 'parent-grant': PARENT_GRANT}
        for flag, value in values.items():
            path = self.bridge.state.root/(flag+'.json')
            path.write_bytes(canonical_json(value))
            args.extend(['--'+flag, str(path)])
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        home = self.bridge.state.root/'cli-home'
        home.mkdir(mode=0o700, exist_ok=True)
        env['HOME'] = str(home)
        for marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID'):
            env.pop(marker, None)
        if host == 'codex':
            env['CODEX_THREAD_ID'] = 'fixture-open-primary'
        else:
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='fixture-claude-primary')
        if metered_backend:
            env['CLAUDE_CODE_USE_BEDROCK'] = '1'
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.dispatch_cli_fixture', 'learning-open-host-run',
            '--project-root', str(self.bridge.project), '--state-root', str(self.bridge.state.root),
            '--orchestration-handle', original.prospective.orchestration_handle, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        self.wait_api(lambda: process.poll() is not None)
        output, errors = process.communicate(timeout=2)
        self.assertEqual(b'', errors)
        return process.returncode, json.loads(output)

    def open_prepared_client(self, child, original):
        self.assertEqual((0, False), (child['prepared']['tickets'], child['prepared']['launched']))
        self.native_context = child['context']
        self.account_path = child['root']/'a.sock'
        self.listener = self.listen_account()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.api = AccountIngress(self.fixture.service, AccountIngressLimits(4, 1, 5, 4, 10_000_000_000, 1_000_000))
        self.api.bootstrap()
        self.addCleanup(self.close_account)
        self.fixture.account = self.original_account_call
        before = self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0]
        for version in (None, 1, 2):
            if version is not None:
                self.hint(child, original, version=version)
            changed = dict(self.base.fixture.assignment, objective='Different original objective') if version == 2 else None
            code, refused = self.invoke_open(original, assigned=changed)
            self.assertEqual(2, code)
            self.assertFalse(refused['ready'])
            self.assertEqual('history_not_authorized' if version == 2 else 'open_unavailable', refused['reason_code'])
            self.assertEqual(before, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
            self.assertFalse((child['root']/'launches').exists())
        code, wrong_host = self.invoke_open(original, host='claude' if self.parent_host == 'codex' else 'codex')
        self.assertEqual(2, code)
        self.assertEqual('host_client_mismatch', wrong_host['reason_code'])
        self.assertEqual(before, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
        code, backend = self.invoke_open(original, host='claude', metered_backend=True)
        self.assertEqual(2, code)
        self.assertEqual('claude_backend_not_subscription', backend['reason_code'])
        self.assertEqual(before, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
        code, first = self.invoke_open(original)
        self.assertEqual(0, code, first)
        self.assertTrue(first['ready'])
        self.assertFalse(first['known'])
        self.assertEqual(('opened', 'admitted', 'advisory'),
                         (first['state'], first['reason_code'], first['read_containment']))
        code, known = self.invoke_open(original)
        self.assertEqual(0, code, known)
        self.assertEqual({**first, 'known': True}, known)
        self.assertEqual(before+1, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
        self.assertFalse((child['root']/'launches').exists())
        self.opened = first
        return {name: value for name, value in first.items() if name not in {'ready', 'read_containment'}}

    def run_original_workflow(self, parent_host):
        self.parent_host = parent_host
        self.fixture.primary_client = parent_host+'-host-v1'
        self.fixture.open_prepared_client = self.open_prepared_client
        child = self.fixture.start(self.fixture.fixture.scope, self.fixture.fixture.request,
            self.fixture.fixture.directory, 'open-rpc', with_provenance=True, client_dispatch=True, client_open=True)
        self.assertEqual(self.opened['run_handle'], child['scope'].run_handle)
        self.assertEqual(0, child['dispatch_ready']['tickets'])
        # Only the account routing socket disappears. Original services and
        # native enrollment remain synthetic; no crash/recovery proof claimed.
        self.listener.close()
        self.account_path.unlink()
        process = self.launch_cli(child, install_hint=False, host=parent_host)
        self.fixture.read(child, 'started')
        self.listener = self.listen_account()
        self.fixture.control(child, 'project.cancel')
        done = self.fixture.accept_terminal(child)
        output, errors = process.communicate(timeout=12)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertTrue(dispatched['ready'])
        self.assertEqual('cancelled', dispatched['result']['execution_state'])
        self.assertEqual(['launch'], done['launches'])
        self.assertEqual(1, done['ticket_count'])
        self.fixture.send(child, {'client_done': True})
        self.assertEqual(0, child['process'].wait(timeout=10))

    def test_codex_primary_fresh_open_and_dispatch_use_original_apis_without_database_or_collector_takeover(self):
        self.run_original_workflow('codex')

    def test_claude_primary_fresh_open_and_dispatch_use_original_apis_without_database_or_collector_takeover(self):
        self.run_original_workflow('claude')
