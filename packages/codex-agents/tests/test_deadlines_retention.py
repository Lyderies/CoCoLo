"""Slice D2 (RS-D D3/D4/D5, RS-B X6-X9): deadlines and retention of result custody.

DESIGN-0.4.0-RS-D.md draft 2 s1, s8, s9, draft 3 s8, draft 4 s2; DESIGN-0.4.0-RS-BD-shared.md SH-3 and SH-4.
No deadline before the commit retires anything: a reserved row past its lease deadline stays charged and publishable
while its obligation is held, and is retired only after RS-B releases that obligation. Committed custody ends at
`retention` (at max(wall, floor)) or when its orchestration is released `closed`, never by an unattended close
(owner decisions 90.3 A, 90.4 A, 90.5 A, 90.8 A). A verifier opens only when its worker outlasts it by a 1 h margin.
Integration stage: real stores in temporary folders, the fixtures of the lease and context tests.
"""
import ast
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import support
from tests import test_record_context as rc
from tests import test_project_runtime as project_runtime_tests
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_project_history import binding

from codex_agents import project_history, project_holds_schema, project_result_contexts
from codex_agents.history_contracts import HistoryError
from codex_agents.host_result_contracts import WorkerResultContext
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

DAY_MS = 86_400_000
MARGIN_MS = 3_600_000  # owner decision 90.4 A: one hour


class LeaseBackedFixture(unittest.TestCase):
    """Attempts born from a lease: reserve (with a record context), arm, bind, publish. Retention is 120 s here."""

    retention_ms = 120_000

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.set_policy(self.retention_ms)

    def set_policy(self, retention_ms):
        self.fixture.capacity = ResultCapacity(16, 8 * MAX_RESULT_BODY_BYTES, 60_000, retention_ms)
        self.base.reopen(result_capacity=self.fixture.capacity)

    @property
    def store(self):
        return self.fixture.store

    @property
    def now(self):
        return self.fixture.now

    @now.setter
    def now(self, value):
        self.fixture.now = value

    def attempt(self, number=1):
        target = binding(number)
        return target.run_handle, target.attempt

    def bound(self, number=1, **context):
        self.store.reserve_result_lease(leases.lease_binding(number), assignment(),
                                        record_context=rc.simple_context(number, **context))
        self.store.arm_result_lease(leases.lease_binding(number))
        return self.base.bind(number)

    def publish(self, number=1):
        return self.fixture.commit(number)

    def reservation(self, number=1):
        return self.store.result_reservation(binding(number))

    def row_state(self, number=1):
        return self.store._connection.execute("SELECT state FROM result_reservations WHERE run_handle=?",
                                              (self.attempt(number)[0],)).fetchone()[0]

    def context_state(self, number=1):
        row = rc.context_row(self.store, *self.attempt(number))
        return None if row is None else row[0]

    def release_kind(self, number=1):
        row = rc.custody(self.store, *self.attempt(number))
        return None if row is None else row[:2]

    def consistent(self):
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))

    def settle(self, kind, number=1):
        """What B4's `record_account_settlement` will write: the settlement row and the obligation released with it."""
        target = binding(number)
        self.store._connection.execute(
            "INSERT INTO project_account_settlements VALUES(?,?,?,?,?,?,?)",
            (target.project_id, target.run_handle, target.attempt, target.claim_id, kind, b'{}', self.now))
        project_holds_schema.release_hold(self.store._connection, target.project_id, 'reservation', target.claim_id,
                                          kind, now_ms=self.now)

    def record(self, digest, number=1):
        with self.store._transaction():
            project_result_contexts.record_outcome(self.store, binding(number), digest)


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class PreCommitDeadlineTests(LeaseBackedFixture):
    """D1, D2', X8, X9: no pre-commit deadline retires anything (SH-4)."""

    def test_D1_retained_until_is_set_once_and_the_context_takes_it_at_commit(self):
        self.bound(1)
        body, content = self.fixture.payload(1)
        with mock.patch.object(self.store, '_insert_terminal', side_effect=RuntimeError('metadata cut')):
            with self.assertRaises(RuntimeError):
                self.store.commit_result(content, binding(1), assignment(), body)
        prepared = self.reservation()
        self.assertEqual(1000 + 120_000, prepared.retained_until_ms)
        self.assertEqual('reserved', prepared.state)
        self.assertIsNone(rc.context_row(self.store, *self.attempt())[5])  # the context's own deadline starts at commit
        self.now = 5000
        self.set_policy(240_000)  # a changed policy cannot move it
        self.publish()
        committed = self.reservation()
        self.assertEqual((1000 + 120_000, 'committed', 5000), (committed.retained_until_ms, committed.state,
                                                               committed.committed_at_ms))
        self.assertEqual(1000 + 120_000, rc.context_row(self.store, *self.attempt())[5])
        self.assertEqual(committed.retained_until_ms, committed.expires_at_ms)
        self.consistent()

    def test_D2prime_a_reserved_row_past_its_lease_deadline_is_not_retired_by_the_expire_pass(self):
        self.bound(1)
        reserved = self.reservation()
        counts = self.store._result_counts()
        self.assertEqual('reserved', reserved.state)
        self.now = reserved.reserved_until_ms + 10 * 3_600_000  # hours past the lease deadline
        self.assertEqual(0, self.store.expire_results())
        self.assertEqual('reserved', self.row_state())
        self.assertEqual('bound', self.context_state())
        self.assertEqual(('held', None), rc.custody(self.store, *self.attempt())[:2])
        self.assertEqual(1, len(rc.context_files(self.fixture.directory)))
        self.assertEqual(counts, self.store._result_counts())  # still charged
        self.consistent()

    def test_D2prime_the_reservation_is_returned_unchanged_and_not_retired_on_a_read(self):
        self.bound(1)
        reserved = self.reservation()
        self.now = reserved.reserved_until_ms + 1
        self.assertEqual(reserved, self.reservation())
        self.assertEqual('reserved', self.row_state())
        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.store.read_result(binding(1))  # not committed: refused, but nothing is retired
        self.assertEqual('reserved', self.row_state())
        self.assertEqual('bound', self.context_state())
        self.consistent()

    def test_D2prime_publication_past_the_lease_deadline_is_still_permitted_and_commits(self):
        self.bound(1)
        reserved = self.reservation()
        self.now = reserved.reserved_until_ms + 5_000
        delivery = self.publish()
        committed = self.reservation()
        self.assertEqual('committed', committed.state)
        self.assertEqual(delivery.record_id, committed.terminal_record_id)
        # Retention is counted from the first preparation of the body; here that is the commit itself (the body was
        # first prepared at the late publication), so the deadline is `now + retention`.
        self.assertEqual(self.now + 120_000, committed.retained_until_ms)
        self.assertEqual(committed.retained_until_ms, committed.expires_at_ms)
        self.assertEqual(('committed', committed.retained_until_ms),
                         (self.context_state(), rc.context_row(self.store, *self.attempt())[5]))
        self.assertEqual(self.fixture.payload(1)[0], self.store.read_result(binding(1)))
        self.consistent()

    def test_X8_a_reserved_row_past_the_deadline_with_the_obligation_held_keeps_files_and_charge(self):
        self.bound(1)
        body, content = self.fixture.payload(1)
        with mock.patch.object(self.store, '_insert_terminal', side_effect=RuntimeError('metadata cut')):
            with self.assertRaises(RuntimeError):
                self.store.commit_result(content, binding(1), assignment(), body)
        self.assertEqual(1, len(self.fixture.files()))
        counts = self.store._result_counts()
        self.now = 61_000 + DAY_MS  # past the lease deadline and the prepared retention
        self.assertEqual(0, self.store.expire_results())
        self.assertEqual('reserved', self.reservation().state)
        self.assertEqual(1, len(self.fixture.files()))
        self.assertEqual(counts, self.store._result_counts())
        self.assertEqual('bound', self.context_state())
        self.assertEqual(1, len(rc.context_files(self.fixture.directory)))
        delivery = self.publish()  # the prepared body is still publishable (a replay would do the same, D4 slice)
        self.assertEqual('committed', self.row_state())  # committed late: due at the next pass, never revived
        self.assertEqual((delivery,), self.store.pending_terminals())
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.consistent()

    def test_X9_the_obligation_released_lost_retires_the_reserved_row_at_the_next_pass(self):
        self.bound(1)
        reserved = self.reservation()
        self.now = reserved.reserved_until_ms + 1
        self.assertEqual(0, self.store.expire_results())
        self.settle('lost')
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual(('released', 'lost_with_reservation'), self.release_kind())
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.assertEqual(0, rc.live_context_bytes(self.store))
        self.consistent()

    def test_X9_a_lost_release_before_the_deadline_retires_it_too_without_waiting_for_the_deadline(self):
        self.bound(1)
        self.settle('crashed')
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual(('released', 'lost_with_reservation'), self.release_kind())
        self.consistent()


    def drop_hold(self, number=1):
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='reservation' AND identity=?",
                                       (binding(number).claim_id,))

    def test_a_lease_born_reserved_row_never_lacks_its_obligation_hold_and_the_register_validator_refuses_one_that_does(self):
        self.bound(1)
        count = "SELECT COUNT(*) FROM project_holds WHERE kind='reservation' AND identity=?"
        self.assertEqual(1, self.store._connection.execute(count, (binding(1).claim_id,)).fetchone()[0])
        self.consistent()  # taken with the row, in the claim's own transaction
        self.drop_hold()
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))  # SH-7 refuses the state

    def test_a_reserved_row_that_somehow_lacks_its_hold_is_kept_and_reported_by_both_paths_never_deleted(self):
        self.bound(1)
        body, content = self.fixture.payload(1)
        with mock.patch.object(self.store, '_insert_terminal', side_effect=RuntimeError('metadata cut')):
            with self.assertRaises(RuntimeError):
                self.store.commit_result(content, binding(1), assignment(), body)  # a prepared body is on disk
        self.assertEqual(1, len(self.fixture.files()))
        self.drop_hold()
        reserved = self.reservation()
        self.now = reserved.reserved_until_ms + 10 * 3_600_000
        counts = self.store._result_counts()
        value = self.store._result_row(binding(1))[1]
        self.assertEqual('missing', self.store._obligation_state(value))  # the predicate: kept, named
        self.assertFalse(self.store._retire_due(value, self.now))
        self.assertEqual(0, self.store.expire_results())  # the selector agrees: nothing selected
        self.assertEqual(reserved, self.reservation())  # and a read retires nothing
        self.assertEqual(('reserved', 'bound'), (self.row_state(), self.context_state()))
        self.assertEqual(counts, self.store._result_counts())
        self.assertEqual(1, len(self.fixture.files()))
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))  # reported (repair)


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class NativeProductCallerTests(unittest.TestCase):
    """The scope note of docs/requirements/d2.md rests on this: only tests reach the non-lease `prepare`.

    The runtime test below is the enforcement (a service lane refuses it). The AST scans are a regression gate only:
    they read `codex_agents/*.py` for direct `.prepare(..., hard_deadline_at_ms=...)` and `.reserve_result(...)` calls
    and do not cover aliases, `**kwargs` spellings, `getattr`, or code outside this package.
    """

    PACKAGE = support.ORCHESTRATION_ROOT / 'codex_agents'

    def calls(self, name, keyword=None):
        found = []
        for path in sorted(self.PACKAGE.glob('*.py')):
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == name
                        and (keyword is None or any(k.arg == keyword for k in node.keywords))):
                    found.append((path.name, node.lineno))
        return found

    def test_no_product_code_calls_the_non_lease_prepare_and_the_driver_uses_prepare_lease(self):
        # The non-lease `prepare(binding, handle, hard_deadline_at_ms=...)` is the only caller of the no-lease branch.
        self.assertEqual([], self.calls('prepare', 'hard_deadline_at_ms'))
        # `reserve_result` is reached from product code only by that unreachable branch of result_persistence.py
        self.assertEqual({'result_persistence.py'}, {name for name, _ in self.calls('reserve_result')})
        self.assertEqual([('project_driver.py', mock.ANY)], [c for c in self.calls('prepare_lease')
                                                              if c[0] == 'project_driver.py'])

    def test_a_product_runtime_lane_refuses_the_non_lease_prepare_at_runtime(self):
        # The runtime boundary: ProjectRuntime builds the lane without the local-reference allowance, so the
        # non-lease reservation cannot be reached from a service, whatever the caller spells.
        base = project_runtime_tests.ProjectRuntimeTests(methodName='setUp')
        base.setUp()
        self.addCleanup(base.doCleanups)
        runtime = base.start()
        for call in (lambda: runtime.persistence.prepare(binding(1), object(), hard_deadline_at_ms=2000),
                     lambda: runtime.persistence.prepare(binding(1), object(), **{'hard_deadline_at_ms': 2000}),
                     lambda: getattr(runtime.persistence, 'prepare')(binding(1), object(), hard_deadline_at_ms=2000)):
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                call()
        runtime.request_close()

    def test_only_result_persistence_defines_the_no_lease_branch(self):
        text = (self.PACKAGE / 'result_persistence.py').read_text()
        self.assertEqual(1, text.count('self._prepare_entry(binding, worker_handle, hard_deadline_at_ms)'))


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class ContextSweepTests(LeaseBackedFixture):
    """X6, X7 (SR-2): a context is retired on its own only with no reservation; a bound attempt keeps it."""

    def test_X6_a_bound_attempt_keeps_context_body_and_charge_while_reserved_committed_and_recorded(self):
        self.bound(1)
        for stage in ('reserved', 'committed', 'recorded'):
            if stage == 'committed':
                self.publish()
            if stage == 'recorded':
                self.record('ab' * 32)
            self.assertEqual(0, self.store.expire_results(), stage)
            self.assertEqual(stage if stage != 'recorded' else 'committed', self.row_state(), stage)
            self.assertEqual(1, len(rc.context_files(self.fixture.directory)), stage)
            self.assertEqual(('held', None), rc.custody(self.store, *self.attempt())[:2], stage)
            self.assertEqual(1, self.store._result_counts()[0], stage)
        self.assertEqual('recorded', self.context_state())
        self.assertEqual(1, len(self.fixture.files()))
        self.consistent()

    def test_X7_a_lease_released_without_a_reservation_retires_its_context(self):
        self.store.reserve_result_lease(leases.lease_binding(1), assignment(), record_context=rc.simple_context(1))
        self.assertEqual(1, self.store.release_earlier_unarmed_leases(set()))
        self.assertEqual('ended', self.context_state())
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assertEqual(('released', 'lost_with_reservation'), self.release_kind())
        self.consistent()


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class CommittedRetentionTests(LeaseBackedFixture):
    """D3: committed custody ends at retention, judged at max(wall, floor)."""

    def test_D3_custody_is_released_retention_exactly_at_equality(self):
        self.bound(1)
        self.publish()
        due = self.reservation().retained_until_ms
        self.now = due - 1
        self.assertEqual(0, self.store.expire_results())
        self.assertEqual('held', rc.custody(self.store, *self.attempt())[0])
        self.assertEqual(self.fixture.payload(1)[0], self.store.read_result(binding(1)))
        self.now = due
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual([], self.fixture.files())
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.consistent()

    def test_D3_with_no_verifier_ever_the_active_orchestration_does_not_extend_it(self):
        self.bound(1)
        self.publish()
        due = self.reservation().retained_until_ms
        self.now = due + 1
        self.assertEqual(1, self.store.expire_results())  # no verifier, no QA, orchestration still active
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.consistent()


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class ClockFloorTests(LeaseBackedFixture):
    """D4 (RS-D-7, draft 2 s8): a clock moved back neither stops expiry nor revives anything."""

    def test_D4_a_floor_past_the_deadline_then_a_backward_wall_clock_still_retires_at_the_next_pass(self):
        self.bound(1)
        self.publish()
        due = self.reservation().retained_until_ms
        self.now = due + 5
        self.store._result_now()  # the floor is persisted past the deadline, before any I/O
        with mock.patch.object(project_history.os, 'unlink', side_effect=OSError('stop before the unlink')):
            with self.assertRaises(HistoryError):
                self.store.expire_results()
        self.assertEqual('retiring', self.row_state())
        self.now = due - 1_000  # the wall clock moved back below the floor
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual([], self.fixture.files())
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.consistent()

    def test_D4_a_committed_row_is_selected_by_the_expire_pass_at_the_floor_when_the_wall_is_below_its_deadline(self):
        self.bound(1)
        self.publish()
        due = self.reservation().retained_until_ms
        self.now = due + 5
        self.store._result_now()  # the floor is persisted past the deadline
        self.now = due - 1_000  # wall < due <= floor, and the row is committed (not retiring)
        self.assertEqual('committed', self.row_state())
        self.assertEqual(1, self.store.expire_results())  # selected at the floor, not at the wall
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.assertEqual([], self.fixture.files())
        self.consistent()

    def test_D4_repeated_jumps_revive_nothing_and_block_no_pass(self):
        self.bound(1)
        self.publish()
        due = self.reservation().retained_until_ms
        passes = []
        for wall in (due + 10 * DAY_MS, 1_000, due + 20 * DAY_MS, 500, due + 30 * DAY_MS, 2):
            self.now = wall
            passes.append(self.store.expire_results())  # never raises on a moved-back clock
        self.assertEqual([1, 0, 0, 0, 0, 0], passes)  # the first jump retires it; nothing is left to revive
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.consistent()

    def test_D4_a_pass_with_a_wall_clock_below_the_floor_does_not_lower_it(self):
        self.bound(1)
        self.now = 50_000
        self.store._result_now()
        self.now = 40_000
        self.store.expire_results()
        floor = self.store._connection.execute("SELECT last_observed_at_ms FROM result_identity").fetchone()[0]
        self.assertEqual(50_000, floor)
        self.consistent()


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class VerifierMarginTests(LeaseBackedFixture):
    """V1, V3, V4 (RS-D-5, draft 2 s9): the margin is an admission check at verifier open, not a lifetime promise."""

    retention_ms = 7 * DAY_MS

    def verifier(self, number, digest):
        task = {'id': f'verify-{number}', 'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': digest}
        return self.store.reserve_result_lease(leases.lease_binding(number), assignment(),
                                               record_context=rc.simple_context(number, task=task))

    def test_V1_the_worker_must_outlast_the_verifier_lease_by_the_margin_at_open(self):
        self.bound(1)
        self.publish()
        digest = 'ab' * 32
        self.record(digest)
        worker_due = self.reservation().retained_until_ms
        self.assertEqual(1000 + 7 * DAY_MS, worker_due)
        lease_ms = 60_000
        limit = worker_due - lease_ms - MARGIN_MS  # the latest open time the worker still outlasts by the margin
        before = rc.live_context_bytes(self.store), len(rc.context_files(self.fixture.directory))
        self.now = limit - 1  # above the margin: accepted
        self.assertEqual('reserved', self.verifier(2, digest).state)
        self.now = limit  # equal to the margin: accepted
        self.assertEqual('reserved', self.verifier(3, digest).state)
        self.now = limit + 1  # below the margin: refused, and nothing is stored
        stored = rc.live_context_bytes(self.store), len(rc.context_files(self.fixture.directory))
        with self.assertRaisesRegex(HistoryError, 'history_worker_result_expiring'):
            self.verifier(4, digest)
        self.assertEqual(stored, (rc.live_context_bytes(self.store), len(rc.context_files(self.fixture.directory))))
        self.assertGreater(stored[0], before[0])
        self.assertIsNone(rc.context_row(self.store, *self.attempt(4)))
        self.assertEqual(0, self.store._connection.execute(
            "SELECT COUNT(*) FROM result_leases WHERE run_handle=?", (self.attempt(4)[0],)).fetchone()[0])
        self.consistent()

    def test_V1_a_task_that_verifies_nothing_is_not_subject_to_the_margin(self):
        self.bound(1)
        self.publish()
        self.now = self.reservation().retained_until_ms - 1_000  # one second before the worker's own retention ends
        self.store.reserve_result_lease(leases.lease_binding(2), assignment(), record_context=rc.simple_context(2))

    def test_V3_the_worker_expiring_while_the_verifier_runs_leaves_the_verifier_custody_live(self):
        self.set_policy(120_000)
        self.bound(1)
        self.bound(2)
        self.publish(1)
        self.now = 1_500
        self.publish(2)
        worker_due, verifier_due = self.reservation(1).retained_until_ms, self.reservation(2).retained_until_ms
        self.assertLess(worker_due, verifier_due)
        self.now = worker_due
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'retention'), self.release_kind(1))
        self.assertEqual('held', rc.custody(self.store, *self.attempt(2))[0])  # the verifier's custody is live
        self.assertEqual(self.fixture.payload(2)[0], self.store.read_result(binding(2)))
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.read_result(binding(1))  # the pair's QA reads the worker: refused
        # The verifier's orchestration is active (no close ends its custody) and it records its outcome.
        self.assertFalse(self.store._custody_ended_by_close(binding(2)))
        self.record('cd' * 32, 2)
        self.assertEqual('recorded', self.context_state(2))
        self.assertEqual('cd' * 32, rc.context_row(self.store, *self.attempt(2))[4])  # its outcome association
        self.assertEqual('held', rc.custody(self.store, *self.attempt(2))[0])
        self.assertEqual(self.fixture.payload(2)[0], self.store.read_result(binding(2)))  # still readable after
        self.consistent()

    def test_V4_a_forward_jump_past_both_deadlines_releases_both_and_the_verifier_result_is_refused(self):
        self.set_policy(120_000)
        self.bound(1)
        self.bound(2)
        self.publish(1)
        self.publish(2)
        self.now = max(self.reservation(1).retained_until_ms, self.reservation(2).retained_until_ms) + 10 * DAY_MS
        self.assertEqual(2, self.store.expire_results())
        self.assertEqual((('released', 'retention'), ('released', 'retention')),
                         (self.release_kind(1), self.release_kind(2)))
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.read_result(binding(2))
        # The verifier's record is refused: nothing is associated with the expired attempt.
        self.record('cd' * 32, 2)
        self.assertEqual('expired', self.context_state(2))
        self.assertIsNone(rc.context_row(self.store, *self.attempt(2))[4])
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.consistent()


@__import__('test_framework').requirements('RS-D-D2', why_not_scenario='D2 checks real project stores in temporary folders (leases, contexts, holds, files); the clock, close and settlement controls expose the deadline boundary.')
class OrchestrationCloseTests(rc.RecordedContextFixture):
    """C1-C3, V2 on a real run (RS-D-4 and draft 3 s8, owner decision 90.8 A)."""

    def retained_until(self):
        return self.store.result_reservation(self.binding).retained_until_ms

    def unattended(self):
        """What B6 will record: the orchestration released `interrupted_unattended` in the register."""
        connection = self.store._connection
        if connection.execute("SELECT 1 FROM project_holds WHERE kind='orchestration' AND identity=?",
                              (self.binding.orchestration_handle,)).fetchone() is None:  # B6: the open took it
            project_holds_schema.take_hold(connection, self.binding.project_id, 'orchestration',
                                           self.binding.orchestration_handle, now_ms=self.fixture.base.stamp)
        project_holds_schema.release_hold(connection, self.binding.project_id, 'orchestration',
                                          self.binding.orchestration_handle, 'interrupted_unattended',
                                          now_ms=self.fixture.base.stamp)

    def closed(self):
        return mock.patch.object(OrchestrationOwner, 'closed_for', return_value=True)

    def test_C1_a_closed_orchestration_releases_committed_custody_orchestration_closed_at_the_next_pass(self):
        self.recorded()
        with self.closed():
            self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'orchestration_closed'), rc.custody(self.store, *self.attempt)[:2])
        self.assertEqual(('expired', 'expired'), (self.store.result_reservation(self.binding).state,
                                                  rc.context_row(self.store, *self.attempt)[0]))
        self.assertEqual([], rc.context_files(self.directory))

    def test_C2_an_unattended_close_keeps_custody_readable_and_recordable_to_its_deadline(self):
        digest = self.recorded()
        self.unattended()
        due = self.retained_until()
        with self.closed():
            self.assertEqual(0, self.store.expire_results())
            self.assertEqual('held', rc.custody(self.store, *self.attempt)[0])
            self.assertEqual(self.body, self.store.read_result(self.binding))  # read works
            again = self.admit_from_store()  # record works: the same admission
            self.assertEqual(digest, again['admission']['host_outcome_digest'])
            self.assertEqual(self.expected(), self.store.stored_worker_context(digest, host_class=self.host_class))
            self.fixture.base.stamp = due - 1
            self.assertEqual(0, self.store.expire_results())
            self.fixture.base.stamp = due
            self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('released', 'retention'), rc.custody(self.store, *self.attempt)[:2])
        self.assertEqual([], rc.context_files(self.directory))

    def test_C2_a_closed_orchestration_without_an_unattended_release_still_reads_as_refused(self):
        self.recorded()
        with self.closed(), self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.store.read_result(self.binding)

    def consistent(self):
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, self.fixture.key))
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))

    def test_V3_the_pair_reference_is_refused_while_its_orchestration_stays_active_after_the_worker_expired(self):
        digest = self.recorded()
        due = self.retained_until()
        task = {**self.task, 'id': 'verify-original-worker', 'family': 'verification',
                'verification_for_family': self.task['family'], 'verification_for_provider': 'codex',
                'verification_for_outcome': digest}
        self.fixture.base.stamp = due
        self.assertEqual(1, self.store.expire_results())
        self.assertFalse(self.store._custody_ended_by_close(self.binding))  # the orchestration is active
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.verification_worker_reference(WorkerResultContext(self.binding, task), digest,
                                                     before_reply=lambda: None)
        self.consistent()

    def test_V4_the_record_of_an_expired_attempt_is_refused_by_the_admission(self):
        self.open_and_consume()
        self.publish()
        self.fixture.base.stamp = self.retained_until() + 10 * DAY_MS  # forward jump past the deadline
        self.assertEqual(1, self.store.expire_results())
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.admit_from_store()
        self.assertEqual('expired', rc.context_row(self.store, *self.attempt)[0])
        self.assertEqual([], rc.context_files(self.directory))
        self.consistent()

    def test_V4_an_expiry_inside_the_admission_refuses_it_and_records_nothing(self):
        self.open_and_consume()
        self.publish()
        due = self.retained_until()
        original = self.store.stored_result_context(self.binding, host_class=self.host_class)

        def expire():
            self.fixture.base.stamp = due
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.admit_host_result(self.reference, assignment=original.assignment, task=original.task,
                                         route=original.route, client_capability=original.client_capability,
                                         before_commit=expire)
        self.assertEqual('expired', rc.context_row(self.store, *self.attempt)[0])
        self.assertEqual('expired', self.store.result_reservation(self.binding).state)
        self.assertIsNone(rc.context_row(self.store, *self.attempt)[4])  # no outcome association was written
        self.consistent()

    def start_retirement(self, how):
        """Leave the row `retiring`: the close or the expiry starts it and the unlink fails (patch stays active)."""
        patch = mock.patch.object(project_history.os, 'unlink', side_effect=OSError('stop before unlink'))
        patch.start()
        self.addCleanup(patch.stop)
        if how == 'close':
            with self.closed(), self.assertRaises(HistoryError):
                self.store.expire_results()
            self.assertEqual(('released', 'orchestration_closed'), rc.custody(self.store, *self.attempt)[:2])
        else:
            self.fixture.base.stamp = self.retained_until()
            with self.assertRaises(HistoryError):
                self.store.expire_results()
            self.assertEqual(('released', 'retention'), rc.custody(self.store, *self.attempt)[:2])
        self.assertEqual('retiring', self.store._connection.execute(
            "SELECT state FROM result_reservations WHERE run_handle=?", (self.binding.run_handle,)).fetchone()[0])
        self.assertEqual('retiring', rc.context_row(self.store, *self.attempt)[0])

    def assert_still_retiring(self):
        self.assertEqual('retiring', self.store._connection.execute(
            "SELECT state FROM result_reservations WHERE run_handle=?", (self.binding.run_handle,)).fetchone()[0])
        self.assertEqual('retiring', rc.context_row(self.store, *self.attempt)[0])

    def check_retiring_body_and_context(self, how):
        digest = self.recorded()
        self.start_retirement(how)
        # Refused, no bytes: fail-closed `expired`, or `unavailable` when the probe's own retirement retry meets the
        # still-failing unlink.
        with self.assertRaisesRegex(HistoryError, 'history_result_expired|history_unavailable'):
            self.store.read_result(self.binding)
        self.assert_still_retiring()  # the failed unlink is still active: the probe retired nothing
        with self.assertRaisesRegex(HistoryError, 'history_result_expired|history_unavailable'):
            self.store.stored_result_context(self.binding, host_class=self.host_class)
        self.assert_still_retiring()
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized|history_result_expired|history_unavailable'):
            self.store.stored_worker_context(digest, host_class=self.host_class)
        self.assert_still_retiring()
        self.consistent()

    def check_retiring_admission(self, how):
        self.open_and_consume()
        self.publish()
        original = self.store.stored_result_context(self.binding, host_class=self.host_class)
        self.start_retirement(how)
        with self.assertRaisesRegex(HistoryError, 'history_result_expired|history_result_not_ready'):
            self.store.admit_host_result(self.reference, assignment=original.assignment, task=original.task,
                                         route=original.route, client_capability=original.client_capability,
                                         before_commit=lambda: None)
        self.assert_still_retiring()
        self.assertIsNone(rc.context_row(self.store, *self.attempt)[4])  # no outcome association was written
        self.consistent()

    def test_C3_a_close_started_retiring_row_refuses_body_and_context_reads(self):
        self.check_retiring_body_and_context('close')

    def test_C3_an_expiry_started_retiring_row_refuses_body_and_context_reads(self):
        self.check_retiring_body_and_context('expiry')

    def test_C3_a_close_started_retiring_unrecorded_row_refuses_the_admission_and_records_nothing(self):
        self.check_retiring_admission('close')

    def test_C3_an_expiry_started_retiring_unrecorded_row_refuses_the_admission_and_records_nothing(self):
        self.check_retiring_admission('expiry')

    def test_C3_a_close_or_expiry_racing_a_read_returns_no_bytes_and_later_context_reads_are_refused(self):
        digest = self.recorded()
        due = self.retained_until()
        read = self.store._read_result_file

        def slow(*arguments):
            body = read(*arguments)
            self.fixture.base.stamp = due
            return body
        with mock.patch.object(self.store, '_read_result_file', side_effect=slow):
            with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
                self.store.read_result(self.binding)
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.stored_result_context(self.binding, host_class=self.host_class)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized|history_result_expired'):
            self.store.stored_worker_context(digest, host_class=self.host_class)
        self.assertEqual([], rc.context_files(self.directory))

    def test_V2_expiry_during_the_verifier_preparation_refuses_it_and_nothing_is_launched(self):
        digest = self.recorded()
        due = self.retained_until()
        task = {**self.task, 'id': 'verify-original-worker', 'family': 'verification',
                'verification_for_family': self.task['family'], 'verification_for_provider': 'codex',
                'verification_for_outcome': digest}
        context = WorkerResultContext(self.binding, task)
        self.fixture.base.stamp = due - 1
        reference = self.store.verification_worker_reference(context, digest, before_reply=lambda: None)
        self.assertEqual(digest, reference['host_outcome_digest'])
        self.fixture.base.stamp = due
        sent = []
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.verification_worker_reference(context, digest, before_reply=lambda: sent.append(True))
        self.assertEqual([], sent)
        self.assertEqual('expired', self.store.result_reservation(self.binding).state)


if __name__ == '__main__':
    unittest.main()
