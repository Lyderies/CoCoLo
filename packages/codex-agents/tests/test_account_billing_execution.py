"""Signed admission and real inert process settlement, not native authorization."""
from copy import copy
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

from tests import test_billing_probe_worker as fixtures
from tests import test_bridge as bridge_fixtures
from codex_agents import bridge
from codex_agents.account_billing_admission import AccountBillingAdmission, ProbeAdmissionError
from codex_agents.account_billing_execution import AccountBillingExecutions
from codex_agents.account_observation import ObservationContext
from codex_agents.account_service import AccountOwnerViews
from codex_agents.owned_execution import ExecutionCustodyError
from codex_agents.signature_job_intake import SignatureJobIntake, SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerPool, SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                     'original fixed macOS signature worker guard')
class AccountBillingExecutionTests(unittest.TestCase):
    stop = fixtures.BillingProbeWorkerTests.stop
    until = fixtures.BillingProbeWorkerTests.until

    def setUp(self):
        fixtures.BillingProbeWorkerTests.setUp(self)
        self.ledger = self.fixture.ledger
        self.clock = time.time_ns()//1_000_000
        patch = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.fixture.admit('claude')
        self.views = AccountOwnerViews(self.ledger)
        intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.signatures = SignatureWorkerPool(intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.signatures.bootstrap()
        self.addCleanup(self.stop_signatures)
        self.owner = AccountBillingAdmission(self.views, self.signatures, SQLReadLimits(100_000, 1000, 2*1_048_576))
        self.executions = AccountBillingExecutions(self.owner, self.pool, self.registry)
        self.context = ObservationContext(str(self.fixture.project.resolve()))
        self.allowed = True

    def stop_signatures(self):
        self.signatures.request_close()
        end = time.monotonic()+10
        while any(s.thread.is_alive() for s in self.signatures._slots):
            self.signatures.poll()
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)

    def reserve(self):
        return self.owner.reserve(self.fixture.project_id, 'a'*64, 'b'*64, 'fixed-probe', self.context,
                                  before_commit=lambda: self.allowed)

    def admit(self, reservation=None):
        reservation = self.reserve() if reservation is None else reservation
        signature = self.owner.prepare(reservation)
        self.signatures.offer_next()
        end = time.monotonic()+10
        while signature not in self.signatures._returned:
            self.signatures.poll()
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        permit = self.owner.admit(reservation)
        return permit

    def prepare(self, frames=None, *, admit=True):
        self.fixture.frames('claude', frames if frames is not None else
            [bridge_fixtures.PROBE_INIT, bridge_fixtures.FAVOURABLE, bridge_fixtures.result_frame('OK')])
        reservation = self.reserve()
        identity = self.ledger.observe_provider(self.fixture.project_id, 'claude', project_root=self.fixture.project,
                                                environ=bridge_fixtures.CLEAN)[0]
        handle = self.executions.prepare(reservation, identity)
        self.until(lambda: self.executions.snapshot(handle)['worker_state'] != 'preparing')
        self.assertEqual('prepared', self.executions.snapshot(handle)['worker_state'])
        self.assertEqual('reserved', self.row()['state'])
        if not admit:
            return handle, reservation
        permit = self.admit(reservation)
        return handle, permit

    def launch(self, frames=None):
        handle, permit = self.prepare(frames)
        supervisor = self.executions.launch(handle, permit)
        self.until(lambda: self.executions.snapshot(handle)['worker_state'] in {'returned', 'unknown'})
        self.assertEqual('returned', self.executions.snapshot(handle)['worker_state'])
        self.clock = time.time_ns()//1_000_000
        return handle, permit, supervisor

    def row(self):
        return dict(self.ledger._connection.execute('SELECT * FROM account_billing_probe_requests').fetchone())

    def model_invocations(self):
        return [i for i in self.fixture.invocations('claude') if i['argv'][0] == '-p']

    def test_original_preparation_binding_refuses_copies_and_mutation_before_admission(self):
        handle, reservation = self.prepare(admit=False)
        job = self.executions._jobs[handle]
        identity, deadline = job.identity, job.deadline_ns
        with self.assertRaises(ProbeAdmissionError):
            self.executions.snapshot(copy(handle))
        with self.assertRaises(ProbeAdmissionError):
            self.admit(copy(reservation))
        for field, value in (('identity', b'{}'), ('deadline_ns', deadline+1),
                             ('payload', b'{}')):
            with self.subTest(field=field):
                original = getattr(job, field)
                setattr(job, field, value)
                try:
                    with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
                        self.admit(reservation)
                    self.assertEqual('reserved', self.row()['state'])
                    self.assertEqual(0, self.ledger._connection.execute(
                        "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])
                    self.assertEqual([], self.model_invocations())
                finally:
                    setattr(job, field, original)
        permit = self.admit(reservation)
        inputs, _ = self.owner._execution(permit)
        self.assertEqual(identity, inputs.identity)
        self.assertEqual(deadline, inputs.deadline_ns)
        self.assertEqual(reservation.deadline, self.row()['deadline_at_ms'])

    def test_actual_original_return_commits_records_and_settles_once_with_known_recovery(self):
        handle, permit, supervisor = self.launch()
        self.assertEqual('admitted', self.row()['state'])
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.assertTrue(supervisor.snapshot()['worker_returned'])
        reply = self.executions.complete(handle)
        self.assertEqual('completed', reply['probe'])
        self.assertIsNone(reply['reason_code'])
        self.assertFalse(reply['known'])
        self.assertEqual(['provider_identity', 'provider_headroom', 'provider_billing'],
                         [r['record_type'] for r in reply['records']])
        self.assertEqual('committed', self.row()['state'])
        self.assertEqual(0, self.pool._charged)
        self.assertNotIn(permit, self.owner._execution_inputs)
        self.assertEqual('released', supervisor.snapshot()['state'])
        self.assertEqual(1, len(self.model_invocations()))
        known = self.reserve()
        self.assertEqual({**reply, 'known': True}, known)
        self.assertEqual(1, len(self.model_invocations()))
        with self.assertRaises(ProbeAdmissionError):
            self.executions.complete(handle)

    def test_revocation_after_record_append_rolls_back_both_records_and_settlement(self):
        handle, _, supervisor = self.launch()
        before = self.ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        append = self.ledger._append_checked_locked
        def revoke(*args, **kwargs):
            result = append(*args, **kwargs)
            self.allowed = False
            return result
        with mock.patch.object(self.ledger, '_append_checked_locked', new=revoke):
            with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
                self.executions.complete(handle)
        self.assertEqual('admitted', self.row()['state'])
        self.assertEqual(before, self.ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual('cleaned', supervisor.snapshot()['state'])
        self.assertGreater(self.pool._charged, 0)
        self.allowed = True
        self.executions.complete(handle)
        self.assertEqual('committed', self.row()['state'])

    def test_unknown_actual_tracking_cannot_free_durable_probe_capacity(self):
        with mock.patch.object(bridge, 'process_table', return_value=None):
            handle, _, supervisor = self.launch()
        self.assertFalse(supervisor.snapshot()['cleanup_confirmed'])
        with self.assertRaises(ExecutionCustodyError):
            self.executions.complete(handle)
        self.assertEqual('admitted', self.row()['state'])
        self.assertGreater(self.pool._charged, 0)

    def test_failed_process_preserves_restrictive_event_before_confirmed_settlement(self):
        (self.fixture.tools/'claude-exit-code').write_text('1')
        warning = {**bridge_fixtures.FAVOURABLE, 'rate_limit_info': {
            **bridge_fixtures.FAVOURABLE['rate_limit_info'], 'status': 'allowed_warning', 'utilization': .9}}
        handle, _, _ = self.launch([bridge_fixtures.PROBE_INIT, warning,
                                   bridge_fixtures.result_frame('OK')])
        reply = self.executions.complete(handle)
        self.assertEqual('probe_incomplete', reply['reason_code'])
        self.assertTrue(any(r['record_type'] == 'provider_headroom' and r['status'] == 'allowed_warning'
                            for r in reply['records']))
        self.assertEqual('committed', self.row()['state'])

    def test_duplicate_launch_and_copied_permit_refuse_without_another_model(self):
        handle, permit, _ = self.launch()
        with self.assertRaises(ProbeAdmissionError):
            self.executions.launch(handle, permit)
        with self.assertRaises(ProbeAdmissionError):
            self.executions.launch(handle, copy(permit))
        self.assertEqual(1, len(self.model_invocations()))

    def test_current_permission_revoked_between_preparation_and_offer_cannot_launch(self):
        handle, permit = self.prepare()
        self.allowed = False
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.executions.launch(handle, permit)
        self.assertEqual([], self.model_invocations())
        self.assertEqual('admitted', self.row()['state'])

    def test_current_raw_admission_evidence_changed_before_offer_cannot_launch(self):
        handle, permit = self.prepare()
        self.clock += 1
        self.ledger.observe_provider(self.fixture.project_id, 'claude', project_root=self.fixture.project,
                                     environ=bridge_fixtures.CLEAN)
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.executions.launch(handle, permit)
        self.assertEqual([], self.model_invocations())
        self.assertEqual('admitted', self.row()['state'])

    def test_mutated_original_permit_cannot_change_execution_identity(self):
        handle, permit = self.prepare()
        object.__setattr__(permit, 'account_fingerprint', 'f'*64)
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.executions.launch(handle, permit)
        self.assertEqual([], self.model_invocations())

    def test_unknown_workspace_removal_holds_both_durable_and_worker_capacity(self):
        with mock.patch.object(fixtures.workers, '_remove', return_value=False):
            handle, _, _ = self.launch()
        with self.assertRaises(ProbeAdmissionError):
            self.executions.complete(handle)
        self.assertEqual('admitted', self.row()['state'])
        self.assertGreater(self.pool._charged, 0)

    def test_completion_journal_has_metadata_only_and_no_raw_model_answer(self):
        handle, _, _ = self.launch([bridge_fixtures.PROBE_INIT, bridge_fixtures.FAVOURABLE,
                                   bridge_fixtures.result_frame('PRIVATE-MODEL-ANSWER')])
        self.executions.complete(handle)
        raw = self.row()['references_json']
        self.assertNotIn('PRIVATE-MODEL-ANSWER', raw)
        self.assertEqual({'probe', 'reason_code', 'records', 'completion_digest'}, json.loads(raw).keys())

    def test_known_reply_cannot_read_expired_records_or_relaunch_to_replace_them(self):
        handle, _, _ = self.launch()
        reply = self.executions.complete(handle)
        record = reply['records'][0]
        self.ledger._connection.execute('UPDATE records SET expires_at_ms=? WHERE project_id=? AND record_id=?',
            (self.clock, self.fixture.project_id, record['record_id']))
        with self.assertRaises(ProbeAdmissionError):
            self.reserve()
        self.assertEqual(1, len(self.model_invocations()))
        self.assertEqual('committed', self.row()['state'])
