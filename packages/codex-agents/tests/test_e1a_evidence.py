"""E1a-T2/T3/T4: real account-ledger boundaries; synthetic enrollment/providers.

Scenario layer: the reused fixtures start real signature processes. No fixture
or persistence work runs at import. Negative-control recipes are in the unit STATUS.
"""
import json
import threading
import time
import types
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_account_api as api_tests
from tests import test_account_claim_jobs as claim_tests
from tests import test_account_original_open as open_tests
from tests import test_host_open_provenance as open_proof_tests
from tests import test_run_provenance as run_proof_tests
from tests.test_collector_read_scope import RecordingLock
from tests.test_ledger_waits import Clock
from tests.learning_support import attest_provider

from codex_agents import account_claim_jobs, account_host_open, account_original_open
from codex_agents import account_run_provenance, learning_store, ledger_waits, refusal_steps
from codex_agents.account_api import (
    AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request,
)
from codex_agents.account_host import AccountServiceHost
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import canonical_json
from codex_agents.sql_read_budget import SQLReadError, SQLReadLimits
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits


def hold_ledger(test, ledger, *, coordination_timeout=5):
    """A real foreign thread owns the real RLock until an explicit acknowledgement."""
    held, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with ledger._lock:
            held.set()
            release.wait(6 * coordination_timeout)
        stopped.set()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()

    def finish():
        release.set()
        thread.join(coordination_timeout)
        test.assertTrue(stopped.is_set(), 'ledger holder did not stop')

    test.addCleanup(finish)
    test.assertTrue(held.wait(coordination_timeout), 'ledger holder did not acquire')
    return finish


def assert_busy(test, raw, request, reason):
    with test.assertRaises(AccountError) as raised:
        decode_account_response(raw, request)
    test.assertEqual(reason, raised.exception.reason_code)
    wire = json.loads(raw)
    test.assertIs(False, wire['ready'])
    test.assertIsNone(wire['result'])
    test.assertEqual(reason, wire['reason_code'])
    test.assertEqual(account_request_digest(request), wire['request_digest'])
    test.assertEqual("The account's records were busy: try again in a moment.",
                     refusal_steps.next_step(raised.exception.reason_code))


def assert_lane_ok(test, jobs):
    health = jobs.health.view()
    test.assertFalse(jobs.snapshot()['faulted'])
    test.assertFalse(health['faulted'])
    test.assertIsNone(health['last_failure'])


class ClaimContentionTests(claim_tests.AccountClaimJobTests):
    __why_not_scenario__ = 'Real account owner and signature pool; synthetic provider and enrollment, no installed services.'
    @requirements('E1a-T2')
    def test_claim_completion_busy_then_retry(self):
        ticket = self.begin()  # Real admission capture and worker submission succeed first.
        original = self.views._claim_from_signature_job
        releases = []

        def complete(*args, **kwargs):
            releases.append(hold_ledger(self, self.ledger))
            return original(*args, **kwargs)

        with mock.patch.object(self.views, '_claim_from_signature_job', side_effect=complete) as reached:
            assert_busy(self, self.pump(ticket), self.request(), 'account_busy')
        self.assertEqual(1, reached.call_count)
        self.assertGreater(sum(self.ledger.lock_refusals().values()), 0)
        assert_lane_ok(self, self.jobs)
        releases[0]()
        self.fixture.assert_no_claim()
        self.assertEqual('claimed', decode_account_response(self.pump(self.begin()), self.request())['state'])
        self.assertEqual((1, 1), (self.fixture.count('launch_claims'), self.fixture.count('launch_receipts')))

    @requirements('E1a-T2')
    def test_real_stale_recapture_busy_then_retry(self):
        ticket = self.begin()
        # A real new statement invalidates the already captured preparation.
        bridge = self.fixture.fixture.fixture
        attest_provider(self.ledger, self.project, bridge.owner_key, 'codex',
                        self.fixture.identity['account_fingerprint'], issued_at=2)
        original = self.jobs._intake.capture_admission
        releases = []

        def recapture(*args, **kwargs):
            releases.append(hold_ledger(self, self.ledger))
            return original(*args, **kwargs)

        with mock.patch.object(self.jobs._intake, 'capture_admission', side_effect=recapture) as reached:
            assert_busy(self, self.pump(ticket), self.request(), 'account_busy')
        self.assertEqual(1, reached.call_count)  # Initial capture preceded the patch.
        self.assertGreater(sum(self.ledger.lock_refusals().values()), 0)
        assert_lane_ok(self, self.jobs)
        releases[0]()
        self.fixture.assert_no_claim()
        self.assertEqual('claimed', decode_account_response(self.pump(self.begin()), self.request())['state'])


class OpenContentionTests(open_tests.OriginalAccountOpenJobTests):
    __why_not_scenario__ = 'Real account open and signature pool; synthetic project enrollment, no installed services.'
    def host_service(self):
        """Use the product host authorizer; only its enrolled connection is synthetic."""
        p, request, token = self.reference, self.wire_command(), object()
        enrolled = types.SimpleNamespace(role='account', host_client='codex-host-v1',
            subject=str(p.base.bridge.project), project_bound=False,
            request_digest=account_request_digest(request),
            context=canonical_json({'purpose': 'open', **p.context.snapshot()[1]}))
        entry = types.SimpleNamespace(kind='account', enrolled=enrolled)
        host = AccountServiceHost.__new__(AccountServiceHost)
        host._frontend = types.SimpleNamespace(context=lambda supplied: entry if supplied is token else None)
        host._views, host._state, host._contexts = self.jobs._views, self.ledger.state, {}
        service = AccountService(request.runtime_digest, self.jobs._views,
                                 authorizer=host._authorize, claim_jobs=self.jobs)
        self.addCleanup(service.close)
        return service, request, token, host

    @requirements('E1a-T2')
    def test_initial_open_real_host_authorization_busy_then_retry(self):
        service, request, token, host = self.host_service()
        self.assertIsInstance(host._authorize(request, token), AccountAuthorization)
        release = hold_ledger(self, self.ledger)
        assert_busy(self, service.begin(encode_account_request(request), token), request, 'open_account_busy')
        self.assertEqual({'project_for_root': 1}, self.ledger.lock_refusals())
        self.assertEqual([], self.reference.pending())
        assert_lane_ok(self, self.jobs)
        release()
        result = decode_account_response(self.pump_wire(service, service.begin(encode_account_request(request), token)), request)
        self.assertEqual('opened', result['state'])

    @requirements('E1a-T2')
    def test_open_completion_busy_then_retry(self):
        service, _ = self.wire_service()
        request = self.wire_command()
        ticket = service.begin(encode_account_request(request), self.token)
        original = account_original_open.finish_open_from_signature_job
        releases = []

        def complete(*args, **kwargs):
            releases.append(hold_ledger(self, self.ledger))
            return original(*args, **kwargs)

        with mock.patch.object(account_original_open, 'finish_open_from_signature_job', side_effect=complete) as reached:
            assert_busy(self, self.pump_wire(service, ticket), request, 'open_account_busy')
        self.assertEqual(1, reached.call_count)
        self.assertGreater(sum(self.ledger.lock_refusals().values()), 0)
        self.assertEqual('pending', self.reference.pending()[0]['state'])
        assert_lane_ok(self, self.jobs)
        releases[0]()
        self.assertEqual('opened', decode_account_response(
            self.pump_wire(service, service.begin(encode_account_request(request), self.token)), request)['state'])

    @requirements('E1a-T3')
    def test_exhausted_deferred_publication_restores_next_request_and_owner_pump(self):
        service, _ = self.wire_service()
        request = self.wire_command()
        ticket = service.begin(encode_account_request(request), self.token)
        clock, publications, releases = Clock(), [], []
        original = service._reply

        def encode(*args, **kwargs):
            raw = original(*args, **kwargs)
            if (kwargs.get('result') or {}).get('state') == 'opened':
                publications.append(ledger_waits.wait_seconds())
                releases.append(hold_ledger(self, self.ledger))
                clock.now += .81  # Encoding/publication consumes more than this request's budget.
            return raw

        recording = RecordingLock(self.ledger._connection._sql_owner_lock)
        with mock.patch.object(ledger_waits, 'time', clock), \
                mock.patch.object(self.ledger._connection, '_sql_owner_lock', recording):
            with mock.patch.object(service, '_reply', side_effect=encode):
                assert_busy(self, self.pump_wire(service, ticket), request, 'open_account_busy')
            self.assertEqual([.75], publications)
            self.assertEqual(0, recording.timeouts[-1])  # The actual publication read used the spent budget.
            self.assertIsNone(getattr(ledger_waits._request, 'deadline', None))
            self.assertEqual('committed', self.reference.pending()[0]['state'])  # Commit already succeeded.
            assert_lane_ok(self, self.jobs)
            releases[0]()
            clock.now += 10
            # Real owner-pump entry, with a real ledger read at its offer boundary.
            offered, offer = [], self.jobs._pool.offer_next

            def owner_work():
                offered.append(ledger_waits.wait_seconds())
                self.assertEqual(request.project_id, self.ledger.project_for_root(self.reference.base.bridge.project))
                return offer()

            with mock.patch.object(self.jobs._pool, 'offer_next', side_effect=owner_work):
                service.drive_claims()
            self.assertTrue(offered)
            self.assertEqual([.75] * len(offered), offered)
            # Same identity retries the retained commit under a new request/publication budget.
            result = decode_account_response(self.pump_wire(service,
                service.begin(encode_account_request(request), self.token)), request)
            self.assertEqual(('opened', True), (result['state'], result['known']))
            self.assertIsNone(getattr(ledger_waits._request, 'deadline', None))


class ProvenanceContentionTests(unittest.TestCase):
    __why_not_scenario__ = 'Real account adapters and sealed lease evidence; synthetic native enrollment, no installed services.'
    def check_provenance(self, fixture, ledger, request, operation, scope, evidence, limits, adapter):
        command = AccountRequest(1, operation, 'busy-proof', scope.project_id, scope.runtime_digest, request.payload())

        def authorize(command, _token):
            return AccountAuthorization(command.project_id, command.runtime_digest, account_request_digest(command),
                'project', frozenset({operation}), 'a' * 64, scope, evidence)

        views = AccountOwnerViews(ledger)
        jobs = account_claim_jobs.AccountClaimJobs(views, SignatureIntakeLimits(2, 2, 512, 262_144, 2),
            SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000), limits)
        jobs.bootstrap()

        def drain():
            jobs.request_close()
            deadline = time.monotonic() + 5
            while not jobs.drain():
                self.assertLess(time.monotonic(), deadline, 'idle provenance lane did not drain')
                time.sleep(.002)

        self.addCleanup(drain)
        service = AccountService(scope.runtime_digest, views, authorizer=authorize, claim_jobs=jobs,
                                 open_provenance_work_limits=limits, run_provenance_work_limits=limits)
        self.addCleanup(service.close)
        expected = decode_account_response(service.handle(encode_account_request(command)), command)
        # Authorization/evidence are valid. Hold only when the real provenance adapter is entered.
        name = 'attest_open_provenance' if operation == 'account.open_provenance' else 'attest_run_provenance'
        original, releases = getattr(adapter, name), []

        def prove(*args, **kwargs):
            releases.append(hold_ledger(self, ledger))
            return original(*args, **kwargs)

        changes = ledger._connection.total_changes
        with mock.patch.object(adapter, name, side_effect=prove) as reached:
            assert_busy(self, service.handle(encode_account_request(command)), command, 'account_busy')
        self.assertEqual(1, reached.call_count)
        self.assertGreater(sum(ledger.lock_refusals().values()), 0)
        self.assertEqual(changes, ledger._connection.total_changes)
        self.assertFalse(ledger._connection.in_transaction)
        assert_lane_ok(self, jobs)
        releases[0]()
        self.assertEqual(expected, decode_account_response(service.handle(encode_account_request(command)), command))

    @requirements('E1a-T2')
    def test_open_provenance_real_contention_then_retry(self):
        fixture = open_proof_tests.HostOpenProvenanceTests('runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.check_provenance(fixture, fixture.fixture.ledger, fixture.request, 'account.open_provenance',
                              fixture.fixture.scope, fixture.fixture.evidence, fixture.limits, account_host_open)

    @requirements('E1a-T2')
    def test_run_provenance_real_contention_then_retry(self):
        fixture = run_proof_tests.RunProvenanceTests('runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.check_provenance(fixture, fixture.base.ledger, fixture.request, 'account.run_provenance',
                              fixture.fixture.scope, fixture.base.evidence, fixture.limits, account_run_provenance)


class RequestDeadlineEvidenceTests(api_tests.AccountApiTests):
    __why_not_scenario__ = 'Real account/collector entry methods and locks; controlled receipt/clock, no installed services.'
    @requirements('E1a-T3')
    def test_real_handle_and_begin_share_successive_waits_and_refresh(self):
        clock, observed = Clock(), []
        original = self.authorize

        def authorize(request, token):
            self.assertEqual(self.projects[0], self.ledger.project_for_root(self.roots[0]))
            clock.now += .5
            return original(request, token)

        service = AccountService(api_tests.RUNTIME, self.views, authorizer=authorize)
        self.addCleanup(service.close)
        recording = RecordingLock(self.ledger._lock)
        with mock.patch.object(ledger_waits, 'time', clock), mock.patch.object(self.ledger, '_lock', recording):
            for entry in (service.handle, service.begin):
                recording.timeouts.clear()
                request = self.request('account.project')
                result = decode_account_response(entry(encode_account_request(request), api_tests.TOKEN), request)
                self.assertEqual(self.projects[0], result['project_id'])
                observed.append(list(recording.timeouts))
                self.assertIsNone(getattr(ledger_waits._request, 'deadline', None))
                clock.now += 10
        self.assertEqual(2, len(observed))
        for timeouts in observed:
            self.assertEqual(2, len(timeouts))
            self.assertAlmostEqual(.75, timeouts[0])
            self.assertAlmostEqual(.3, timeouts[1])

    @requirements('E1a-T3')
    def test_slow_collector_receipt_and_next_frame_get_fresh_budgets(self):
        clock = Clock()
        server = learning_store.CollectorServer(self.ledger)
        recording = RecordingLock(self.ledger._lock)
        frame = canonical_json({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION, 'op': 'project',
                                'project_root': str(self.roots[0])}) + b'\n'
        dispatch = server._dispatch

        def successive_waits(request):
            reply = dispatch(request)
            clock.now += .5
            self.assertEqual(self.projects[0], self.ledger.project_for_root(self.roots[0]))
            return reply

        class SlowReceipt:
            def __init__(self):
                self.chunks = iter((frame[:8], frame[8:]))

            def settimeout(self, seconds):
                pass

            def recv(self, size):
                clock.now += .6  # Total receipt exceeds the ledger budget, within frame receipt's own limit.
                return next(self.chunks)

        with mock.patch.object(ledger_waits, 'time', clock), \
                mock.patch.object(self.ledger, '_lock', recording), \
                mock.patch.object(server, '_dispatch', side_effect=successive_waits):
            for _ in range(2):
                reply = server._handle(SlowReceipt())
                self.assertEqual({'ok': True, 'project_id': self.projects[0]}, reply)
                self.assertIsNone(getattr(ledger_waits._request, 'deadline', None))
        self.assertEqual(4, len(recording.timeouts))
        for actual, expected in zip(recording.timeouts, (.75, .3, .75, .3)):
            self.assertAlmostEqual(expected, actual)

    @requirements('E1a-T3')
    def test_raising_collector_frame_restores_deadline_and_next_frame_budget(self):
        clock = Clock()
        server = learning_store.CollectorServer(self.ledger)
        recording = RecordingLock(self.ledger._lock)
        frame = canonical_json({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION, 'op': 'project',
                                'project_root': str(self.roots[0])}) + b'\n'
        connection = mock.Mock()
        connection.recv.return_value = frame
        dispatch = server._dispatch
        failure = RuntimeError('collector dispatch failed after ledger read')
        previous = getattr(ledger_waits._request, 'deadline', None)
        self.addCleanup(setattr, ledger_waits._request, 'deadline', previous)

        def raising_dispatch(request):
            self.assertEqual({'ok': True, 'project_id': self.projects[0]}, dispatch(request))
            clock.now += .5
            raise failure

        with mock.patch.object(ledger_waits, 'time', clock), \
                mock.patch.object(self.ledger, '_lock', recording):
            with mock.patch.object(server, '_dispatch', side_effect=raising_dispatch):
                with self.assertRaises(RuntimeError) as raised:
                    server._handle(connection)
            self.assertIs(failure, raised.exception)
            self.assertEqual(previous, getattr(ledger_waits._request, 'deadline', None),
                             'exceptional collector frame leaked request deadline')
            clock.now += 10
            self.assertEqual({'ok': True, 'project_id': self.projects[0]}, server._handle(connection))
            self.assertEqual(previous, getattr(ledger_waits._request, 'deadline', None))
        self.assertEqual(2, len(recording.timeouts))
        for timeout in recording.timeouts:
            self.assertAlmostEqual(.75, timeout)


class ConcurrentRefusalEvidenceTests(api_tests.AccountApiTests):
    __why_not_scenario__ = 'Real acquisition paths and health dispatch; event-coordinated foreign holder, no installed services.'
    @requirements('E1a-T4')
    def test_event_coordinated_exact_counts_for_all_acquisition_paths_during_health(self):
        server = learning_store.CollectorServer(self.ledger)
        coordination_timeout = 20
        release = hold_ledger(self, self.ledger, coordination_timeout=coordination_timeout)
        limits = SQLReadLimits(1000, 1000, 4096)

        def held_default():
            with self.ledger._held():
                self.fail('refused hold body ran')

        def held_explicit():
            with self.ledger._held('explicit'):
                self.fail('refused explicit hold body ran')

        def write_unit():
            with self.ledger._write_unit():
                self.fail('refused write body ran')

        def account_transaction():
            with self.ledger._account_transaction():
                self.fail('refused transaction body ran')

        def reader_entry():
            with self.ledger.repository(self.projects[0]):
                self.fail('refused reader body ran')

        def slot_health():
            self.assertEqual({'available': False, 'reason': 'ledger_busy'},
                             self.ledger.account_slot_health(self.projects[0]))

        def sweep():
            self.assertEqual('busy', server._sweep_cycle())

        def idle_probe():
            self.assertIs(False, server._holds_due_idle())  # Skips without adding to read_call.

        def project_health():
            self.assertIs(True, server._dispatch({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION,
                'op': 'health', 'project_root': str(self.roots[0])})['ledger_busy'])

        paths = [held_default, held_explicit, write_unit, account_transaction, reader_entry,
                 slot_health, sweep, project_health, idle_probe]
        labels = ['held_default', 'explicit', 'write_unit', 'account_transaction', 'repository',
                  'slot_health', 'sweeps', 'health']

        def expected(wave):
            # Only the three owner-thread SQL reads below add read_call counts; idle_probe adds none.
            return {**dict.fromkeys(labels, wave), 'read_call': 3 * wave}
        start, finished = threading.Barrier(len(paths) + 2), threading.Barrier(len(paths) + 2)
        counted, health_read = threading.Event(), threading.Event()
        errors, snapshots = [], []
        original_refused = self.ledger._lock_waits.refused

        def refused(label):
            original_refused(label)
            counted.set()
            self.assertTrue(health_read.wait(coordination_timeout), 'health blocked behind ledger')

        def invoke(call):
            try:
                call()
            except SQLReadError as error:
                self.assertEqual('sql_read_busy', error.reason_code)
            else:
                self.assertIn(call, (slot_health, sweep, project_health, idle_probe))

        def worker(call):
            try:
                for _ in range(3):
                    start.wait(coordination_timeout)
                    invoke(call)
                    finished.wait(coordination_timeout)
            except BaseException as error:
                errors.append(error)
                start.abort()
                finished.abort()

        def health():
            try:
                for wave in range(1, 4):
                    start.wait(coordination_timeout)
                    self.assertTrue(counted.wait(coordination_timeout))
                    reply = server._dispatch({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION, 'op': 'health'})
                    self.assertIs(True, reply['ok'])
                    self.assertTrue(reply['ledger_waits'])
                    health_read.set()  # Refusers cannot complete before a ledger-free health read.
                    finished.wait(coordination_timeout)
                    snapshots.append(server._dispatch({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION,
                                                       'op': 'health'})['ledger_waits'])
                    if snapshots[-1] != expected(wave):
                        errors.append(AssertionError(f'wave {wave}: {snapshots[-1]!r}'))
                    counted.clear()
                    health_read.clear()
            except BaseException as error:
                errors.append(error)
                start.abort()
                finished.abort()

        threads = [threading.Thread(target=worker, args=(path,), daemon=True) for path in paths]
        threads.append(threading.Thread(target=health, daemon=True))
        for thread in threads:
            self.addCleanup(thread.join, coordination_timeout)
        with mock.patch.object(self.ledger._lock_waits, 'refused', side_effect=refused):
            for thread in threads:
                thread.start()
            for _ in range(3):
                start.wait(coordination_timeout)
                # SQLReadConnection enforces owner-thread reads: exercise this path on its real owner.
                for read in (self.ledger._connection.read_call, self.ledger._connection.transaction_read_call,
                             self.ledger._connection.read_in_current_mode):
                    invoke(lambda: read(limits, lambda: self.fail('read body ran')))
                finished.wait(coordination_timeout)
            for thread in threads:
                thread.join(coordination_timeout)
                self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(3, len(snapshots))
        self.assertEqual(expected(3), self.ledger.lock_refusals())
        self.assertEqual(0, self.ledger._readers())
        self.assertFalse(self.ledger._connection.in_transaction)
        release()
        self.assertEqual(self.projects[0], self.ledger.project_for_root(self.roots[0]))


def load_tests(loader, tests, pattern):
    """Do not rerun inherited fixture tests or imported test classes."""
    suite = unittest.TestSuite()
    for cls in (ClaimContentionTests, OpenContentionTests, ProvenanceContentionTests,
                RequestDeadlineEvidenceTests, ConcurrentRefusalEvidenceTests):
        for name in loader.getTestCaseNames(cls):
            if name in cls.__dict__:
                suite.addTest(cls(name))
    return suite
