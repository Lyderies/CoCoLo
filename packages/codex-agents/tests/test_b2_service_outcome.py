"""Slice B2 (decision 92 A): the project service records a finished child's outcome itself.

DESIGN-0.4.0-B2-outcome.md draft 2 with the s2 corrections, rows B2-1 to B2-18 (measurements/b2/requirements.md maps
each row to its code and its test). Owner decision 92: after the orchestration closes, is interrupted or ends at the
24 h idle point, nothing new is recorded ("unrecorded" is final); the service records right after the result is safely
stored, plus a check after a restart; who recorded lives in a small separate table (schema step 19 -> 20), receipts and
records unchanged. The service never writes QA and never sets `accepted`.
Integration stage: real stores in temporary folders, the run fixtures of the record-context tests. The orchestration
state the final predicate reads is set through `OrchestrationOwner._current` (the journal has its own tests).
"""
import copy
import inspect
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_deadlines_retention as dr
from tests import test_record_context as rc
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_project_history import binding as history_binding

from codex_agents import history_api, project_history, project_result_contexts, service_record
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.hosts import CLAUDE_HOST_CLIENT as CLAUDE, CODEX_HOST_CLIENT as CODEX
from codex_agents.host_result_contracts import HostResultReference, decode_host_result_reference
from codex_agents.learning_contracts import canonical_json
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.service_record import ServiceRecordAuthority

REQUIREMENT = ('B2-OUTCOME', 'B2 records real project stores in temporary folders (run fixtures, the writer, the '
               'store transaction); the orchestration journal and the clock are the controls that expose each refusal.')
OTHER_RUNTIME = 'b' * 64


def requirement(cls):
    return __import__('test_framework').requirements(REQUIREMENT[0], why_not_scenario=REQUIREMENT[1])(cls)


def upgrading_store(fixture):
    """The record-context fixtures' store, opened by the authorized upgrade path."""
    return ProjectHistory(fixture.directory, fixture.project, result_key=fixture.key, allow_schema_upgrade=True,
                          allow_legacy_orchestration_reference=True,
                          result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * rc.DAY_MS),
                          result_clock_ms=lambda: fixture.base.stamp)


def view(status='active', client=CODEX, runtime=None):
    runtime = runtime or history_binding().runtime_digest
    return {'snapshot': {'status': status, 'client_version': client, 'scope': {'runtime_digest': runtime}},
            'checkpoint_digest': '0' * 64}


class B2Fixture(rc.RecordedContextFixture):
    """One real committed run (opened, consumed, published) with nothing recorded yet."""

    def setUp(self):
        super().setUp()
        self.open_and_consume()
        self.publish()
        self.receipt = service_record._receipt_id(self.store, self.binding)

    def authority(self, **changes):
        return ServiceRecordAuthority(**{'runtime_digest': self.binding.runtime_digest, 'service': True, **changes})

    def service_record(self, store=None):
        return (store or self.store).admit_service_result(self.binding, final_check=self.authority())

    def primary_record(self, store=None, **options):
        store = store or self.store
        original = store.stored_result_context(self.binding, host_class=self.host_class)
        return store.admit_host_result(self.reference, assignment=original.assignment, task=original.task,
                                       route=original.route, client_capability=original.client_capability,
                                       before_commit=options.pop('before_commit', lambda: None), **options)

    def orchestration(self, status='active', **changes):
        return mock.patch.object(OrchestrationOwner, '_current', return_value=view(status, **changes))

    def counts(self):
        db = self.store._connection
        return (db.execute('SELECT COUNT(*) FROM records').fetchone()[0],
                db.execute('SELECT COUNT(*) FROM project_result_recorders').fetchone()[0],
                tuple(db.execute('SELECT state,outcome_digest FROM project_result_contexts').fetchone()))

    def recorder(self):
        return service_record.recorder_of(self.store, self.receipt)

    def due(self):
        return self.store.result_reservation(self.binding).retained_until_ms

    def copy_store(self):
        """The store as it is now, as another process would find it: a copy of the folder, opened on its own."""
        self.store.close()
        target = Path(tempfile.mkdtemp(dir=self.directory.parent))
        self.addCleanup(shutil.rmtree, target, True)
        for item in self.directory.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
        self.fixture.store = rc.seven_day_store(self.fixture)
        other = copy.copy(self.fixture)
        other.directory = target
        return rc.seven_day_store(other)


@requirement
class ServiceRecordsTests(B2Fixture):
    """B2-1, B2-2, B2-4, B2-8, B2-11, B2-17."""

    def test_service_records_committed_terminal_without_primary(self):
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])
        result = self.service_record()
        self.assertEqual('recorded', rc.context_row(self.store, *self.attempt)[0])
        self.assertEqual(self.receipt, result['admission']['record_id'])
        self.assertEqual(result['admission']['host_outcome_digest'], rc.context_row(self.store, *self.attempt)[4])
        self.assertEqual('service', self.recorder())
        self.assertEqual(self.store.record(result['outcome_commit']['record_id']).content_digest,
                         result['admission']['host_outcome_digest'])

    def test_orchestration_closes_after_primary_never_returns(self):
        owner = OrchestrationOwner(self.store)
        scope = mock.Mock()
        scope.matches_attempt = lambda value: True
        scope.project_id = self.binding.project_id
        with mock.patch.object(OrchestrationOwner, '_account_released', return_value=False):
            self.assertTrue(owner._unfinished(scope, self.binding.orchestration_handle))  # unrecorded: held open
            self.service_record()
            self.assertFalse(owner._unfinished(scope, self.binding.orchestration_handle))  # recorded: may close

    def test_outcome_digest_equal_whoever_records(self):
        other = self.copy_store()
        by_service = self.service_record()
        by_primary = self.primary_record(other)
        self.assertEqual(by_service['admission'], by_primary['admission'])
        for name in ('event_commit', 'outcome_commit', 'admission_commit'):
            self.assertEqual(by_service[name]['content_digest'], by_primary[name]['content_digest'], name)
        self.assertEqual('primary', service_record.recorder_of(other, self.receipt))
        self.assertEqual('service', self.recorder())
        other.close()

    def test_primary_retrieval_idempotent_after_service_record(self):
        recorded = self.service_record()
        counted = self.counts()
        first = self.primary_record()
        second = self.primary_record()
        self.assertEqual(recorded, first)
        self.assertEqual(first, second)
        self.assertEqual(counted, self.counts())  # retrieval wrote nothing
        self.assertEqual('service', self.recorder())

    def test_retrieval_after_normal_close(self):
        recorded = self.service_record()
        with self.orchestration('completed'):
            self.assertEqual(recorded, self.primary_record())  # K2 reads only: no orchestration state, no expiry
        self.fixture.base.stamp = self.due() - 1
        self.assertEqual(recorded, self.primary_record())

    def test_service_and_primary_race_exactly_one_record(self):
        for first in ('service', 'primary'):
            with self.subTest(first=first):
                other = self.copy_store()
                ran = []

                def between():
                    ran.append(True)
                    other._after_body_io = None
                    if first == 'primary':
                        self.primary_record(other)
                    else:
                        self.service_record(other)
                other._after_body_io = between
                if first == 'primary':  # the service started, the primary got in at the barrier
                    loser = self.service_record(other)
                    self.assertIsNone(loser)  # stops with no write
                else:  # the primary started, the service got in: the primary takes the retrieval branch
                    loser = self.primary_record(other)
                    self.assertEqual(self.receipt, loser['admission']['record_id'])
                self.assertEqual([True], ran)
                db = other._connection
                self.assertEqual((1, 1, 1), tuple(db.execute(
                    f'SELECT COUNT(*) FROM records WHERE record_type=?', (kind,)).fetchone()[0]
                    for kind in ('event', 'outcome', 'host_result_admission')))
                self.assertEqual(1, db.execute('SELECT COUNT(*) FROM project_result_recorders').fetchone()[0])
                self.assertEqual(first, service_record.recorder_of(other, self.receipt))
                other.close()

    def test_service_record_feeds_verifier_open(self):
        recorded = self.service_record()
        digest = recorded['admission']['host_outcome_digest']
        self.assertEqual(self.expected(), self.store.stored_worker_context(digest, host_class=self.host_class))
        self.assertEqual(self.expected(), self.store.stored_result_context(self.binding, host_class=self.host_class,
                                                                           recorded=True))

    def test_service_never_writes_qa_or_accepted(self):
        before = self.store._connection.execute('SELECT COUNT(*) FROM project_operational_qa').fetchone()[0]
        recorded = self.service_record()
        outcome = json.loads(self.store.record(recorded['outcome_commit']['record_id']).content)
        self.assertEqual(before, self.store._connection.execute('SELECT COUNT(*) FROM project_operational_qa').fetchone()[0])
        self.assertIsNot(True, outcome.get('accepted'))
        source = Path(service_record.__file__).read_text()
        self.assertNotRegex(source, r"project_operational_qa|commit_operational_qa|\[.accepted.\]\s*=")

    def test_unreviewed_marker_in_status(self):
        recorded = self.service_record()
        status = service_record.recording_status(self.store, self.authority())
        (entry,) = status['recorded']
        self.assertEqual(('service', True, recorded['admission']['host_outcome_digest']),
                         (entry['recorded_by'], entry['automatic'], entry['outcome_digest']))

    def test_recorder_first_winner_immutable(self):
        self.service_record()
        self.primary_record()
        self.assertEqual('service', self.recorder())
        # a pre-D1 receipt whose association is missing: the repair writes the association only
        self.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        before = self.store._connection.execute('SELECT * FROM project_result_recorders').fetchall()
        self.primary_record()
        self.assertEqual('recorded', rc.context_row(self.store, *self.attempt)[0])
        self.assertEqual(before, self.store._connection.execute('SELECT * FROM project_result_recorders').fetchall())
        self.assertIsNone(self.service_record_again())

    def service_record_again(self):
        return self.service_record()

    def test_receipt_versions_1_and_2_unchanged(self):
        receipt = json.loads(self.service_record() and self.store.record(self.receipt).content)
        self.assertEqual(1, receipt['schema_version'])
        self.assertEqual({'schema_version', 'record_type', 'record_id', 'run_id', 'binding_digest',
                          'registration_digest', 'dispatch_result', 'terminal_content_digest', 'observed_at_ms',
                          'event_record_id', 'event_content_digest', 'outcome_record_id', 'host_outcome_digest'},
                         set(receipt))
        self.assertTrue(service_record.recorders_consistent(self.store._connection))


@requirement
class EligibilityTests(B2Fixture):
    """B2-3, B2-7, B2-13, B2-14, B2-16: only a fully verified, still-recordable run is recorded."""

    def refused(self, reason, *, record=None):
        before = self.counts()
        with self.assertRaisesRegex(HistoryError, reason):
            (record or self.service_record)()
        self.assertEqual(before, self.counts())  # nothing written

    def test_each_break_is_refused_with_its_named_reason_and_writes_nothing(self):
        other_runtime = lambda: self.store.admit_service_result(self.binding,
                                                               final_check=self.authority(runtime_digest=OTHER_RUNTIME))
        self.refused('history_unrecordable_other_runtime', record=other_runtime)
        with self.orchestration(client=CLAUDE):
            self.refused('history_unrecordable_host_class')
        for status in ('completed', 'failed', 'interrupted', 'cancelled'):
            with self.orchestration(status):
                self.refused('history_unrecordable_orchestration_closed')
        with self.orchestration('paused'):  # a paused orchestration still takes its finished runs
            self.assertEqual('recorded', self.service_record() and 'recorded')

    def test_context_in_any_state_but_committed_is_refused(self):
        for state in ('bound', 'leased', 'opened'):
            with self.subTest(state=state):
                self.store._connection.execute('UPDATE project_result_contexts SET state=?', (state,))
                self.refused('history_unrecordable_context_state')
        self.store._connection.execute("UPDATE project_result_contexts SET state='legacy_no_context'")
        self.refused('history_unrecordable_legacy_no_context')

    def test_a_tampered_body_is_refused_and_nothing_is_written(self):
        name = sorted(self.directory.glob('.result-*.json'))
        self.assertTrue(name)
        name[0].write_bytes(b'{"tampered": true}')
        with self.assertRaises(HistoryError):
            self.service_record()
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])
        self.assertIsNone(self.recorder())

    def test_closed_or_expired_not_recorded(self):
        for status in ('completed', 'interrupted'):  # closed, interrupted (the 24 h idle end is the same close)
            for entry in ('service', 'primary'):
                with self.subTest(status=status, entry=entry), self.orchestration(status):
                    self.refused('history_unrecordable_orchestration_closed',
                                 record=self.service_record if entry == 'service' else self.primary_record)
        self.fixture.base.stamp = self.due()
        for entry in ('service', 'primary'):
            with self.subTest(expired=entry):
                before = self.counts()[:2]
                with self.assertRaisesRegex(HistoryError, 'history_result_expired|history_not_authorized'):
                    (self.service_record if entry == 'service' else self.primary_record)()
                self.assertEqual(before, self.counts()[:2])

    def test_final_check_required_and_typed(self):
        with self.assertRaises(TypeError):
            self.store.admit_service_result(self.binding)  # required keyword
        for entry in (lambda check: self.store.admit_service_result(self.binding, final_check=check),
                      lambda check: self.primary_record(final_check=check)):
            for bad in (lambda: None, lambda *a, **k: None, object(), mock.Mock(), 'authority'):
                with self.subTest(bad=repr(bad)[:30]), self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                    entry(bad)
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])

    def test_final_check_refuses_each_state(self):
        for phase in ('K1', 'K3'):
            with self.subTest(phase=phase, state='closed'), self.orchestration('completed'), \
                    self.assertRaisesRegex(HistoryError, 'history_unrecordable_orchestration_closed'):
                self.authority().check(self.store, self.binding, phase, reservation=self.store.result_reservation(self.binding))
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            self.authority().check(self.store, self.binding, 'K9')
        self.fixture.base.stamp = self.due()
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.authority().check(self.store, self.binding, 'K1', reservation=self.store.result_reservation(self.binding))

    def test_final_check_is_load_bearing(self):
        with self.orchestration('completed'), mock.patch.object(ServiceRecordAuthority, 'check', return_value=None):
            result = self.service_record()  # nothing else stops a closed orchestration's record: the predicate does
        self.assertEqual('recorded', rc.context_row(self.store, *self.attempt)[0])
        self.assertEqual(self.receipt, result['admission']['record_id'])

    def test_primary_omitting_final_check_still_runs_the_predicate(self):
        with self.orchestration('completed'), self.assertRaisesRegex(HistoryError, 'history_unrecordable_orchestration_closed'):
            self.primary_record()

    def test_forged_and_replayed_outcomes_refused(self):
        counted = self.counts()
        unknown = AttemptBinding(**{**self.binding.as_data(), 'claim_id': 'claim-unknown',
                                    'claim_digest': 'c' * 64})
        with self.assertRaises(HistoryError):  # (a) an unknown run
            self.store.admit_service_result(unknown, final_check=self.authority())
        swapped = HostResultReference(self.binding, self.reference.terminal_record_id,
                                      self.reference.terminal_content_digest, 'd' * 64)
        original = self.store.stored_result_context(self.binding, host_class=self.host_class)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):  # (b) swapped digests
            self.store.admit_host_result(swapped, assignment=original.assignment, task=original.task,
                                         route=original.route, client_capability=original.client_capability,
                                         before_commit=lambda: None)
        other = AttemptBinding(**{**self.binding.as_data(), 'claim_id': 'claim-2', 'claim_digest': 'e' * 64})
        with self.assertRaises(HistoryError):  # (c) one run's reference with another run's binding
            self.store.admit_host_result(HostResultReference(other, self.reference.terminal_record_id,
                                                             self.reference.terminal_content_digest,
                                                             self.reference.result_digest),
                                         assignment=original.assignment, task=original.task, route=original.route,
                                         client_capability=original.client_capability, before_commit=lambda: None)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):  # (d) injected into the internal entry
            self.store._admit_host_result(swapped, assignment=original.assignment, task=original.task,
                                         route=original.route, client_capability=original.client_capability,
                                         before_commit=None, final_check=self.authority(), recorder='service')
        self.assertEqual(counted, self.counts())
        self.fixture.base.stamp = self.due()  # (e) replay after expiry: first admission refused
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.admit_service_result(self.binding, final_check=self.authority())
        with self.assertRaises(HistoryError):  # (f) another project's binding
            self.store.admit_service_result(AttemptBinding(**{**self.binding.as_data(),
                                                              'project_id': 'prj_' + '1' * 32}),
                                            final_check=self.authority())
        payload = self.reference.as_data()
        for name in ('recorded_by', 'recorder'):  # (g) no request field sets the recorder
            with self.assertRaises(Exception):
                decode_host_result_reference({**payload, name: 'service'})

    def test_known_receipt_branches(self):
        start = self.fixture.base.stamp
        recorded = self.service_record()
        rows = self.counts()
        with self.orchestration('completed'):  # K2: open, closed -> the same answer, no write
            self.assertEqual(recorded, self.primary_record())
        self.assertEqual(rows, self.counts())
        # K3: a receipt with the association missing is a mutation: only while the predicate passes
        self.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        with self.orchestration('completed'), self.assertRaisesRegex(HistoryError, 'history_unrecordable_orchestration_closed'):
            self.primary_record()
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])
        self.fixture.base.stamp = self.due()
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.primary_record()
        # K2 with a corrupted outcome digest
        self.fixture.base.stamp = start
        self.store._connection.execute("UPDATE project_result_contexts SET state='recorded',outcome_digest=?", ('f' * 64,))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.primary_record()


@requirement
class RecheckTests(unittest.TestCase):
    """B2-5: the primary's recheck still enforces what it enforced, by a named reason, and writes nothing."""

    def test_recheck_refusals_are_named_and_write_nothing(self):
        from codex_agents import client_runtime
        names = client_runtime.RECHECK_REASONS
        self.assertEqual({'recheck_result_mismatch', 'recheck_inputs_mismatch'}, set(names))
        for reason in names:
            self.assertEqual(reason, client_runtime.RuntimeResultError(reason).reason_code)
        self.assertEqual('host_client_mismatch', client_runtime.RuntimeResultError('host_client_mismatch').reason_code)
        self.assertEqual('dispatch_not_found', client_runtime.RuntimeResultError('dispatch_not_found').reason_code)
        source = inspect.getsource(client_runtime.record_codex_result_client)
        for reason in ('recheck_result_mismatch', 'recheck_inputs_mismatch', 'host_client_mismatch',
                       'dispatch_not_found'):
            self.assertIn(reason, source)


@requirement
class AuthorityTests(unittest.TestCase):
    """B2-6: no new authority."""

    OPERATIONS = frozenset({
        'history.record_event', 'history.record_intent', 'history.record_outcome', 'history.bind_verifier',
        'history.commit_terminal', 'history.pending_terminals', 'history.acknowledge_terminal',
        'history.read_record', 'history.admit_host_result', 'history.record_host_open', 'history.worker_reference',
        'history.qa_scope', 'history.issue_qa_checks', 'history.commit_qa', 'history.qa_details',
        'history.open_scope', 'history.worker_input', 'history.learning_cycle', 'history.learning_report',
        'history.routing_evidence', 'history.learning_feedback', 'history.learning_review_case',
        'history.orchestration_report', 'history.capacity_report', 'history.result_reference'})

    def test_operation_set_unchanged(self):
        self.assertEqual(self.OPERATIONS, history_api.HISTORY_OPERATIONS)

    def test_service_entry_not_reachable_over_socket(self):
        self.assertFalse(any('service_result' in name for name in history_api.HISTORY_OPERATIONS))
        from codex_agents import history_service, service_host
        for module in (history_api, history_service, service_host):
            self.assertNotIn('admit_service_result', inspect.getsource(module))
        self.assertTrue(hasattr(ProjectHistory, 'admit_service_result'))
        with self.assertRaises(Exception):
            history_api.HistoryRequest(1, 'history.admit_service_result', 'req-1', 'prj_' + '1' * 32, 'a' * 64, {})

    def test_unenrolled_owner_and_other_class_refused(self):
        from codex_agents import service_host
        source = inspect.getsource(service_host)
        self.assertIn("entry.enrolled.host_client == OWNER_CLIENT", source)
        self.assertIn("stored_result_context(binding, entry.enrolled.host_client)", source)


@requirement
class RestartTests(B2Fixture):
    """B2-9: the next service of the same runtime records from the store; nothing is ever half recorded."""

    def sweep(self, store=None, **changes):
        return service_record.record_pass(store or self.store, self.authority(**changes))

    def test_restart_a_stop_after_commit_is_recorded_by_the_next_service(self):
        store = self.reopen()
        result = self.sweep(store)
        self.assertEqual(1, result['recorded'])
        self.assertEqual('service', self.recorder())

    def test_restart_b_a_stop_mid_admission_leaves_nothing_half_recorded(self):
        before = self.counts()
        with mock.patch.object(self.store, '_append_locked', side_effect=[mock.DEFAULT, mock.DEFAULT, OSError('cut')]), \
                mock.patch.object(project_history.ProjectHistory, '_append_locked', autospec=True,
                                  side_effect=OSError('cut')):
            with self.assertRaises(HistoryError):
                self.service_record()
        self.assertEqual(before, self.counts())
        self.assertEqual(1, self.sweep()['recorded'])  # and the next try finishes it

    def test_restart_c_a_stop_before_commit_records_nothing(self):
        self.store._connection.execute("UPDATE project_result_contexts SET state='bound'")
        self.assertEqual({'attempted': 0, 'recorded': 0, 'refused': {}, 'already': 0}, self.sweep())
        self.assertEqual(0, self.counts()[1])

    def test_restart_d_a_run_of_another_runtime_is_unrecordable(self):
        result = self.sweep(runtime_digest=OTHER_RUNTIME)
        self.assertEqual({'history_unrecordable_other_runtime': 1}, result['refused'])
        before = self.counts()
        with self.assertRaisesRegex(HistoryError, 'history_unrecordable_other_runtime'):
            self.primary_record(final_check=self.authority(service=False, runtime_digest=OTHER_RUNTIME))
        self.assertEqual(before, self.counts())
        status = service_record.recording_status(self.store, self.authority(runtime_digest=OTHER_RUNTIME))
        self.assertEqual({'other_runtime': 1}, status['unrecordable'])

    def test_restart_e_a_second_process_cannot_open_the_writer(self):
        with self.assertRaisesRegex(HistoryError, 'history_writer_busy'):
            rc.seven_day_store(self.fixture)


@requirement
class StatusTests(B2Fixture):
    """B2-10: who recorded, from the recorder row; what awaits a record; what cannot be recorded."""

    def test_status_shows_recorder_and_pending(self):
        authority = self.authority()
        status = service_record.recording_status(self.store, authority)
        self.assertEqual((0, 1, {}), (len(status['recorded']), status['awaiting_record'], status['unrecordable']))
        with self.orchestration('completed'):
            status = service_record.recording_status(self.store, authority)
        self.assertEqual({'orchestration_closed': 1}, status['unrecordable'])
        self.primary_record()
        (entry,) = service_record.recording_status(self.store, authority)['recorded']
        self.assertEqual(('primary', False), (entry['recorded_by'], entry['automatic']))

    def test_a_receipt_with_no_recorder_row_reads_before_b2_until_a_later_row_exists(self):
        self.service_record()
        self.store._connection.execute('DELETE FROM project_result_recorders')
        (entry,) = service_record.recording_status(self.store, self.authority())['recorded']
        self.assertEqual('primary (before B2)', entry['recorded_by'])

    def test_attribution_survives_reservation_retirement(self):
        self.service_record()
        self.fixture.base.stamp = self.due()
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual('expired', self.store.result_reservation(self.binding).state)
        self.assertEqual('service', self.recorder())  # keyed by the receipt, no foreign key to the reservation
        foreign = self.store._connection.execute("PRAGMA foreign_key_list(project_result_recorders)").fetchall()
        self.assertEqual(['history_identity'], [row[2] for row in foreign])


@requirement
class SerializationTests(B2Fixture):
    """B2-15: one boundary for admission, close, interruption, idle end and expiry (4.2)."""

    def at_barrier(self, action, entry='service'):
        self.store._after_body_io = lambda: (setattr(self.store, '_after_body_io', None), action())
        before = self.counts()
        return before

    def test_close_vs_record_barrier(self):
        before = self.at_barrier(lambda: setattr(self, 'patch', self.orchestration('completed')) or self.patch.start())
        self.addCleanup(lambda: self.patch.stop() if hasattr(self, 'patch') else None)
        with self.assertRaisesRegex(HistoryError, 'history_unrecordable_orchestration_closed'):
            self.service_record()
        self.assertEqual(before, self.counts())

    def test_idle_end_vs_record_barrier(self):
        before = self.at_barrier(lambda: setattr(self, 'patch', self.orchestration('interrupted')) or self.patch.start())
        self.addCleanup(lambda: self.patch.stop() if hasattr(self, 'patch') else None)
        with self.assertRaisesRegex(HistoryError, 'history_unrecordable_orchestration_closed'):
            self.primary_record()
        self.assertEqual(before, self.counts())

    def test_expiry_vs_record_barrier(self):
        due = self.due()
        before = self.at_barrier(lambda: setattr(self.fixture.base, 'stamp', due))
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.service_record()
        self.assertEqual(before, self.counts())
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])  # the pass retires it, not the record

    def test_retirement_and_lost_owner_at_the_barrier_refuse_the_record(self):
        before = self.at_barrier(lambda: project_result_contexts.begin_retirement(self.store, self.binding, 'retention'))
        with self.assertRaises(HistoryError):
            self.service_record()
        self.assertEqual(before[:2], self.counts()[:2])

    def test_admission_first_then_close_sees_a_recorded_run(self):
        self.service_record()
        owner = OrchestrationOwner(self.store)
        scope = mock.Mock(project_id=self.binding.project_id)
        scope.matches_attempt = lambda value: True
        with mock.patch.object(OrchestrationOwner, '_account_released', return_value=False):
            self.assertFalse(owner._unfinished(scope, self.binding.orchestration_handle))
        with self.orchestration('completed'):  # and now nothing new may be recorded
            self.assertIsNone(self.service_record())


@requirement
class SweepTests(dr.LeaseBackedFixture):
    """B2-12, B2-18: bounded, fair, history-independent selection; permanent refusals never starve it."""

    RUNS = 25

    def committed(self):
        for number in range(1, self.RUNS + 1):
            self.bound(number)
            self.publish(number)

    def drive(self, behave, passes=8):
        seen = []

        def admit(binding, *, final_check):
            seen.append(binding.claim_id)
            outcome = behave(binding)
            if outcome is not None:
                raise HistoryError(outcome)
            return {}
        authority = ServiceRecordAuthority(history_binding().runtime_digest, True)
        counts = []
        with mock.patch.object(self.store, 'admit_service_result', side_effect=admit):
            for index in range(passes):
                counts.append(service_record.record_pass(self.store, authority, clock=lambda: 10_000 + index))
        return seen, counts

    def test_service_recording_past_every_limit(self):
        self.committed()
        seen, counts = self.drive(lambda binding: None, passes=4)
        self.assertTrue(all(count['attempted'] <= service_record.SWEEP_ATTEMPTS for count in counts))
        self.assertEqual(self.RUNS, len(set(seen)))  # every eligible terminal is reached, later

    def test_selection_invariants(self):
        self.committed()
        authority = ServiceRecordAuthority(history_binding().runtime_digest, True)
        window = self.store._connection.execute(service_record.WINDOW_SQL, (self.store.project_id, 0, 0,
                                                                            service_record.SWEEP_WINDOW)).fetchall()
        self.assertEqual(self.RUNS, len(window))
        for _, run_handle, attempt in window:
            row = project_result_contexts._row(self.store, run_handle, attempt)
            binding = project_result_contexts._lease_binding(self.store, row)
            self.assertEqual((row['claim_id'], run_handle, attempt),
                             (binding.claim_id, binding.run_handle, binding.attempt))  # I1 association
        self.assertIsNotNone(authority)

    def test_permanent_refusals_do_not_starve(self):
        self.committed()
        closed = {history_binding(number).claim_id for number in range(1, 11)}
        seen, counts = self.drive(lambda binding: 'history_unrecordable_orchestration_closed'
                                  if binding.claim_id in closed else None, passes=6)
        self.assertEqual(self.RUNS, len(set(seen)))  # the refused ten never block the other fifteen
        self.assertEqual(len(closed), sum(seen.count(claim) for claim in closed))  # and are tried once, not looped

    def test_transient_retry_backoff(self):
        self.committed()
        failing = history_binding(1).claim_id
        seen, counts = self.drive(lambda binding: 'history_result_unavailable' if binding.claim_id == failing else None,
                                  passes=4)
        self.assertEqual(1, seen.count(failing))  # inside its first minute it is not retried
        state = self.store._record_sweep['backoff']
        self.assertEqual(1, len(state))
        (waiting,) = state.values()
        self.assertEqual(1, waiting[1])
        authority = ServiceRecordAuthority(history_binding().runtime_digest, True)
        with mock.patch.object(self.store, 'admit_service_result', side_effect=HistoryError('history_result_unavailable')):
            for _ in range(3):  # later passes after the wait double it, up to half an hour
                service_record.record_pass(self.store, authority, clock=lambda: waiting[0] + 10 ** 9)
        self.assertLessEqual(max(delay - 0 for delay, _ in self.store._record_sweep['backoff'].values()) - (waiting[0] + 10 ** 9),
                             service_record.BACKOFF_LAST_MS)

    def test_refusal_health_signal(self):
        self.committed()
        bad = history_binding(2).claim_id
        self.drive(lambda binding: 'history_conflict' if binding.claim_id == bad else
                   ('history_unrecordable_orchestration_closed' if binding.claim_id == history_binding(3).claim_id
                    else None), passes=2)
        signals = service_record.health_signals(self.store)
        self.assertEqual(1, len(signals))  # an integrity refusal once per claim; an ordinary refusal none
        self.assertTrue(signals[0].startswith('integrity:'))

    def test_recording_pass_cost_with_tombstones(self):
        self.committed()
        plan = ' '.join(row[3] for row in self.store._connection.execute(
            'EXPLAIN QUERY PLAN ' + service_record.WINDOW_SQL, (self.store.project_id, 0, 0, service_record.SWEEP_WINDOW)))
        self.assertIn('project_result_contexts_live', plan)
        self.assertNotIn('SCAN c', plan.replace('SCAN c USING', ''))
        db = self.store._connection
        run_handle, attempt = self.attempt(1)
        for index in range(2_000):  # tombstones: ended contexts leave the selection however many there are
            db.execute("INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,lease_deadline_ms) "
                       "VALUES(?,?,?,'expired',0,0)", (self.store.project_id, f'hrn_{9000 + index:048x}', 1))
        steps = []
        db.set_trace_callback(steps.append)
        service_record.record_pass(self.store, ServiceRecordAuthority(history_binding().runtime_digest, True), clock=lambda: 1)
        db.set_trace_callback(None)
        self.assertLess(len(steps), 400)


@requirement
class SchemaStepTests(B2Fixture):
    """Owner decision 92.3 A: one schema step, 19 -> 20, for the recorder table, with its upgrade path."""

    def test_schema_is_20_and_the_table_is_exactly_as_designed(self):
        self.assertEqual(20, project_history._SCHEMA_VERSION)
        columns = [row[1] for row in self.store._connection.execute('PRAGMA table_info(project_result_recorders)')]
        self.assertEqual(['project_id', 'record_id', 'recorded_by', 'recorded_at_ms'], columns)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute("INSERT INTO project_result_recorders VALUES(?,?,?,1)",
                                           (self.store.project_id, 'host_result_admission:' + 'a' * 64, 'someone'))

    def test_a_format_19_project_store_upgrades_to_20_and_keeps_its_rows(self):
        self.service_record()
        db = self.store._connection
        rows = {name: sorted(tuple(row) for row in db.execute(f'SELECT * FROM {name}'))
                for name in ('records', 'project_result_contexts', 'project_holds')}
        db.execute('DROP TABLE project_result_recorders')
        db.execute('PRAGMA user_version=19')
        self.store.close()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):  # an ordinary start never upgrades
            rc.seven_day_store(self.fixture)
        self.fixture.store = upgrading_store(self.fixture)
        db = self.store._connection
        self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(0, db.execute('SELECT COUNT(*) FROM project_result_recorders').fetchone()[0])
        for name, kept in rows.items():
            self.assertEqual(kept, sorted(tuple(row) for row in db.execute(f'SELECT * FROM {name}')), name)
        self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-20.*'))))  # the copy made for it

    def test_a_failed_validator_rolls_the_step_back_and_removes_its_copy(self):
        db = self.store._connection
        db.execute('DROP TABLE project_result_recorders')
        db.execute('PRAGMA user_version=19')
        self.store.close()
        with mock.patch.object(service_record, 'recorders_consistent', return_value=False), \
                self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            upgrading_store(self.fixture)
        connection = sqlite3.connect(self.directory / 'history.sqlite3')
        self.assertEqual(19, connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual([], connection.execute("SELECT name FROM sqlite_master WHERE name='project_result_recorders'").fetchall())
        connection.close()
        self.assertEqual([], list(self.directory.glob('history.sqlite3.before-20.*')))
        self.fixture.store = upgrading_store(self.fixture)  # and a later honest upgrade succeeds
        self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
