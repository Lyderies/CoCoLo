"""Recurring fix (stale collector, 21 November 2026): no probe_unavailable is left bare.

Every path that answers probe_unavailable names its cause in the billing lane's last_failure (the cause is
a fixed code, never the generic one), so learning-status says why. The reply code itself is unchanged.
"""
import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_account_billing_jobs as base
from codex_agents.account_api import AccountError


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'original fixed macOS signature worker guard')
class ProbeFailureCauseTests(unittest.TestCase):
    setUp = base.AccountBillingJobTests.setUp
    stop = base.AccountBillingJobTests.stop
    request = base.AccountBillingJobTests.request
    begin = base.AccountBillingJobTests.begin
    until = base.AccountBillingJobTests.until
    take = base.AccountBillingJobTests.take
    launches = base.AccountBillingJobTests.launches

    def cause(self):
        failure = self.billing.health.view()['last_failure']
        self.assertIsNotNone(failure, 'no last_failure was recorded')
        self.assertNotEqual('probe_unavailable', failure['cause'])
        return failure['cause']

    def test_a_reply_step_that_raises_anything_names_its_cause(self):
        # account_service.py catch-all (was :820-821)
        with mock.patch('codex_agents.account_service.decode_account_response', side_effect=RuntimeError('x')):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_reply_failed', self.cause())

    def test_a_reason_off_the_allow_list_names_its_cause(self):
        # account_service.py allow-list fallback (was :837-842)
        real = self.billing.take

        def take(*args, **kwargs):
            reply = real(*args, **kwargs)
            return None if reply is None else dataclasses.replace(reply, result=None, reason='not_a_known_reason')
        with mock.patch.object(self.billing, 'take', side_effect=take):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_reason_unrecognised', self.cause())

    def test_a_finish_that_fails_without_a_reason_names_its_cause(self):
        # account_billing_jobs.py _discard after a failed finish_signature (was :274)
        with mock.patch.object(self.billing._owner, 'admit', side_effect=RuntimeError('x')):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_admit_failed', self.cause())

    def test_a_stuck_signature_pool_names_its_cause(self):
        # account_billing_jobs.py (was :364)
        with mock.patch.object(self.claims._pool, 'stuck', return_value=True):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_signature_pool_stuck', self.cause())

    def test_a_reply_read_that_fails_names_its_cause(self):
        # account_billing_jobs.py take() (was :517)
        real = self.billing._owner.verify_committed  # decision 44 (BR-8): publication checks the committed row

        def verify(*args, **kwargs):
            if any(job.state == 'ready' for job in self.billing._jobs.values()):
                raise RuntimeError('x')
            return real(*args, **kwargs)
        with mock.patch.object(self.billing._owner, 'verify_committed', side_effect=verify):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_reply_read_failed', self.cause())

    def test_an_unsettled_failed_step_names_its_cause(self):
        """GUARD (also passes on bf78c373, where this cause was already named): an admitted probe whose launch
        fails and whose settlement cannot be confirmed names probe_settle_unconfirmed, and the cause the lane
        finally reports is one of the two settlement causes (never the bare one, never another stage's)."""
        real, calls = self.billing._executions.launch, []

        def failing(*args):
            calls.append(1)
            raise RuntimeError('fault after the admission commit')
        self.enterContext(mock.patch.object(self.billing._executions, 'launch', side_effect=failing))
        self.enterContext(mock.patch.object(self.billing._executions, 'ticketed',
                                            side_effect=lambda r: self.ledger._connection.in_transaction))
        causes = []
        real_note = self.billing.health.note_failure
        self.enterContext(mock.patch.object(self.billing.health, 'note_failure',
                                            side_effect=lambda slot, cause, error: (causes.append(cause),
                                                                                    real_note(slot, cause, error))))
        with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
            self.take(self.begin())
        # Both checks: the history (the new cause was named for this failure) and the earlier one on the final
        # cause (a later retry may name its own failure over it, but only a settlement one).
        self.assertIn('probe_settle_unconfirmed', causes)
        self.assertIn(self.cause(), {'probe_settle_unconfirmed', 'probe_settle_failed'})

    def test_an_observation_reason_off_its_list_names_its_cause(self):
        # account_billing_jobs.py (was :342)
        real = self.observations.take

        def take(*args, **kwargs):
            reply = real(*args, **kwargs)
            return None if reply is None else dataclasses.replace(reply, result=None, reason='not_a_known_reason')
        with mock.patch.object(self.observations, 'take', side_effect=take):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_observation_reason_unrecognised', self.cause())

    def test_a_recognised_observation_reason_is_kept_and_not_called_unrecognised(self):
        # account_billing_jobs.py (the reason is on the allow-list: no cause is invented for it)
        real = self.observations.take

        def take(*args, **kwargs):
            reply = real(*args, **kwargs)
            return None if reply is None else dataclasses.replace(reply, result=None, reason='observation_not_authorized')
        with mock.patch.object(self.observations, 'take', side_effect=take):
            with self.assertRaisesRegex(AccountError, '^observation_not_authorized$'):
                self.take(self.begin())
        self.assertIsNone(self.billing.health.view()['last_failure'])

    def test_a_job_cancelled_by_its_deadline_names_it(self):
        from types import SimpleNamespace
        from codex_agents import account_billing_jobs
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        late = SimpleNamespace(monotonic_ns=lambda: job.reservation.deadline_ns + 1)
        with mock.patch.object(account_billing_jobs, 'time', late), \
                mock.patch.object(self.observations, 'take', return_value=None):
            self.service.drive_claims()
        self.assertTrue(job.cancelled)
        self.assertEqual('probe_deadline_expired', self.cause())

    def test_a_caller_cancel_names_itself(self):
        ticket = self.begin()
        self.billing.cancel(ticket)
        with self.assertRaises(AccountError):
            self.take(ticket)
        self.assertEqual('probe_cancelled', self.cause())

    def test_a_close_names_itself(self):
        ticket = self.begin()
        self.billing.request_close()
        self.assertEqual('probe_closing', self.cause())

    def test_a_cancel_that_reaches_the_signature_finish_is_not_an_admission_failure(self):
        real = type(self.billing).finish_signature

        def finish(billing, handle):
            billing._jobs[billing._signatures[handle]].cancelled = True  # cancelled while its signature was out
            return real(billing, handle)
        with mock.patch.object(type(self.billing), 'finish_signature', finish):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_cancelled', self.cause())

    def test_a_cancel_that_follows_a_named_failure_on_a_live_job_keeps_the_earlier_cause(self):
        """GUARD on 8ec26a08 (the production code is unchanged since), red on bf78c373: a caller's cancel names the
        job's failure while its signature work is out; the close that follows would name another cause when
        that work returns. The first named cause stays the one shown."""
        real = type(self.billing).finish_signature
        seen = []

        def finish(billing, handle):
            ticket = billing._signatures[handle]
            # the pool reports its signature work as still running, so the handle stays mapped until it returns
            with mock.patch.object(billing._claims._pool, 'cancel', return_value='running'):
                billing.cancel(ticket)
            seen.append(self.billing.health.view()['last_failure']['cause'])
            billing.request_close()
            return real(billing, handle)
        with mock.patch.object(type(self.billing), 'finish_signature', finish):
            ticket = self.begin()
            self.until(lambda: seen)
        self.assertEqual(['probe_cancelled'], seen)
        self.assertEqual('probe_cancelled', self.cause())  # not probe_closing, not probe_admit_failed

    def test_a_discarding_job_that_ends_unknown_through_cleanup_faults_is_named_by_them(self):
        """GUARD (passes on bf78c373 and 8ec26a08): repeated cleanup faults on a cancelled job take it to
        unknown, and each fault names itself (`probe_cleanup_failed`), so the job ends named, never bare. This
        does not enter _settle_failed_step's discarding branch (a fault in a discarding step does, and `drive`
        names that `probe_step_failed`). The job is held in 'preparing' (its worker never reports prepared) so
        the cancel cannot meet an offered job."""
        self.enterContext(mock.patch.object(self.billing._executions, 'snapshot',
                                            return_value={'worker_state': 'running'}))
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        self.until(lambda: job.preparation is not None and job.state == 'preparing')
        self.assertEqual('preparing', job.state)
        with mock.patch.object(self.billing._executions, 'discard', side_effect=RuntimeError('cleanup fault')):
            self.billing.cancel(ticket)
            self.until(lambda: job.state == 'unknown')
        self.assertEqual('probe_cleanup_failed', self.cause())


if __name__ == '__main__':
    unittest.main()
