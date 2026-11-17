"""Fresh guarded CLIs and actual account ingress; native peer identity is synthetic."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import test_account_billing_jobs as fixtures
from tests import test_bridge as bridge_fixtures
from codex_agents.account_ingress import AccountIngress, AccountIngressLimits
from codex_agents.account_service import AccountService
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
from codex_agents.control_api import ControlScope
from codex_agents.learning_contracts import canonical_json, keyed_digest


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'original fixed macOS signature worker guard')
class CliBillingRuntimeTests(unittest.TestCase):
    launches = fixtures.AccountBillingJobTests.launches

    def setUp(self):
        fixtures.AccountBillingJobTests.setUp(self)
        self.service.close()
        self.runtime = COLLECTOR_RUNTIME_DIGEST
        self.service = AccountService(self.runtime, self.views, authorizer=self.authorize,
                                      claim_jobs=self.claims, observation_jobs=self.observations)
        self.ingress = AccountIngress(self.service, AccountIngressLimits(4, 1, 5, 2, 15_000_000_000, 1_000_000))
        self.ingress.bootstrap()
        self.state = CollectorPaths(self.fixture.state.root)
        self.endpoint = self.state.root/'billing.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.clients = []
        self.hint(self.fixture.project_id, self.fixture.project)

    def hint(self, project, root):
        path = runtime_hint_path(self.state, root)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(canonical_json({'schema_version': 2,
            'project_root_digest': keyed_digest(self.state.digest_key(), str(root.resolve())),
            'scope': asdict(ControlScope(project_id=project, primary_session_id='billing-primary',
                runtime_digest=self.runtime, supervisor_instance_id='billing-supervisor', supervisor_generation=1)),
            'dispatch_endpoint': str(self.state.root/'absent-dispatch.sock'),
            'result_endpoint': str(self.state.root/'absent-result.sock'), 'account_endpoint': str(self.endpoint)}))
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
            self.assertLess(time.monotonic(), end, 'bounded billing CLI progress expired')
            time.sleep(.002)

    def start(self, *, identity=None, root=None, host='codex', billing=True, provider='claude', extra=(), environment=None):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        if host == 'codex':
            env['CODEX_THREAD_ID'] = 'fixture-billing-primary'
        else:
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='fixture-billing-primary')
        env.update(environment or {})
        args = [] if identity is None else ['--request-id', identity]
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.dispatch_cli_fixture', 'provider-observe',
            '--project-root', str(root or self.fixture.project), '--state-root', str(self.state.root),
            '--provider', provider, *(['--billing-probe'] if billing else []), *args, *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        return process

    def take(self, process):
        self.wait(lambda: process.poll() is not None)
        output, errors = process.communicate(timeout=2)
        self.assertEqual(b'', errors)
        return process.returncode, json.loads(output)

    def stop(self):
        for process in getattr(self, 'clients', ()):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        if hasattr(self, 'listener'):
            self.listener.close()
            self.ingress.request_close()
            end = time.monotonic()+10
            while not self.ingress.drain():
                self.assertLess(time.monotonic(), end, 'actual account ingress did not drain')
                time.sleep(.005)
        fixtures.AccountBillingJobTests.stop(self)

    def test_both_primary_hosts_use_account_api_and_preserve_launched_projection_and_redaction(self):
        for host in ('codex', 'claude'):
            code, result = self.take(self.start(host=host))
            self.assertEqual(0, code, result)
            self.assertTrue(result['ready'])
            self.assertIn(result['probe'], {'launched', 'reused'})
            self.assertTrue(result['request_id'].startswith('cli-billing-'))
            self.assertTrue(all('client_path' not in r for r in result['records']))
            self.assertFalse(next(r for r in result['records'] if r['record_type'] == 'provider_billing')[
                'paid_usage_waived_by_attestation'])
        self.assertEqual(1, len(self.launches()))

    def test_explicit_known_request_returns_original_records_without_another_observation_or_model(self):
        code, first = self.take(self.start(identity='original-cli-probe'))
        self.assertEqual(0, code, first)
        with mock.patch.object(self.billing._observations, 'submit', side_effect=AssertionError('known identity reprobe')):
            code, known = self.take(self.start(identity='original-cli-probe'))
        self.assertEqual((0, {**first, 'known': True}), (code, known))
        self.assertEqual(1, len(self.launches()))

    def test_lost_committed_reply_retains_generated_id_and_explicit_recovery_never_reprobes(self):
        original, lost = self.service._reply, []
        def withhold(labels, *, result=None, reason=None):
            if result is not None and result.get('probe') == 'completed' and not lost:
                lost.append(result)
                raise OSError('PRIVATE-BILLING-REPLY-LOSS')
            return original(labels, result=result, reason=reason)
        with mock.patch.object(self.service, '_reply', new=withhold):
            code, failed = self.take(self.start())
        self.assertEqual((2, 'probe_unavailable'), (code, failed['reason_code']))
        self.assertTrue(failed['request_id'].startswith('cli-billing-'))
        self.assertNotIn('PRIVATE-BILLING', json.dumps(failed))
        self.assertEqual(1, len(lost))
        code, known = self.take(self.start(identity=failed['request_id']))
        self.assertEqual(0, code, known)
        self.assertTrue(known['known'])
        self.assertEqual(1, len(self.launches()))

    def test_paid_usage_failure_is_fixed_and_recovery_does_not_refresh_clean_billing(self):
        self.fixture.frames('claude', [bridge_fixtures.PROBE_INIT, bridge_fixtures.OVERAGE, {'sleep': 3}])
        code, failed = self.take(self.start(identity='paid-failure'))
        self.assertEqual((2, 'probe_incomplete'), (code, failed['reason_code']))
        self.assertEqual('paid-failure', failed['request_id'])
        self.assertEqual(1, len(self.launches()))
        code, known = self.take(self.start(identity='paid-failure'))
        self.assertEqual((2, 'probe_incomplete'), (code, known['reason_code']))
        self.assertEqual(1, len(self.launches()))

    def test_invalid_input_and_missing_endpoint_refuse_without_local_persistence_or_model(self):
        for options, expected in (({'identity': 'PRIVATE invalid'}, 'probe_invalid_request'),
                                  ({'extra': ('--phase', 'before')}, 'probe_invalid_request')):
            code, failed = self.take(self.start(**options))
            self.assertEqual((2, expected), (code, failed['reason_code']))
            self.assertNotIn('PRIVATE', json.dumps(failed))
        self.endpoint.unlink()
        code, failed = self.take(self.start(identity='missing-endpoint'))
        # COCOLO's own account service is unreachable: never reported as a provider client problem.
        self.assertEqual((2, 'account_unavailable'), (code, failed['reason_code']))
        self.assertEqual('missing-endpoint', failed['request_id'])
        self.assertEqual([], self.launches())

    def test_client_api_key_environment_refuses_before_account_intake(self):
        code, failed = self.take(self.start(identity='api-key-refusal', environment={'ANTHROPIC_API_KEY': 'PRIVATE-TEST-KEY'}))
        self.assertEqual((2, 'provider_api_key_present'), (code, failed['reason_code']))
        self.assertEqual('api-key-refusal', failed['request_id'])
        self.assertNotIn('PRIVATE-TEST-KEY', json.dumps(failed))
        self.assertEqual([], self.launches())
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_billing_probe_origins').fetchone()[0])

    def test_C9_a_probe_beside_an_observation_of_the_same_provider_leaves_nothing_stuck(self):
        # 0.3.3 requirement C9: a Claude billing probe and a plain Claude observation of the same project,
        # at the same time. Each succeeds or refuses with its own reason; then a probe on its own succeeds.
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                probe = self.start(identity=f'overlap-probe-{attempt}')
                observe = self.start(billing=False)
                (_, probed), (_, observed) = self.take(probe), self.take(observe)
                self.assertIn(probed.get('reason_code'), {None, 'probe_conflict', 'probe_busy'}, probed)
                self.assertIn(observed.get('reason_code'), {None, 'observation_full'}, observed)
        self.assertEqual([], [job.key for job in self.billing._jobs.values() if job.state == 'unknown'])
        code, alone = self.take(self.start(identity='probe-alone'))
        self.assertEqual((0, None), (code, alone.get('reason_code')), alone)

    def test_C9_an_observation_landing_between_capture_and_admit_leaves_the_probe_worker_free(self):
        # 0.3.3 requirement C9, the control project's order: the probe has captured its admission
        # history, then an observation of the same provider records, then the probe is admitted.
        probe = self.start(identity='captured-then-observed')
        self.wait(lambda: any(j.state == 'signature' for j in self.billing._jobs.values()))
        code, observed = self.take(self.start(billing=False))
        self.assertEqual(0, code, observed)
        code, probed = self.take(probe)
        self.assertIn(probed.get('reason_code'), {None, 'probe_conflict'}, probed)
        self.wait(lambda: not self.billing._jobs)  # Nothing is kept, so the one probe worker is free.
        code, alone = self.take(self.start(identity='probe-after-overlap'))
        self.assertEqual((0, None), (code, alone.get('reason_code')), alone)

    def test_second_project_fresh_cli_observes_while_first_probe_process_is_held(self):
        # The first probe is held well past what a fresh CLI process takes to start on a slow machine.
        self.fixture.frames('claude', [bridge_fixtures.PROBE_INIT, {'sleep': 8},
                                     bridge_fixtures.FAVOURABLE, bridge_fixtures.result_frame('OK')])
        first = self.start(identity='held-first')
        self.wait(lambda: any(j.state == 'offered' and j.supervisor is not None
                             and j.supervisor.snapshot()['state'] == 'active' for j in self.billing._jobs.values()))
        second_root = self.fixture.project.parent/'second-project'
        second_root.mkdir()
        second = self.ledger.register_project(second_root)
        self.contexts[second] = fixtures.ObservationContext(str(second_root))
        self.hint(second, second_root)
        start = time.monotonic()
        code, observed = self.take(self.start(root=second_root, billing=False))
        self.assertEqual(0, code, observed)
        self.assertLess(time.monotonic()-start, 6)
        self.assertIsNone(first.poll())  # Answered while the first probe was still held.
        self.assertEqual(0, self.take(first)[0])
