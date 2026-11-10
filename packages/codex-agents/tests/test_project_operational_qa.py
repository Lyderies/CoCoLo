"""Project-local original QA pair; fake native/provider and open-dependency seam.

Both claims, registrations, original account proofs and project returns are real
disposable persistence operations. The legacy account opener's central worker
lookup is explicitly replaced by the actual project lookup in this fixture only;
production account-open API integration and native identity remain unproved.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_project_worker_result as fixtures
from tests.test_bridge import result

from codex_agents import history_api
from codex_agents.account_api import AccountRequest, account_request_digest
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.execution_grant import ExecutionRegistrationInput, checked_execution_grant
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultContext, HostResultReference, OperationalQAContext, WorkerResultContext
from codex_agents.learning_contracts import canonical_json, content_digest, external_id, parse_bounded_json
from codex_agents.learning_engine import installed_execution_configuration
from codex_agents.orchestration_api import OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultObservation
from codex_agents.run_provenance import RunProvenanceInput
from codex_agents.service_transport import ServiceTransportError


class ProjectOperationalQATests(unittest.TestCase):
    verifier_role = 'codex_agents_verification_worker'

    def setUp(self):
        self.worker = fixtures.ProjectWorkerResultTests(methodName='runTest')
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        self.store, self.binding = self.worker.store, self.worker.binding
        self.ledger = self.worker.base.base.ledger
        self.fixture = self.worker.fixture
        self.bridge = self.fixture.base.fixture.fixture
        self.worker_digest = self.worker.digest
        self.worker_context = HostResultContext(self.binding, self.fixture.assigned, self.worker.original_task,
            self.worker.route, self.bridge.clients['bridge-v1'])
        self.make_verifier()
        self.context = OperationalQAContext(self.worker_context, self.verifier_context)
        self.factory = mock.Mock(return_value=self.store)
        self.token = object()
        self.service = self.make_service()
        self.client = HistoryClient(Path('/private/tmp/project-qa.sock'), self.binding.project_id,
                                    self.binding.runtime_digest, timeout=1)

    def make_verifier(self):
        self.verifier_task = deepcopy(self.worker.task)
        route = deepcopy(self.worker.route)
        route['role'] = self.verifier_role
        route['execution_configuration'] = installed_execution_configuration(
            self.bridge.bundle, route, client_capability=self.bridge.clients['bridge-v1'])
        assignment = deepcopy(self.fixture.assigned)
        assignment.update(assignment_id='qa-verifier-original', task_id=self.verifier_task['id'],
            role=route['role'], model=route['model'], effort=route['effort'],
            context=[self.worker_digest], verification_required=False)

        def project_dependency(project, task, **options):
            self.assertEqual(self.binding.project_id, project)
            self.assertEqual(self.binding.orchestration_handle, options['orchestration_handle'])
            self.assertFalse(options['with_result'])
            return self.store.verification_worker_reference(WorkerResultContext(self.binding, task),
                self.worker_digest, before_reply=lambda: None)

        with mock.patch.object(self.ledger, 'verification_worker_result', side_effect=project_dependency) as seam:
            opened = self.ledger.open_host_run(self.binding.project_id, assignment=assignment,
                task=self.verifier_task, route=route, execution_configuration=route['execution_configuration'],
                client_version='bridge-v1', orchestration_handle=self.binding.orchestration_handle,
                now_ms=self.fixture.base.stamp)
            seam.assert_called_once()
        identity = self.ledger.observe_provider(self.binding.project_id, 'codex', project_root=self.bridge.tools,
            environ={}, run_handle=opened['run_handle'], phase='before')[0]
        launch = LaunchClaimInput(opened['run_handle'], content_digest(identity))
        claim_request = AccountRequest(1, 'account.claim_launch', 'qa-verifier-claim', self.binding.project_id,
            self.binding.runtime_digest, launch.payload())
        scope = replace(self.fixture.scope, run_handle=opened['run_handle'],
            run_id=external_id(self.fixture.key, 'run', opened['run_handle']),
            claim_request_id=claim_request.request_id, claim_request_digest=account_request_digest(claim_request))
        self.store.reserve_result_lease(scope, assignment)
        self.store.arm_result_lease(scope)
        pending = self.store.attest_pending_result_lease(scope)
        request = OpenProvenanceInput(scope.run_handle, assignment['assignment_id'], self.worker_digest)
        proof = self.ledger._attest_open_provenance(scope.project_id, runtime_digest=scope.runtime_digest,
            caller_scope='a'*64, request=request, storage_binding=scope, storage_evidence=pending,
            work_limits=self.worker.base.limits)
        self.verifier_open = self.store.record_host_open(scope, request, proof, assignment=assignment,
            task=self.verifier_task, route=route, before_commit=lambda: None)
        receipt = self.ledger.claim_launch(scope.project_id, runtime_digest=scope.runtime_digest,
            caller_scope='a'*64, request_id=scope.claim_request_id, launch=launch, storage_required=True,
            storage_binding=scope, storage_evidence=pending)
        self.assertEqual('claimed', receipt['state'])
        binding = AttemptBinding(**{name: getattr(scope, name) for name in LEASE_SCOPE_FIELDS},
            claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
        self.store.bind_result_lease(scope, binding, assignment, hard_deadline_at_ms=receipt['deadline_at_ms'])
        evidence = self.store.attest_bound_result_lease(scope)
        registration = ExecutionRegistrationInput(scope.run_handle, binding.claim_id, binding.claim_digest,
            scope.claim_request_id, launch.identity_digest)
        grant = checked_execution_grant(self.ledger._register_project_execution(scope.project_id,
            runtime_digest=scope.runtime_digest, caller_scope='a'*64, registration=registration,
            storage_binding=scope, storage_evidence=evidence)['grant'])
        proof = self.ledger._attest_run_provenance(scope.project_id, runtime_digest=scope.runtime_digest,
            caller_scope='a'*64, request=RunProvenanceInput(registration, grant.registration_digest,
                assignment['assignment_id']), storage_binding=scope, storage_evidence=evidence,
            work_limits=self.worker.base.limits)
        self.store.consume_execution_grant(binding, grant.encode(), task=self.verifier_task, route=route, provenance=proof)
        self.verifier_body = canonical_json(self.verifier_result(assignment))
        delivered = self.store.publish_result(binding, assignment, self.verifier_body,
            ResultObservation(binding.claim_id, self.fixture.base.stamp, False, True, None))
        admitted = self.store.admit_host_result(HostResultReference(binding, delivered.record_id,
            delivered.record_content_digest, delivered.result_digest), assignment=assignment,
            task=self.verifier_task, route=route, client_capability=self.bridge.clients['bridge-v1'], before_commit=lambda: None)
        self.verifier_digest = admitted['admission']['host_outcome_digest']
        self.verifier_binding = binding
        self.verifier_context = HostResultContext(binding, assignment, self.verifier_task, route,
                                                 self.bridge.clients['bridge-v1'])

    def verifier_result(self, assignment):
        return {**result(), 'assignment_id': assignment['assignment_id'], 'summary': 'PRIVATE-ORIGINAL-VERIFIER'}

    def authorize(self, command, token):
        if token is not self.token:
            return None
        return HistoryAuthorization(command.project_id, command.runtime_digest, history_request_digest(command),
            'host', frozenset({'history.qa_scope'}), operational_qa_context=self.context)

    def make_service(self, authorizer=None):
        service = HistoryService(self.binding.project_id, self.binding.runtime_digest, self.factory,
            authorizer=authorizer or self.authorize)
        self.addCleanup(service.close)
        return service

    def command(self, **changes):
        payload = {'worker_binding': self.binding.as_data(), 'verifier_binding': self.verifier_binding.as_data(),
            'host_outcome_digest': self.worker_digest, 'verifier_host_outcome_digest': self.verifier_digest, **changes}
        return HistoryRequest(1, 'history.qa_scope', hashlib.sha256(canonical_json(payload)).hexdigest(),
            self.binding.project_id, self.binding.runtime_digest, payload)

    def call(self, *, service=None):
        command = self.command()
        return decode_history_response((service or self.service).handle(encode_history_request(command), self.token), command)

    def test_two_original_registered_returns_resolve_without_account_outcome_copy_or_new_claim(self):
        before = self.ledger._connection.total_changes
        with mock.patch.object(history_api, 'exchange_frame', side_effect=lambda path, raw, **opts:
                self.service.handle(raw, self.token)) as transport:
            value = self.client.operational_qa_scope(self.binding, self.verifier_binding,
                self.worker_digest, self.verifier_digest)
        transport.assert_called_once()
        self.assertEqual(self.worker_digest, value['worker']['host_outcome_digest'])
        self.assertEqual(self.verifier_digest, value['verifier']['host_outcome_digest'])
        self.assertEqual('verification', value['verifier']['task_family'])
        self.assertNotIn(b'PRIVATE-', canonical_json(value))
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='outcome'").fetchone()[0])
        self.assertEqual(2, self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_executions').fetchone()[0])
        self.assertEqual(2, self.store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        self.assertEqual(value, self.call())

    def test_default_denial_and_wrong_independent_original_scope_precede_open(self):
        service = HistoryService(self.binding.project_id, self.binding.runtime_digest, self.factory)
        self.addCleanup(service.close)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call(service=service)
        self.context = replace(self.context, verifier=replace(self.verifier_context,
            binding=replace(self.verifier_binding, primary_session_id='foreign')))
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()
        self.factory.assert_not_called()

    def test_caller_cannot_supply_assignments_results_checks_or_accepted_flags(self):
        for field, value in (('assignment', {}), ('worker_result', {}), ('check_report', {}), ('accepted', True), ('path', '/private/tmp/foreign')):
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                self.command(**{field: value})
        self.factory.assert_not_called()

    def test_same_registered_return_cannot_act_as_its_own_verifier(self):
        self.context = OperationalQAContext(self.worker_context, self.worker_context)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.operational_qa_scope(self.context, self.worker_digest, self.worker_digest, before_reply=lambda: None)

    def test_changed_original_assignment_task_route_or_capability_is_not_replacement_authority(self):
        for field, key, value in (('assignment', 'objective', 'changed'), ('task', 'verification_for_outcome', 'f'*64),
                                  ('route', 'model', 'gpt-6-astra'), ('client_capability', 'version', 'changed')):
            original = deepcopy(getattr(self.verifier_context, field))
            original[key] = value
            self.context = OperationalQAContext(self.worker_context, replace(self.verifier_context, **{field: original}))
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, 'history_conflict'):
                self.call()

    def test_missing_original_verifier_open_or_registration_never_reconstructs(self):
        self.store._connection.execute('DELETE FROM project_host_opens WHERE run_handle=?', (self.verifier_binding.run_handle,))
        with self.assertRaises(HistoryError):
            self.call()
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_missing_consumed_original_companion_refuses_even_in_legacy_mode(self):
        self.store._allow_legacy_orchestration_reference = True
        self.store._connection.execute("DELETE FROM records WHERE record_type='execution_provenance' "
            "AND json_extract(CAST(content AS TEXT),'$.run_id')=?",
            (self.verifier_binding.run_id,))
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            self.call()

    def pause(self):
        scope = OrchestrationScope(**{name: getattr(self.binding, name) for name in SCOPE_FIELDS})
        request = OrchestrationRequest(1, 'orchestration.pause', 'pause-qa-scope', scope,
            {'orchestration_handle': self.binding.orchestration_handle, 'expected_sequence': 1})
        OrchestrationOwner(self.store).apply(request, client_version='codex-host-v1', before_commit=lambda: None)

    def test_pause_after_both_private_reads_prevents_scope_reply(self):
        calls = []
        read = self.store.read_result
        def pause_after_read(binding):
            value = read(binding)
            calls.append(binding)
            if len(calls) == 2:
                self.pause()
            return value
        with mock.patch.object(self.store, 'read_result', side_effect=pause_after_read), self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call()
        self.assertEqual([self.binding, self.verifier_binding], calls)

    def test_revocation_mutated_context_or_close_after_private_reads_returns_no_scope(self):
        for mode in ('revoke', 'mutate', 'close'):
            calls = []
            def authorize(command, token):
                value = self.authorize(command, token)
                calls.append(True)
                if len(calls) == 2:
                    if mode == 'revoke':
                        return None
                    if mode == 'mutate':
                        return replace(value, operational_qa_context=replace(self.context,
                            verifier=replace(self.verifier_context, assignment={**self.verifier_context.assignment, 'objective': 'changed'})))
                    service.close()
                return value
            service = self.make_service(authorize)
            with self.subTest(mode=mode), self.assertRaises(HistoryError):
                self.call(service=service)

    def test_missing_private_body_cannot_be_substituted(self):
        _, name = self.store._result_names(self.verifier_binding)
        (self.fixture.directory / name).unlink()
        with self.assertRaises(HistoryError):
            self.call()

    def test_unswept_expired_original_result_cannot_supply_qa_dependencies(self):
        until = self.store.result_reservation(self.verifier_binding).expires_at_ms
        self.fixture.base.stamp = until
        with self.assertRaises(HistoryError):
            self.call()

    def test_final_authorizer_cannot_remove_original_evidence_after_prepublication_checks(self):
        calls = []
        def authorize(command, token):
            value = self.authorize(command, token)
            calls.append(True)
            if len(calls) == 3:
                self.store._connection.execute('DELETE FROM project_host_opens WHERE run_handle=?',
                    (self.verifier_binding.run_handle,))
            return value
        with self.assertRaises(HistoryError):
            self.call(service=self.make_service(authorize))
        self.assertEqual(3, len(calls))

    def test_pause_during_reply_encoding_cannot_publish_success(self):
        reply = self.service._reply
        def encode(*args, **options):
            raw = reply(*args, **options)
            if options.get('result') is not None:
                self.pause()
            return raw
        with mock.patch.object(self.service, '_reply', side_effect=encode), self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call()

    def test_closed_response_rejects_swapped_digest_extra_body_and_coercing_size(self):
        command = self.command()
        original = parse_bounded_json(self.service.handle(encode_history_request(command), self.token))
        for field, value in (('host_outcome_digest', self.worker_digest), ('body_size', True),
                            ('binding', {**self.verifier_binding.as_data(), 'supervisor_generation': True}),
                            ('body', 'PRIVATE-CANARY')):
            changed = deepcopy(original)
            changed['result']['verifier'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json(changed), command)

    def test_lost_lookup_reply_is_one_bounded_exchange_without_automatic_resend(self):
        with mock.patch.object(history_api, 'exchange_frame', side_effect=ServiceTransportError('transport_unavailable')) as transport:
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                self.client.operational_qa_scope(self.binding, self.verifier_binding, self.worker_digest, self.verifier_digest)
        transport.assert_called_once()


class ProjectQAWrongVerifierRoleTests(unittest.TestCase):
    """An actually opened verification task with a non-verifier route is refused."""
    def setUp(self):
        self.base = ProjectOperationalQATests(methodName='runTest')
        self.base.verifier_role = 'codex_agents_researcher'
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def test_original_nonverification_route_does_not_gain_qa_authority_from_task_family(self):
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.base.call()


QA_SERVER = r'''
import json,socket,sys
from tests.test_project_operational_qa import ProjectOperationalQATests
fixture=ProjectOperationalQATests(methodName='runTest')
fixture.setUp()
try:
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.settimeout(15)
        listener.bind(sys.argv[1]); listener.listen(1)
        print(json.dumps({'endpoint':sys.argv[1],'project':fixture.binding.project_id,
            'runtime':fixture.binding.runtime_digest,'worker':fixture.binding.as_data(),
            'verifier':fixture.verifier_binding.as_data(),'worker_digest':fixture.worker_digest,
            'verifier_digest':fixture.verifier_digest}),flush=True)
        connection,_=listener.accept()
        with connection:
            connection.settimeout(5)
            data=bytearray()
            while not data.endswith(b'\n') and len(data)<16384:
                part=connection.recv(4096)
                if not part: raise RuntimeError('fixture_incomplete_request')
                data.extend(part)
            if not data.endswith(b'\n') or len(data)>16384: raise RuntimeError('fixture_invalid_frame')
            connection.sendall(fixture.service.handle(bytes(data[:-1]),fixture.token)+b'\n')
finally:
    fixture.doCleanups()
'''

QA_CLIENT = r'''
import json,sys
from pathlib import Path
class NoSql:
    def find_spec(self,fullname,path=None,target=None):
        if fullname in ('sqlite3','_sqlite3'): raise AssertionError('client imported SQLite')
sys.meta_path.insert(0,NoSql())
from codex_agents.history_api import HistoryClient
from codex_agents.history_contracts import AttemptBinding
v=json.load(sys.stdin)
client=HistoryClient(Path(v['endpoint']),v['project'],v['runtime'],timeout=5)
scope=client.operational_qa_scope(AttemptBinding(**v['worker']),AttemptBinding(**v['verifier']),
    v['worker_digest'],v['verifier_digest'])
print(json.dumps({'scope':scope,'sqlite_loaded':'sqlite3' in sys.modules}))
'''


class ProjectQASocketTests(unittest.TestCase):
    def test_real_separate_client_and_writer_processes_keep_sqlite_out_of_client(self):
        with tempfile.TemporaryDirectory(prefix='project-qa-', dir='/private/tmp') as temporary:
            endpoint = Path(temporary) / 'qa.sock'
            server_program = 'import sys;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ');' + QA_SERVER
            server = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', server_program, str(endpoint)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([server.stdout], [], [], 20)[0], 'fixture server did not become ready')
                line = server.stdout.readline()
                if not line:
                    self.fail(server.stderr.read(3000))
                bootstrap = json.loads(line)
                client_program = 'import sys;sys.path.insert(0,' + repr(str(ORCHESTRATION_ROOT)) + ');' + QA_CLIENT
                client = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', client_program],
                    input=line, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, client.returncode, client.stderr)
                result = json.loads(client.stdout)
                self.assertFalse(result['sqlite_loaded'])
                self.assertEqual(bootstrap['worker_digest'], result['scope']['worker']['host_outcome_digest'])
                self.assertEqual(bootstrap['verifier_digest'], result['scope']['verifier']['host_outcome_digest'])
                self.assertNotIn('PRIVATE-', client.stdout)
                self.assertEqual(0, server.wait(timeout=15), server.stderr.read(3000))
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=10)
                server.stdout.close()
                server.stderr.close()
