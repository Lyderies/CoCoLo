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
