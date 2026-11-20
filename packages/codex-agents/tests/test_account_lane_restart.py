"""O0c M8-M10: an unknown execution faults its account lane, and the account restarts itself (H1c).

ANALYSIS-0.4.0-lifetime-caps.md Part 2: two unconfirmed signature executions stopped claims and
opens machine-wide (M8), two billing jobs left `unknown` gave `probe_full` for good (M9), and the one
probe worker kept after an unknown execution stopped probes machine-wide (M10). None of them faulted
its lane, so the self-restart never fired, and held work in one lane vetoed another lane's restart.

Requirement: an unknown execution in any account lane faults that lane (noted in its health), so the
self-restart restarts the account service at its next check; held unknown work never blocks another
lane's restart; after the restart claims, opens and probes work again with no owner action; and no
slot is freed while its child may still run: the restart waits until every child the lanes started
is proven gone, by the same evidence a normal release uses.

A restart here is what the collector does: the old account objects are abandoned with their threads,
and new ones are built on the same ledger.
"""
import contextlib
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_jobs as claim_tests
from tests import test_bridge as bridge_fixtures
from tests.test_account_restart import IDLE, NOW, lane, lanes

from codex_agents import account_billing_admission, account_host, account_restart, bridge, billing_probe_worker
from codex_agents import client_runtime
from codex_agents import signature_execution as execution
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountError, AccountRequest, account_request_digest, decode_account_response,
    encode_account_request,
)
from codex_agents.account_billing_jobs import AccountBillingJobs, BillingJobLimits
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_observation import AccountObservationOwner, ObservationContext
from codex_agents.account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.billing_probe_worker import BillingProbeWorkers, ProbeWorkerLimits, PROBE_SLOT_BYTES
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits

SIGNATURE_WORKER = claim_tests.SIGNATURE_WORKER
WORKER_LIMITS = SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000)


def notes_of(**views):
    """Every lane healthy except the ones given (each a real lane's `view()`)."""
    return lanes(**views)


class LaneRestartDecisionTests(unittest.TestCase):
    def check(self, notes, children=lambda: []):
        return account_restart.check(lambda: notes, lambda: IDLE, None, now_ms=NOW, read_children=children)

    def test_M8_held_unknown_work_in_another_lane_never_blocks_a_restart(self):
        # The signature lane is stuck; billing and the probe worker keep unknown work of their own.
        notes = notes_of(signature=lane(faulted=True, kept=2), billing=lane(faulted=True, kept=2),
                         probe_worker=lane(faulted=True, kept=1), claims=lane(kept=2))
        decision = self.check(notes)
        self.assertEqual(('restart', 'signature'), (decision['action'], decision['lane']))
        # A stuck lane whose own kept work is elsewhere (H1c's C3 case) restarts as well.
        decision = self.check(notes_of(ingress=lane(faulted=True), billing=lane(kept=1)))
        self.assertEqual(('restart', 'ingress'), (decision['action'], decision['lane']))

    def test_M8_work_still_running_in_any_lane_still_waits(self):
        decision = self.check(notes_of(signature=lane(faulted=True, kept=1), claims=lane(busy=1)))
        self.assertEqual(('wait', {'claims': 1}), (decision['action'], decision['held']['lanes']))
        decision = self.check(notes_of(signature=lane(faulted=True, kept=1), observations=lane(queued=2)))
        self.assertEqual(('wait', {'observations': 2}), (decision['action'], decision['held']['lanes']))

    def test_M8_the_restart_waits_while_a_child_may_still_run_and_names_its_lane(self):
        notes = notes_of(signature=lane(faulted=True, kept=1))
        decision = self.check(notes, children=lambda: ['signature'])
        self.assertEqual(('wait', ['signature']), (decision['action'], decision['held']['children']))

        def broken():
            raise RuntimeError('cannot say')
        for children in (broken, None, lambda: 'signature', lambda: ['not_a_lane']):
            with self.subTest(children=children):
                decision = account_restart.check(lambda: notes, lambda: IDLE, None, now_ms=NOW,
                                                 **({} if children is None else {'read_children': children}))
                self.assertEqual('wait', decision['action'])
                self.assertEqual(['unknown'], decision['held']['children'])
        self.assertEqual('restart', self.check(notes)['action'])

    def test_M8_the_children_are_only_asked_about_once_a_restart_is_due(self):
        asked = []
        self.check(notes_of(), children=lambda: asked.append(1) or [])
        self.check(notes_of(signature=lane(faulted=True), claims=lane(busy=1)), children=lambda: asked.append(1) or [])
        self.assertEqual([], asked)

    def test_M8_status_names_the_lane_whose_child_may_still_run(self):
        decision = {'action': 'wait', 'lane': 'signature', 'cause': 'signature_execution_unconfirmed',
                    'held': {'run_slots': [], 'probes': [], 'lanes': {}, 'children': ['signature', 'probe_worker']}}
        text = ' '.join(client_runtime._restart_attention(decision))
        self.assertIn('signature_execution_unconfirmed', text)
        self.assertIn('a child process of the signature, probe worker lane(s) may still be running', text)


class Unproven:
    """A retained execution context that proves nothing (as a corrupted or foreign one would)."""


@unittest.skipUnless(SIGNATURE_WORKER, 'needs the macOS sandbox-exec policy or Linux')
class SignatureLaneRestartTests(unittest.TestCase):
    """M8 with real signature workers and claims (the claims harness of test_account_claim_jobs)."""

    def setUp(self):
        self.claims = claim_tests.AccountClaimJobTests('test_C5_a_submitted_claim_is_busy_in_the_lane_notes')
        self.claims.setUp()
        # Its tearDown drains the pool, which a stuck worker never lets happen: the stuck pool is
        # abandoned here as the collector abandons it, and the fixture's own cleanups still run.
        self.addCleanup(lambda: self.assertTrue(self.claims.doCleanups()))

    def drive_until(self, jobs, condition, timeout=20):
        end = time.monotonic() + timeout
        while not condition():
            jobs.drive()
            self.assertLess(time.monotonic(), end, 'the claims lane did not progress')
            time.sleep(.005)

    def two_unconfirmed(self):
        """Two claims, one per project, whose executions end with their cleanup unconfirmed (the helper
        root reaped, but the confirmation failed then): both signature workers stay held."""
        c = self.claims
        other, launch = c.other_project()
        real, failing = execution._cleanup_process, [True]
        with mock.patch.object(execution, '_cleanup_process',
                               side_effect=lambda context: False if failing[0] else real(context)):
            c.begin()
            c.begin(c.request('other', project=other, launch=launch))
            self.drive_until(c.jobs, lambda: c.jobs._pool.health.view()['kept'] >= 2)
            failing[0] = False
        c.jobs.drive()
        self.addCleanup(self.remove_scratch, c.jobs._pool)
        return other, launch

    @staticmethod
    def remove_scratch(pool):
        import shutil
        for slot in pool._slots:
            context = getattr(slot, 'retained', None)
            scratch = getattr(context, 'scratch', None)
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    def restarted(self):
        """New claim jobs and service on the same ledger, as the next collector builds them."""
        c = self.claims
        c.ledger._signature_job_intake = None  # the next collector's ledger has no intake yet
        views = AccountOwnerViews(c.ledger)
        jobs = AccountClaimJobs(views, SignatureIntakeLimits(2, 2, 128, 262_144, 2), WORKER_LIMITS,
                                SQLReadLimits(10_000, 1000, 1_048_576), allow_unleased_claim_reference=True)
        jobs.bootstrap()

        def authorize(request, context):
            if context is not c.token:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                        'host', ACCOUNT_OPERATIONS, 'a' * 64)
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, views, authorizer=authorize, claim_jobs=jobs,
                                 allow_unleased_claim_reference=True)

        def stop():
            jobs.request_close()
            end = time.monotonic() + 10
            while not jobs.drain():
                self.assertLess(time.monotonic(), end, 'the restarted workers did not drain')
                time.sleep(.002)
            service.close()
        self.addCleanup(stop)
        return jobs, service

    def pump(self, jobs, service, ticket):
        end = time.monotonic() + 20
        while True:
            jobs.drive()
            raw = service.take_claim_reply(ticket)
            if raw is not None:
                return raw
            self.assertLess(time.monotonic(), end, 'the claim did not finish')
            time.sleep(.002)

    def test_M8_two_unconfirmed_signature_executions_restart_the_account_and_a_claim_succeeds(self):
        c = self.claims
        other, launch = self.two_unconfirmed()
        pool = c.jobs._pool
        signature, claims = pool.health.view(), c.jobs.health.view()
        # The lane is faulted (noted in its health), its two jobs kept; the claims waiting on them are
        # kept too, never counted as running work.
        self.assertEqual((True, 2, 0), (signature['faulted'], signature['kept'], signature['busy']))
        self.assertEqual('signature_execution_unconfirmed', signature['last_failure']['cause'])
        self.assertEqual((2, 0), (claims['kept'], claims['busy']))
        # A claim that could only wait for a worker that never frees is refused at once (retryable),
        # so it never holds the restart.
        request = c.request('third')
        raw = c.begin(request)
        self.assertIsInstance(raw, bytes)
        with self.assertRaises(AccountError) as refused:
            decode_account_response(raw, request)
        self.assertEqual('claim_unavailable', refused.exception.reason_code)
        # The self-restart's first check restarts it: the children both executions started are proven
        # gone (each helper root reaped inside the no-fork boundary).
        self.assertEqual([], account_host.unended_children(pool, None))
        notes = notes_of(signature=pool.health.view(), claims=c.jobs.health.view())
        decision = account_restart.check(lambda: notes, lambda: IDLE, None, now_ms=NOW,
                                         read_children=lambda: account_host.unended_children(pool, None))
        self.assertEqual(('restart', 'signature', 'signature_execution_unconfirmed'),
                         (decision['action'], decision['lane'], decision['cause']))
        # After the restart both projects claim again, with no owner action.
        jobs, service = self.restarted()
        for request in (c.request('after-restart'), c.request('other-after-restart', project=other, launch=launch)):
            with self.subTest(project=request.project_id):
                ticket = service.begin(encode_account_request(request), c.token)
                self.assertEqual('claimed', decode_account_response(self.pump(jobs, service, ticket), request)['state'])

    def test_M8_an_execution_whose_child_cannot_be_proven_gone_holds_the_restart(self):
        c = self.claims
        with mock.patch.object(execution, '_execute_signature_job',
                               side_effect=execution.SignatureExecutionUnconfirmed(Unproven())):
            c.begin()
            self.drive_until(c.jobs, lambda: c.jobs._pool.health.view()['kept'] >= 1)
        c.jobs.drive()  # the owner sees it unconfirmed
        pool = c.jobs._pool
        self.assertTrue(pool.health.view()['faulted'])
        self.assertEqual(['signature'], account_host.unended_children(pool, None))
        notes = notes_of(signature=pool.health.view(), claims=c.jobs.health.view())
        decision = account_restart.check(lambda: notes, lambda: IDLE, None, now_ms=NOW,
                                         read_children=lambda: account_host.unended_children(pool, None))
        self.assertEqual(('wait', ['signature']), (decision['action'], decision['held']['children']))

    def test_M8_a_spawn_that_may_have_started_a_child_is_never_proven_gone(self):
        context = execution._Context(None, None, 1, threading.Event(), 1, 1)
        context.spawn_ambiguous = True
        self.assertFalse(execution.children_ended(context))
        context.spawn_ambiguous = False
        self.assertTrue(execution.children_ended(context))  # nothing was started, or every root was reaped
        context.process = object()  # a root this execution started and never confirmed
        self.assertFalse(execution.children_ended(context))

    def test_M8_an_ordinary_execution_in_flight_is_not_proven_gone(self):
        c = self.claims
        gate, started, patcher = c.gate_first_project()
        with patcher:
            c.begin()
            c.jobs.drive()
            self.assertTrue(started.wait(5))
            self.assertEqual(['signature'], account_host.unended_children(c.jobs._pool, None))
            gate.set()


@unittest.skipUnless(SIGNATURE_WORKER, 'original fixed macOS signature worker guard')
class BillingLaneRestartTests(unittest.TestCase):
    """M9 and M10 with the real billing phases and a fake Claude (the test_account_billing_jobs fixture)."""

    def setUp(self):
        self.fixture = bridge_fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.admit('claude')
        self.ledger = self.fixture.ledger
        self.runtime, self.token = 'a' * 64, object()
        self.contexts = {self.fixture.project_id: ObservationContext(str(self.fixture.project.resolve()))}
        self.fixture.frames('claude', [bridge_fixtures.PROBE_INIT, bridge_fixtures.FAVOURABLE,
                                       bridge_fixtures.result_frame('OK')])
        self.services = self.build()

    def build(self):
        """The account's billing phases as AccountServiceHost builds them: two probe jobs per project (M9's
        limit) and one probe worker per machine (M10's), both scaled to the test."""
        # A restarted collector opens its own ledger, with no account owners registered on it yet.
        for owner in ('_signature_job_intake', '_observation_owner', '_billing_admission_owner'):
            setattr(self.ledger, owner, None)
        views = AccountOwnerViews(self.ledger)
        limits = SQLReadLimits(100_000, 1000, 2 * 1_048_576)
        claims = AccountClaimJobs(views, SignatureIntakeLimits(4, 2, 512, 262_144, 4), WORKER_LIMITS, limits)
        observations = AccountObservationJobs(AccountObservationOwner(views, limits), ObservationJobLimits(4, 8, 2, 2))
        workers = BillingProbeWorkers(OwnedExecutionRegistry(), ProbeWorkerLimits(2, 2 * PROBE_SLOT_BYTES))
        billing = AccountBillingJobs(claims, observations, workers, BillingJobLimits(4, 2, 4))
        claims.bootstrap()
        observations.bootstrap()

        def authorize(request, context):
            if context is not self.token or request.project_id not in self.contexts:
                return None
            return AccountAuthorization(request.project_id, self.runtime, account_request_digest(request),
                                        'host', ACCOUNT_OPERATIONS, 'b' * 64,
                                        observation_context=self.contexts[request.project_id])
        service = AccountService(self.runtime, views, authorizer=authorize, claim_jobs=claims,
                                 observation_jobs=observations)
        services = {'claims': claims, 'observations': observations, 'workers': workers, 'billing': billing,
                    'service': service}
        self.addCleanup(self.abandon, services)
        return services

    def abandon(self, services):
        """Stop what can stop; a stuck worker is left as the collector leaves it."""
        for name in ('billing', 'claims', 'observations'):
            try:
                services[name].request_close()
            except Exception:
                pass
        try:
            services['workers'].close()
        except Exception:
            pass
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                for notice in services['claims']._pool.poll():
                    services['claims']._pool.discard(notice.handle)
                services['observations'].drain()
            except Exception:
                break
            if not any(s.thread is not None and s.thread.is_alive() for s in services['observations']._slots):
                break
            time.sleep(.005)
        services['service'].close()

    def request(self, identity):
        return AccountRequest(1, 'account.billing_probe', identity, self.fixture.project_id, self.runtime, {})

    def take(self, identity, services=None):
        services = services or self.services
        service, request = services['service'], self.request(identity)
        ticket = service.begin(encode_account_request(request), self.token)
        end = time.monotonic() + 30
        while type(ticket) is not bytes:
            raw = service.take_claim_reply(ticket)
            if raw is not None:
                ticket = raw
                break
            service.drive_claims()
            self.assertLess(time.monotonic(), end, 'the probe did not answer')
            time.sleep(.005)
        return decode_account_response(ticket, request)

    def refusal(self, identity, services=None):
        with self.assertRaises(AccountError) as refused:
            self.take(identity, services)
        return refused.exception.reason_code

    def drive_until(self, condition, services=None, timeout=20):
        services = services or self.services
        end = time.monotonic() + timeout
        while not condition():
            services['service'].drive_claims()
            self.assertLess(time.monotonic(), end, 'the billing lane did not progress')
            time.sleep(.005)

    def lane_notes(self, services=None):
        services = services or self.services
        return notes_of(signature=services['claims']._pool.health.view(), claims=services['claims'].health.view(),
                        observations=services['observations'].health.view(),
                        billing=services['billing'].health.view(), probe_worker=services['workers'].health.view())

    def assert_restarts(self, lane_name):
        services = self.services
        children = account_host.unended_children(services['claims']._pool, services['workers'])
        self.assertEqual([], children)
        decision = account_restart.check(lambda: self.lane_notes(), self.ledger.machine_idle, None, now_ms=NOW,
                                         read_children=lambda: children)
        self.assertEqual(('restart', lane_name), (decision['action'], decision['lane']), decision)

    def admitted(self):
        return self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0]

    def test_M9_two_unknown_billing_jobs_in_a_project_restart_the_account_and_a_probe_succeeds(self):
        # Each preparation made its workspace, then failed, and the workspace could not be removed:
        # the job is kept unknown with its worker (P1: kept, not recycled).
        with mock.patch.object(bridge, '_private_file', side_effect=RuntimeError('unexpected')), \
                mock.patch.object(billing_probe_worker, '_remove', return_value=False):
            for identity in ('held-1', 'held-2'):
                self.assertEqual('probe_preparation_unknown', self.refusal(identity))
        # Today: the project's two jobs are taken for good.
        self.assertEqual('probe_full', self.refusal('third'))
        billing = self.services['billing'].health.view()
        self.assertEqual((True, 2), (billing['faulted'], billing['kept']))
        self.assertTrue(self.services['workers'].health.view()['faulted'])
        self.assert_restarts('billing')
        self.services = self.build()
        answer = self.take('after-restart')
        self.assertEqual('completed', answer['probe'])

    def fail_workspace_removal_after_the_probe_ran(self, stack):
        """The probe ran and its child was confirmed gone, then its workspace could not be removed."""
        real_run, ran = bridge._run_claude_billing_probe, []

        def run(*args, **kwargs):
            outcome = real_run(*args, **kwargs)
            ran.append(1)
            return outcome
        real_remove = billing_probe_worker._remove

        def remove(workspace):
            if ran:
                ran.clear()
                return False
            return real_remove(workspace)
        stack.enter_context(mock.patch.object(bridge, '_run_claude_billing_probe', side_effect=run))
        stack.enter_context(mock.patch.object(billing_probe_worker, '_remove', side_effect=remove))

    def fail_handoff_after_issue(self, stack):
        """The execution ticket was issued, then handing it to the worker failed: no child started."""
        registry = self.services['workers']._registry
        stack.enter_context(mock.patch.object(registry, '_handoff_worker', side_effect=RuntimeError('handoff')))

    def unknown_probe(self, make_unknown):
        with contextlib.ExitStack() as stack:
            make_unknown(stack)
            reason = self.refusal('held')
        self.assertIn(reason, {'probe_step_failed', 'probe_unavailable', 'probe_worker_failed'})
        self.assertTrue(self.services['workers'].health.view()['faulted'])

    def restart_after_unknown_probe(self, make_unknown):
        self.unknown_probe(make_unknown)
        # Its child is proven gone (or never started), so its admitted row is settled: it no longer
        # holds the machine's probe capacity or vetoes the restart.
        self.drive_until(lambda: self.admitted() == 0)
        self.assertEqual([], self.ledger.machine_idle()['probes'])
        self.assert_restarts('billing')
        self.services = self.build()
        # Two product rules, not held slots, are passed here: the five-minute cooldown after an admitted
        # probe (the next test shows it still applies), and the account's one bootstrap admission, which
        # the lost probe spent without keeping its records (an earlier run's billing reading stands in).
        self.seed_billing(minutes_ago=10)
        with mock.patch.object(account_billing_admission, 'PROBE_COOLDOWN_MS', 0):
            answer = self.take('after-restart')
            self.assertEqual('completed', answer['probe'], answer)

    def seed_billing(self, *, minutes_ago):
        from codex_agents.providers import claude_rate_limit_records
        identity = self.ledger._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='provider_identity' "
            "ORDER BY rowid DESC LIMIT 1", (self.fixture.project_id,)).fetchone()[0]
        fingerprint = __import__('json').loads(bytes(identity))['account_fingerprint']
        observed = time.time_ns() // 1_000_000 - minutes_ago * 60_000
        records = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'},
            account_fingerprint=fingerprint, observed_at_ms=observed)
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=observed)

    def test_M10_an_unknown_probe_execution_restarts_the_account_and_a_probe_succeeds(self):
        self.restart_after_unknown_probe(self.fail_workspace_removal_after_the_probe_ran)

    def test_M10_a_ticket_issued_but_never_handed_off_restarts_the_account_and_a_probe_succeeds(self):
        self.restart_after_unknown_probe(self.fail_handoff_after_issue)

    def test_M10_the_cooldown_still_applies_after_the_restart(self):
        self.unknown_probe(self.fail_workspace_removal_after_the_probe_ran)
        self.drive_until(lambda: self.admitted() == 0)
        self.services = self.build()
        answer = self.take('after-restart')
        self.assertEqual(('denied', 'probe_cooldown'), (answer['probe'], answer['reason_code']))

    def test_M10_a_probe_whose_child_may_still_run_is_never_settled_and_holds_the_restart(self):
        from codex_agents.process_tree import ProcessTree
        real = ProcessTree.cleanup_after_owned_wait

        def unconfirmed(tree, process):
            real(tree, process)
            return False  # the child's tree was not confirmed empty

        def make_unknown(stack):
            stack.enter_context(mock.patch.object(ProcessTree, 'cleanup_after_owned_wait', autospec=True,
                                                  side_effect=unconfirmed))
        self.unknown_probe(make_unknown)
        for _ in range(50):
            self.services['service'].drive_claims()
        self.assertEqual(1, self.admitted())
        self.assertEqual(['probe_worker'],
                         account_host.unended_children(self.services['claims']._pool, self.services['workers']))
        decision = account_restart.check(lambda: self.lane_notes(), self.ledger.machine_idle, None, now_ms=NOW,
                                         read_children=lambda: ['probe_worker'])
        self.assertEqual('wait', decision['action'])

if __name__ == '__main__':
    unittest.main()
