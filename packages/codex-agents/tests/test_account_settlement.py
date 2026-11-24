"""Slice B4s (RS-B-B4-1..7): lost-run settlement through the register outbox, at the project store.

REQUIREMENTS-account-settlement.md; ANALYSIS-0.4.0-rsb-b4-b7.md §2 (rows V1, V2, V4, V5, V6a, V6b, Q1, Q2, the planted
helper's equivalence and the validator rows). The account answers a lost or released run with a closure keyed with the
account's digest key; the project writer verifies it with its own result key (the same key, see KeyIdentityTests) and
records it in one transaction. Before any account call the attempt's register row is `releasing` with the outbox
request's id and digest (`open_account_settlement`); the identical request is re-sent after any stop.
Integration stage: real stores in temporary folders, the fixtures of the D2/D4 tests. The closure is built with the
product's own closure function and the fixtures' result key, as the account builds it with its digest key.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_publication_replay import ReplayFixture, claim_binding

from codex_agents import project_holds_schema
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json

KEY = leases.fixtures.KEY
RUNTIME = 'a' * 64
IDENTITY = 'd' * 64
WHY = ('B4s checks real project stores in temporary folders (leases, reservations, holds, contexts, settlements); the '
       'closure is the product\'s own, keyed as the account keys it; stops are store reopenings between steps.')
TABLES = ('project_holds', 'project_account_settlements', 'result_leases', 'result_reservations',
          'project_result_contexts', 'project_result_publications', 'terminal_outbox', 'project_execution_consumptions',
          'project_cleanup_receipts', 'records')


def requirements(*ids):
    return __import__('test_framework').requirements(*ids, why_not_scenario=WHY)


class SettlementFixture(ReplayFixture):
    def lease(self, number=1):
        return leases.lease_binding(number)

    def armed(self, number=1):
        """A claim sent and admitted, never bound: the lease stays `claim_pending`, no reservation."""
        from tests import test_record_context as rc
        self.store.reserve_result_lease(self.lease(number), assignment(), record_context=rc.simple_context(number))
        self.store.arm_result_lease(self.lease(number))

    def payload(self, number=1):
        from codex_agents.lost_run_contracts import SettleLostInput
        lease = self.store.result_lease(self.lease(number))
        return SettleLostInput(lease.binding.run_handle, IDENTITY, lease.binding.claim_request_id,
                               claim_binding(number).claim_id, lease.lease_digest).payload()

    def request_id(self, number=1):
        return 'lost-' + f'{number:032x}'

    def open(self, number=1, *, arbitrate=True):
        """The outbox step the driver runs before any account call (after D4's arbitration)."""
        target = claim_binding(number)
        if arbitrate:
            self.store.arbitrate_publication(target.run_handle, target.attempt, self.request_id(number))
        return self.store.open_account_settlement(target.run_handle, target.attempt, self.request_id(number),
                                                  self.payload(number), runtime_digest=RUNTIME)

    def closure(self, number=1, *, kind='lost', key=KEY, request_digest=None, released_at_ms=None, **changes):
        """The account's closure for this attempt, as `account.lost_run_closure` builds it."""
        from codex_agents.lost_run_closure import lost_run_closure
        lease = self.store.result_lease(self.lease(number))
        if request_digest is None:
            request_digest = self.store.open_account_settlement(
                lease.binding.run_handle, lease.binding.attempt, self.request_id(number), self.payload(number),
                runtime_digest=RUNTIME)['request_digest']
        options = dict(lease_digest=lease.lease_digest, assignment_digest=lease.assignment_digest,
                       bridge_run_id=claim_binding(number).claim_id, release_kind=kind,
                       released_at_ms=self.now + 5 if released_at_ms is None else released_at_ms,
                       request_digest=request_digest)
        options.update(changes)
        binding = options.pop('binding', lease.binding)
        return canonical_json(lost_run_closure(key, binding, **options))

    def record_settlement(self, number=1, raw=None, **closure):
        return self.store.record_account_settlement(self.lease(number), self.closure(number, **closure) if raw is None
                                                    else raw)

    def snapshot(self):
        connection = self.store._connection
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in TABLES}

    def hold(self, kind, identity):
        row = self.store._connection.execute(
            'SELECT state,release_kind,receipt,request_id,request_digest FROM project_holds WHERE kind=? AND identity=?',
            (kind, identity)).fetchone()
        return None if row is None else tuple(row)

    def settlement(self, number=1):
        target = claim_binding(number)
        row = self.store._connection.execute(
            'SELECT claim_id,release_kind,closure FROM project_account_settlements WHERE run_handle=? AND attempt=?',
            (target.run_handle, target.attempt)).fetchone()
        return None if row is None else (row[0], row[1], bytes(row[2]))

    def custody(self, number=1):
        from tests import test_record_context as rc
        row = rc.custody(self.store, *self.attempt(number))
        return None if row is None else tuple(row[:2])

    def counts(self):
        connection = self.store._connection
        return tuple(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
            'terminal_outbox', 'project_execution_consumptions', 'project_cleanup_receipts', 'records'))

    def consistent(self):
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, KEY))


@requirements('RS-B-B4')
class OutboxTests(SettlementFixture):
    """RS-B-B4-2: the register row is `releasing` with the outbox request's id and digest before any account call."""

    def test_B4_2_open_marks_the_reservation_releasing_with_the_request_id_and_digest(self):
        from codex_agents.account_api import ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest
        self.bound(1)
        opened = self.open(1)
        target = claim_binding(1)
        digest = account_request_digest(AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.lost_run_closure',
                                                       self.request_id(1), target.project_id, RUNTIME, self.payload(1)))
        self.assertEqual((self.request_id(1), digest), (opened['request_id'], opened['request_digest']))
        self.assertEqual(('releasing', None, None, self.request_id(1), digest), self.hold('reservation', target.claim_id))
        self.consistent()  # releasing without a settlement row is legal (RS-B-B4-7)

    def test_B4_2_a_lease_never_bound_carries_the_outbox_on_its_lease_row(self):
        self.armed(1)
        opened = self.open(1)
        identity = project_holds_schema.attempt_identity(*self.attempt(1))
        self.assertEqual(('releasing', None, None, self.request_id(1), opened['request_digest']),
                         self.hold('lease', identity))
        self.consistent()

    def test_B4_2_a_publishable_reservation_is_never_fenced_without_arbitration(self):
        self.bound(1)
        before = self.snapshot()
        with self.assertRaises(HistoryError) as caught:
            self.open(1, arbitrate=False)
        self.assertEqual('history_conflict', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())

    def test_Q1_after_releasing_a_successor_resends_the_identical_request(self):
        self.bound(1)
        first = self.open(1)
        self.successor()
        self.assertEqual(first['request_digest'], self.open(1)['request_digest'])
        self.record_settlement(1)
        self.assertEqual('lost', self.settlement(1)[1])

    def test_Q1_after_the_call_the_kept_closure_records_after_a_restart(self):
        self.bound(1)
        self.open(1)
        raw = self.closure(1)
        self.successor()
        self.open(1)
        receipt = self.store.record_account_settlement(self.lease(1), raw)
        self.assertEqual(('lost', False), (receipt['release_kind'], receipt['known']))

    def test_Q1_after_the_receipt_a_replay_is_known_and_writes_nothing(self):
        self.bound(1)
        self.open(1)
        raw = self.closure(1)
        first = self.store.record_account_settlement(self.lease(1), raw)
        self.successor()
        self.assertEqual('settling', self.store.arbitrate_publication(*self.attempt(1), self.request_id(1)))
        self.assertEqual('settled', self.open(1, arbitrate=False)['state'])
        before = self.snapshot()
        again = self.store.record_account_settlement(self.lease(1), raw)
        self.assertEqual((first['receipt_digest'], True), (again['receipt_digest'], again['known']))
        self.assertEqual(before, self.snapshot())

    def test_Q2_a_closure_naming_another_request_digest_is_refused_and_writes_nothing(self):
        self.bound(1)
        self.open(1)
        before = self.snapshot()
        with self.assertRaises(HistoryError) as caught:
            self.record_settlement(1, request_digest='e' * 64)
        self.assertEqual('history_conflict', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())


@requirements('RS-B-B4')
class RecordSettlementTests(SettlementFixture):
    """RS-B-B4-1 and RS-B-B4-3: the verified closure and its one writer transaction."""

    def test_V1_a_lost_unpublished_attempt_is_settled_in_one_transaction(self):
        self.bound(1)
        self.open(1)
        counts = self.counts()
        receipt = self.record_settlement(1)
        target = claim_binding(1)
        self.assertEqual(('lost', False), (receipt['release_kind'], receipt['known']))
        self.assertEqual((target.claim_id, 'lost'), self.settlement(1)[:2])
        self.assertEqual(('released', 'lost', receipt['receipt_digest']), self.hold('reservation', target.claim_id)[:3])
        self.assertEqual('retiring', self.row_state(1))
        self.assertEqual(('released', 'lost_with_reservation'), self.custody(1))
        self.assertIsNone(self.material(1))
        self.assertEqual(counts, self.counts())  # no terminal, consumption, cleanup receipt or record (oracle S3)
        self.consistent()
        self.assertGreaterEqual(self.store.expire_results(), 1)  # the ordinary retirement finishes it
        self.assertEqual('expired', self.row_state(1))
        self.consistent()

    def test_V2_a_released_committed_attempt_keeps_its_body_for_retention(self):
        self.bound(1)
        self.commit(1)
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(1), self.request_id(1)))
        self.open(1, arbitrate=False)
        receipt = self.record_settlement(1, kind='released_unacknowledged')
        self.assertEqual('released_unacknowledged', receipt['release_kind'])
        self.assertEqual('committed', self.row_state(1))
        self.assertEqual(('held', None), self.custody(1))
        self.assertEqual(('released', 'released_unacknowledged'),
                         self.hold('reservation', claim_binding(1).claim_id)[:2])
        self.consistent()

    def test_V4_a_lease_never_bound_expires_and_releases_lost_unbound(self):
        self.armed(1)
        self.open(1)
        self.record_settlement(1)
        lease = self.store.result_lease(self.lease(1))
        identity = project_holds_schema.attempt_identity(*self.attempt(1))
        self.assertEqual('expired', lease.state)
        self.assertEqual(('released', 'lost_unbound'), self.hold('lease', identity)[:2])
        self.assertEqual((None, 'lost'), self.settlement(1)[:2])
        self.assertEqual(('released', 'lost_with_reservation'), self.custody(1))
        self.consistent()

    def test_V5_a_normally_released_attempt_is_never_settled(self):
        self.bound(1)
        target = claim_binding(1)
        self.store.arbitrate_publication(target.run_handle, target.attempt, self.request_id(1))
        project_holds_schema.release_hold(self.store._connection, target.project_id, 'reservation', target.claim_id,
                                          'completed', now_ms=self.now)
        with self.assertRaises(HistoryError):
            self.open(1, arbitrate=False)
        before = self.snapshot()
        with self.assertRaises(HistoryError):
            self.record_settlement(1, request_digest='f' * 64)
        self.assertEqual(before, self.snapshot())

    def test_V6a_a_closure_keyed_with_another_key_is_refused(self):
        self.bound(1)
        self.open(1)
        before = self.snapshot()
        with self.assertRaises(HistoryError) as caught:
            self.record_settlement(1, key=b'x' * 32)
        self.assertEqual('history_invalid_input', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())

    def test_V6b_a_closure_for_another_claim_lease_or_attempt_is_refused(self):
        import dataclasses
        self.bound(1)
        self.bound(2)
        self.open(1)
        self.open(2)
        before = self.snapshot()
        other = self.store.result_lease(self.lease(2))
        for changes in ({'bridge_run_id': claim_binding(2).claim_id}, {'lease_digest': other.lease_digest},
                        {'assignment_digest': 'c' * 64}, {'released_at_ms': 0},
                        {'binding': dataclasses.replace(self.lease(1), attempt=2)},
                        {'binding': dataclasses.replace(self.lease(1), project_id='prj_' + 'f' * 32)}):
            with self.subTest(changes=sorted(changes)):
                with self.assertRaises(HistoryError):
                    self.record_settlement(1, **changes)
                self.assertEqual(before, self.snapshot())
        with self.assertRaises(HistoryError):  # attempt 2's closure presented for attempt 1
            self.store.record_account_settlement(self.lease(1), self.closure(2))
        self.assertEqual(before, self.snapshot())

    def test_B4_1_the_closure_past_the_size_limit_is_refused(self):
        from codex_agents.lost_run_closure import MAX_LOST_RUN_CLOSURE_BYTES
        self.bound(1)
        self.open(1)
        raw = self.closure(1)
        before = self.snapshot()
        padded = raw + b' ' * (MAX_LOST_RUN_CLOSURE_BYTES + 1 - len(raw))
        with self.assertRaises(HistoryError):
            self.store.record_account_settlement(self.lease(1), padded)
        self.assertEqual(before, self.snapshot())
        self.assertEqual('lost', self.store.record_account_settlement(self.lease(1), raw)['release_kind'])

    def test_B4_3_its_register_writes_equal_the_planted_helpers(self):
        self.bound(1)
        self.bound(2)
        self.open(1)
        self.store.arbitrate_publication(*self.attempt(2), self.request_id(2))
        self.record_settlement(1)
        with self.store._transaction():
            self.settle('lost', 2)
        rows = {number: self.store._connection.execute(
            'SELECT t.claim_id IS NOT NULL,t.release_kind,t.settled_at_ms,h.state,h.release_kind,h.released_at_ms '
            'FROM project_account_settlements t JOIN project_holds h ON h.kind=\'reservation\' AND h.identity=t.claim_id '
            'WHERE t.run_handle=?', (claim_binding(number).run_handle,)).fetchone() for number in (1, 2)}
        self.assertEqual(tuple(rows[2]), tuple(rows[1]))


@requirements('RS-B-B4')
class RetryAndPurgeTests(SettlementFixture):
    """RS-B-B4-5 and RS-B-B4-6: readers, identical retries and tombstones after the purge."""

    def test_B4_5_a_verified_settlement_releases_the_attempt_for_the_purge_only(self):
        self.bound(1)
        self.open(1)
        lease = self.store.result_lease(self.lease(1))
        self.assertFalse(self.store._attempt_settled(lease))
        self.record_settlement(1)
        self.assertTrue(self.store._attempt_settled(self.store.result_lease(self.lease(1))))
        # A planted (unverifiable) settlement row is not a verified one.
        self.bound(2)
        self.store.arbitrate_publication(*self.attempt(2), self.request_id(2))
        with self.store._transaction():
            self.settle('lost', 2)
        self.assertFalse(self.store._attempt_settled(self.store.result_lease(self.lease(2))))

    def test_B4_6_after_the_purge_an_identical_retry_gets_the_tombstones_receipt(self):
        self.bound(1)
        self.open(1)
        raw = self.closure(1)
        different = self.closure(1, released_at_ms=self.now + 99)
        first = self.store.record_account_settlement(self.lease(1), raw)
        target = claim_binding(1)
        with self.store._transaction():  # what the purge removes for a finished attempt
            self.store._delete_attempt_rows(target.run_handle, target.attempt, target.claim_id)
        self.assertIsNone(self.settlement(1))
        before = self.snapshot()
        again = self.store.record_account_settlement(self.lease(1), raw)
        self.assertEqual((first['receipt_digest'], True), (again['receipt_digest'], again['known']))
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(HistoryError):  # a different closure for the purged attempt
            self.store.record_account_settlement(self.lease(1), different)
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(HistoryError):  # a purged identity is never taken again (S4)
            project_holds_schema.take_hold(self.store._connection, target.project_id, 'reservation', target.claim_id,
                                           now_ms=self.now)

    def test_B4_6_another_project_cannot_present_the_closure(self):
        self.bound(1)
        self.open(1)
        raw = self.closure(1)
        before = self.snapshot()
        value = json.loads(raw)
        value['binding']['project_id'] = 'prj_' + 'e' * 32
        with self.assertRaises(HistoryError):
            self.store.record_account_settlement(self.lease(1), canonical_json(value))
        self.assertEqual(before, self.snapshot())


@requirements('RS-B-B4')
class ValidatorTests(SettlementFixture):
    """RS-B-B4-7 (SH-7): the register validator's settlement rows."""

    def plant(self, kind, *, hold_kind=None, number=1, claim=True):
        target = claim_binding(number)
        self.store._connection.execute(
            'INSERT INTO project_account_settlements VALUES(?,?,?,?,?,?,?)',
            (target.project_id, target.run_handle, target.attempt, target.claim_id if claim else None, kind, b'{}',
             self.now))
        if hold_kind is not None:
            project_holds_schema.release_hold(self.store._connection, target.project_id, 'reservation',
                                              target.claim_id, hold_kind, now_ms=self.now)

    def test_B4_7_a_held_reservation_with_a_settlement_row_is_refused(self):
        self.bound(1)
        with self.store._transaction():
            self.plant('lost')
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))

    def test_B4_7_a_settlement_row_whose_hold_has_another_kind_is_refused(self):
        self.bound(1)
        with self.store._transaction():
            self.plant('crashed', hold_kind='lost')
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))

    def test_B4_7_a_lease_only_settlement_needs_its_lease_released_lost_unbound(self):
        self.armed(1)
        with self.store._transaction():
            self.plant('lost', claim=False)
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))

    def test_B4_7_releasing_without_a_settlement_row_and_a_matching_one_are_accepted(self):
        self.bound(1)
        self.bound(2)
        self.store.arbitrate_publication(*self.attempt(1), self.request_id(1))
        self.store.arbitrate_publication(*self.attempt(2), self.request_id(2))
        with self.store._transaction():
            self.plant('lost', hold_kind='lost', number=2)
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))


@requirements('RS-B-B4')
class KeyIdentityTests(unittest.TestCase):
    """§9 (proved first): the account's digest key and the project writer's result key are one file in every install
    path: the project service (CollectorPaths), the offline owner tool (LearningState) and the account (the ledger's
    LearningState) all read `<state root>/digest.key`; and the claim itself is admitted only after the account checked
    the project's sealed lease with its key."""

    def setUp(self):
        from tests.support import TEMP_ROOT
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'state'

    def test_9_the_account_key_and_the_project_result_key_are_the_same_key(self):
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.learning_store import LearningState
        state = LearningState(self.root)
        account = state.digest_key()
        self.assertEqual(32, len(account))
        self.assertEqual(account, CollectorPaths(self.root).digest_key())  # serve_project's result key
        self.assertEqual(account, LearningState(self.root, create=False).digest_key())  # open_offline_history's
        self.assertEqual(state.key_path, CollectorPaths(self.root).key_path)

    def test_9_project_service_and_offline_tool_give_the_store_that_key(self):
        import ast
        from codex_agents import project_service
        source = Path(project_service.__file__).read_text()
        keys = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.keyword) and node.arg == 'result_key']
        # open_offline_history (the owner's LearningState) and serve_project (CollectorPaths of the same state root,
        # the one its collector socket and project registration came from): nothing else opens a keyed store.
        self.assertEqual(['key', 'state.digest_key()'], sorted(ast.unparse(node.value) for node in keys))

    def test_9_the_claim_requires_the_projects_seal_to_verify_with_the_account_key(self):
        from codex_agents.claim_storage import encode_storage_evidence, verify_pending_storage_evidence
        from codex_agents.result_contracts import ResultLease
        from codex_agents.account_claim_contracts import ClaimError
        fixture = SettlementFixture(methodName='run')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.armed(1)
        lease = fixture.store.result_lease(fixture.lease(1))
        self.assertIs(ResultLease, type(lease))
        raw = encode_storage_evidence(KEY, lease)
        self.assertEqual(lease, verify_pending_storage_evidence(KEY, lease.binding, raw))
        with self.assertRaises(ClaimError):
            verify_pending_storage_evidence(b'z' * 32, lease.binding, raw)

    def test_9_a_closure_the_account_keys_verifies_at_a_project_store_of_the_same_state(self):
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.learning_store import LearningState
        from codex_agents.lost_run_closure import lost_run_closure, verify_lost_run_closure
        state = LearningState(self.root)
        binding = leases.lease_binding(1)
        value = lost_run_closure(state.digest_key(), binding, lease_digest='1' * 64, assignment_digest='2' * 64,
                                 bridge_run_id='brg_' + '3' * 32, release_kind='lost', released_at_ms=5,
                                 request_digest='4' * 64)
        checked = verify_lost_run_closure(CollectorPaths(self.root).digest_key(), canonical_json(value),
                                          project_id=binding.project_id)
        self.assertEqual(value, checked)


if __name__ == '__main__':
    unittest.main()
