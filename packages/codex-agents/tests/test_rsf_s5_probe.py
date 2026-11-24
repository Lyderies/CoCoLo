"""Decision 188: store-level probe visibility, preserving the decided P6 expiry."""
import json
import sqlite3
import unittest
from unittest import mock

from contextlib import ExitStack
from tests import test_account_billing_admission as fixtures
from tests import test_providers as provider_fixtures
from codex_agents import learning_store
from codex_agents.account_billing_admission import AccountBillingAdmission
from codex_agents.account_observation import ObservationContext
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_contracts import content_digest
from codex_agents.signature_job_intake import SignatureJobIntake, SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerPool, SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits
from codex_agents.account_billing_admission import PROBE_ORPHAN_GRACE_MS
from codex_agents.client_runtime import _status_account_holds


class _StoreFixture(unittest.TestCase):
    """Real sealed admission rows; no signature process, worker thread, model or socket."""
    reserve = fixtures.AccountBillingAdmissionTests.reserve
    rows = fixtures.AccountBillingAdmissionTests.rows
    sweep_now = fixtures.AccountBillingAdmissionTests.sweep_now

    def setUp(self):
        self.fixture = provider_fixtures.AdmissionTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.clock = self.fixture.clock + 100
        self.allowed = True
        self.context = ObservationContext(str(self.fixture.project.resolve()))
        self.stack = self.enterContext(ExitStack())
        self.owner_for_ledger()

    def owner_for_ledger(self):
        self.stack.enter_context(mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.clock))
        views = AccountOwnerViews(self.ledger)
        intake = SignatureJobIntake(views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        pool = SignatureWorkerPool(intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.owner = AccountBillingAdmission(views, pool, SQLReadLimits(100_000, 1000, 2*1_048_576))

    def admit(self, original):
        with self.ledger._account_transaction():
            record = self.ledger._store_probe_admission_locked(self.fixture.project_id,
                {'provider': 'claude', 'decision': 'pass', 'reason_code': 'admitted',
                 'admission_basis': 'bootstrap', 'account_fingerprint': 'c' * 64,
                 'evidence_digests': ['e' * 64], 'observed_at_ms': self.clock}, timestamp=self.clock)
            row = self.owner._row(self.fixture.project_id, original.key)
            self.owner._update({**row, 'account_fingerprint': 'c' * 64,
                'admission_record_id': record['record_id'], 'admission_digest': content_digest(record)}, state='admitted')

    def restart(self):
        self.ledger.close()
        self.ledger = self.fixture.ledger = learning_store.LearningLedger(
            self.fixture.state, client_capabilities=self.fixture.clients)
        self.owner_for_ledger()


class JournalLessProbeStatusTests(unittest.TestCase):
    def setUp(self):
        self.case = _StoreFixture(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        self.original = self.case.reserve()
        self.case.admit(self.original)
        self.cutoff = self.original.deadline + PROBE_ORPHAN_GRACE_MS

    def status(self):
        return self.case.ledger.account_holds_status(self.case.fixture.project_id)['probe']

    def expected_detail(self, cutoff=None):
        return {
            'state': 'probe_admitted_no_launch_evidence',
            'reason': 'No launch evidence is available; this billing check holds capacity until its deadline plus grace.',
            'release_after_ms': self.cutoff if cutoff is None else cutoff,
        }

    def test_reopened_admitted_probe_has_named_private_value_free_status_and_cutoff(self):
        self.case.restart()
        status = self.status()
        self.assertEqual([self.expected_detail()], status.get('admitted_no_launch_evidence'))
        self.assertEqual((1, 0, self.cutoff + 1, 0),
                         (status['held'], status['past_due'], status['next_eligible'], status['release_by_evidence']))
        serialized = json.dumps(status)
        row = self.case.rows()[0]
        for name in ('project_id', 'request_key', 'parameters_digest', 'runtime_digest',
                     'caller_scope', 'account_fingerprint', 'admission_record_id', 'admission_digest', 'state_seal'):
            self.assertNotIn(row[name], serialized, name)
        self.assertNotIn(str(self.case.fixture.project), serialized)
        self.assertEqual('admitted', row['state'])

    def test_status_is_read_only_even_when_p6_release_is_due(self):
        self.case.restart()
        self.case.clock = self.cutoff + 1
        connection = self.case.ledger._connection
        before = list(connection.iterdump())
        changes = connection.total_changes
        writes = []
        def read_only(action, arg1, arg2, database, source):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                writes.append((action, arg1))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(read_only)
        try:
            first = self.status()
            second = self.status()
            report = self.case.ledger.account_capacity(self.case.fixture.project_id)
        finally:
            connection.set_authorizer(None)
        self.assertEqual([], writes)
        self.assertEqual(changes, connection.total_changes)
        self.assertEqual(before, list(connection.iterdump()))
        self.assertEqual(first, second)
        self.assertEqual(first, report['holds']['probe'])
        self.assertEqual(1, first['past_due'])
        self.assertEqual([self.expected_detail()], first.get('admitted_no_launch_evidence'))
        self.assertEqual('admitted', self.case.rows()[0]['state'])

    def test_exact_grace_boundary_is_held_then_sweep_records_expiry_once_across_reopen(self):
        self.case.restart()
        self.case.clock = self.cutoff
        self.assertEqual(0, self.status()['past_due'])
        self.assertEqual(self.cutoff + 1, self.status()['next_eligible'])
        self.assertEqual(0, self.case.sweep_now())
        self.case.clock += 1
        self.assertEqual(1, self.status()['past_due'])
        self.assertEqual(1, self.case.sweep_now())
        row = self.case.rows()[0]
        self.assertEqual('committed', row['state'])
        self.assertEqual({'probe': 'denied', 'reason_code': 'probe_admitted_expired', 'records': []},
                         json.loads(row['references_json']))
        hold = dict(self.case.ledger._connection.execute("SELECT * FROM run_holds WHERE kind='probe'").fetchone())
        self.assertEqual(('released', self.case.clock), (hold['state'], hold['released_at_ms']))
        self.case.restart()
        self.assertEqual(row, self.case.rows()[0])
        self.assertEqual(0, self.case.sweep_now())
        self.assertEqual(hold, dict(self.case.ledger._connection.execute(
            "SELECT * FROM run_holds WHERE kind='probe'").fetchone()))
        self.assertEqual(0, self.status()['held'])
        self.assertNotIn('admitted_no_launch_evidence', self.status())
        replay = self.case.reserve()
        self.assertEqual(('denied', 'probe_admitted_expired', True),
                         (replay['probe'], replay['reason_code'], replay['known']))

    def test_current_original_stays_evidence_held_and_reserved_probe_is_not_misclassified(self):
        self.case.clock = self.cutoff + 1
        live = self.status()
        self.assertEqual((1, 0, None, 1),
                         (live['held'], live['past_due'], live['next_eligible'], live['release_by_evidence']))
        self.assertNotIn('admitted_no_launch_evidence', live)
        self.assertEqual(0, self.case.sweep_now())
        reserved = self.case.reserve('still-reserved')
        status = self.status()
        self.assertNotIn('admitted_no_launch_evidence', status)
        self.assertEqual(2, status['held'])
        self.assertEqual(reserved.deadline + 1, status['next_eligible'])

    def test_each_orphan_cutoff_is_reported_without_another_projects_details(self):
        self.case.restart()
        second = self.case.reserve('second-admitted')
        # The account's existing admitted probe denies a second admission; use a distinct fixture
        # row inside a store transaction to exercise multiple held deadlines without model work.
        row = self.case.rows()[0]
        from codex_agents.account_billing_admission import _FIELDS, keyed
        final = {**row, 'request_key': second.key, 'created_at_ms': second.created,
                 'deadline_at_ms': second.deadline + 1000}
        final['state_seal'] = keyed(self.case.ledger.state.digest_key(), b'billing-probe-state-v1\0',
                                    {name: final[name] for name in _FIELDS})
        with self.case.ledger._account_transaction():
            self.case.ledger._connection.execute(
                'UPDATE account_billing_probe_requests SET state=?,account_fingerprint=?,admission_record_id=?,'
                'admission_digest=?,deadline_at_ms=?,state_seal=? WHERE request_key=?',
                (final['state'], final['account_fingerprint'], final['admission_record_id'],
                 final['admission_digest'], final['deadline_at_ms'], final['state_seal'], second.key))
        self.case.restart()
        self.assertEqual([self.expected_detail(), self.expected_detail(final['deadline_at_ms'] + PROBE_ORPHAN_GRACE_MS)],
                         self.status().get('admitted_no_launch_evidence'))
        other_project = self.case.fixture.project.parent / 'other-project'
        other_project.mkdir()
        project = self.case.ledger.register_project(other_project, now_ms=1)
        self.assertNotIn('admitted_no_launch_evidence', self.case.ledger.account_holds_status(project)['probe'])

    def test_status_display_keeps_named_state_plain_reason_and_precise_release_time(self):
        self.case.restart()
        report = self.case.ledger.account_capacity(self.case.fixture.project_id)
        display = _status_account_holds(report)
        self.assertIsNotNone(display)
        self.assertEqual([self.expected_detail()], display['kinds']['probe'].get('admitted_no_launch_evidence'))
        from datetime import datetime, timezone
        cutoff_text = datetime.fromtimestamp(self.cutoff / 1000, timezone.utc).isoformat(timespec='milliseconds')
        for text in ('probe_admitted_no_launch_evidence', self.expected_detail()['reason'], cutoff_text):
            self.assertIn(text, display['summary'])
        self.assertNotIn('freed only by its result', display['summary'])

    def test_status_display_refuses_injected_private_or_unfixed_detail(self):
        self.case.restart()
        report = self.case.ledger.account_capacity(self.case.fixture.project_id)
        detail = self.expected_detail()
        for bad in ({**detail, 'reason': 'private account value'}, {**detail, 'request_key': 'private'},
                    {**detail, 'state': 'unknown'}, {**detail, 'release_after_ms': True}):
            with self.subTest(detail=bad):
                report['holds']['probe']['admitted_no_launch_evidence'] = [bad]
                self.assertIsNone(_status_account_holds(report))
