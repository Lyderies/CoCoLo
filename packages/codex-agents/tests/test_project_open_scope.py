"""Original pre-open project handoff; native/provider enrollment is synthetic."""
from tests.support import SHORT_TMP
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_project_worker_result as fixtures
from tests.test_bridge import PARENT_GRANT

from codex_agents import history_api
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_result_contracts import HostResultContext
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.learning_engine import installed_execution_configuration
from codex_agents.open_scope_contracts import OpenScopeContext, open_inputs_digest, verify_open_scope
from codex_agents.orchestration_api import OrchestrationScope, SCOPE_FIELDS
from codex_agents.service_transport import ServiceTransportError


class ProjectOpenScopeTests(unittest.TestCase):
    def setUp(self):
        self.worker = fixtures.ProjectWorkerResultTests(methodName='runTest')
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        self.store, self.binding = self.worker.store, self.worker.binding
        self.ledger = self.worker.base.base.ledger
        self.scope = OrchestrationScope(**{n: getattr(self.binding, n) for n in SCOPE_FIELDS})
        self.task = deepcopy(self.worker.task)
        self.route = deepcopy(self.worker.route)
        self.route.update(task_id=self.task['id'], role='codex_agents_verification_worker', verification_required=False, verifier=None)
        self.bridge = self.worker.fixture.base.fixture.fixture
        self.route['execution_configuration'] = installed_execution_configuration(
            self.bridge.bundle, self.route, client_capability=self.bridge.clients['bridge-v1'])
        self.assignment = deepcopy(self.worker.fixture.assigned)
        self.assignment.update(assignment_id='prospective-verifier', task_id=self.task['id'],
            role=self.route['role'], context=[self.worker.digest], verification_required=False)
        worker_context = HostResultContext(self.binding, self.worker.fixture.assigned, self.worker.original_task,
            self.worker.route, self.bridge.clients['bridge-v1'])
        self.context = OpenScopeContext(self.scope, self.binding.orchestration_handle,
            self.assignment, self.task, self.route, deepcopy(PARENT_GRANT), worker_context)
        self.token = object()
        self.factory = mock.Mock(return_value=self.store)
        self.service = self.make_service()

    def authorize(self, command, token):
        if token is not self.token:
            return None
        return HistoryAuthorization(command.project_id, command.runtime_digest, history_request_digest(command),
            'host', frozenset({'history.open_scope'}), open_scope_context=self.context)

    def make_service(self, **options):
        service = HistoryService(self.scope.project_id, self.scope.runtime_digest, self.factory,
            authorizer=options.get('authorizer', self.authorize))
        self.addCleanup(service.close)
        return service

    def payload(self):
        return {'scope': self.scope.as_data(), 'orchestration_handle': self.binding.orchestration_handle,
            'inputs_digest': open_inputs_digest(self.assignment, self.task, self.route, self.context.parent_grant)}

    def command(self, payload=None):
        payload = self.payload() if payload is None else payload
        return HistoryRequest(1, 'history.open_scope', hashlib.sha256(canonical_json(payload)).hexdigest(),
            self.scope.project_id, self.scope.runtime_digest, payload)

    def call(self, service=None):
        command = self.command()
        return decode_history_response((service or self.service).handle(encode_history_request(command), self.token), command)

    def test_actual_project_originals_supply_preopen_proof_without_account_reads_or_writes(self):
        before = self.ledger._connection.total_changes
        counts = {t: self.ledger._connection.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
            for t in ('host_run_bindings', 'orchestrations', 'records', 'active_run_capacity')}
        with mock.patch.object(self.ledger, 'verification_worker_result', side_effect=AssertionError('central history read')):
            value = self.call()
        self.assertEqual(self.worker.digest, value['facts']['worker']['host_outcome_digest'])
        self.assertEqual(self.assignment['assignment_id'], value['facts']['assignment_id'])
        self.assertEqual(value, verify_open_scope(self.worker.fixture.key, self.scope,
            self.binding.orchestration_handle, self.payload()['inputs_digest'], value))
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(counts, {t: self.ledger._connection.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in counts})
        self.assertNotIn(b'PRIVATE-WORKER-RESULT-CANARY', canonical_json(value))
        self.assertNotIn(b'objective', canonical_json(value))

    def test_new_worker_scope_needs_no_private_result_and_creates_no_clock_or_run(self):
        self.context = OpenScopeContext(self.scope, self.binding.orchestration_handle,
            self.worker.fixture.assigned, self.worker.original_task, self.worker.route, deepcopy(PARENT_GRANT))
        self.assignment, self.task, self.route = self.context.assignment, self.context.task, self.context.route
        before = self.store._connection.total_changes
        with (mock.patch.object(self.store, 'read_result', side_effect=AssertionError('read unrelated result')),
                mock.patch.object(self.store, '_result_clock', side_effect=AssertionError('new clock'))):
            value = self.call()
        self.assertIsNone(value['facts']['worker'])
        self.assertEqual(before, self.store._connection.total_changes)

    def test_default_denial_and_foreign_scope_refuse_before_writer(self):
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call(self.make_service(authorizer=None))
        self.context = replace(self.context, scope=replace(self.scope, primary_session_id='foreign'))
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()
        self.factory.assert_not_called()

    def test_wire_inputs_active_flags_reports_and_worker_bodies_cannot_be_substituted(self):
        for name, value in (('assignment', self.assignment), ('worker', {}), ('active', True), ('report', {'passed': True})):
            with self.subTest(name=name), self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                self.command({**self.payload(), name: value})
        self.factory.assert_not_called()

    def test_changed_input_digest_refuses_before_writer(self):
        self.assignment = {**self.assignment, 'objective': 'different input'}
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()
        self.factory.assert_not_called()

    def test_exact_worker_inputs_provider_family_role_and_criteria_are_required(self):
        original = self.context
        changes = [
            replace(original, worker=None),
            replace(original, worker=replace(original.worker, assignment={**original.worker.assignment, 'objective': 'changed'})),
            replace(original, worker=replace(original.worker, client_capability={**original.worker.client_capability, 'changed': True})),
            replace(original, worker=replace(original.worker, binding=replace(self.binding, primary_session_id='foreign'))),
        ]
        for changed in changes:
            self.context = changed
            with self.subTest(context=changed), self.assertRaises(HistoryError):
                self.call()
        self.context = original
        for name, value in (('verification_for_provider', 'claude'), ('verification_for_family', 'coding')):
            task = {**original.task, name: value}
            self.context = replace(original, task=task)
            self.task = task
            with self.subTest(name=name), self.assertRaises(HistoryError):
                self.call()
        self.task = original.task
        for name, value in (('assignment_id', original.worker.assignment['assignment_id']),
                            ('context', ['a'*64]), ('required_evidence', [])):
            assignment = {**original.assignment, name: value}
            self.context = replace(original, assignment=assignment)
            self.assignment = assignment
            with self.subTest(name=name), self.assertRaises(HistoryError):
                self.call()

    def test_missing_owned_journal_cannot_use_legacy_reference_exception(self):
        self.store._allow_legacy_orchestration_reference = True
        for table in ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations'):
            self.store._connection.execute('DELETE FROM '+table)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_unavailable'):
            self.call()

    def test_prospective_bounds_attempt_and_read_only_permissions_cannot_widen(self):
        original = self.context
        candidates = [
            {**original.assignment, 'bounds': {**original.assignment['bounds'],
                'timeout_seconds': original.route['bounds']['timeout_seconds'] + 1}},
            {**original.assignment, 'attempt': 2, 'retry_of_assignment_id': 'earlier-unrecorded'},
            {**original.assignment, 'permissions': {**original.assignment['permissions'], 'filesystem': ['docs:write']}},
            {**original.assignment, 'meaningful': not original.task['meaningful']},
        ]
        for index, assignment in enumerate(candidates):
            self.context = replace(original, assignment=assignment)
            self.assignment = assignment
            with self.subTest(index=index), self.assertRaises(HistoryError):
                self.call()

    def test_independently_held_parent_grant_cannot_be_inferred_from_assignment(self):
        original = self.context
        for name in ('tools', 'filesystem'):
            self.context = replace(original, parent_grant={**original.parent_grant, name: []})
            with self.subTest(name=name), self.assertRaisesRegex(HistoryError, 'history_conflict'):
                self.call()

    def test_write_capable_route_stays_out_of_the_codex_030_handoff(self):
        route = {**self.route, 'sandbox_mode': 'workspace-write'}
        self.context = replace(self.context, route=route)
        self.route = route
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call()

    def test_missing_worker_companion_never_reconstructs_or_opens_verifier(self):
        self.store._connection.execute('DELETE FROM project_host_opens')
        before = self.ledger._connection.total_changes
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call()
        self.assertEqual(before, self.ledger._connection.total_changes)

    def test_missing_original_body_refuses_even_with_intact_metadata(self):
        with mock.patch.object(self.store, 'read_result', side_effect=HistoryError('history_result_not_ready')):
            with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
                self.call()

    def test_revocation_after_original_body_io_prevents_publication(self):
        calls = []
        def authorize(command, token):
            calls.append(True)
            return self.authorize(command, token) if len(calls) == 1 else None
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call(self.make_service(authorizer=authorize))
        self.assertEqual(2, len(calls))

    def test_changed_protected_inputs_after_body_io_prevent_publication(self):
        read = self.store.read_result
        def mutate(binding):
            body = read(binding)
            self.context.task['objective'] = 'changed after protected read'
            return body
        with mock.patch.object(self.store, 'read_result', side_effect=mutate), self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()

    def test_pause_during_reply_encoding_prevents_stale_active_proof(self):
        encode = self.service._reply
        def pause(*args, **kwargs):
            raw = encode(*args, **kwargs)
            if kwargs.get('result') is not None:
                self.worker.pause()
            return raw
        with mock.patch.object(self.service, '_reply', side_effect=pause), self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call()

    def test_expiry_during_reply_encoding_prevents_stale_worker_proof(self):
        encode = self.service._reply
        def expire(*args, **kwargs):
            raw = encode(*args, **kwargs)
            if kwargs.get('result') is not None:
                self.worker.fixture.base.stamp = kwargs['result']['facts']['worker']['expires_at_ms']
            return raw
        with mock.patch.object(self.service, '_reply', side_effect=expire), self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.call()

    def test_closed_writer_during_authorization_denies_before_original_read(self):
        def close(command, token):
            answer = self.authorize(command, token)
            self.service.close()
            return answer
        self.service._authorizer = close
        with mock.patch.object(self.store, 'read_result') as reader, self.assertRaisesRegex(HistoryError, 'history_wrong_owner'):
            self.call()
        reader.assert_not_called()

    def test_closed_reply_checks_nested_bool_integer_and_mac_owner_binding(self):
        command = self.command()
        reply = parse_bounded_json(self.service.handle(encode_history_request(command), self.token))
        for location, name, value in (('facts', 'sequence', True), ('facts', 'attempt', True),
                ('scope', 'supervisor_generation', True), ('binding', 'supervisor_generation', True), ('facts', 'active', True)):
            changed = deepcopy(reply)
            target = changed['result']['facts']
            if location == 'scope':
                target = target['scope']
            elif location == 'binding':
                target = target['worker']['binding']
            target[name] = value
            with self.subTest(name=name, location=location), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json(changed), command)
        changed = deepcopy(reply['result'])
        changed['seal'] = 'a'*64
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            verify_open_scope(self.worker.fixture.key, self.scope, self.binding.orchestration_handle,
                self.payload()['inputs_digest'], changed)

    def test_client_performs_one_bounded_exchange_without_retry_or_fallback(self):
        client = HistoryClient(Path('/private/tmp/fixture-open-scope.sock'), self.scope.project_id, self.scope.runtime_digest, timeout=1)
        with mock.patch.object(history_api, 'exchange_frame', side_effect=lambda path, raw, **kw: self.service.handle(raw, self.token)) as exchange:
            self.assertEqual(self.call(), client.original_open_scope(self.scope, self.binding.orchestration_handle, self.payload()['inputs_digest']))
        self.assertEqual(1, exchange.call_count)
        self.assertEqual(16_384, exchange.call_args.kwargs['response_limit'])
        with mock.patch.object(history_api, 'exchange_frame', side_effect=ServiceTransportError('transport_unavailable')) as exchange:
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                client.original_open_scope(self.scope, self.binding.orchestration_handle, self.payload()['inputs_digest'])
        self.assertEqual(1, exchange.call_count)

    def test_actual_unix_client_process_has_no_sqlite_import_or_connection(self):
        code = '''import importlib.abc,json,pathlib,sys
class NoSql(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in ('sqlite3','_sqlite3'): raise AssertionError('client reached SQL')
sys.meta_path.insert(0,NoSql()); sys.path.insert(0,sys.argv[1])
from codex_agents.history_api import HistoryClient
from codex_agents.orchestration_api import OrchestrationScope
p=json.loads(sys.argv[3]); c=HistoryClient(pathlib.Path(sys.argv[2]),p['scope']['project_id'],p['scope']['runtime_digest'],timeout=3)
v=c.original_open_scope(OrchestrationScope(**p['scope']),p['orchestration_handle'],p['inputs_digest'])
assert 'sqlite3' not in sys.modules and '_sqlite3' not in sys.modules
print(json.dumps(v))
'''
        with tempfile.TemporaryDirectory(prefix='opn-', dir=SHORT_TMP) as directory:
            path = Path(directory)/'h.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                server.settimeout(5)
                process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', code,
                    str(ORCHESTRATION_ROOT), str(path), json.dumps(self.payload())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(5)
                        raw = bytearray()
                        while not raw.endswith(b'\n'):
                            piece = connection.recv(4096)
                            self.assertTrue(piece)
                            raw.extend(piece)
                            self.assertLessEqual(len(raw), 8192)
                        connection.sendall(self.service.handle(bytes(raw[:-1]), self.token)+b'\n')
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(0, process.returncode, stderr.decode())
                    self.assertEqual(self.call(), json.loads(stdout))
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
