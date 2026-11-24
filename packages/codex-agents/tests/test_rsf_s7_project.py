"""RSF-S7-3: authenticated project replies and permanent identities."""
from dataclasses import replace
import json
import unittest

from tests.rsf_s7_support import ProjectFixture
from codex_agents.history_contracts import HistoryError
from codex_agents import project_holds_schema
from codex_agents.lost_run_contracts import SettleLostInput


class ProjectReplayTests(ProjectFixture):
    def test_settled_request_refuses_an_altered_digest_before_pruning(self):
        self.bound()
        self.open()
        self.record_settlement()
        before = self.snapshot()
        payload = {**self.payload(), 'identity_digest': 'e' * 64}
        with self.assertRaises(HistoryError):
            self.store.open_account_settlement(*self.attempt(), self.request_id(), payload, runtime_digest='a' * 64)
        self.assertEqual(before, self.snapshot())

    def test_bound_and_unbound_originals_replay_after_upgrade_and_pruning(self):
        for bound in (True, False):
            with self.subTest(bound=bound):
                # Separate fixture per case, preserving every original assertion.
                case = ProjectFixture(methodName='runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                (case.bound if bound else case.armed)()
                case.open()
                raw = case.closure()
                first = case.record_settlement(raw=raw)
                settled = case.open(arbitrate=False)
                case.migrate()
                case.prune()
                case.reopen()
                case.reopen()
                for table in ('project_account_settlements', 'result_leases', 'result_reservations',
                              'project_result_contexts', 'project_result_publications'):
                    self.assertEqual(0, case.store._connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])
                before = case.snapshot()
                for _ in range(2):
                    # Payload is retained by the caller, not reconstructed from expired rows.
                    closure = json.loads(raw)
                    payload = SettleLostInput(case.lease().run_handle, 'd' * 64, case.lease().claim_request_id,
                        closure['bridge_run_id'], closure['lease_digest']).payload()
                    self.assertEqual(settled, case.store.open_account_settlement(*case.attempt(), case.request_id(),
                        payload, runtime_digest='a' * 64))
                    self.assertEqual({**first, 'known': True}, case.store.record_account_settlement(case.lease(), raw))
                for changes in ({'identity_digest': 'e' * 64}, {'lease_digest': 'f' * 64}):
                    with self.assertRaises(HistoryError):
                        case.store.open_account_settlement(*case.attempt(), case.request_id(), {**payload, **changes},
                                                           runtime_digest='a' * 64)
                with self.assertRaises(HistoryError):
                    case.store.open_account_settlement(case.attempt()[0], 2, case.request_id(), payload,
                                                       runtime_digest='a' * 64)
                with self.assertRaises(HistoryError):
                    case.store.open_account_settlement(*case.attempt(), 'different-request', payload,
                                                       runtime_digest='a' * 64)
                with self.assertRaises(HistoryError):
                    case.store.record_account_settlement(replace(case.lease(), supervisor_generation=2), raw)
                with self.assertRaises(HistoryError):
                    project_holds_schema.take_hold(case.store._connection, case.lease().project_id, 'lease',
                        project_holds_schema.attempt_identity(*case.attempt()), now_ms=case.now, lease_case='armed')
                self.assertEqual(before, case.snapshot())

    def test_upgrade_then_prune_preserves_a_historical_settlement_reply(self):
        self.bound()
        self.open()
        payload = self.payload()
        raw = self.closure()
        first = self.record_settlement(raw=raw)
        answer = self.open(arbitrate=False)
        # Schema18/19/20 predecessor writers kept closure bytes in the settlement
        # table, but not on the hold. Reproduce that exact historical placement.
        with self.store._transaction():
            self.store._connection.execute("UPDATE project_holds SET evidence=NULL WHERE kind='reservation'")
        before = self.snapshot()
        self.migrate()
        self.assertEqual(before, self.snapshot())
        self.prune()
        self.reopen()
        before = self.snapshot()
        self.assertEqual(answer, self.store.open_account_settlement(*self.attempt(), self.request_id(), payload,
                                                                   runtime_digest='a' * 64))
        self.assertEqual({**first, 'known': True}, self.store.record_account_settlement(self.lease(), raw))
        self.assertEqual(before, self.snapshot())

    def test_pruned_bound_tombstone_without_replay_proof_is_refused(self):
        self.bound()
        self.open()
        payload = self.payload()
        self.record_settlement()
        self.prune()
        with self.store._transaction():
            self.store._connection.execute("UPDATE project_holds SET evidence=NULL WHERE kind='reservation'")
        self.reopen()
        before = self.snapshot()
        with self.assertRaises(HistoryError):
            self.store.open_account_settlement(*self.attempt(), self.request_id(), payload, runtime_digest='a' * 64)
        self.assertEqual(before, self.snapshot())
