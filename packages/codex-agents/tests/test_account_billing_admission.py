"""Real registered signature returns; model launch/native enrollment remain gated."""
from copy import copy
from pathlib import Path
import json
import runpy
import sys
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
from tests import test_providers as fixtures

from codex_agents.account_billing_admission import AccountBillingAdmission, ProbeAdmission, ProbeAdmissionError
from codex_agents.account_observation import ObservationContext
from codex_agents.account_service import AccountOwnerViews
from codex_agents.active_run_capacity import ActiveRunLimits
from codex_agents.billing_probe_schema import assert_billing_probe_schema
from codex_agents.learning_contracts import canonical_json
from codex_agents import learning_store
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.providers import claude_rate_limit_records
from codex_agents.signature_job_intake import SignatureJobIntake, SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerPool, SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'original fixed macOS signature worker guard')
class AccountBillingAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdmissionTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        self.ledger = self.fixture.ledger
        self.clock = self.fixture.clock+100
        self.clock_patch = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.identity = self.fixture.observe('claude', now_ms=self.clock)[0]
        self.fixture.attest('claude', self.identity['account_fingerprint'])
        assert_billing_probe_schema(self.ledger._connection)
        self.views = AccountOwnerViews(self.ledger)
        self.intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.pool = SignatureWorkerPool(self.intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.pool.bootstrap()
        self.addCleanup(self.stop)
        self.owner = AccountBillingAdmission(self.views, self.pool, SQLReadLimits(100_000, 1000, 2*1_048_576))
        self.context = ObservationContext(str(self.fixture.project.resolve()))
        self.allowed = True

    def stop(self):
        if self.pool is None:
            return
        self.pool.request_close()
        end = time.monotonic()+10
        while any(s.thread.is_alive() for s in self.pool._slots):
            self.pool.poll()
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)

    def reserve(self, identity='probe-original', **changes):
        options = dict(before_commit=lambda: self.allowed)
        options.update(changes)
        return self.owner.reserve(self.fixture.project_id, 'a'*64, 'b'*64, identity, self.context, **options)

    def prepared(self, original):
        handle = self.owner.prepare(original)
        self.pool.offer_next()
        end = time.monotonic()+10
        while handle not in self.pool._returned:
            self.pool.poll()
            self.assertLess(time.monotonic(), end, 'original signature return expired')
            time.sleep(.002)
        return handle

    def rows(self):
        return [dict(r) for r in self.ledger._connection.execute('SELECT * FROM account_billing_probe_requests')]

    def test_actual_registered_signatures_commit_one_probe_admission_without_launching_a_model(self):
        original = self.reserve()
        self.assertIs(original, self.reserve())
        self.assertEqual('reserved', self.rows()[0]['state'])
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.owner.prepare(copy(original))
        handle = self.prepared(original)
        with mock.patch.object(self.ledger, '_prepare_attestations', side_effect=AssertionError('inline SSH')):
            admitted = self.owner.admit(original)
        self.assertIs(type(admitted), ProbeAdmission)
        self.assertEqual(self.identity['account_fingerprint'], admitted.account_fingerprint)
        self.assertEqual('admitted', self.rows()[0]['state'])
        self.assertIs(admitted, self.owner.admit(original))
        self.assertNotIn(handle, self.pool._returned)
        self.assertEqual(1, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertNotIn(b'client_path', canonical_json(self.rows()))

    def test_second_request_cannot_take_the_account_bootstrap_or_refresh_original_deadline(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        second = self.reserve('second')
        self.prepared(second)
        denied = self.owner.admit(second)
        self.assertEqual('denied', denied['probe'])
        self.assertEqual('probe_cooldown', denied['reason_code'])
        self.assertEqual({**denied, 'known': True}, self.reserve('second'))
        self.assertEqual(original.deadline, self.reserve().deadline)
        self.assertEqual(['admitted', 'committed'], [r['state'] for r in self.rows()])

    def test_changed_raw_evidence_after_signature_return_refuses_without_spending_bootstrap(self):
        original = self.reserve()
        self.prepared(original)
        self.clock += 1
        self.fixture.observe('claude', now_ms=self.clock)
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.admit(original)
        self.assertEqual('reserved', self.rows()[0]['state'])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_late_permission_revocation_rolls_back_both_admission_and_request_transition(self):
        original = self.reserve()
        self.prepared(original)
        store = self.ledger._store_probe_admission_locked
        def revoke(*args, **options):
            store(*args, **options)
            self.allowed = False
        with mock.patch.object(self.ledger, '_store_probe_admission_locked', new=revoke):
            with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
                self.owner.admit(original)
        self.assertEqual('reserved', self.rows()[0]['state'])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_recent_billing_reuses_original_records_without_probe_admission_and_known_retry_needs_no_worker(self):
        records = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'},
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=self.clock)
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
        original = self.reserve()
        self.prepared(original)
        reused = self.owner.admit(original)
        self.assertEqual('reused', reused['probe'])
        self.assertEqual([self.identity, *records], reused['records'])
        with mock.patch.object(self.intake, 'capture_admission', side_effect=AssertionError('known signature work')):
            self.assertEqual({**reused, 'known': True}, self.reserve())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_default_denial_and_tampered_journal_cannot_start_signature_work(self):
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.reserve(before_commit=None)
        self.assertEqual([], self.rows())
        original = self.reserve()
        self.ledger._connection.execute('UPDATE account_billing_probe_requests SET deadline_at_ms=deadline_at_ms+1')
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.prepare(original)
        self.assertEqual(0, len(self.intake._jobs))

    def test_admitted_probe_counts_against_shared_machine_and_account_capacity(self):
        original = self.reserve()
        self.prepared(original)
        with mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(3, 1)):
            self.owner.admit(original)
            with self.ledger._account_transaction():
                with self.assertRaisesRegex(ClaimError, 'claim_busy'):
                    self.ledger._check_active_capacity_locked('claude', self.identity['account_fingerprint'])
                self.ledger._check_active_capacity_locked('codex', 'c'*64)
                self.ledger._check_active_capacity_locked('claude', 'c'*64)
            with mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(1, 1)):
                with self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, 'claim_busy'):
                    self.ledger._check_active_capacity_locked('codex', 'c'*64)
        self.clock += 1_000_000
        with self.assertRaisesRegex(learning_store.LedgerError, 'project_has_active_billing_probe'):
            self.ledger.delete_project(self.fixture.project_id)
        self.assertEqual('admitted', self.rows()[0]['state'])

    def test_expired_original_cannot_start_new_signature_work_or_get_a_new_window(self):
        original = self.reserve()
        self.clock = original.deadline
        self.assertIs(original, self.reserve())
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.prepare(original)
        self.assertEqual(0, len(self.intake._jobs))
        self.assertEqual(original.deadline, self.rows()[0]['deadline_at_ms'])

    def test_mutated_original_native_deadline_or_authorizer_cannot_prepare_signatures(self):
        for field, value in (('deadline_ns', time.monotonic_ns()+120_000_000_000),
                             ('authorize', lambda: True)):
            with self.subTest(field=field):
                original = self.reserve('mutated-'+field)
                object.__setattr__(original, field, value)
                with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
                    self.owner.prepare(original)
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_standalone_backup_preserves_genuine_sealed_admission_and_rejects_missing_request(self):
        from copy import deepcopy
        from codex_agents.learning_contracts import content_digest, validate_learning_record
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        before = self.rows()
        path = self.ledger.create_sanitized_backup(self.fixture.project_id, file_id='original_probe',
            expires_at_ms=time.time_ns()//1_000_000+60_000)
        body = json.loads(path.read_bytes())['body']
        self.assertEqual(13, body['ledger_schema_version'])
        self.assertTrue(self.ledger.verify_sanitized_backup(self.fixture.project_id, file_id='original_probe')['verified'])
        tool = runpy.run_path(str(REPOSITORY_ROOT/'scripts/backup_installed_project.py'))
        tool['_verify_relational_restore'](self.ledger._connection, body,
            validate_learning_record=validate_learning_record, content_digest=content_digest)
        broken = deepcopy(body)
        broken['tables']['account_billing_probe_requests'] = []
        broken['counts']['account_billing_probe_requests'] = 0
        with self.assertRaises(RuntimeError):
            tool['_verify_relational_restore'](self.ledger._connection, broken,
                validate_learning_record=validate_learning_record, content_digest=content_digest)
        self.assertEqual(before, self.rows())

    def test_restart_preserves_admitted_unknown_without_recreating_a_launchable_original(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        before = self.rows()
        self.stop()
        self.pool = None
        self.ledger.close()
        reopened = learning_store.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.ledger = self.fixture.ledger = reopened
        self.views = AccountOwnerViews(reopened)
        self.intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.pool = SignatureWorkerPool(self.intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.pool.bootstrap()
        restarted = AccountBillingAdmission(self.views, self.pool, SQLReadLimits(100_000, 1000, 2*1_048_576))
        with (mock.patch.object(reopened, '_clock_ms', side_effect=AssertionError('restart window')),
                self.assertRaisesRegex(ProbeAdmissionError, 'probe_unavailable')):
            restarted.reserve(self.fixture.project_id, 'a'*64, 'b'*64, 'probe-original',
                ObservationContext(self.context.root), before_commit=lambda: True)
        self.assertEqual(before, self.rows())
