"""Protected project journal and API with fake native enrollment; no model calls."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_host_open_provenance as fixtures

from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_open_contracts import HostOpenContext, OpenProvenanceInput, seal_open_provenance
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.project_history import ProjectHistory
from codex_agents.service_transport import ServiceTransportError


def owned_orchestration(store, binding):
    # Explicitly fake enrollment links the original account handle to a real
    # sealed project journal. This is not native identity or migration evidence.
    store._orchestration_limits = OrchestrationLimits(1, 8)
    scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
    command = OrchestrationRequest(1, 'orchestration.open', 'fixture-open', scope, {})
    with mock.patch('codex_agents.orchestration_store.secrets.token_hex', return_value=binding.orchestration_handle[4:]):
        OrchestrationOwner(store).apply(command, client_version='codex-host-v1', before_commit=lambda: None)
    store._allow_legacy_orchestration_reference = False


class ProjectHostOpenTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.HostOpenProvenanceTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.scope, self.request = self.fixture.scope, self.base.request
        self.proof = self.base.proof()
        self.assignment = self.fixture.assigned
        self.task, self.route = self.fixture.base.fixture.fixture.task(), self.fixture.base.fixture.route
        owned_orchestration(self.store, self.scope)
        self.context = HostOpenContext(self.scope, self.assignment, self.task, self.route)
        self.token = object()
        self.factory = mock.Mock(side_effect=lambda: self.store)
        self.service = self.make_service()

    @property
    def store(self):
        return self.fixture.store

    def authorize(self, command, context):
        if context is not self.token:
            return None
        return HistoryAuthorization(self.scope.project_id, self.scope.runtime_digest,
            history_request_digest(command), 'host', frozenset({'history.record_host_open', 'history.read_record'}),
            host_open_context=self.context)

    def make_service(self, **options):
        service = HistoryService(self.scope.project_id, self.scope.runtime_digest,
            options.pop('factory', self.factory), authorizer=options.pop('authorizer', self.authorize), **options)
        self.addCleanup(service.close)
        return service

    def command(self, proof=None):
        return HistoryRequest(1, 'history.record_host_open', self.request.run_handle,
            self.scope.project_id, self.scope.runtime_digest,
            {'open_request': self.request.payload(), 'provenance': self.proof if proof is None else proof})

    def call(self, *, service=None, proof=None, context=None):
        request = self.command(proof)
        return decode_history_response((service or self.service).handle(encode_history_request(request),
            self.token if context is None else context), request)

    def persist(self, *, proof=None, before_commit=lambda: None):
        return self.store.record_host_open(self.scope, self.request, self.proof if proof is None else proof,
            assignment=self.assignment, task=self.task, route=self.route, before_commit=before_commit)

    def counts(self):
        return (self.store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='host_open_provenance'").fetchone()[0],
                self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_api_commits_one_protected_original_record_and_index_without_claim_or_execution(self):
        first = self.call()
        self.assertEqual(self.proof, first['record']['provenance'])
        self.assertEqual(self.proof['provenance']['opened_at_ms'], first['record']['observed_at_ms'])
        self.assertEqual(first, self.call())
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        self.assertEqual(0, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            self.store.append_record(canonical_json(first['record']), source='host', ingested_at_ms=first['metadata']['ingested_at_ms'])

    def test_explicit_known_retry_does_not_read_clock_or_extend_original_windows(self):
        first = self.call()
        before = self.store._lease_row(self.scope)
        changes = self.store._connection.total_changes
        with mock.patch.object(self.store, '_result_clock', side_effect=AssertionError('clock refreshed')):
            self.assertEqual(first, self.call())
        self.assertEqual(before, self.store._lease_row(self.scope))
        self.assertEqual(changes, self.store._connection.total_changes)

    def test_record_and_index_rollback_together_and_known_reply_survives_lost_commit_reply(self):
        transaction = self.store._transaction
        @contextmanager
        def rollback():
            with transaction():
                yield
                raise OSError('PRIVATE-BEFORE-COMMIT')
        with mock.patch.object(self.store, '_transaction', new=rollback), self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.persist()
        self.assertEqual((0, 0), self.counts())
        @contextmanager
        def lose_reply():
            with transaction():
                yield
            raise OSError('PRIVATE-AFTER-COMMIT')
        with mock.patch.object(self.store, '_transaction', new=lose_reply), self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.persist()
        self.assertEqual((1, 1), self.counts())
        first = self.persist()
        self.assertEqual(first, self.persist())
        self.assertEqual((1, 1), self.counts())

    def test_missing_record_or_missing_index_is_not_reconstructed(self):
        first = self.persist()
        self.store._connection.execute('PRAGMA foreign_keys=OFF')  # Deliberate isolated corruption.
        self.store._connection.execute('DELETE FROM records WHERE record_id=?', (first.record_id,))
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.persist()
        self.assertEqual((0, 1), self.counts())

    def test_orphaned_record_or_changed_journal_seal_refuses_known_retry(self):
        first = self.persist()
        self.store._connection.execute('UPDATE project_host_opens SET journal_seal=?', ('f'*64,))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.persist()
        self.store._connection.execute('DELETE FROM project_host_opens')
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.persist()
        self.assertEqual(first, self.store.record(first.record_id))
        self.assertEqual((1, 0), self.counts())

    def test_changed_validly_sealed_account_fact_cannot_replace_committed_original(self):
        first = self.call()
        facts = deepcopy(self.proof['provenance'])
        facts['admission_record_id'] = 'admission:foreign'
        changed = seal_open_provenance(self.fixture.key, self.request, facts)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call(proof=changed)
        self.assertEqual(first, self.call())
        self.assertEqual((1, 1), self.counts())

    def test_first_write_checks_current_owned_state_and_original_window(self):
        self.fixture.base.stamp = self.proof['provenance']['observation_deadline_at_ms']
        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.persist()
        self.assertEqual((0, 0), self.counts())
        self.fixture.base.stamp = self.proof['provenance']['opened_at_ms']
        with self.assertRaisesRegex(HistoryError, 'history_result_unavailable'):
            self.persist()  # Trusted observed expiry cannot roll back into fresh work.

    def test_missing_project_lifecycle_is_not_replaced_by_a_legacy_reference_flag(self):
        self.store._connection.execute('DELETE FROM project_orchestration_requests')
        self.store._connection.execute('DELETE FROM project_orchestration_checkpoints')
        self.store._connection.execute('DELETE FROM project_orchestrations')
        self.store._allow_legacy_orchestration_reference = True
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_unavailable'):
            self.persist()
        self.assertEqual((0, 0), self.counts())

    def test_paused_current_project_cannot_accept_a_first_open_journal(self):
        scope = OrchestrationScope(**{name: getattr(self.scope, name) for name in SCOPE_FIELDS})
        pause = OrchestrationRequest(1, 'orchestration.pause', 'pause-before-open', scope,
            {'orchestration_handle': self.scope.orchestration_handle, 'expected_sequence': 1})
        OrchestrationOwner(self.store).apply(pause, client_version='codex-host-v1', before_commit=lambda: None)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.persist()
        self.assertEqual((0, 0), self.counts())

    def test_closed_request_cannot_supply_native_flags_or_choose_a_record_id(self):
        from codex_agents.history_api import decode_history_request
        raw = parse_bounded_json(encode_history_request(self.command()))
        for name, value in (('native', True), ('record_id', 'host_open_provenance:chosen'), ('active', True)):
            changed = deepcopy(raw)
            changed['payload'][name] = value
            with self.subTest(name=name), self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                decode_history_request(canonical_json(changed))
        self.factory.assert_not_called()

    def test_default_denial_and_wrong_independent_scope_never_open_writer(self):
        service = self.make_service(authorizer=None)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call(service=service)
        self.context = replace(self.context, binding=replace(self.scope, primary_session_id='foreign'))
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()
        self.factory.assert_not_called()
        self.assertEqual((0, 0), self.counts())

    def test_final_authorization_revocation_context_mutation_or_closure_never_commits(self):
        for mode in ('revoke', 'context', 'close'):
            service = self.make_service()
            calls = []
            original = deepcopy(self.task)
            def change(request, token):
                value = self.authorize(request, token)
                calls.append(True)
                if len(calls) == 2:
                    if mode == 'revoke':
                        return None
                    if mode == 'context':
                        self.task['objective'] = 'PRIVATE-MUTATED-INPUT'
                    else:
                        service.close()
                return value
            service._authorizer = change
            with self.subTest(mode=mode), self.assertRaises(HistoryError):
                self.call(service=service)
            if mode != 'close':
                self.assertEqual((0, 0), self.counts())
                self.task.clear()
                self.task.update(original)

    def test_factory_closure_cannot_lend_a_new_writer_or_return_success(self):
        service = self.make_service()
        def close_factory():
            service.close()
            return self.store
        service._open = close_factory
        with self.assertRaises(HistoryError):
            self.call(service=service)
        self.assertIsNone(service._store)

    def test_reply_encoder_closure_keeps_commit_but_returns_only_denial(self):
        original = self.service._reply
        def close(*args, **kwargs):
            encoded = original(*args, **kwargs)
            if kwargs.get('result') is not None:
                self.service.close()
            return encoded
        with mock.patch.object(self.service, '_reply', side_effect=close), self.assertRaises(HistoryError):
            self.call()
        self.fixture.store = self.fixture.open_history()
        self.store._orchestration_limits = OrchestrationLimits(1, 8)
        self.store._allow_legacy_orchestration_reference = False
        self.service = self.make_service()
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(self.call(), self.call())

    def test_known_receipt_metadata_is_sealed_against_stored_timestamp_changes(self):
        first = self.persist()
        self.store._connection.execute('UPDATE records SET ingested_at_ms=ingested_at_ms+1 WHERE record_id=?', (first.record_id,))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.persist()

    def test_receipt_capture_is_immutable_across_native_reauthorization(self):
        original = deepcopy(self.proof)
        def mutate():
            self.proof['provenance']['admission_record_id'] = 'PRIVATE-MUTATED-RECEIPT'
        entry = self.persist(before_commit=mutate)
        self.assertEqual(original, parse_bounded_json(entry.content)['provenance'])
        self.assertNotIn(b'PRIVATE-MUTATED-RECEIPT', entry.content)

    def test_lost_transport_reply_is_not_retried_and_original_receipt_remains(self):
        client = HistoryClient(Path('/private/tmp/fixture-host-open.sock'), self.scope.project_id,
                               self.scope.runtime_digest, timeout=1)
        def lost(path, raw, **kwargs):
            self.service.handle(raw, self.token)
            raise ServiceTransportError('service_unavailable')
        with mock.patch('codex_agents.history_api.exchange_frame', side_effect=lost) as exchange:
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                client.record_host_open(self.request, self.proof)
            exchange.assert_called_once()
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(self.call(), self.call())

    def test_explicit_schema5_upgrade_preserves_old_rows_and_creates_no_open_facts(self):
        tables = ('history_identity', 'records', 'result_identity', 'result_leases', 'project_orchestrations',
                  'project_orchestration_checkpoints', 'project_orchestration_requests')
        before = {name: [tuple(row) for row in self.store._connection.execute('SELECT * FROM '+name)] for name in tables}
        capacity = self.store._result_capacity
        self.store._connection.execute('DROP TABLE project_host_opens')
        self.store._connection.execute('PRAGMA user_version=5')
        self.service.close()
        self.store.close()
        path = self.fixture.directory/'history.sqlite3'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.fixture.open_history()
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.fixture.store = ProjectHistory(self.fixture.directory, self.scope.project_id,
            result_key=self.fixture.key, result_capacity=capacity, result_clock_ms=lambda: self.fixture.base.stamp,
            orchestration_limits=OrchestrationLimits(1, 8), allow_schema_upgrade=True)
        self.assertEqual(6, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
        for name, rows in before.items():
            self.assertEqual(rows, [tuple(row) for row in self.store._connection.execute('SELECT * FROM '+name)])
        self.assertEqual((0, 0), self.counts())
        self.persist()
        self.assertEqual((1, 1), self.counts())

    def test_two_process_client_service_sqlite_path_keeps_sqlite_out_of_client(self):
        endpoint = self.fixture.directory.parent/'project-open.sock'
        capacity = self.store._result_capacity
        self.service.close()
        self.store.close()  # Only the child data owner holds the writer now.
        program = 'import sys;sys.path.insert(0,'+repr(str(ORCHESTRATION_ROOT))+');'+JOURNAL_SERVER_FIXTURE
        server = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', program],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        bootstrap = {'directory': str(self.fixture.directory), 'endpoint': str(endpoint),
            'project': self.scope.project_id, 'runtime': self.scope.runtime_digest,
            'fixture_key': self.fixture.key.hex(), 'binding': self.scope.as_data(),
            'assignment': self.assignment, 'task': self.task, 'route': self.route,
            'capacity': {'max_entries': capacity.max_entries, 'max_reserved_bytes': capacity.max_reserved_bytes,
                         'reservation_ms': capacity.reservation_ms, 'retention_ms': capacity.retention_ms},
            'now': self.fixture.base.stamp}
        try:
            server.stdin.write(json.dumps(bootstrap)+'\n')
            server.stdin.flush()
            self.assertTrue(select.select([server.stdout], [], [], 15)[0], 'fixture server did not become ready')
            self.assertEqual('ready', server.stdout.readline().strip())
            client_program = 'import sys;sys.path.insert(0,'+repr(str(ORCHESTRATION_ROOT))+');'+JOURNAL_CLIENT_FIXTURE
            response = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', client_program],
                input=json.dumps({'endpoint': str(endpoint), 'project': self.scope.project_id,
                    'runtime': self.scope.runtime_digest, 'request': self.request.payload(), 'proof': self.proof}),
                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, response.returncode, response.stderr)
            returned = json.loads(response.stdout)
            self.assertFalse(returned['sqlite_loaded'])
            self.assertEqual(0, server.wait(timeout=15), server.stderr.read(2000))
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            server.stdin.close()
            server.stdout.close()
            server.stderr.close()
        self.fixture.store = self.fixture.open_history()
        self.store._orchestration_limits = OrchestrationLimits(1, 8)
        self.store._allow_legacy_orchestration_reference = False
        self.service = self.make_service()
        recovered = self.call()
        self.assertEqual(returned['record_id'], recovered['record']['record_id'])
        self.assertEqual(returned['digest'], recovered['metadata']['content_digest'])
        self.assertEqual((1, 1), self.counts())


class OriginalProjectVerifierJournalTests(unittest.TestCase):
    def test_original_account_verifier_dependency_survives_project_commit(self):
        source = fixtures.OriginalVerifierOpenTests(methodName='runTest')
        source.setUp()
        self.addCleanup(source.doCleanups)
        owned_orchestration(source.store, source.lease.binding)
        proof = source.proof()
        entry = source.store.record_host_open(source.lease.binding, source.request, proof,
            assignment=source.assignment, task=source.task, route=source.route, before_commit=lambda: None)
        stored = parse_bounded_json(entry.content)
        self.assertEqual(source.digest, stored['provenance']['provenance']['worker_outcome_digest'])
        self.assertEqual(proof, stored['provenance'])
        self.assertEqual(1, source.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_first_open_journal_cannot_be_added_after_execution_consumption(self):
        from tests.test_run_provenance import RunProvenanceTests
        source = RunProvenanceTests(methodName='runTest')
        source.setUp()
        self.addCleanup(source.doCleanups)
        # Explicit old reference consumption predates managed enrollment. Fresh
        # managed consumption now requires this journal and cannot create it late.
        source.base.consume(source.grant)
        store = source.managed_project(record_host_open=False)
        request = OpenProvenanceInput(source.fixture.scope.run_handle, source.fixture.assigned['assignment_id'])
        proof = source.base.ledger._attest_open_provenance(source.fixture.project,
            runtime_digest=source.fixture.scope.runtime_digest, caller_scope='a'*64, request=request,
            storage_binding=source.fixture.scope, storage_evidence=source.fixture.evidence, work_limits=source.limits)
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            store.record_host_open(source.fixture.scope, request, proof,
                assignment=source.fixture.assigned, task=source.fixture.base.fixture.fixture.task(),
                route=source.fixture.base.fixture.route, before_commit=lambda: None)
        self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])


JOURNAL_SERVER_FIXTURE = r'''
import json,socket,sys
from pathlib import Path
from codex_agents.history_service import HistoryService,HistoryAuthorization
from codex_agents.history_api import history_request_digest
from codex_agents.host_open_contracts import HostOpenContext
from codex_agents.result_contracts import ResultLeaseBinding,ResultCapacity
from codex_agents.orchestration_api import OrchestrationLimits
from codex_agents.project_history import ProjectHistory
data=json.loads(sys.stdin.readline(1048577))
context=HostOpenContext(ResultLeaseBinding(**data['binding']),data['assignment'],data['task'],data['route'])
token=object() # Fake enrolled transport, not native authentication evidence.
def authorize(command,actual):
 if actual is token:
  return HistoryAuthorization(data['project'],data['runtime'],history_request_digest(command),'host',
   frozenset({'history.record_host_open'}),host_open_context=context)
def factory():
 return ProjectHistory(Path(data['directory']),data['project'],result_key=bytes.fromhex(data['fixture_key']),
  result_capacity=ResultCapacity(**data['capacity']),result_clock_ms=lambda:data['now'],
  orchestration_limits=OrchestrationLimits(1,8))
service=HistoryService(data['project'],data['runtime'],factory,authorizer=authorize)
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
try:
 listener.bind(data['endpoint']);listener.listen(1);listener.settimeout(10)
 print('ready',flush=True)
 connection,_=listener.accept()
 with connection:
  connection.settimeout(2);raw=bytearray()
  while b'\n' not in raw and len(raw)<=81920:
   part=connection.recv(min(8192,81921-len(raw)))
   if not part:break
   raw.extend(part)
  connection.sendall(service.handle(bytes(raw).split(b'\n',1)[0],token)+b'\n')
finally:
 listener.close();service.close()
'''

JOURNAL_CLIENT_FIXTURE = r'''
import json,sys
from pathlib import Path
from codex_agents.history_api import HistoryClient
from codex_agents.host_open_contracts import OpenProvenanceInput
data=json.loads(sys.stdin.read(16384))
client=HistoryClient(Path(data['endpoint']),data['project'],data['runtime'],timeout=2)
value=client.record_host_open(OpenProvenanceInput(**data['request']),data['proof'])
print(json.dumps({'record_id':value['record']['record_id'],'digest':value['metadata']['content_digest'],
 'sqlite_loaded':'sqlite3' in sys.modules}))
'''
