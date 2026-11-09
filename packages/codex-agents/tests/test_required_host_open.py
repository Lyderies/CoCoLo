"""Original open is mandatory for fresh managed execution; fake native peers."""
from copy import deepcopy
from unittest import mock
import unittest

import tests.support  # noqa: F401
from tests import test_run_provenance as fixtures
from tests import test_account_claim_storage as storage_fixtures
from tests import test_account_claim_jobs as job_fixtures
from tests.test_bridge import result

from codex_agents.account_api import AccountError, decode_account_response
from codex_agents.history_contracts import HistoryError
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultReference
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.orchestration_api import OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.project_history import _ExecutionProvenanceRejected
from codex_agents.result_contracts import ResultObservation
from codex_agents.run_provenance import seal_run_provenance


class RequiredHostOpenTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.RunProvenanceTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.store = self.base.managed_project(record_host_open=False)
        self.grant = self.base.grant
        self.proof = self.base.proof()
        self.request = OpenProvenanceInput(self.fixture.scope.run_handle, self.fixture.assigned['assignment_id'])
        self.open_proof = self.base.base.ledger._attest_open_provenance(self.fixture.project,
            runtime_digest=self.fixture.scope.runtime_digest, caller_scope='a'*64, request=self.request,
            storage_binding=self.fixture.scope, storage_evidence=self.fixture.evidence, work_limits=self.base.limits)

    def persist_open(self):
        return self.store.record_host_open(self.fixture.scope, self.request, self.open_proof,
            assignment=self.fixture.assigned, task=self.fixture.base.fixture.fixture.task(),
            route=self.fixture.base.fixture.route, before_commit=lambda: None)

    def consume(self, proof=None):
        return self.base.base.consume(self.grant, provenance=self.proof if proof is None else proof)

    def unconsumed(self):
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='execution_provenance'").fetchone()[0])
        self.assertEqual('reserved', self.store.result_reservation(self.grant.binding).state)

    def test_missing_original_open_cannot_consume_or_invent_a_companion(self):
        before = self.base.base.ledger._connection.total_changes
        with self.assertRaises(_ExecutionProvenanceRejected):
            self.consume()
        self.unconsumed()
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])
        self.assertEqual(before, self.base.base.ledger._connection.total_changes)

    def test_actual_managed_project_cannot_use_legacy_mode_to_omit_all_provenance(self):
        self.persist_open()
        self.store._allow_legacy_orchestration_reference = True
        with self.assertRaises(_ExecutionProvenanceRejected):
            self.base.base.consume(self.grant)
        self.unconsumed()
        self.assertEqual(self.grant, self.consume())

    def test_orphaned_original_record_is_not_reconstructed_during_consumption(self):
        entry = self.persist_open()
        self.store._connection.execute('DELETE FROM project_host_opens')
        with self.assertRaises(_ExecutionProvenanceRejected):
            self.consume()
        self.unconsumed()
        self.assertEqual(entry, self.store.record(entry.record_id))

    def test_corrupt_journal_seal_refuses_without_consuming_or_replacing_the_record(self):
        entry = self.persist_open()
        self.store._connection.execute('UPDATE project_host_opens SET journal_seal=?', ('f'*64,))
        with self.assertRaises(_ExecutionProvenanceRejected):
            self.consume()
        self.unconsumed()
        self.assertEqual(entry, self.store.record(entry.record_id))

    def test_validly_sealed_registration_cannot_disagree_with_original_admission_or_start(self):
        self.persist_open()
        for name in ('admission_content_digest', 'start_content_digest'):
            facts = deepcopy(self.proof['provenance'])
            facts[name] = 'f'*64
            with self.subTest(name=name), self.assertRaises(_ExecutionProvenanceRejected):
                self.consume(seal_run_provenance(self.fixture.key, facts))
            self.unconsumed()
        self.assertEqual(self.grant, self.consume())
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])

    def test_known_original_metadata_uses_no_new_clock_or_window(self):
        self.persist_open()
        self.consume()
        with mock.patch.object(self.store, '_result_clock', side_effect=AssertionError('fresh clock')):
            self.assertEqual(self.proof, self.store._execution_provenance(self.grant))
        with self.assertRaisesRegex(HistoryError, 'history_execution_consumed'):
            self.consume()

    def test_missing_original_open_refuses_known_host_receipt_and_close_without_acceptance(self):
        self.persist_open()
        self.consume()
        source = self.fixture.base.fixture
        delivered = self.store.publish_result(self.grant.binding, self.fixture.assigned, canonical_json(result()),
            ResultObservation(self.grant.binding.claim_id, self.fixture.base.stamp, False, True, None))
        reference = HostResultReference(self.grant.binding, delivered.record_id,
            delivered.record_content_digest, delivered.result_digest)
        options = dict(assignment=self.fixture.assigned, task=source.fixture.task(), route=source.route,
            client_capability=source.fixture.clients['bridge-v1'], before_commit=lambda: None)
        receipt = self.store.admit_host_result(reference, **options)
        self.store._connection.execute('DELETE FROM project_host_opens')
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.store.admit_host_result(reference, **options)
        scope = OrchestrationScope(**{n: getattr(self.grant.binding, n) for n in SCOPE_FIELDS})
        close = OrchestrationRequest(1, 'orchestration.close', 'close-missing-open', scope,
            {'orchestration_handle': self.grant.binding.orchestration_handle, 'expected_sequence': 1,
             'status': 'completed', 'goal_tokens_used': None, 'goal_time_used_seconds': None})
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            OrchestrationOwner(self.store).apply(close, client_version='codex-host-v1', before_commit=lambda: None)
        self.assertEqual(receipt['admission']['host_outcome_digest'], self.store.record(
            receipt['admission']['outcome_record_id']).content_digest)
        OrchestrationOwner(self.store).active_for(self.grant.binding)


class ClaimOriginalWindowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = storage_fixtures.AccountClaimStorageTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.arm()
        self.ledger = self.fixture.ledger
        self.handle = keyed_digest(self.fixture.key, self.fixture.scope.run_handle)

    def window(self, remaining):
        deadline = self.fixture.base.stamp + remaining
        self.ledger._connection.execute('UPDATE host_run_bindings SET observation_deadline_at_ms=? '
            'WHERE project_id=? AND handle_digest=?', (deadline, self.fixture.project, self.handle))
        return deadline

    def test_actual_claim_wall_and_monotonic_deadlines_use_original_remaining_window(self):
        deadline = self.window(30_000)
        claimed = self.fixture.call()
        self.assertEqual('claimed', claimed['state'])
        self.assertEqual(deadline, claimed['deadline_at_ms'])
        row = self.ledger._connection.execute('SELECT * FROM active_run_capacity WHERE project_id=? AND handle_digest=?',
            (self.fixture.project, self.handle)).fetchone()
        self.assertEqual(deadline, row['deadline_at_ms'])
        self.assertEqual(30_000_000_000, row['execution_deadline_ns']-row['clock_reference_ns'])
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known clock refreshed')):
            self.assertEqual({**claimed, 'known': True}, self.fixture.call())

    def test_expired_original_window_denies_without_claim_or_capacity(self):
        self.fixture.base.stamp = self.window(1)
        denied = self.fixture.call()
        self.assertEqual(('denied', 'claim_not_open'), (denied['state'], denied['reason_code']))
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])

    def test_missing_original_window_is_unknown_not_a_fresh_full_timeout(self):
        self.ledger._connection.execute('UPDATE host_run_bindings SET observation_deadline_at_ms=NULL '
            'WHERE project_id=? AND handle_digest=?', (self.fixture.project, self.handle))
        with self.assertRaisesRegex(AccountError, 'claim_unavailable'):
            self.fixture.call()
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])


@unittest.skipUnless(job_fixtures.MACOS_WORKER, 'fixed macOS worker guard; no Linux policy selected')
class DeferredClaimOriginalWindowTests(unittest.TestCase):
    def setUp(self):
        self.base = job_fixtures.AccountClaimJobTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.addCleanup(self.base.tearDown)
        self.handle = keyed_digest(self.base.ledger.state.digest_key(), self.base.launch.run_handle)

    def test_actual_returned_signature_job_uses_remaining_original_window(self):
        deadline = self.base.fixture.stamp + 30_000
        self.base.ledger._connection.execute('UPDATE host_run_bindings SET observation_deadline_at_ms=? '
            'WHERE project_id=? AND handle_digest=?', (deadline, self.base.project, self.handle))
        ticket = self.base.begin()
        claimed = decode_account_response(self.base.pump(ticket), self.base.request())
        self.assertEqual(('claimed', deadline), (claimed['state'], claimed['deadline_at_ms']))

    def test_window_changed_during_signature_preparation_is_stale_without_claim(self):
        gate, started, patcher = self.base.gate_first_project()
        with patcher:
            ticket = self.base.begin()
            self.base.jobs.drive()
            self.assertTrue(started.wait(2))
            self.base.ledger._connection.execute('UPDATE host_run_bindings SET '
                'observation_deadline_at_ms=observation_deadline_at_ms-1 WHERE project_id=? AND handle_digest=?',
                (self.base.project, self.handle))
            gate.set()
            self.base.assert_wire_error(self.base.pump(ticket), self.base.request(), 'claim_preparation_stale')
        self.base.fixture.assert_no_claim()
