"""What the project host's run driver tries again, and for how long.

A fence the account refused, a given-back preparation whose cleanup did not
finish and a retired run's monitor slot the monitor refused are each asked for
again with a growing pause. None of them frees account capacity without the
account's own answer, and a live attempt is never forgotten.
"""
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_agents import project_driver
from codex_agents.project_driver import RunDriver, _Run
from codex_agents.project_monitor import ProjectMonitorError


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def driver():
    """A run driver with only the parts these steps touch."""
    value = object.__new__(RunDriver)
    value._clock = Clock()
    value._giving_back, value._cleanup_retry, value._forgetting = [], {}, {}
    value._preparation = mock.Mock()
    value._runtime = SimpleNamespace(persistence=mock.Mock(), owner=mock.Mock())
    value._runtime.persistence.lease.return_value = SimpleNamespace(lease_digest='e' * 64)
    value._account_call = mock.Mock()
    value._custody = mock.Mock(return_value=b'{}')
    return value


def fencing_run(**changes):
    run = _Run(key=('hrn_' + 'a' * 48, 1), scope=None, host_client='codex-host-v1', prospective=None,
               run_id='run-1', inputs_digest='d' * 64)
    run.state, run.claim_sent, run.prepared, run.persistence = 'fencing', True, 'preparation', 'lease-job'
    run.identity_digest, run.claim_request_id, run.evidence = 'c' * 64, 'claim-' + 'b' * 32, b'{}'
    for name, value in changes.items():
        setattr(run, name, value)
    return run


class RefusedFenceTests(unittest.TestCase):
    def refuse(self, value, run):
        """The fence the account was sent comes back refused (claim_denial_unavailable, say)."""
        value._fail(run, 'claim_denial_unavailable')

    def test_a_refused_fence_keeps_everything_and_is_asked_again_with_a_growing_pause(self):
        value, run = driver(), fencing_run()
        self.refuse(value, run)
        # Nothing is freed without the account's answer.
        self.assertEqual(('failed', 'preparation'), (run.state, run.prepared))
        value._preparation.discard.assert_not_called()
        self.assertEqual(101.0, run.fence_at)
        value._advance(run)
        value._account_call.assert_not_called()  # Not yet.
        pauses = []
        for _ in range(project_driver.MAX_FENCE_ATTEMPTS - 1):
            value._clock.now = run.fence_at
            value._advance(run)
            self.assertEqual('fencing', run.state)
            self.assertEqual('fence_claim', value._account_call.call_args.args[2])
            self.refuse(value, run)
            if run.fence_at is not None:
                pauses.append(run.fence_at - value._clock.now)
        self.assertEqual([2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0], pauses)
        self.assertEqual(project_driver.MAX_FENCE_ATTEMPTS, value._account_call.call_count + 1)

    def test_a_fence_never_answered_stops_holding_a_preparation_slot_but_keeps_its_lease(self):
        value, run = driver(), fencing_run()
        for _ in range(project_driver.MAX_FENCE_ATTEMPTS):
            if run.fence_at is not None:
                value._clock.now = run.fence_at
                value._advance(run)
            self.refuse(value, run)
        self.assertIsNone(run.fence_at)
        # The preparation can never be used (a failed run is never launched): it is given back.
        value._preparation.discard.assert_called_once_with('preparation')
        self.assertEqual((None, ['preparation']), (run.prepared, value._giving_back))
        # The lease is kept: the account's own timed rule settles the capacity.
        self.assertEqual('lease-job', run.persistence)
        value._runtime.persistence.retire_denied.assert_not_called()
        calls = value._account_call.call_count
        value._clock.now += 10_000
        value._advance(run)
        self.assertEqual(calls, value._account_call.call_count)

    def test_the_pause_is_capped(self):
        value, run = driver(), fencing_run(fence_attempts=20)
        with mock.patch.object(project_driver, 'MAX_FENCE_ATTEMPTS', 100):
            self.refuse(value, run)
        self.assertEqual(project_driver.FENCE_RETRY_MAX_SECONDS, run.fence_at - value._clock.now)

    def test_a_fence_that_cannot_be_built_is_tried_again_and_then_sent(self):
        value, run = driver(), fencing_run(state='recording_open')
        value._runtime.persistence.lease.side_effect = [RuntimeError('lane busy'), SimpleNamespace(lease_digest='e' * 64)]
        value._fence(run)
        self.assertEqual(('failed', 'preparation', 101.0), (run.state, run.prepared, run.fence_at))
        value._account_call.assert_not_called()
        value._clock.now = 101.0
        value._advance(run)
        self.assertEqual('fencing', run.state)
        value._account_call.assert_called_once()

    def test_a_run_whose_claim_was_never_sent_gives_its_preparation_back_at_once(self):
        value, run = driver(), fencing_run(state='withdrawing', claim_sent=False)
        value._runtime.persistence.lease.side_effect = RuntimeError('lane busy')
        value._fence(run)
        self.assertEqual(('failed', None, None), (run.state, run.prepared, run.fence_at))
        self.assertEqual(['preparation'], value._giving_back)

    def test_a_denied_or_admitted_run_is_never_fenced_again(self):
        value = driver()
        denied = fencing_run(state='settling', denied=True)
        value._fail(denied, 'claim_denial_unavailable')
        self.assertIsNone(denied.fence_at)
        admitted = fencing_run(state='binding', receipt={'state': 'claimed'})
        value._fail(admitted, 'run_failed')
        self.assertEqual(('failed', None, 'preparation'), (admitted.state, admitted.fence_at, admitted.prepared))


class GivenBackPreparationTests(unittest.TestCase):
    def test_a_preparation_whose_cleanup_did_not_finish_is_kept_and_its_removal_asked_again(self):
        value = driver()
        value._giving_back = ['preparation']
        states = {'preparation': 'unknown'}
        value._preparation.snapshot.side_effect = lambda handle: {'state': states[handle]}
        value._retire_given_back()
        self.assertEqual(['preparation'], value._giving_back)  # Kept: its slot is not lost.
        value._preparation.retry_cleanup.assert_called_once_with('preparation')
        value._retire_given_back()
        self.assertEqual(1, value._preparation.retry_cleanup.call_count)  # Not before its pause.
        value._clock.now += 1.0
        value._preparation.retry_cleanup.side_effect = project_driver.PreparationError('preparation_busy')
        value._retire_given_back()
        self.assertEqual(2, value._preparation.retry_cleanup.call_count)
        self.assertEqual(['preparation'], value._giving_back)
        states['preparation'] = 'cleaned'
        value._retire_given_back()
        value._preparation.retire.assert_called_once_with('preparation')
        self.assertEqual(([], {}), (value._giving_back, value._cleanup_retry))


    def test_a_removal_that_keeps_being_refused_is_asked_a_bounded_number_of_times_and_kept(self):
        value = driver()
        value._giving_back = ['preparation']
        value._preparation.snapshot.side_effect = lambda handle: {'state': 'unknown'}
        value._preparation.retry_cleanup.side_effect = project_driver.PreparationError('preparation_busy')
        for _ in range(project_driver.MAX_CLEANUP_ATTEMPTS + 5):
            value._retire_given_back()
            value._clock.now += project_driver.CLEANUP_RETRY_MAX_SECONDS
        self.assertEqual(project_driver.MAX_CLEANUP_ATTEMPTS, value._preparation.retry_cleanup.call_count)
        # Its workspace may still be there, so it is kept rather than counted free.
        self.assertEqual(['preparation'], value._giving_back)
        value._preparation.retire.assert_not_called()

class RetiredMonitorSlotTests(unittest.TestCase):
    BINDING = ('binding',)

    def test_a_refused_forget_is_tried_again_less_often_and_then_left(self):
        for reason in ('monitor_attempt_live', 'control_invalid_input', 'control_unavailable'):
            with self.subTest(reason=reason):
                value = driver()
                value._forgetting = {self.BINDING: (0, value._clock())}
                value._runtime.owner.forget.side_effect = ProjectMonitorError(reason) \
                    if reason.startswith('monitor') else RuntimeError(reason)
                value._forget_retired()
                value._forget_retired()  # Not tried again before its pause.
                self.assertEqual(1, value._runtime.owner.forget.call_count)
                for _ in range(project_driver.MAX_FORGET_ATTEMPTS + 5):
                    value._clock.now += project_driver.CLEANUP_RETRY_MAX_SECONDS
                    value._forget_retired()
                # A live attempt is never forgotten; after the last refusal it is no longer asked.
                self.assertEqual(project_driver.MAX_FORGET_ATTEMPTS, value._runtime.owner.forget.call_count)
                self.assertEqual({}, value._forgetting)

    def test_a_forget_that_succeeds_or_finds_nothing_is_done(self):
        for error in (None, ProjectMonitorError('monitor_attempt_not_found')):
            with self.subTest(error=error):
                value = driver()
                value._forgetting = {self.BINDING: (0, value._clock())}
                value._runtime.owner.forget.side_effect = error
                value._forget_retired()
                self.assertEqual({}, value._forgetting)

    def test_a_slot_freed_on_a_later_try(self):
        value = driver()
        value._forgetting = {self.BINDING: (0, value._clock())}
        value._runtime.owner.forget.side_effect = [ProjectMonitorError('monitor_attempt_live'), None]
        value._forget_retired()
        self.assertEqual({self.BINDING: (1, value._clock() + 1.0)}, value._forgetting)
        value._clock.now += 1.0
        value._forget_retired()
        self.assertEqual({}, value._forgetting)



class StuckSlotTests(unittest.TestCase):
    """O0c M1, M3, M4, M7: a hold nothing in this service will free is counted, kept, and makes the host
    replaced once idle (at once when no run slot is left); never freed while a process may live."""

    def stuck_driver(self, *runs):
        value = driver()
        value._runs = {run.key: run for run in runs}
        value._dispatch = SimpleNamespace(_jobs={})
        return value

    def test_M4_a_preparation_whose_removal_was_given_up_is_kept_and_counted_stuck(self):
        value = self.stuck_driver()
        value._giving_back = ['preparation']
        value._preparation.snapshot.side_effect = lambda handle: {'state': 'unknown'}
        value._preparation.retry_cleanup.side_effect = project_driver.PreparationError('preparation_busy')
        self.assertEqual(0, value.stuck_slots())
        for _ in range(project_driver.MAX_CLEANUP_ATTEMPTS + 1):
            value._retire_given_back()
            value._clock.now += project_driver.CLEANUP_RETRY_MAX_SECONDS
        self.assertEqual(['preparation'], value._giving_back)  # kept: its workspace may still be there
        self.assertEqual(1, value.stuck_slots())
        self.assertTrue(value.replace_when_idle())

    def test_M7_a_monitor_attempt_given_up_is_counted_stuck(self):
        value = self.stuck_driver()
        value._forgetting = {('binding',): (0, value._clock())}
        value._runtime.owner.forget.side_effect = ProjectMonitorError('monitor_attempt_live')
        for _ in range(project_driver.MAX_FORGET_ATTEMPTS + 1):
            value._forget_retired()
            value._clock.now += project_driver.CLEANUP_RETRY_MAX_SECONDS
        self.assertEqual(({}, 1), (value._forgetting, value.stuck_slots()))
        self.assertEqual(0, self.stuck_driver().stuck_slots())  # never shared between drivers

    def test_M3_an_admitted_failed_run_keeps_everything_and_is_stuck(self):
        admitted = fencing_run(state='binding', receipt={'state': 'claimed'})
        value = self.stuck_driver(admitted)
        value._fail(admitted, 'run_failed')
        self.assertEqual(('failed', 'preparation', 'lease-job'), (admitted.state, admitted.prepared, admitted.persistence))
        self.assertEqual(1, value.stuck_slots())
        self.assertTrue(value.replace_when_idle())
        self.assertFalse(value.exhausted())  # two slots are still free: replaced once idle, not at once

    def test_M3_a_run_still_waiting_for_an_answer_is_not_stuck(self):
        fencing = fencing_run(state='failed', fence_at=200.0)
        calling = fencing_run(state='failed', future=object(), key=('hrn_' + 'b' * 48, 1))
        closed = fencing_run(state='failed', closed=True, key=('hrn_' + 'c' * 48, 1))
        self.assertEqual(0, self.stuck_driver(fencing, calling, closed).stuck_slots())

    def test_M3_with_every_slot_stuck_the_host_is_replaced_at_once_when_no_run_is_under_way(self):
        runs = [fencing_run(state='failed', receipt={'state': 'claimed'}, key=('hrn_' + c * 48, 1)) for c in 'abc']
        value = self.stuck_driver(*runs)
        self.assertEqual(3, value.stuck_slots())
        self.assertTrue(value.exhausted())
        busy = fencing_run(state='ready', key=('hrn_' + 'd' * 48, 1))
        value._runs[busy.key] = busy
        self.assertFalse(value.exhausted())  # never from under a run under way
        self.assertFalse(value.replace_when_idle())

    def test_M1_a_cleanup_unconfirmed_run_is_held_not_busy_and_stuck(self):
        held = fencing_run(state='cleanup_unconfirmed', receipt={'state': 'claimed'}, reason='run_cleanup_unconfirmed')
        value = self.stuck_driver(held)
        self.assertFalse(value.busy())
        self.assertEqual(1, value.stuck_slots())
        self.assertEqual({'state': 'ready', 'reason_code': 'run_cleanup_unconfirmed'}, RunDriver._view(held))

    def test_M1_only_an_execution_left_unknown_after_its_worker_returned_is_held_unconfirmed(self):
        from codex_agents.history_contracts import HistoryError
        for token, view, expected in (
                (None, {'state': 'unknown', 'worker_returned': True}, 'cleanup_unconfirmed'),
                (None, {'state': 'active', 'worker_returned': False}, 'acknowledging'),  # still running
                (None, {'state': 'unknown', 'worker_returned': False}, 'acknowledging'),  # cleanup under way
                ('token', {'state': 'unknown', 'worker_returned': True}, 'acknowledging')):  # C3's container proof
            with self.subTest(token=token, view=view):
                run = fencing_run(state='acknowledging', receipt={'state': 'claimed'}, launch_token=token,
                                  prepared_inputs=SimpleNamespace(worker='worker'))
                value = self.stuck_driver(run)
                value._runtime.pool = mock.Mock()
                value._runtime.pool.supervisor.return_value.snapshot.return_value = view
                value._runtime.persistence.snapshot.return_value = {'state': 'acknowledged'}
                value._runtime.persistence.retain_cleanup.side_effect = HistoryError('history_not_authorized')
                value._advance(run)
                self.assertEqual(expected, run.state)
                self.assertEqual(expected == 'cleanup_unconfirmed', value.stuck_slots() == 1)


@unittest.skip("gates the run supervisor: the owner narrowed B0 on 2026-11-20 and moved the never-armed "
               "withdrawal there (DESIGN-0.4.0-B0-holds.md, Stop and analyse)")
class UnclaimedRunFenceTests(unittest.TestCase):
    """O0b B0-1: a run that fails before its claim is sent is fenced at the account, so its binding ends
    at once instead of holding every drain for 24 h (2026-11-20, the control project's migration)."""

    def test_B0_1_a_run_with_a_lease_but_no_claim_sent_is_fenced(self):
        value, run = driver(), fencing_run(state='preparing', claim_sent=False)
        value._fail(run, 'run_failed')
        calls = [call.args[2] for call in value._account_call.call_args_list]
        self.assertIn('fence_claim', calls)

    def test_B0_1_a_run_with_no_lease_and_no_claim_sent_gives_back_at_once(self):
        value, run = driver(), fencing_run(state='preparing', claim_sent=False, persistence=None)
        value._give_back = mock.Mock()
        value._fail(run, 'run_failed')
        value._give_back.assert_called_once_with(run)
        value._account_call.assert_not_called()  # nothing to fence it with; nothing was claimed

if __name__ == '__main__':
    unittest.main()


class DispatchSlotRegisterTests(unittest.TestCase):
    """O0c M11: a host with no dispatch slot left refuses a new run at registration, before any claim, so the
    run is never admitted only to meet dispatch_full; the host is replaced (exhausted) and the next run
    registers on a fresh one."""

    KEY = ('hrn_' + 'a' * 48, 1)

    def host(self, jobs):
        value = driver()
        value._runs = {}
        value._dispatch = SimpleNamespace(_jobs={number: object() for number in range(jobs)})
        value._scope = None
        value._submit_registration = mock.Mock()
        return value

    def register(self, value):
        fields = {name: None for name in project_driver._REGISTER_FIELDS}
        fields.update(purpose='register', host_client='codex-host-v1', run_id='run-1',
                      assignment={'attempt': 1}, orchestration_handle='orc_' + 'a' * 48)
        with mock.patch.object(project_driver, 'open_inputs_digest', return_value='d' * 64), \
                mock.patch.object(project_driver, 'OpenScopeContext', return_value=mock.Mock()):
            value._register(self.KEY, 'd' * 64, fields, 'codex-host-v1')

    def test_M11_past_every_dispatch_slot_the_run_is_refused_before_its_claim_and_the_host_is_replaced(self):
        with mock.patch.object(project_driver, 'DISPATCH_SLOTS', 2):
            full = self.host(2)
            with self.assertRaises(project_driver.HistoryError) as refused:
                self.register(full)
            self.assertEqual('history_service_unavailable', refused.exception.reason_code)
            self.assertEqual({}, full._runs)  # nothing registered, so nothing to claim
            full._submit_registration.assert_not_called()
            full.busy = lambda: False
            self.assertTrue(full.exhausted())  # the host is replaced; a fresh one takes the next run
            fresh = self.host(0)
            self.register(fresh)
            self.assertIn(self.KEY, fresh._runs)
