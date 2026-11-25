"""Actual durable denial/fence/lease recovery; synthetic enrollment, no model."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_storage as fixtures

from codex_agents import account_api, claim_denial
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, encode_account_request
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.claim_denial import CloseDeniedInput, denial_record_id, verify_denial_closure
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, validate_learning_record
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.service_transport import ServiceTransportError
from codex_agents import sql_read_budget
from codex_agents.sql_read_budget import SQLReadConnection, SQLReadError, SQLReadLimits


class ClaimDenialRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimStorageTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.lease = self.fixture.arm()
        self.attestations = self.ledger._connection.execute('SELECT * FROM owner_attestations').fetchall()
        self.ledger._connection.execute('DELETE FROM owner_attestations')
        self.denial = self.fixture.call()
        self.assertEqual('denied', self.denial['state'])
        self.assertEqual(self.fixture.scope.run_id, self.denial['run_id'])
        self.closure = CloseDeniedInput(self.fixture.scope.run_handle, self.fixture.base.launch.identity_digest,
                                       self.fixture.request.request_id, self.denial['receipt_digest'], self.lease.lease_digest)
        self.request = AccountRequest(1, 'account.close_denied_attempt', 'close-denied', self.fixture.project,
                                      COLLECTOR_RUNTIME_DIGEST, self.closure.payload())
        self.expected, self.evidence, self.source = self.fixture.scope, self.fixture.evidence, 'project'
        self.caller_scope = 'a' * 64
        self.service = self.make_service()

    @property
    def ledger(self):
        return self.fixture.base.ledger

    def make_service(self):
        def authorize(request, context):
            if context is self.fixture.base.token:
                return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                            self.source, frozenset({'account.close_denied_attempt'}), self.caller_scope,
                                            self.expected, self.evidence)
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(service.close)
        return service

    def call(self, request=None):
        return self.fixture.base.call(request or self.request, service=self.service)

    def state(self):
        return self.ledger._connection.execute('SELECT binding_state,abandoned_reason FROM host_run_bindings WHERE run_id=?',
                                               (self.fixture.scope.run_id,)).fetchone()

    def settlements(self):
        return self.ledger._connection.execute("SELECT * FROM records WHERE record_type='claim_denial_settlement'").fetchall()

    def pending(self):
        self.assertEqual('pending', self.state()[0])
        self.assertEqual('claim_pending', self.fixture.store.result_lease(self.fixture.scope).state)
        self.assertEqual([], self.settlements())

    def restore_attestations(self):
        for row in self.attestations:
            self.ledger._connection.execute('INSERT INTO owner_attestations VALUES(' + ','.join('?' for _ in row) + ')', tuple(row))

    def compete(self):
        return self.fixture.base.claim('competing-request')  # Explicit legacy owner reference, no model invocation.

    def legacy_intent(self, *, record_id=None, run_id=None):
        route = self.fixture.base.fixture.route
        data = {'schema_version': 1, 'record_type': 'dispatch_intent', 'run_id': run_id or self.fixture.scope.run_id,
                'bridge_run_id': 'brg_' + 'f' * 32, 'provider': route['provider'], 'model': route['model'],
                'effort': route['effort'], 'deadline_at_ms': self.fixture.base.stamp + 10_000,
                'observed_at_ms': self.fixture.base.stamp}
        data['record_id'] = record_id or 'dispatch_intent:' + content_digest(data)
        data = validate_learning_record(data)
        with self.ledger._account_transaction():
            self.ledger._append_checked_locked(self.fixture.project, data, producer='lifecycle', timestamp=self.fixture.base.stamp)

    def test_C3f_only_a_sweep_fence_takes_an_earlier_runtime(self):
        # C3f: the account fences an earlier version's claim request under that request's own
        # runtime only for a fence that names no identity; a claim, or anything else, under
        # another runtime is refused as before, and so is a fence that claims the exception for
        # this account's own runtime.
        from codex_agents.account_claim_contracts import ClaimError, LaunchClaimInput
        launch = LaunchClaimInput(self.fixture.scope.run_handle, self.fixture.base.launch.identity_digest)
        for runtime, earlier in (('e' * 64, False), (COLLECTOR_RUNTIME_DIGEST, True)):
            with self.subTest(runtime=runtime[:4], earlier=earlier), self.assertRaisesRegex(ClaimError, 'claim_invalid_request'):
                self.ledger._claim_preflight(self.fixture.project, runtime_digest=runtime, caller_scope='a' * 64,
                                             request_id='claim-other-runtime', launch=launch, earlier_runtime=earlier)
        self.pending()

    def test_legacy_intent_without_launch_marker_is_unknown_not_an_unused_attempt(self):
        self.legacy_intent()
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        with self.assertRaisesRegex(AccountError, 'claim_denial_unavailable'):
            self.call()
        self.pending()

    def test_global_record_namespace_collision_never_fences_or_repairs_existing_content(self):
        identity = denial_record_id(self.fixture.key, self.fixture.project, self.lease.lease_digest)
        self.legacy_intent(record_id=identity, run_id='run:' + 'f' * 64)
        before = self.ledger._connection.execute('SELECT content FROM records WHERE record_id=?', (identity,)).fetchone()[0]
        with self.assertRaisesRegex(AccountError, 'claim_denial_conflict'):
            self.call()
        self.assertEqual(before, self.ledger._connection.execute('SELECT content FROM records WHERE record_id=?', (identity,)).fetchone()[0])
        self.pending()

    def test_actual_legacy_scan_budget_exhaustion_rolls_back_and_can_retry_after_scope_restoration(self):
        for number in range(100):
            self.legacy_intent(run_id='run:' + f'{number:064x}')
        budget = (1, 1000, 16_384)
        # U3: the legacy read seeks this run's identity, so unrelated history
        # never fills even one 256-step callback. To exhaust the actual read,
        # only that read counts a callback per VM step; its limits are unchanged.
        original, refused = SQLReadConnection.transaction_read_call, []
        def per_step(connection, limits, operation):
            if limits != SQLReadLimits(*budget):
                return original(connection, limits, operation)
            with mock.patch.object(sql_read_budget, 'SQL_READ_PROGRESS_INTERVAL', 1):
                try:
                    return original(connection, limits, operation)
                except SQLReadError as error:
                    refused.append(error.reason_code)
                    raise
        with mock.patch.object(claim_denial, 'DENIAL_LEGACY_READ_LIMITS', budget), \
                mock.patch.object(SQLReadConnection, 'transaction_read_call', per_step):
            with self.assertRaisesRegex(AccountError, 'claim_busy'):
                self.call()
        self.assertEqual(['sql_read_work_limit'], refused)
        self.pending()
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(0, self.ledger._connection.execute('PRAGMA query_only').fetchone()[0])
        # The retry fits the same smallest budget at the normal granularity,
        # whatever the unrelated history (U3).
        with mock.patch.object(claim_denial, 'DENIAL_LEGACY_READ_LIMITS', budget):
            self.assertFalse(self.call()['known'])

    def earlier_dispatch_results(self, count, content_bytes):
        """`count` earlier dispatch results of this project, written as 0.2.x wrote them (0.3.3 C2)."""
        import hashlib
        stamp = self.fixture.base.stamp
        sequence = self.ledger._connection.execute("SELECT MAX(source_sequence) FROM records").fetchone()[0] or 0
        rows = []
        for index in range(count):
            content = json.dumps({'record_type': 'dispatch_result',
                                  'run_id': 'run:' + hashlib.sha256(f'earlier-{index}'.encode()).hexdigest(),
                                  'summary': 'x' * content_bytes}, separators=(',', ':')).encode()
            digest = hashlib.sha256(content).hexdigest()
            observed = stamp - 40 * 86_400_000 + index
            rows.append((self.fixture.project, 'dispatch_result:' + digest, 'dispatch_result', 'lifecycle',
                         sequence + 1 + index, observed, observed, observed + 90 * 86_400_000, content, digest))
        with self.ledger._lock:
            self.ledger._connection.executemany(
                "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
                "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)", rows)
            self.ledger._connection.commit()

    def assert_closed(self):
        self.assertFalse(self.call()['known'])
        self.assertEqual(('abandoned', 'claim_denied'), tuple(self.state()))

    def test_C2_a_refused_claim_closes_whatever_the_number_of_earlier_records(self):
        """0.3.3 C2: 150,000 small earlier records; the old budget refused the closure for good."""
        self.earlier_dispatch_results(150_000, 64)
        # U3: the read seeks the run's identity, so it closes even under the old budget.
        with mock.patch.object(claim_denial, 'DENIAL_LEGACY_READ_LIMITS', (2000, 50, 2 * 1_048_576)):
            self.assert_closed()

    def test_C2_a_refused_claim_closes_whatever_the_size_of_an_earlier_record(self):
        """0.3.3 C2: one earlier record over the old 16 KiB value limit."""
        self.earlier_dispatch_results(1, 20_000)
        # U3: the read never fetches an earlier record's content, so it closes even under the old value limit.
        with mock.patch.object(claim_denial, 'DENIAL_LEGACY_READ_LIMITS', (2000, 50, 16_384)):
            self.assert_closed()

    def test_fence_commits_before_body_charge_retires_and_same_identity_never_rearms(self):
        self.fixture.store._result_capacity = ResultCapacity(8, MAX_RESULT_BODY_BYTES, 1_800_000, 120_000)
        other = replace(self.fixture.scope, run_handle='hrn_' + 'f' * 48, run_id='run:' + 'f' * 64,
                        claim_request_id='other', claim_request_digest='f' * 64)
        with self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
            self.fixture.store.reserve_result_lease(other, self.fixture.assigned)
        closed = self.call()
        self.assertFalse(closed['known'])
        self.assertEqual(('abandoned', 'claim_denied'), tuple(self.state()))
        self.assertIsNone(self.settlements()[0]['expires_at_ms'])
        with self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
            self.fixture.store.reserve_result_lease(other, self.fixture.assigned)
        value = self.fixture.store.settle_denied_result_lease(self.fixture.scope, canonical_json(closed))
        self.assertEqual('expired', value.state)
        self.assertEqual(self.lease.reserved_until_ms, value.reserved_until_ms)
        self.assertEqual(value, self.fixture.store.reserve_result_lease(self.fixture.scope, self.fixture.assigned))
        with self.assertRaises(HistoryError):
            self.fixture.store.arm_result_lease(self.fixture.scope)
        self.assertEqual('reserved', self.fixture.store.reserve_result_lease(other, self.fixture.assigned).state)
        self.restore_attestations()
        self.assertEqual('denied', self.compete()['state'])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))

    def test_unknown_reply_or_no_durable_denial_never_creates_a_settlement(self):
        with mock.patch.object(self.ledger, '_known_claim_receipt_locked', return_value=None), \
                mock.patch.object(self.ledger, '_claim_commit', side_effect=AssertionError('new claim during recovery')):
            with self.assertRaisesRegex(AccountError, 'claim_denial_unavailable'):
                self.call()
        self.pending()

    def test_claim_wins_race_then_denial_closure_refuses_and_retains_pending_charge(self):
        original = self.ledger._account_transaction
        claims = []

        @contextmanager
        def claim_first():
            if not claims:
                claims.append(None)
                self.restore_attestations()
                claims[0] = self.compete()
            with original():
                yield

        with mock.patch.object(self.ledger, '_account_transaction', new=claim_first), \
                self.assertRaisesRegex(AccountError, 'claim_denial_unavailable'):
            self.call()
        self.assertEqual('claimed', claims[0]['state'])
        self.pending()
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0])

    def test_missing_or_foreign_proof_or_native_bound_run_cannot_fence(self):
        self.evidence = None
        with self.assertRaisesRegex(AccountError, 'claim_storage_required'):
            self.call()
        self.evidence = self.fixture.evidence
        self.expected = replace(self.expected, supervisor_generation=2)
        with self.assertRaises(AccountError):
            self.call()
        self.expected = self.fixture.scope
        self.ledger._connection.execute('UPDATE host_run_bindings SET native_assignment_id=? WHERE run_id=?',
                                        ('PRIVATE-NATIVE-BOUND', self.fixture.scope.run_id))
        with self.assertRaisesRegex(AccountError, 'claim_denial_unavailable'):
            self.call()
        self.pending()

    def test_only_project_context_and_closed_metadata_are_accepted(self):
        for source in ('host', 'owner', 'hook', 'app_server'):
            self.source = source
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.source = 'project'
        self.caller_scope = 'b' * 64
        with self.assertRaisesRegex(AccountError, 'claim_denial_unavailable'):
            self.call()
        self.caller_scope = 'a' * 64
        for extra in ({'not_sent': True}, {'cleanup_confirmed': True}, {'pid': 1}, {'storage_evidence': 'PRIVATE'}):
            raw = json.loads(encode_account_request(self.request))
            raw['payload'].update(extra)
            reply = json.loads(self.service.handle(canonical_json(raw), self.fixture.base.token))
            self.assertEqual('account_invalid_request', reply['reason_code'])
            self.assertNotIn('PRIVATE', str(reply))
        self.pending()

    def test_account_rollback_keeps_unfenced_pending_lease(self):
        original = self.ledger._account_transaction

        @contextmanager
        def fail_commit():
            with original():
                yield
                raise OSError('PRIVATE-COMMIT-FAILURE')

        with mock.patch.object(self.ledger, '_account_transaction', new=fail_commit), \
                self.assertRaisesRegex(AccountError, 'account_unavailable'):
            self.call()
        self.pending()
        self.assertFalse(self.call()['known'])

    def test_lost_account_reply_reopens_as_exact_known_closure_without_new_proof_or_clock(self):
        original = self.ledger._account_transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-FENCE-REPLY')

        with mock.patch.object(self.ledger, '_account_transaction', new=lose_reply), \
                self.assertRaisesRegex(AccountError, 'account_unavailable'):
            self.call()
        closed = self.call()
        self.assertTrue(closed['known'])
        self.ledger.close()
        bridge = self.fixture.base.fixture.fixture
        self.fixture.base.ledger = LearningLedger(bridge.state, client_capabilities=bridge.clients)
        self.addCleanup(self.ledger.close)
        self.service = self.make_service()
        self.evidence = None
        with mock.patch.object(self.ledger, '_clock_ms', return_value=0), \
                mock.patch.object(self.ledger, '_prepare_attestations', side_effect=AssertionError('fresh signatures')):
            self.assertEqual(closed, self.call())
        self.fixture.store.settle_denied_result_lease(self.fixture.scope, canonical_json(closed))
        self.assertEqual(1, len(self.settlements()))

    def test_lost_local_settlement_reply_reopens_and_retries_without_time_or_capacity_refresh(self):
        raw = canonical_json(self.call())
        original = self.fixture.store._transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-LOCAL-REPLY')

        with mock.patch.object(self.fixture.store, '_transaction', new=lose_reply), \
                self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.fixture.store.settle_denied_result_lease(self.fixture.scope, raw)
        self.fixture.store.close()
        self.fixture.store = self.fixture.open_history()
        value = self.fixture.store.settle_denied_result_lease(self.fixture.scope, raw)
        self.assertEqual('expired', value.state)
        self.assertEqual(self.lease.lease_digest, value.lease_digest)
        self.assertEqual(self.lease.reserved_until_ms, value.reserved_until_ms)

    def test_expired_uncertain_lease_stays_charged_until_real_historical_denial_closes(self):
        self.fixture.base.stamp = self.lease.reserved_until_ms + 1
        self.assertEqual(0, self.fixture.store.expire_result_leases(limit=1))
        self.assertEqual('claim_pending', self.fixture.store.result_lease(self.fixture.scope).state)
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.fixture.store.attest_result_lease(self.fixture.scope)
        self.assertEqual(self.fixture.evidence, self.fixture.store.attest_pending_result_lease(self.fixture.scope))
        closed = self.call()
        self.fixture.store.settle_denied_result_lease(self.fixture.scope, canonical_json(closed))
        self.assertEqual('expired', self.fixture.store.result_lease(self.fixture.scope).state)

    def test_forged_or_changed_closure_never_retires_pending_body_charge(self):
        closed = self.call()
        for change in ({'receipt_digest': 'f' * 64}, {'lease_digest': 'f' * 64}, {'not_sent': True}, {'known': 1}):
            raw = canonical_json({**closed, **change})
            with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                self.fixture.store.settle_denied_result_lease(self.fixture.scope, raw)
            self.assertEqual('claim_pending', self.fixture.store.result_lease(self.fixture.scope).state)
        self.assertEqual(closed, verify_denial_closure(self.fixture.key, self.lease, canonical_json(closed)))

    def test_known_closure_conflicts_when_actor_lease_or_immutable_record_changes(self):
        closed = self.call()
        self.expected = replace(self.expected, primary_session_id='foreign')
        with self.assertRaisesRegex(AccountError, 'claim_denial_conflict'):
            self.call()
        self.expected = self.fixture.scope
        self.ledger._connection.execute("UPDATE host_run_bindings SET binding_state='pending',abandoned_reason=NULL WHERE run_id=?",
                                        (self.fixture.scope.run_id,))
        with self.assertRaisesRegex(AccountError, 'claim_denial_conflict'):
            self.call()
        self.assertEqual('claim_pending', self.fixture.store.result_lease(self.fixture.scope).state)
        self.assertEqual(1, len(self.settlements()))
        self.assertFalse(closed['known'])

    def test_client_makes_one_exchange_and_preserves_ambiguous_fence_reply(self):
        client = AccountClient(Path('/private/tmp/fixture-account.sock'), self.fixture.project, COLLECTOR_RUNTIME_DIGEST)

        def exchange(path, raw, **options):
            self.assertEqual(account_api.MAX_ACCOUNT_FRAME_BYTES, options['response_limit'])
            self.service.handle(raw, self.fixture.base.token)
            raise ServiceTransportError('connection_failed')

        with mock.patch.object(account_api, 'exchange_frame', side_effect=exchange) as transport:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.close_denied_attempt(self.closure, request_id='client-close')
            self.assertEqual(1, transport.call_count)
        self.assertEqual('claim_pending', self.fixture.store.result_lease(self.fixture.scope).state)
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lambda path, raw, **kw: self.service.handle(raw, self.fixture.base.token)):
            self.assertTrue(client.close_denied_attempt(self.closure, request_id='client-close')['known'])


if __name__ == '__main__':
    unittest.main()
