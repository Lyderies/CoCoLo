"""Slice D1 (RS-D D5): the record context lives in the project store, not in the service's memory.

DESIGN-0.4.0-RS-D.md draft 2 §2-§4, draft 3 §3 and §6 (rows O1-O5, A1-A3, K3, K4, M1) and
DESIGN-0.4.0-RS-BD-shared.md SH-3. The context a record needs (assignment, task, route, client capability, host
class) is stored with the lease, verified at host open, bound at bind, committed with the result and associated with
its outcome at admission; every later reader (admission, the run's own result read, worker input, routing evidence,
verifier open, QA) reads it from the store, so a restarted service of the same runtime finishes the run.
Integration stage: real stores in temporary folders, the account fixtures of the store tests.
"""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_storage as claim_storage
from tests import test_claim_denial_recovery as denials
from tests import test_result_leases as leases
from tests import test_run_provenance as provenance
from tests.learning_support import assignment
from tests.test_bridge import PARENT_GRANT, result
from tests.test_project_history import binding

from codex_agents import project_holds_schema, project_result_contexts
from codex_agents.history_contracts import HistoryError
from codex_agents.hosts import CLAUDE_HOST_CLIENT as CLAUDE, CODEX_HOST_CLIENT as CODEX
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultContext, HostResultReference, RecordContext
from codex_agents.learning_contracts import canonical_json
from codex_agents.open_scope_contracts import open_inputs_digest
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity, ResultObservation

DAY_MS = 86_400_000


def rows(store, sql, *arguments):
    return [tuple(row) for row in store._connection.execute(sql, arguments)]


def context_row(store, run_handle, attempt):
    row = store._connection.execute(
        'SELECT state,context_size,host_class,claim_id,outcome_digest,retained_until_ms FROM project_result_contexts '
        'WHERE run_handle=? AND attempt=?', (run_handle, attempt)).fetchone()
    return None if row is None else tuple(row)


def custody(store, run_handle, attempt):
    row = store._connection.execute(
        "SELECT state,release_kind,evidence FROM project_holds WHERE kind='result_custody' AND identity=?",
        (f'{run_handle}:{attempt}',)).fetchone()
    return None if row is None else tuple(row)


def context_files(directory):
    return sorted(path.name for path in Path(directory).glob('.context-*'))


def live_context_bytes(store):
    return store._connection.execute("SELECT COALESCE(SUM(context_size),0) FROM project_result_contexts "
                                     "WHERE state NOT IN ('ended','expired')").fetchone()[0]


def simple_context(number=1, **changes):
    task = {'id': f'task-{number}', 'family': 'research'}
    route = {'provider': 'codex', 'model': 'gpt-6.1-sol'}
    values = dict(task=task, route=route, client_capability={'client_version': 'bridge-v1'}, host_class=CODEX,
                  inputs_digest=open_inputs_digest(assignment(), task, route, PARENT_GRANT))
    values.update(changes)
    return RecordContext(**values)


class LeaseContextTests(unittest.TestCase):
    """O1, O2, O5: the context is written with the lease, and goes with an unfinished lease."""

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.capacity = ResultCapacity(16, 8 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen(result_capacity=self.fixture.capacity)

    @property
    def store(self):
        return self.fixture.store

    def reserve(self, number=1, context=None):
        return self.store.reserve_result_lease(leases.lease_binding(number), assignment(),
                                               record_context=context or simple_context(number))

    def attempt(self, number):
        target = binding(number)
        return target.run_handle, target.attempt

    def test_D1_the_context_is_stored_with_the_lease_in_its_own_transaction(self):
        self.reserve(1)
        state, size, host_class, claim, outcome, _ = context_row(self.store, *self.attempt(1))
        self.assertEqual(('leased', CODEX, None, None), (state, host_class, claim, outcome))
        self.assertGreater(size, 0)
        self.assertEqual(('held', None, None), custody(self.store, *self.attempt(1)))
        self.assertEqual(1, len(context_files(self.fixture.directory)))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))
        # The file holds the inputs, never SQLite; it is the owner's alone.
        (name,) = context_files(self.fixture.directory)
        info = os.lstat(self.fixture.directory / name)
        self.assertEqual((0o600, 1, size), (info.st_mode & 0o777, info.st_nlink, info.st_size))
        self.assertNotIn(b'task-1', b''.join(bytes(value) if type(value) is bytes else str(value).encode()
                                             for row in rows(self.store, 'SELECT * FROM project_result_contexts')
                                             for value in row))

    def test_O1_a_context_left_by_a_lease_transaction_that_never_committed_is_removed_at_the_next_open(self):
        self.reserve(1)
        self.store.close()
        # The stop came after the file was written and before the lease transaction committed: no row of it exists.
        with closing(sqlite3.connect(self.fixture.directory / 'history.sqlite3')) as db:
            db.execute("DELETE FROM project_holds WHERE identity=?", ('%s:%d' % self.attempt(1),))
            db.execute('DELETE FROM project_result_contexts')
            db.execute('DELETE FROM project_cleanup_reservations')
            db.execute('DELETE FROM result_leases')
            db.commit()
        self.assertEqual(1, len(context_files(self.fixture.directory)))
        self.fixture.store = self.fixture.open(result_capacity=self.fixture.capacity)
        self.assertEqual([], context_files(self.fixture.directory))
        self.assertEqual([], rows(self.store, 'SELECT * FROM project_result_contexts'))
        self.assertEqual((0, 0, 0), self.store._result_counts())
        self.assertEqual(0, live_context_bytes(self.store))

    def test_O2_a_lease_left_unarmed_by_a_stopped_service_ends_its_context_at_the_next_start(self):
        self.reserve(1)
        self.base.reopen(result_capacity=self.fixture.capacity)
        self.assertEqual(1, self.store.release_earlier_unarmed_leases(set()))
        self.assertEqual(('released', 'withdrawn_unsent'), rows(
            self.store, "SELECT state,release_kind FROM project_holds WHERE kind='lease'")[0])
        state, release_kind, evidence = custody(self.store, *self.attempt(1))
        self.assertEqual(('released', 'lost_with_reservation'), (state, release_kind))
        self.assertEqual('withdrawn_unsent', json.loads(evidence)['cause'])
        self.assertEqual('ended', context_row(self.store, *self.attempt(1))[0])
        self.assertEqual([], context_files(self.fixture.directory))
        self.assertEqual(0, live_context_bytes(self.store))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))
        self.base.reopen(result_capacity=self.fixture.capacity)  # the validators pass at the next open too

    def test_O2_an_unsent_or_expired_lease_ends_its_context_with_it(self):
        self.reserve(1)
        self.reserve(2)
        self.store.release_unsent_result_lease(leases.lease_binding(1))
        self.fixture.now = 61_000
        self.assertEqual(1, self.store.expire_result_leases())
        for number in (1, 2):
            with self.subTest(number=number):
                self.assertEqual('ended', context_row(self.store, *self.attempt(number))[0])
                self.assertEqual('lost_with_reservation', custody(self.store, *self.attempt(number))[1])
        self.assertEqual([], context_files(self.fixture.directory))

    def test_O5_a_context_at_its_size_limit_is_kept_and_one_byte_over_is_refused_before_the_arm(self):
        self.reserve(1)
        size = context_row(self.store, *self.attempt(1))[1]
        for limit, refused in ((size + 1, False), (size, False), (size - 1, True)):
            number = 2 if refused else (3 if limit == size else 4)
            with self.subTest(limit=limit), mock.patch.object(project_result_contexts, 'MAX_RECORD_CONTEXT_BYTES', limit):
                if refused:
                    with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                        self.reserve(number)
                    self.assertIsNone(context_row(self.store, *self.attempt(number)))
                    with self.assertRaisesRegex(HistoryError, 'history_result_not_reserved'):
                        self.store.result_lease(leases.lease_binding(number))
                else:
                    self.assertEqual('reserved', self.reserve(number).state)
        self.assertEqual(3, len(context_files(self.fixture.directory)))

    def test_O5_the_live_context_budget_admits_exactly_what_fits_and_a_refusal_leaves_nothing(self):
        self.reserve(1)
        size = context_row(self.store, *self.attempt(1))[1]
        with mock.patch.object(project_result_contexts, 'MAX_LIVE_CONTEXT_BYTES', 2 * size):
            self.reserve(2)  # equal to the budget: fits
            self.assertEqual(2 * size, live_context_bytes(self.store))
            # Two competing for no room left: the one writer serializes them and both are refused, charging nothing.
            for number in (3, 4):
                with self.subTest(number=number), self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
                    self.reserve(number)
                self.assertIsNone(context_row(self.store, *self.attempt(number)))
            self.assertEqual(2, len(context_files(self.fixture.directory)))
            # A retry of the same lease is not a second charge.
            self.reserve(1)
            self.assertEqual(2 * size, live_context_bytes(self.store))
            # Ending one frees exactly its bytes; of the two competing for that place, exactly one gets it.
            self.store.release_unsent_result_lease(leases.lease_binding(1))
            self.assertEqual(size, live_context_bytes(self.store))
            self.reserve(3)
            with self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
                self.reserve(4)
            self.assertEqual(2 * size, live_context_bytes(self.store))
            self.assertEqual(2, len(context_files(self.fixture.directory)))

    def test_O5_a_changed_context_for_a_known_lease_is_a_conflict_and_writes_nothing(self):
        self.reserve(1)
        before = context_files(self.fixture.directory), context_row(self.store, *self.attempt(1))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.reserve(1, simple_context(1, host_class=CLAUDE))
        self.assertEqual(before, (context_files(self.fixture.directory), context_row(self.store, *self.attempt(1))))

    def test_D1_a_host_class_outside_the_known_ones_or_a_malformed_context_is_refused(self):
        for changes in ({'host_class': 'owner'}, {'inputs_digest': 'x'}, {'task': []}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                self.reserve(1, simple_context(1, **changes))
        self.assertEqual([], context_files(self.fixture.directory))


def seven_day_store(fixture):
    """The account fixtures' project store, with the service's seven days of retention."""
    return ProjectHistory(fixture.directory, fixture.project, result_key=fixture.key,
                          allow_legacy_orchestration_reference=True,
                          result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * DAY_MS),
                          result_clock_ms=lambda: fixture.base.stamp)


class RecordedContextFixture(unittest.TestCase):
    """One real run through the account fixtures: its lease stored with its context, then host open, bind,
    consumption, publication and admission. Retention is seven days (the service's)."""

    host_class = CODEX

    def setUp(self):
        owner = self

        def arm(fixture):
            source = fixture.base.fixture
            owner.task, owner.route = source.fixture.task(), source.route
            owner.capability = source.fixture.clients['bridge-v1']
            owner.record_context = RecordContext(owner.task, owner.route, owner.capability, owner.host_class,
                                                 open_inputs_digest(fixture.assigned, owner.task, owner.route,
                                                                    PARENT_GRANT))
            fixture.store.reserve_result_lease(fixture.scope, fixture.assigned, record_context=owner.record_context)
            fixture.store.arm_result_lease(fixture.scope)
            fixture.evidence = fixture.store.attest_result_lease(fixture.scope)
            return fixture.store.result_lease(fixture.scope)

        self.base = provenance.RunProvenanceTests(methodName='runTest')
        with mock.patch.object(claim_storage.AccountClaimStorageTests, 'open_history', seven_day_store), \
                mock.patch.object(claim_storage.AccountClaimStorageTests, 'arm', arm):
            self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture  # AccountClaimStorageTests
        self.binding = self.base.base.binding
        self.assigned = self.fixture.assigned
        self.directory = self.fixture.directory

    @property
    def store(self):
        return self.fixture.store

    @property
    def attempt(self):
        return self.binding.run_handle, self.binding.attempt

    def reopen(self):
        self.store.close()
        self.fixture.store = seven_day_store(self.fixture)
        return self.store

    def advance(self, milliseconds):
        self.fixture.base.stamp += milliseconds

    def open_and_consume(self):
        self.base.managed_project()  # the host open, checked against the stored context
        self.base.base.consume(self.base.grant, provenance=self.base.proof())

    def publish(self):
        self.body = canonical_json({**result(), 'summary': 'PRIVATE-STORED-CONTEXT-CANARY'})
        self.delivery = self.store.publish_result(self.binding, self.assigned, self.body,
            ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None))
        self.reference = HostResultReference(self.binding, self.delivery.record_id,
                                             self.delivery.record_content_digest, self.delivery.result_digest)

    def admit_from_store(self, store=None):
        """Admission as the service does it now: the inputs are the stored context, read back and checked."""
        store = store or self.store
        original = store.stored_result_context(self.binding, host_class=self.host_class)
        return store.admit_host_result(self.reference, assignment=original.assignment, task=original.task,
                                       route=original.route, client_capability=original.client_capability,
                                       before_commit=lambda: None)

    def recorded(self):
        self.open_and_consume()
        self.publish()
        self.admitted = self.admit_from_store()
        self.digest = self.admitted['admission']['host_outcome_digest']
        return self.digest

    def context_file(self):
        (name,) = context_files(self.directory)
        return self.directory / name

    def rewrite_context(self, **changes):
        path = self.context_file()
        value = json.loads(path.read_bytes())
        value.update(changes)
        path.write_bytes(canonical_json(value))

    def expected(self):
        return HostResultContext(self.binding, self.assigned, self.task, self.route, self.capability)


class StoredContextTests(RecordedContextFixture):
    """O4, A1-A3, K3, K4: verified at host open, bound at bind, and read back by every consumer."""

    def test_D1_the_context_moves_with_the_run_and_stays_after_a_restart(self):
        self.assertEqual('bound', context_row(self.store, *self.attempt)[0])  # this fixture binds before its open
        self.assertEqual(self.binding.claim_id, context_row(self.store, *self.attempt)[3])
        self.open_and_consume()
        self.publish()
        state, _, host_class, claim_id, outcome, retained = context_row(self.store, *self.attempt)
        self.assertEqual(('committed', CODEX, self.binding.claim_id, None), (state, host_class, claim_id, outcome))
        reservation = self.store.result_reservation(self.binding)
        self.assertEqual(reservation.retained_until_ms, retained)
        self.assertEqual(('held', None, None), custody(self.store, *self.attempt))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, self.fixture.key))

    def test_O4_stored_inputs_changed_before_the_host_open_refuse_it_and_no_claim_is_sent(self):
        self.rewrite_context(task={**self.task, 'objective': 'Not what the run was given'})
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.base.managed_project()
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_O4_inputs_at_the_host_open_that_differ_from_the_stored_ones_are_refused(self):
        self.base.managed_project(record_host_open=False)
        request = OpenProvenanceInput(self.fixture.scope.run_handle, self.assigned['assignment_id'])
        proof = self.base.base.ledger._attest_open_provenance(
            self.fixture.project, runtime_digest=self.fixture.scope.runtime_digest, caller_scope='a' * 64,
            request=request, storage_binding=self.fixture.scope, storage_evidence=self.fixture.evidence,
            work_limits=self.base.limits)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.record_host_open(self.fixture.scope, request, proof, assignment=self.assigned,
                                        task={**self.task, 'objective': 'Another'}, route=self.route,
                                        before_commit=lambda: None)
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_A1_a_restarted_service_reads_the_stored_context_and_records_the_result(self):
        self.open_and_consume()
        self.publish()
        store = self.reopen()  # a successor of the same runtime: nothing of the first is in memory
        self.assertEqual(self.expected(), store.stored_result_context(self.binding, host_class=CODEX))
        admitted = self.admit_from_store(store)
        digest = admitted['admission']['host_outcome_digest']
        store = self.reopen()
        self.assertEqual(self.expected(), store.stored_worker_context(digest, host_class=CODEX))
        self.assertEqual(self.expected(), store.stored_worker_context(
            digest, host_class=CODEX, orchestration_handle=self.binding.orchestration_handle))
        self.assertEqual(self.expected(), store.stored_result_context(self.binding, host_class=CODEX, recorded=True))
        self.assertEqual(self.body, store.read_result(self.binding))

    def test_A1_the_capability_stored_with_the_lease_is_the_one_checked_even_if_the_installed_one_changed(self):
        self.recorded()
        stored = self.reopen().stored_result_context(self.binding, host_class=CODEX)
        self.assertEqual(self.capability, stored.client_capability)

    def test_A1_the_result_reference_is_found_from_the_attempt_and_its_inputs_alone(self):
        self.open_and_consume()
        self.publish()
        store = self.reopen()
        found = store.stored_result_reference(self.binding.run_handle, self.binding.attempt, host_class=CODEX,
                                              inputs_digest=self.record_context.inputs_digest)
        self.assertEqual({'binding': self.binding.as_data(), 'delivery': {
            'record_id': self.delivery.record_id, 'record_content_digest': self.delivery.record_content_digest,
            'result_digest': self.delivery.result_digest}}, found)
        for changes in ({'host_class': CLAUDE}, {'inputs_digest': 'f' * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                store.stored_result_reference(self.binding.run_handle, self.binding.attempt,
                                              **{'host_class': CODEX,
                                                 'inputs_digest': self.record_context.inputs_digest, **changes})

    def test_A1_an_attempt_that_was_never_opened_is_not_found_while_a_wrong_caller_is_still_not_authorized(self):
        # O0d R1: a missing dispatch is named plainly; only a caller that is not allowed is "not authorized".
        self.open_and_consume()
        self.publish()
        store = self.reopen()
        inputs = self.record_context.inputs_digest
        with self.assertRaisesRegex(HistoryError, 'history_not_found'):
            store.stored_result_reference('hrn_' + 'e' * 48, self.binding.attempt, host_class=CODEX,
                                          inputs_digest=inputs)
        with self.assertRaisesRegex(HistoryError, 'history_not_found'):
            store.stored_result_reference(self.binding.run_handle, self.binding.attempt + 1, host_class=CODEX,
                                          inputs_digest=inputs)
        for changes in ({'host_class': CLAUDE}, {'inputs_digest': 'f' * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                store.stored_result_reference(self.binding.run_handle, self.binding.attempt,
                                              **{'host_class': CODEX, 'inputs_digest': inputs, **changes})

    def test_A2_each_wrong_caller_or_changed_input_is_refused_and_changes_nothing(self):
        digest = self.recorded()
        before = rows(self.store, 'SELECT * FROM project_result_contexts'), context_files(self.directory)
        cases = {
            'host class': (lambda: self.store.stored_result_context(self.binding, host_class=CLAUDE),
                           'history_not_authorized'),
            'worker host class': (lambda: self.store.stored_worker_context(digest, host_class=CLAUDE),
                                  'history_not_authorized'),
            'binding': (lambda: self.store.stored_result_context(replace(self.binding, claim_digest='f' * 64),
                                                                 host_class=CODEX), 'history_not_authorized'),
            'attempt': (lambda: self.store.stored_result_context(replace(self.binding, attempt=2), host_class=CODEX),
                        'history_not_authorized'),
            'orchestration': (lambda: self.store.stored_worker_context(digest, host_class=CODEX,
                                                                       orchestration_handle='orc_' + 'f' * 48),
                              'history_not_authorized'),
            'unknown outcome': (lambda: self.store.stored_worker_context('f' * 64, host_class=CODEX),
                                'history_not_authorized'),
        }
        for name, (call, reason) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(HistoryError, reason):
                call()
        self.assertEqual(before, (rows(self.store, 'SELECT * FROM project_result_contexts'), context_files(self.directory)))

    def test_A2_a_forged_outcome_association_is_refused(self):
        digest = self.recorded()
        self.store._connection.execute('UPDATE project_result_contexts SET outcome_digest=?', ('e' * 64,))
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.store.stored_worker_context(digest, host_class=CODEX)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.stored_worker_context('e' * 64, host_class=CODEX)

    def test_A2_changed_stored_inputs_or_capability_are_refused(self):
        self.recorded()
        original = self.context_file().read_bytes()
        for changes in ({'task': {**self.task, 'objective': 'changed'}}, {'client_capability': {'changed': True}},
                        {'host_class': CLAUDE}):
            with self.subTest(changes=changes):
                self.rewrite_context(**changes)
                for host_class in (CODEX, CLAUDE):
                    with self.assertRaises(HistoryError) as caught:
                        self.store.stored_result_context(self.binding, host_class=host_class)
                    self.assertIn(caught.exception.reason_code, {'history_conflict', 'history_not_authorized'})
                self.context_file().write_bytes(original)
        self.assertEqual(self.expected(), self.store.stored_result_context(self.binding, host_class=CODEX))

    def test_A2_a_context_without_its_consumed_grant_is_refused(self):
        self.base.managed_project()
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            self.store.stored_result_context(self.binding, host_class=CODEX)

    def test_A2_once_retirement_began_the_stored_context_is_no_longer_read(self):
        self.recorded()
        self.store._connection.execute("UPDATE result_reservations SET state='retiring'")
        self.store._connection.execute("UPDATE project_result_contexts SET state='retiring'")
        with self.assertRaisesRegex(HistoryError, 'history_result_expired'):
            self.store.stored_result_context(self.binding, host_class=CODEX)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized|history_result_expired'):
            self.store.stored_worker_context(self.digest, host_class=CODEX)

    def test_A3_admission_writes_the_outcome_association_in_its_own_transaction(self):
        self.open_and_consume()
        self.publish()
        real = ProjectHistory._append_locked
        calls = []

        def stop_on_the_receipt(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise sqlite3.OperationalError('fixture stop before the admission commits')
            return real(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, '_append_locked', stop_on_the_receipt):
            with self.assertRaises(HistoryError):
                self.admit_from_store()
        self.assertEqual(('committed', None), (context_row(self.store, *self.attempt)[0],
                                               context_row(self.store, *self.attempt)[4]))
        first = self.admit_from_store(self.reopen())
        digest = first['admission']['host_outcome_digest']
        self.assertEqual(('recorded', digest), (context_row(self.store, *self.attempt)[0],
                                                context_row(self.store, *self.attempt)[4]))
        # A lost reply: the same admission again is the same identity, and the association is unchanged.
        self.assertEqual(first['admission'], self.admit_from_store(self.reopen())['admission'])
        self.assertEqual(1, self.store._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='host_result_admission'").fetchone()[0])
        self.assertEqual(('recorded', digest), (context_row(self.store, *self.attempt)[0],
                                                context_row(self.store, *self.attempt)[4]))
        # No `run.recorded` exists in the store at all: the lookup needs only the outcome digest.
        self.assertEqual(self.expected(), self.store.stored_worker_context(digest, host_class=CODEX))

    def test_K3_a_recorded_worker_is_found_among_two_hundred_recorded_before_and_after_it(self):
        def recorded_rows(start, count):
            for index in range(start, start + count):
                self.store._connection.execute(
                    "INSERT INTO project_result_contexts VALUES(?,?,1,'recorded',?,10,?,?,?,?,1,2,NULL)",
                    (self.binding.project_id, 'hrn_%048x' % index, '%064x' % index, CODEX, 'brg_%032x' % index,
                     '%064x' % (index + 1_000_000), '%064x' % (index + 2_000_000)))
        recorded_rows(1, 199)  # older workers
        digest = self.recorded()
        recorded_rows(1000, 199)  # newer ones: an eviction by age would have dropped this worker long ago
        self.assertEqual(399, self.store._connection.execute(
            "SELECT COUNT(*) FROM project_result_contexts WHERE state='recorded'").fetchone()[0])
        self.assertEqual(self.expected(), self.store.stored_worker_context(digest, host_class=CODEX))

    def test_K4_a_result_is_recorded_more_than_an_hour_and_six_days_after_release_with_restarts_between(self):
        self.open_and_consume()
        self.publish()
        self.advance(3_600_001)  # past the former one-hour record grace
        first = self.admit_from_store(self.reopen())
        self.advance(6 * DAY_MS)
        again = self.admit_from_store(self.reopen())
        self.assertEqual(first['admission'], again['admission'])
        self.assertEqual('recorded', context_row(self.store, *self.attempt)[0])


class DeniedContextTests(unittest.TestCase):
    """O3: an armed lease keeps its context until the hold that ends it; a verified closure ends both."""

    def setUp(self):
        def arm(fixture):
            source = fixture.base.fixture
            task, route = source.fixture.task(), source.route
            fixture.store.reserve_result_lease(fixture.scope, fixture.assigned, record_context=RecordContext(
                task, route, source.fixture.clients['bridge-v1'], CODEX,
                open_inputs_digest(fixture.assigned, task, route, PARENT_GRANT)))
            fixture.store.arm_result_lease(fixture.scope)
            fixture.evidence = fixture.store.attest_result_lease(fixture.scope)
            return fixture.store.result_lease(fixture.scope)

        self.base = denials.ClaimDenialRecoveryTests(methodName='runTest')
        with mock.patch.object(claim_storage.AccountClaimStorageTests, 'arm', arm):
            self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture

    def test_O3_an_armed_lease_keeps_its_context_until_its_verified_closure_ends_both(self):
        store, scope = self.fixture.store, self.fixture.scope
        attempt = (scope.run_handle, scope.attempt)
        closed = self.base.call()  # the account's verified closure, kept until the lease is settled with it
        # Never earlier: neither a restart's start pass, nor its lease deadline, nor the expiry pass ends it.
        store.close()
        self.fixture.store = store = self.fixture.open_history()
        self.assertEqual(0, store.release_earlier_unarmed_leases(set()))
        self.fixture.base.stamp = store.result_lease(scope).reserved_until_ms + 1
        self.assertEqual(0, store.expire_result_leases())
        self.assertEqual('leased', context_row(store, *attempt)[0])
        self.assertEqual(('held', None, None), custody(store, *attempt))
        self.assertEqual(1, len(context_files(self.fixture.directory)))
        store.settle_denied_result_lease(scope, canonical_json(closed))
        self.assertEqual('ended', context_row(store, *attempt)[0])
        state, release_kind, evidence = custody(store, *attempt)
        self.assertEqual(('released', 'lost_with_reservation', 'denied'),
                         (state, release_kind, json.loads(evidence)['cause']))
        self.assertEqual([], context_files(self.fixture.directory))
        self.assertTrue(project_holds_schema.custody_consistent(store._connection, self.fixture.key))


class LegacyAdoptionTests(unittest.TestCase):
    """M1: the 17 -> 18 step adopts each reservation with no context as `legacy_no_context`; nothing is invented."""

    def setUp(self):
        from tests import test_project_holds_schema as schema
        self.schema = schema.SchemaStepTests(methodName='runTest')
        self.schema.setUp()
        self.addCleanup(self.schema.doCleanups)
        fixture = self.schema.fixture
        fixture.reserve(5)
        fixture.commit(5)
        self.schema.store.retire_result(binding(5))  # an expired tombstone
        fixture.reserve(6)
        fixture.commit(6)
        self.schema.store._connection.execute("UPDATE result_reservations SET state='retiring' WHERE claim_id=?",
                                              (binding(6).claim_id,))
        self.schema.format_17_store()
        self.store = self.schema.upgrade()

    def test_M1_each_adoption_class_is_adopted_as_tabled_and_nothing_is_invented(self):
        adopted = {row[0]: row[1:] for row in rows(
            self.store, 'SELECT run_handle,state,context_digest,context_size,host_class,claim_id,outcome_digest '
                        'FROM project_result_contexts')}
        expected = {number: ('legacy_no_context', None, 0, None, binding(number).claim_id, None)
                    for number in (2, 3, 4, 6)}  # in flight (2), committed (3), prepared (4), retiring (6)
        self.assertEqual({binding(number).run_handle: value for number, value in expected.items()}, adopted)
        self.assertEqual(sorted(f'{binding(number).run_handle}:1' for number in (2, 3, 4, 6)), sorted(
            row[0] for row in rows(self.store, "SELECT identity FROM project_holds WHERE kind='result_custody' "
                                               "AND state='held'")))
        self.assertEqual([], context_files(self.schema.directory))  # no file is made for a context never stored
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))

    def test_M1_a_legacy_result_is_refused_plainly_and_keeps_its_body_to_its_own_deadline(self):
        for host_class in (CODEX, CLAUDE):
            with self.subTest(host_class=host_class), \
                    self.assertRaisesRegex(HistoryError, 'history_worker_context_unavailable'):
                self.store.stored_result_context(binding(3), host_class=host_class)
        self.assertTrue(self.store.read_result(binding(3)))  # the body itself is kept

    def test_M1_a_legacy_row_retiring_at_the_update_is_released_when_its_retirement_finishes(self):
        self.store.expire_results()
        self.assertEqual('expired', context_row(self.store, binding(6).run_handle, 1)[0])
        self.assertEqual(('released', 'retention'), custody(self.store, binding(6).run_handle, 1)[:2])
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))


if __name__ == '__main__':
    unittest.main()
