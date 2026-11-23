"""Slice B2 fix round 1: the recording sweep and the writer lane (rev040b2r1a MED-1 and LOWs, rev040b2r1b M1 and
the window / attempt-cap findings).

The lane tests drive the product's writer lane itself: the publication of a result through the lane's own writer loop
(which records right after it is stored), and the restart/backstop request (`expire_results`, the pass a restarted
service runs). The sweep tests use the lease-backed run fixtures, with the admission replaced only where a case needs an
unexpected failure. Time budget: scenario.
"""
import hashlib
import threading
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests import test_codex_worker as workers
from tests import test_deadlines_retention as dr
from tests import test_record_context as rc
from tests.test_b2_service_outcome import B2Fixture, requirement
from tests.test_bridge import result
from tests.test_project_history import binding as history_binding

from codex_agents import project_result_contexts, result_persistence, service_record
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory
from codex_agents.result_persistence import (MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceHandle,
                                             ResultPersistenceLane, _Pending)
from codex_agents.service_record import ServiceRecordAuthority


class SweepFixture(dr.LeaseBackedFixture):
    RUNS = 14

    def committed(self):
        for number in range(1, self.RUNS + 1):
            self.bound(number)
            self.publish(number)

    def authority(self):
        return ServiceRecordAuthority(history_binding().runtime_digest, True)

    def positions(self):
        return [row[0] for row in self.store._connection.execute(
            service_record.WINDOW_SQL, (self.store.project_id, 0, 0, service_record.SWEEP_WINDOW))]

    def drive(self, behave, *, passes=1, notes=None, start=0, step=1, **options):
        seen = []

        def admit(binding, *, final_check):
            seen.append(binding.claim_id)
            outcome = behave(binding)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                raise HistoryError(outcome)
            return {}
        counts = []
        with mock.patch.object(self.store, 'admit_service_result', side_effect=admit):
            for index in range(passes):
                counts.append(service_record.record_pass(self.store, self.authority(), notes=notes,
                                                         clock=lambda at=start + index * step: at, **options))
        return seen, counts

    def mark_recorded(self, binding):
        """What a real admission does to the run's context: it leaves the selection."""
        with self.store._transaction():
            project_result_contexts.record_outcome(self.store, binding, hashlib.sha256(binding.claim_id.encode()).hexdigest())


@requirement
class SweepIsolationTests(SweepFixture):
    """MED-1: one candidate's unexpected failure never stops the pass, loses its progress or skips the cleanup."""

    def test_an_unexpected_error_in_one_admission_is_a_signal_and_the_pass_goes_on(self):
        self.committed()
        bad = history_binding(2).claim_id
        notes = service_record.RecordingNotes()
        seen, (counts,) = self.drive(lambda binding: ValueError('malformed') if binding.claim_id == bad else None,
                                     notes=notes)
        self.assertEqual(service_record.SWEEP_ATTEMPTS, counts['attempted'])
        self.assertEqual(service_record.SWEEP_ATTEMPTS - 1, counts['recorded'])  # the others were recorded
        self.assertEqual({'unexpected_error': 1}, counts['refused'])
        self.assertEqual(self.positions()[service_record.SWEEP_ATTEMPTS - 1], self.store._purge_cursors['record'])
        self.assertEqual(1, len(self.store._record_sweep['backoff']))  # transient: retried later, not at once
        self.assertEqual(['error'], sorted({signal.split(':')[0] for signal in notes.snapshot()[1]}))

    def test_a_failure_while_building_the_binding_is_isolated_the_same_way(self):
        self.committed()
        bad_handle = self.attempt(3)[0]
        original = project_result_contexts._lease_binding

        def lease_binding(history, row):
            if row['run_handle'] == bad_handle:
                raise ValueError('malformed row')
            return original(history, row)
        with mock.patch.object(project_result_contexts, '_lease_binding', side_effect=lease_binding):
            seen, (counts,) = self.drive(lambda binding: None)
        self.assertEqual(({'unexpected_error': 1}, service_record.SWEEP_ATTEMPTS - 1),
                         (counts['refused'], counts['recorded']))
        self.assertEqual(self.positions()[service_record.SWEEP_ATTEMPTS - 1], self.store._purge_cursors['record'])

    def test_the_failed_candidate_is_tried_again_after_its_wait_and_the_pass_after_records_it(self):
        self.committed()
        bad = history_binding(2).claim_id
        failing = [True]
        seen, _ = self.drive(lambda binding: ValueError('x') if binding.claim_id == bad and failing[0] else None,
                             passes=1, start=0)
        failing[0] = False
        seen, counts = self.drive(lambda binding: None, passes=1, start=10 ** 9)
        self.assertIn(bad, seen)
        self.assertEqual({}, counts[0]['refused'])

    def test_every_error_class_a_candidate_can_raise_is_contained(self):
        self.committed()
        for error in (OSError('disk'), KeyError('k'), RuntimeError('r'), ValueError('v')):
            self.store._record_sweep = {'permanent': {}, 'backoff': {}, 'health': set()}
            self.store._purge_cursors['record'] = None
            seen, (counts,) = self.drive(lambda binding, error=error: error)
            self.assertEqual({'unexpected_error': service_record.SWEEP_ATTEMPTS}, counts['refused'], repr(error))

    def test_health_signals_are_bounded_and_cleared_when_the_run_recovers(self):
        self.committed()
        notes = service_record.RecordingNotes()
        key = history_binding(1).claim_id
        for index in range(service_record.HEALTH_TRANSIENT_FAILURES):
            self.drive(lambda binding: 'history_result_unavailable' if binding.claim_id == key else None,
                       start=(index + 1) * 10 ** 9, notes=notes)
        self.assertEqual(1, len([s for s in service_record.health_signals(self.store) if s.startswith('transient:')]))
        self.drive(lambda binding: None, start=10 ** 12, notes=notes)  # it recovers
        self.assertEqual([], service_record.health_signals(self.store))
        self.assertEqual([], notes.snapshot()[1])

    def test_health_signals_never_exceed_the_map_limit(self):
        self.committed()
        with mock.patch.object(service_record, 'BACKOFF_MAP_LIMIT', 3):
            self.drive(lambda binding: 'history_conflict')
        self.assertLessEqual(len(service_record.health_signals(self.store)), 3)
        self.assertLessEqual(len(self.store._record_sweep['permanent']), 3)

    def test_nothing_is_reported_before_the_fifth_transient_failure(self):
        self.committed()
        key = history_binding(1).claim_id
        for index in range(service_record.HEALTH_TRANSIENT_FAILURES - 1):
            self.drive(lambda binding: 'history_result_unavailable' if binding.claim_id == key else None,
                       start=(index + 1) * 10 ** 9)
            self.assertEqual([], service_record.health_signals(self.store), index)

    def test_the_backoff_doubles_from_one_minute_and_stops_at_half_an_hour(self):
        self.committed()
        key = (history_binding(1).run_handle, history_binding(1).attempt)
        delays, now = [], 0
        for _ in range(8):
            self.drive(lambda binding: 'history_result_unavailable' if (binding.run_handle, binding.attempt) == key else None,
                       start=now)
            until, count = self.store._record_sweep['backoff'][key]
            delays.append(until - now)
            self.store._purge_cursors['record'] = None
            now = until
        self.assertEqual([60_000, 120_000, 240_000, 480_000, 960_000, 1_800_000, 1_800_000, 1_800_000], delays)

    def test_a_repeated_identical_integrity_refusal_stays_one_signal(self):
        self.committed()
        for _ in range(3):
            self.store._record_sweep['permanent'].clear()
            self.drive(lambda binding: 'history_conflict' if binding.claim_id == history_binding(2).claim_id else None)
            self.store._purge_cursors['record'] = None
        self.assertEqual(1, len(service_record.health_signals(self.store)))


@requirement
class SweepLimitTests(SweepFixture):
    """B2-12: the attempt cap and the selection window are enforced, and every eligible run is eventually recorded."""

    def test_the_attempt_cap_holds_whatever_the_caller_asks_for(self):
        self.committed()
        seen, (counts,) = self.drive(lambda binding: None, limit=service_record.SWEEP_ATTEMPTS + 1)
        self.assertEqual(service_record.SWEEP_ATTEMPTS, counts['attempted'])

    def test_a_limit_that_is_not_a_positive_number_is_refused(self):
        self.committed()
        for bad in (0, -1, True, 'x', None, 1.5):
            with self.subTest(limit=bad), self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                service_record.record_pass(self.store, self.authority(), limit=bad)

    def test_the_window_follows_the_limit(self):
        self.committed()
        window = []
        original = self.store._connection

        class Spy:
            def __getattr__(inner, name):
                return getattr(original, name)

            def execute(inner, sql, parameters=()):
                if 'project_result_contexts_live' in sql and 'LIMIT' in sql:
                    window.append(parameters[-1])
                return original.execute(sql, parameters)
        self.store._connection = Spy()
        try:
            service_record.record_pass(self.store, self.authority(), limit=2, clock=lambda: 1)
        except HistoryError:
            pass
        finally:
            self.store._connection = original
        self.assertEqual([2 * 16], window[:1])

    def test_a_reduced_window_and_attempt_cap_still_records_every_eligible_run_with_real_state_changes(self):
        self.committed()
        refused = {history_binding(number).claim_id for number in (1, 5, 6, 12)}

        def behave(binding):
            if binding.claim_id in refused:
                return 'history_unrecordable_orchestration_closed'
            self.mark_recorded(binding)
            return None
        with mock.patch.object(service_record, 'SWEEP_WINDOW', 5):
            for index in range(20):
                self.drive(behave, limit=3, start=index)
        states = {number: self.context_state(number) for number in range(1, self.RUNS + 1)}
        self.assertEqual({'recorded'}, {state for number, state in states.items()
                                        if history_binding(number).claim_id not in refused}, states)
        self.assertEqual({'committed'}, {state for number, state in states.items()
                                         if history_binding(number).claim_id in refused})

    def test_a_permanent_set_larger_than_the_window_never_starves_the_rest(self):
        self.committed()
        refused = {history_binding(number).claim_id for number in range(1, 9)}

        def behave(binding):
            if binding.claim_id in refused:
                return 'history_unrecordable_other_runtime'
            self.mark_recorded(binding)
            return None
        with mock.patch.object(service_record, 'SWEEP_WINDOW', 4):
            for index in range(20):
                self.drive(behave, limit=2, start=index)
        self.assertEqual(self.RUNS - len(refused), sum(1 for number in range(1, self.RUNS + 1)
                                                       if self.context_state(number) == 'recorded'))

    def test_the_selection_does_not_grow_with_expired_rows(self):
        self.committed()
        db = self.store._connection

        def work():
            steps = [0]

            def tick():
                steps[0] += 1
                return 0
            db.set_progress_handler(tick, 1)
            rows = db.execute(service_record.WINDOW_SQL, (self.store.project_id, 0, 0,
                                                          service_record.SWEEP_WINDOW)).fetchall()
            db.set_progress_handler(None, 1)
            return len(rows), steps[0]
        rows, without = work()
        db.executemany("INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,lease_deadline_ms) "
                       "VALUES(?,?,1,'expired',0,0)",
                       ((self.store.project_id, f'hrn_{9000 + index:048x}') for index in range(100_000)))
        self.assertEqual((rows, True), (work()[0], work()[1] <= without * 1.05 + 50))


class LaneFixture(rc.RecordedContextFixture):
    """The B2 run opened and consumed but not yet published, and the product's writer lane over the same folder."""

    def setUp(self):
        super().setUp()
        self.open_and_consume()
        self.workers = workers.CodexWorkerTests(methodName='runTest')
        self.workers.setUp()
        self.addCleanup(self.workers.doCleanups)
        self.body = canonical_json({**result(), 'summary': 'PRIVATE-STORED-CONTEXT-CANARY'})
        self.store.close()

    def stop(self, lane):
        lane.close()
        lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive())

    def lane(self):
        lane = ResultPersistenceLane(self.workers.owner, self.workers.pool, lambda: rc.seven_day_store(self.fixture),
                                     max_entries=4, retained_bytes=4 * MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=True)
        self.addCleanup(self.stop, lane)
        lane._project, lane._runtime = self.binding.project_id, self.binding.runtime_digest  # this run's service
        return lane

    def wait(self, predicate):
        end = time.monotonic() + 10
        while not predicate():
            self.assertLess(time.monotonic(), end, 'the writer did not answer')
            time.sleep(0.005)

    def publish_through(self, lane):
        """The lane's own writer loop publishes the result, as it does for a finished child."""
        handle = ResultPersistenceHandle()
        packet = SimpleNamespace(body=self.body, fenced=False, reviewed=True, terminal_state=None,
                                 validation_reason=None, report=None)
        pending = _Pending(self.binding, None, None, None, None, state='committing',
                           assignment=canonical_json(self.assigned), selected=SimpleNamespace(packet=packet),
                           observed_at_ms=self.fixture.base.stamp)
        with lane._lock:
            lane._jobs[handle] = pending
        lane._incoming.put(('publish', handle))
        self.wait(lambda: pending.state in {'committed', 'failed'})
        return pending

    def reopened(self):
        self.fixture.store = rc.seven_day_store(self.fixture)
        return self.store

    def recorder_row(self):
        return service_record.recorder_of(self.store, service_record._receipt_id(self.store, self.binding))


@requirement
class LaneRecordingTests(LaneFixture):
    """B2-1: the writer records right after it stores a result, and a restarted writer's pass records what is left."""

    def test_publication_through_the_lane_records_the_outcome_by_the_service_without_any_explicit_admission(self):
        lane = self.lane()
        self.assertEqual('committed', self.publish_through(lane).state)
        self.stop(lane)
        store = self.reopened()
        self.assertEqual('recorded', rc.context_row(store, *self.attempt)[0])
        self.assertEqual('service', self.recorder_row())
        self.assertEqual(2, json_schema(store, self.binding))  # the lane publishes with the client usage: version 2

    def test_a_refusal_or_an_error_in_the_record_never_fails_the_publication(self):
        for error in (HistoryError('history_unrecordable_orchestration_closed'), RuntimeError('boom'), ValueError('v')):
            with self.subTest(error=repr(error)):
                fresh = LaneFixture()
                fresh.setUp()
                self.addCleanup(fresh.doCleanups)
                lane = fresh.lane()
                with mock.patch.object(ProjectHistory, 'admit_service_result', side_effect=error):
                    pending = fresh.publish_through(lane)
                self.assertEqual('committed', pending.state)
                self.assertFalse(lane._fault)
                fresh.stop(lane)
                store = fresh.reopened()
                self.assertEqual('committed', rc.context_row(store, *fresh.attempt)[0])  # recorded later by the pass

    def test_a_restarted_writers_backstop_pass_records_what_the_stopped_one_left_unrecorded(self):
        store = self.reopened()
        self.publish_direct(store)
        self.assertIsNone(self.recorder_row())
        store.close()
        lane = self.lane()
        request = lane.expire_results()
        self.wait(lambda: request.state != 'queued')
        self.assertEqual(('done', None), (request.state, request.reason))
        self.stop(lane)
        store = self.reopened()
        self.assertEqual('recorded', rc.context_row(store, *self.attempt)[0])
        self.assertEqual('service', self.recorder_row())
        self.assertEqual(['recording:'], [s for s in ['recording:'] if lane.recording_notes.snapshot()[0] is not None] or
                         ['recording:'])

    def publish_direct(self, store):
        from codex_agents.result_contracts import ResultObservation
        store.publish_result(self.binding, self.assigned, self.body,
                             ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None))

    def test_the_cleanup_runs_even_when_the_record_pass_fails_unexpectedly(self):
        store = self.reopened()
        self.publish_direct(store)
        due = store.result_reservation(self.binding).retained_until_ms
        store.close()
        self.fixture.base.stamp = due  # the result is past its retention: the pass retires it
        lane = self.lane()
        with mock.patch.object(service_record, 'record_pass', side_effect=RuntimeError('boom')):
            request = lane.expire_results()
            self.wait(lambda: request.state != 'queued')
        self.assertEqual(('done', None), (request.state, request.reason))
        self.assertFalse(lane._fault)
        self.stop(lane)
        store = self.reopened()
        self.assertEqual('expired', store.result_reservation(self.binding).state)


def json_schema(store, binding):
    import json
    return json.loads(store.record(service_record._receipt_id(store, binding)).content)['schema_version']
