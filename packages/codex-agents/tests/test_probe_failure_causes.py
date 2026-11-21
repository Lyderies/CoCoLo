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
        real = self.billing._owner.reserve

        def reserve(*args, **kwargs):
            if any(job.state == 'ready' for job in self.billing._jobs.values()):
                raise RuntimeError('x')
            return real(*args, **kwargs)
        with mock.patch.object(self.billing._owner, 'reserve', side_effect=reserve):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_reply_read_failed', self.cause())

    def test_an_unsettled_failed_step_names_its_cause(self):
        # account_billing_jobs.py _settle_failed_step (was :272)
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
        # Strengthened (never loosened): the earlier assertion also accepted probe_settle_failed, which the old
        # code already named from the retry, so it could not tell the new cause from the old behaviour. The new
        # cause must itself have been named for this failure, whatever a later retry names over it.
        self.assertIn('probe_settle_unconfirmed', causes)
        self.assertNotEqual('probe_unavailable', self.cause())

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

    def test_a_cancel_after_an_earlier_named_failure_keeps_the_earlier_cause(self):
        with mock.patch.object(self.claims._pool, 'stuck', return_value=True):
            with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
                self.take(self.begin())
        self.assertEqual('probe_signature_pool_stuck', self.cause())

    def test_a_discarding_job_whose_step_fails_again_names_the_unsettled_cause(self):
        # _settle_failed_step in its discarding branch: the bare probe_unavailable it leaves is named.
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        job.state, job.reason = 'discarding', None
        job.discard_faults = 2  # the third fault keeps the job as unknown
        self.billing._settle_failed_step(job, unsure='probe_unavailable')
        self.assertEqual('unknown', job.state)
        self.assertEqual('probe_settle_unconfirmed', self.cause())


if __name__ == '__main__':
    unittest.main()
