"""Fresh CLI -> original project/account APIs; native enrollment is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests import test_dispatch_client_runtime as dispatch_fixtures
from tests.test_bridge import PARENT_GRANT

from codex_agents.account_api import decode_account_response, encode_account_request
from codex_agents import bridge, providers
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

    @classmethod
    def setUpClass(cls):
        # TESTSPEED-4: the fresh CLIs share one bytecode cache outside the source tree, so only the first compiles.
        cls.bytecode = tempfile.mkdtemp(prefix='cli-pyc-')
        cls.addClassCleanup(shutil.rmtree, cls.bytecode, ignore_errors=True)

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
        env['COCOLO_TEST_RUN_CHECK_READY'] = '1'  # decision 363: this fixture has no run driver; see dispatch_cli_fixture
        env['COCOLO_TEST_ENDED_SERVICE_STARTS'] = '1'  # TESTSPEED-3: no service runs here, so its start ends at once
        # Decision 363: the open's `run.check` needs the run driver's endpoint to exist and answer. This fixture has no
        # driver, so the file is present only while the open runs (later dispatch behaves differently when it exists).
        driver_endpoint = self.account_path.parent/'run.sock'
        driver_endpoint.touch()
        self.addCleanup(lambda: driver_endpoint.unlink(missing_ok=True))
        process = subprocess.Popen([sys.executable, '-X', 'pycache_prefix=' + self.bytecode, '-m', 'tests.dispatch_cli_fixture',
            'learning-open-host-run',
            '--project-root', str(self.bridge.project), '--state-root', str(self.bridge.state.root),
            '--orchestration-handle', original.prospective.orchestration_handle, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        self.wait_api(lambda: process.poll() is not None)
        output, errors = process.communicate(timeout=2)
        driver_endpoint.unlink(missing_ok=True)
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
            started = time.monotonic()
            code, refused = self.invoke_open(original, assigned=changed)
            took = round(time.monotonic() - started, 1)  # On GitHub only, this answered unavailable (0.4.0 O5).
            self.assertEqual(2, code)
            self.assertFalse(refused['ready'])
            # S1-U10c: no hint, the project's service is not running; a version-1 hint, an earlier version's.
            expected = {None: 'open_service_not_running', 1: 'open_service_other_version', 2: 'history_not_authorized'}
            self.assertEqual(expected[version], refused.get('reason_code'),
                             {**refused, 'seconds': took, 'version': version})
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

    def invoke_record(self, child, dispatched, *, body=None, route=True, host=None, terminal='cancelled', inputs=None):
        values = {'task': self.bridge.task(), 'route': self.base.fixture.route,
            'assignment': self.base.fixture.assignment, 'parent-grant': PARENT_GRANT,
            'result': dispatched['result'] if body is None else body,
            'observation': {'run_handle': child['scope'].run_handle, 'terminal_state': terminal,
                'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]}}
        values.update(inputs or {})
        if not route:
            del values['route']
        args = []
        for flag, value in values.items():
            path = self.bridge.state.root/('record-'+flag+'.json')
            path.write_bytes(canonical_json(value))
            args.extend(['--'+flag, str(path)])
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID'):
            env.pop(marker, None)
        if (host or self.parent_host) == 'codex':
            env['CODEX_THREAD_ID'] = 'fixture-result-primary'
        else:
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='fixture-result-primary')
        process = subprocess.Popen([sys.executable, '-X', 'pycache_prefix=' + self.bytecode, '-m', 'tests.dispatch_cli_fixture',
            'learning-record-host-result',
            '--project-root', str(self.bridge.project), '--state-root', str(self.bridge.state.root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        self.wait_api(lambda: process.poll() is not None)
        output, errors = process.communicate(timeout=2)
        self.assertEqual(b'', errors)
        return process.returncode, json.loads(output)

    def record_original_result(self, child, dispatched):
        before = self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0]
        wrong_body = dict(dispatched['result'], summary='PRIVATE-CHANGED-RESULT-CANARY')
        changed_assignment = dict(self.base.fixture.assignment, objective='Different original objective')
        for options, reason in (({'route': False}, 'history_invalid_input'),
                ({'body': wrong_body}, 'recheck_result_mismatch'),  # B2-5 (119 A)
                ({'terminal': None}, 'history_conflict'),
                ({'host': 'claude' if self.parent_host == 'codex' else 'codex'}, 'host_client_mismatch'),
                ({'inputs': {'assignment': changed_assignment}}, 'recheck_inputs_mismatch')):  # B2-5 (119 A)
            code, refused = self.invoke_record(child, dispatched, **options)
            self.assertEqual(2, code, refused)
            self.assertEqual(reason, refused['reason_code'])
            self.assertNotIn('PRIVATE-CHANGED-RESULT-CANARY', json.dumps(refused))
        code, lost = self.invoke_record(child, dispatched)
        self.assertEqual(2, code, lost)
        self.assertEqual('history_service_unavailable', lost['reason_code'])
        code, recorded = self.invoke_record(child, dispatched)
        self.assertEqual(0, code, recorded)
        self.assertTrue(recorded['ready'])
        self.assertEqual(recorded['host_outcome_digest'], recorded['outcome_commit']['content_digest'])
        self.assertEqual('retained', recorded['capacity_state'])
        self.assertFalse(recorded['outcome']['accepted'])
        code, known = self.invoke_record(child, dispatched)
        self.assertEqual((0, recorded), (code, known))
        self.assertEqual(before, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
        self.assertEqual(['launch'], (child['root']/'launches').read_text().splitlines())

    def run_original_workflow(self, parent_host):
        self.parent_host = parent_host
        self.fixture.primary_client = parent_host+'-host-v1'
        self.fixture.open_prepared_client = self.open_prepared_client
        child = self.fixture.start(self.fixture.fixture.scope, self.fixture.fixture.request,
            self.fixture.fixture.directory, 'open-rpc', with_provenance=True, client_dispatch=True, client_open=True,
            product_runtime=True, client_result=getattr(self, 'record_results', False),
            lose_result_reply=getattr(self, 'record_results', False), actual_preparation=True)
        self.assertEqual(self.opened['run_handle'], child['scope'].run_handle)
        self.assertEqual(0, child['dispatch_ready']['tickets'])
        manifest = bridge.committed_export_manifest(self.bridge.project, ['source.txt'])
        self.assertEqual([manifest['file_count'], manifest['commit'], manifest['manifest_digest']], child['prepared']['export_info'])
        self.assertEqual(list(providers.resolve_client('codex', untrusted=[self.bridge.project])), child['prepared']['client_identity'])
        if getattr(self, 'record_results', False):
            code, missing = self.invoke_record(child, {'result': {}})
            self.assertEqual((2, 'dispatch_not_found'), (code, missing['reason_code']))
            self.assertFalse((child['root']/'launches').exists())
        # Only the account routing socket disappears. Original services and
        # native enrollment remain synthetic; no crash/recovery proof claimed.
        self.listener.close()
        self.account_path.unlink()
        process = self.launch_cli(child, install_hint=False, host=parent_host, expected_export_commit=manifest['commit'])
        self.fixture.read(child, 'started')
        self.listener = self.listen_account()
        self.fixture.control(child, 'project.cancel')
        done = self.fixture.accept_terminal(child)
        output, errors = process.communicate(timeout=12)
        self.assertEqual(0, process.returncode, errors.decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertTrue(dispatched['ready'])
        self.assertEqual('cancelled', dispatched['result']['execution_state'])
        self.assertEqual((manifest['file_count'], manifest['commit'], manifest['manifest_digest']),
            tuple(dispatched['bridge_report'][name] for name in ('export_file_count', 'export_commit', 'export_manifest_digest')))
        self.assertEqual(['launch'], done['launches'])
        self.assertEqual(1, done['ticket_count'])
        if getattr(self, 'record_results', False):
            self.record_original_result(child, dispatched)
        self.fixture.send(child, {'client_done': True})
        if getattr(self, 'record_results', False):
            reconciled = self.fixture.read(child, 'result_reconciled')
            self.assertEqual((3, True), (reconciled['attempts'], reconciled['identical']))
        self.assertEqual(0, child['process'].wait(timeout=10))

    @__import__('test_framework').requirements('TESTSPEED-1', 'TESTSPEED-3', 'TESTSPEED-4', why_not_scenario='Test-speed requirement: these are existing tests whose seconds and fixture setup were changed; the requirement is how fast and how cheaply they run, not a new outcome (TESTSPEED).')
    def test_codex_primary_fresh_open_and_dispatch_use_original_apis_without_database_or_collector_takeover(self):
        self.run_original_workflow('codex')

    @__import__('test_framework').requirements('B2-OUTCOME', why_not_scenario='Real CLI and service boundaries check stored-result recheck refusals, immediate recording or orchestration counts after recovery (B2, decision 119 A).')
    def test_codex_primary_records_exact_original_terminal_through_project_api(self):
        self.record_results = True
        self.run_original_workflow('codex')

    @__import__('test_framework').requirements('B2-OUTCOME', why_not_scenario='Real CLI and service boundaries check stored-result recheck refusals, immediate recording or orchestration counts after recovery (B2, decision 119 A).')
    def test_claude_primary_records_exact_original_codex_terminal_through_project_api(self):
        self.record_results = True
        self.run_original_workflow('claude')

    @__import__('test_framework').requirements('TESTSPEED-1', 'TESTSPEED-3', 'TESTSPEED-4', why_not_scenario='Test-speed requirement: these are existing tests whose seconds and fixture setup were changed; the requirement is how fast and how cheaply they run, not a new outcome (TESTSPEED).')
    def test_claude_primary_fresh_open_and_dispatch_use_original_apis_without_database_or_collector_takeover(self):
        self.run_original_workflow('claude')
