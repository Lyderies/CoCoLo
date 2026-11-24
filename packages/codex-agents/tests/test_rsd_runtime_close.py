"""RS-D-2 narrowing: changed runtime N1/N2, with real stores and account release."""
from dataclasses import replace
import unittest

import tests.support  # noqa: F401
from tests import test_record_context as rc

from codex_agents import project_holds_schema, project_cleanup, project_release_ack
from codex_agents.account_api import (AccountRequest, account_request_digest,
    encode_account_request, decode_account_response)
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.cleanup_contracts import CleanupEvidence, encode_cleanup_evidence
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.orchestration_api import OrchestrationScope, SCOPE_FIELDS
from codex_agents.project_history import RESULT_PURGE_GRACE_MS
from codex_agents.terminal_delivery import TerminalSubmissionInput

WHY = ('Real account handlers and project stores verify release evidence and custody; '
       'synthetic protected cleanup custody and a controlled clock expose close/deadline boundaries.')


def scope(binding):
    return OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})


def consistent(test, store, key):
    test.assertTrue(project_holds_schema.register_consistent(store._connection))
    test.assertTrue(project_holds_schema.custody_consistent(store._connection, key))


class RuntimeCloseFixture(rc.RecordedContextFixture):
    def changed(self):
        return replace(scope(self.binding), runtime_digest='f' * 64)

    def status(self):
        return self.store.runtime_close_status()

    def consistent(self):
        consistent(self, self.store, self.fixture.key)


class RuntimeCloseTests(RuntimeCloseFixture):
    @__import__('test_framework').requirements('RS-D-2-N1', why_not_scenario=WHY)
    def test_N1_recorded_closes_and_releases_custody_without_counting_unrecorded(self):
        self.recorded()
        owner = self.store._orchestration_owner()
        self.assertEqual(0, owner.end_earlier(scope(self.binding), 10))
        self.assertEqual(0, owner.end_earlier(replace(self.changed(), supervisor_generation=2), 10))
        self.assertEqual(1, owner.end_earlier(self.changed(), 10))
        self.assertEqual('interrupted', owner.metadata_for(self.binding)['snapshot']['status'])
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'orchestration_closed'), rc.custody(self.store, *self.attempt)[:2])
        self.assertEqual([], rc.context_files(self.directory))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())
        self.assertEqual(0, owner.end_earlier(self.changed(), 10))
        self.consistent()

    @__import__('test_framework').requirements('RS-D-2-N2', why_not_scenario=WHY)
    def test_N2_unrecorded_without_verified_release_stays_open_until_custody_deadline(self):
        self.open_and_consume()
        self.publish()
        due = self.store.result_reservation(self.binding).retained_until_ms
        counts = self.store._result_counts(), rc.live_context_bytes(self.store)
        self.reopen()
        owner = self.store._orchestration_owner()
        for now in (due - 1, due):
            self.fixture.base.stamp = now
            self.assertEqual(0, owner.end_earlier(self.changed(), 10))
            self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])
            if now < due:
                self.assertEqual(0, self.store.expire_results())
                self.assertEqual(self.body, self.store.read_result(self.binding))
                self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])
                self.assertEqual(counts, (self.store._result_counts(), rc.live_context_bytes(self.store)))
            else:
                self.assertEqual(1, self.store.expire_results())
                self.assertEqual(('released', 'retention'), rc.custody(self.store, *self.attempt)[:2])
        self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())
        self.assertEqual([], rc.context_files(self.directory))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.consistent()


class RuntimeCloseServiceTests(RuntimeCloseFixture):
    @__import__('test_framework').requirements('RS-D-2-N1', 'RS-D-2-N2', why_not_scenario=WHY)
    def test_N1_N2_real_release_then_update_counts_unrecorded_once_across_restart(self):
        self.open_and_consume()
        self.publish()
        execution = self.base.base
        token = object()
        terminal_proof = self.store.attest_terminal_delivery(self.binding)
        cleanup_raw = None

        def authorize(request, context):
            if context is token:
                return AccountAuthorization(request.project_id, request.runtime_digest,
                    account_request_digest(request), 'project',
                    frozenset({'account.submit_terminal', 'account.release_capacity'}), 'a' * 64,
                    self.fixture.scope, execution.evidence, terminal_proof,
                    cleanup_evidence=cleanup_raw)

        service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(execution.ledger),
                                 authorizer=authorize)
        self.addCleanup(service.close)
        submission = TerminalSubmissionInput(execution.registration, self.base.grant.registration_digest,
            self.delivery.record_id, self.delivery.record_content_digest, self.delivery.result_digest)

        def call(operation):
            request = AccountRequest(1, operation, operation, self.binding.project_id,
                                     COLLECTOR_RUNTIME_DIGEST, submission.payload())
            return decode_account_response(service.handle(encode_account_request(request), token), request)

        terminal = call('account.submit_terminal')
        self.store.acknowledge_account_terminal(self.binding, canonical_json(terminal))
        cleanup_raw = encode_cleanup_evidence(self.fixture.key, CleanupEvidence(self.fixture.scope,
            self.base.grant, self.delivery, terminal['receipt_digest']))
        receipt = call('account.release_capacity')
        self.assertEqual('released', receipt['state'])
        owner = self.store._orchestration_owner()
        # Account release alone is insufficient until the project has its verified acknowledgement.
        self.assertEqual(0, owner.end_earlier(self.changed(), 10))
        self.assertEqual(0, self.store.expire_results())
        self.assertEqual(self.body, self.store.read_result(self.binding))

        # Synthetic protected cleanup custody, as in test_account_cleanup_release. The account
        # service above and all release/close verification below are real and unpatched.
        store, db, lease_binding = self.store, self.store._connection, self.fixture.scope
        lease = store.result_lease(lease_binding)
        cleanup_digest = keyed_digest(self.fixture.key, cleanup_raw)
        ack_raw = canonical_json(receipt)
        ack_digest = keyed_digest(self.fixture.key, ack_raw)
        with store._transaction():
            db.execute('INSERT INTO project_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                (self.binding.project_id, *self.attempt, self.binding.claim_id, cleanup_raw, cleanup_digest,
                 project_cleanup._journal_seal(store, lease, self.binding.claim_id, cleanup_digest)))
            db.execute('UPDATE project_cleanup_reservations SET cleanup_digest=?,account_release_digest=?,state_digest=?',
                (cleanup_digest, ack_digest, project_cleanup._state_seal(store,
                    lease.cleanup_reservation_digest, cleanup_digest, account_release_digest=ack_digest)))
            db.execute('INSERT INTO project_cleanup_acknowledgements VALUES(?,?,?,?,?,?)',
                (self.binding.project_id, *self.attempt, ack_raw, ack_digest,
                 project_release_ack._journal_seal(store, lease, cleanup_digest, ack_digest)))
            project_holds_schema.release_hold(db, self.binding.project_id, 'reservation', self.binding.claim_id,
                'completed', now_ms=self.fixture.base.stamp, receipt=ack_digest)
        self.assertEqual(ack_raw, store.account_release_receipt(lease_binding))
        journal_seal = db.execute('SELECT journal_seal FROM project_cleanup_acknowledgements').fetchone()[0]
        db.execute("UPDATE project_cleanup_acknowledgements SET journal_seal=?", ('0' * 64,))
        self.assertEqual(0, owner.end_earlier(self.changed(), 10))
        db.execute('UPDATE project_cleanup_acknowledgements SET journal_seal=?', (journal_seal,))
        self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])
        self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])
        # Failure to persist the status fact rolls back the close too; the sweep can retry.
        db.execute("CREATE TEMP TRIGGER refuse_update_close BEFORE INSERT ON project_holds "
                   "WHEN NEW.kind='orchestration' BEGIN SELECT RAISE(ABORT,'fixture close cut'); END")
        self.assertEqual(0, owner.end_earlier(self.changed(), 10))
        self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])
        self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())
        db.execute('DROP TRIGGER refuse_update_close')
        self.assertEqual(1, owner.end_earlier(self.changed(), 10))
        self.assertEqual('interrupted', owner.metadata_for(self.binding)['snapshot']['status'])
        self.assertEqual(1, store.expire_results())
        self.assertEqual(('released', 'orchestration_closed'), rc.custody(store, *self.attempt)[:2])
        self.assertEqual({'ended_by_update_unrecorded': 1}, store.runtime_close_status())
        self.assertEqual(0, owner.end_earlier(self.changed(), 10))
        self.assertEqual([], rc.context_files(self.directory))
        self.assertEqual((0, 0, 0), store._result_counts())
        self.reopen()
        self.assertEqual({'ended_by_update_unrecorded': 1}, self.status())
        now = self.base.grant.deadline_at_ms + RESULT_PURGE_GRACE_MS + 1
        self.fixture.base.stamp = now
        self.assertEqual(1, self.store._purge_finished_results(now, 10))
        self.assertEqual({'ended_by_update_unrecorded': 1}, self.status())
        self.consistent()


if __name__ == '__main__':
    unittest.main()
