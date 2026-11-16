"""Actual signed/worker service phases; native authorization is a fixture only."""
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock

from tests import test_bridge as fixtures
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
