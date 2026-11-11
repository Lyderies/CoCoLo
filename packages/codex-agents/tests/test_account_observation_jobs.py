"""Actual deferred probes and account API; native enrollment remains synthetic."""
import socket
import sys
import threading
import time
import unittest
from unittest import mock

from tests import test_account_observation as fixtures
from tests.learning_support import trusted_tools

from codex_agents import providers
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_response, encode_account_request,
)
from codex_agents.account_observation import ObservationContext
from codex_agents.account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents.provider_observation_contracts import ObservationInput
from codex_agents.learning_store import DEFAULT_RETENTION_MS


class AccountObservationJobTests(unittest.TestCase):
    def setUp(self):
        self.backend = fixtures.AccountObservationTests(methodName='runTest')
        self.backend.setUp()
        self.addCleanup(self.backend.doCleanups)
        self.views, self.ledger = self.backend.views, self.backend.ledger
        self.projects, self.roots = self.backend.fixture.projects, self.backend.fixture.roots
        self.runtime = fixtures.fixtures.RUNTIME
        self.token, self.allowed = object(), True
        self.contexts = {project: ObservationContext(str(root)) for project, root in zip(self.projects, self.roots)}
        self.gates = []
        self.jobs = AccountObservationJobs(self.backend.owner, ObservationJobLimits(2, 2, 2, 1))
        self.jobs.bootstrap()
        self.addCleanup(self.stop_jobs)

        def authorize(request, context):
            if not self.allowed or context is not self.token or request.project_id not in self.contexts:
                return None
            return AccountAuthorization(request.project_id, self.runtime, account_request_digest(request),
                'host', ACCOUNT_OPERATIONS, 'b' * 64, observation_context=self.contexts[request.project_id])

        self.authorize = authorize
        self.service = AccountService(self.runtime, self.views, authorizer=authorize, observation_jobs=self.jobs)
        self.addCleanup(self.service.close)

    def stop_jobs(self):
        for gate in self.gates:
            gate.set()
        self.jobs.request_close()
        end = time.monotonic() + 8
        while not self.jobs.drain():
            self.assertLess(time.monotonic(), end, 'fixture observation workers did not drain')
            time.sleep(.002)

    def request(self, identity='first', *, project=None):
        return AccountRequest(1, 'account.observe_provider', identity, project or self.projects[0], self.runtime,
                              ObservationInput('codex').payload())

    def begin(self, request=None):
        return self.service.begin(encode_account_request(request or self.request()), self.token)

    def wait(self, condition):
        end = time.monotonic() + 8
        while not condition():
            self.service.drive_claims()
            self.assertLess(time.monotonic(), end, 'fixture account observation progress expired')
            time.sleep(.002)

    def take(self, ticket, request):
        result = []
        def received():
            raw = self.service.take_claim_reply(ticket)
            if raw is None:
                return False
            result.append(raw)
            return True
        self.wait(received)
        return decode_account_response(result[0], request)

    def hold_first_project(self):
        entered, resume = threading.Event(), threading.Event()
        self.gates.append(resume)
        original = providers.observe_codex
        def held(root, **kwargs):
            if root == self.roots[0]:
                entered.set()
                if not resume.wait(6):
                    raise AssertionError('fixture observation release expired')
            return original(root, **kwargs)
        return entered, resume, mock.patch.object(providers, 'observe_codex', new=held)

    def test_held_project_probe_cannot_block_second_project_probe_or_account_read(self):
        entered, resume, held = self.hold_first_project()
        with trusted_tools(self.backend.tools), held:
            first_request = self.request()
            first = self.begin(first_request)
            self.service.drive_claims()
            self.assertTrue(entered.wait(5))
            self.assertIs(first, self.begin(first_request))
            second_request = self.request('other-project', project=self.projects[1])
            second = self.begin(second_request)
            self.assertFalse(self.take(second, second_request)['known'])
            read = AccountRequest(1, 'account.project', 'other-read', self.projects[1], self.runtime, {})
            self.assertEqual('active', decode_account_response(
                self.service.handle(encode_account_request(read), self.token), read)['status'])
            self.assertEqual(1, self.jobs.snapshot()['active'])
            resume.set()
            self.assertFalse(self.take(first, first_request)['known'])
        self.assertFalse(self.jobs.snapshot()['faulted'])

    def test_active_cancel_keeps_quota_until_actual_return_and_reply_retirement(self):
        entered, resume, held = self.hold_first_project()
        with trusted_tools(self.backend.tools), held:
            request = self.request()
            ticket = self.begin(request)
            self.service.drive_claims()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            self.jobs.cancel(ticket)
            self.assertLess(time.monotonic() - started, .5)
            second = self.begin(self.request('second'))
            overloaded = self.request('third')
            with self.assertRaisesRegex(AccountError, 'observation_full'):
                decode_account_response(self.begin(overloaded), overloaded)
            self.assertEqual(2, self.jobs.snapshot()['jobs'])
            self.jobs.cancel(second)
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                self.take(second, self.request('second'))
            self.assertEqual(1, self.jobs.snapshot()['active'])
            resume.set()
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                self.take(ticket, request)
        self.assertEqual(0, self.backend.provider_rows())
        self.assertEqual(0, self.jobs.snapshot()['jobs'])

    def test_queued_revocation_never_runs_probe_or_faults_shared_service(self):
        request = self.request()
        ticket = self.begin(request)
        self.allowed = False
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('revoked probe')):
            self.service.drive_claims()
            with self.assertRaisesRegex(AccountError, 'observation_not_authorized'):
                self.take(ticket, request)
        self.assertEqual(0, self.backend.provider_rows())
        self.assertFalse(self.jobs.snapshot()['faulted'])

    def test_queued_expiry_never_probes_and_active_expiry_keeps_slot_until_actual_return(self):
        from codex_agents import account_observation_jobs
        queued_request = self.request('queued-expiry')
        queued = self.begin(queued_request)
        deadline = self.jobs._jobs[queued].deadline_ns
        with mock.patch.object(account_observation_jobs.time, 'monotonic_ns', return_value=deadline), \
                mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('expired queued probe')):
            self.service.drive_claims()
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                self.take(queued, queued_request)
        entered, resume, held = self.hold_first_project()
        with trusted_tools(self.backend.tools), held:
            request = self.request('active-expiry')
            ticket = self.begin(request)
            self.service.drive_claims()
            self.assertTrue(entered.wait(5))
            deadline = self.jobs._jobs[ticket].deadline_ns
            with mock.patch.object(account_observation_jobs.time, 'monotonic_ns', return_value=deadline):
                self.service.drive_claims()
                self.assertIsNone(self.service.take_claim_reply(ticket))
                self.assertEqual(1, self.jobs.snapshot()['active'])
                resume.set()
                with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                    self.take(ticket, request)
        self.assertEqual(0, self.jobs.snapshot()['jobs'])
        self.assertEqual(0, self.backend.provider_rows())

    def test_default_denial_and_no_synchronous_probe_fallback(self):
        request = self.request()
        default = AccountService(self.runtime, self.views)
        self.addCleanup(default.close)
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('synchronous probe')):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(default.begin(encode_account_request(request), self.token), request)
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                decode_account_response(self.service.handle(encode_account_request(request), self.token), request)
        self.assertEqual(0, self.jobs.snapshot()['jobs'])

    def test_role_or_missing_context_denial_reserves_no_slot_or_durable_request(self):
        request = self.request()
        for source, context, scope in (('owner', self.contexts[self.projects[0]], 'b' * 64),
                ('hook', self.contexts[self.projects[0]], 'b' * 64),
                ('app_server', self.contexts[self.projects[0]], 'b' * 64),
                ('project', self.contexts[self.projects[0]], 'b' * 64),
                ('host', None, 'b' * 64), ('host', self.contexts[self.projects[0]], None)):
            with self.subTest(source=source, context=context, scope=scope):
                def authorize(command, token):
                    return AccountAuthorization(command.project_id, self.runtime, account_request_digest(command),
                        source, ACCOUNT_OPERATIONS, scope, observation_context=context)
                service = AccountService(self.runtime, self.views, authorizer=authorize, observation_jobs=self.jobs)
                try:
                    with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                        decode_account_response(service.begin(encode_account_request(request), self.token), request)
                finally:
                    service.close()
        self.assertEqual(0, self.jobs.snapshot()['jobs'])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])

    def test_post_probe_service_close_cannot_publish_or_commit(self):
        entered, resume, held = self.hold_first_project()
        with trusted_tools(self.backend.tools), held:
            request = self.request()
            ticket = self.begin(request)
            self.service.drive_claims()
            self.assertTrue(entered.wait(5))
            self.service.close()
            resume.set()
            with self.assertRaisesRegex(AccountError, 'observation_not_authorized'):
                self.take(ticket, request)
        self.assertEqual(0, self.backend.provider_rows())

    def test_ready_known_reply_cannot_outlive_or_resurrect_original_provider_records(self):
        request = self.request('retained-original')
        with trusted_tools(self.backend.tools):
            reply = self.take(self.begin(request), request)
        for change in ('expire', 'delete'):
            with self.subTest(change=change):
                known = self.begin(request)
                if change == 'expire':
                    with mock.patch.object(self.ledger, '_clock_ms',
                            return_value=reply['observed_at_ms'] + DEFAULT_RETENTION_MS):
                        with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                            decode_account_response(self.service.take_claim_reply(known), request)
                else:
                    self.ledger._connection.execute("DELETE FROM records WHERE source='provider'")
                    with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                        decode_account_response(self.service.take_claim_reply(known), request)
        self.assertEqual(0, self.jobs.snapshot()['jobs'])

    @unittest.skipUnless(sys.platform == 'darwin', 'existing signature-worker startup requires reviewed macOS policy')
    def test_actual_account_client_round_trip_through_original_account_ingress(self):
        from codex_agents.account_claim_jobs import AccountClaimJobs
        from codex_agents.account_ingress import AccountIngress, AccountIngressLimits
        from codex_agents.signature_job_intake import SignatureIntakeLimits
        from codex_agents.signature_worker_pool import SignatureWorkerLimits
        from codex_agents.sql_read_budget import SQLReadLimits

        claims = AccountClaimJobs(self.views, SignatureIntakeLimits(2, 2, 128, 262_144, 2),
            SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
            SQLReadLimits(10_000, 1000, 1_048_576), allow_unleased_claim_reference=True)
        claims.bootstrap()
        service = AccountService(self.runtime, self.views, authorizer=self.authorize, claim_jobs=claims,
                                 observation_jobs=self.jobs, allow_unleased_claim_reference=True)
        self.addCleanup(service.close)
        ingress = AccountIngress(service, AccountIngressLimits(4, 1, 5, 2, 5_000_000_000, 1_000_000))
        ingress.bootstrap()
        endpoint = self.backend.fixture.base / 'observation.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(endpoint))
        listener.listen(1)
        listener.settimeout(5)
        results, failures = [], []

        def accept():
            try:
                connection, _ = listener.accept()
                ingress.attach(connection, self.token)
            except BaseException as error:
                failures.append(error)

        def client():
            try:
                results.append(AccountClient(endpoint, self.projects[0], self.runtime, timeout=5).observe_provider(
                    'codex', request_id='socket-original'))
            except BaseException as error:
                failures.append(error)

        acceptor, caller = threading.Thread(target=accept), threading.Thread(target=client)
        with trusted_tools(self.backend.tools):
            acceptor.start()
            caller.start()
            end = time.monotonic() + 8
            try:
                while caller.is_alive():
                    ingress.pump_owner()
                    self.assertLess(time.monotonic(), end)
                    time.sleep(.002)
            finally:
                ingress.request_close()
                while not ingress.drain():
                    self.assertLess(time.monotonic(), end + 8)
                    time.sleep(.002)
                caller.join(3)
                acceptor.join(3)
        self.assertFalse(caller.is_alive() or acceptor.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, len(results))
        self.assertFalse(results[0]['known'])
        self.assertEqual('active', self.views.project(self.projects[1])['status'])
