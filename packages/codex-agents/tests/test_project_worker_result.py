"""Original worker references before verifier open; synthetic native authority."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_run_provenance as fixtures
from tests.test_bridge import result

from codex_agents import history_api, result_read_api
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultReference, WorkerResultContext
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.orchestration_api import OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.result_contracts import ResultObservation
from codex_agents.result_read_api import ResultReadClient, result_read_request_digest
from codex_agents.result_read_service import ResultReadAuthorization


class ProjectWorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.RunProvenanceTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture, self.binding = self.base.fixture, self.base.base.binding
        self.store = self.base.managed_project()
        source = self.fixture.base.fixture
        self.original_task, self.route = source.fixture.task(), source.route
        request = OpenProvenanceInput(self.binding.run_handle, self.fixture.assigned['assignment_id'])
        proof = self.base.base.ledger._attest_open_provenance(self.binding.project_id,
            runtime_digest=self.binding.runtime_digest, caller_scope='a'*64, request=request,
            storage_binding=self.fixture.scope, storage_evidence=self.fixture.evidence, work_limits=self.base.limits)
        self.opened = self.store.record_host_open(self.fixture.scope, request, proof,
            assignment=self.fixture.assigned, task=self.original_task, route=self.route, before_commit=lambda: None)
        self.base.base.consume(self.base.grant, provenance=self.base.proof())
        self.body = canonical_json({**result(), 'summary': 'PRIVATE-WORKER-RESULT-CANARY'})
        self.delivery = self.store.publish_result(self.binding, self.fixture.assigned, self.body,
            ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None))
        self.host_reference = HostResultReference(self.binding, self.delivery.record_id,
            self.delivery.record_content_digest, self.delivery.result_digest)
        self.admitted = self.store.admit_host_result(self.host_reference, assignment=self.fixture.assigned,
            task=self.original_task, route=self.route, client_capability=source.fixture.clients['bridge-v1'],
            before_commit=lambda: None)
        self.digest = self.admitted['admission']['host_outcome_digest']
        self.task = {**self.original_task, 'id': 'verify-original-worker', 'family': 'verification',
            'verification_for_family': self.original_task['family'], 'verification_for_provider': 'codex',
            'verification_for_outcome': self.digest}
        self.context = WorkerResultContext(self.binding, self.task)
        self.token = object()
        self.factory = mock.Mock(return_value=self.store)
        self.service = self.make_service()
        self.client = HistoryClient(Path('/private/tmp/worker-reference.sock'), self.binding.project_id,
            self.binding.runtime_digest, timeout=1)

    def authorize(self, command, token):
        if token is not self.token:
            return None
        return HistoryAuthorization(command.project_id, command.runtime_digest, history_request_digest(command),
            'host', frozenset({'history.worker_reference'}), worker_result_context=self.context)

    def result_authorize(self, command, token):
        if token is not self.token:
            return None
        return ResultReadAuthorization(self.binding, self.delivery.result_digest, result_read_request_digest(command))

    def make_service(self, **options):
        service = HistoryService(self.binding.project_id, self.binding.runtime_digest, self.factory,
            authorizer=options.get('authorizer', self.authorize), result_authorizer=self.result_authorize)
        self.addCleanup(service.close)
        return service

    def command(self, **payload):
        return HistoryRequest(1, 'history.worker_reference', self.digest, self.binding.project_id,
            self.binding.runtime_digest, {'binding': self.binding.as_data(), 'host_outcome_digest': self.digest, **payload})

    def call(self, *, service=None):
        command = self.command()
        return decode_history_response((service or self.service).handle(encode_history_request(command), self.token), command)

    def test_actual_original_reference_precedes_any_verifier_open_and_private_bytes_use_bound_reader(self):
        before = self.base.base.ledger._connection.total_changes
        def endpoint(path, raw, **opts):
            return self.service.handle(raw, self.token)
        with mock.patch.object(history_api, 'exchange_frame', side_effect=endpoint) as transport:
            reference = self.client.verification_worker_reference(self.binding, self.digest)
            self.assertEqual(1, transport.call_count)
        self.assertEqual(self.binding.as_data(), reference['binding'])
        self.assertEqual(self.opened.content_digest, reference['host_open_content_digest'])
        self.assertEqual('codex', reference['provider'])
        self.assertEqual(self.original_task['family'], reference['task_family'])
        self.assertNotIn(b'PRIVATE-WORKER', canonical_json(reference))
        with mock.patch.object(result_read_api, 'exchange_frame', side_effect=endpoint):
            actual = ResultReadClient(Path('/private/tmp/worker-reference.sock'), timeout=1).read_result(
                self.binding, reference['result_digest'])
        self.assertEqual(self.body, actual)
        self.assertEqual(before, self.base.base.ledger._connection.total_changes)
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_default_denial_and_foreign_independent_scope_refuse_before_writer_or_body(self):
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call(service=self.make_service(authorizer=None))
        self.context = replace(self.context, binding=replace(self.binding, primary_session_id='foreign'))
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call()
        self.factory.assert_not_called()

    def test_wrong_digest_provider_or_family_cannot_substitute_dependency(self):
        for changes in ({'verification_for_outcome': 'a'*64}, {'verification_for_provider': 'claude'},
                        {'verification_for_family': 'coding'}):
            self.context = WorkerResultContext(self.binding, {**self.task, **changes})
            reason = 'history_invalid_input' if 'verification_for_outcome' in changes else 'history_conflict'
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, reason):
                self.call()

    def test_arbitrary_task_flags_paths_and_result_body_are_not_wire_authority(self):
        for key, value in (('task', self.task), ('active', True), ('path', '/private/tmp/foreign'), ('worker_result', {})):
            with self.subTest(key=key), self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                self.command(**{key: value})
        self.factory.assert_not_called()

    def test_missing_or_corrupt_open_companion_is_never_reconstructed(self):
        self.store._connection.execute('UPDATE project_host_opens SET journal_seal=?', ('f'*64,))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call()
        self.store._connection.execute('DELETE FROM project_host_opens')
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call()
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_missing_registration_companion_is_refused_even_with_legacy_reference_enabled(self):
        self.store._allow_legacy_orchestration_reference = True
        self.store._connection.execute("DELETE FROM records WHERE record_type='execution_provenance'")
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            self.call()

    def pause(self):
        scope = OrchestrationScope(**{name: getattr(self.binding, name) for name in SCOPE_FIELDS})
        command = OrchestrationRequest(1, 'orchestration.pause', 'pause-worker-reference', scope,
            {'orchestration_handle': self.binding.orchestration_handle, 'expected_sequence': 1})
        OrchestrationOwner(self.store).apply(command, client_version='codex-host-v1', before_commit=lambda: None)

    def test_paused_owned_project_cannot_supply_active_verifier_dependency(self):
        self.pause()
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call()

    def test_missing_owned_project_cannot_use_legacy_reference_flag(self):
        self.store._allow_legacy_orchestration_reference = True
        for table in ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations'):
            self.store._connection.execute('DELETE FROM '+table)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_unavailable'):
            self.call()

    def test_expired_unswept_result_refuses_original_bytes(self):
        reservation = self.store.result_reservation(self.binding)
        self.fixture.base.stamp = reservation.expires_at_ms
        with self.assertRaises(HistoryError):
            self.call()
        self.assertEqual('expired', self.store.result_reservation(self.binding).state)

    def test_deleted_private_body_cannot_be_supplied_from_the_primary_or_metadata(self):
        files = list(self.fixture.directory.glob('.result-*'))
        self.assertTrue(files)
        for path in files:
            path.unlink()
        with self.assertRaises(HistoryError):
            self.call()
        self.assertEqual(self.body, canonical_json({**result(), 'summary': 'PRIVATE-WORKER-RESULT-CANARY'}))

    def test_changed_prepared_request_cannot_replace_original_return(self):
        self.store._connection.execute('UPDATE result_reservations SET prepared_digest=?', ('f'*64,))
        with mock.patch.object(self.store, 'read_result', side_effect=AssertionError('unbound body read')):
            with self.assertRaisesRegex(HistoryError, 'history_conflict'):
                self.call()

    def test_revocation_context_change_or_close_after_body_io_returns_no_reference(self):
        original = self.context
        for mode in ('revoke', 'mutate', 'close'):
            calls = []
            def authorize(command, token):
                value = self.authorize(command, token)
                calls.append(True)
                if len(calls) == 2:
                    if mode == 'revoke':
                        return None
                    if mode == 'mutate':
                        self.task['objective'] = 'changed'
                    if mode == 'close':
                        service.close()
                return value
            service = self.make_service(authorizer=authorize)
            with self.subTest(mode=mode), self.assertRaises(HistoryError):
                self.call(service=service)
            self.task = deepcopy(original.task)
            self.task['objective'] = self.original_task['objective']
            self.context = WorkerResultContext(self.binding, self.task)

    def test_pause_during_reply_encoding_cannot_publish_success(self):
        reply = self.service._reply
        def encode(*args, **kwargs):
            raw = reply(*args, **kwargs)
            if kwargs.get('result') is not None:
                self.pause()
            return raw
        with mock.patch.object(self.service, '_reply', side_effect=encode), self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call()

    def test_expiry_during_reply_encoding_cannot_publish_success(self):
        reply = self.service._reply
        def encode(*args, **kwargs):
            raw = reply(*args, **kwargs)
            if kwargs.get('result') is not None:
                self.fixture.base.stamp = kwargs['result']['expires_at_ms']
            return raw
        with mock.patch.object(self.service, '_reply', side_effect=encode), self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.call()

    def test_final_authorizer_cannot_remove_original_journal_after_prepublication_checks(self):
        calls = []
        def authorize(command, token):
            value = self.authorize(command, token)
            calls.append(True)
            if len(calls) == 3:
                self.store._connection.execute('DELETE FROM project_host_opens')
            return value
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call(service=self.make_service(authorizer=authorize))
        self.assertEqual(3, len(calls))

    def test_closed_response_and_exact_request_binding_reject_extra_or_changed_fields(self):
        reference = self.call()
        command = self.command()
        raw = parse_bounded_json(self.service.handle(encode_history_request(command), self.token))
        for field, value in (('provider', 'claude'), ('host_outcome_digest', 'f'*64), ('body_size', True), ('native', True)):
            changed = deepcopy(raw)
            changed['result'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json(changed), command)
        self.assertEqual(reference, self.call())
