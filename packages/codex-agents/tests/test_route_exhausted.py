"""RT-RX: pure usage classification and policy-preserving availability routing."""
from copy import deepcopy
import itertools
import unittest

from tests.support import ORCHESTRATION_ROOT
from tests.test_routing_risk import base_task, enable_providers, FAMILIES, RISKS, with_failures, build_policy
from codex_agents import admission_metadata as metadata, routing, provider_restrictions
from codex_agents.learning_contracts import content_digest
from test_framework import requirements

NOW = 10_000_000_000
ACCOUNT = 'a' * 64
PURE = 'Pure classifier and selector cross products have no service or persistence effects.'


def head(provider='codex', used=95, at=NOW, reset=None, status=None, account=ACCOUNT):
    return {'provider': provider, 'account_fingerprint': account, 'observed_at_ms': at,
            'status': status if status is not None else ('allowed' if provider == 'claude' else None),
            'windows': [] if used == 'empty' else [
                {'scope': None, 'window_minutes': 300, 'used_percent': used, 'resets_at': reset}]}


def pairs(*records):
    return [(record, content_digest(record)) for record in records]


def snapshot(codex='ample', claude='ample'):
    states = {}
    for provider, state in (('codex', codex), ('claude', claude)):
        records = [] if state == 'unknown' else pairs(head(provider, {'ample': 10, 'constrained': 80, 'exhausted': 95}[state]))
        states[provider] = {**metadata.classify_provider_state(provider, ACCOUNT, records, [], NOW),
                            'account_fingerprint': ACCOUNT, 'identity_evidence': '1' * 64}
    return metadata.ProviderStateSnapshot('project', NOW, states)


class RouteExhaustedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = enable_providers(routing.load_policy(ORCHESTRATION_ROOT / 'config/routing-policy.json'))

    def route(self, task=None, policy=None, states=('ample', 'ample'), primary='codex'):
        return routing.route_task(task or base_task(), policy or self.policy, primary_provider=primary,
                                  provider_snapshot=snapshot(*states), project_id='project')

    @requirements('RT-RX-1', 'RT-RX-2', why_not_scenario=PURE)
    def test_boundaries_unknown_and_billing_share_the_admission_classifier(self):
        for provider, (used, expected) in itertools.product(('codex', 'claude'),
                ((74.99, 'ample'), (75, 'constrained'), (89.99, 'constrained'), (90, 'exhausted'))):
            records = pairs(head(provider, used))
            result = metadata.classify_provider_state(provider, ACCOUNT, records, [], NOW)
            self.assertEqual((expected, True), (result['state'], result['known']))
            self.assertEqual(expected, metadata.headroom_state(provider, ACCOUNT, records, [], NOW)[0])
        for records in ([], pairs(head(used=None)), pairs(head(reset=NOW))):
            result = metadata.classify_provider_state('codex', ACCOUNT, records, [], NOW)
            self.assertEqual(('unknown', False), (result['state'], result['known']))
            self.assertEqual(('ample', None), metadata.headroom_state('codex', ACCOUNT, records, [], NOW))
        billing = pairs({'provider': 'codex', 'account_fingerprint': ACCOUNT,
                         'observed_at_ms': NOW + 1, 'ordinary_usage_allowed': False})
        result = metadata.classify_provider_state('codex', ACCOUNT, [], billing, NOW)
        self.assertEqual('exhausted', result['state'])
        self.assertEqual('ordinary_usage_denied', result['reasons'][0])
        self.assertEqual([billing[0][1]], result['evidence_digests'])

    @requirements('RT-RX-3', 'RT-RX-4', 'RT-RX-5', why_not_scenario=PURE)
    def test_chronology_five_hours_explicit_resets_and_rollback(self):
        for provider in ('codex', 'claude'):
            restrictive = head(provider, 95, at=NOW - 100)
            favorable = head(provider, 10, at=NOW - 1)
            for later, expected in ((favorable, 'ample'),
                    ({**favorable, 'observed_at_ms': NOW - 100}, 'exhausted'),
                    ({**favorable, 'observed_at_ms': NOW + 1}, 'exhausted'),
                    ({**favorable, 'account_fingerprint': 'b' * 64}, 'exhausted'),
                    (head(provider, None, at=NOW), 'exhausted')):
                for records in (pairs(restrictive, later), pairs(later, restrictive)):
                    self.assertEqual(expected, metadata.headroom_state(provider, ACCOUNT, records, [], NOW)[0])
            for age, expected in ((18_000_000 - 1, 'exhausted'), (18_000_000, 'exhausted'),
                                  (18_000_001, 'unknown'), (-1, 'exhausted')):
                result = metadata.classify_provider_state(provider, ACCOUNT,
                    pairs(head(provider, 95, at=NOW - age)), [], NOW)
                self.assertEqual(expected, result['state'])
                self.assertIsNone(result['next_reset_ms'])
            explicit = head(provider, 95, at=NOW - 100, reset=NOW + 1)
            self.assertEqual('exhausted', metadata.headroom_state(provider, ACCOUNT, pairs(explicit, favorable), [], NOW)[0])
            self.assertEqual('ample', metadata.headroom_state(provider, ACCOUNT, pairs(explicit, favorable), [], NOW + 1)[0])
        for status, expected in (('rejected', 'exhausted'), ('allowed_warning', 'constrained')):
            old = head('claude', 'empty', at=NOW - 1, status=status)
            self.assertEqual(expected, metadata.headroom_state('claude', ACCOUNT, pairs(old), [], NOW)[0])
            self.assertEqual('ample', metadata.headroom_state('claude', ACCOUNT,
                pairs(old, head('claude', 'empty', status='allowed')), [], NOW)[0])

    @requirements('RT-RX-8', 'RT-RX-9', 'RT-RX-10', 'RT-RX-11', why_not_scenario=PURE)
    def test_policy_order_survives_all_non_exhausted_states_and_retries(self):
        policy = build_policy(self.policy)
        for primary in ('codex', 'claude'):
            for states in itertools.product(('ample', 'constrained', 'unknown'), repeat=2):
                baseline = self.route(policy=policy, primary=primary)
                actual = self.route(policy=policy, primary=primary, states=states)
                self.assertEqual(baseline['selection'], actual['selection'])
                self.assertEqual(baseline['provider'], actual['provider'])
            for failed in (0, 1, 2):
                task = with_failures(base_task(), failed)
                result = self.route(task, policy, states=('exhausted', 'constrained'), primary=primary)
                self.assertEqual('claude', result['provider'])
                self.assertEqual('claude', result['verifier']['provider'])
                self.assertEqual(failed, task['failed_attempts'])

    @requirements('RT-RX-11', 'RT-RX-12', 'RT-RX-21', why_not_scenario=PURE)
    def test_every_same_provider_pair_has_an_explicit_non_independent_marker(self):
        for provider, family, risk in itertools.product(('codex', 'claude'), FAMILIES, RISKS):
            if family == 'verification':
                continue
            states = ('ample', 'exhausted') if provider == 'codex' else ('exhausted', 'ample')
            task = base_task(family=family, risk=risk)
            route = self.route(task, states=states, primary=provider)
            self.assertEqual((provider, False), (route['verifier']['provider'], route['verifier']['independent']))
            verification = base_task(family='verification', risk=risk, verification_for_provider=provider,
                                     verification_for_family=family)
            actual = self.route(verification, states=states, primary=provider)
            self.assertEqual((provider, False), (actual['provider'], actual['independent']))
            diverse = self.route(verification, primary=provider)
            self.assertNotIn('independent', diverse)

    @requirements('RT-RX-7', 'RT-RX-16', 'RT-RX-17', 'RT-RX-18', 'RT-RX-19', why_not_scenario=PURE)
    def test_unsatisfied_configuration_and_offline_audit_are_distinct(self):
        result = self.route(states=('exhausted', 'exhausted'))
        self.assertEqual(('unsatisfied', 'no_provider_allowance', 'worker'),
                         (result['status'], result['reason_code'], result['phase']))
        self.assertNotIn('model', result)
        for provider in ('codex', 'claude'):
            self.assertEqual(('exhausted', 'usage', None), (result['providers'][provider]['state'],
                result['providers'][provider]['cause'], result['providers'][provider]['next_reset_ms']))
        blocked = self.route(policy=enable_providers(deepcopy(self.policy), bridge=False),
                             states=('exhausted', 'ample'))
        self.assertEqual('no_eligible_provider', blocked['reason_code'])
        self.assertEqual('configuration', blocked['providers']['claude']['cause'])
        offline = routing.route_task(base_task(), self.policy)
        self.assertEqual('not_checked', offline['availability']['status'])
        with self.assertRaises(Exception):
            routing.route_task(base_task(), self.policy, provider_snapshot=snapshot(), project_id='foreign')
        self.assertEqual(self.route(), self.route())

    @requirements('RT-RX-20', 'RT-RX-4', 'RT-RX-5', why_not_scenario=PURE)
    def test_merge_preserves_resetless_provenance_and_later_clearing(self):
        from codex_agents.providers import headroom_record
        restrictive = headroom_record(provider='claude', account_fingerprint=ACCOUNT, windows=[],
            status='rejected', run_id='run-1', phase='during', observed_at_ms=NOW - 100)
        merged = provider_restrictions.merged_restriction([restrictive], observed_at_ms=NOW)
        later = head('claude', 10, at=NOW - 1)
        before = metadata.classify_provider_state('claude', ACCOUNT, pairs(restrictive, later), [], NOW)
        after = metadata.classify_provider_state('claude', ACCOUNT, pairs(merged, later), [], NOW)
        self.assertEqual('ample', before['state'])
        self.assertEqual(before['state'], after['state'])
        result = metadata.classify_provider_state('claude', ACCOUNT, pairs(merged), [], NOW)
        self.assertIsNone(result['next_reset_ms'])
        self.assertEqual(NOW - 100 + 18_000_001, result['fallback_expiry_ms'])
