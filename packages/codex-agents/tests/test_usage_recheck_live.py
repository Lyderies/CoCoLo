"""USAGE-RECHECK on the real wire: a far-reset exhaustion is re-read through the running account services.

The 0.8.55 install showed Claude exhausted until Sat 2026-11-28 01:00 after the owner had used an early
reset: routing skipped Claude, so no Claude child gave a newer reading, the billing probe refused an
exhausted account, and a measured reset was never cleared by a later reading. Here the real CLI talks to
the real in-process account and project services (service harness); only the Claude client is the
harness fake (its stream frames), no model is launched.
"""
import unittest

import tests.support  # noqa: F401 -- the source CLI import path
from tests import test_bridge
from tests.test_bridge import ClaudePrimaryCase, PROBE_INIT, result_frame
from codex_agents.providers import claude_rate_limit_records
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


if __name__ == '__main__':
    unittest.main()
