"""Actual project/account stores; synthetic enrollment, no provider launch."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_execution_grant as fixtures
from tests.test_bridge import result

from codex_agents import account_api
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, encode_account_request
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.capacity_status_contracts import CapacityStatusInput
from codex_agents.history_contracts import HistoryError, TerminalDelivery
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultObservation
from codex_agents.service_transport import ServiceTransportError
from codex_agents.terminal_delivery import (
    TerminalSubmissionInput, encode_terminal_evidence, verify_terminal_evidence, verify_terminal_receipt,
)


class AccountTerminalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectExecutionGrantTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.grant = self.base.grant()
        self.base.consume(self.grant)
        self.binding = self.base.binding
        observation = ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None, None)
        body = result()
        body['summary'] = 'PRIVATE-BODY-CANARY'
        self.delivery = self.fixture.store.publish_result(self.binding, self.fixture.assigned, canonical_json(body), observation)
        self.proof = self.fixture.store.attest_terminal_delivery(self.binding)
        self.expected, self.source, self.caller_scope = self.fixture.scope, 'project', 'a' * 64
        self.bound = self.base.evidence
        self.submission = TerminalSubmissionInput(self.base.registration, self.grant.registration_digest,
                                                  self.delivery.record_id, self.delivery.record_content_digest,
                                                  self.delivery.result_digest)
        self.request = AccountRequest(1, 'account.submit_terminal', 'terminal-delivery', self.binding.project_id,
                                      COLLECTOR_RUNTIME_DIGEST, self.submission.payload())
        self.service = self.make_service()

    @property
    def ledger(self):
        return self.fixture.base.ledger

    def make_service(self):
        def authorize(request, context):
            if context is self.fixture.base.token:
                return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                            self.source, frozenset({'account.submit_terminal'}), self.caller_scope,
                                            self.expected, self.bound, self.proof)
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(service.close)
        return service

    def call(self, request=None):
        return self.fixture.base.call(request or self.request, service=self.service)

    def held(self):
        self.assertEqual('held', self.ledger._capacity_status(self.binding.project_id,
                         CapacityStatusInput(self.binding.run_handle, self.binding.claim_id))['state'])

    def count(self):
        return self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_completions').fetchone()[0]

    def test_exact_account_acceptance_and_local_acknowledgement_do_not_release_capacity(self):
        with mock.patch.object(ProjectHistory, '__init__', side_effect=AssertionError('account opened project SQL')):
            receipt = self.call()
        self.assertEqual('recorded', receipt['state'])
        self.assertFalse(receipt['known'])
        self.assertEqual({**receipt, 'known': True}, self.call())
        verified = verify_terminal_receipt(self.fixture.key, self.grant, self.delivery, canonical_json(receipt))
        self.assertEqual(receipt, verified)
        self.assertEqual(1, self.count())
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))
        self.assertNotIn(b'PRIVATE-BODY-CANARY', self.proof)
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='dispatch_result'").fetchone()[0])
        self.fixture.store.acknowledge_account_terminal(self.binding, canonical_json(receipt))
        self.assertEqual((), self.fixture.store.pending_terminals())
        self.assertEqual(self.proof, self.fixture.store.attest_terminal_delivery(self.binding))
        self.held()

    def test_only_original_project_actor_can_submit_and_payload_cannot_supply_authority(self):
        for source in ('host', 'owner', 'hook', 'app_server'):
            self.source = source
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.source = 'project'
        for field, value in (('pid', 1), ('cleanup_confirmed', True), ('terminal_evidence', 'PRIVATE')):
            raw = json.loads(encode_account_request(self.request))
            raw['payload'][field] = value
            reply = json.loads(self.service.handle(canonical_json(raw), self.fixture.base.token))
            self.assertEqual('account_invalid_request', reply['reason_code'])
            self.assertNotIn('PRIVATE', str(reply))
        self.expected = replace(self.expected, supervisor_generation=2)
        with self.assertRaises(AccountError):
            self.call()
        self.expected = self.fixture.scope
        self.caller_scope = 'b' * 64
        with self.assertRaisesRegex(AccountError, 'claim_terminal_unavailable'):
            self.call()
        self.assertEqual(0, self.count())
        self.held()

    def test_missing_malformed_forged_or_noncanonical_evidence_cannot_consume_completion(self):
        original = self.proof
        changed = json.loads(original)
        changed['seal'] = 'f' * 64
        extra = json.loads(original)
        extra['evidence']['delivery']['dispatch_result']['summary'] = 'PRIVATE-REJECTED-CONTENT'
        for raw in (None, b'PRIVATE-ARBITRARY', original + b'\n', canonical_json(changed), canonical_json(extra),
                    b'{"protocol_version":1,"protocol_version":1,' + original[1:], b'x' * 16_385):
            self.proof = raw
            with self.assertRaises(AccountError) as caught:
                self.call()
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(0, self.count())
        self.proof = original
        self.bound = None
        with self.assertRaisesRegex(AccountError, 'claim_storage_required'):
            self.call()
        self.held()

    def test_original_registration_sql_and_terminal_metadata_must_match(self):
        for name, value in (('registration_digest', 'f' * 64), ('terminal_content_digest', 'f' * 64),
                            ('result_digest', 'f' * 64), ('terminal_record_id', 'dispatch_result:' + 'f' * 64)):
            request = replace(self.request, payload=replace(self.submission, **{name: value}).payload())
            with self.subTest(field=name), self.assertRaisesRegex(AccountError, 'claim_terminal_invalid'):
                self.call(request)
        original = self.ledger._connection.execute('SELECT assignment_digest FROM host_run_bindings WHERE run_id=?',
                                                  (self.binding.run_id,)).fetchone()[0]
        self.ledger._connection.execute('UPDATE host_run_bindings SET assignment_digest=? WHERE run_id=?',
                                        ('f' * 64, self.binding.run_id))
        with self.assertRaisesRegex(AccountError, 'claim_terminal_unavailable'):
            self.call()
        self.ledger._connection.execute('UPDATE host_run_bindings SET assignment_digest=? WHERE run_id=?',
                                        (original, self.binding.run_id))
        self.assertEqual(0, self.count())

    def test_conflicting_terminal_even_with_fixture_key_cannot_replace_first_completion(self):
        first = self.call()
        data = json.loads(self.proof)['evidence']['delivery']['dispatch_result']
        data['reviewed'] = False
        data['record_id'] = 'dispatch_result:' + content_digest({k: v for k, v in data.items() if k != 'record_id'})
        delivery = TerminalDelivery(self.delivery.delivery_sequence, self.binding, data['record_id'],
                                    content_digest(data), data['result_digest'])
        # Explicit forged protected-writer fixture; authentication is not inferred.
        self.proof = encode_terminal_evidence(self.fixture.key, self.grant, delivery, canonical_json(data))
        request = replace(self.request, payload=replace(self.submission, terminal_record_id=delivery.record_id,
                                                       terminal_content_digest=delivery.record_content_digest).payload())
        with self.assertRaisesRegex(AccountError, 'claim_terminal_conflict'):
            self.call(request)
        self.assertEqual(1, self.count())
        self.assertEqual(first['terminal_record_id'], self.ledger._connection.execute('SELECT terminal_record_id FROM capacity_completions').fetchone()[0])
        self.held()

    def test_account_transaction_rollback_leaves_project_outbox_pending(self):
        original = self.ledger._account_transaction

        @contextmanager
        def fail_commit():
            with original():
                yield
                raise OSError('PRIVATE-ACCOUNT-COMMIT')

        with mock.patch.object(self.ledger, '_account_transaction', new=fail_commit), \
                self.assertRaisesRegex(AccountError, 'account_unavailable'):
            self.call()
        self.assertEqual(0, self.count())
        self.assertEqual((self.delivery,), self.fixture.store.pending_terminals())
        self.held()
        self.assertFalse(self.call()['known'])

    def test_lost_account_reply_reopens_as_same_receipt_without_clock_or_signature_refresh(self):
        original = self.ledger._account_transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-ACCOUNT-REPLY')

        with mock.patch.object(self.ledger, '_account_transaction', new=lose_reply), \
                self.assertRaisesRegex(AccountError, 'account_unavailable'):
            self.call()
        receipt = self.call()
        self.assertTrue(receipt['known'])
        self.ledger.close()
        bridge = self.fixture.base.fixture.fixture
        self.fixture.base.ledger = LearningLedger(bridge.state, client_capabilities=bridge.clients)
        self.addCleanup(self.ledger.close)
        self.service = self.make_service()
        with mock.patch.object(self.ledger, '_clock_ms', return_value=self.grant.claimed_at_ms - 1), \
                mock.patch.object(self.ledger, '_prepare_attestations', side_effect=AssertionError('repeat signatures')):
            self.assertEqual(receipt, self.call())
        self.assertEqual(1, self.count())
        self.fixture.store.acknowledge_account_terminal(self.binding, canonical_json(receipt))
        self.held()

    def test_lost_project_ack_reply_reopens_and_reconciles_exactly(self):
        receipt = canonical_json(self.call())
        original = self.fixture.store._transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-PROJECT-ACK')

        with mock.patch.object(self.fixture.store, '_transaction', new=lose_reply), \
                self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.fixture.store.acknowledge_account_terminal(self.binding, receipt)
        self.fixture.store.close()
        self.fixture.store = self.fixture.open_history()
        self.assertEqual((), self.fixture.store.pending_terminals())
        self.fixture.store.acknowledge_account_terminal(self.binding, receipt)
        self.held()

    def test_unverified_or_changed_account_reply_never_acknowledges_project_outbox(self):
        receipt = self.call()
        for changes in ({'receipt_digest': 'f' * 64}, {'result_digest': 'f' * 64}, {'cleanup_confirmed': True}, {'known': 1}):
            with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                self.fixture.store.acknowledge_account_terminal(self.binding, canonical_json({**receipt, **changes}))
            self.assertEqual((self.delivery,), self.fixture.store.pending_terminals())
        self.held()

    def test_export_requires_actual_consumption_and_reads_no_private_result_artifact(self):
        with mock.patch.object(ProjectHistory, 'read_result', side_effect=AssertionError('body read during metadata export')):
            evidence = verify_terminal_evidence(self.fixture.key, self.fixture.scope,
                                                self.fixture.store.attest_terminal_delivery(self.binding))
        self.assertEqual(self.delivery, evidence.delivery)
        self.fixture.store._connection.execute('DELETE FROM project_execution_consumptions')
        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.fixture.store.attest_terminal_delivery(self.binding)
        self.assertEqual(0, self.count())

    def test_client_never_retries_ambiguous_terminal_submission_automatically(self):
        client = AccountClient(Path('/private/tmp/fixture-account.sock'), self.binding.project_id, COLLECTOR_RUNTIME_DIGEST)

        def exchange(path, raw, **options):
            self.assertEqual(account_api.MAX_ACCOUNT_FRAME_BYTES, options['response_limit'])
            self.service.handle(raw, self.fixture.base.token)
            raise ServiceTransportError('connection_failed')

        with mock.patch.object(account_api, 'exchange_frame', side_effect=exchange) as transport:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.submit_terminal(self.submission, request_id='client-terminal')
            self.assertEqual(1, transport.call_count)
        self.assertEqual(1, self.count())
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lambda path, raw, **kw: self.service.handle(raw, self.fixture.base.token)) as transport:
            receipt = client.submit_terminal(self.submission, request_id='client-terminal')
            self.assertTrue(receipt['known'])
            self.assertEqual(1, transport.call_count)
        self.held()

    def test_body_retirement_preserves_exact_terminal_delivery_and_acknowledgement(self):
        reservation = self.fixture.store.result_reservation(self.binding)
        self.fixture.base.stamp = reservation.expires_at_ms + 1
        self.assertEqual(1, self.fixture.store.expire_results(limit=1))
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.fixture.store.read_result(self.binding)
        self.assertEqual(self.proof, self.fixture.store.attest_terminal_delivery(self.binding))
        receipt = self.call()
        self.fixture.store.acknowledge_account_terminal(self.binding, canonical_json(receipt))
        self.assertEqual((), self.fixture.store.pending_terminals())
        self.held()

    def test_codex_completion_cannot_import_claude_client_observations(self):
        data = json.loads(self.proof)['evidence']['delivery']['dispatch_result']
        data['client_diagnostics'] = {'exit_code': None, 'result_frame_seen': False, 'result_subtype': None, 'error_code': None}
        data['record_id'] = 'dispatch_result:' + content_digest({k: v for k, v in data.items() if k != 'record_id'})
        delivery = TerminalDelivery(self.delivery.delivery_sequence, self.binding, data['record_id'],
                                    content_digest(data), data['result_digest'])
        self.proof = encode_terminal_evidence(self.fixture.key, self.grant, delivery, canonical_json(data))
        request = replace(self.request, payload=replace(self.submission, terminal_record_id=delivery.record_id,
                                                       terminal_content_digest=delivery.record_content_digest).payload())
        with self.assertRaisesRegex(AccountError, 'claim_terminal_unavailable'):
            self.call(request)
        self.assertEqual(0, self.count())
        self.held()

    def test_fresh_account_acceptance_refuses_clock_rollback_without_consuming_a_receipt(self):
        self.fixture.base.stamp = self.grant.claimed_at_ms - 1
        with self.assertRaisesRegex(AccountError, 'claim_terminal_unavailable'):
            self.call()
        self.assertEqual(0, self.count())
        self.assertEqual((self.delivery,), self.fixture.store.pending_terminals())
        self.held()

    def test_transient_history_failure_keeps_its_fixed_operational_reason(self):
        with mock.patch.object(self.fixture.store, 'record', side_effect=HistoryError('history_unavailable')):
            with self.assertRaisesRegex(HistoryError, 'history_unavailable'):
                self.fixture.store.attest_terminal_delivery(self.binding)
        self.assertEqual((self.delivery,), self.fixture.store.pending_terminals())
        self.assertEqual(0, self.count())


if __name__ == '__main__':
    unittest.main()
