"""USAGE-RECHECK on the real wire: a far-reset exhaustion is re-read through the running account services.

The 0.8.55 install showed Claude exhausted until Sat 2026-11-28 01:00 after the owner had used an early
reset: routing skipped Claude, so no Claude child gave a newer reading, the billing probe refused an
exhausted account, and a measured reset was never cleared by a later reading. Here the real CLI talks to
the real in-process account and project services (service harness); only the Claude client is the
harness fake (its stream frames), no model is launched.
"""
import unittest
from unittest import mock

import tests.support  # noqa: F401 -- the source CLI import path
from tests import test_bridge
from tests.test_bridge import ClaudePrimaryCase, PROBE_INIT, result_frame
from codex_agents.providers import claude_rate_limit_records
from codex_agents import account_billing_admission
from codex_agents.providers import headroom_record
from test_framework import requirements

MINUTE = 60_000
DAY = 86_400_000
OVERAGE_OFF = {'isUsingOverage': False, 'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'}


def weekly(status, utilization, resets_at_s):
    return {'status': status, 'unifiedWindows': {'seven_day': {
        'status': status, 'utilization': utilization, 'resetsAt': resets_at_s}}, **OVERAGE_OFF}


class UsageRecheckLiveTests(ClaudePrimaryCase):
    __why_not_scenario__ = ('Real CLI, real account and project services in-process (service harness); '
                            'the Claude client is the harness fake, no model is launched.')
    launches = test_bridge.ClaudeHostReviewTests.launches

    def exhaust_claude(self, age_ms):
        """Store a Claude weekly rejection (reset three days away) observed `age_ms` ago."""
        fingerprint = self.admit('claude')
        self.admit('codex')
        stamp = self.ledger._clock_ms()
        self.far_reset_s = (stamp + 3 * DAY) // 1000
        records = claude_rate_limit_records(weekly('rejected', 1.0, self.far_reset_s),
            account_fingerprint=fingerprint, observed_at_ms=stamp - age_ms)
        self.ledger._store_provider_observations(self.project_id, records, timestamp=stamp)
        return stamp

    def probe_frames(self, info):
        self.frames('claude', [PROBE_INIT, {'type': 'rate_limit_event', 'session_id': 'session-1',
                                            'uuid': 'rate-1', 'rate_limit_info': info}, result_frame('OK')])

    def early_reset(self):
        self.probe_frames(weekly('allowed', 0.02, (self.ledger._clock_ms() + 7 * DAY) // 1000))

    def route(self):
        return self.cli('learning-route', '--task', self.write('task.json', self.task()),
                        '--host-client', 'claude-host-v1')

    @requirements('USAGE-RECHECK-2')
    def test_a_due_recheck_probe_runs_while_claude_is_exhausted(self):
        self.exhaust_claude(61 * MINUTE)
        self.early_reset()
        before = self.launches()
        observed = self.cli('provider-observe', '--provider', 'claude', '--billing-probe')
        self.assertEqual(('launched', before + 1), (observed.get('probe'), self.launches()), observed)

    @requirements('USAGE-RECHECK-2', 'USAGE-RECHECK-5')
    def test_a_probe_before_the_interval_is_still_refused_as_exhausted(self):
        self.exhaust_claude(10 * MINUTE)
        self.early_reset()
        before = self.launches()
        refused = self.cli('provider-observe', '--provider', 'claude', '--billing-probe', expected=2)
        self.assertEqual('provider_exhausted', refused['reason_code'])
        self.assertEqual(before, self.launches())

    @requirements('USAGE-RECHECK-4', 'USAGE-RECHECK-3', 'USAGE-RECHECK-5')
    def test_the_route_rechecks_once_and_an_early_reset_clears_claude(self):
        self.exhaust_claude(61 * MINUTE)
        self.early_reset()
        before = self.launches()
        route = self.route()
        availability = route['availability']
        self.assertEqual(before + 1, self.launches())
        self.assertEqual('cleared', availability.get('recheck', {}).get('claude', {}).get('outcome'), availability)
        self.assertNotEqual('exhausted', availability['providers']['claude']['state'], availability)
        self.assertNotIn({'phase': 'worker', 'provider': 'claude', 'skip': 'usage'}, availability['candidates'])

    @requirements('USAGE-RECHECK-4', 'USAGE-RECHECK-5')
    def test_a_still_exhausted_recheck_skips_claude_and_is_not_repeated(self):
        self.exhaust_claude(61 * MINUTE)
        self.probe_frames(weekly('rejected', 1.0, self.far_reset_s))
        before = self.launches()
        first = self.route()
        self.assertEqual(before + 1, self.launches())
        self.assertEqual('still_exhausted', first['availability'].get('recheck', {}).get('claude', {}).get('outcome'),
                         first['availability'])
        self.assertEqual('exhausted', first['availability']['providers']['claude']['state'])
        self.assertNotEqual('claude', first['provider'])
        second = self.route()
        self.assertEqual(before + 1, self.launches())  # the stored re-check reading starts a new interval
        self.assertNotIn('recheck', second['availability'])
        self.assertNotEqual('claude', second['provider'])


class UsageRecheckReviewLiveTests(ClaudePrimaryCase):
    """Review round 1 (NOT-READY) HIGH 2 and the ledger-path MED, on the real wire."""
    __why_not_scenario__ = UsageRecheckLiveTests.__why_not_scenario__
    launches = UsageRecheckLiveTests.launches
    exhaust_claude, probe_frames = UsageRecheckLiveTests.exhaust_claude, UsageRecheckLiveTests.probe_frames
    early_reset, route = UsageRecheckLiveTests.early_reset, UsageRecheckLiveTests.route

    def codex_reads(self):
        log = self.tools / 'methods.log'
        return log.read_text().splitlines().count('account/rateLimits/read') if log.exists() else 0

    def exhaust_codex(self):
        """Codex observed 61 minutes ago at 95% of its weekly window (reset three days away); its sign-in is fresh."""
        from tests.learning_support import attest_provider
        from codex_agents.providers import _record
        self.admit('claude')
        stamp = self.ledger._clock_ms()
        old = stamp - 61 * MINUTE
        self.codex_answers(used=95, reset=stamp + 3 * DAY)
        with mock.patch.object(self.collector_ledger, '_clock_ms', return_value=old):
            identity = self.ledger.observe_provider(self.project_id, 'codex', project_root=self.tools,
                                                    environ={})[0]
        attest_provider(self.ledger, self.project_id, self.owner_key, 'codex', identity['account_fingerprint'])
        fresh = _record('provider_identity', {**{k: v for k, v in identity.items()
            if k not in ('schema_version', 'record_type', 'record_id')}, 'observed_at_ms': stamp})
        self.ledger._store_provider_observations(self.project_id, [fresh], timestamp=stamp)
        return old

    def codex_answers(self, used=None, reset=None):
        import json
        path = self.tools / 'codex-result.json'
        fixture = json.loads(path.read_text())
        if used is None:
            fixture.pop('rateLimits')  # an unreadable answer: the read fails
        else:
            stamp = self.ledger._clock_ms()
            fixture['rateLimits']['primary'] = {'usedPercent': used, 'windowDurationMins': 10080,
                                                'resetsAt': (reset or stamp + 7 * DAY) // 1000}
            fixture['rateLimits']['secondary'] = {'usedPercent': 5, 'windowDurationMins': 300,
                                                  'resetsAt': (stamp + 3_600_000 * 2) // 1000}
        path.write_text(json.dumps(fixture))

    @requirements('USAGE-RECHECK-2', 'USAGE-RECHECK-3', 'USAGE-RECHECK-4', 'USAGE-RECHECK-5')
    def test_the_route_rechecks_codex_once_with_its_non_spending_read(self):
        old = self.exhaust_codex()
        self.codex_answers(used=10)
        reads = self.codex_reads()
        route = self.route()
        recheck = route['availability'].get('recheck', {}).get('codex', {})
        self.assertEqual(reads + 1, self.codex_reads())
        self.assertEqual('cleared', recheck.get('outcome'), route['availability'])
        self.assertEqual(int, type(recheck['observed_at_ms']))
        self.assertGreater(recheck['observed_at_ms'], old)
        self.assertNotEqual('exhausted', route['availability']['providers']['codex']['state'])
        second = self.route()
        self.assertEqual(reads + 1, self.codex_reads())
        self.assertNotIn('recheck', second['availability'])

    @requirements('USAGE-RECHECK-5')
    def test_a_failed_codex_recheck_is_not_retried_within_the_interval(self):
        self.exhaust_codex()
        self.codex_answers(used=None)
        reads = self.codex_reads()
        first = self.route()
        recheck = first['availability'].get('recheck', {}).get('codex', {})
        self.assertEqual(reads + 1, self.codex_reads())
        self.assertTrue(str(recheck.get('outcome')).startswith('failed:'), first['availability'])
        self.assertIsNone(recheck['observed_at_ms'])
        self.assertEqual('exhausted', first['availability']['providers']['codex']['state'])
        second = self.route()
        self.assertEqual(reads + 1, self.codex_reads())  # the attempt, not a reading, started the interval
        self.assertNotIn('recheck', second['availability'])

    @requirements('USAGE-RECHECK-2', 'USAGE-RECHECK-5')
    def test_a_claude_probe_with_no_events_is_not_spent_again_after_the_cooldown(self):
        self.exhaust_claude(61 * MINUTE)
        self.frames('claude', [PROBE_INIT, result_frame('OK')])  # the probe runs but reports no usage
        before = self.launches()
        first = self.route()
        recheck = first['availability'].get('recheck', {}).get('claude', {})
        self.assertEqual(before + 1, self.launches())
        self.assertTrue(str(recheck.get('outcome')).startswith('failed:'), first['availability'])
        self.assertIsNone(recheck['observed_at_ms'])
        # Five minutes on: the probe cooldown and reuse no longer apply, the hourly re-check bound does.
        with mock.patch.object(account_billing_admission, 'PROBE_REUSE_MS', 0), \
                mock.patch.object(account_billing_admission, 'PROBE_COOLDOWN_MS', 0):
            second = self.route()
            self.assertEqual(before + 1, self.launches())
            self.assertNotIn('recheck', second['availability'])
            refused = self.cli('provider-observe', '--provider', 'claude', '--billing-probe', expected=2)
            self.assertEqual('provider_exhausted', refused['reason_code'])
            self.assertEqual(before + 1, self.launches())

    @requirements('USAGE-RECHECK-4', 'USAGE-RECHECK-5')
    def test_the_ledger_backed_route_also_rechecks(self):
        import cli
        self.exhaust_claude(61 * MINUTE)
        self.early_reset()
        before = self.launches()
        with mock.patch.object(cli, '_store_free_route', return_value=None):  # the route that opens the ledger
            route = self.route()
        availability = route['availability']
        self.assertEqual(before + 1, self.launches())
        self.assertEqual('cleared', availability.get('recheck', {}).get('claude', {}).get('outcome'), availability)
        self.assertNotEqual('exhausted', availability['providers']['claude']['state'], availability)

if __name__ == '__main__':
    unittest.main()
