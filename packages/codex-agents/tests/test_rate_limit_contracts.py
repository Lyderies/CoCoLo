from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from codex_agents.rate_limit_contracts import (
    MAX_WINDOWS, RateLimitEventInput, bounded_rate_limit_info, checked_rate_limit_reply,
)


class BoundedRateLimitInfoTests(unittest.TestCase):
    def test_size_never_drops_a_restriction(self):
        info = {'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed', 'padding': 'x' * 100_000,
                'overageDisabledReason': 'y' * 1_000}
        facts = bounded_rate_limit_info(info)
        self.assertEqual({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed',
                          'overageDisabledReason': None}, facts)
        RateLimitEventInput('hrn_' + 'a' * 48, facts)  # Always fits the request.

    def test_dropped_windows_never_make_the_event_less_restrictive(self):
        windows = {f'w{i:02}': {'status': 'allowed', 'utilization': 0.1} for i in range(MAX_WINDOWS)}
        windows['zz-rejected'] = {'status': 'rejected', 'utilization': 1.0, 'extra': 'x' * 10_000}
        windows['zzz-allowed'] = {'status': 'allowed'}
        facts = bounded_rate_limit_info({'status': 'allowed', 'unifiedWindows': windows})
        self.assertEqual('rejected', facts['status'])
        self.assertEqual(MAX_WINDOWS, len(facts['unifiedWindows']))
        unknown = bounded_rate_limit_info({'status': 'allowed', 'unifiedWindows': {
            **{f'w{i:02}': {'status': 'allowed'} for i in range(MAX_WINDOWS)}, 'zz': {'status': 'allowed_warning'}}})
        self.assertIsNone(unknown['status'])  # Unknown, so the account refuses it as a lost reading.

    def test_reply_shape_is_closed(self):
        self.assertEqual({'kept': 2, 'stop': 'paid_usage_observed'},
                         checked_rate_limit_reply({'kept': 2, 'stop': 'paid_usage_observed'}))
        for bad in ({'kept': 0, 'stop': 'provider_rejected'}, {'kept': 5, 'stop': None}, {'kept': 1}):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                checked_rate_limit_reply(bad)


if __name__ == '__main__':
    unittest.main()
