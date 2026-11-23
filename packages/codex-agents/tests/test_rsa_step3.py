"""Decision 87: outstanding owner signatures, restored decisions and late releases."""
import sqlite3
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_billing_jobs as billing_fixtures
from tests import test_account_cleanup_release as cleanup_fixtures
from tests import test_owned_execution as execution_fixtures
from tests import test_run_holds as hold_fixtures
from test_framework import requirements

from codex_agents import learning_store, signature_execution
from codex_agents.account_api import AccountError
from codex_agents.account_billing_admission import ProbeAdmissionError
from codex_agents.run_holds_schema import check_run_holds_consistency


class OutstandingOwnerSignatureTests(unittest.TestCase):
    def setUp(self):
        self.case = billing_fixtures.AccountBillingJobTests('runTest')
        self.case.setUp()
        self.addCleanup(lambda: self.assertTrue(self.case.doCleanups()))

    @requirements('RSA3-1', why_not_scenario='Canonical scenario: sweep an owner-API reservation while its shared-pool worker is running, with no billing job.')
    def test_RSA3_1_swept_owner_signature_returns_without_fault_and_is_forgotten(self):
        case = self.case
        owner, pool = case.billing._owner, case.claims._pool
        project = case.fixture.project_id
        reservation = owner.reserve(project, case.runtime, 'b' * 64, 'owner-only', case.contexts[project],
                                    before_commit=lambda: True)
        gate, started = threading.Event(), threading.Event()
        execute = signature_execution._execute_signature_job

        def held(*args, **kwargs):
            started.set()
            if not gate.wait(8):
                raise AssertionError('owner signature gate was not released')
            return execute(*args, **kwargs)

        self.addCleanup(gate.set)
        with mock.patch.object(signature_execution, '_execute_signature_job', side_effect=held):
            handle = owner.prepare(reservation)
            case.claims.drive()
            self.assertTrue(started.wait(2))
            generation = case.ledger._connection.execute(
                "SELECT generation FROM run_holds WHERE kind='probe'").fetchone()[0]
            self.assertEqual({}, case.billing._jobs)  # prepare() has no billing ticket
            self.assertEqual({}, case.claims._signatures)
            with mock.patch.object(case.ledger, '_clock_ms', return_value=reservation.deadline + 1), \
                    mock.patch.object(case.ledger, '_capacity_clock_reference', return_value=0):
                self.assertEqual(1, case.ledger.release_due_account_holds(limit=64))
                decision = tuple(case.ledger._connection.execute(
                    "SELECT state,generation,released_at_ms,receipt FROM run_holds WHERE kind='probe'").fetchone())
                self.assertEqual(('released', generation + 1), decision[:2])
                self.assertIn(handle, owner._signatures.values())  # cancellation awaits worker confirmation
                gate.set()
                end = time.monotonic() + 10
                while owner._pending:
                    case.claims.drive()
                    self.assertFalse(case.claims.health.view()['faulted'])
                    self.assertLess(time.monotonic(), end, 'closed owner signature was retained')
                    time.sleep(.002)
                self.assertEqual(0, case.ledger.release_due_account_holds(limit=64))
            self.assertEqual(decision, tuple(case.ledger._connection.execute(
                "SELECT state,generation,released_at_ms,receipt FROM run_holds WHERE kind='probe'").fetchone()))
        self.assertEqual(({}, {}, {}), (owner._pending, owner._reserved_inputs, owner._signatures))
        self.assertNotIn(handle, pool._returned)
        self.assertNotIn(handle, case.claims._intake._jobs)
        self.assertEqual(0, case.ledger.account_capacity(project)['billing_probes']['live'])
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            owner.admit(reservation)
        replay = owner.reserve(project, case.runtime, 'b' * 64, 'owner-only', case.contexts[project],
                               before_commit=lambda: True)
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (replay['probe'], replay['reason_code'], replay['known']))
        self.assertFalse(case.claims.health.view()['faulted'])


class RestoredDecisionTests(unittest.TestCase):
    @requirements('RSA3-2', why_not_scenario='Canonical scenario: crashed and lost slot decisions survive both project backup formats and isolated restoration.')
    def test_RSA3_2_crashed_and_lost_slots_restore_exactly_once(self):
        for kind in ('crashed', 'lost'):
            with self.subTest(kind=kind):
                case = hold_fixtures.RunHoldTests('runTest')
                case.setUp()
                self.addCleanup(lambda case=case: self.assertTrue(case.doCleanups()))
                case.case.claim()
                row = case.rows()[0]
                if kind == 'lost':
                    with case.ledger._account_transaction():
                        case.ledger._connection.execute(
                            "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                            "WHERE handle_digest=?", (row['handle_digest'],))
                now = row['deadline_at_ms'] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
                self.assertGreaterEqual(case.sweep(now, row['clock_reference_ns'] - 1), 1)
                self.assertEqual(('released', kind, 1), case.slot_hold(case.ledger, row['handle_digest']))
                expected = {table: [tuple(r) for r in case.ledger._connection.execute(
                    f'SELECT * FROM {table} ORDER BY 1,2,3')]
                    for table in ('active_run_capacity', 'run_holds')}
                backup = case.ledger.create_sanitized_backup(
                    case.project, file_id='decided_slot', expires_at_ms=now + 60_000)
                case.ledger.verify_sanitized_backup(case.project, file_id='decided_slot')
                # Format 3 is itself the isolated restored partition; copy it to a fresh database.
                with sqlite3.connect(backup) as source, sqlite3.connect(':memory:') as restored:
                    source.backup(restored)
                    self.assertTrue(check_run_holds_consistency(restored))
                    for table, rows in expected.items():
                        self.assertEqual(rows, [tuple(r) for r in restored.execute(
                            f'SELECT * FROM {table} ORDER BY 1,2,3')])
                    self.assertEqual(0, restored.execute(
                        "SELECT COUNT(*) FROM run_holds WHERE kind='run_slot' AND state='held'").fetchone()[0])
                # Format 2's verifier restores the whole partition before checking coverage.
                body = learning_store._snapshot_project_partition(case.ledger._connection, case.project,
                    ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION)
                verify = learning_store.check_run_holds_consistency

                def restored_check(connection):
                    self.assertIsNot(connection, case.ledger._connection)
                    self.assertTrue(verify(connection))
                    for table, rows in expected.items():
                        self.assertEqual(rows, [tuple(r) for r in connection.execute(
                            f'SELECT * FROM {table} ORDER BY 1,2,3')])
                    return True

                with mock.patch.object(learning_store, 'check_run_holds_consistency', side_effect=restored_check) as checked:
                    learning_store._verify_project_backup_body(body)
                self.assertEqual(1, checked.call_count)


class LateCompletionTests(unittest.TestCase):
    @requirements('RSA3-3', why_not_scenario='Canonical account release scenario: valid terminal and cleanup evidence cannot release a decided slot again.')
    def test_RSA3_3_owner_release_refuses_a_decided_slot(self):
        case = cleanup_fixtures.AccountCleanupReleaseTests('runTest')
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        handle, before = case.base.decided('crashed')
        with self.assertRaisesRegex(AccountError, 'claim_cleanup_conflict'):
            case.call()
        self.assertEqual(before, tuple(case.ledger._connection.execute(
            'SELECT released_at_ms,release_digest FROM active_run_capacity WHERE handle_digest=?',
            (handle,)).fetchone()))
        self.assertIsNone(case.ledger._connection.execute(
            'SELECT receipt_digest FROM capacity_completions').fetchone()[0])

    @requirements('RSA3-3', why_not_scenario='Ledger-local transaction edge with synthetic cleaned registry custody: compare the durable late-release rule; no claim of process containment proof.')
    @unittest.expectedFailure  # F2: ledger-local release accepts a decided slot; owner decides the fix.
    def test_RSA3_3_ledger_local_late_completion_matches_owner_refusal(self):
        case = execution_fixtures.CapacityCompletionTests('runTest')
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        handle, intent, ticket = case.prepare_claim()
        case.ledger.record_dispatch_result(case.fixture.project_id, run_handle=handle,
            bridge_run_id=intent['bridge_run_id'], result=execution_fixtures.fixtures.null_filled(
                execution_fixtures.fixtures.result()), fenced=False, reviewed=True, terminal_state=None)
        # Synthetic custody isolates this ledger rule from kernel process inspection (unavailable in
        # some sandboxes). This does not test or assert that a real process tree was contained.
        case.ledger._capacity_executions._entry(ticket).state = 'cleaned'
        key = case.ledger._capacity_executions.key(ticket, cleaned=True)
        with case.ledger._account_transaction():
            case.ledger._release_slot_locked(key.project_id, key.handle_digest, 'crashed',
                case.ledger._clock_ms(), case.ledger._capacity_clock_reference())
        before = tuple(case.ledger._connection.execute(
            'SELECT released_at_ms,release_digest FROM active_run_capacity').fetchone())
        refused = False
        try:
            reply = case.ledger._release_active_capacity(ticket)
        except learning_store.LedgerError:
            refused = True
        if not refused:
            self.assertEqual(('released', False), (reply['state'], reply['known']))
            self.assertEqual(reply['receipt_digest'], case.ledger._connection.execute(
                'SELECT receipt_digest FROM capacity_completions').fetchone()[0])
            self.assertEqual(before, tuple(case.ledger._connection.execute(
                'SELECT released_at_ms,release_digest FROM active_run_capacity').fetchone()))
        self.assertTrue(refused, 'ledger-local late completion accepted after crashed decision; owner release_capacity refuses claim_cleanup_conflict')
