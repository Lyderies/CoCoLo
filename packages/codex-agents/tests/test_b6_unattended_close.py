"""RS-B B6: orchestration register rows, the unattended close, cleanup purge (K1/K2), schema step 20 -> 21.

Real project stores and the real orchestration owner; a controlled clock; fixture keys only (no owner authority, no
model calls). docs/requirements/b6.md holds the acceptance text for every requirement named here.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests import test_record_context as rc
from tests.test_project_history import PROJECT_A

from codex_agents import project_holds_schema
from codex_agents.history_contracts import HistoryError
from codex_agents.orchestration_api import (OrchestrationLimits, OrchestrationRequest, OrchestrationScope,
                                            SCOPE_FIELDS)
from codex_agents.orchestration_store import ORCHESTRATION_IDLE_MS, ORCHESTRATION_RETENTION_MS
from codex_agents.project_history import RESULT_PURGE_GRACE_MS, ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

requirements = __import__('test_framework').requirements
KEY = b'j' * 32  # the format-20 fixture's key (tests/fixtures/history_step20/make_store.py)
CLIENT = 'codex-host-v1'
STEP20 = Path(__file__).resolve().parent / 'fixtures' / 'history_step20' / 'store.sql'
# B6 review r1: the same step-20 runtime's store with lease and reservation histories (make_store.py --leases)
STEP20_LEASES = STEP20.with_name('store_leases.sql')
WHY = ('The register rows, the close transaction and the schema step are store contracts: real stores, the real '
       'orchestration owner and the real upgrade path, with a controlled clock; no service boundary is patched.')
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


def consistent(test, store):
    test.assertTrue(project_holds_schema.register_consistent(store._connection))
    test.assertTrue(project_holds_schema.custody_consistent(store._connection, store._result_key))


def hold(store, handle):
    row = store._connection.execute(
        "SELECT state,release_kind,taken_at_ms,due_at_ms,next_check_ms,evidence FROM project_holds "
        "WHERE kind='orchestration' AND identity=?", (handle,)).fetchone()
    return None if row is None else tuple(row)


def state_column(store, handle):
    return store._connection.execute('SELECT state FROM project_orchestrations WHERE orchestration_handle=?',
                                     (handle,)).fetchone()[0]


class OrchestrationStore(unittest.TestCase):
    """A project store with the orchestration owner, as tests/test_project_orchestration.py builds it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / 'history'
        self.directory.mkdir(mode=0o700)
        self.scope = OrchestrationScope(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1)
        self.now = 1000
        self.store = self.open_history()
        self.addCleanup(lambda: self.store.close())

    def open_history(self, **options):
        return ProjectHistory(self.directory, PROJECT_A, result_key=KEY,
                              result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                              result_clock_ms=lambda: self.now, orchestration_limits=OrchestrationLimits(8, 12),
                              **options)

    def reopen(self, **options):
        self.store.close()
        self.store = self.open_history(**options)
        return self.store

    @property
    def owner(self):
        return self.store._orchestration_owner()

    def call(self, operation, request_id, payload=None, scope=None):
        request = OrchestrationRequest(1, 'orchestration.' + operation, request_id, scope or self.scope, payload or {})
        return self.owner.apply(request, client_version=CLIENT, before_commit=lambda: None)

    def open(self, request_id='open'):
        return self.call('open', request_id)['current']['snapshot']

    @staticmethod
    def target(snapshot, **values):
        return {'orchestration_handle': snapshot['orchestration_handle'], 'expected_sequence': snapshot['sequence'],
                **values}

    def close(self, snapshot, request_id='close', status='completed'):
        return self.call('close', request_id, self.target(snapshot, status=status, goal_tokens_used=None,
                                                          goal_time_used_seconds=None))['current']['snapshot']

    def status(self, handle):
        return self.owner._current(self.scope, handle)['snapshot']['status']

    def block(self, handle, created_at_ms=0):
        """A reservation row of `handle` whose sealed binding does not verify: `_unfinished` refuses the close."""
        self.store._connection.execute(
            "INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,orchestration_handle,"
            "binding,assignment_digest,created_at_ms,hard_deadline_at_ms,reserved_until_ms,expires_at_ms,state) "
            "VALUES(?,?,?,1,?,?,x'00','a',?,0,0,0,'expired')",
            (PROJECT_A, 'clm_' + handle[-8:], 'hrn_' + handle[-8:], hashlib.sha256(handle.encode()).hexdigest(),
             handle, created_at_ms))


class RegisterRowTests(OrchestrationStore):
    @requirements('RS-B-B6-1', why_not_scenario=WHY)
    def test_B6_1_open_takes_a_held_row_activity_moves_its_due_time_and_the_primary_close_releases_it_closed(self):
        opened = self.open()
        handle = opened['orchestration_handle']
        self.assertEqual(('held', None, 1000, 1000 + ORCHESTRATION_IDLE_MS, 0, None), hold(self.store, handle))
        self.assertEqual('open', state_column(self.store, handle))
        self.now = 5000
        current = self.call('checkpoint', 'checkpoint', self.target(opened, goal_tokens_used=1,
                                                                    goal_time_used_seconds=1))['current']['snapshot']
        self.assertEqual(5000 + ORCHESTRATION_IDLE_MS, hold(self.store, handle)[3])
        self.now = 6000
        current = self.call('pause', 'pause', self.target(current))['current']['snapshot']
        self.assertEqual(6000 + ORCHESTRATION_IDLE_MS, hold(self.store, handle)[3])
        self.now = 6500
        current = self.call('resume', 'resume', self.target(current))['current']['snapshot']
        self.assertEqual(6500 + ORCHESTRATION_IDLE_MS, hold(self.store, handle)[3])
        self.now = 7000
        self.close(current, status='interrupted')  # the primary's own close, whatever status it gives
        state, kind, taken, _, _, evidence = hold(self.store, handle)
        self.assertEqual(('released', 'closed', 1000, {'by': 'primary'}), (state, kind, taken, json.loads(evidence)))
        self.assertEqual('closed', state_column(self.store, handle))
        consistent(self, self.store)
        self.reopen()
        consistent(self, self.store)

    @requirements('RS-B-B6-1', why_not_scenario=WHY)
    def test_B6_1_end_earlier_releases_the_row_taken_at_open_closed_and_the_unattended_close_interrupted_unattended(
            self):
        from dataclasses import replace
        updated = self.open('open-updated')['orchestration_handle']
        idle = self.open('open-idle')['orchestration_handle']
        self.now = 1500
        self.assertEqual(2, self.owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))
        state, kind, taken, _, _, evidence = hold(self.store, updated)
        self.assertEqual(('released', 'closed', 1000, {'ended_by_update_unrecorded': False}),
                         (state, kind, taken, json.loads(evidence)))
        self.assertEqual({'ended_by_update_unrecorded': 0}, self.store.runtime_close_status())
        other = self.open('open-other')['orchestration_handle']
        self.now = 1500 + ORCHESTRATION_IDLE_MS
        self.assertEqual(1, self.owner.end_idle(self.now))
        state, kind, _, _, _, evidence = hold(self.store, other)
        self.assertEqual(('released', 'interrupted_unattended', {'by': 'unattended'}),
                         (state, kind, json.loads(evidence)))
        self.assertEqual(('interrupted', 'closed'), (self.status(other), state_column(self.store, other)))
        self.assertEqual('released', hold(self.store, idle)[0])
        consistent(self, self.store)

    @requirements('RS-B-B6-1', why_not_scenario=WHY)
    def test_B6_1_a_failed_release_write_rolls_the_close_back(self):
        opened = self.open()
        handle = opened['orchestration_handle']
        self.store._connection.execute(
            "CREATE TEMP TRIGGER cut_release BEFORE UPDATE ON project_holds WHEN NEW.kind='orchestration' "
            "AND NEW.state='released' BEGIN SELECT RAISE(ABORT,'fixture release cut'); END")
        with self.assertRaises(HistoryError):
            self.close(opened)
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(0, self.owner.end_idle(self.now))
        self.assertEqual(('active', 'open', 'held'),
                         (self.status(handle), state_column(self.store, handle), hold(self.store, handle)[0]))
        self.store._connection.execute('DROP TRIGGER cut_release')
        self.close(opened)
        self.assertEqual(('released', 'closed'), hold(self.store, handle)[:2])
        consistent(self, self.store)

    @requirements('RS-B-B6-1', why_not_scenario=WHY)
    def test_B6_1_the_validator_refuses_an_open_orchestration_without_its_held_row_and_a_held_row_of_a_closed_one(
            self):
        opened = self.open()
        handle = opened['orchestration_handle']
        closed = self.open('open-closed')
        self.close(closed)
        self.store.close()
        saved = {path.name: path.read_bytes() for path in self.directory.glob('history.sqlite3*')}
        for change in ("DELETE FROM project_holds WHERE kind='orchestration' AND identity=:open",
                       "UPDATE project_holds SET state='released',release_kind='closed',released_at_ms=1 "
                       "WHERE kind='orchestration' AND identity=:open",
                       "UPDATE project_orchestrations SET state='closed' WHERE orchestration_handle=:open",
                       "UPDATE project_holds SET state='held',release_kind=NULL,released_at_ms=NULL "
                       "WHERE kind='orchestration' AND identity=:closed"):
            with self.subTest(change=change):
                with closing(sqlite3.connect(self.directory / 'history.sqlite3')) as raw:
                    raw.execute('PRAGMA foreign_keys=OFF')
                    raw.execute(change, {'open': handle, 'closed': closed['orchestration_handle']})
                    raw.commit()
                try:
                    self.open_history().close()
                except HistoryError as refused:
                    self.assertEqual('history_incompatible', refused.reason_code)
                else:
                    self.fail('opened: ' + change)
                for path in self.directory.glob('history.sqlite3*'):
                    path.unlink()
                for name, content in saved.items():
                    (self.directory / name).write_bytes(content)
                    (self.directory / name).chmod(0o600)
        self.store = self.open_history()
        consistent(self, self.store)


class EligibilityTests(OrchestrationStore):
    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_the_unattended_close_waits_for_its_due_time_and_then_refuses_new_work(self):
        handle = self.open()['orchestration_handle']
        binding = SimpleNamespace(**{name: getattr(self.scope, name) for name in SCOPE_FIELDS},
                                  orchestration_handle=handle)
        self.owner.active_for(binding)
        self.now = 1000 + ORCHESTRATION_IDLE_MS - 1
        self.assertEqual(0, self.owner.end_idle(self.now))
        self.assertEqual(('active', 0), (self.status(handle), hold(self.store, handle)[4]))  # not due: no back-off
        self.now += 1
        self.assertEqual(1, self.owner.end_idle(self.now))
        self.assertEqual('interrupted', self.status(handle))
        # Memory kinds are not checked (decision 129.1): a registered run with no lease gets no new work after it.
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.owner.active_for(binding)

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_effective_now_is_the_persisted_floor_when_the_wall_clock_went_back(self):
        handle = self.open()['orchestration_handle']
        self.now = 1000 + ORCHESTRATION_IDLE_MS + 5
        self.store._result_now()  # the floor moves to here (any result operation does it)
        self.now = 10  # the wall clock went back below the floor
        self.assertEqual(1, self.owner.end_idle(self.now))
        self.assertEqual(('released', 'interrupted_unattended'), hold(self.store, handle)[:2])
        ended = self.owner._current(self.scope, handle)['snapshot']
        self.assertEqual(1000 + ORCHESTRATION_IDLE_MS + 5, ended['ended_at_ms'])

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_the_eligibility_is_checked_inside_the_close_transaction(self):
        opened = self.open()
        handle = opened['orchestration_handle']
        request = self.owner._unattended_request(handle, opened['sequence'], opened['scope'])
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_pending'):  # not due yet
            self.owner.apply(request, client_version=CLIENT, before_commit=lambda: None, _unattended=True)
        self.assertEqual(('active', 'held'), (self.status(handle), hold(self.store, handle)[0]))


class EligibilityWithRunsTests(rc.RecordedContextFixture):
    def setUp(self):
        super().setUp()
        if self.store._orchestration_limits is None:
            self.store._orchestration_limits = OrchestrationLimits(4, 12)

    @property
    def owner(self):
        return self.store._orchestration_owner()

    @property
    def handle(self):
        return self.binding.orchestration_handle

    def snapshot(self):
        return self.owner.metadata_for(self.binding)['snapshot']

    def primary_close(self):
        snapshot = self.snapshot()
        scope = OrchestrationScope(**snapshot['scope'])
        request = OrchestrationRequest(1, 'orchestration.close', 'primary-close', scope, {
            'orchestration_handle': self.handle, 'expected_sequence': snapshot['sequence'], 'status': 'completed',
            'goal_tokens_used': None, 'goal_time_used_seconds': None})
        return self.owner.apply(request, client_version=snapshot['client_version'], before_commit=lambda: None)

    def past_due(self):
        self.fixture.base.stamp = hold(self.store, self.handle)[3] + ORCHESTRATION_IDLE_MS

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_an_unfinished_attempt_keeps_it_open(self):
        self.open_and_consume()  # opens the orchestration; the attempt is bound and not committed: `_unfinished`
        self.past_due()
        self.assertEqual(0, self.owner.end_idle(self.fixture.base.stamp))
        self.assertEqual(('active', 'held'), (self.snapshot()['status'], hold(self.store, self.handle)[0]))

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_a_recorded_result_whose_reservation_is_still_held_blocks_the_unattended_close_not_the_primarys(
            self):
        self.recorded()
        self.assertEqual('held', self.store._connection.execute(
            "SELECT state FROM project_holds WHERE kind='reservation' AND identity=?",
            (self.binding.claim_id,)).fetchone()[0])
        self.past_due()
        self.assertEqual(0, self.owner.end_idle(self.fixture.base.stamp))
        self.assertEqual('active', self.snapshot()['status'])
        self.assertGreater(hold(self.store, self.handle)[4], 0)  # backed off
        self.primary_close()
        self.assertEqual(('released', 'closed'), hold(self.store, self.handle)[:2])
        consistent(self, self.store)

    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_releasing_a_reservation_of_the_orchestration_resets_its_next_check(self):
        self.recorded()
        self.past_due()
        self.assertEqual(0, self.owner.end_idle(self.fixture.base.stamp))
        self.assertGreater(hold(self.store, self.handle)[4], 0)
        db = self.store._connection

        class Undo(Exception):
            pass

        with self.assertRaises(Undo):
            with self.store._transaction():
                project_holds_schema.release_hold(db, self.binding.project_id, 'reservation', self.binding.claim_id,
                                                  'completed', now_ms=self.fixture.base.stamp)
                self.assertEqual((0, None), hold(self.store, self.handle)[4:])
                raise Undo  # the fixture has no account release: undo the synthetic release
        self.assertGreater(hold(self.store, self.handle)[4], 0)

    @requirements('RS-B-B6-5', why_not_scenario=WHY)
    def test_B6_5_K2_an_admitted_attempt_without_release_or_settlement_is_never_purged(self):
        self.recorded()
        self.primary_close()
        db = self.store._connection
        reservation = self.store.result_reservation(self.binding)
        for late in (RESULT_PURGE_GRACE_MS + 1, ORCHESTRATION_RETENTION_MS + 1):
            self.fixture.base.stamp = reservation.hard_deadline_at_ms + late
            for _ in range(4):
                try:
                    self.store.expire_results()
                except HistoryError:
                    pass
            self.assertEqual((1, 1, 1), tuple(db.execute(
                'SELECT (SELECT COUNT(*) FROM result_reservations),(SELECT COUNT(*) FROM result_leases),'
                '(SELECT COUNT(*) FROM project_cleanup_reservations)').fetchone()))
            self.assertEqual('held', db.execute("SELECT state FROM project_holds WHERE kind='reservation' "
                                                "AND identity=?", (self.binding.claim_id,)).fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
        consistent(self, self.store)


class LeaseTests(OrchestrationStore):
    """A lease of a real orchestration (reserved, never armed, never admitted)."""

    def lease(self, handle):
        from codex_agents.result_contracts import ResultLeaseBinding
        from tests.learning_support import assignment
        self.lease_binding = ResultLeaseBinding(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1, handle,
                                                'hrn_' + '1' * 48, 'run-1', 1, 'claim-request-1', '1' * 64)
        return self.store.reserve_result_lease(self.lease_binding, assignment())

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_a_live_lease_keeps_it_open(self):
        handle = self.open()['orchestration_handle']
        self.lease(handle)
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(0, self.owner.end_idle(self.now))
        self.assertEqual(('active', 'held'), (self.status(handle), hold(self.store, handle)[0]))

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_taking_an_execution_hold_is_activity_releasing_one_is_not(self):
        handle = self.open()['orchestration_handle']
        self.now = 1000 + ORCHESTRATION_IDLE_MS - 10
        self.lease(handle)
        due = self.now + ORCHESTRATION_IDLE_MS
        self.assertEqual(due, hold(self.store, handle)[3])
        self.now += 5
        self.store.release_unsent_result_lease(self.lease_binding)
        self.assertEqual((due, 0), hold(self.store, handle)[3:5])
        self.now = due - 1
        self.assertEqual(0, self.owner.end_idle(self.now))
        self.assertEqual(0, hold(self.store, handle)[4])  # not due: no back-off
        self.now = due
        self.assertEqual(1, self.owner.end_idle(self.now))

    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_releasing_a_lease_of_the_orchestration_resets_its_next_check(self):
        handle = self.open()['orchestration_handle']
        self.lease(handle)
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(0, self.owner.end_idle(self.now))
        self.assertEqual(self.now + 10_000, hold(self.store, handle)[4])
        self.store.release_unsent_result_lease(self.lease_binding)  # the lease's release resets the row
        self.assertEqual((0, None), hold(self.store, handle)[4:])
        self.assertEqual(1, self.owner.end_idle(self.now))
        self.assertEqual(('released', 'interrupted_unattended'), hold(self.store, handle)[:2])
        consistent(self, self.store)

    @requirements('RS-B-B6-5', why_not_scenario=WHY)
    def test_B6_5_K1_a_never_admitted_attempt_of_a_closed_orchestration_purges_through_its_register_release(self):
        opened = self.open()
        handle = opened['orchestration_handle']
        lease = self.lease(handle)
        self.assertIsNotNone(lease.cleanup_reservation_digest)
        self.store.release_unsent_result_lease(self.lease_binding)
        self.close(opened)
        db = self.store._connection
        self.now = lease.reserved_until_ms + RESULT_PURGE_GRACE_MS + 1
        self.store.expire_results()
        self.assertEqual((0, 0), (db.execute('SELECT COUNT(*) FROM result_leases').fetchone()[0],
                                  db.execute('SELECT COUNT(*) FROM project_cleanup_reservations').fetchone()[0]))
        self.assertEqual(('released', 'withdrawn_unsent'), tuple(db.execute(
            "SELECT state,release_kind FROM project_holds WHERE kind='lease' AND identity=?",
            (f'{self.lease_binding.run_handle}:1',)).fetchone()))
        self.assertEqual(('released', 'closed'), hold(self.store, handle)[:2])
        consistent(self, self.store)


    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_a_blocked_row_never_stops_a_later_due_row(self):
        blocked = self.open('open-blocked')['orchestration_handle']
        later = self.open('open-later')['orchestration_handle']
        self.lease(blocked)  # a live lease: only this one is unfinished
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(0, self.owner.end_idle(self.now, 1))  # the first selected is the blocked one
        self.assertEqual(1, self.owner.end_idle(self.now, 1))  # it waits; the later one closes
        self.assertEqual(('active', 'interrupted'), (self.status(blocked), self.status(later)))
        plan = ' '.join(row[3] for row in self.store._connection.execute(
            'EXPLAIN QUERY PLAN ' + self.owner._UNATTENDED_SELECT, (PROJECT_A, self.now, self.now, 4)))
        self.assertIn('project_holds_orchestration_check', plan)

class ReplayAndFairnessTests(OrchestrationStore):
    @requirements('RS-B-B6-3', why_not_scenario=WHY)
    def test_B6_3_the_request_is_derived_so_a_replay_is_the_identical_request_and_answers_known(self):
        opened = self.open()
        handle = opened['orchestration_handle']
        request = self.owner._unattended_request(handle, opened['sequence'], opened['scope'])
        self.assertRegex(request.request_id, r'\Aend-idle-[0-9a-f]{32}\Z')
        self.assertEqual(request, self.owner._unattended_request(handle, opened['sequence'], opened['scope']))
        self.reopen()
        self.assertEqual(request, self.owner._unattended_request(handle, opened['sequence'], opened['scope']))
        self.assertNotEqual(request.request_id,
                            self.owner._unattended_request(handle, opened['sequence'] + 1, opened['scope']).request_id)
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(1, self.owner.end_idle(self.now))
        checkpoints = self.store._connection.execute('SELECT COUNT(*) FROM project_orchestration_checkpoints'
                                                     ).fetchone()[0]
        current = self.owner._current(self.scope, handle)
        replay = self.owner.apply(request, client_version=CLIENT, before_commit=lambda: None, _unattended=True)
        self.assertTrue(replay['known'])
        self.assertEqual(current['checkpoint_digest'], replay['receipt']['checkpoint_digest'])
        self.assertEqual(checkpoints, self.store._connection.execute(
            'SELECT COUNT(*) FROM project_orchestration_checkpoints').fetchone()[0])

    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_at_most_four_close_per_pass_and_the_service_asks_for_four(self):
        from codex_agents.orchestration_store import UNATTENDED_CLOSE_BATCH
        self.assertEqual(4, UNATTENDED_CLOSE_BATCH)
        handles = [self.open(f'open-{n}')['orchestration_handle'] for n in range(6)]
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(4, self.owner.end_idle(self.now))
        self.assertEqual(2, self.owner.end_idle(self.now))
        self.assertEqual({'interrupted'}, {self.status(handle) for handle in handles})
        source = (ORCHESTRATION_ROOT / 'codex_agents' / 'result_persistence.py').read_text()
        self.assertIn('end_idle(None, UNATTENDED_CLOSE_BATCH)', source)  # review r1: the owner reads its own time

    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_a_blocked_row_backs_off_from_10_seconds_doubling_to_5_minutes(self):
        handle = self.open()['orchestration_handle']
        self.block(handle)
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        waits = []
        for _ in range(8):
            self.assertEqual(0, self.owner.end_idle(self.now))
            next_check = hold(self.store, handle)[4]
            waits.append(next_check - self.now)
            self.assertEqual(0, self.owner.end_idle(next_check - 1))  # not selected before its next check
            self.assertEqual(next_check, hold(self.store, handle)[4])
            self.now = next_check
        self.assertEqual([10_000, 20_000, 40_000, 80_000, 160_000, 300_000, 300_000, 300_000], waits)
        self.assertEqual('active', self.status(handle))


class OrchestrationStateTests(OrchestrationStore):
    @requirements('RS-B-B6-8', why_not_scenario=WHY)
    def test_B6_8_counting_open_orchestrations_reads_the_indexed_state_not_the_checkpoints(self):
        self.open()
        measurements = []
        for extra in (0, 1000):
            db = self.store._connection
            if extra:
                with self.store._transaction():
                    db.executemany('INSERT INTO project_orchestrations(project_id,orchestration_handle,scope_digest,'
                                   "sequence,checkpoint_digest,state) VALUES(?,?,?,2,?,'closed')",
                                   ((PROJECT_A, f'orc_closed_{n}', 'c' * 64, 'd' * 64) for n in range(extra)))
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(progress, 1)
            try:
                count = self.owner._open_count()
            finally:
                db.set_progress_handler(None, 0)
            measurements.append((count, steps))
        self.assertEqual(measurements[0], measurements[1])
        self.assertEqual(1, measurements[0][0])
        plan = ' '.join(row[3] for row in self.store._connection.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM project_orchestrations WHERE project_id=? AND state='open'",
            (PROJECT_A,)))
        self.assertIn('project_orchestrations_state', plan)


class SchemaStepTests(OrchestrationStore):
    def setUp(self):
        super().setUp()
        self.store.close()
        self.database = self.directory / 'history.sqlite3'
        for name in self.directory.iterdir():
            name.unlink()
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(STEP20.read_text())
        self.database.chmod(0o600)
        self.store = SimpleNamespace(close=lambda: None)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: sorted((tuple(row) for row in db.execute('SELECT * FROM ' + name)), key=repr)
                    for name in names}

    def version(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def copies(self):
        return sorted(path for path in self.directory.glob('history.sqlite3.before-*')
                      if not path.name.endswith('.partial'))

    def upgrade(self):
        self.store = self.open_history(allow_schema_upgrade=True)
        return self.store

    def assert_left_at_20(self, before, digest):
        self.assertEqual(20, self.version())
        self.assertEqual(before, self.rows())
        self.assertEqual(digest, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertEqual([], self.copies())
        self.assertFalse(list(self.directory.glob('*.partial')))

    @requirements('RS-B-B6-7', why_not_scenario=WHY)
    def test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations(self):
        before = self.rows()
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(20, self.version())
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()  # an ordinary open never upgrades
        self.assert_left_at_20(before, digest)
        self.now = 4000
        store = self.upgrade()
        db = store._connection
        self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(1, len(self.copies()))
        self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-21.'))
        with closing(sqlite3.connect(self.copies()[0])) as copy:
            self.assertEqual(20, copy.execute('PRAGMA user_version').fetchone()[0])
        after = self.rows()
        orchestrations = after.pop('project_orchestrations')
        holds = after.pop('project_holds')
        self.assertEqual(before.pop('project_orchestrations'), sorted((row[:5] for row in orchestrations), key=repr))
        old_holds = before.pop('project_holds')
        self.assertEqual(before, after)  # every other table byte for byte
        self.assertLessEqual(set(old_holds), set(holds))  # the update's tombstone kept as it was
        statuses = {}
        owner = store._orchestration_owner()
        for row in db.execute('SELECT orchestration_handle,sequence,checkpoint_digest,state FROM project_orchestrations'):
            snapshot = owner._latest_snapshot(row)
            statuses[row[0]] = snapshot['status']
            self.assertEqual('closed' if snapshot['status'] in TERMINAL else 'open', row[3])
            current = hold(store, row[0])
            if snapshot['status'] in TERMINAL:
                self.assertTrue(current is None or current[0] == 'released')
            else:
                self.assertEqual(('held', None, snapshot['started_at_ms'],
                                  snapshot['observed_at_ms'] + ORCHESTRATION_IDLE_MS, 0, {'adopted': 21}),
                                 (*current[:5], json.loads(current[5])))
        self.assertEqual(['active', 'completed', 'interrupted', 'paused'], sorted(statuses.values()))
        self.assertEqual(2, owner._open_count())
        consistent(self, store)
        # The fresh format and the upgraded one are the same schema.
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as other:
            fresh_directory = Path(other) / 'history'
            fresh_directory.mkdir(mode=0o700)
            fresh = ProjectHistory(fresh_directory, PROJECT_A, result_key=KEY,
                                   result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                                   result_clock_ms=lambda: self.now)
            schema = "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            self.assertEqual(fresh._connection.execute(schema).fetchall(), db.execute(schema).fetchall())
            fresh.close()
        # Repeated opens change nothing.
        upgraded = self.rows()
        self.store.close()
        self.store = self.open_history()
        self.assertEqual(upgraded, self.rows())
        self.assertEqual(1, len(self.copies()))
        # The adopted ones close unattended at their due time; the closed ones purge after retention as before.
        owner = self.store._orchestration_owner()
        self.now = 2500 + ORCHESTRATION_IDLE_MS
        self.assertEqual(2, owner.end_idle(self.now))
        self.assertEqual(0, owner._open_count())
        self.now = 3000 + ORCHESTRATION_IDLE_MS + ORCHESTRATION_RETENTION_MS + 1
        for _ in range(3):
            owner.purge_closed(self.now, 10)
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
        self.assertEqual({'ended_by_update_unrecorded': 0}, self.store.runtime_close_status())
        consistent(self, self.store)

    @requirements('RS-B-B6-7', why_not_scenario=WHY)
    def test_B6_7_a_failure_inside_the_step_leaves_20_and_removes_its_copy_then_a_later_attempt_upgrades(self):
        before = self.rows()
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        original = project_holds_schema.adopt_orchestrations

        def fail(*arguments, **options):
            original(*arguments, **options)  # every write of the step done, then the failure
            raise OSError('fixture step cut')

        with mock.patch.object(project_holds_schema, 'adopt_orchestrations', side_effect=fail):
            with self.assertRaises((HistoryError, OSError)):
                self.upgrade()
        self.assert_left_at_20(before, digest)
        self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])

    @requirements('RS-B-B6-7', why_not_scenario=WHY)
    def test_B6_7_process_death_inside_the_step_leaves_20_and_a_later_attempt_upgrades(self):
        before = self.rows()
        script = (
            'import os, sys\n'
            f'sys.path.insert(0, {str(ORCHESTRATION_ROOT)!r})\n'
            'from pathlib import Path\n'
            'from codex_agents import project_holds_schema\n'
            'from codex_agents.project_history import ProjectHistory\n'
            'from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity\n'
            'original = project_holds_schema.adopt_orchestrations\n'
            'def die(*a, **k):\n'
            '    original(*a, **k)\n'
            '    os._exit(9)\n'
            'project_holds_schema.adopt_orchestrations = die\n'
            f'ProjectHistory(Path({str(self.directory)!r}), {PROJECT_A!r}, result_key={KEY!r}, '
            'result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000), '
            'result_clock_ms=lambda: 4000, allow_schema_upgrade=True)\n')
        completed = subprocess.run([sys.executable, '-I', '-B', '-c', script], capture_output=True, timeout=60)
        self.assertEqual(9, completed.returncode, completed.stderr)
        self.assertEqual(20, self.version())
        self.assertEqual(before, self.rows())
        self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])
        # B6 review r1 (MED crash copy): the dead attempt's own copy is removed by the retry; exactly one is left,
        # the store as it was (format 20), and no attempt marker or temporary file stays behind.
        (copy,) = self.copies()
        with closing(sqlite3.connect(copy)) as kept:
            self.assertEqual(20, kept.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual([], [path.name for path in self.directory.iterdir()
                              if path.name.endswith(('.partial', '.attempt'))])

    @requirements('RS-B-B6-7', why_not_scenario=WHY)
    def test_B6_7_an_unknown_object_in_a_step_20_store_is_refused_before_any_write(self):
        for statement in ('CREATE TRIGGER unexpected AFTER INSERT ON project_holds BEGIN SELECT 1; END',
                          'CREATE INDEX unexpected ON project_orchestrations(sequence)',
                          "ALTER TABLE project_orchestrations ADD COLUMN state TEXT NOT NULL DEFAULT 'open'"):
            with self.subTest(statement=statement):
                with closing(sqlite3.connect(self.database)) as db:
                    db.execute(statement)
                    db.commit()
                before = self.rows()
                digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
                with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
                    self.upgrade()
                self.assert_left_at_20(before, digest)
                with closing(sqlite3.connect(self.database)) as db:
                    db.executescript('DROP TRIGGER IF EXISTS unexpected; DROP INDEX IF EXISTS unexpected;')
                    if 'ADD COLUMN' in statement:
                        db.execute('ALTER TABLE project_orchestrations DROP COLUMN state')
                    db.commit()
        self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])

    @requirements('RS-B-B6-7', 'RS-D-9-S3', why_not_scenario=WHY)
    def test_B6_7_the_step_adds_the_retirement_partial_index(self):
        db = self.upgrade()._connection
        indexes = {row[0]: row[1] for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        self.assertIn("WHERE state<>'expired'", indexes['result_reservations_live'])
        self.assertIn("WHERE kind='orchestration' AND state='held'", indexes['project_holds_orchestration_check'])
        self.assertIn('project_orchestrations_state', indexes)



# B6 review round 1 (Codex security review of 700d41f8): each HIGH and MED with a behavioural test, red on 700d41f8.

def handles_by_start(database):
    """{started_at_ms: handle} of a raw store's orchestrations, from their first checkpoints (plain JSON)."""
    with closing(sqlite3.connect(database)) as db:
        return {json.loads(content)['snapshot']['started_at_ms']: handle for handle, content in db.execute(
            'SELECT orchestration_handle,content FROM project_orchestration_checkpoints WHERE sequence=1')}


class Step20Store(OrchestrationStore):
    """SchemaStepTests' store and helpers, without its tests."""
    fixture = STEP20
    rows, version, copies = SchemaStepTests.rows, SchemaStepTests.version, SchemaStepTests.copies
    upgrade, assert_left_at_20 = SchemaStepTests.upgrade, SchemaStepTests.assert_left_at_20

    def setUp(self):
        super().setUp()
        self.store.close()
        self.database = self.directory / 'history.sqlite3'
        for name in self.directory.iterdir():
            name.unlink()
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(self.fixture.read_text())
        self.database.chmod(0o600)
        self.store = SimpleNamespace(close=lambda: None)


class ReviewAdoptionTests(Step20Store):
    """HIGH (adoption idle window): the backfill's last activity includes the latest attributable lease and
    reservation take, and an existing held row with no due time gets one."""
    fixture = STEP20_LEASES

    def status_of(self, handle):
        return self.store._orchestration_owner()._current(self.scope, handle)['snapshot']['status']

    @requirements('RS-B-B6-7', 'RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_7_adoption_counts_the_latest_lease_and_reservation_take_and_dates_a_held_row_without_a_due_time(
            self):
        starts = handles_by_start(self.database)
        withdrawn, bound, quiet = starts[3500], starts[3800], starts[4100]
        with closing(sqlite3.connect(self.database)) as db:
            # A held orchestration row with no due time (no step-20 writer leaves one; a damaged or hand-made row).
            db.execute("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms) "
                       "VALUES(?,'orchestration',?,'held',4100)", (PROJECT_A, quiet))
            db.commit()
        self.now = 5000
        store = self.upgrade()
        # withdrawn: checkpoint 3500, lease taken 3600 (withdrawn unsent at 3700: a release is not activity).
        self.assertEqual(3600 + ORCHESTRATION_IDLE_MS, hold(store, withdrawn)[3])
        # bound: checkpoint 3800, lease (and the reservation's created_at_ms) 3900, reservation's hold taken 4000.
        self.assertEqual(4000 + ORCHESTRATION_IDLE_MS, hold(store, bound)[3])
        # quiet: its existing held row keeps its take and evidence and gets the due time of its last activity.
        self.assertEqual(('held', None, 4100, 4100 + ORCHESTRATION_IDLE_MS, 0, None), hold(store, quiet))
        consistent(self, store)
        owner = store._orchestration_owner()
        self.now = 3600 + ORCHESTRATION_IDLE_MS - 1
        owner.end_idle(self.now)  # the step-20 active and paused ones are due; this one is not
        self.assertEqual('active', self.status_of(withdrawn))
        self.now = 3600 + ORCHESTRATION_IDLE_MS
        owner.end_idle(self.now)
        self.assertEqual('interrupted', self.status_of(withdrawn))
        self.now = 4100 + ORCHESTRATION_IDLE_MS
        owner.end_idle(self.now)
        self.assertEqual('interrupted', self.status_of(quiet))  # selected: its row now has a due time
        self.assertEqual('active', self.status_of(bound))  # its reservation is still held


OTHER = 'orc_' + 'e' * 48


def retain(store, count):
    """`count` finished attempts of another orchestration kept in the store: leases (expired), reservations
    (expired), consumptions and their released register rows. None of them is the target's. Both stores also get the
    same two neighbours (orchestrations sorting before and after any target), so an index seek ends on a neighbour
    in both and the only difference measured is the retained history."""
    db = store._connection
    owners = ['orc_' + '0' * 48, 'orc_' + 'f' * 48] + [OTHER] * count
    with store._transaction():
        db.executemany(
            'INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,orchestration_handle,'
            "binding,assignment_digest,created_at_ms,hard_deadline_at_ms,reserved_until_ms,expires_at_ms,state) "
            "VALUES(?,?,?,1,?,?,x'7b7d','a',0,1,1,1,'expired')",
            ((PROJECT_A, f'clm-r-{n}', f'hrn-r-{n}', f'{n:064x}', owner) for n, owner in enumerate(owners)))
        db.executemany(
            'INSERT INTO result_leases(project_id,run_handle,attempt,claim_request_id,binding,assignment_digest,'
            "created_at_ms,reserved_until_ms,state,lease_digest,state_digest) VALUES(?,?,1,?,?,'a',0,1,'expired','d','s')",
            ((PROJECT_A, f'hrn-l-{n}', f'req-{n}', json.dumps({'orchestration_handle': owner}).encode())
             for n, owner in enumerate(owners)))
        db.executemany('INSERT INTO project_execution_consumptions VALUES(?,?,?,x\'00\',0)',
                       ((PROJECT_A, f'clm-r-{n}', f'reg-{n}') for n, owner in enumerate(owners)))
        db.executemany("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,"
                       "release_kind) VALUES(?,'reservation',?,'released',0,0,'completed')",
                       ((PROJECT_A, f'clm-r-{n}') for n, owner in enumerate(owners)))
        db.executemany("INSERT INTO project_holds(project_id,kind,identity,state,lease_case,taken_at_ms,"
                       "released_at_ms,release_kind) VALUES(?,'lease',?,'released','unarmed',0,0,'withdrawn_unsent')",
                       ((PROJECT_A, f'hrn-l-{n}:1') for n, owner in enumerate(owners)))


def vm_steps(db, call):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    db.set_progress_handler(progress, 1)
    try:
        result = call()
    finally:
        db.set_progress_handler(None, 0)
    return steps, result


class ReviewRetainedHistoryTests(OrchestrationStore):
    """HIGH (retained history): a close checks only the target orchestration's own obligations, through indexes:
    other orchestrations' finished rows neither refuse it (the 4,096-row bound) nor change its work."""
    lease = LeaseTests.lease

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_a_complete_close_does_equal_work_at_0_and_100000_retained_rows_of_other_orchestrations(self):
        measurements = []
        for count in (0, 100_000):
            with self.subTest(retained=count):
                if count:
                    self.store.close()
                    for path in self.directory.iterdir():
                        path.unlink()
                    self.now = 1000
                    self.store = self.open_history()
                primary = self.open('open-primary')
                unattended = self.open('open-unattended')['orchestration_handle']
                self.lease(primary['orchestration_handle'])  # the target's own finished attempt (withdrawn)
                self.store.release_unsent_result_lease(self.lease_binding)
                primary = self.owner._current(self.scope, primary['orchestration_handle'])['snapshot']
                retain(self.store, count)
                db = self.store._connection
                self.now = 1000 + ORCHESTRATION_IDLE_MS
                primary_steps, _ = vm_steps(db, lambda: self.close(primary))
                unattended_steps, ended = vm_steps(db, lambda: self.owner.end_idle(self.now))
                self.assertEqual(('completed', 1, 'interrupted'),
                                 (self.status(primary['orchestration_handle']), ended, self.status(unattended)))
                measurements.append((primary_steps, unattended_steps))
        self.assertEqual(measurements[0], measurements[1], ('VM steps at 0 and 100000 retained rows', measurements))


class ReviewClockTests(OrchestrationStore):
    """MED (clock rollback): the service's pass closes an idle orchestration at the floor when the wall clock went
    back below it, and the decision time is read inside the close's transaction."""

    def service_pass(self):
        import threading
        from codex_agents import result_persistence
        lane = SimpleNamespace(
            _history=lambda: self.store, _service_scope=self.scope, _service_instance='instance-1',
            _lock=threading.Lock(), _jobs={}, _unanswered={}, _earlier_held=1, _reporting=0,
            _adopt_committed=lambda *arguments, **options: None, _record_authority=lambda: None,
            recording_notes=None)
        request = result_persistence._EarlierLeases('expire')
        result_persistence.ResultPersistenceLane._earlier(lane, request)
        return request

    @requirements('RS-B-B6-2', why_not_scenario=WHY)
    def test_B6_2_the_service_pass_closes_at_the_floor_after_the_wall_clock_went_back(self):
        handle = self.open()['orchestration_handle']
        floor = 1000 + ORCHESTRATION_IDLE_MS + 5
        self.now = floor
        self.store._result_now()  # the floor moves to here
        self.now = 10  # the wall clock went back below the floor
        reads = []
        original = self.store._hold_now

        def hold_now():
            reads.append(self.store._connection.in_transaction)
            return original()
        self.store._hold_now = hold_now
        self.service_pass()
        self.assertEqual(('released', 'interrupted_unattended'), hold(self.store, handle)[:2])
        ended = self.owner._current(self.scope, handle)['snapshot']
        self.assertEqual(('interrupted', floor), (ended['status'], ended['ended_at_ms']))
        self.assertIn(True, reads)  # the close's decision time is read inside its transaction
        self.assertEqual(floor, self.store._connection.execute(
            'SELECT last_observed_at_ms FROM result_identity').fetchone()[0])  # never moved back


class ReviewLayoutTests(Step20Store):
    """MED (layout): the schema comparison keeps quoted literals as they are (no case folding inside them)."""

    @requirements('RS-B-B6-7', why_not_scenario=WHY)
    def test_B6_7_an_index_predicate_whose_literal_differs_only_in_case_is_refused(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("DROP INDEX project_holds_due; CREATE INDEX project_holds_due ON "
                             "project_holds(state,next_check_ms) WHERE state<>'RELEASED';")
        before = self.rows()
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.upgrade()
        self.assert_left_at_20(before, digest)
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("DROP INDEX project_holds_due; CREATE INDEX project_holds_due ON "
                             "project_holds(state,next_check_ms) WHERE state<>'released';")
        self.upgrade().close()
        with closing(sqlite3.connect(self.database)) as db:  # the same at an ordinary open of format 21
            db.executescript("DROP INDEX result_reservations_live; CREATE INDEX result_reservations_live ON "
                             "result_reservations(project_id) WHERE state<>'EXPIRED';")
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()


class ReviewStateValidationTests(OrchestrationStore):
    """MED (state validation): at open the state column is checked against each orchestration's latest sealed
    checkpoint, so a matching but wrong state and register row cannot undercount open orchestrations."""

    @requirements('RS-B-B6-8', 'RS-B-B6-1', why_not_scenario=WHY)
    def test_B6_8_a_closed_state_and_released_row_of_an_active_orchestration_is_refused_at_open(self):
        handle = self.open()['orchestration_handle']
        self.store.close()
        with closing(sqlite3.connect(self.directory / 'history.sqlite3')) as raw:
            raw.execute("UPDATE project_orchestrations SET state='closed' WHERE orchestration_handle=?", (handle,))
            raw.execute("UPDATE project_holds SET state='released',release_kind='closed',released_at_ms=1 "
                        "WHERE kind='orchestration' AND identity=?", (handle,))
            raw.commit()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()
        self.store = SimpleNamespace(close=lambda: None)


class ReviewBatchTests(OrchestrationStore):
    @requirements('RS-B-B6-4', why_not_scenario=WHY)
    def test_B6_4_an_explicit_limit_above_four_is_clamped_to_four(self):
        handles = [self.open(f'open-{n}')['orchestration_handle'] for n in range(6)]
        self.now = 1000 + ORCHESTRATION_IDLE_MS
        self.assertEqual(4, self.owner.end_idle(self.now, 10))
        self.assertEqual(2, sum(self.status(handle) == 'active' for handle in handles))


if __name__ == '__main__':
    unittest.main()
