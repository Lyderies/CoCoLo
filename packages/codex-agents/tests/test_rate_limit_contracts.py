from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from codex_agents.learning_contracts import canonical_json
from codex_agents.rate_limit_contracts import (
    MAX_RATE_LIMIT_INFO_BYTES, MAX_TEXT, MAX_WINDOWS, RateLimitEventInput, bounded_rate_limit_info,
    checked_rate_limit_reply, merged_restriction,
)


class BoundedRateLimitInfoTests(unittest.TestCase):
    def test_size_never_drops_a_restriction(self):
        info = {'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed', 'padding': 'x' * 100_000,
                'overageDisabledReason': 'y' * (MAX_TEXT + 1)}
        facts = bounded_rate_limit_info(info)
        self.assertEqual({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed',
                          'overageDisabledReason': None}, facts)
        RateLimitEventInput('hrn_' + 'a' * 48, facts)  # Always fits the request.

    def test_the_largest_reduced_event_always_fits_the_request(self):
        text = 'x' * MAX_TEXT
        fields = ('status', 'rateLimitType', 'utilization', 'resetsAt', 'isUsingOverage', 'overageStatus',
                  'overageDisabledReason')
        windows = {('s%02d' % i).ljust(MAX_TEXT // 2, 'x'): {'status': text, 'utilization': text, 'resetsAt': text}
                   for i in range(MAX_WINDOWS * 3)}
        facts = bounded_rate_limit_info({**{name: text for name in fields}, 'unifiedWindows': windows,
                                         'utilization': float('inf')})
        self.assertLessEqual(len(canonical_json(facts)), MAX_RATE_LIMIT_INFO_BYTES)
        self.assertIsNone(facts['utilization'])  # Non-finite numbers become unknown.
        RateLimitEventInput('hrn_' + 'a' * 48, facts)

    def test_escaped_characters_cannot_grow_the_encoding(self):
        nul, quote = '\x00' * MAX_TEXT, '"' * MAX_TEXT
        windows = {('\x00' * 31) + str(i): {'status': 'allowed', 'resetsAt': nul} for i in range(MAX_WINDOWS)}
        windows.update({chr(0x41 + i) * 31: {'status': quote, 'resetsAt': '\u2028' * MAX_TEXT} for i in range(MAX_WINDOWS)})
        facts = bounded_rate_limit_info({'status': 'allowed', 'isUsingOverage': True, 'resetsAt': nul,
                                         'unifiedWindows': windows})
        self.assertLessEqual(len(canonical_json(facts)), MAX_RATE_LIMIT_INFO_BYTES)
        self.assertIs(True, facts['isUsingOverage'])
        self.assertIsNone(facts['resetsAt'])
        self.assertTrue(all(entry['status'] is None for entry in facts['unifiedWindows'].values()))

    def test_a_merged_event_is_at_least_as_restrictive_as_each(self):
        merged = merged_restriction([
            {'status': 'allowed', 'isUsingOverage': False, 'overageStatus': 'rejected', 'overageDisabledReason': 'x'},
            {'status': 'allowed_warning', 'isUsingOverage': True, 'overageStatus': 'rejected',
             'unifiedWindows': {'five_hour': {'status': 'rejected'}}},
        ])
        self.assertEqual({'status': 'rejected', 'isUsingOverage': True, 'overageStatus': 'rejected',
                          'overageDisabledReason': None}, merged)

    def test_unreadable_windows_and_integer_flags_merge_as_unknown(self):
        merged = merged_restriction([
            {'status': 'allowed', 'isUsingOverage': False, 'overageStatus': 'rejected'},
            {'status': 'allowed', 'isUsingOverage': 0, 'unifiedWindows': {'a': [], 'b': {'utilization': 7}}},
        ])
        self.assertIsNone(merged['status'])
        self.assertIsNone(merged['isUsingOverage'])

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
