"""USAGE-RECHECK: an exhausted provider with a far reset is re-read on a bounded interval (pure parts)."""
import unittest

import tests.support  # noqa: F401 -- the source import path
from codex_agents import admission_metadata as metadata, refusal_steps
from codex_agents.learning_contracts import content_digest
from test_framework import requirements

NOW = 10_000_000_000
ACCOUNT = 'a' * 64
MINUTE = 60_000
DAY = 86_400_000
PURE = 'Pure classifier and refusal-text checks have no service or persistence effects.'


def window(scope, used, reset):
    minutes = {'five_hour': 300, 'seven_day': 10_080, None: 300}[scope]
    return {'scope': scope, 'window_minutes': minutes, 'used_percent': used, 'resets_at': reset}


def claude(at, status, *windows):
    return {'provider': 'claude', 'account_fingerprint': ACCOUNT, 'observed_at_ms': at, 'status': status,
            'windows': list(windows)}


def codex(at, *windows):
    return {'provider': 'codex', 'account_fingerprint': ACCOUNT, 'observed_at_ms': at, 'status': None,
            'windows': list(windows)}


def pairs(*records):
    return [(record, content_digest(record)) for record in records]


def state(provider, *records, now=NOW):
    return metadata.classify_provider_state(provider, ACCOUNT, pairs(*records), [], now)


class RecheckDueTests(unittest.TestCase):
    @requirements('USAGE-RECHECK-1', why_not_scenario=PURE)
    def test_a_far_reset_exhaustion_is_due_for_a_recheck_after_the_interval(self):
        self.assertEqual(3_600_000, metadata.USAGE_RECHECK_INTERVAL_MS)
        self.assertEqual(3_600_000, metadata.USAGE_RECHECK_FAR_RESET_MS)
        far = NOW + 3 * DAY
        old = claude(NOW - 61 * MINUTE, 'rejected', window('seven_day', 100, far))
        result = state('claude', old)
        self.assertEqual('exhausted', result['state'])
        self.assertEqual(NOW - 61 * MINUTE + metadata.USAGE_RECHECK_INTERVAL_MS, result['recheck_due_at_ms'])
        self.assertLessEqual(result['recheck_due_at_ms'], NOW)
        recent = claude(NOW - 10 * MINUTE, 'rejected', window('seven_day', 100, far))
        self.assertEqual(NOW + 50 * MINUTE, state('claude', recent)['recheck_due_at_ms'])
        # The newest reading of the account sets the clock, whichever window it measured.
        self.assertEqual(NOW + 50 * MINUTE, state('claude', old, claude(NOW - 10 * MINUTE, 'rejected',
            window('five_hour', 20, NOW + 2 * 3_600_000)))['recheck_due_at_ms'])
        # A near reset is waited for; a resetless restriction (fallback expiry) is re-read like a far one.
        near = claude(NOW - 61 * MINUTE, 'rejected', window('seven_day', 100, NOW + 30 * MINUTE))
        self.assertIsNone(state('claude', near)['recheck_due_at_ms'])
        resetless = codex(NOW - 61 * MINUTE, window(None, 95, None))
        self.assertEqual(NOW - MINUTE, state('codex', resetless)['recheck_due_at_ms'])
        for used in (10, 80):
            self.assertIsNone(state('codex', codex(NOW - 61 * MINUTE, window(None, used, far)))['recheck_due_at_ms'])
        self.assertIsNone(state('codex')['recheck_due_at_ms'])


class EarlyResetTests(unittest.TestCase):
    @requirements('USAGE-RECHECK-3', why_not_scenario=PURE)
    def test_a_later_reading_of_the_same_window_with_a_new_reset_clears_it(self):
        far = NOW + 3 * DAY
        rejected = claude(NOW - 2 * 3_600_000, 'rejected', window('seven_day', 100, far))
        fresh = claude(NOW - MINUTE, 'allowed', window('seven_day', 2, NOW + 7 * DAY))
        self.assertEqual('ample', state('claude', rejected, fresh)['state'])
        self.assertEqual('ample', state('claude', fresh, rejected)['state'])  # ledger order never matters
        exhausted = codex(NOW - 2 * 3_600_000, window(None, 95, far))
        self.assertEqual('ample', state('codex', exhausted, codex(NOW - MINUTE, window(None, 10, NOW + 7 * DAY)))['state'])

    @requirements('USAGE-RECHECK-3', 'RT-RX-5', why_not_scenario=PURE)
    def test_other_later_readings_still_do_not_clear_a_measured_reset(self):
        far = NOW + 3 * DAY
        rejected = claude(NOW - 2 * 3_600_000, 'rejected', window('seven_day', 100, far))
        for later in (claude(NOW - MINUTE, 'allowed', window('five_hour', 2, NOW + 3_600_000)),  # another window
                      claude(NOW - MINUTE, 'allowed', window('seven_day', 2, None)),             # no reset time
                      claude(NOW - MINUTE, 'allowed', window('seven_day', 2, far)),              # same reset
                      claude(NOW - 2 * 3_600_000, 'allowed', window('seven_day', 2, NOW + 7 * DAY)),  # not later
                      claude(NOW + MINUTE, 'allowed', window('seven_day', 2, NOW + 7 * DAY)),    # after now
                      {**claude(NOW - MINUTE, 'allowed', window('seven_day', 2, NOW + 7 * DAY)),
                       'account_fingerprint': 'b' * 64}):                                          # another account
            with self.subTest(later=later):
                self.assertEqual('exhausted', state('claude', rejected, later)['state'])


class HostClientUnknownStepTests(unittest.TestCase):
    @requirements('UX-HCU-1', why_not_scenario=PURE)
    def test_host_client_unknown_says_to_run_it_from_the_host_session(self):
        step = refusal_steps.next_step('host_client_unknown')
        self.assertNotEqual(refusal_steps.DEFAULT, step)
        self.assertNotIn('learning-status', step)
        self.assertIn('host', step)
        self.assertIn('session', step)


if __name__ == '__main__':
    unittest.main()
