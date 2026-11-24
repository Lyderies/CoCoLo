"""RSF-S7-1/2: permanent account replies through migration and payload pruning."""
from dataclasses import replace
import unittest

from tests.rsf_s7_support import AccountFixture
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.account_lost_run import settle_lost_run, lost_run_closure
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LedgerError


class BindingReplayTests(AccountFixture):
    def test_original_withdrawal_survives_migration_pruning_and_reopen(self):
        options = dict(run_handle=self.handle, request_digest='a' * 64, generation=1)
        first = self.ledger.withdraw_open(self.project, **options)
        self.migrate()
        self.prune()
        self.reopen()
        self.reopen()
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM host_run_bindings').fetchone()[0])
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual({**first, 'known': True}, self.ledger.withdraw_open(self.project, **options))
        for changes in ({'generation': 2}, {'generation': 3}, {'request_digest': 'b' * 64}):
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                self.ledger.withdraw_open(self.project, **{**options, **changes})
        self.assertEqual(before, self.snapshot())
        self.assertEqual([(2, 'released', 'withdrawn')], [tuple(r) for r in self.ledger._connection.execute(
            "SELECT generation,state,release_kind FROM run_holds WHERE kind='binding'")])

    def test_authenticated_owner_replay_refuses_changed_caller_and_generation(self):
        owner = self.withdrawal_owner()
        options = dict(run_handle=self.handle, request_digest='a' * 64, generation=1, owner=owner)
        first = self.ledger.withdraw_open(self.project, **options)
        self.migrate()
        self.prune()
        self.reopen()
        before = self.snapshot()
        self.assertEqual({**first, 'known': True}, self.ledger.withdraw_open(self.project, **options))
        for changes in ({'generation': 2}, {'request_digest': 'b' * 64},
                        {'owner': {**owner, 'caller_scope': 'b' * 64}}, {'owner': {**owner, 'attempt': 2}},
                        {'owner': {**owner, 'inputs_digest': '9' * 64}}):
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                self.ledger.withdraw_open(self.project, **{**options, **changes})
        self.assertEqual(before, self.snapshot())

    def test_legacy_digest_only_retry_still_returns_original_receipt(self):
        options = dict(run_handle=self.handle, request_digest='a' * 64, generation=1)
        first = self.ledger.withdraw_open(self.project, **options)
        self.prune()
        self.reopen()
        before = self.snapshot()
        self.assertEqual({**first, 'known': True}, self.ledger.withdraw_open(
            self.project, **{**options, 'generation': None}))
        self.assertEqual(before, self.snapshot())


class SettlementReplayTests(AccountFixture):
    def test_original_sealed_settlement_and_closure_survive_migration_and_pruning(self):
        self.claimed()
        options = self.settle_options()
        first = settle_lost_run(self.ledger, self.project, **options)
        closure = canonical_json(lost_run_closure(self.ledger, self.project, **options, request_digest='d' * 64))
        self.migrate()
        self.prune()
        self.reopen()
        self.reopen()
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM host_run_bindings').fetchone()[0])
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual({**first, 'known': True}, settle_lost_run(self.ledger, self.project, **options))
            self.assertEqual(closure, canonical_json(lost_run_closure(
                self.ledger, self.project, **options, request_digest='d' * 64)))
        for changes in ({'settle': replace(self.settle, identity_digest='e' * 64)},
                        {'storage_binding': replace(self.binding, supervisor_generation=2)},
                        {'caller_scope': 'b' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ClaimError):
                settle_lost_run(self.ledger, self.project, **{**options, **changes})
        self.assertEqual(before, self.snapshot())
        self.assertEqual([('released', 'lost', 2)], [tuple(r) for r in self.ledger._connection.execute(
            "SELECT state,release_kind,generation FROM run_holds WHERE kind='run_slot'")])
