"""Negative dispatch boundaries; typed fixture context is never native authority."""
from copy import copy
from dataclasses import replace
from pathlib import Path
import json
import unittest
from unittest import mock

from tests import test_codex_worker as workers
from tests.test_bridge_liveness import attempt
from tests.test_project_results import KEY

from codex_agents.dispatch_api import (
    DispatchError, decode_dispatch_response, dispatch_request, dispatch_request_digest, encode_dispatch_request,
)
from codex_agents.prepared_dispatch import DispatchAuthorization, PreparedDispatchContext, PreparedDispatchService
from codex_agents.project_history import ProjectHistory
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceHandle, ResultPersistenceLane


class PreparedDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workers.CodexWorkerTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = Path(self.fixture.temporary.name) / 'dispatch-history'
        self.directory.mkdir(mode=0o700)
        self.factory = mock.Mock(side_effect=lambda: ProjectHistory(self.directory,
            self.fixture.scope.project_id, result_key=KEY, allow_legacy_orchestration_reference=True))
        self.lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, self.factory,
            max_entries=1, retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES)
        self.addCleanup(self.stop_lane)
        # Deliberately no original persistence entry, claim or consumption.
        # Matching typed handles must not be enough to issue launch custody.
        self.context = PreparedDispatchContext(attempt(), ResultPersistenceHandle(),
            self.fixture.pool.prepare(b'{}'), 'a'*64, (0, '0'*40, 'b'*64))
        self.marker = object()

    def stop_lane(self):
        self.lane.close()
        self.lane._writer.join(6)
        self.assertFalse(self.lane._writer.is_alive())

    def authorize(self, request, transport):
        if transport is self.marker:
            return DispatchAuthorization(self.fixture.scope, dispatch_request_digest(request), self.context,
                                         frozenset({'dispatch.submit', 'dispatch.status'}))

    def service(self, **options):
        service = PreparedDispatchService(self.fixture.owner, self.fixture.pool, self.lane,
            max_requests=1, steps_per_cycle=1, **options)
        self.fixture.service.bind_dispatch(service)
        return service

    def request(self, operation='dispatch.submit', **options):
        return dispatch_request(self.fixture.scope, operation, self.context.binding.run_handle,
                                self.context.binding.attempt, self.context.inputs_digest, **options)

    def call(self, service, request=None, *, transport=None):
        request = request or self.request()
        raw = service.handle(encode_dispatch_request(request), self.marker if transport is None else transport)
        return decode_dispatch_response(raw, request)

    def assert_unlaunched(self):
        self.assertEqual(0, len(self.fixture.registry._issued))
        self.factory.assert_not_called()  # Even writer startup remains lazy.
        self.assertTrue(self.lane._writer.is_alive())
        self.assertFalse(self.fixture.pool._closed)

    def test_default_denial_and_transport_labels_cannot_queue_or_touch_originals(self):
        service = self.service()
        with self.assertRaisesRegex(DispatchError, 'dispatch_not_authorized'):
            self.call(service)
        service._authorizer = self.authorize
        with self.assertRaisesRegex(DispatchError, 'dispatch_not_authorized'):
            self.call(service, transport={'authorized': True})
        self.assertEqual({}, service._jobs)
        self.assert_unlaunched()

    def test_or4_a_published_report_may_carry_any_readable_diagnostic_reason(self):
        """OR-4: bridge.py gives a result with no typed reason `contract_validation_failed`, and ledgers
        written before 0.8.43 carry `inconsistent_axes`; the client must read both, and still refuse others."""
        from dataclasses import asdict
        from codex_agents.contracts import RESULT_DIAGNOSTIC_REASON_CODES
        from codex_agents.history_contracts import TerminalDelivery
        from codex_agents.learning_contracts import canonical_json
        request = self.request()
        binding = self.context.binding

        def reply(code):
            report = {'provider': 'codex', 'requested_model': 'gpt-6.1-sol', 'observed_model': None, 'effort': 'high',
                      'wall_seconds': 1, 'read_containment': 'advisory', 'rerouted': False, 'outcome': 'invalid_result',
                      'usage': None, 'result_validation_reason_code': code, 'bridge_run_id': binding.claim_id,
                      'reviewed': True, 'terminal_state': 'failed', 'capacity_state': 'retained',
                      'export_file_count': 1, 'export_commit': '0' * 40, 'export_manifest_digest': 'b' * 64}
            delivery = TerminalDelivery(1, binding, 'record-1', 'c' * 64, 'd' * 64)
            result = {'state': 'published', 'known': True, 'binding': asdict(binding), 'delivery': asdict(delivery),
                      'report': report, 'inputs_digest': request.inputs_digest}
            return canonical_json({'protocol_version': 1, 'request_id': request.request_id,
                                   'request_digest': dispatch_request_digest(request), 'ready': True,
                                   'result': result, 'reason_code': None})
        for code in (None, 'missing_evidence'):
            self.assertEqual('published', decode_dispatch_response(reply(code), request)['state'])
        for code in sorted(RESULT_DIAGNOSTIC_REASON_CODES):
            with self.subTest(code=code):
                self.assertEqual(code, decode_dispatch_response(reply(code), request)['report']['result_validation_reason_code'])
        with self.assertRaisesRegex(DispatchError, 'dispatch_invalid_response'):
            decode_dispatch_response(reply('free text from a child'), request)

    def test_unconfigured_control_facade_refuses_dispatch_before_any_execution(self):
        request = self.request()
        raw = self.fixture.service.handle(encode_dispatch_request(request), self.marker)
        with self.assertRaisesRegex(DispatchError, 'dispatch_unavailable'):
            decode_dispatch_response(raw, request)
        self.assert_unlaunched()

    def test_unknown_status_changed_scope_digest_and_export_pin_cannot_queue(self):
        service = self.service(authorizer=self.authorize)
        for request, reason in (
            (self.request('dispatch.status'), 'dispatch_not_found'),
            (dispatch_request(replace(self.fixture.scope, primary_session_id='another'), 'dispatch.submit',
                self.context.binding.run_handle, 1, self.context.inputs_digest), 'dispatch_not_authorized'),
            (dispatch_request(self.fixture.scope, 'dispatch.submit', self.context.binding.run_handle, 1, 'f'*64),
                'dispatch_conflict'),
            (self.request(expected_export_commit='f'*40), 'dispatch_conflict'),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(DispatchError, reason):
                self.call(service, request)
        self.assertEqual({}, service._jobs)
        self.assert_unlaunched()

    def test_typed_context_without_original_consumption_never_issues_a_ticket(self):
        service = self.service(authorizer=self.authorize)
        self.assertEqual('queued', self.call(service)['state'])
        service.drive()
        self.assertEqual('unknown', self.call(service, self.request('dispatch.status'))['state'])
        self.assertEqual('unknown', self.call(service)['state'])
        self.assertEqual(1, len(service._jobs))  # Uncertain slots cannot be recycled.
        self.assert_unlaunched()

    def test_authorizer_closing_service_cannot_queue_after_revocation(self):
        def closing(request, transport):
            authorization = self.authorize(request, transport)
            service.close()
            return authorization
        service = self.service(authorizer=closing)
        with self.assertRaisesRegex(DispatchError, 'dispatch_unavailable'):
            self.call(service)
        self.assertEqual({}, service._jobs)
        self.assert_unlaunched()

    def test_equal_copied_context_cannot_replace_queued_original(self):
        service = self.service(authorizer=self.authorize)
        self.call(service)
        self.context = copy(self.context)
        with self.assertRaisesRegex(DispatchError, 'dispatch_conflict'):
            self.call(service)
        service.drive()
        self.assertEqual('unknown', next(iter(service._jobs.values())).state)
        self.assert_unlaunched()

    def test_reentrant_refusal_does_not_clear_outer_request_guard(self):
        visited = []
        def reentrant(request, transport):
            if not visited:
                raw = service.handle(encode_dispatch_request(request), transport)
                visited.append((json.loads(raw), service._busy))
            return self.authorize(request, transport)
        service = self.service(authorizer=reentrant)
        self.assertEqual('queued', self.call(service)['state'])
        self.assertFalse(service._busy)
        self.assertEqual('dispatch_unavailable', visited[0][0]['reason_code'])
        self.assertFalse(visited[0][0]['ready'])
        self.assertTrue(visited[0][1])
        self.assertEqual(1, len(service._jobs))
        self.assert_unlaunched()

    def test_closing_facade_prevents_drive_and_preserves_borrowed_owners(self):
        service = self.service(authorizer=self.authorize)
        self.call(service)
        self.fixture.service.close()
        with self.assertRaisesRegex(DispatchError, 'dispatch_unavailable'):
            service.drive()
        self.assert_unlaunched()

    def test_unknown_dispatch_cannot_be_retired_as_if_account_release_was_confirmed(self):
        service = self.service(authorizer=self.authorize)
        self.call(service)
        service.drive()
        job = next(iter(service._jobs.values()))
        with self.assertRaisesRegex(DispatchError, 'dispatch_conflict'):
            service.retire_released(self.context)
        self.assertEqual('unknown', job.state)
        self.assertEqual(1, len(service._jobs))
        self.assert_unlaunched()

    def test_status_can_read_a_pinned_original_without_changing_submit_identity(self):
        service = self.service(authorizer=self.authorize)
        pinned = self.request(expected_export_commit=self.context.export_info[1])
        first = self.call(service, pinned)
        status = self.call(service, self.request('dispatch.status'))
        self.assertEqual({**first, 'known': True}, status)
        with self.assertRaisesRegex(DispatchError, 'dispatch_conflict'):
            self.call(service, self.request())
        with self.assertRaisesRegex(DispatchError, 'dispatch_conflict'):
            self.call(service, self.request('dispatch.status', expected_export_commit='f'*40))
        self.assert_unlaunched()
