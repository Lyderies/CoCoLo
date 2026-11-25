"""Slice B1: the lease and reservation register (PLAN-0.4.0-supervisor-slices.md B1; DESIGN-0.4.0-RS-B.md drafts 2-4,
DESIGN-0.4.0-RS-BD-shared.md SH-2 and SH-7).

Every lease and reservation take and release writes its `project_holds` row in the same store transaction as its
source row: the SH-2 lease lifecycle (unarmed -> withdrawn_unsent | armed; armed -> denied | fenced | bound), the
reservation taken at bind (RS-B draft 3 §6) and released `completed` with the account's release acknowledgement.
Existing holds are adopted in the 17 -> 18 step, idempotently, before the validators; an ordinary open refuses a held
source with no register row (R3). Rows of the RS-B matrix (draft 3 §14.2): R1, R2, R3, X1, X4, L1, L3, L5, each
adapted to what B1 delivers. Their parts that need a later slice are named there and left to it: custody rows (D1),
the open binding's `fenced` and `withdrawn` (B3), `lost_unbound` (B5).

Integration stage: real stores, a real account ledger, and (L3, L5, the completed release) the in-process services.
"""
from contextlib import closing, contextmanager, nullcontext
import json
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_claim_denial_recovery as denial
from tests import test_result_leases as leases
from tests import test_result_lease_lane as lease_lane
from tests.learning_support import assignment
from tests.project_schema_fixtures import legacy_project_holds_layout
from tests.test_bridge import null_filled, result
from tests.test_lost_run_settlement import LostRunAfterStopTests
from tests.test_project_history import PROJECT_A, binding

from codex_agents import project_driver, project_history
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger
from codex_agents.project_history import RESULT_PURGE_GRACE_MS, ProjectHistory
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding

TABLES = ('result_leases', 'result_reservations', 'project_holds', 'project_cleanup_reservations')


class StopFixture(BaseException):
    """A stop inside a store transaction, after its writes and before its commit."""


@contextmanager
def failing_commit():
    real = ProjectHistory._transaction

    @contextmanager
    def cut(store):
        with real(store):
            yield
            raise StopFixture()
    with mock.patch.object(ProjectHistory, '_transaction', cut):
        yield


def identity(number):
    return f'{binding(number).run_handle}:{binding(number).attempt}'


def hold_row(db, kind, key, project=PROJECT_A):
    row = db.execute('SELECT state,lease_case,release_kind FROM project_holds WHERE project_id=? AND kind=? '
                     'AND identity=?', (project, kind, key)).fetchone()
    return None if row is None else tuple(row)


class RegisterWriteTests(unittest.TestCase):
    """R1, R2, X1 and R3 on the store's own operations (a lease store with a 16-entry capacity)."""

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.fixture.capacity = ResultCapacity(16, 8 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen()

    @property
    def store(self):
        return self.base.store

    @property
    def db(self):
        return self.store._connection

    def lease(self, number):
        return hold_row(self.db, 'lease', identity(number))

    def reservation(self, number):
        return hold_row(self.db, 'reservation', binding(number).claim_id)

    def snapshot(self):
        return {name: sorted(tuple(row) for row in self.db.execute('SELECT * FROM ' + name)) for name in TABLES}

    def full(self, number):
        return self.db.execute('SELECT * FROM project_holds WHERE project_id=? AND kind=? AND identity=?',
                               (PROJECT_A, 'lease', identity(number))).fetchone()

    # R1: each take and release branch -------------------------------------------------------------------------------

    def test_R1_each_lease_take_and_release_writes_its_row_with_the_source(self):
        taken = self.base.reserve(1)
        self.assertEqual(('held', 'unarmed', None), self.lease(1))
        row = self.full(1)
        self.assertEqual((taken.created_at_ms, taken.reserved_until_ms, None),
                         (row['taken_at_ms'], row['due_at_ms'], row['released_at_ms']))
        self.store.arm_result_lease(leases.lease_binding(1))
        self.assertEqual(('held', 'armed', None), self.lease(1))
        self.assertIsNone(self.reservation(1))
        self.base.bind(1)
        # Bound: the lease ends nothing else; the attempt's obligation passes to its reservation row (SH-2).
        self.assertEqual(('released', 'armed', 'bound'), self.lease(1))
        self.assertEqual(('held', None, None), self.reservation(1))
        self.base.reserve(2)
        self.store.release_unsent_result_lease(leases.lease_binding(2))
        self.assertEqual(('released', 'unarmed', 'withdrawn_unsent'), self.lease(2))
        self.assertEqual(1000, self.full(2)['released_at_ms'])
        self.base.reserve(3)
        self.base.fixture.now = 61_000
        self.assertEqual(1, self.store.expire_result_leases())  # unarmed past its time: never armed, never sent
        self.assertEqual(('released', 'unarmed', 'withdrawn_unsent'), self.lease(3))
        # A repeat changes nothing: nothing is released twice.
        before = self.snapshot()
        self.store.release_unsent_result_lease(leases.lease_binding(2))
        self.assertEqual(0, self.store.expire_result_leases())
        self.assertEqual(self.base.bind(1), self.base.bind(1))
        self.assertEqual(before, self.snapshot())

    def test_R1_the_commit_retirement_and_expiry_of_a_reservation_never_release_its_obligation(self):
        self.base.reserve(1)
        self.store.arm_result_lease(leases.lease_binding(1))
        self.base.bind(1)
        self.base.fixture.commit(1)
        self.assertEqual(('held', None, None), self.reservation(1))
        self.store.retire_result(binding(1))
        self.assertEqual('expired', self.store.result_reservation(binding(1)).state)
        self.assertEqual(('held', None, None), self.reservation(1))  # RS-B draft 3 §6: retention is not release

    def test_R1_a_reference_reservation_takes_its_row(self):
        self.base.fixture.reserve(5)
        self.assertEqual(('held', None, None), self.reservation(5))
        self.assertIsNone(self.lease(5))

    def test_R1_a_runs_lease_and_reservation_rows_are_distinct_companions(self):
        self.base.reserve(1)
        self.store.arm_result_lease(leases.lease_binding(1))
        self.base.bind(1)
        kinds = sorted(tuple(row) for row in self.db.execute('SELECT kind,identity FROM project_holds'))
        self.assertEqual([('lease', identity(1)), ('reservation', binding(1).claim_id)], kinds)

    def operations(self):
        """Each branch, from a store holding what it needs: (name, setup, operation, the register write)."""
        def reserve_then(*steps):
            def setup():
                self.base.reserve(1)
                for step in steps:
                    step()
            return setup
        arm = lambda: self.store.arm_result_lease(leases.lease_binding(1))  # noqa: E731

        def expire():
            self.base.fixture.now = 61_000
            return self.store.expire_result_leases()
        return (
            ('reserve', lambda: None, lambda: self.base.reserve(1), 'take_hold'),
            ('arm', reserve_then(), arm, 'arm_hold'),
            ('bind', reserve_then(arm), lambda: self.base.bind(1), 'release_hold'),
            ('bind', reserve_then(arm), lambda: self.base.bind(1), 'take_hold'),
            ('release_unsent', reserve_then(), lambda: self.store.release_unsent_result_lease(leases.lease_binding(1)),
             'release_hold'),
            ('expire', reserve_then(), expire, 'release_hold'),
            ('reference', lambda: None, lambda: self.base.fixture.reserve(1), 'take_hold'),
        )

    def test_R1_a_fault_at_the_register_write_rolls_back_the_source(self):
        from codex_agents import project_holds_schema
        for name, setup, operation, write in self.operations():
            with self.subTest(name=name, write=write):
                self.tearDown_store()
                setup()
                before = self.snapshot()
                with mock.patch.object(project_holds_schema, write, side_effect=RuntimeError('fixture fault')):
                    with self.assertRaisesRegex(RuntimeError, 'fixture fault'):
                        operation()
                self.assertEqual(before, self.snapshot())  # source and row change together or not at all
                operation()
                self.assertNotEqual(before, self.snapshot())
                self.base.reopen()  # the register validator accepts what the operation wrote

    def test_R1_a_fault_at_commit_rolls_back_the_source_and_its_row(self):
        for name, setup, operation, _ in self.operations()[:3] + self.operations()[4:]:
            with self.subTest(name=name):
                self.tearDown_store()
                setup()
                before = self.snapshot()
                with failing_commit(), self.assertRaises(StopFixture):
                    operation()
                self.assertEqual(before, self.snapshot())
                self.base.reopen()
                self.assertEqual(before, self.snapshot())
                operation()  # then it commits: the source and its register row together
                self.assertNotEqual(before['project_holds'], self.snapshot()['project_holds'])

    def tearDown_store(self):
        """A fresh, empty store in the same fixture (each subtest starts from nothing)."""
        self.store.close()
        for path in self.base.fixture.directory.iterdir():
            path.unlink()
        self.base.fixture.now = 1000
        self.base.fixture.store = self.base.fixture.open()

    # R2 and S4: a tombstone is never taken again ------------------------------------------------------------------

    def purge(self):
        owner = type(self.store._orchestration_owner())
        with mock.patch.object(owner, 'closed_for', return_value=True):
            return self.store._purge_finished_results(self.base.fixture.now, 10)

    def test_R2_a_purged_leases_identity_is_never_taken_again(self):
        self.base.reserve(1)
        self.store.release_unsent_result_lease(leases.lease_binding(1))
        self.base.fixture.now = 61_001 + RESULT_PURGE_GRACE_MS
        self.assertEqual(1, self.purge())
        self.assertEqual(0, self.db.execute('SELECT COUNT(*) FROM result_leases').fetchone()[0])
        self.assertEqual(('released', 'unarmed', 'withdrawn_unsent'), self.lease(1))  # kept for good
        for reopen in (False, True):
            with self.subTest(after_restart=reopen):
                if reopen:
                    self.base.reopen()
                before = self.snapshot()
                with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
                    self.base.reserve(1)
                self.assertEqual(before, self.snapshot())  # nothing written: no lease, cleanup slot or row

    def test_R2_a_reservation_identity_with_a_tombstone_is_never_taken_again(self):
        with self.store._transaction():
            self.db.execute("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,"
                            "release_kind) VALUES(?,'reservation',?,'released',1,2,'completed')",
                            (PROJECT_A, binding(5).claim_id))
        before = self.snapshot()
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.base.fixture.reserve(5)
        self.assertEqual(before, self.snapshot())

    def test_R2_a_held_reservation_is_never_purged_without_its_release(self):
        """Failed is not release (RS-B draft 2 §3): an expired reservation whose obligation no release ended keeps
        its source, so its held row always has one."""
        self.base.fixture.reserve(5)
        self.store.retire_result(binding(5))
        self.base.fixture.now = 2000 + RESULT_PURGE_GRACE_MS + 61_000
        self.assertEqual(0, self.purge())
        self.assertEqual(('held', None, None), self.reservation(5))
        self.assertEqual(1, self.db.execute('SELECT COUNT(*) FROM result_reservations').fetchone()[0])
        self.base.reopen()

    # X1: the lease transaction ------------------------------------------------------------------------------------

    def test_X1_lease_cleanup_slot_and_register_row_are_taken_together(self):
        self.base.reserve(1)
        self.assertEqual(('held', 'unarmed', None), self.lease(1))
        self.assertEqual(1, self.db.execute('SELECT COUNT(*) FROM project_cleanup_reservations').fetchone()[0])
        before = self.snapshot()
        with mock.patch.object(project_history, 'reserve_cleanup', side_effect=HistoryError('history_result_capacity')):
            with self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
                self.base.reserve(2)
        self.assertEqual(before, self.snapshot())
        # P on the later release: the row is released with its lease, the cleanup slot stays for the purge.
        self.store.release_unsent_result_lease(leases.lease_binding(1))
        self.assertEqual(('released', 'unarmed', 'withdrawn_unsent'), self.lease(1))

    # R3: the validator's coverage rule, enabled ------------------------------------------------------------------

    def refused(self, *statements):
        self.store.close()
        database = self.base.fixture.directory / 'history.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:
            for sql, values in statements:
                db.execute(sql, values)
        with closing(sqlite3.connect(database)) as db:
            before = {name: sorted(tuple(r) for r in db.execute('SELECT * FROM ' + name)) for name in TABLES}
        for allowed in (False, True):
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.base.fixture.open(allow_schema_upgrade=allowed)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(before, {name: sorted(tuple(r) for r in db.execute('SELECT * FROM ' + name))
                                      for name in TABLES})

    def held_store(self):
        self.base.reserve(1)
        self.base.reserve(2)
        self.store.arm_result_lease(leases.lease_binding(2))
        self.base.reserve(3)
        self.store.arm_result_lease(leases.lease_binding(3))
        self.base.bind(3)

    def test_R3_a_held_lease_with_no_register_row_is_refused(self):
        self.held_store()
        self.refused(("DELETE FROM project_holds WHERE kind='lease' AND identity=?", (identity(1),)))

    def test_R3_a_held_lease_whose_row_is_released_is_refused(self):
        self.held_store()
        self.refused(("UPDATE project_holds SET state='released',released_at_ms=1,release_kind='withdrawn_unsent' "
                      "WHERE kind='lease' AND identity=?", (identity(2),)))

    def test_R3_a_lease_case_that_disagrees_with_its_lease_is_refused(self):
        self.held_store()
        self.refused(("UPDATE project_holds SET lease_case='armed' WHERE kind='lease' AND identity=?", (identity(1),)))

    def test_R3_a_released_lease_whose_row_is_still_held_is_refused(self):
        self.held_store()
        self.refused(("UPDATE project_holds SET state='held',released_at_ms=NULL,release_kind=NULL "
                      "WHERE kind='lease' AND identity=?", (identity(3),)))

    def test_R3_a_reservation_with_no_register_row_is_refused(self):
        self.held_store()
        self.refused(("DELETE FROM project_holds WHERE kind='reservation' AND identity=?", (binding(3).claim_id,)))


class RegisterAdoptionTests(unittest.TestCase):
    """SH-1 adoption for B1's kinds: the 17 -> 18 step adopts every lease and reservation by class, before both
    validators, idempotently; an ordinary open never adopts (R3 stays a refusal)."""

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.capacity = ResultCapacity(16, 8 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen()
        self.base.reserve(1)                                                    # unarmed
        self.base.reserve(2)
        self.base.store.arm_result_lease(leases.lease_binding(2))               # armed
        self.base.reserve(3)
        self.base.store.arm_result_lease(leases.lease_binding(3))
        self.base.bind(3)                                                       # bound, reservation reserved
        self.base.reserve(4)
        self.base.store.release_unsent_result_lease(leases.lease_binding(4))   # withdrawn before any claim
        self.fixture.reserve(5)                                                 # reference reservation
        self.fixture.commit(5)                                                  # committed, no release ack

    @property
    def store(self):
        return self.base.store

    def format_17(self):
        self.store._connection.execute('DELETE FROM project_holds')  # the rows this format never had
        legacy_project_holds_layout(self.store)
        self.store._connection.execute('PRAGMA user_version=17')
        self.store.close()

    def upgrade(self):
        self.fixture.store = self.fixture.open(allow_schema_upgrade=True)
        return self.store._connection

    def test_the_step_adopts_every_lease_and_reservation_by_class(self):
        self.format_17()
        db = self.upgrade()
        self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)
        self.assertEqual(('held', 'unarmed', None), hold_row(db, 'lease', identity(1)))
        self.assertEqual(('held', 'armed', None), hold_row(db, 'lease', identity(2)))
        self.assertEqual('released', hold_row(db, 'lease', identity(3))[0])
        self.assertEqual('bound', hold_row(db, 'lease', identity(3))[2])
        self.assertEqual('released', hold_row(db, 'lease', identity(4))[0])
        self.assertEqual('withdrawn_unsent', hold_row(db, 'lease', identity(4))[2])  # no host open, no reservation
        self.assertEqual(('held', None, None), hold_row(db, 'reservation', binding(3).claim_id))
        self.assertEqual(('held', None, None), hold_row(db, 'reservation', binding(5).claim_id))  # failed is not release
        self.assertEqual(6, db.execute("SELECT COUNT(*) FROM project_holds WHERE kind IN ('lease','reservation')")
                         .fetchone()[0])
        # (requirement) D1 (RS-D M1): each reservation not yet expired (3, 5) also gets its `result_custody` row,
        # held, with its `legacy_no_context` context; nothing else is adopted.
        self.assertEqual(2, db.execute('SELECT COUNT(*) FROM project_holds').fetchone()[0] - 6)
        evidence = {json.loads(row[0])['adopted'] for row in db.execute('SELECT evidence FROM project_holds')}
        self.assertEqual({18}, evidence)  # each adopted row says so

    def test_adoption_is_idempotent(self):
        from codex_agents import project_holds_schema
        self.format_17()
        db = self.upgrade()
        before = sorted(tuple(row) for row in db.execute('SELECT * FROM project_holds'))
        with self.store._transaction():
            project_holds_schema.adopt_project_holds(db, 9_999)
        self.assertEqual(before, sorted(tuple(row) for row in db.execute('SELECT * FROM project_holds')))

    def test_an_ordinary_open_never_adopts_a_missing_row(self):
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='lease' AND identity=?", (identity(1),))
        self.store.close()
        for allowed in (False, True):
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.fixture.open(allow_schema_upgrade=allowed)


class DenialRegisterTests(denial.ClaimDenialRecoveryTests):
    """X4 and L3 at the store with a real account: the verified denial closure releases the lease `denied` in its
    own transaction; the denied reply alone, or a closure not yet stored, releases nothing."""

    def lease_row(self):
        scope = self.fixture.scope
        return hold_row(self.fixture.store._connection, 'lease', f'{scope.run_handle}:{scope.attempt}',
                        scope.project_id)

    def test_X4_the_stored_denial_closure_releases_the_lease_denied_in_its_transaction(self):
        self.assertEqual(('held', 'armed', None), self.lease_row())  # the denied reply alone releases nothing
        closed = canonical_json(self.call())
        store = self.fixture.store
        before = sorted(tuple(r) for r in store._connection.execute('SELECT * FROM project_holds'))
        from codex_agents import project_holds_schema
        with mock.patch.object(project_holds_schema, 'release_hold', side_effect=RuntimeError('fixture fault')):
            with self.assertRaisesRegex(RuntimeError, 'fixture fault'):
                store.settle_denied_result_lease(self.fixture.scope, closed)
        self.assertEqual('claim_pending', store.result_lease(self.fixture.scope).state)
        self.assertEqual(before, sorted(tuple(r) for r in store._connection.execute('SELECT * FROM project_holds')))
        self.assertEqual('expired', store.settle_denied_result_lease(self.fixture.scope, closed).state)
        self.assertEqual(('released', 'armed', 'denied'), self.lease_row())
        scope = self.fixture.scope
        receipt = store._connection.execute("SELECT receipt FROM project_holds WHERE kind='lease' AND identity=?",
                                            (f'{scope.run_handle}:{scope.attempt}',)).fetchone()[0]
        self.assertEqual(json.loads(closed)['receipt_digest'], receipt)
        store.settle_denied_result_lease(self.fixture.scope, closed)  # the identical retry: released once
        self.assertEqual(('released', 'armed', 'denied'), self.lease_row())
        store.close()
        self.fixture.store = self.fixture.open_history()  # the validator accepts it after a restart

    def test_X4_N_a_settlement_that_skipped_its_register_write_is_refused_at_the_next_open(self):
        from codex_agents import project_holds_schema
        closed = canonical_json(self.call())
        with mock.patch.object(project_holds_schema, 'release_hold', return_value=None):
            self.fixture.store.settle_denied_result_lease(self.fixture.scope, closed)
        self.fixture.store.close()
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.fixture.store = self.fixture.open_history()

    def test_L3_a_closure_received_but_not_stored_before_a_crash_releases_nothing_until_stored(self):
        closed = canonical_json(self.call())
        self.fixture.store.close()  # the crash: the closure was never stored
        self.fixture.store = self.fixture.open_history()
        self.assertEqual(('held', 'armed', None), self.lease_row())
        self.fixture.store.settle_denied_result_lease(self.fixture.scope, closed)  # the successor stores it
        self.assertEqual(('released', 'armed', 'denied'), self.lease_row())

    def test_an_altered_closure_releases_nothing(self):
        value = self.call()
        value['fenced_at_ms'] += 1
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            self.fixture.store.settle_denied_result_lease(self.fixture.scope, canonical_json(value))
        self.assertEqual(('held', 'armed', None), self.lease_row())


class StartPassTests(lease_lane.ResultLeaseLaneTests):
    """L1: an unarmed lease of a stopped instance is released `withdrawn_unsent` by the successor's first writer
    pass, before its time; the unarmed lease of a live run of this service is untouched."""

    def earlier_lease(self):
        target = binding(1, project=self.composition.project)
        lease = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                   claim_request_id='claim-request-1', claim_request_digest='1' * 64)
        earlier = self.composition.open_history()  # the earlier service, which stopped before arming its lease
        try:
            until = earlier.reserve_result_lease(lease, assignment()).reserved_until_ms
        finally:
            earlier.close()
        self.assertGreater(until, self.composition.fixture_time_ns() // 1_000_000)  # not past its time
        return lease

    def rows(self):
        with closing(sqlite3.connect(f'file:{self.composition.directory / "history.sqlite3"}?mode=ro', uri=True)) as db:
            return {tuple(row[:2]): tuple(row[2:]) for row in db.execute(
                "SELECT l.run_handle,l.state,h.state,h.release_kind FROM result_leases l JOIN project_holds h "
                "ON h.kind='lease' AND h.identity=l.run_handle||':'||l.attempt")}

    def test_L1_a_stopped_instances_unarmed_lease_is_released_at_the_start_pass(self):
        lease = self.earlier_lease()
        self.composition.answered(self.lane.expire_results())
        self.assertEqual({(lease.run_handle, 'expired'): ('released', 'withdrawn_unsent')}, self.rows())

    def test_L1_N_without_the_start_pass_the_unarmed_lease_stays_held(self):
        lease = self.earlier_lease()
        with mock.patch.object(ProjectHistory, 'release_earlier_unarmed_leases', return_value=0):
            self.composition.answered(self.lane.expire_results())
        self.assertEqual({(lease.run_handle, 'reserved'): ('held', None)}, self.rows())

    def test_L1_a_live_runs_unarmed_lease_is_untouched(self):
        seen = []

        def before(target, worker_handle):
            scope = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                       claim_request_id='original-claim', claim_request_digest='a' * 64)
            handle = self.lane.prepare_lease(scope, worker_handle)
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'preparing')
            self.composition.answered(self.lane.expire_results())
            seen.append((self.lane.lease(handle).state, self.rows()))
            raise HistoryError('history_result_not_ready')  # the test stops here

        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.worker.prepare(before_bind=before, after_bind=self.after)
        ((state, rows),) = seen
        self.assertEqual('reserved', state)
        self.assertEqual([('held', None)], list(rows.values()))


class SuccessorRegisterTests(LostRunAfterStopTests):
    """L3, L5 and the completed release, with the real driver, account and project services."""

    def store(self):
        path = next(self.state.root.glob('projects/*/history.sqlite3'))
        return closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30))

    def lease_row(self, run_handle):
        with self.store() as db:
            row = db.execute("SELECT state,lease_case,release_kind FROM project_holds WHERE kind='lease' "
                             "AND identity=?", (run_handle + ':1',)).fetchone()
        return None if row is None else tuple(row)

    def reservation_row(self, run_handle):
        with self.store() as db:
            row = db.execute("SELECT h.state,h.release_kind FROM result_reservations s JOIN project_holds h "
                             "ON h.kind='reservation' AND h.identity=s.claim_id WHERE s.run_handle=?",
                             (run_handle,)).fetchone()
        return None if row is None else tuple(row)

    def denial_settled(self):
        with closing(sqlite3.connect(f"file:{self.state.root / 'learning.sqlite3'}?mode=ro", uri=True, timeout=30)) as db:
            return db.execute("SELECT COUNT(*) FROM records WHERE record_type='claim_denial_settlement'").fetchone()[0]

    def held_then_restarted(self, holding, *, ready=lambda: True, patches=()):
        """Hold the run where `holding` says (its advance stops there), stop its host, start the next one; the
        next one's own sweep must end the lease. Returns the lease row while held, the row after, and the run."""
        original, seen = project_driver.RunDriver._advance, []

        def held(driver, run):
            if holding(driver, run):
                seen.append(run.state)
                return None
            return original(driver, run)
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        unsent, released = [], ProjectHistory.release_unsent_result_lease

        def spy(store, *args, **kwargs):
            unsent.append(args)
            return released(store, *args, **kwargs)
        withdrawn, withdraw = [], LearningLedger.withdraw_open

        def withdraw_spy(ledger, *args, **kwargs):
            withdrawn.append(args)
            return withdraw(ledger, *args, **kwargs)
        with mock.patch.object(project_driver.RunDriver, '_advance', held), \
                mock.patch.object(ProjectHistory, 'release_unsent_result_lease', spy), \
                mock.patch.object(LearningLedger, 'withdraw_open', withdraw_spy):
            with patches[0] if patches else nullcontext():
                dispatch = self.separate_dispatch(context, route_path, run_handle)
                self.wait_for(lambda: seen and ready(), seconds=30)
                before = self.lease_row(run_handle)
                self.stop_host()
            dispatch.communicate(timeout=30)
            self.start_host()
            self.wait_for(lambda: (self.lease_row(run_handle) or ('held',))[0] == 'released', seconds=30)
        self.assertEqual(([], []), (unsent, withdrawn))  # SH-2: never release_unsent or withdraw_open when armed
        self.assertEqual('result', self.next_run(orchestration, 'after')['bridge_report']['outcome'])
        return before, self.lease_row(run_handle), run_handle

    def test_L5_armed_then_a_crash_before_the_host_open_is_fenced_by_the_successor(self):
        before, after, _ = self.held_then_restarted(lambda driver, run: run.state == 'recording_open')
        self.assertEqual(('held', 'armed', None), before)
        self.assertEqual(('released', 'armed', 'fenced'), after)

    def test_L5_a_crash_after_the_host_open_before_the_claim_is_fenced_by_the_successor(self):
        def opened(driver, run):
            if run.state != 'recording_open' or run.future is not None:
                return False
            lane = driver._runtime.persistence
            try:
                return (lane.snapshot(run.persistence)['state'] == 'claim_pending'
                        and lane.host_open_record(run.persistence) is not None)
            except HistoryError:
                return False
        before, after, _ = self.held_then_restarted(opened)
        self.assertEqual(('held', 'armed', None), before)
        self.assertEqual(('released', 'armed', 'fenced'), after)

    def denied_claim(self, *, drop_close):
        from codex_agents import providers
        dropped, call = [], project_driver.RunDriver._account_call

        def account_call(driver, run, role, method, *args, **options):
            if method == 'close_denied_attempt' and drop_close:
                dropped.append(run.key)  # the crash comes before the closure is asked for
                return None
            return call(driver, run, role, method, *args, **options)

        @contextmanager
        def patched():
            with mock.patch.object(providers, 'evaluate_launch', return_value='billing_unverified'), \
                    mock.patch.object(project_driver.RunDriver, '_account_call', account_call):
                yield
        ready = (lambda: True) if drop_close else (lambda: self.denial_settled() == 1)
        before, after, _ = self.held_then_restarted(lambda driver, run: run.state == 'closing', ready=ready,
                                                    patches=(patched(),))
        self.assertEqual(bool(drop_close), bool(dropped))
        return before, after

    def test_L3_a_denied_reply_then_a_crash_before_the_closure_releases_nothing_until_the_successor_closes_it(self):
        before, after = self.denied_claim(drop_close=True)
        self.assertEqual(('held', 'armed', None), before)  # the denied reply alone releases nothing
        self.assertEqual(('released', 'armed', 'denied'), after)

    def test_L3_a_closure_the_account_stored_but_the_project_did_not_is_released_denied_by_the_successor(self):
        before, after = self.denied_claim(drop_close=False)
        self.assertEqual(('held', 'armed', None), before)
        self.assertEqual(('released', 'armed', 'denied'), after)

    def test_R1_a_completed_run_releases_its_lease_bound_and_its_reservation_completed(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.assertEqual('result', self.cli('dispatch', *context, '--route', route_path,
                                            '--run-handle', run_handle)['bridge_report']['outcome'])
        self.wait_for(lambda: self.reservation_row(run_handle) == ('released', 'completed'), seconds=30)
        self.assertEqual(('released', 'armed', 'bound'), self.lease_row(run_handle))
        self.restart_host()  # the register validator accepts the finished run's rows at the next open
        self.assertEqual(('released', 'completed'), self.reservation_row(run_handle))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited ones run in their own modules."""
    suite = unittest.TestSuite()
    for case in (RegisterWriteTests, RegisterAdoptionTests, DenialRegisterTests, StartPassTests,
                 SuccessorRegisterTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
