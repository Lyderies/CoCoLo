"""RSF-S7-3: authenticated project replies and permanent identities."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

from tests.rsf_s7_support import ProjectFixture
from codex_agents.history_contracts import HistoryError
from codex_agents import project_holds_schema
from codex_agents.lost_run_contracts import SettleLostInput


class ReviewValidationTests(unittest.TestCase):
    def test_validation_does_not_cite_the_removed_regression_module(self):
        validation = Path(__file__).resolve().parents[3] / 'docs/requirements/rsf-s7/validation.md'
        self.assertNotIn('tests.test_rsf_s7_regressions', validation.read_text())


class ProjectReplayTests(ProjectFixture):
    def test_attempt_pruning_backfills_only_missing_or_adopted_hold_evidence(self):
        prior = project_holds_schema.evidence_json(prior='retained evidence')
        for bound in (True, False):
            for evidence in (prior, None, project_holds_schema.ADOPTED):
                with self.subTest(bound=bound, evidence=evidence):
                    case = ProjectFixture(methodName='runTest')
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    (case.bound if bound else case.armed)()
                    case.open()
                    raw = case.closure()
                    case.record_settlement(raw=raw)
                    kind = 'reservation' if bound else 'lease'
                    with case.store._transaction():
                        case.store._connection.execute(
                            'UPDATE project_holds SET evidence=? WHERE kind=?', (evidence, kind))
                    before = dict(case.store._connection.execute(
                        'SELECT * FROM project_holds WHERE kind=?', (kind,)).fetchone())
                    case.prune()
                    after = dict(case.store._connection.execute(
                        'SELECT * FROM project_holds WHERE kind=?', (kind,)).fetchone())
                    expected = prior if evidence == prior else raw.decode('ascii')
                    self.assertEqual({**before, 'evidence': expected}, after)
                    self.assertIsNone(case.settlement())

    def test_retained_hold_closure_has_an_inclusive_byte_ceiling_and_a_bounded_window(self):
        from codex_agents import lost_run_closure

        self.bound()
        self.open()
        raw = self.closure()
        self.assertEqual(8192, lost_run_closure.MAX_LOST_RUN_CLOSURE_BYTES)
        before = self.snapshot()
        # Move the same valid, authenticated closure across the inclusive limit;
        # this isolates the byte guard from JSON and authentication refusals.
        with mock.patch.object(lost_run_closure, 'MAX_LOST_RUN_CLOSURE_BYTES', len(raw) - 1):
            with self.assertRaises(HistoryError):
                self.record_settlement(raw=raw)
        self.assertEqual(before, self.snapshot())
        with mock.patch.object(lost_run_closure, 'MAX_LOST_RUN_CLOSURE_BYTES', len(raw)):
            first = self.record_settlement(raw=raw)
        self.prune()
        self.reopen()
        retained = self.store._connection.execute(
            "SELECT evidence FROM project_holds WHERE kind='reservation'").fetchone()[0]
        self.assertEqual(raw, retained.encode('ascii'))
        before = self.snapshot()
        oversized = raw + b' ' * (8193 - len(raw))
        with self.assertRaises(HistoryError):
            self.record_settlement(raw=oversized)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({**first, 'known': True}, self.record_settlement(raw=raw))

        registry = json.loads(Path(__file__).with_name('lifetime_caps.json').read_text())['sites']
        entry = registry.get('project_history.py::record_account_settlement', {})
        self.assertEqual(8192, entry.get('max_closure_bytes'))
        self.assertIn('BOUNDED evidence', entry['ends'])
        self.assertEqual(self._testMethodName, entry['test'])

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


class BoundedEvidenceReplayTests(ProjectFixture):
    def test_retained_closure_window_boundary_and_fixed_refusal(self):
        from codex_agents.launch_journal import TOMBSTONE_MS
        from codex_agents.result_contracts import MAX_RESULT_RETENTION_MS

        window = max(MAX_RESULT_RETENTION_MS, TOMBSTONE_MS) + TOMBSTONE_MS
        for bound in (True, False):
            with self.subTest(bound=bound):
                case = ProjectFixture(methodName='runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                (case.bound if bound else case.armed)()
                case.open()
                payload, raw = case.payload(), case.closure()
                first = case.record_settlement(raw=raw)
                answer = case.open(arbitrate=False)
                case.prune()
                kind = 'reservation' if bound else 'lease'

                def hold():
                    return dict(case.store._connection.execute(
                        'SELECT * FROM project_holds WHERE kind=?', (kind,)).fetchone())

                original = hold()
                deadline = original['released_at_ms'] + window
                for now in (deadline - 1, deadline):
                    case.now = now
                    case.store.expire_results(limit=1)
                    self.assertEqual(original, hold())
                    self.assertEqual(answer, case.store.open_account_settlement(
                        *case.attempt(), case.request_id(), payload, runtime_digest='a' * 64))
                    self.assertEqual({**first, 'known': True}, case.record_settlement(raw=raw))
                case.now = deadline + 1
                # Expiry refuses even before the scheduled clearing pass runs.
                for cleared in (False, True):
                    if cleared:
                        case.store.expire_results(limit=1)
                        self.assertEqual({**original, 'evidence': None}, hold())
                        case.reopen()
                        case.reopen()
                        case.now = deadline - 1  # the persisted expiry floor still applies
                    before = case.snapshot()
                    for replay in (
                        lambda: case.store.open_account_settlement(
                            *case.attempt(), case.request_id(), payload, runtime_digest='a' * 64),
                        lambda: case.record_settlement(raw=raw),
                    ):
                        with self.assertRaises(HistoryError) as caught:
                            replay()
                        self.assertEqual('history_result_not_reserved', caught.exception.reason_code)
                        self.assertEqual(before, case.snapshot())
                    with self.assertRaises(HistoryError):
                        project_holds_schema.take_hold(case.store._connection, original['project_id'],
                            original['kind'], original['identity'], now_ms=case.now)
                    self.assertEqual(before, case.snapshot())

        registry = json.loads(Path(__file__).with_name('lifetime_caps.json').read_text())['sites']
        entry = registry['project_history.py::record_account_settlement']
        self.assertEqual(8192, entry['max_closure_bytes'])
        self.assertEqual(self._testMethodName, entry['boundary_test'])
        self.assertIn('PURGE_SCAN_FACTOR', entry['ends'])
        self.assertIn('cleared', entry['ends'])

    def test_evidence_expiry_pass_is_bounded_and_eventually_clears_all_rows(self):
        from codex_agents.launch_journal import TOMBSTONE_MS
        from codex_agents.result_contracts import MAX_RESULT_RETENTION_MS
        from tests.test_publication_replay import claim_binding

        for number in range(1, 4):
            (self.bound if number % 2 else self.armed)(number)
            self.open(number)
            self.record_settlement(number)
            with self.store._transaction():
                self.store._delete_attempt_rows(*self.attempt(number),
                    claim_binding(number).claim_id if number % 2 else None)
        original = [dict(row) for row in self.store._connection.execute(
            'SELECT * FROM project_holds ORDER BY rowid')]
        closures = [row for row in original if row['release_kind'] in ('lost', 'lost_unbound')]
        self.assertEqual(3, len(closures))
        self.now = max(row['released_at_ms'] for row in closures) + max(
            MAX_RESULT_RETENTION_MS, TOMBSTONE_MS) + TOMBSTONE_MS + 1
        for cleared in range(1, 4):
            self.store.expire_results(limit=1)
            current = [dict(row) for row in self.store._connection.execute(
                'SELECT * FROM project_holds ORDER BY rowid')]
            self.assertEqual(len(original), len(current))
            self.assertEqual(cleared, sum(row['evidence'] is None for row in current
                                         if row['release_kind'] in ('lost', 'lost_unbound')))
            for before, after in zip(original, current):
                self.assertEqual({**before, 'evidence': after['evidence']}, after)
                if before['release_kind'] not in ('lost', 'lost_unbound'):
                    self.assertEqual(before, after)
        snapshot = self.snapshot()
        self.store.expire_results(limit=1)
        self.assertEqual(snapshot, self.snapshot())

    def test_expired_settlement_copy_cannot_answer_or_backfill_evidence(self):
        from codex_agents.launch_journal import TOMBSTONE_MS
        from codex_agents.result_contracts import MAX_RESULT_RETENTION_MS

        for bound in (True, False):
            with self.subTest(bound=bound):
                case = ProjectFixture(methodName='runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                (case.bound if bound else case.armed)()
                case.open()
                payload, raw = case.payload(), case.closure()
                case.record_settlement(raw=raw)
                kind = 'reservation' if bound else 'lease'
                released = case.store._connection.execute(
                    'SELECT released_at_ms FROM project_holds WHERE kind=?', (kind,)).fetchone()[0]
                with case.store._transaction():
                    case.store._connection.execute('UPDATE project_holds SET evidence=NULL WHERE kind=?', (kind,))
                case.now = released + max(MAX_RESULT_RETENTION_MS, TOMBSTONE_MS) + TOMBSTONE_MS + 1
                before = case.snapshot()
                for replay in (
                    lambda: case.store.open_account_settlement(
                        *case.attempt(), case.request_id(), payload, runtime_digest='a' * 64),
                    lambda: case.record_settlement(raw=raw),
                ):
                    with self.assertRaises(HistoryError) as caught:
                        replay()
                    self.assertEqual('history_result_not_reserved', caught.exception.reason_code)
                    self.assertEqual(before, case.snapshot())
                case.prune()
                self.assertIsNone(case.settlement())
                self.assertIsNone(case.store._connection.execute(
                    'SELECT evidence FROM project_holds WHERE kind=?', (kind,)).fetchone()[0])
