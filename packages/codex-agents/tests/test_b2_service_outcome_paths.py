"""Slice B2 fix round 1: the real paths (reviews rev040b2r1a / rev040b2r1b).

The first B2 module sets the orchestration state through `OrchestrationOwner._current` and drives the admission
directly. This module drives the store's own operations instead: the real orchestration close, interruption and 24 h
idle end, the real publication and restart/backstop entry of the writer lane, real lost ownership, the primary's record
command and the service host's own authorization, both receipt versions, and the recording sweep's limits, isolation and
refusal reasons. It ends with the production findings MED-1 (one candidate's failure never stops the pass) and MED-2 (a
missing or unverifiable orchestration refuses). Time budget: scenario (60 s); the first B2 module is kept separate.
"""
import json
import threading
from unittest import mock

import tests.support  # noqa: F401
from tests import test_record_context as rc
from tests.test_b2_service_outcome import B2Fixture, OTHER_RUNTIME, requirement

from codex_agents import project_history, refusal_steps, service_record
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.hosts import CODEX_HOST_CLIENT as CODEX
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import ORCHESTRATION_IDLE_MS, OrchestrationOwner
from codex_agents.service_record import ServiceRecordAuthority

TERMINAL = ('completed', 'failed', 'interrupted', 'cancelled')
ORCHESTRATION_TABLES = ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations')


class RealFixture(B2Fixture):
    """The B2 run with its orchestration journal real: it is ended only through the store's own operations."""

    def scope(self):
        return OrchestrationScope(**{name: getattr(self.binding, name) for name in SCOPE_FIELDS})

    def orchestration_owner(self):
        return OrchestrationOwner(self.store)

    def end_orchestration(self, status='completed'):
        """The real close request. The account's release of the run is the one thing not under test here."""
        self.store._orchestration_limits = OrchestrationLimits(1, 8)
        owner, handle = self.orchestration_owner(), self.binding.orchestration_handle
        current = owner._current(self.scope(), handle)['snapshot']
        request = OrchestrationRequest(1, 'orchestration.close', 'b2-close-' + status, self.scope(), {
            'orchestration_handle': handle, 'expected_sequence': current['sequence'], 'status': status,
            'goal_tokens_used': None, 'goal_time_used_seconds': None})
        with mock.patch.object(OrchestrationOwner, '_account_released', return_value=True):
            return owner.apply(request, client_version=CODEX, before_commit=lambda: None)

    def idle_end(self):
        """The 24 h idle end through the store's own pass (clock moved past the idle point)."""
        self.fixture.base.stamp += ORCHESTRATION_IDLE_MS
        # B6 (RS-B-B6-2): the unattended close also waits for the reservation's register row, which the account's
        # release acknowledgement releases `completed`; this stands in for it as `_account_released` does.
        self.store._connection.execute(
            "UPDATE project_holds SET state='released',release_kind='completed',released_at_ms=? "
            "WHERE kind='reservation' AND identity=? AND state<>'released'",
            (self.fixture.base.stamp, self.binding.claim_id))
        with mock.patch.object(OrchestrationOwner, '_account_released', return_value=True):
            return self.orchestration_owner().end_idle(self.fixture.base.stamp, 10)

    def status_of_orchestration(self):
        return self.orchestration_owner()._current(self.scope(), self.binding.orchestration_handle)['snapshot']['status']

    def end_by(self, way):
        if way == 'idle':
            self.assertEqual(1, self.idle_end())
        else:
            self.end_orchestration(way)

    def record_by(self, entry):
        return self.service_record() if entry == 'service' else self.primary_record()

    def writes(self):
        """Every durable thing a recording would write: records, recorders and the context's state."""
        return self.counts()

    def refused_by(self, entry, reason):
        before = self.writes()
        with self.assertRaisesRegex(HistoryError, f'^{reason}$'):
            self.record_by(entry)
        self.assertEqual(before, self.writes())

    def put(self, state):
        if state == 'closed':
            self.end_orchestration('completed')
        elif state in ('expired', 'retired'):
            self.fixture.base.stamp = self.due()
            if state == 'retired':
                self.assertEqual(1, self.store.expire_results())
                self.assertEqual('expired', self.store.result_reservation(self.binding).state)

    def save_orchestration(self):
        db = self.store._connection
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] for table in ORCHESTRATION_TABLES}

    def drop_orchestration(self):
        for table in ORCHESTRATION_TABLES:
            self.store._connection.execute(f'DELETE FROM {table}')

    def restore_orchestration(self, saved):
        for table in reversed(ORCHESTRATION_TABLES):
            for row in saved[table]:
                self.store._connection.execute(f'INSERT INTO {table} VALUES({",".join("?" * len(row))})', row)


@requirement
class Spare(RealFixture):
    """One more run of the same fixture, for a case that needs its own store."""

    def runTest(self):
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])


@requirement
class RealOrchestrationEndTests(RealFixture):
    """B2-13, B2-15 through the store's real close, interruption and idle end, both admission paths."""

    def test_the_real_journal_is_the_state_the_predicate_reads(self):
        self.assertEqual('active', self.status_of_orchestration())
        self.assertEqual('recorded', 'recorded' if self.service_record() else None)
        self.assertEqual('service', self.recorder())

    def test_a_really_ended_orchestration_records_nothing_through_either_entry(self):
        for way in (*TERMINAL, 'idle'):
            for entry in ('service', 'primary'):
                with self.subTest(way=way, entry=entry):
                    fresh = Spare()
                    fresh.setUp()
                    self.addCleanup(fresh.doCleanups)
                    fresh.end_by(way)
                    self.assertIn(fresh.status_of_orchestration(), TERMINAL)
                    fresh.refused_by(entry, 'history_unrecordable_orchestration_closed')
                    status = service_record.recording_status(fresh.store, fresh.authority())
                    self.assertEqual({'orchestration_closed': 1}, status['unrecordable'])
                    self.assertEqual(0, status['awaiting_record'])

    def test_a_restarted_service_does_not_record_into_an_ended_orchestration(self):
        self.end_orchestration('failed')
        store = self.reopen()
        before = self.writes()
        result = service_record.record_pass(store, self.authority())
        self.assertEqual({'history_unrecordable_orchestration_closed': 1}, result['refused'])
        self.assertEqual(before, self.writes())
        self.assertEqual({'attempted': 0, 'recorded': 0, 'refused': {}, 'already': 0},
                         service_record.record_pass(store, self.authority()))  # visited once, not looped

    def test_recorded_first_then_a_real_close_is_allowed_and_the_primary_still_retrieves(self):
        recorded = self.service_record()
        owner = self.orchestration_owner()
        self.store._orchestration_limits = OrchestrationLimits(1, 8)
        self.assertFalse(owner._unfinished(self.scope(), self.binding.orchestration_handle))  # no account patch
        self.end_orchestration('completed')
        self.assertEqual(recorded, self.primary_record())  # K2 after the real close
        self.assertIsNone(self.service_record())

    def test_a_real_close_at_the_barrier_between_body_io_and_the_transaction_refuses_both_entries(self):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Spare()
                fresh.setUp()
                self.addCleanup(fresh.doCleanups)
                fresh.store._after_body_io = lambda f=fresh: (setattr(f.store, '_after_body_io', None),
                                                              f.end_orchestration('completed'))
                fresh.refused_by(entry, 'history_unrecordable_orchestration_closed')

    def test_a_real_idle_end_at_the_barrier_refuses_both_entries(self):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Spare()
                fresh.setUp()
                self.addCleanup(fresh.doCleanups)
                fresh.store._after_body_io = lambda f=fresh: (setattr(f.store, '_after_body_io', None), f.idle_end())
                fresh.refused_by(entry, 'history_unrecordable_orchestration_closed')

    def test_a_close_that_comes_after_the_record_is_not_blocked_by_it(self):
        self.store._after_body_io = None
        self.service_record()
        self.end_orchestration('completed')
        self.assertEqual('completed', self.status_of_orchestration())
        self.assertEqual('service', self.recorder())


@requirement
class RealOwnershipTests(RealFixture):
    """B2-15: the writer's ownership, really lost: another thread, and the writer closed at the barrier."""

    def test_another_thread_cannot_record_through_either_entry(self):
        for entry in ('service', 'primary'):
            seen = []

            def attempt():
                try:
                    self.record_by(entry)
                except HistoryError as error:
                    seen.append(error.reason_code)
            before = self.writes()
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(['history_wrong_owner'], seen, entry)
            self.assertEqual(before, self.writes())

    def test_a_writer_closed_at_the_barrier_writes_nothing_and_a_successor_records_it(self):
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                fresh = Spare()
                fresh.setUp()
                self.addCleanup(fresh.doCleanups)
                fresh.store._after_body_io = lambda f=fresh: (setattr(f.store, '_after_body_io', None), f.store.close())
                with self.assertRaises(HistoryError):
                    fresh.record_by(entry)
                store = fresh.reopen()
                self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_result_recorders').fetchone()[0])
                self.assertEqual('committed', rc.context_row(store, *fresh.attempt)[0])
                self.assertEqual(1, service_record.record_pass(store, fresh.authority())['recorded'])
                self.assertEqual('service', fresh.recorder())


@requirement
class MissingOrchestrationTests(RealFixture):
    """MED-2: a missing or unverifiable orchestration checkpoint refuses; it is never treated as open."""

    def test_a_missing_orchestration_refuses_the_service_and_the_primary_and_writes_nothing(self):
        for legacy in (True, False):  # the store's own legacy allowance does not make a context-backed run open
            for entry in ('service', 'primary'):
                with self.subTest(legacy=legacy, entry=entry):
                    fresh = Spare()
                    fresh.setUp()
                    self.addCleanup(fresh.doCleanups)
                    fresh.store._allow_legacy_orchestration_reference = legacy
                    fresh.drop_orchestration()
                    fresh.refused_by(entry, 'history_unrecordable_orchestration_closed')

    def test_a_missing_orchestration_refuses_the_association_repair_too(self):
        self.service_record()
        self.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        self.store._allow_legacy_orchestration_reference = True
        self.drop_orchestration()
        before = self.writes()
        with self.assertRaisesRegex(HistoryError, '^history_unrecordable_orchestration_closed$'):
            self.primary_record()  # K3
        self.assertEqual(before, self.writes())

    def test_an_unverifiable_checkpoint_refuses_like_a_closed_orchestration(self):
        self.store._connection.execute('UPDATE project_orchestration_checkpoints SET checkpoint_digest=?', ('0' * 64,))
        for entry in ('service', 'primary'):
            with self.subTest(entry=entry):
                self.refused_by(entry, 'history_unrecordable_orchestration_closed')

    def test_a_missing_orchestration_is_named_by_the_status_and_the_sweep(self):
        self.store._allow_legacy_orchestration_reference = True
        self.drop_orchestration()
        status = service_record.recording_status(self.store, self.authority())
        self.assertEqual({'orchestration_closed': 1}, status['unrecordable'])
        result = service_record.record_pass(self.store, self.authority())
        self.assertEqual({'history_unrecordable_orchestration_closed': 1}, result['refused'])
        self.assertEqual(0, self.counts()[1])

    def test_a_run_with_no_stored_context_still_cannot_be_recorded_by_the_service(self):
        self.store._connection.execute('DELETE FROM project_result_contexts')
        self.store._allow_legacy_orchestration_reference = True
        self.drop_orchestration()
        with self.assertRaisesRegex(HistoryError, '^history_unrecordable_legacy_no_context$'):
            self.service_record()

    def test_a_permanent_refusal_is_tried_again_when_the_state_it_depends_on_changes(self):
        saved = self.save_orchestration()
        self.store._allow_legacy_orchestration_reference = True
        self.drop_orchestration()
        first = service_record.record_pass(self.store, self.authority())
        self.assertEqual({'history_unrecordable_orchestration_closed': 1}, first['refused'])
        self.assertEqual(0, service_record.record_pass(self.store, self.authority())['attempted'])  # not retried as is
        self.restore_orchestration(saved)  # the relevant state changes
        again = service_record.record_pass(self.store, self.authority())
        self.assertEqual(1, again['recorded'])
        self.assertEqual('service', self.recorder())


@requirement
class AuthorityLowsTests(RealFixture):
    """The review's LOW findings of the production part, each by its behaviour."""

    def test_L1_the_service_authority_needs_the_writers_runtime(self):
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            ServiceRecordAuthority(service=True)
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            ServiceRecordAuthority(runtime_digest=None, service=True)
        self.assertEqual(self.binding.runtime_digest, self.authority().runtime_digest)

    def test_L2_the_race_loser_compares_what_the_normal_path_compares(self):
        other = self.copy_store()
        raced = []

        def between():
            other._after_body_io = None
            raced.append(True)
            self.primary_record(other)
            # the winner's receipt is altered after it committed: the loser must not take it as its own
            other._connection.execute(
                "UPDATE records SET content=replace(CAST(content AS TEXT),'\"observed_at_ms\":','\"observed_at_ms\":1,\"was\":')"
                " WHERE record_id=?", (self.receipt,))
        other._after_body_io = between
        with self.assertRaises(HistoryError):
            self.service_record(other)
        self.assertEqual([True], raced)
        other.close()

    def test_L3_the_eligibility_time_is_sampled_once(self):
        log = []
        original_now = self.store._result_now
        original_check = ServiceRecordAuthority.check

        def now():
            log.append('now')
            return original_now()

        def check(authority, *args, **kwargs):
            outcome = original_check(authority, *args, **kwargs)
            log.append('check')
            return outcome
        with mock.patch.object(self.store, '_result_now', side_effect=now), \
                mock.patch.object(ServiceRecordAuthority, 'check', autospec=True, side_effect=check):
            self.service_record()
        self.assertEqual(3, log.count('check'))  # the predicate ran before the body I/O, in the transaction, and last
        self.assertEqual(['now', 'check', 'now', 'check'], log[:4])  # each early decision sampled the clock once
        self.assertEqual('check', log[-1])
        last = len(log) - 1 - log[::-1].index('check')
        self.assertNotIn('now', log[last + 1:])  # nothing samples the clock again once the last decision is made

    def test_L5_the_orchestration_prefilter_is_replaced_by_a_documented_permanent_set(self):
        self.assertIn('permanent', service_record.record_pass.__doc__)
        with open(service_record.__file__) as module:
            source = ' '.join(module.read().split())
        self.assertIn('replaces the design 4.6 orchestration prefilter', source)

    def test_L9_every_new_refusal_has_plain_guidance(self):
        for reason in (*service_record.UNRECORDABLE.values(),):
            with self.subTest(reason=reason):
                self.assertIn(reason, refusal_steps.EXACT)
                self.assertTrue(refusal_steps.EXACT[reason].strip())

    def test_L10_the_status_report_uses_the_writers_runtime(self):
        from codex_agents import project_orchestration_report as report
        snapshot = {'status': 'active', 'started_at_ms': 1, 'ended_at_ms': None, 'observed_at_ms': 1, 'sequence': 1,
                    'goal_tokens_used': None, 'goal_time_used_seconds': None}
        with mock.patch.object(report, '_orchestration',
                               return_value=(self.binding.orchestration_handle, [snapshot], 1)):
            same = report.orchestration_report(self.store, 'orc:' + 'a' * 64, runtime_digest=self.binding.runtime_digest)
            other = report.orchestration_report(self.store, 'orc:' + 'a' * 64, runtime_digest=OTHER_RUNTIME)
        self.assertEqual(1, same['recording']['awaiting_record'])
        self.assertEqual({'other_runtime': 1}, other['recording']['unrecordable'])

    def test_unsupported_host_class_is_named_as_a_host_class_refusal(self):
        self.store._connection.execute("UPDATE project_result_contexts SET host_class='weird-host-v1'")
        with self.orchestration(client='weird-host-v1'):
            with self.assertRaisesRegex(HistoryError, '^history_unrecordable_host_class$'):
                self.authority().check(self.store, self.binding, 'K1', reservation=self.store.result_reservation(self.binding))


# ---------------------------------------------------------------------------------------------------------------------
# K1 / K2 / K3 across the run's states, altered rows, receipt versions, the run of another body, a failed write
# ---------------------------------------------------------------------------------------------------------------------

from tests.test_bridge import result  # noqa: E402
from tests.test_project_result_usage import COUNTS  # noqa: E402
from codex_agents.host_result_contracts import HostResultReference  # noqa: E402
from codex_agents.learning_contracts import canonical_json  # noqa: E402
from codex_agents.result_contracts import CodexUsageObservation, ResultObservation  # noqa: E402


StatedFixture = RealFixture


@requirement
class StatedRunTests(StatedFixture):
    """B2-4, B2-16: the first admission (K1), the retrieval (K2) and the association repair (K3) of a run that is
    open, closed, expired or retired, by exact reason, and what each writes."""

    K1 = {'open': None, 'closed': 'history_unrecordable_orchestration_closed',
          'expired': 'history_result_expired', 'retired': 'history_result_expired'}

    def spare(self):
        spare = Spare()
        spare.setUp()
        self.addCleanup(spare.doCleanups)
        return spare

    def test_K1_each_state_by_either_entry(self):
        for state, reason in self.K1.items():
            for entry in ('service', 'primary'):
                with self.subTest(state=state, entry=entry):
                    run = self.spare()
                    run.put(state)
                    if reason is None:
                        self.assertTrue(run.record_by(entry))
                        self.assertEqual(entry, run.recorder())
                    else:
                        with self.assertRaisesRegex(HistoryError, f'^{reason}$'):
                            run.record_by(entry)
                        self.assertIsNone(run.recorder())
                        self.assertEqual(0, run.counts()[1])

    def test_K2_a_recorded_run_is_retrieved_in_every_state_by_the_primary_and_never_by_the_service(self):
        for state in ('open', 'closed', 'expired'):
            with self.subTest(state=state):
                run = self.spare()
                recorded = run.service_record()
                run.put(state)
                before = run.counts()
                self.assertEqual(recorded, run.primary_record())
                self.assertIsNone(run.service_record())
                self.assertEqual(before, run.counts())
                self.assertEqual('service', run.recorder())

    def test_K2_after_the_retirement_the_attribution_stays_and_the_status_still_says_who(self):
        run = self.spare()
        run.service_record()
        run.put('retired')
        self.assertEqual('service', run.recorder())
        status = service_record.recording_status(run.store, run.authority())  # lists held runs only: a retired one is
        self.assertEqual(([], 0, {}), (status['recorded'], status['awaiting_record'], status['unrecordable']))  # not
        self.assertEqual(1, run.store._connection.execute(  # mislabelled as waiting; its attribution is the row
            'SELECT COUNT(*) FROM project_result_recorders WHERE recorded_by=?', ('service',)).fetchone()[0])
        with self.assertRaisesRegex(HistoryError, '^history_result_expired$'):
            run.service_record()

    def test_K3_the_association_repair_is_a_mutation_and_follows_the_same_states(self):
        expected = {'open': None, 'closed': 'history_unrecordable_orchestration_closed',
                    'expired': 'history_result_expired', 'retired': 'history_result_expired'}
        for state, reason in expected.items():
            for entry in ('service', 'primary'):
                with self.subTest(state=state, entry=entry):
                    run = self.spare()
                    run.service_record()
                    run.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
                    run.put(state)
                    if reason is None:
                        run.record_by(entry)
                        self.assertEqual('recorded', rc.context_row(run.store, *run.attempt)[0])
                    else:
                        with self.assertRaisesRegex(HistoryError, f'^{reason}$'):
                            run.record_by(entry)
                        self.assertEqual('expired' if state == 'retired' else 'committed',
                                         rc.context_row(run.store, *run.attempt)[0])
                    self.assertEqual('service', run.recorder())  # the repair never writes the recorder


@requirement
class AlteredRowsTests(StatedFixture):
    """B2-3, B2-7: an altered terminal, grant, context or body is refused with its exact reason, writes nothing and is
    counted by the sweep as an integrity refusal."""

    def altered(self, change, service_reason, primary_reason=None):
        for entry, reason in (('service', service_reason), ('primary', primary_reason or service_reason)):
            with self.subTest(entry=entry):
                run = Spare()
                run.setUp()
                self.addCleanup(run.doCleanups)
                change(run)
                before = run.writes()
                with self.assertRaisesRegex(HistoryError, f'^{reason}$'):
                    run.record_by(entry)
                self.assertEqual(before, run.writes())
                self.assertIsNone(run.recorder())
                swept = service_record.record_pass(run.store, run.authority())
                self.assertEqual(1, sum(swept['refused'].values()), swept)
                self.assertEqual(before, run.writes())

    def test_an_altered_result_body(self):
        def change(run):
            path = sorted(run.directory.glob('.result-*.json'))[0]
            path.write_bytes(b'{"tampered": true}')
        self.altered(change, 'history_result_unavailable')

    def test_an_altered_terminal_delivery(self):
        self.altered(lambda run: run.store._connection.execute(
            'UPDATE terminal_outbox SET record_content_digest=?', ('0' * 64,)), 'history_incompatible')

    def test_an_altered_consumed_grant(self):
        self.altered(lambda run: run.store._connection.execute(
            'UPDATE project_execution_consumptions SET registration_digest=?', ('0' * 64,)), 'history_conflict')

    def test_an_altered_stored_context(self):
        def change(run):
            path = sorted(run.directory.glob('.context-*'))[0]
            path.write_bytes(path.read_bytes() + b' ')
        self.altered(change, 'history_conflict')

    def test_an_altered_reservation_digest(self):
        self.altered(lambda run: run.store._connection.execute(
            'UPDATE result_reservations SET prepared_digest=?', ('0' * 64,)), 'history_conflict')

    def test_the_integrity_refusal_is_one_health_signal_per_run_and_the_sweep_does_not_loop(self):
        run = Spare()
        run.setUp()
        self.addCleanup(run.doCleanups)
        run.store._connection.execute('UPDATE terminal_outbox SET record_content_digest=?', ('0' * 64,))
        notes = service_record.RecordingNotes()
        for _ in range(3):
            service_record.record_pass(run.store, run.authority(), notes=notes)
        self.assertEqual(1, len(service_record.health_signals(run.store)))
        self.assertTrue(notes.snapshot()[1][0].startswith('integrity:'))


class UsageFixture(StatedFixture):
    """The same run published with the client's usage: receipt version 2."""

    def publish(self):
        self.body = canonical_json({**result(), 'summary': 'PRIVATE-STORED-CONTEXT-CANARY'})
        self.delivery = self.store.publish_result(
            self.binding, self.assigned, self.body,
            ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None),
            client_usage=CodexUsageObservation(self.assigned['model'], dict(COUNTS)))
        self.reference = HostResultReference(self.binding, self.delivery.record_id,
                                             self.delivery.record_content_digest, self.delivery.result_digest)


@requirement
class ReceiptVersionTests(UsageFixture):
    """B2-17, B2-2: both receipt versions, the expected comparison on a known receipt, matching and not."""

    def receipt_of(self, run):
        return json.loads(run.store.record(run.receipt).content)

    def test_version_2_is_written_by_either_entry_and_is_byte_identical(self):
        other = self.copy_store()
        by_service = self.service_record()
        by_primary = self.primary_record(other)
        self.assertEqual(2, by_service['admission']['schema_version'])
        self.assertEqual(by_service['admission'], by_primary['admission'])
        for name in ('event_commit', 'outcome_commit', 'admission_commit'):
            self.assertEqual(by_service[name]['content_digest'], by_primary[name]['content_digest'], name)
        self.assertEqual(self.receipt_of(self), json.loads(other.record(self.receipt).content))
        self.assertTrue({'usage_record_id', 'usage_content_digest'} <= set(self.receipt_of(self)))
        other.close()

    def test_a_matching_known_receipt_is_retrieved_and_an_altered_one_is_a_conflict_for_both_entries(self):
        recorded = self.service_record()
        self.assertEqual(recorded, self.primary_record())  # matching expected values
        self.assertIsNone(self.service_record())
        self.store._connection.execute(
            "UPDATE records SET content=replace(CAST(content AS TEXT),'\"usage_content_digest\":\"','\"usage_content_digest\":\"0')"
            " WHERE record_id=?", (self.receipt,))
        self.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        before = self.counts()
        for entry in ('service', 'primary'):
            with self.assertRaises(HistoryError):
                self.record_by(entry)
        self.assertEqual(before, self.counts())

    def test_the_recorder_of_a_version_2_receipt_is_immutable_across_a_primary_retrieval(self):
        self.service_record()
        self.primary_record()
        self.assertEqual('service', self.recorder())

    def test_the_primary_first_winner_stays_immutable_through_a_repair(self):
        self.primary_record()
        self.store._connection.execute("UPDATE project_result_contexts SET state='committed',outcome_digest=NULL")
        before = self.store._connection.execute('SELECT * FROM project_result_recorders').fetchall()
        self.primary_record()
        self.assertEqual(before, self.store._connection.execute('SELECT * FROM project_result_recorders').fetchall())
        self.assertEqual('primary', self.recorder())


class OtherBodyFixture(StatedFixture):
    """The same run published with another body: a second genuine publication of the attempt, for swaps."""

    def publish(self):
        self.body = canonical_json({**result(), 'summary': 'ANOTHER-RUNS-BODY'})
        self.delivery = self.store.publish_result(
            self.binding, self.assigned, self.body,
            ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None))
        self.reference = HostResultReference(self.binding, self.delivery.record_id,
                                             self.delivery.record_content_digest, self.delivery.result_digest)


class OtherBodyRun(OtherBodyFixture):
    """A fixture only (it has no test of its own): `SwappedRunTests` builds it for the second publication."""


@requirement
class SwappedRunTests(StatedFixture):
    """B2-7: a second genuine publication's body and reference swapped in, and another project's binding."""

    def other(self):
        run = OtherBodyRun()
        run.setUp()
        self.addCleanup(run.doCleanups)
        return run

    def test_another_publications_reference_is_refused_and_writes_nothing(self):
        run = self.other()
        self.assertNotEqual(self.reference.result_digest, run.reference.result_digest)
        swapped = HostResultReference(self.binding, run.reference.terminal_record_id,
                                      run.reference.terminal_content_digest, run.reference.result_digest)
        original = self.store.stored_result_context(self.binding, host_class=self.host_class)
        before = self.writes()
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.store.admit_host_result(swapped, assignment=original.assignment, task=original.task,
                                         route=original.route, client_capability=original.client_capability,
                                         before_commit=lambda: None)
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.store._admit_host_result(swapped, assignment=original.assignment, task=original.task,
                                          route=original.route, client_capability=original.client_capability,
                                          before_commit=None, final_check=self.authority(), recorder='service')
        self.assertEqual(before, self.writes())

    def test_another_publications_body_in_place_of_the_stored_one_is_refused_for_both_entries(self):
        run = self.other()
        mine = sorted(self.directory.glob('.result-*.json'))[0]
        theirs = sorted(run.directory.glob('.result-*.json'))[0]
        mine.write_bytes(theirs.read_bytes())
        before = self.writes()
        for entry in ('service', 'primary'):
            with self.assertRaises(HistoryError):
                self.record_by(entry)
        self.assertEqual(before, self.writes())

    def test_a_binding_of_another_project_cannot_read_count_or_admit_while_the_reservation_is_live(self):
        foreign = AttemptBinding(**{**self.binding.as_data(), 'project_id': 'prj_' + '1' * 32})
        before = self.writes()
        for call in (lambda: self.store.read_result(foreign),
                     lambda: self.store.result_reservation(foreign),
                     lambda: self.store.admit_service_result(foreign, final_check=self.authority()),
                     lambda: self.store.admit_host_result(
                         HostResultReference(foreign, self.reference.terminal_record_id,
                                             self.reference.terminal_content_digest, self.reference.result_digest),
                         assignment=self.assigned, task=self.task, route=self.route,
                         client_capability=self.capability, before_commit=lambda: None)):
            with self.assertRaises(HistoryError) as caught:
                call()
            self.assertEqual('history_invalid_input', caught.exception.reason_code)
        self.assertEqual(before, self.writes())
        self.assertGreater(self.due(), self.fixture.base.stamp)  # the reservation was live throughout


@requirement
class FailedWriteTests(StatedFixture):
    """B2-9: real appends happen before the injected failure; the whole admission rolls back, and a successor records."""

    def test_two_real_appends_then_a_failure_leave_nothing_and_the_next_pass_records(self):
        calls = []
        original = project_history.ProjectHistory._append_locked

        def append(store, *args, **kwargs):
            calls.append(args[0].get('record_type') if isinstance(args[0], dict) else None)
            if len(calls) == 3:
                raise OSError('cut')
            return original(store, *args, **kwargs)
        before = self.writes()
        with mock.patch.object(project_history.ProjectHistory, '_append_locked', autospec=True, side_effect=append):
            with self.assertRaisesRegex(HistoryError, '^history_unavailable$'):
                self.service_record()
        self.assertEqual(3, len(calls))  # the intended failure point was reached, after two real appends
        self.assertEqual(before, self.writes())
        store = self.reopen()
        self.assertEqual(before, self.counts())  # a new writer reads the same counts as before the failed write
        self.assertEqual(1, service_record.record_pass(store, self.authority())['recorded'])
        self.assertEqual('service', self.recorder())
        self.assertEqual((1, 1, 1), tuple(store._connection.execute(
            'SELECT COUNT(*) FROM records WHERE record_type=?', (kind,)).fetchone()[0]
            for kind in ('event', 'outcome', 'host_result_admission')))
