"""Slice B2 fix round 2: the production LOWs N1-N5 and the remaining test-assurance findings of the round-1 re-check
(rev040b2r2ca / r2cb1 / r2cb2).

Real runs only where the finding asks for them: `tests.b2_extra_runs.ExtraRun` builds further committed, context-backed
runs in the one store of the B2 fixture through the same real operations as the QA tests' verifier pair, so two distinct
valid runs, many runs past a reduced selection window, a real verifier pair over a service-recorded worker, and the
real orchestration close can all be driven without patching the admission. Time budget: scenario.
"""
import inspect
import json
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_operational_qa as qa
from tests import test_record_context as rc
from tests.b2_extra_runs import ExtraRun
from tests.test_b2_service_outcome import B2Fixture, OTHER_RUNTIME, requirement
from tests.test_b2_service_outcome_hosts import HostFixture
from tests.test_b2_service_outcome_sweep import LaneFixture, SweepFixture
from tests.test_project_history import binding as history_binding

from codex_agents import client_runtime, project_history, refusal_steps, service_record
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.hosts import CLAUDE_HOST_CLIENT as CLAUDE, CODEX_HOST_CLIENT as CODEX, OWNER_CLIENT
from codex_agents.host_result_contracts import HostResultReference
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.project_history import ProjectHistory
from codex_agents.service_record import RecordingNotes


def durable(store):
    """Every durable row of the store and its stored body and context files: what a failed write must not change."""
    db = store._connection
    names = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'runtime_takeover' ORDER BY name")]
    tables = {name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr) for name in names}
    files = sorted(path.name for path in store._directory.iterdir() if path.name.startswith(('.result-', '.context-')))
    try:
        counts = store._result_counts()
    except HistoryError as error:  # an altered delivery is refused by the counts too: the reason is part of the state
        counts = error.reason_code
    return {'tables': tables, 'files': files, 'counts': counts}


def publish_direct(test, store):
    """The result stored by the writer's own call, with nothing recorded yet."""
    from codex_agents.result_contracts import ResultObservation
    store.publish_result(test.binding, test.assigned, test.body,
                         ResultObservation(test.binding.claim_id, test.fixture.base.stamp, False, True, None))


def attribution(store):
    """What the recording writes and what a closed orchestration must leave alone: records, recorders and each context's
    outcome (a real close also releases the run's custody at the next pass, which is not a recording)."""
    db = store._connection
    return {'records': sorted(tuple(row) for row in db.execute('SELECT record_id,content_digest FROM records')),
            'recorders': sorted(tuple(row) for row in db.execute('SELECT * FROM project_result_recorders')),
            'outcomes': sorted(tuple(row) for row in db.execute(
                'SELECT run_handle,attempt,outcome_digest FROM project_result_contexts'))}


def receipt_type(store, record_id):
    return json.loads(store.record(record_id).content)['record_type']


def statements(store, call):
    """The SQL the store's connection ran during `call`, with a marker where the final predicate ran."""
    log, original, check = [], store._connection, service_record.ServiceRecordAuthority.check

    class Connection:
        def __getattr__(inner, name):
            return getattr(original, name)

        def execute(inner, sql, parameters=()):
            log.append(' '.join(sql.split())[:40])
            return original.execute(sql, parameters)

    def marked(authority, history, binding, phase, **options):
        log.append(f'CHECK {phase}')
        return check(authority, history, binding, phase, **options)
    store._connection = Connection()
    try:
        with mock.patch.object(service_record.ServiceRecordAuthority, 'check', autospec=True, side_effect=marked):
            call()
    finally:
        store._connection = original
    return log


def close_orchestration(store, binding, status='completed'):
    """The orchestration's real close request. The account's release of the run is the one thing not under test."""
    store._orchestration_limits = OrchestrationLimits(1, 8)
    scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
    owner = OrchestrationOwner(store)
    current = owner._current(scope, binding.orchestration_handle)['snapshot']
    request = OrchestrationRequest(1, 'orchestration.close', 'b2-r2-close-' + status, scope, {
        'orchestration_handle': binding.orchestration_handle, 'expected_sequence': current['sequence'],
        'status': status, 'goal_tokens_used': None, 'goal_time_used_seconds': None})
    with mock.patch.object(OrchestrationOwner, '_account_released', return_value=True):
        return owner.apply(request, client_version=CODEX, before_commit=lambda: None)


def failing_third_append(error, calls):
    """The third append of the admission raises `error`, after two real appends."""
    original = ProjectHistory._append_locked

    def append(store, *args, **kwargs):
        calls.append(args[0].get('record_type') if isinstance(args[0], dict) else None)
        if len(calls) == 3:
            raise error
        return original(store, *args, **kwargs)
    return mock.patch.object(ProjectHistory, '_append_locked', autospec=True, side_effect=append)


class Fresh:
    """A further, independent B2 run in its own store (own temporary folder), cleaned up at once."""

    @staticmethod
    def make(test, cls=B2Fixture):
        fresh = cls(methodName='runTest')
        fresh.setUp()
        test.addCleanup(fresh.doCleanups)
        return fresh


# ---------------------------------------------------------------------------------------------------------------------
# Production LOWs N1, N5 and the wording N3, N4 (sweep)
# ---------------------------------------------------------------------------------------------------------------------

@requirement
class SweepRoundTwoTests(SweepFixture):
    """N1: the whole candidate operation is isolated; N5: a permanent refusal clears the transient retry state."""

    def key(self, number):
        return (history_binding(number).run_handle, history_binding(number).attempt)

    def fingerprint_failing_for(self, key, error):
        original = service_record._fingerprint
        calls = []

        def fingerprint(history, run_handle, attempt):
            calls.append((run_handle, attempt))
            if (run_handle, attempt) == key:
                raise error
            return original(history, run_handle, attempt)
        return mock.patch.object(service_record, '_fingerprint', side_effect=fingerprint), calls

    def test_N1_a_fingerprint_failure_on_a_skipped_permanent_candidate_is_an_isolated_transient_error(self):
        self.committed()
        bad = history_binding(2)
        notes = RecordingNotes()
        self.drive(lambda binding: 'history_unrecordable_other_runtime' if binding.claim_id == bad.claim_id else None,
                   notes=notes)
        self.assertIn(self.key(2), self.store._record_sweep['permanent'])  # refused for good, fingerprint stored
        patched, calls = self.fingerprint_failing_for(self.key(2), sqlite3.OperationalError('disk I/O error'))
        with patched:
            seen, (counts,) = self.drive(lambda binding: None, notes=notes, rewind=True, start=10 ** 9)
        self.assertIn(self.key(2), calls)  # the injected failure was reached
        self.assertNotIn(bad.claim_id, seen)  # the candidate never reached the admission
        self.assertEqual({'unexpected_error': 1}, counts['refused'])
        self.assertEqual(service_record.SWEEP_ATTEMPTS, counts['attempted'])  # the others went on
        self.assertEqual(service_record.SWEEP_ATTEMPTS - 1, len(seen))
        self.assertIn(self.key(2), self.store._record_sweep['backoff'])  # transient: retried after a wait
        self.assertEqual(self.positions()[service_record.SWEEP_ATTEMPTS - 1], self.store._purge_cursors['record'])
        project, signals = notes.snapshot()
        self.assertEqual((self.store.project_id, [f'error:{bad.run_handle}:{bad.attempt}']), (project, signals))

    def test_N1_a_fingerprint_failure_while_installing_a_permanent_refusal_is_isolated_too(self):
        self.committed()
        bad = history_binding(2)
        notes = RecordingNotes()
        patched, calls = self.fingerprint_failing_for(self.key(2), OSError('read failed'))
        with patched:
            seen, (counts,) = self.drive(
                lambda binding: 'history_unrecordable_other_runtime' if binding.claim_id == bad.claim_id else None,
                notes=notes)
        self.assertIn(self.key(2), calls)
        self.assertIn(bad.claim_id, seen)  # the refusal itself was reached
        self.assertEqual({'unexpected_error': 1}, counts['refused'])
        self.assertEqual(service_record.SWEEP_ATTEMPTS, counts['attempted'])
        self.assertNotIn(self.key(2), self.store._record_sweep['permanent'])  # nothing half installed
        self.assertIn(self.key(2), self.store._record_sweep['backoff'])
        self.assertEqual(self.positions()[service_record.SWEEP_ATTEMPTS - 1], self.store._purge_cursors['record'])
        self.assertEqual([f'error:{bad.run_handle}:{bad.attempt}'], notes.snapshot()[1])

    def test_N1_the_notes_are_published_after_every_candidate_failed(self):
        self.committed()
        notes = RecordingNotes()
        seen, (counts,) = self.drive(lambda binding: OSError('disk'), notes=notes)
        self.assertEqual({'unexpected_error': service_record.SWEEP_ATTEMPTS}, counts['refused'])
        project, signals = notes.snapshot()
        self.assertEqual(self.store.project_id, project)
        self.assertEqual(service_record.SWEEP_ATTEMPTS, len(signals))
        self.assertTrue(all(signal.startswith('error:') for signal in signals))

    def test_N5_a_transient_candidate_that_becomes_permanent_loses_its_backoff_and_transient_signal(self):
        self.committed()
        bad = history_binding(1)
        notes = RecordingNotes()
        for index in range(service_record.HEALTH_TRANSIENT_FAILURES):
            self.drive(lambda binding: 'history_result_unavailable' if binding.claim_id == bad.claim_id else None,
                       start=(index + 1) * 10 ** 9, notes=notes, rewind=True)
        self.assertEqual([f'transient:{bad.run_handle}:{bad.attempt}'], service_record.health_signals(self.store))
        self.assertIn(self.key(1), self.store._record_sweep['backoff'])
        self.drive(lambda binding: 'history_unrecordable_orchestration_closed' if binding.claim_id == bad.claim_id
                   else None, start=10 ** 12, notes=notes, rewind=True)
        self.assertIn(self.key(1), self.store._record_sweep['permanent'])
        self.assertNotIn(self.key(1), self.store._record_sweep['backoff'])
        self.assertEqual([], service_record.health_signals(self.store))
        self.assertEqual([], notes.snapshot()[1])

    def test_N5_an_unexpected_error_that_becomes_permanent_loses_its_error_signal_and_backoff(self):
        self.committed()
        bad = history_binding(1)
        notes = RecordingNotes()
        self.drive(lambda binding: ValueError('malformed') if binding.claim_id == bad.claim_id else None, notes=notes)
        self.assertEqual([f'error:{bad.run_handle}:{bad.attempt}'], notes.snapshot()[1])
        self.drive(lambda binding: 'history_unrecordable_other_runtime' if binding.claim_id == bad.claim_id else None,
                   start=10 ** 12, notes=notes, rewind=True)
        self.assertNotIn(self.key(1), self.store._record_sweep['backoff'])
        self.assertEqual([], service_record.health_signals(self.store))
        self.assertEqual([], notes.snapshot()[1])

    def test_N5_the_integrity_signal_of_a_permanent_refusal_is_kept(self):
        self.committed()
        bad = history_binding(1)
        notes = RecordingNotes()
        self.drive(lambda binding: ValueError('malformed') if binding.claim_id == bad.claim_id else None, notes=notes)
        self.drive(lambda binding: 'history_conflict' if binding.claim_id == bad.claim_id else None,
                   start=10 ** 12, notes=notes, rewind=True)
        self.assertNotIn(self.key(1), self.store._record_sweep['backoff'])
        self.assertEqual([f'integrity:{bad.run_handle}:{bad.attempt}'], service_record.health_signals(self.store))
        self.assertEqual([f'integrity:{bad.run_handle}:{bad.attempt}'], notes.snapshot()[1])

    def test_N3_the_documentation_says_what_a_selected_permanent_candidate_still_costs(self):
        text = ' '.join(inspect.getdoc(service_record.record_pass).split())
        self.assertNotIn('without another read', text)
        self.assertIn('fingerprint query', text)

    def test_N4_the_expiry_guidance_does_not_claim_the_result_was_removed(self):
        text = refusal_steps.EXACT['history_result_expired']
        self.assertNotIn('removed', text)
        self.assertIn('recording window', text)


# ---------------------------------------------------------------------------------------------------------------------
# N2: the publish-time record (the lane) reports an unexpected error; a failed admission leaves nothing
# ---------------------------------------------------------------------------------------------------------------------

@requirement
class PublishTimeReportTests(LaneFixture):
    """N2: the publication stands, and an unexpected error of the record that follows it is reported, not silent."""

    def test_an_unexpected_error_after_the_publication_is_reported_and_the_publication_stands(self):
        for error in (RuntimeError('boom'), ValueError('v'), sqlite3.OperationalError('database is locked')):
            with self.subTest(error=repr(error)):
                fresh = Fresh.make(self, LaneFixture)
                lane = fresh.lane()
                with mock.patch.object(ProjectHistory, 'admit_service_result', side_effect=error) as admit:
                    pending = fresh.publish_through(lane)
                self.assertEqual(1, admit.call_count)  # the injected failure was reached
                self.assertEqual('committed', pending.state)
                self.assertFalse(lane._fault)
                project, signals = lane.recording_notes.snapshot()
                run = f'{fresh.binding.run_handle}:{fresh.binding.attempt}'
                self.assertEqual((fresh.binding.project_id, [f'error:{run}']), (project, signals))

    def test_a_refusal_is_a_normal_outcome_and_is_not_reported(self):
        lane = self.lane()
        with mock.patch.object(ProjectHistory, 'admit_service_result',
                               side_effect=HistoryError('history_unrecordable_orchestration_closed')) as admit:
            pending = self.publish_through(lane)
        self.assertEqual(1, admit.call_count)
        self.assertEqual('committed', pending.state)
        self.assertEqual([], lane.recording_notes.snapshot()[1])

    def test_the_reported_error_clears_when_the_backstop_records_the_run(self):
        lane = self.lane()
        with mock.patch.object(ProjectHistory, 'admit_service_result', side_effect=RuntimeError('boom')):
            self.publish_through(lane)
        self.assertEqual(1, len(lane.recording_notes.snapshot()[1]))
        request = lane.expire_results()
        self.wait(lambda: request.state != 'queued')
        self.assertEqual(('done', None), (request.state, request.reason))
        self.assertEqual((self.binding.project_id, []), lane.recording_notes.snapshot())
        self.stop(lane)
        store = self.reopened()
        self.assertEqual('recorded', rc.context_row(store, *self.attempt)[0])
        self.assertEqual('service', self.recorder_row())


@requirement
class FailedAdmissionLeavesNothingTests(B2Fixture):
    """N2 (rollback): an error that is not a history error, raised inside the admission transaction, leaves no open
    transaction and no partial write, in this connection and in what a new writer reads."""

    def test_a_non_history_error_inside_the_transaction_leaves_no_open_transaction_and_no_partial_write(self):
        for error in (ValueError('malformed'), RuntimeError('boom'), sqlite3.OperationalError('disk I/O error')):
            with self.subTest(error=repr(error)):
                fresh = Fresh.make(self)
                before = durable(fresh.store)
                calls = []
                with failing_third_append(error, calls):
                    if isinstance(error, sqlite3.Error):  # a storage error is named by the store
                        with self.assertRaisesRegex(HistoryError, '^history_unavailable$'):
                            fresh.service_record()
                    else:  # anything else passes through unchanged, after the rollback
                        with self.assertRaises(type(error)) as caught:
                            fresh.service_record()
                        self.assertIs(error, caught.exception)
                self.assertEqual(3, len(calls))  # the failure came after two real appends
                self.assertFalse(fresh.store._connection.in_transaction)
                self.assertEqual(before, durable(fresh.store))
                store = fresh.reopen()
                self.assertEqual(before, durable(store))  # what a new writer reads
                self.assertEqual('recorded', 'recorded' if fresh.service_record() else None)
                self.assertEqual('service', fresh.recorder())

    def test_a_storage_error_is_a_named_unavailability_and_the_reopened_store_matches_the_snapshot(self):
        before = durable(self.store)
        calls = []
        with failing_third_append(OSError('cut'), calls):
            with self.assertRaisesRegex(HistoryError, '^history_unavailable$'):
                self.service_record()
        self.assertEqual(3, len(calls))
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(before, durable(self.store))
        store = self.reopen()
        self.assertEqual(before, durable(store))
        self.assertEqual(1, service_record.record_pass(store, self.authority())['recorded'])
        self.assertEqual('service', self.recorder())


# ---------------------------------------------------------------------------------------------------------------------
# M1: a really closed orchestration, through the sweep and the lane's backstop
# ---------------------------------------------------------------------------------------------------------------------

@requirement
class ClosedOrchestrationBackstopTests(LaneFixture):
    """B2-13 through the real close request: the restart/backstop pass refuses, writes nothing, never loops."""

    def closed_with_a_committed_result(self):
        store = self.reopened()
        publish_direct(self, store)
        close_orchestration(store, self.binding)
        before = attribution(store)
        store.close()
        return before

    def test_the_lane_backstop_refuses_a_really_closed_orchestration_and_attribution_stays_unchanged(self):
        before = self.closed_with_a_committed_result()
        lane = self.lane()
        with mock.patch.object(ProjectHistory, 'admit_service_result', autospec=True,
                               side_effect=ProjectHistory.admit_service_result) as admit:
            for _ in range(2):
                request = lane.expire_results()
                self.wait(lambda: request.state != 'queued')
                self.assertEqual(('done', None), (request.state, request.reason))
        self.assertEqual(1, admit.call_count)  # the second pass does not try it again
        self.assertFalse(lane._fault)
        self.assertEqual((self.binding.project_id, []), lane.recording_notes.snapshot())  # normal outcome, no finding
        self.stop(lane)
        store = self.reopened()
        self.assertEqual(before, attribution(store))
        self.assertEqual(0, len(before['recorders']))
        self.assertIsNone(self.recorder_row())

    def test_a_service_recorded_run_keeps_its_attribution_through_a_real_close_and_the_backstop(self):
        store = self.reopened()
        publish_direct(self, store)
        service_record.record_pass(store, service_record.ServiceRecordAuthority(self.binding.runtime_digest, True))
        self.assertEqual('service', self.recorder_row())
        close_orchestration(store, self.binding)
        before = attribution(store)
        store.close()
        lane = self.lane()
        request = lane.expire_results()
        self.wait(lambda: request.state != 'queued')
        self.assertEqual(('done', None), (request.state, request.reason))
        self.stop(lane)
        store = self.reopened()
        self.assertEqual(before, attribution(store))
        self.assertEqual(1, len(before['recorders']))
        self.assertEqual('service', self.recorder_row())

    def test_the_status_names_the_closed_orchestration_and_the_report_says_nothing_awaits(self):
        self.closed_with_a_committed_result()
        store = self.reopened()
        status = service_record.recording_status(
            store, service_record.ServiceRecordAuthority(self.binding.runtime_digest, True))
        self.assertEqual(({'orchestration_closed': 1}, 0, []),
                         (status['unrecordable'], status['awaiting_record'], status['recorded']))


# ---------------------------------------------------------------------------------------------------------------------
# M4: two distinct valid runs in one store
# ---------------------------------------------------------------------------------------------------------------------

class TwoRunFixture(B2Fixture):
    def setUp(self):
        super().setUp()
        self.other = ExtraRun(self, 2)
        self.addCleanup(lambda: None)

    def swapped(self, binding, reference):
        return HostResultReference(binding, reference.terminal_record_id, reference.terminal_content_digest,
                                   reference.result_digest)

    def context_of(self, binding):
        return self.store.stored_result_context(binding, host_class=CODEX)

    def admit_primary(self, reference, binding):
        original = self.context_of(binding)
        return self.store.admit_host_result(reference, assignment=original.assignment, task=original.task,
                                            route=original.route, client_capability=original.client_capability,
                                            before_commit=lambda: None)

    def admit_internal(self, reference, binding):
        original = self.context_of(binding)
        return self.store._admit_host_result(reference, assignment=original.assignment, task=original.task,
                                             route=original.route, client_capability=original.client_capability,
                                             before_commit=None, final_check=self.authority(), recorder='service')

    def both_refuse(self, reference, binding, reason):
        for name, call in (('primary', self.admit_primary), ('internal service', self.admit_internal)):
            before = durable(self.store)
            with self.assertRaisesRegex(HistoryError, f'^{reason}$', msg=name):
                call(reference, binding)
            self.assertFalse(self.store._connection.in_transaction)
            self.assertEqual(before, durable(self.store), name)


@requirement
class TwoRunStoreTests(TwoRunFixture):
    """B2-7: both runs exist in the admitting store, so every swap is between two genuine rows."""

    def test_the_fixture_holds_two_distinct_valid_unrecorded_runs(self):
        self.assertNotEqual(self.binding.run_handle, self.other.binding.run_handle)
        self.assertNotEqual(self.reference.result_digest, self.other.reference.result_digest)
        self.assertEqual(2, self.store._result_counts()[0])
        states = [row[0] for row in self.store._connection.execute('SELECT state FROM project_result_contexts')]
        self.assertEqual(['committed', 'committed'], states)

    def test_the_other_runs_reference_with_this_runs_binding_is_refused_by_both_entries(self):
        self.both_refuse(self.swapped(self.binding, self.other.reference), self.binding, 'history_conflict')

    def test_this_runs_reference_with_the_other_runs_binding_is_refused_by_both_entries(self):
        self.both_refuse(self.swapped(self.other.binding, self.reference), self.other.binding, 'history_conflict')

    def test_swapped_result_bodies_are_refused_for_both_runs_and_both_entries(self):
        mine, theirs = [sorted(self.directory.glob('.result-*.json')) for _ in range(2)][0][:2]
        first, second = mine.read_bytes(), theirs.read_bytes()
        self.assertNotEqual(first, second)
        mine.write_bytes(second)
        theirs.write_bytes(first)
        for run, binding in ((self, self.binding), (self.other, self.other.binding)):
            for entry in ('service', 'primary'):
                before = durable(self.store)
                with self.assertRaisesRegex(HistoryError, '^history_result_unavailable$'):
                    if entry == 'service':
                        self.store.admit_service_result(binding, final_check=self.authority())
                    else:
                        self.admit_primary(run.reference, binding)
                self.assertEqual(before, durable(self.store))
        self.assertEqual(0, self.counts()[1])

    def test_an_outbox_row_pointing_at_the_other_runs_delivery_is_refused_and_the_other_run_still_records(self):
        self.store._connection.execute(
            'UPDATE terminal_outbox SET record_content_digest=? WHERE record_id=?',
            (self.other.reference.terminal_content_digest, self.reference.terminal_record_id))
        before = durable(self.store)
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.service_record()
        self.assertEqual(before, durable(self.store))
        recorded = self.store.admit_service_result(self.other.binding, final_check=self.authority())
        self.assertEqual('recorded', 'recorded' if recorded else None)
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_result_recorders').fetchone()[0])

    def test_a_context_of_one_run_cannot_be_admitted_under_the_other_runs_task(self):
        mine, theirs = self.context_of(self.binding), self.context_of(self.other.binding)
        self.assertNotEqual(mine.task, theirs.task)
        before = durable(self.store)
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.store.admit_host_result(self.reference, assignment=mine.assignment, task=theirs.task,
                                         route=mine.route, client_capability=mine.client_capability,
                                         before_commit=lambda: None)
        self.assertEqual(before, durable(self.store))

    def test_the_count_surface_follows_only_what_is_recorded_and_both_runs_record_independently(self):
        self.assertEqual(2, self.store._result_counts()[0])
        first = self.store.admit_service_result(self.binding, final_check=self.authority())
        counted = self.store._result_counts()
        second = self.store.admit_service_result(self.other.binding, final_check=self.authority())
        self.assertEqual(counted, self.store._result_counts())  # recording never changes what is held
        self.assertNotEqual(first['admission']['host_outcome_digest'], second['admission']['host_outcome_digest'])
        self.assertNotEqual(first['admission']['record_id'], second['admission']['record_id'])
        rows = self.store._connection.execute('SELECT record_id,recorded_by FROM project_result_recorders').fetchall()
        self.assertEqual({(first['admission']['record_id'], 'service'), (second['admission']['record_id'], 'service')},
                         {tuple(row) for row in rows})
        self.assertEqual(['recorded', 'recorded'],
                         [row[0] for row in self.store._connection.execute('SELECT state FROM project_result_contexts')])


# ---------------------------------------------------------------------------------------------------------------------
# M5, M8: window and attempt limits with real admissions; altered rows named by the status and the sweep
# ---------------------------------------------------------------------------------------------------------------------

class WindowSpy:
    """The selection query's LIMIT, as the store's connection saw it."""

    def __init__(self, store):
        self.limits, self.original, self.store = [], store._connection, store

    def __enter__(self):
        spy = self

        class Connection:
            def __getattr__(inner, name):
                return getattr(spy.original, name)

            def execute(inner, sql, parameters=()):
                if 'project_result_contexts_live' in sql and 'LIMIT' in sql:
                    spy.limits.append(parameters[-1])
                return spy.original.execute(sql, parameters)
        self.store._connection = Connection()
        return self

    def __exit__(self, *exc):
        self.store._connection = self.original


class ManyRunFixture(B2Fixture):
    EXTRA = 5  # six real runs in all (the store keeps at most eight results)

    def setUp(self):
        super().setUp()
        self.runs = [SimpleNamespace(binding=self.binding)] + [ExtraRun(self, number + 2) for number in range(self.EXTRA)]

    def refusing(self, closed):
        """The real admission, except that the runs named here are refused as an ended orchestration would."""
        original = ProjectHistory.admit_service_result
        calls = []

        def admit(store, binding, *, final_check):
            calls.append(binding.claim_id)
            if binding.claim_id in closed:
                raise HistoryError('history_unrecordable_orchestration_closed')
            return original(store, binding, final_check=final_check)
        return mock.patch.object(ProjectHistory, 'admit_service_result', autospec=True, side_effect=admit), calls

    def receipt_rows(self):
        return {tuple(row) for row in self.store._connection.execute(
            'SELECT record_id,recorded_by FROM project_result_recorders')}


@requirement
class RealSweepPersistenceTests(ManyRunFixture):
    """B2-12: past a reduced window and attempt limit every eligible run is really recorded, by the service."""

    def sweep(self, passes, *, closed, **options):
        patched, calls = self.refusing(closed)
        done = []
        with patched, WindowSpy(self.store) as spy:
            for index in range(passes):
                done.append(service_record.record_pass(self.store, self.authority(), clock=lambda at=index: at,
                                                       **options))
        return done, calls, spy.limits

    def assert_every_eligible_run_recorded(self, closed):
        for run in self.runs:
            row = rc.context_row(self.store, run.binding.run_handle, run.binding.attempt)
            self.assertEqual('committed' if run.binding.claim_id in closed else 'recorded', row[0], run.binding.claim_id)
        recorded = {run.binding.claim_id for run in self.runs} - closed
        receipts = self.receipt_rows()
        self.assertEqual(len(recorded), len(receipts))
        self.assertEqual({'service'}, {who for _, who in receipts})
        for record_id, _ in receipts:
            self.assertEqual('host_result_admission', receipt_type(self.store, record_id))

    def test_a_window_of_two_and_one_attempt_per_pass_records_every_eligible_run(self):
        closed = {self.runs[0].binding.claim_id, self.runs[3].binding.claim_id}
        with mock.patch.object(service_record, 'SWEEP_WINDOW', 2):
            done, calls, limits = self.sweep(24, closed=closed, limit=1)
        self.assertEqual({2}, set(limits))  # the effective query limit: min(window, limit*16)
        self.assertLessEqual(max(count['attempted'] for count in done), 1)
        self.assertEqual(len(self.runs) - len(closed), sum(count['recorded'] for count in done))
        self.assertEqual(1, calls.count(self.runs[0].binding.claim_id))  # refused once, never looped
        self.assertEqual(1, calls.count(self.runs[3].binding.claim_id))
        self.assert_every_eligible_run_recorded(closed)

    def test_a_request_above_the_attempt_cap_is_held_to_the_cap_and_the_window_follows_it(self):
        closed = {self.runs[1].binding.claim_id}
        with mock.patch.object(service_record, 'SWEEP_ATTEMPTS', 2), mock.patch.object(service_record, 'SWEEP_WINDOW', 5):
            done, calls, limits = self.sweep(12, closed=closed, limit=9)
        self.assertEqual({5}, set(limits))
        self.assertEqual(2, max(count['attempted'] for count in done))
        self.assertGreaterEqual(len([count for count in done if count['attempted']]), 3)
        self.assert_every_eligible_run_recorded(closed)


@requirement
class AlteredRowStatusTests(B2Fixture):
    """B2-3, B2-7: each altered row is refused with its exact reason by both entries and by the sweep, writes nothing,
    and the status says what it can see: it names the predicate's refusals; an integrity refusal is the sweep's and the
    health signal's."""

    def run_case(self, change, reason, *, status):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Fresh.make(self)
                change(fresh)
                before = durable(fresh.store)
                with self.assertRaisesRegex(HistoryError, f'^{reason}$'):
                    fresh.service_record() if entry == 'service' else fresh.primary_record()
                self.assertEqual(before, durable(fresh.store))
                shown = service_record.recording_status(fresh.store, fresh.authority())
                self.assertEqual(status, (shown['unrecordable'], shown['awaiting_record'], shown['recorded']))
                notes = RecordingNotes()
                swept = service_record.record_pass(fresh.store, fresh.authority(), notes=notes)
                self.assertEqual({reason: 1}, swept['refused'])
                self.assertEqual(before, durable(fresh.store))
                integrity = reason in {'history_conflict', 'history_incompatible'}
                self.assertEqual(1 if integrity else 0, len(notes.snapshot()[1]))
                if integrity:
                    self.assertTrue(notes.snapshot()[1][0].startswith('integrity:'))

    def test_an_altered_body(self):
        self.run_case(lambda fresh: sorted(fresh.directory.glob('.result-*.json'))[0].write_bytes(b'{"x": 1}'),
                      'history_result_unavailable', status=({}, 1, []))

    def test_an_altered_terminal_delivery(self):
        # the status skips a run whose own delivery no longer verifies: it is the sweep and the health signal that name it
        self.run_case(lambda fresh: fresh.store._connection.execute(
            'UPDATE terminal_outbox SET record_content_digest=?', ('0' * 64,)), 'history_incompatible',
            status=({}, 0, []))

    def test_an_altered_consumed_grant(self):
        self.run_case(lambda fresh: fresh.store._connection.execute(
            'UPDATE project_execution_consumptions SET registration_digest=?', ('0' * 64,)), 'history_conflict',
            status=({}, 1, []))

    def test_an_altered_reservation_digest(self):
        self.run_case(lambda fresh: fresh.store._connection.execute(
            'UPDATE result_reservations SET prepared_digest=?', ('0' * 64,)), 'history_conflict', status=({}, 1, []))

    def test_a_context_that_is_not_committed_is_named_by_the_status_and_the_sweep_never_selects_it(self):
        fresh = Fresh.make(self)
        fresh.store._connection.execute("UPDATE project_result_contexts SET state='opened'")
        shown = service_record.recording_status(fresh.store, fresh.authority())
        self.assertEqual(({}, 0, []), (shown['unrecordable'], shown['awaiting_record'], shown['recorded']))
        self.assertEqual(0, service_record.record_pass(fresh.store, fresh.authority())['attempted'])
        with self.assertRaisesRegex(HistoryError, '^history_unrecordable_context_state$'):
            fresh.service_record()

    def test_a_foreign_runtime_and_a_host_class_are_named_by_the_status_and_refused_with_exact_reasons(self):
        fresh = Fresh.make(self)
        other = fresh.authority(runtime_digest=OTHER_RUNTIME)
        shown = service_record.recording_status(fresh.store, other)
        self.assertEqual({'other_runtime': 1}, shown['unrecordable'])
        with fresh.orchestration(client=CLAUDE):
            shown = service_record.recording_status(fresh.store, fresh.authority())
            self.assertEqual({'host_class': 1}, shown['unrecordable'])
            with self.assertRaisesRegex(HistoryError, '^history_unrecordable_host_class$'):
                fresh.service_record()


# ---------------------------------------------------------------------------------------------------------------------
# M5 (retired primary), M2/M3 (authorized requests), M-barrier (ownership transfer), M-receipt (versions, order)
# ---------------------------------------------------------------------------------------------------------------------

@requirement
class RetiredRunTests(B2Fixture):
    """B2-16: after the retention retires a result, the primary's retrieval is refused by name, the receipt and the
    attribution stay, and the status lists only what is still held."""

    def retire(self):
        self.fixture.base.stamp = self.due()
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual('expired', self.store.result_reservation(self.binding).state)

    def test_the_primary_retrieval_after_retirement_is_refused_and_the_receipt_and_recorder_stay(self):
        recorded = self.service_record()
        self.retire()
        before = durable(self.store)
        with self.assertRaisesRegex(HistoryError, '^history_result_expired$'):
            self.primary_record()
        with self.assertRaisesRegex(HistoryError, '^history_result_expired$'):
            self.service_record()
        self.assertEqual(before, durable(self.store))
        self.assertEqual(recorded['admission']['record_id'], self.receipt)
        self.assertEqual('host_result_admission', receipt_type(self.store, self.receipt))  # receipt visible
        self.assertEqual('service', self.recorder())
        status = service_record.recording_status(self.store, self.authority())
        self.assertEqual(([], 0, {}), (status['recorded'], status['awaiting_record'], status['unrecordable']))

    def test_a_primary_first_run_keeps_its_attribution_after_retirement(self):
        self.primary_record()
        self.retire()
        self.assertEqual('primary', self.recorder())
        with self.assertRaisesRegex(HistoryError, '^history_result_expired$'):
            self.primary_record()

    def test_an_unrecorded_run_retires_unrecorded_and_the_status_does_not_call_it_waiting(self):
        self.retire()
        self.assertIsNone(self.recorder())
        status = service_record.recording_status(self.store, self.authority())
        self.assertEqual(0, status['awaiting_record'])
        self.assertEqual({'attempted': 0, 'recorded': 0, 'refused': {}, 'already': 0},
                         service_record.record_pass(self.store, self.authority()))


@requirement
class AuthorizedRequestTests(HostFixture):
    """B2-6: the host's own authorization of the primary's admit request, with the authorizer's calls asserted and the
    durable state compared whole. The transport is a direct call: see the known gap in the round-2 notes."""

    def test_each_caller_that_may_not_record_is_refused_by_the_authorizer_with_the_whole_store_unchanged(self):
        cases = {'unenrolled': dict(enrolled=False), 'owner label': dict(host_client=OWNER_CLIENT),
                 'other host class': dict(host_client=CLAUDE)}
        for name, options in cases.items():
            with self.subTest(case=name):
                service, token = self.serve(**options)
                before = durable(self.store)
                with mock.patch.object(project_history.ProjectHistory, 'admit_host_result') as admitted, \
                        mock.patch.object(project_history.ProjectHistory, 'admit_service_result') as served:
                    self.assertEqual('history_not_authorized', self.refusal(lambda: self.admit(service, token)))
                admitted.assert_not_called()  # refused before the writer was asked
                served.assert_not_called()
                self.assertEqual(before, durable(self.store))
                self.assertIsNone(self.recorder())

    def test_the_enrolled_primary_is_authorized_and_records_exactly_once_through_the_writer(self):
        service, token = self.serve()
        with mock.patch.object(project_history.ProjectHistory, 'admit_host_result', autospec=True,
                               side_effect=project_history.ProjectHistory.admit_host_result) as admitted:
            reply = self.admit(service, token)
        self.assertEqual(1, admitted.call_count)
        self.assertEqual(self.receipt, reply['admission']['record_id'])
        self.assertEqual('primary', self.recorder())
        again = self.admit(service, token)
        self.assertEqual(reply['admission'], again['admission'])
        self.assertEqual('primary', self.recorder())

    def test_the_service_entry_is_not_an_operation_and_a_forged_one_is_refused_by_the_decoder(self):
        service, token = self.serve()
        before = durable(self.store)
        with self.assertRaises(HistoryError) as caught:
            self.ask(service, token, 'history.admit_service_result', self.binding.claim_id, {})
        self.assertEqual('history_invalid_request', caught.exception.reason_code)
        self.assertEqual(before, durable(self.store))


@requirement
class OwnershipTransferTests(B2Fixture):
    """B2-15: the writer's ownership really passes to a successor at the barrier, through both entries."""

    def test_a_successor_opens_the_writer_at_the_barrier_and_the_first_one_writes_nothing(self):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Fresh.make(self)
                before = durable(fresh.store)
                successor = []

                def transfer():
                    fresh.store._after_body_io = None
                    fresh.store.close()
                    successor.append(rc.seven_day_store(fresh.fixture))
                fresh.store._after_body_io = transfer
                with self.assertRaises(HistoryError) as caught:
                    fresh.service_record() if entry == 'service' else fresh.primary_record()
                self.assertEqual(1, len(successor))
                self.assertIn(caught.exception.reason_code, {'history_unavailable', 'history_wrong_owner'})
                fresh.fixture.store = successor[0]
                self.assertEqual(before, durable(successor[0]))  # nothing was written by the old writer
                self.assertEqual(1, service_record.record_pass(successor[0], fresh.authority())['recorded'])
                self.assertEqual('service', fresh.recorder())


@requirement
class ReceiptVersionAndOrderTests(B2Fixture):
    """B2-14, B2-17: both receipt versions with matching and mismatching expected values, and the order of the final
    predicate inside the admission transaction (decision 114.2 is open: the default of the primary's keyword is not
    pinned here, only that the predicate runs whichever way the keyword is given)."""

    def test_the_predicate_is_the_first_statement_of_the_transaction_for_both_entries(self):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Fresh.make(self)
                log = statements(fresh.store, fresh.primary_record if entry == 'primary' else fresh.service_record)
                start = max(index for index, line in enumerate(log) if line.startswith('BEGIN'))
                before_check = log[start + 1:log.index('CHECK K1', start)]
                # nothing is written before the predicate, and the only statement ahead of it is the writer's own read
                self.assertLessEqual(len(before_check), 1, before_check)
                self.assertTrue(all(line.startswith('SELECT') for line in before_check), before_check)
                self.assertGreater(log.index('COMMIT'), max(index for index, line in enumerate(log)
                                                            if line.startswith('BEGIN')))

    def test_the_primary_that_omits_the_keyword_still_runs_the_predicate_and_the_service_must_give_it(self):
        phases = []
        original = service_record.ServiceRecordAuthority.check

        def watch(authority, history, binding, phase, **options):
            phases.append((phase, authority.service))
            return original(authority, history, binding, phase, **options)
        context = self.store.stored_result_context(self.binding, host_class=self.host_class)
        with mock.patch.object(service_record.ServiceRecordAuthority, 'check', autospec=True, side_effect=watch):
            self.store.admit_host_result(self.reference, assignment=context.assignment, task=context.task,
                                         route=context.route, client_capability=context.client_capability,
                                         before_commit=lambda: None)
        self.assertIn(('K1', False), phases)
        with self.assertRaises(TypeError):
            self.store.admit_service_result(self.binding)

    def test_a_known_receipt_is_retrieved_for_matching_inputs_and_conflicts_for_any_other(self):
        recorded = self.service_record()
        context = self.store.stored_result_context(self.binding, host_class=self.host_class)
        same = dict(assignment=context.assignment, task=context.task, route=context.route,
                    client_capability=context.client_capability, before_commit=lambda: None)
        self.assertEqual(recorded, self.store.admit_host_result(self.reference, **same))  # matching expected values
        for name, change in (('assignment', {'assignment': {**context.assignment, 'objective': 'something else'}}),
                             ('task', {'task': {**context.task, 'id': 'another-task'}}),
                             ('route', {'route': {**context.route, 'model': 'another-model'}})):
            with self.subTest(changed=name):
                before = durable(self.store)
                with self.assertRaises(HistoryError) as caught:
                    self.store.admit_host_result(self.reference, **{**same, **change})
                self.assertIn(caught.exception.reason_code, {'history_conflict', 'history_invalid_input'}, name)
                self.assertEqual(before, durable(self.store))

    def test_receipt_version_2_with_matching_and_mismatching_expected_values_for_both_entries(self):
        from tests.test_b2_service_outcome_paths import UsageFixture
        fresh = Fresh.make(self, UsageFixture)
        recorded = fresh.service_record()
        self.assertEqual(2, recorded['admission']['schema_version'])
        context = fresh.store.stored_result_context(fresh.binding, host_class=fresh.host_class)
        self.assertEqual(recorded, fresh.primary_record())  # matching expected values (usage digest included)
        fresh.store._connection.execute(
            "UPDATE records SET content=replace(CAST(content AS TEXT),'\"usage_record_id\":\"','\"usage_record_id\":\"x')"
            " WHERE record_id=?", (fresh.receipt,))
        fresh.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        before = durable(fresh.store)
        for entry in ('service', 'primary'):
            with self.assertRaises(HistoryError) as caught:
                fresh.service_record() if entry == 'service' else fresh.primary_record()
            self.assertIn(caught.exception.reason_code, {'history_conflict', 'history_incompatible'}, entry)
        self.assertEqual(before, durable(fresh.store))
        self.assertIsNotNone(context)


# ---------------------------------------------------------------------------------------------------------------------
# M7: verifier open, QA scope and routing evidence over a service-recorded worker, on independent stores
# ---------------------------------------------------------------------------------------------------------------------

class VerifierPairFixture(unittest.TestCase):
    """The QA tests' real verifier pair (host open with the worker dependency, claim, registration, consumption,
    publication, admission, QA scope through the history service) over a worker that is recorded by the service or by
    the primary. The pair's own methods are the QA tests' (borrowed, so none of its tests run here)."""

    recorded_by = 'service'
    verifier_role = qa.ProjectOperationalQATests.verifier_role
    make_verifier = qa.ProjectOperationalQATests.make_verifier
    verifier_result = qa.ProjectOperationalQATests.verifier_result
    authorize = qa.ProjectOperationalQATests.authorize
    make_service = qa.ProjectOperationalQATests.make_service
    command = qa.ProjectOperationalQATests.command
    call = qa.ProjectOperationalQATests.call

    def setUp(self):
        self.b2 = Fresh.make(self)
        record = self.b2.service_record if self.recorded_by == 'service' else self.b2.primary_record
        self.worker_digest = record()['admission']['host_outcome_digest']
        self.store, self.binding = self.b2.store, self.b2.binding
        verification = {**self.b2.task, 'id': 'verify-original-worker', 'family': 'verification',
                        'verification_for_family': self.b2.task['family'], 'verification_for_provider': 'codex',
                        'verification_for_outcome': self.worker_digest}
        self.worker = SimpleNamespace(base=self.b2.base, fixture=self.b2.fixture, store=self.store,
                                      binding=self.binding, digest=self.worker_digest, original_task=self.b2.task,
                                      route=self.b2.route, task=verification)
        self.ledger = self.b2.base.base.ledger
        self.fixture = self.b2.fixture
        self.bridge = self.fixture.base.fixture.fixture
        self.worker_context = qa.HostResultContext(self.binding, self.fixture.assigned, self.b2.task, self.b2.route,
                                                   self.bridge.clients['bridge-v1'])
        self.make_verifier()
        self.context = qa.OperationalQAContext(self.worker_context, self.verifier_context)
        self.factory = mock.Mock(return_value=self.store)
        self.token = object()
        self.service = self.make_service()


@requirement
class ServiceRecordedWorkerVerifiedTests(unittest.TestCase):
    """B2-11: a service-recorded worker is opened by a real verifier and scoped for QA exactly as a primary-recorded one
    is; the unreviewed marker is on the status of the service's store and absent from the primary's."""

    def pair(self, recorded_by):
        class Pair(VerifierPairFixture):
            pass
        Pair.recorded_by = recorded_by
        pair = Pair(methodName='runTest')
        pair.setUp()
        self.addCleanup(pair.doCleanups)
        return pair

    def shape(self, value):
        """A QA scope with the values that differ between two runs (handles, digests, ids) replaced by their kind."""
        if isinstance(value, dict):
            return {key: self.shape(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.shape(item) for item in value]
        if isinstance(value, bool) or value is None or (isinstance(value, int) and value < 10 ** 6):
            return value
        return value if isinstance(value, str) and len(value) < 24 else '<value>'

    def test_a_verifier_opens_the_service_recorded_worker_and_the_qa_scope_resolves_both_returns(self):
        pair = self.pair('service')
        self.assertEqual('service', pair.b2.recorder())
        self.assertEqual(1, pair.store._connection.execute('SELECT COUNT(*) FROM project_host_opens WHERE run_handle=?',
                                                           (pair.verifier_binding.run_handle,)).fetchone()[0])
        self.assertEqual(1, pair.store._connection.execute('SELECT COUNT(*) FROM project_host_opens WHERE run_handle=?',
                                                           (pair.binding.run_handle,)).fetchone()[0])
        value = pair.call()
        self.assertEqual(pair.worker_digest, value['worker']['host_outcome_digest'])
        self.assertEqual(pair.verifier_digest, value['verifier']['host_outcome_digest'])
        self.assertEqual('verification', value['verifier']['task_family'])
        self.assertNotIn(b'PRIVATE-', json.dumps(value).encode())

    def test_the_qa_scope_of_either_recorder_is_the_same_and_only_the_status_carries_the_marker(self):
        values, statuses = {}, {}
        for recorded_by in ('service', 'primary'):  # two independent stores, each with its own real pair
            pair = self.pair(recorded_by)
            values[recorded_by] = self.shape(pair.call())
            statuses[recorded_by] = service_record.recording_status(pair.store, pair.b2.authority())
            self.assertEqual(recorded_by, pair.b2.recorder())
        self.assertEqual(values['service'], values['primary'])
        marker = 'recorded by the service, not reviewed by the primary'
        worker = [entry for entry in statuses['service']['recorded'] if entry['automatic']]
        self.assertEqual([marker], [entry['note'] for entry in worker])
        self.assertEqual([None], [entry['note'] for entry in statuses['primary']['recorded'] if not entry['automatic']
                                  and entry['recorded_by'] == 'primary'][:1])
        self.assertNotIn(marker, json.dumps(values))

    def test_routing_evidence_and_worker_input_are_asked_of_independent_stores_and_agree(self):
        answers, notes = {}, {}
        for recorded_by in ('service', 'primary'):
            fresh = Fresh.make(self, HostFixture)
            record = fresh.service_record if recorded_by == 'service' else fresh.primary_record
            digest = record()['admission']['host_outcome_digest']
            service, token = fresh.serve()
            task_id = fresh.task['task_id'] if 'task_id' in fresh.task else fresh.task['id']
            payload = {'host_outcome_digest': digest, 'task_id': task_id, 'family': fresh.task['family']}
            answers[recorded_by] = (fresh.ask(service, token, 'history.routing_evidence', digest, payload),
                                    fresh.ask(service, token, 'history.worker_input', digest,
                                              {'host_outcome_digest': digest}))
            notes[recorded_by] = [entry['note'] for entry in
                                  service_record.recording_status(fresh.store, fresh.authority())['recorded']]
            self.assertEqual(recorded_by, fresh.recorder())
        self.assertEqual(answers['service'][0], answers['primary'][0])
        self.assertEqual(self.shape(answers['service'][1]), self.shape(answers['primary'][1]))
        self.assertEqual(['recorded by the service, not reviewed by the primary'], notes['service'])
        self.assertEqual([None], notes['primary'])
