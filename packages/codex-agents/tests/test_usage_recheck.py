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


def unnamed(used, reset):
    """A window whose identity is unknown: no scope and no duration."""
    return {'scope': None, 'window_minutes': None, 'used_percent': used, 'resets_at': reset}


class RecheckReviewFixTests(unittest.TestCase):
    """Review round 1 (NOT-READY) findings HIGH 1, HIGH 2, HIGH 4 and its MED, on the pure classifier."""

    @requirements('USAGE-RECHECK-3', 'RT-RX-5', why_not_scenario=PURE)
    def test_windows_of_unknown_identity_never_clear_each_other(self):
        far = NOW + 3 * DAY
        for provider, restrictive, later in (
                ('codex', codex(NOW - 2 * 3_600_000, unnamed(95, far)), codex(NOW - MINUTE, unnamed(10, NOW + 7 * DAY))),
                ('claude', claude(NOW - 2 * 3_600_000, 'rejected', unnamed(100, far)),
                 claude(NOW - MINUTE, 'allowed', unnamed(2, NOW + 7 * DAY)))):
            with self.subTest(provider=provider):
                self.assertEqual('exhausted', state(provider, restrictive, later)['state'])

    @requirements('USAGE-RECHECK-3', 'RT-RX-5', why_not_scenario=PURE)
    def test_an_inherited_merged_reset_is_not_proof_of_an_early_reset(self):
        from codex_agents.provider_restrictions import merged_restriction
        from codex_agents.providers import headroom_record

        def run_reading(at, used, reset):
            return headroom_record(provider='claude', account_fingerprint=ACCOUNT, status='allowed', run_id=None,
                                   phase=None, observed_at_ms=at, windows=[window('seven_day', used, reset)])
        far, new = NOW + 3 * DAY, NOW + 7 * DAY
        rejected = claude(NOW - 2 * 3_600_000, 'rejected', window('seven_day', 100, far))
        # A low reading that reports the OLD reset, merged with an unmeasured event that names another reset:
        # the merge gives the low component the later reset, which the provider never reported for it.
        low = run_reading(NOW - 2 * MINUTE, 2, far)
        unmeasured = run_reading(NOW - MINUTE, None, new)
        merged = merged_restriction([low, unmeasured], observed_at_ms=NOW - MINUTE)
        self.assertEqual('exhausted', state('claude', rejected, merged)['state'])

    @requirements('USAGE-RECHECK-3', why_not_scenario=PURE)
    def test_a_proven_early_reset_stays_cleared_as_the_old_reset_nears(self):
        far = NOW + 3 * DAY
        rejected = claude(NOW - 2 * 3_600_000, 'rejected', window('seven_day', 100, far))
        fresh = claude(NOW - MINUTE, 'allowed', window('seven_day', 2, NOW + 7 * DAY))
        for later in (NOW, far - 30 * MINUTE, far - 1):
            with self.subTest(now=later):
                self.assertEqual('ample', state('claude', rejected, fresh, now=later)['state'])

    @requirements('USAGE-RECHECK-1', 'USAGE-RECHECK-5', why_not_scenario=PURE)
    def test_a_recheck_attempt_without_a_reading_starts_the_interval(self):
        far = NOW + 3 * DAY
        old = claude(NOW - 61 * MINUTE, 'rejected', window('seven_day', 100, far))
        result = metadata.classify_provider_state('claude', ACCOUNT, pairs(old), [], NOW,
                                                  recheck_attempts_ms=[NOW - 5 * MINUTE, NOW + DAY])
        self.assertEqual(NOW + 55 * MINUTE, result['recheck_due_at_ms'])
        self.assertEqual(NOW - 61 * MINUTE, result['last_reading_at_ms'])  # an attempt is not a reading
        self.assertFalse(metadata.usage_recheck_due('claude', ACCOUNT, pairs(old), [], NOW,
                                                    recheck_attempts_ms=[NOW - 5 * MINUTE]))
        self.assertTrue(metadata.usage_recheck_due('claude', ACCOUNT, pairs(old), [], NOW,
                                                   recheck_attempts_ms=[NOW - 61 * MINUTE]))


class RecheckAuditTests(unittest.TestCase):
    """Review round 1 MED: the audit reports the account's actual new reading, or null; never 'cleared' because the
    identity expired, never the snapshot's own time when no reading arrived."""

    @requirements('USAGE-RECHECK-5', why_not_scenario='The CLI re-check step with its account-service clients '
                  'replaced; no service, ledger or provider is reached.')
    def test_the_audit_names_the_actual_reading_or_null(self):
        from unittest import mock
        import cli
        before = metadata.ProviderStateSnapshot('p', NOW, {
            'codex': {'state': 'ample', 'recheck_due_at_ms': None, 'account_fingerprint': ACCOUNT},
            'claude': {'state': 'exhausted', 'recheck_due_at_ms': NOW - MINUTE, 'account_fingerprint': ACCOUNT,
                       'last_reading_at_ms': NOW - 61 * MINUTE}})

        def after(claude_state):
            return metadata.ProviderStateSnapshot('p', NOW + 5, {
                'codex': before.providers['codex'], 'claude': claude_state})
        expired = {'state': 'unknown', 'recheck_due_at_ms': None, 'account_fingerprint': None,
                   'last_reading_at_ms': None}
        unchanged = {**before.providers['claude'], 'recheck_due_at_ms': NOW + 5 * MINUTE}
        read = {**before.providers['claude'], 'recheck_due_at_ms': NOW + 2 + 3_600_000, 'last_reading_at_ms': NOW + 2}
        for fresh, expected in ((expired, {'outcome': 'failed:no_reading', 'observed_at_ms': None}),
                                (unchanged, {'outcome': 'failed:no_reading', 'observed_at_ms': None}),
                                (read, {'outcome': 'still_exhausted', 'observed_at_ms': NOW + 2}),
                                ({**read, 'state': 'ample', 'recheck_due_at_ms': None},
                                 {'outcome': 'cleared', 'observed_at_ms': NOW + 2})):
            with self.subTest(fresh=fresh), \
                    mock.patch.object(cli, 'provider_state_snapshot_client', return_value=after(fresh)), \
                    mock.patch.object(cli, 'billing_probe_client', return_value={'probe': 'launched'}), \
                    mock.patch.object(cli, '_reserve_usage_recheck', return_value=True, create=True), \
                    mock.patch.object(cli, '_host_client', return_value='claude-host-v1'):
                self.assertEqual({'claude': expected}, cli._usage_recheck(None, None, before).recheck)


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


class ReservationFileSafetyTests(unittest.TestCase):
    """Confirming check HIGH: the reservation files never follow links, trust foreign owners, hang or grow unbounded.
    Any refusal means no re-check this route (no spend, route as today)."""
    WHY = 'The CLI reservation step on a temporary state root; no service, ledger or provider is reached.'

    def setUp(self):
        import json, os, tempfile
        from pathlib import Path
        import cli
        self.cli, self.os, self.json = cli, os, json
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o700)
        self.root.chmod(0o700)
        self.dir = cli._recheck_reservation_path(self.state, 'claude', ACCOUNT).parent
        self.record = cli._recheck_reservation_path(self.state, 'claude', ACCOUNT)
        self.target = self.root / 'victim'
        self.target.write_text('keep')

    def reserve(self):
        return self.cli._reserve_usage_recheck(self.state, 'claude', ACCOUNT, NOW)

    def private_dir(self):
        self.dir.mkdir(mode=0o700)
        self.dir.chmod(0o700)

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_fresh_reservation_is_taken_once_in_a_private_directory(self):
        self.assertTrue(self.reserve())
        self.assertFalse(self.reserve())
        self.assertEqual(0o700, self.dir.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.record.stat().st_mode & 0o777)

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_planted_link_at_the_temporary_name_never_truncates_its_target(self):
        self.private_dir()
        self.record.with_name(self.record.name + '.tmp').symlink_to(self.target)
        self.reserve()
        self.assertEqual('keep', self.target.read_text())

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_link_at_the_record_path_is_refused(self):
        self.private_dir()
        self.record.symlink_to(self.target)
        self.assertFalse(self.reserve())
        self.assertEqual('keep', self.target.read_text())

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_link_as_the_directory_is_refused(self):
        other = self.root / 'elsewhere'
        other.mkdir(mode=0o700)
        self.dir.symlink_to(other)
        self.assertFalse(self.reserve())
        self.assertEqual([], list(other.iterdir()))

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_fifo_at_the_record_path_refuses_without_hanging(self):
        import threading
        self.private_dir()
        self.os.mkfifo(self.record)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.reserve()), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([False], result)

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_foreign_owner_or_loose_mode_is_refused(self):
        from unittest import mock
        self.assertTrue(self.reserve())
        later = NOW + 10 * 24 * 3_600_000
        for loose in (0o620, 0o602):
            with self.subTest(mode=oct(loose)):
                self.record.chmod(loose)
                self.assertFalse(self.cli._reserve_usage_recheck(self.state, 'claude', ACCOUNT, later))
        self.record.chmod(0o600)
        with mock.patch.object(self.os, 'geteuid', return_value=self.os.geteuid() + 1):
            self.assertFalse(self.cli._reserve_usage_recheck(self.state, 'claude', ACCOUNT, later))
        self.record.chmod(0o600)
        self.dir.chmod(0o770)
        self.assertFalse(self.cli._reserve_usage_recheck(self.state, 'claude', ACCOUNT, later))

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_an_oversized_record_is_refused(self):
        self.private_dir()
        self.record.write_text('{"reserved_at_ms": 1, "pad": "' + 'x' * 100_000 + '"}')
        self.record.chmod(0o600)
        self.assertFalse(self.reserve())

    @requirements('USAGE-RECHECK-5', why_not_scenario=WHY)
    def test_a_held_lock_gives_a_bounded_wait_then_no_recheck(self):
        import fcntl, time
        self.private_dir()
        lock = self.os.open(self.dir / '.lock', self.os.O_RDWR | self.os.O_CREAT, 0o600)
        self.addCleanup(self.os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        self.assertFalse(self.reserve())
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(self.record.exists())
