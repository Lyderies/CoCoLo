"""RT-RX real ledger evidence, admission and route/open race boundary."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests import test_providers as fixtures
from tests.test_route_exhausted import head, NOW
from tests.test_routing_risk import base_task, enable_providers
from codex_agents import routing, admission_metadata
from codex_agents.providers import AdmissionDenied, headroom_record
from codex_agents.learning_contracts import content_digest
from test_framework import requirements


class RouteExhaustedOpenTests(unittest.TestCase):
    __why_not_scenario__ = ('In-process service-boundary scenario using real ledger observations and original-open '
                            'admission; native/provider enrollment is supplied by the existing synthetic fixture.')

    def setUp(self):
        self.base = fixtures.AdmissionTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.ledger, self.project = self.base.ledger, self.base.project_id
        self.policy = enable_providers(routing.load_policy(ORCHESTRATION_ROOT / 'config/routing-policy.json'))
        self.accounts = {}
        for provider in ('codex', 'claude'):
            identity = self.base.observe(provider)[0]
            self.accounts[provider] = identity['account_fingerprint']
            self.base.attest(provider, identity['account_fingerprint'])

    def snapshot(self):
        return self.ledger.provider_state_snapshot(self.project, now_ms=self.base.clock + 1)

    def store_head(self, provider, used=95, *, at=None, project=None, reset=None):
        stamp = self.base.tick() if at is None else at
        record = headroom_record(provider=provider, account_fingerprint=self.accounts[provider],
            windows=[{'scope': None, 'window_minutes': 300, 'used_percent': used, 'resets_at': reset}],
            status='allowed' if provider == 'claude' else None, run_id=None, phase=None, observed_at_ms=stamp)
        self.ledger._store_provider_observations(project or self.project, [record], timestamp=stamp)
        return record

    def recover(self, task, opener):
        return routing.open_routed_task(task, self.policy, project_id=self.project,
            snapshot=self.snapshot, open_route=opener, primary_provider='codex')

    @requirements('RT-RX-1', 'RT-RX-6', 'RT-RX-7', 'RT-RX-19')
    def test_service_builds_account_scoped_snapshot_from_foreign_project_evidence(self):
        foreign_root = self.base.project.parent / 'foreign'
        foreign_root.mkdir()
        foreign = self.ledger.register_project(foreign_root, now_ms=1)
        reading = self.store_head('codex', project=foreign)
        snap = self.snapshot()
        state = snap.providers['codex']
        self.assertEqual(('exhausted', self.accounts['codex']), (state['state'], state['account_fingerprint']))
        self.assertIn({'project_id': foreign, 'digest': content_digest(reading)}, state['evidence_refs'])
        self.assertIsNotNone(state['identity_evidence'])
        with self.assertRaises(AdmissionDenied) as raised:
            self.base.admit()
        self.assertEqual('provider_exhausted', raised.exception.reason_code)
        with self.ledger.repository(self.project) as repository:
            decision = repository.records('admission_decision')[-1]
        self.assertNotIn(content_digest(reading), decision['evidence_digests'])
        clearing = self.store_head('codex', 10, project=foreign)
        self.assertEqual('ample', self.snapshot().providers['codex']['state'])
        self.assertEqual('pass', self.base.admit()['decision'])
        self.assertIn({'project_id': foreign, 'digest': content_digest(clearing)},
                      self.snapshot().providers['codex']['evidence_refs'])

    @requirements('RT-RX-13', 'RT-RX-14', 'RT-RX-15', 'RT-RX-18')
    def test_one_reroute_after_a_real_exhaustion_denial_and_verifier_repick(self):
        task = base_task()
        advertised = routing.route_task(task, self.policy, provider_snapshot=self.snapshot(), project_id=self.project)
        first = advertised['provider']
        attempts = []

        def open_route(route):
            attempts.append(route['provider'])
            if len(attempts) == 1:
                self.store_head(first)
            # The real ledger admits at open; a route never grants permission.
            return self.base.open_run(route['provider'])

        result = self.recover(task, open_route)
        self.assertEqual([first, 'claude' if first == 'codex' else 'codex'], attempts)
        self.assertEqual(1, result['route']['availability']['reroute_count'])
        self.assertIn('run_handle', result['open'])
        worker = result['route']['provider']
        verification = base_task(family='verification', verification_for_provider=worker,
                                 verification_for_family='coding')
        actual = routing.route_task(verification, self.policy, provider_snapshot=self.snapshot(), project_id=self.project)
        self.assertEqual((worker, False), (actual['provider'], actual['independent']))

    @requirements('RT-RX-14', 'RT-RX-15', 'RT-RX-16')
    def test_second_exhaustion_stops_and_other_denials_never_retry(self):
        for reason, expected in (('provider_exhausted', 2), ('window_run_cap_reached', 1),
                                 ('paid_usage_usable', 1), ('provider_identity_missing', 1)):
            attempts = []

            def denied(route):
                attempts.append(route['provider'])
                raise AdmissionDenied(reason)

            result = self.recover(base_task(), denied)
            self.assertEqual(expected, len(attempts))
            self.assertFalse(result['ready'])
            self.assertEqual(reason, result['reason_code'])

    @requirements('RT-RX-3', 'RT-RX-4', 'RT-RX-5', 'RT-RX-15', 'RT-RX-20', 'RT-RX-21')
    def test_newer_reading_clears_at_real_open_but_api_key_and_explicit_reset_still_deny(self):
        self.store_head('codex')
        self.store_head('codex', 10)
        self.assertEqual('ample', self.snapshot().providers['codex']['state'])
        self.assertIn('run_handle', self.base.open_run())
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture-key'}):
            with self.assertRaises(AdmissionDenied) as raised:
                self.base.open_run()
        self.assertEqual('provider_api_key_present', raised.exception.reason_code)
        self.store_head('codex', reset=self.base.clock + 10_000)
        self.store_head('codex', 10)
        with self.assertRaises(AdmissionDenied) as raised:
            self.base.open_run()
        self.assertEqual('provider_exhausted', raised.exception.reason_code)

    @requirements('RT-RX-6')
    def test_snapshot_and_open_share_identity_selector_with_future_and_account_switch(self):
        old = self.accounts['claude']
        self.store_head('claude')
        switched = self.base.observe('claude', orgId='new-trusted-account')[0]
        self.base.attest('claude', switched['account_fingerprint'])
        # A later future identity is not current, just as at open.
        self.base.observe('claude', now_ms=self.base.clock + 10_000, orgId='future-account')
        with mock.patch.object(admission_metadata, 'select_provider_identity',
                               wraps=admission_metadata.select_provider_identity) as select:
            state = self.snapshot().providers['claude']
            admitted = self.base.admit('claude')
        self.assertNotEqual(old, state['account_fingerprint'])
        self.assertEqual(switched['account_fingerprint'], state['account_fingerprint'])
        self.assertEqual(state['account_fingerprint'], admitted['account_fingerprint'])
        self.assertEqual(content_digest(switched), state['identity_evidence'])
        self.assertEqual(3, select.call_count)  # two snapshot providers and actual admission

    @requirements('RT-RX-11', 'RT-RX-18')
    def test_verifier_configuration_and_containment_skips_are_audited(self):
        policy = deepcopy(self.policy)
        policy['provider_routing']['providers']['claude']['enabled'] = False
        audit = {'providers': {p: {'state': 'unknown'} for p in ('codex', 'claude')}, 'candidates': []}
        routing._select_verifier(policy, 'coding', worker_provider='codex', primary_provider='codex',
            use_legacy_fallback=False, risk='medium', availability=audit)
        self.assertIn({'phase': 'verifier', 'provider': 'claude', 'skip': 'configuration'}, audit['candidates'])
        with mock.patch.object(routing, '_codex_verifier', return_value={
                'model': 'gpt-6.1-sol', 'sandbox_mode': 'workspace-write'}):
            audit['candidates'] = []
            routing._select_verifier(self.policy, 'coding', worker_provider='claude', primary_provider='claude',
                use_legacy_fallback=False, risk='medium', availability=audit)
        self.assertEqual(('codex', 'containment'),
                         (audit['candidates'][0]['provider'], audit['candidates'][0]['skip']))

    @requirements('RT-RX-3', 'RT-RX-7', 'RT-RX-13', 'RT-RX-14', 'RT-RX-16', 'RT-RX-18')
    def test_real_cli_service_exhaustion_recovery_and_2026_11_24_stale_block(self):
        """Real CLI, account snapshot wire and ledger admission; synthetic native delivery.

        No model is launched. The enrolled delivery adapter uses the ledger's
        original-open entry point for both providers, with all admission gates live.
        """
        import contextlib
        import io
        import json
        import sys
        from contextlib import ExitStack
        import cli
        from codex_agents.account_api import (AccountClient, account_request_digest, decode_account_request)
        from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
        from tests.learning_support import assignment
        from tests.test_bridge import PARENT_GRANT

        import shutil
        from tests.support import FIXTURES_ROOT
        bundle = self.base.project.parent / 'bundle'
        shutil.copytree(ORCHESTRATION_ROOT.parent.parent, bundle, ignore=shutil.ignore_patterns('__pycache__'))
        (bundle / '.claude/agents').mkdir(parents=True)
        for name in ('claude_agents_implementation_worker', 'claude_agents_verification_worker', 'claude_agents_researcher'):
            target = bundle / ('.claude/agents/' + name + '.md')
            source = FIXTURES_ROOT / 'claude/claude_agents_implementation_worker.md'
            target.write_text(source.read_text().replace('claude_agents_implementation_worker', name))
        runtime = 'a' * 64
        token = object()
        def authorize(request, peer):
            if peer is token:
                return AccountAuthorization(self.project, runtime, account_request_digest(request),
                    'host', frozenset({'account.provider_state_snapshot'}))
        service = AccountService(runtime, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(service.close)
        client = AccountClient(self.base.state.root / 'synthetic-account.sock', self.project, runtime)
        def exchange(path, raw, **kwargs):
            request = decode_account_request(raw)
            self.assertEqual('account.provider_state_snapshot', request.operation)
            return service.handle(raw, token)
        def status(provider, value):
            stamp = self.base.tick()
            record = headroom_record(provider=provider, account_fingerprint=self.accounts[provider],
                windows=[], status=value, run_id=None, phase=None, observed_at_ms=stamp)
            self.ledger._store_provider_observations(self.project, [record], timestamp=stamp)
        orchestration = self.ledger.open_orchestration(self.project, client_version='codex-host-v1', now_ms=self.base.clock)
        task = base_task(family='research')
        parent = deepcopy(PARENT_GRANT)
        counter = 0
        def invoke(command, task, route=None, expected=0):
            nonlocal counter
            counter += 1
            values = {'task': task}
            if route is not None:
                assigned = assignment()
                assigned.update(assignment_id='cli-rtrx-' + str(counter), task_id=task['id'],
                    meaningful=task['meaningful'], role=route['role'], model=route['model'], effort=route['effort'],
                    verification_required=route['verification_required'], bounds=deepcopy(route['bounds']))
                values.update(route=route, assignment=assigned, **{'parent-grant': parent})
            args = ['cli.py', command, '--state-root', str(self.base.state.root), '--project-root', str(self.base.project)]
            for name, value in values.items():
                path = self.base.project.parent / (str(counter) + '-' + name + '.json')
                path.write_text(json.dumps(value))
                args.extend(['--' + name, str(path)])
            if route is not None:
                args.extend(['--orchestration-handle', orchestration['orchestration_handle']])
            output = io.StringIO()
            failures = []
            original_print = cli._print
            def diagnostic_print(value):
                if sys.exception() is not None:
                    failures.append(repr(sys.exception()))
                return original_print(value)
            with mock.patch.object(sys, 'argv', args), contextlib.redirect_stdout(output), \
                    mock.patch.object(cli, '_print', side_effect=diagnostic_print):
                code = cli.main()
            self.assertEqual(expected, code, (output.getvalue(), failures))
            return json.loads(output.getvalue())
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, 'BUNDLE_ROOT', bundle))
            stack.enter_context(mock.patch.object(cli, '_require_protected_runtime'))
            stack.enter_context(mock.patch.object(cli, '_require_own_roots'))
            stack.enter_context(mock.patch.object(cli, '_host_client', return_value='codex-host-v1'))
            stack.enter_context(mock.patch.object(cli, 'load_effective_policy', return_value=self.policy))
            stack.enter_context(mock.patch.object(cli, 'provider_state_snapshot_client', side_effect=lambda *a, **k: client.provider_state_snapshot()))
            stack.enter_context(mock.patch('codex_agents.account_api.exchange_frame', side_effect=exchange))
            stack.enter_context(mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.base.clock + 1))
            stack.enter_context(mock.patch.object(cli, '_learning_ledger', return_value=(self.ledger, self.project)))
            stack.enter_context(mock.patch.object(cli, '_close_ledger'))
            stack.enter_context(mock.patch.object(cli, '_host_driven', return_value=False))
            stack.enter_context(mock.patch.dict('os.environ', {}, clear=True))
            status('claude', 'rejected')  # The exact windowless stale-block shape.
            self.store_head('codex', 10)
            worker = invoke('learning-route', task)
            self.assertEqual(('codex', 'codex', False),
                (worker['provider'], worker['verifier']['provider'], worker['verifier']['independent']))
            self.assertTrue(worker['availability']['usage_switch'])
            opened = invoke('learning-open-host-run', task, worker)
            self.assertEqual('admitted', opened['admission'])
            verification = base_task(id='fresh-verifier', family='verification',
                verification_for_provider=worker['provider'], verification_for_family='research')
            stale_verifier = invoke('learning-route', verification)
            self.assertEqual(('codex', False), (stale_verifier['provider'], stale_verifier['independent']))
            self.store_head('codex', 95)
            refused = invoke('learning-route', task, expected=2)
            self.assertEqual(('unsatisfied', False, 'no_provider_allowance'),
                (refused['status'], refused['ready'], refused['reason_code']))
            self.assertIn('no provider has allowance; next reset at', refused['error'])
            refused_open = invoke('learning-open-host-run', task, worker, expected=2)
            self.assertEqual(('unsatisfied', False, 'no_provider_allowance'),
                (refused_open['status'], refused_open['ready'], refused_open['reason_code']))
            self.assertTrue(all(v['state'] == 'exhausted' for v in refused['providers'].values()))
            status('claude', 'allowed')
            recovered = invoke('learning-open-host-run', verification, stale_verifier)
            self.assertEqual(('claude', 'admitted'), (recovered['route']['provider'], recovered['admission']))
            # Race at the real admission boundary: initial Claude pick exhausts
            # after the snapshot; the same CLI call may re-route exactly once.
            self.store_head('codex', 10)
            self.base.clean_claude_billing(self.base.tick())
            race_task = base_task(id='open-race', family='research')
            race_route = invoke('learning-route', race_task)
            original_open = self.ledger.open_host_run
            providers = []
            def racing_open(*args, **kwargs):
                providers.append(kwargs['route']['provider'])
                if len(providers) == 1:
                    status('claude', 'rejected')
                return original_open(*args, **kwargs)
            with mock.patch.object(self.ledger, 'open_host_run', side_effect=racing_open):
                raced = invoke('learning-open-host-run', race_task, race_route)
            self.assertEqual(['claude', 'codex'], providers)
            audit = raced['route']['availability']
            self.assertEqual((1, 2, 'provider_exhausted'),
                (audit['reroute_count'], audit['open_attempt_count'], audit['reroute_trigger']))
            self.assertEqual(['provider_exhausted', 'admitted'], [a['reason_code'] for a in audit['open_attempts']])
            self.assertNotEqual(audit['original_open_ref'], audit['replacement_open_ref'])
            self.assertEqual(self.project, audit['open_attempts'][0]['denial_reference']['project_id'])
            with self.ledger.repository(self.project) as repository:
                stored = repository.records('route_availability')[-1]
            self.assertEqual(audit, stored['availability'])
            admitted = self.ledger.host_run_admission(self.project, raced['run_handle'])
            self.assertEqual(admitted['run_id'], stored['run_id'])
