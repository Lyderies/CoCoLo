"""Fresh clients, original account ingress and inert providers; synthetic peers."""
from dataclasses import asdict
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests import test_account_observation as fixtures
from tests.learning_support import attest_provider, trusted_tools

from codex_agents import providers
from codex_agents.account_api import ACCOUNT_OPERATIONS, account_request_digest
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_ingress import AccountIngress, AccountIngressLimits
from codex_agents.account_observation import ObservationContext
from codex_agents.account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
from codex_agents.control_api import ControlScope
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.provider_observation_contracts import ObservationError, ObservationInput, checked_observation_reply
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits


@unittest.skipUnless(sys.platform == 'darwin', 'existing signature-worker startup requires reviewed macOS policy')
class CliObservationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.backend = fixtures.AccountObservationTests(methodName='runTest')
        self.backend.setUp()
        self.addCleanup(self.backend.doCleanups)
        self.ledger, self.views = self.backend.ledger, self.backend.views
        self.projects, self.roots = self.backend.fixture.projects, self.backend.fixture.roots
        self.state = CollectorPaths(self.backend.fixture.state.root)
        self.contexts = {p: ObservationContext(str(r)) for p, r in zip(self.projects, self.roots)}
        self.token, self.allowed = object(), True
        self.clients, self.gates = [], []
        self.claims = AccountClaimJobs(self.views, SignatureIntakeLimits(2, 2, 128, 262_144, 2),
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
            SQLReadLimits(10_000, 1000, 1_048_576))
        self.claims.bootstrap()
        self.jobs = AccountObservationJobs(self.backend.owner, ObservationJobLimits(2, 2, 2, 1))
        self.jobs.bootstrap()

        def authorize(request, context):
            if self.allowed and context is self.token and request.project_id in self.contexts:
                return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                    'host', ACCOUNT_OPERATIONS, 'b'*64, observation_context=self.contexts[request.project_id])

        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views, authorizer=authorize,
                                      claim_jobs=self.claims, observation_jobs=self.jobs)
        self.ingress = AccountIngress(self.service, AccountIngressLimits(4, 1, 5, 2, 10_000_000_000, 1_000_000))
        self.ingress.bootstrap()
        self.endpoint = self.state.root/'a.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.addCleanup(self.stop)
        for project, root in zip(self.projects, self.roots):
            self.hint(project, root)

    def hint(self, project, root, *, version=2, digest=COLLECTOR_RUNTIME_DIGEST):
        path = runtime_hint_path(self.state, root)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        value = {'schema_version': version, 'project_root_digest': keyed_digest(self.state.digest_key(), str(root.resolve())),
            'scope': asdict(ControlScope(project_id=project, primary_session_id='primary', runtime_digest=digest,
                                        supervisor_instance_id='supervisor', supervisor_generation=1)),
            'dispatch_endpoint': str(self.state.root/'absent-dispatch.sock'),
            'result_endpoint': str(self.state.root/'absent-project.sock')}
        if version == 2:
            value['account_endpoint'] = str(self.endpoint)
        path.write_bytes(canonical_json(value))
        path.chmod(0o600)

    def pump(self):
        for _ in range(4):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                break
            self.ingress.attach(connection, self.token)
        self.ingress.pump_owner()

    def wait(self, predicate):
        end = time.monotonic()+15
        while not predicate():
            self.pump()
            self.assertLess(time.monotonic(), end, 'bounded account/client progress expired')
            time.sleep(.002)

    def stop(self):
        for gate in self.gates:
            gate.set()
        for process in self.clients:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.listener.close()
        self.ingress.request_close()
        end = time.monotonic()+10
        while not self.ingress.drain():
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        self.service.close()

    def start(self, *, provider='codex', identity=None, root=None, host='codex', extra=()):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for marker in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(marker, None)
        if host == 'codex':
            env['CODEX_THREAD_ID'] = 'fixture-observe-primary'
        else:
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='fixture-observe-primary')
        args = [] if identity is None else ['--request-id', identity]
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.dispatch_cli_fixture', 'provider-observe',
            '--project-root', str(root or self.roots[0]), '--state-root', str(self.state.root),
            '--provider', provider, *args, *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        return process

    def take(self, process):
        self.wait(lambda: process.poll() is not None)
        output, errors = process.communicate(timeout=2)
        self.assertEqual(b'', errors)
        return process.returncode, json.loads(output)

    def test_both_providers_and_primary_hosts_use_original_account_without_project_sockets(self):
        with trusted_tools(self.backend.tools):
            for provider in ('codex', 'claude'):
                for host in ('codex', 'claude'):
                    code, reply = self.take(self.start(provider=provider, host=host))
                    self.assertEqual(0, code, reply)
                    self.assertTrue(reply['ready'])
                    self.assertFalse(reply['known'])
                    self.assertEqual(provider, reply['provider'])
                    self.assertTrue(reply['request_id'].startswith('cli-observe-'))
                    self.assertTrue(all('client_path' not in record for record in reply['records']))
                    self.assertNotIn('probe', reply)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertEqual(0, self.claims.snapshot()['tickets'])

    def test_same_identity_returns_original_records_and_changed_provider_cannot_reprobe(self):
        calls, owner = [], threading.current_thread()
        original = providers.observe_codex
        def counted(*args, **kwargs):
            calls.append(threading.current_thread())
            return original(*args, **kwargs)
        with trusted_tools(self.backend.tools), mock.patch.object(providers, 'observe_codex', new=counted):
            code, first = self.take(self.start(identity='one-original'))
            self.assertEqual(0, code, first)
            code, known = self.take(self.start(identity='one-original'))
            self.assertEqual((0, {**first, 'known': True}), (code, known))
            code, changed = self.take(self.start(identity='one-original', provider='claude'))
            self.assertEqual((2, 'observation_conflict'), (code, changed['reason_code']))
        self.assertEqual(1, len(calls))
        self.assertIsNot(owner, calls[0])

    def test_lost_committed_reply_preserves_generated_identity_and_explicit_retry_never_reprobes(self):
        original, lost = self.service._reply, []
        def withhold(labels, *, result=None, reason=None):
            if result is not None and 'records' in result and not lost:
                lost.append(result)
                raise OSError('PRIVATE-LOST-OBSERVATION-REPLY')
            return original(labels, result=result, reason=reason)
        with trusted_tools(self.backend.tools), mock.patch.object(self.service, '_reply', new=withhold):
            code, failure = self.take(self.start())
        self.assertEqual((2, 'observation_unavailable'), (code, failure['reason_code']))
        self.assertEqual(1, len(lost))
        self.assertNotIn('PRIVATE-LOST-OBSERVATION-REPLY', json.dumps(failure))
        self.assertTrue(failure['request_id'].startswith('cli-observe-'))
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('explicit retry reprobed')):
            code, known = self.take(self.start(identity=failure['request_id']))
        expected = {**lost[0], 'ready': True, 'known': True, 'request_id': failure['request_id'],
            'records': [{**{k: v for k, v in r.items() if k != 'client_path'},
                         **({'paid_usage_waived_by_attestation': lost[0]['billing_waivers'][r['record_id']]}
                            if r['record_type'] == 'provider_billing' else {})} for r in lost[0]['records']]}
        self.assertEqual((0, expected), (code, known))
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])
        billing_id = next(r['record_id'] for r in lost[0]['records'] if r['record_type'] == 'provider_billing')
        for waivers in ({}, {'PRIVATE-UNKNOWN-RECORD': True}, {billing_id: 1}):
            with self.assertRaisesRegex(ObservationError, 'observation_invalid_response'):
                checked_observation_reply({**lost[0], 'billing_waivers': waivers}, ObservationInput('codex'))

    def test_owner_signed_credits_waiver_is_current_on_known_reply_and_revocation_during_encoding_withholds_it(self):
        path = self.backend.tools/'codex-result.json'
        fixture = json.loads(path.read_text())
        fixture['rateLimits']['credits']['hasCredits'] = True
        path.write_text(json.dumps(fixture))
        with trusted_tools(self.backend.tools):
            code, first = self.take(self.start(identity='credit-original'))
        self.assertEqual(0, code, first)
        billing = next(r for r in first['records'] if r['record_type'] == 'provider_billing')
        self.assertEqual((True, False), (billing['paid_usage_usable'], billing['paid_usage_waived_by_attestation']))
        attest_provider(self.ledger, self.projects[0], self.backend.fixture.base/'test-owner-key', 'codex',
                        billing['account_fingerprint'], credits_spending_disabled=True)
        original, revoked = self.service._reply, []
        def revoke_after_encoding(labels, *, result=None, reason=None):
            raw = original(labels, result=result, reason=reason)
            if result is not None and 'records' in result and not revoked:
                self.ledger._connection.execute("DELETE FROM records WHERE record_type='billing_attestation'")
                revoked.append(True)
            return raw
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('waiver projection reprobed')):
            code, waived = self.take(self.start(identity='credit-original'))
            self.assertEqual(0, code, waived)
            observed = next(r for r in waived['records'] if r['record_type'] == 'provider_billing')
            self.assertEqual((True, True), (observed['paid_usage_usable'], observed['paid_usage_waived_by_attestation']))
            self.assertEqual((True, first['observed_at_ms']), (waived['known'], waived['observed_at_ms']))
            with mock.patch.object(self.service, '_reply', new=revoke_after_encoding):
                code, refused = self.take(self.start(identity='credit-original'))
            self.assertEqual((2, 'observation_not_authorized'), (code, refused['reason_code']))
            code, current = self.take(self.start(identity='credit-original'))
        self.assertEqual(0, code, current)
        observed = next(r for r in current['records'] if r['record_type'] == 'provider_billing')
        self.assertFalse(observed['paid_usage_waived_by_attestation'])
        self.assertEqual(first['observed_at_ms'], current['observed_at_ms'])
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])

    def test_invalid_identity_and_phase_or_missing_account_runtime_never_fall_back(self):
        for options in ({'identity': 'PRIVATE INVALID ID'}, {'identity': 'x'*161},
                        {'extra': ('--phase', 'before')},
                        {'extra': ('--run-handle', 'hrn_'+'0'*48)}):
            code, reply = self.take(self.start(**options))
            self.assertEqual((2, 'observation_invalid_request'), (code, reply['reason_code']))
            if 'identity' in options:
                self.assertNotIn('request_id', reply)
            self.assertNotIn('PRIVATE INVALID ID', json.dumps(reply))
        code, reply = self.take(self.start(extra=('--run-handle', 'hrn_'+'0'*48, '--phase', 'before')))
        self.assertEqual((2, 'observation_not_authorized'), (code, reply['reason_code']))
        self.hint(self.projects[0], self.roots[0], version=1)
        code, reply = self.take(self.start())
        self.assertEqual((2, 'observation_unavailable'), (code, reply['reason_code']))
        self.hint(self.projects[0], self.roots[0], digest='f'*64)
        code, reply = self.take(self.start())
        self.assertEqual((2, 'observation_unavailable'), (code, reply['reason_code']))
        self.assertEqual(0, self.backend.provider_rows())
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])

    def test_default_authorization_failure_reserves_no_request_and_claude_probe_flags_do_not_become_observations(self):
        self.allowed = False
        code, reply = self.take(self.start())
        self.assertEqual((2, 'account_not_authorized'), (code, reply['reason_code']))
        for provider, extra in (('codex', ('--billing-probe',)),
                ('claude', ('--billing-probe', '--request-id', 'not-an-ordinary-probe'))):
            code, reply = self.take(self.start(provider=provider, extra=extra))
            self.assertEqual(2, code, reply)
        self.assertEqual(0, self.backend.provider_rows())
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])

    def test_held_first_project_observation_does_not_block_second_fresh_cli_or_owner_read(self):
        entered, resume = threading.Event(), threading.Event()
        self.gates.append(resume)
        original = providers.observe_codex
        def held(root, **options):
            if root == self.roots[0]:
                entered.set()
                if not resume.wait(10):
                    raise RuntimeError('fixture held observation expired')
            return original(root, **options)
        with trusted_tools(self.backend.tools), mock.patch.object(providers, 'observe_codex', new=held):
            first = self.start(identity='held-first')
            self.wait(entered.is_set)
            second = self.start(identity='second-project', root=self.roots[1])
            code, reply = self.take(second)
            self.assertEqual(0, code, reply)
            self.assertIsNone(first.poll())
            self.assertEqual('active', self.views.project(self.projects[1])['status'])
            self.assertEqual(1, self.jobs.snapshot()['active'])
            resume.set()
            code, reply = self.take(first)
            self.assertEqual(0, code, reply)
