"""Actual signed/worker service phases; native authorization is a fixture only."""
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock

from tests import test_bridge as fixtures
from codex_agents import billing_probe_worker
from codex_agents.billing_probe_contracts import ProbeAdmissionError
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request,
)
from codex_agents.account_billing_jobs import AccountBillingJobs, BillingJobLimits
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_observation import AccountObservationOwner, ObservationContext
from codex_agents.account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.billing_probe_worker import BillingProbeWorkers, ProbeWorkerLimits, PROBE_SLOT_BYTES
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.provider_observation_contracts import ObservationInput
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'original fixed macOS signature worker guard')
class AccountBillingJobTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.admit('claude')
        self.ledger = self.fixture.ledger
        self.views = AccountOwnerViews(self.ledger)
        limits = SQLReadLimits(100_000, 1000, 2*1_048_576)
        self.claims = AccountClaimJobs(self.views, SignatureIntakeLimits(4, 2, 512, 262_144, 4),
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000), limits)
        self.observations = AccountObservationJobs(AccountObservationOwner(self.views, limits),
                                                  ObservationJobLimits(4, 8, 2, 2))
        self.workers = BillingProbeWorkers(OwnedExecutionRegistry(), ProbeWorkerLimits(2, 2*PROBE_SLOT_BYTES))
        self.billing = AccountBillingJobs(self.claims, self.observations, self.workers, BillingJobLimits(4, 4, 4))
        self.claims.bootstrap()
        self.observations.bootstrap()
        self.addCleanup(self.stop)
        self.runtime, self.token, self.allowed = 'a'*64, object(), True
        self.contexts = {self.fixture.project_id: ObservationContext(str(self.fixture.project.resolve()))}

        def authorize(request, context):
            if context is not self.token or not self.allowed or request.project_id not in self.contexts:
                return None
            return AccountAuthorization(request.project_id, self.runtime, account_request_digest(request),
                'host', ACCOUNT_OPERATIONS, 'b'*64, observation_context=self.contexts[request.project_id])

        self.authorize = authorize
        self.service = AccountService(self.runtime, self.views, authorizer=authorize,
                                      claim_jobs=self.claims, observation_jobs=self.observations)
        self.fixture.frames('claude', [fixtures.PROBE_INIT, fixtures.FAVOURABLE, fixtures.result_frame('OK')])

    def stop(self):
        # A disposable test may deliberately quarantine unknown ownership. Stop
        # real fixture resources without asserting durable account release.
        self.billing.request_close()
        self.claims.request_close()
        self.observations.request_close()
        self.workers.close()
        end = time.monotonic()+10
        while True:
            for notice in self.claims._pool.poll():
                self.claims._pool.discard(notice.handle)
            self.observations.drain()
            threads = ([s.thread for s in self.claims._pool._slots] +
                       [s.thread for s in self.observations._slots] + [s.thread for s in self.workers._slots])
            if not any(t is not None and t.is_alive() for t in threads):
                break
            self.assertLess(time.monotonic(), end, 'actual fixture workers did not stop')
            time.sleep(.005)
        self.service.close()

    def request(self, identity='probe-first', *, project=None, operation='account.billing_probe'):
        payload = ObservationInput('claude').payload() if operation == 'account.observe_provider' else {}
        return AccountRequest(1, operation, identity, project or self.fixture.project_id, self.runtime, payload)

    def begin(self, request=None):
        return self.service.begin(encode_account_request(request or self.request()), self.token)

    def until(self, condition, *, timeout=10):
        end = time.monotonic()+timeout
        while not condition():
            self.service.drive_claims()
            self.assertLess(time.monotonic(), end, 'fixed billing phases did not progress')
            time.sleep(.005)

    def take(self, ticket, request=None):
        if type(ticket) is bytes:
            return decode_account_response(ticket, request or self.request())
        received = []
        def ready():
            raw = self.service.take_claim_reply(ticket)
            if raw is None:
                return False
            received.append(raw)
            return True
        self.until(ready)
        return decode_account_response(received[0], request or self.request())

    def launches(self):
        return [i for i in self.fixture.invocations('claude') if i['argv'][0] == '-p']

    def test_RS_A_G3_the_owner_s_step_forgets_a_probe_the_sweep_expired_with_no_request(self):
        """O0b RS-A round 2 G3: the collector's sweep thread commits an expired reservation; the owner's next
        step (drive_claims, run by the owner loop whether or not a request came) forgets it and its work."""
        import threading
        owner = self.billing._owner
        context = self.contexts[self.fixture.project_id]
        original = owner.reserve(self.fixture.project_id, self.runtime, 'b'*64, 'probe-expiring', context,
                                 before_commit=lambda: True)
        queued = owner.prepare(original)
        self.assertIn(queued, self.claims._intake._jobs)
        clock = original.deadline + 1

        def sweep():
            with mock.patch.object(self.ledger, '_clock_ms', return_value=clock), \
                    mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
                swept.append(self.ledger.release_due_account_holds(limit=64))
        swept = []
        worker = threading.Thread(target=sweep)
        worker.start()
        worker.join(10)
        self.assertEqual([1], swept)
        self.assertIn(original, owner._reserved_inputs)  # untouched from the sweep's thread
        with mock.patch.object(self.claims._pool, 'offer_next', return_value=None):
            self.service.drive_claims()
        self.assertEqual(({}, {}, {}), (owner._pending, owner._reserved_inputs, owner._signatures))
        self.assertNotIn(queued, self.claims._intake._jobs)

    def sweep_expired(self, deadline):
        """O0b RS-A round 2 G9: the collector's sweep, on its own thread, with the ledger clock past `deadline`."""
        import threading

        def sweep():
            with mock.patch.object(self.ledger, '_clock_ms', return_value=deadline + 1), \
                    mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
                swept.append(self.ledger.release_due_account_holds(limit=64))
        swept = []
        worker = threading.Thread(target=sweep)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        return swept[0]

    def assertEndedWithStoredAnswer(self, ticket):
        answer = self.take(ticket)
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (answer['probe'], answer['reason_code'], answer['known']))
        self.until(lambda: not self.billing._jobs)
        owner = self.billing._owner
        self.assertEqual(({}, {}, {}, {}),
                         (owner._pending, owner._reserved_inputs, owner._signatures, self.billing._signatures))
        self.assertEqual({}, self.claims._intake._jobs)
        self.assertEqual({}, self.claims._pool._returned)
        self.assertEqual([], self.launches())
        self.assertFalse(self.claims._faulted or self.billing._faulted)

    def test_RS_A_G9_a_live_job_observing_its_identity_ends_with_the_stored_answer(self):
        """Round 2 G9 (2): the sweep closes the reservation of a job still observing its identity; the job ends
        with the committed denial (as a replay reads it), never probe_conflict or probe_not_authorized."""
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        self.assertEqual('identity', job.state)
        self.assertEqual(1, self.sweep_expired(job.reservation.deadline))
        self.assertEndedWithStoredAnswer(ticket)

    def test_RS_A_G9_a_live_job_with_queued_signature_work_ends_with_the_stored_answer(self):
        """Round 2 G9 (2): its signature work is queued (never offered); the job ends with the stored answer and
        its handle mapping goes in the same step, so no stale handle stays."""
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        with mock.patch.object(self.claims._pool, 'offer_next', return_value=None):
            self.until(lambda: job.state == 'signature')
            handle = job.signature
            self.assertIn(handle, self.claims._intake._jobs)
            self.assertEqual(1, self.sweep_expired(job.reservation.deadline))
            self.service.drive_claims()
            self.assertNotIn(handle, self.billing._signatures)
            self.assertNotIn(handle, self.claims._intake._jobs)
            self.assertIsNone(job.signature)
        self.assertEndedWithStoredAnswer(ticket)

    def test_RS_A_G9_a_signature_return_after_the_sweep_ends_with_the_stored_answer(self):
        """Round 2 G9 (2): the job's signature work returns after the sweep committed, and the return is handled
        before the owner's forget step runs; the job still ends with the stored answer."""
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        with mock.patch.object(self.claims._pool, 'offer_next', return_value=None):
            self.until(lambda: job.state == 'signature')
        self.assertIs(job.signature, self.claims._pool.offer_next())
        slots = self.claims._pool._slots
        end = time.monotonic() + 10
        while not any(s.current is not None and not s.outgoing.empty() for s in slots):
            self.assertLess(time.monotonic(), end, 'signature work did not return')
            time.sleep(.002)
        self.assertEqual(1, self.sweep_expired(job.reservation.deadline))
        self.assertEndedWithStoredAnswer(ticket)

    def test_RS_A_G9_a_failed_forget_step_is_noted_and_retried_without_faulting_the_lane(self):
        """Round 2 G9 (1): a failure inside the owner's forget step is noted in the billing lane's health, keeps
        the notice so the next step retries, and never faults the claims lane or stops the drive."""
        owner = self.billing._owner
        original = owner.reserve(self.fixture.project_id, self.runtime, 'b'*64, 'probe-expiring',
                                 self.contexts[self.fixture.project_id], before_commit=lambda: True)
        queued = owner.prepare(original)
        self.assertEqual(1, self.sweep_expired(original.deadline))
        with mock.patch.object(self.claims._pool, 'offer_next', return_value=None):
            with mock.patch.object(owner, '_forget_closed', side_effect=ProbeAdmissionError('probe_conflict')):
                self.service.drive_claims()
            self.assertFalse(self.claims._faulted or self.billing._faulted)
            view = self.billing.health.view()
            self.assertEqual(('probe_forget_failed', 'ProbeAdmissionError'),
                             (view['last_failure']['cause'], view['last_failure']['type']))
            self.assertIn(original, owner._reserved_inputs)
            self.service.drive_claims()  # retried
        self.assertEqual(({}, {}, {}), (owner._pending, owner._reserved_inputs, owner._signatures))
        self.assertNotIn(queued, self.claims._intake._jobs)
        self.take(self.begin(self.request('probe-after-failed-forget')), self.request('probe-after-failed-forget'))

    # O0b RS-A round 3 (review 040-rsa-code-r3-2): faults inside the G9 cleanup itself, not around it.

    def failing_once(self, target, name):
        """`target.name` raises on its first call, then works (a transient fault)."""
        real, calls = getattr(target, name), []

        def call(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError(f'injected {name} failure')
            return real(*args, **kwargs)
        return mock.patch.object(target, name, side_effect=call), calls

    def assertNotedNotFaulted(self):
        self.assertFalse(self.claims._faulted or self.billing._faulted)
        view = self.billing.health.view()
        self.assertEqual(('probe_forget_failed', 'RuntimeError'),
                         (view['last_failure']['cause'], view['last_failure']['type']))

    def test_RS_A_G9_a_failed_pool_cancel_while_forgetting_keeps_its_work_and_is_retried(self):
        """Round 3 (1): the pool's cancel fails inside the forget step: noted, the lane drives on, the original
        and its handle stay as the retry target, and the next step frees the queued work."""
        owner = self.billing._owner
        original = owner.reserve(self.fixture.project_id, self.runtime, 'b'*64, 'probe-expiring',
                                 self.contexts[self.fixture.project_id], before_commit=lambda: True)
        queued = owner.prepare(original)
        self.assertEqual(1, self.sweep_expired(original.deadline))
        patch, calls = self.failing_once(self.claims._pool, 'cancel')
        with mock.patch.object(self.claims._pool, 'offer_next', return_value=None), patch:
            self.service.drive_claims()
            self.assertNotedNotFaulted()
            self.assertIs(queued, owner._signatures.get(original))  # kept for the retry
            self.assertIn(queued, self.claims._intake._jobs)
            self.service.drive_claims()  # retried
        self.assertEqual([(queued,), (queued,)], calls)
        self.assertEqual(({}, {}, {}), (owner._pending, owner._reserved_inputs, owner._signatures))
        self.assertNotIn(queued, self.claims._intake._jobs)

    def test_RS_A_G9_a_failed_cancel_of_a_closed_live_job_is_noted_and_it_still_ends_with_the_stored_answer(self):
        """Round 3 (1): ending a closed live job fails (its observation's cancel raises): the failure is noted
        in this lane, never escapes the drive into the claims lane, and the job still ends with the stored
        denial once the cancel works."""
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        self.assertEqual('identity', job.state)
        self.assertEqual(1, self.sweep_expired(job.reservation.deadline))
        patch, calls = self.failing_once(self.observations, 'cancel')
        with patch:
            self.service.drive_claims()
            self.assertNotedNotFaulted()
            self.assertGreaterEqual(len(calls), 1)  # the failed attempt (a retry may follow in the same step)
            self.assertEndedWithStoredAnswer(ticket)
        self.assertGreaterEqual(len(calls), 2)  # tried again after the failure

    def test_RS_A_G9_a_job_cancelled_by_its_own_deadline_before_the_sweep_ends_with_the_stored_answer(self):
        """Round 3 (3): the job's own deadline cancels it (discarding) before the sweep commits its close; the
        answer is the stored denial, as an authenticated replay reads it, not probe_unavailable."""
        from types import SimpleNamespace
        from codex_agents import account_billing_jobs
        ticket = self.begin()
        job = self.billing._jobs[ticket]
        self.assertEqual('identity', job.state)
        late = SimpleNamespace(monotonic_ns=lambda: job.reservation.deadline_ns + 1)
        with mock.patch.object(account_billing_jobs, 'time', late):
            # Its observation's stop is not yet confirmed, so the job is still discarding when the sweep commits.
            with mock.patch.object(self.observations, 'take', return_value=None):
                self.service.drive_claims()
            self.assertEqual('discarding', job.state)
            self.assertTrue(job.cancelled)
            self.assertEqual(1, self.sweep_expired(job.reservation.deadline))
            self.assertEndedWithStoredAnswer(ticket)
        replay = self.take(self.begin())
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (replay['probe'], replay['reason_code'], replay['known']))

    def test_complete_service_phases_use_one_signature_pool_and_recover_without_reprobe(self):
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('second SQL connection')):
            ticket = self.begin()
            self.assertIs(ticket, self.begin())
            result = self.take(ticket)
        self.assertEqual('completed', result['probe'])
        self.assertFalse(result['known'])
        self.assertEqual(1, len(self.launches()))
        self.assertIs(self.claims._pool, self.billing._owner._pool)
        self.assertEqual(0, self.claims._intake.snapshot().jobs)
        self.assertEqual(0, self.workers._charged)
        known = self.take(self.begin())
        self.assertTrue(known['known'])
        self.assertEqual(result['records'], known['records'])
        self.assertEqual(1, len(self.launches()))

    def test_newer_same_project_identity_before_signature_capture_retires_unoffered_work(self):
        owner = self.billing._owner
        capture = owner.prepare
        originals, replacements = [], []

        def capture_after_new_identity(reservation):
            handle = self.billing._executions._reservations[reservation]
            prepared = self.billing._executions._jobs[handle]
            identity = json.loads(prepared.identity)
            clock = max(time.time_ns()//1_000_000, identity['observed_at_ms']+1)
            patch = mock.patch.object(self.ledger, '_clock_ms', return_value=clock)
            patch.start()
            self.addCleanup(patch.stop)
            newer = self.ledger.observe_provider(self.fixture.project_id, 'claude',
                project_root=self.fixture.project, environ=fixtures.CLEAN)[0]
            self.assertGreater(newer['observed_at_ms'], identity['observed_at_ms'])
            self.assertNotEqual(newer['record_id'], identity['record_id'])
            for field in ('account_fingerprint', 'client_path', 'client_sha256'):
                self.assertEqual(identity[field], newer[field])
            originals.append((reservation, reservation.deadline, reservation.deadline_ns))
            replacements.append(newer)
            return capture(reservation)

        with (mock.patch.object(owner, 'prepare', side_effect=capture_after_new_identity),
                mock.patch.object(self.ledger, '_check_active_capacity_locked',
                                  wraps=self.ledger._check_active_capacity_locked) as capacity,
                mock.patch.object(self.workers._registry, 'issue',
                                  wraps=self.workers._registry.issue) as issue):
            ticket = self.begin()
            with self.assertRaisesRegex(AccountError, 'probe_conflict'):
                self.take(ticket)
        self.assertEqual(1, len(replacements))
        capacity.assert_not_called()
        issue.assert_not_called()
        reservation, deadline, deadline_ns = originals[0]
        self.assertEqual(deadline, reservation.deadline)
        self.assertEqual(deadline_ns, reservation.deadline_ns)
        row = self.ledger._connection.execute(
            'SELECT state,deadline_at_ms FROM account_billing_probe_requests').fetchone()
        self.assertEqual(('reserved', deadline), tuple(row))
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.workers._charged)
        self.assertEqual(0, self.claims._intake.snapshot().jobs)
        self.assertEqual({}, self.billing._jobs)
        self.assertEqual({}, self.billing._executions._jobs)
        self.assertEqual({}, owner._pending)
        self.assertEqual({}, owner._permits)

    def test_cancel_before_admission_reaps_preparation_without_spending_bootstrap(self):
        ticket = self.begin()
        self.billing.cancel(ticket)
        with self.assertRaises(AccountError):
            self.take(ticket)
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])

    def test_full_signature_intake_waits_with_original_deadline_then_completes(self):
        intake = self.claims._intake
        occupied = [intake._reserve(self.fixture.project_id, 'claude') for _ in range(2)]
        self.addCleanup(lambda: [intake._release(h) for h in occupied if h in intake._jobs])
        with mock.patch.object(self.billing._owner, 'prepare', wraps=self.billing._owner.prepare) as prepare:
            ticket = self.begin()
            original = self.billing._jobs[ticket].reservation
            deadline = original.deadline_ns
            self.until(lambda: prepare.call_count > 0)
        self.assertEqual('preparing', self.billing._jobs[ticket].state)
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])
        for handle in occupied:
            intake._release(handle)
        result = self.take(ticket)
        self.assertEqual('completed', result['probe'])
        self.assertEqual(deadline, original.deadline_ns)
        self.assertEqual(1, len(self.launches()))
        self.assertEqual(0, self.workers._charged)

    def refuse_observations(self, times, reason='observation_unavailable'):
        """The probe's identity observation is refused `times` times, then runs as normal."""
        from codex_agents.account_observation_jobs import ObservationJobReply
        take, refused = self.observations.take, []

        def flaky(ticket, **options):
            reply = take(ticket, **options)
            if reply is not None and len(refused) < times:
                refused.append(ticket)
                return ObservationJobReply(reply.key, reply.request, None, reason, reply.before_publish)
            return reply
        self.enterContext(mock.patch.object(self.observations, 'take', side_effect=flaky))
        return refused

    def test_P1_a_probe_whose_identity_observation_is_refused_names_that_reason(self):
        # 0.3.2 requirement P1: a probe that fails before launching names the failing
        # step; an identity observation refused twice reports the observation's reason.
        refused = self.refuse_observations(2)
        with self.assertRaisesRegex(AccountError, '^observation_unavailable$'):
            self.take(self.begin())
        self.assertEqual(2, len(refused))
        self.assertEqual([], self.launches())

    def test_P2_a_probe_tries_its_identity_observation_once_more_before_failing(self):
        # 0.3.2 requirement P2: right after the services restart, a first observation can
        # be refused; the probe observes once more (it spends nothing) and then completes.
        refused = self.refuse_observations(1)
        self.assertEqual('completed', self.take(self.begin())['probe'])
        self.assertEqual(1, len(refused))
        self.assertEqual(1, len(self.launches()))

    def test_P1_a_probe_whose_preparation_fails_names_it(self):
        from codex_agents import bridge
        # Preparing the probe fails for real: it cannot create its private workspace.
        self.enterContext(mock.patch.object(bridge, 'private_workspace', side_effect=RuntimeError('unexpected')))
        # P1, refined: a failed preparation names itself, as unknown when its cleanup cannot
        # be confirmed (a workspace failure part-way): its slot is then kept, not recycled.
        with self.assertRaisesRegex(AccountError, '^probe_preparation_(failed|unknown)$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())

    def test_C9_a_probe_admitted_but_never_launched_frees_its_worker_and_settles(self):
        # 0.3.3 requirement C9 (live, 17 November): a probe's admission was committed, then a step
        # before its launch failed. The job was kept as unknown with the machine's only probe worker,
        # so every later probe waited out its deadline and answered probe_unavailable. With no
        # execution ticket nothing can have started: the probe names the failed step, its worker is
        # retired and its row settled, and the next probe is answered at once (the cooldown may refuse
        # it, never a held worker).
        real = self.billing._executions.launch
        calls = []
        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('fault after the admission commit')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'launch', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        self.until(lambda: not self.billing._jobs)
        self.assertEqual([], [s for s in self.workers._slots if s.current is not None])
        self.assertEqual([('committed', '{"probe":"denied","reason_code":"probe_step_failed","records":[]}')],
            [tuple(r) for r in self.ledger._connection.execute(
                "SELECT state, references_json FROM account_billing_probe_requests").fetchall()])
        # The admission record stays, so the cooldown still refuses at once: nothing is spent twice.
        started = time.monotonic()
        refused = self.take(self.begin(self.request('probe-after-fault')), self.request('probe-after-fault'))
        self.assertEqual(('denied', 'probe_cooldown'), (refused['probe'], refused['reason_code']))
        self.assertLess(time.monotonic()-started, 20)

    def _fail_launch_once(self):
        real, calls = self.billing._executions.launch, []
        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('fault after the admission commit')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'launch', side_effect=failing_once))

    def _admitted_rows(self):
        return self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0]

    def test_C9d_a_recovered_request_for_a_settled_probe_reads_its_denial(self):
        self._fail_launch_once()
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.until(lambda: not self.billing._jobs)
        # The same request again reads its committed denial (the CLI turns it into the error), never
        # an invalid reply or a relaunch.
        again = self.take(self.begin())
        self.assertEqual(('denied', 'probe_step_failed', True), (again['probe'], again['reason_code'], again['known']))
        self.assertEqual([], self.launches())

    def test_C9d_a_ticket_issued_during_settlement_is_never_settled(self):
        # The ticket appears after the first checks (as a reentrant callback could issue it): the
        # check just before the write refuses, and the admitted run is kept.
        self._fail_launch_once()
        # Any check made inside the settling write sees a ticket.
        self.enterContext(mock.patch.object(self.billing._executions, 'ticketed',
                                            side_effect=lambda r: self.ledger._connection.in_transaction))
        with self.assertRaisesRegex(AccountError, '^probe_unavailable$'):
            self.take(self.begin())
        self.assertEqual(1, self._admitted_rows())

    def test_C9d_settling_runs_no_callback(self):
        # No reentrant callback can run between the ticket check and the write: settling calls none.
        self._fail_launch_once()
        real_settle, real_authorize, during = self.billing._owner.settle_unlaunched, self.billing._owner._authorize, []
        state = {'settling': False}
        def settle(*args):
            state['settling'] = True
            try:
                return real_settle(*args)
            finally:
                state['settling'] = False
        def authorize(*args):
            during.append(state['settling'])
            return real_authorize(*args)
        self.enterContext(mock.patch.object(self.billing._owner, 'settle_unlaunched', side_effect=settle))
        self.enterContext(mock.patch.object(self.billing._owner, '_authorize', side_effect=authorize))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.assertNotIn(True, during)

    def test_C9d_settling_needs_no_publication_permission(self):
        # The launch fails and the caller's permission is gone too: settling publishes nothing, so
        # it still frees the worker.
        self._fail_launch_once()
        real, state = self.billing._owner._authorize, {'revoked': False}
        def authorize(*args):
            if state['revoked']:
                raise ProbeAdmissionError('probe_not_authorized')
            return real(*args)
        real_launch = self.billing._executions.launch
        def launch(*args):
            state['revoked'] = True
            return real_launch(*args)
        self.enterContext(mock.patch.object(self.billing._owner, '_authorize', side_effect=authorize))
        self.enterContext(mock.patch.object(self.billing._executions, 'launch', side_effect=launch))
        with self.assertRaises(AccountError):
            self.take(self.begin())
        self.assertTrue(state['revoked'])  # The launch was reached, then permission was gone.
        self.until(lambda: all(s.current is None for s in self.workers._slots))
        self.assertEqual(0, self._admitted_rows())

    def test_C9d_settling_works_when_the_permit_was_never_installed(self):
        # A step after the commit but before the permit is installed fails.
        real, calls = self.billing._owner._pool.discard, []
        def failing_once(handle):
            calls.append((self._admitted_rows(), dict(self.billing._owner._permits)))
            if len(calls) == 1:
                raise RuntimeError('fault before the permit')
            return real(handle)
        self.enterContext(mock.patch.object(self.billing._owner._pool, 'discard', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.assertEqual((1, {}), calls[0])  # Admitted and committed, with no permit yet.
        self.until(lambda: all(s.current is None for s in self.workers._slots))
        self.assertEqual(0, self._admitted_rows())
        self.assertEqual([], self.launches())

    def test_C9d_a_discard_whose_cleanup_fails_still_answers(self):
        from codex_agents import billing_probe_worker
        self._fail_launch_once()
        self.enterContext(mock.patch.object(billing_probe_worker, '_remove', return_value=False))
        with self.assertRaisesRegex(AccountError, '^probe_preparation_unknown$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        self.assertEqual(1, len([s for s in self.workers._slots if s.current is not None]))  # Kept.

    def test_C9d_a_transient_error_while_discarding_is_retried(self):
        self._fail_launch_once()
        real, calls = self.billing._executions.retire_discarded, []
        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('transient')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'retire_discarded', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.until(lambda: not self.billing._jobs)
        self.assertEqual([], [s for s in self.workers._slots if s.current is not None])

    def test_C9d_a_failed_cleanup_request_is_asked_again(self):
        self._fail_launch_once()
        real, calls = self.billing._executions.discard, []
        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('cleanup request failed')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'discard', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.until(lambda: not self.billing._jobs)
        self.assertEqual([], [s for s in self.workers._slots if s.current is not None])

    def test_C7_a_failed_cleanup_request_is_noted_with_its_type(self):
        self._fail_launch_once()
        real, calls = self.billing._executions.discard, []

        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('a secret path')
            return real(*args)
        notes = mock.Mock(wraps=self.billing.health.note_failure)
        self.enterContext(mock.patch.object(self.billing._executions, 'discard', side_effect=failing_once))
        self.enterContext(mock.patch.object(self.billing.health, 'note_failure', notes))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.until(lambda: not self.billing._jobs)
        noted = [(call.args[1], type(call.args[2]).__name__) for call in notes.call_args_list]
        self.assertIn(('probe_cleanup_failed', 'OSError'), noted)
        failure = self.billing.health.view()['last_failure']  # round 3: never replaced by an older reason
        self.assertEqual(('probe_cleanup_failed', 'OSError'), (failure['cause'], failure['type']))

    def test_C6_a_probe_worker_held_by_a_kept_billing_job_is_held_in_the_probe_lane(self):
        from types import SimpleNamespace
        from codex_agents import lane_health
        handle, token = object(), lane_health.new_token()
        self.workers._jobs[handle] = SimpleNamespace(token=token)
        self.addCleanup(self.workers._jobs.pop, handle, None)
        kept_job = SimpleNamespace(state='unknown', preparation='its preparation', reason=None, noted=True,
                                   token=lane_health.new_token())
        self.billing._jobs[object()] = kept_job
        self.enterContext(mock.patch.object(self.billing._executions, 'worker_of', return_value=handle))
        self.billing._publish_health()
        view = self.workers.health.view()
        self.assertEqual(1, view['kept'])
        self.assertIs(int, type(view['held_since_ms']))
        self.billing._jobs = {ticket: job for ticket, job in self.billing._jobs.items() if job is not kept_job}
        self.billing._publish_health()  # the kept parent is gone: the worker is no longer held by it
        view = self.workers.health.view()
        self.assertEqual((0, None), (view['kept'], view['held_since_ms']))

    def test_C9d_a_cleanup_request_that_always_fails_still_answers(self):
        self._fail_launch_once()
        discard = self.enterContext(mock.patch.object(self.billing._executions, 'discard',
                                                      side_effect=RuntimeError('cleanup request failed')))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        # Asked exactly three times, then kept: the job, its preparation and its worker stay held,
        # because the cleanup was never confirmed, and nothing asks again.
        for _ in range(20):
            self.service.drive_claims()
        self.assertEqual(3, discard.call_count)
        kept = list(self.billing._jobs.values())
        self.assertEqual(['unknown'], [j.state for j in kept])
        self.assertIsNotNone(kept[0].preparation)
        self.assertEqual(1, len([s for s in self.workers._slots if s.current is not None]))

    def test_C9d_a_committed_denial_whose_step_then_fails_finishes(self):
        # A pass, then a second probe inside the cooldown is denied and committed; a step after that
        # commit fails. The job still finishes and is dropped, holding no project place.
        self.take(self.begin())
        real, calls = self.billing._owner._pool.discard, []
        def failing_once(handle):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('fault after a committed denial')
            return real(handle)
        self.enterContext(mock.patch.object(self.billing._owner._pool, 'discard', side_effect=failing_once))
        with self.assertRaises(AccountError):
            self.take(self.begin(self.request('denied')), self.request('denied'))
        self.until(lambda: not self.billing._jobs)
        self.assertEqual({}, self.billing._owner._pending)
        # The second request's row is final: committed as reused (fresh billing) or denied (cooldown),
        # never admitted and never aborted. Either way it is a committed row that is not admitted.
        states = [r[0] for r in self.ledger._connection.execute(
            "SELECT references_json FROM account_billing_probe_requests WHERE state='committed'")]
        self.assertEqual(2, len(states), states)
        self.assertTrue(any('"probe":"reused"' in r or '"probe":"denied"' in r for r in states), states)
        self.assertEqual(0, self._admitted_rows())

    def test_C9d_a_worker_held_by_a_kept_job_counts_as_stuck(self):
        # A launched probe's completion fails, so its job is kept (its model ran) with its worker; the
        # other worker slot has faulted. No worker can free: the next probe says so at once.
        self.enterContext(mock.patch.object(self.billing._executions, 'complete',
                                            side_effect=RuntimeError('completion failed')))
        with self.assertRaises(AccountError):
            self.take(self.begin())
        self.assertEqual(1, len([s for s in self.workers._slots if s.current is not None]))
        for slot in self.workers._slots:
            if slot.current is None:
                slot.fault = True
        started = time.monotonic()
        with self.assertRaisesRegex(AccountError, '^probe_worker_unavailable$'):
            self.take(self.begin(self.request('after-kept')), self.request('after-kept'))
        self.assertLess(time.monotonic()-started, 20)

    def test_C9d_a_kept_job_whose_worker_was_retired_does_not_fail_other_probes(self):
        # The completed probe's reply cannot be authorized after its worker was retired, so its job
        # is kept with a handle that no longer names a worker. A later probe waiting for a busy
        # worker is not failed by that stale handle.
        real = self.billing._executions.complete
        def complete_then_fail(*args):
            real(*args)
            raise RuntimeError('reply could not be authorized')
        with mock.patch.object(self.billing._executions, 'complete', side_effect=complete_then_fail):
            with self.assertRaises(AccountError):
                self.take(self.begin())
        kept = [j for j in self.billing._jobs.values() if j.state == 'unknown']
        self.assertEqual(1, len(kept))
        self.assertNotIn(kept[0].preparation, self.billing._executions._jobs)
        held = self.enterContext(mock.patch.object(self.workers, 'prepare',
            side_effect=billing_probe_worker.ProbeWorkerError('probe_worker_full')))
        _ticket = self.begin(self.request('waits'))  # Held open: the model stays busy.
        self.until(lambda: held.called)
        for _ in range(20):
            self.service.drive_claims()
        job = next(j for j in self.billing._jobs.values() if j.key[3] == 'waits')
        self.assertEqual('prepare', job.state)  # Still waiting for a worker, not failed.

    def test_C9b_a_failed_step_before_launch_frees_the_worker(self):
        # 0.3.3 requirement C9b: a step of an unstarted probe fails (here reading its preparation's
        # state). It names the failed step and frees its worker; nothing is kept or launched.
        real = self.billing._executions.snapshot
        calls = []
        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('a step failed')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'snapshot', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        self.until(lambda: not self.billing._jobs)
        self.assertEqual([], [s for s in self.workers._slots if s.current is not None])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])
        self.take(self.begin(self.request('probe-after-step')), self.request('probe-after-step'))

    def test_H1a_a_failed_step_is_noted_without_a_fault_and_the_next_probe_is_still_taken(self):
        real = self.billing._executions.snapshot
        calls = []

        def failing_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('a secret step detail')
            return real(*args)
        self.enterContext(mock.patch.object(self.billing._executions, 'snapshot', side_effect=failing_once))
        with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
            self.take(self.begin())
        self.until(lambda: not self.billing._jobs)
        view = self.billing.health.view()
        self.assertEqual((False, 0, 0), (view['faulted'], view['kept'], view['busy']))
        self.assertEqual(('probe_step_failed', 'RuntimeError'),
                         (view['last_failure']['cause'], view['last_failure']['type']))
        self.assertNotIn('secret', repr(view))
        self.assertFalse(self.billing._faulted)  # instrumentation never touches the operational flag
        self.take(self.begin(self.request('probe-after-health')), self.request('probe-after-health'))

    def test_C9c_a_probe_with_every_worker_lost_says_so_at_once(self):
        # 0.3.3 requirement C9c: every worker slot has faulted, so no worker can free without a
        # restart. The probe says so at once, launches nothing, and does not wait out its deadline.
        for slot in self.workers._slots:
            slot.fault = True
        started = time.monotonic()
        with self.assertRaisesRegex(AccountError, '^probe_worker_unavailable$'):
            self.take(self.begin())
        self.assertLess(time.monotonic()-started, 20)
        self.assertEqual([], self.launches())

    def test_C9c_a_probe_with_every_worker_held_unknown_says_so_at_once(self):
        # 0.3.3 requirement C9c: both workers are kept because their workspaces could not be removed
        # (P1: kept, not recycled). The next probe says no worker is available, at once.
        from codex_agents import bridge
        with mock.patch.object(bridge, 'private_workspace', side_effect=RuntimeError('unexpected')):
            for n in range(len(self.workers._slots)):
                with self.assertRaisesRegex(AccountError, '^probe_preparation_(failed|unknown)$'):
                    self.take(self.begin(self.request(f'held-{n}')), self.request(f'held-{n}'))
        if any(s.current is None for s in self.workers._slots):
            self.skipTest('preparation failed cleanly here, so no worker is held')
        started = time.monotonic()
        with self.assertRaisesRegex(AccountError, '^probe_worker_unavailable$'):
            self.take(self.begin(self.request('after-held')), self.request('after-held'))
        self.assertLess(time.monotonic()-started, 20)

    def test_a_probe_when_every_slot_is_taken_says_busy_and_launches_nothing(self):
        # Both of the account's slots are held (for example by runs an earlier version
        # left): the probe is refused as busy, not as unavailable.
        from codex_agents.account_claim_contracts import ClaimError
        with mock.patch.object(self.ledger, '_check_active_capacity_locked', side_effect=ClaimError('claim_busy')), \
                self.assertRaisesRegex(AccountError, '^probe_busy$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])

    def test_a_probe_whose_admission_history_does_not_fit_says_so_and_launches_nothing(self):
        from codex_agents.admission_inputs import AdmissionCaptureError
        with mock.patch.object(self.claims._intake._views, 'capture_admission_inputs',
                               side_effect=AdmissionCaptureError('admission_capture_limit')), \
                self.assertRaisesRegex(AccountError, '^admission_capture_limit$'):
            self.take(self.begin())
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])

    def test_cancel_while_signature_intake_is_full_retires_unadmitted_preparation(self):
        intake = self.claims._intake
        occupied = [intake._reserve(self.fixture.project_id, 'claude') for _ in range(2)]
        self.addCleanup(lambda: [intake._release(h) for h in occupied if h in intake._jobs])
        with mock.patch.object(self.billing._owner, 'prepare', wraps=self.billing._owner.prepare) as prepare:
            ticket = self.begin()
            self.until(lambda: prepare.call_count > 0)
        self.billing.cancel(ticket)
        with self.assertRaises(AccountError):
            self.take(ticket)
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.workers._charged)
        self.assertEqual({}, self.billing._jobs)
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_held_model_does_not_block_another_project_observation_or_health(self):
        self.fixture.frames('claude', [fixtures.PROBE_INIT, {'sleep': 3}, fixtures.FAVOURABLE, fixtures.result_frame('OK')])
        ticket = self.begin()
        self.until(lambda: self.billing._jobs[ticket].state == 'offered'
                   and self.billing._jobs[ticket].supervisor.snapshot()['state'] == 'active')
        second_root = self.fixture.project.parent/'second-project'
        second_root.mkdir()
        second = self.ledger.register_project(second_root)
        self.contexts[second] = ObservationContext(str(second_root))
        request = self.request('second-identity', project=second, operation='account.observe_provider')
        start = time.monotonic()
        observed = self.take(self.begin(request), request)
        self.assertLess(time.monotonic()-start, 2.5)
        self.assertEqual('claude', observed['provider'])
        health = self.request('second-health', project=second, operation='account.health')
        self.assertTrue(decode_account_response(self.service.handle(encode_account_request(health), self.token), health)['transport_alive'])
        self.assertFalse(self.billing._jobs[ticket].supervisor.snapshot()['worker_returned'])
        self.take(ticket)

    def test_default_denial_and_revoked_context_start_no_observation_or_model(self):
        default = AccountService(self.runtime, self.views, claim_jobs=self.claims, observation_jobs=self.observations)
        request = self.request()
        with mock.patch.object(self.billing, 'submit', side_effect=AssertionError('unauthorized work')):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(default.begin(encode_account_request(request), self.token), request)
        self.allowed = False
        with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
            decode_account_response(self.begin(), request)
        self.assertEqual([], self.launches())

    def test_failed_probe_reply_retains_restriction_and_known_request_never_relaunches(self):
        self.fixture.frames('claude', [fixtures.PROBE_INIT, fixtures.OVERAGE, {'sleep': 3}])
        result = self.take(self.begin())
        self.assertEqual('probe_incomplete', result['reason_code'])
        self.assertTrue(any(r['record_type'] == 'provider_billing' and r['paid_usage_usable'] is True
                            for r in result['records']))
        self.assertEqual(1, len(self.launches()))
        self.assertTrue(self.take(self.begin())['known'])
        self.assertEqual(1, len(self.launches()))

    def test_service_reply_contains_no_private_answer(self):
        self.fixture.frames('claude', [fixtures.PROBE_INIT, fixtures.FAVOURABLE, fixtures.result_frame('PRIVATE-ANSWER')])
        result = self.take(self.begin())
        self.assertNotIn('PRIVATE-ANSWER', json.dumps(result))

    def test_commit_survives_local_retirement_fault_and_known_recovery_does_not_relaunch(self):
        ticket = self.begin()
        with mock.patch.object(self.workers._registry, 'acknowledge_release',
                               side_effect=RuntimeError('PRIVATE-POST-COMMIT-FAULT')):
            # Requirement P1 (0.3.2): an unexpected fault in a probe step is named.
            with self.assertRaisesRegex(AccountError, '^probe_step_failed$'):
                self.take(ticket)
        self.assertEqual('committed', self.ledger._connection.execute(
            'SELECT state FROM account_billing_probe_requests').fetchone()[0])
        recovered = self.take(self.begin())
        self.assertTrue(recovered['known'])
        self.assertEqual('completed', recovered['probe'])
        self.assertEqual(1, len(self.launches()))

    def test_owner_budget_rotates_projects_fairly_when_one_project_queues_more_requests(self):
        for index in range(3):
            self.begin(self.request('loaded-'+str(index)))
        second_root = self.fixture.project.parent/'fair-second-project'
        second_root.mkdir()
        second = self.ledger.register_project(second_root)
        self.contexts[second] = ObservationContext(str(second_root))
        self.begin(self.request('other-project', project=second))
        scheduled = []
        with mock.patch.object(self.billing, '_step', side_effect=lambda ticket, job: scheduled.append(job.key)):
            self.billing.drive()
            self.billing.drive()
        self.assertEqual([self.fixture.project_id, second]*2, [key[0] for key in scheduled])
        self.assertNotEqual(scheduled[0][3], scheduled[2][3])
