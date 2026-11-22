from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.support import ORCHESTRATION_ROOT
from codex_agents import admission_metadata, providers
from codex_agents.learning_contracts import validate_provider_headroom


FIXTURE = Path(__file__).resolve().parent / 'fixtures/learning/admission-metadata-vectors.json'


def calculate(module, case):
    arguments = json.loads(json.dumps(case['arguments']))
    if case['function'] == 'evaluate_admission':
        arguments['prior_claude_admissions'] = set(arguments['prior_claude_admissions'])
        if arguments.get('cited_digests') is not None:
            arguments['cited_digests'] = set(arguments['cited_digests'])
    functions = {name: getattr(module, name) for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms')}
    return json.loads(json.dumps(functions[case['function']](**arguments)))


class AdmissionMetadataTests(unittest.TestCase):
    def test_mixed_future_reset_evidence_keeps_the_earliest_shared_cap_window(self):
        cases = {case['name']: case for case in json.loads(FIXTURE.read_text())['cases']}
        hour = 3_600_000
        for provider in ('codex', 'claude'):
            case = json.loads(json.dumps(cases[provider + '-admitted']))
            arguments = case['arguments']
            now = arguments['now_ms']
            records = []
            for observed, reset, digest in ((now - 1, now + 4 * hour, '7' * 64),
                                             (now + 1, now + hour, '8' * 64)):
                record = validate_provider_headroom(providers._record('provider_headroom', {
                    'provider': provider, 'account_fingerprint': 'a' * 64,
                    'observed_at_ms': observed, 'status': 'allowed' if provider == 'claude' else None,
                    'run_id': None, 'phase': None,
                    'windows': [{'scope': 'five_hour' if provider == 'claude' else None,
                                 'window_minutes': 300, 'used_percent': 0, 'resets_at': reset}],
                }))
                records.append([record, digest])
            arguments['admission_policy']['max_admissions_per_window'] = 1
            arguments['prior_run_admissions'] = [['a' * 64, now - 2 * hour]]
            arguments['window_headroom'] = records
            with self.subTest(provider=provider, check='window'):
                self.assertEqual((now - 4 * hour, '8' * 64), admission_metadata.window_start_ms(
                    provider, 'a' * 64, records, now))
            with self.subTest(provider=provider, check='denial'):
                self.assertEqual('window_run_cap_reached', calculate(admission_metadata, case)['reason_code'])
            # An expired or more-than-five-hours-away reset is ineligible,
            # regardless of its future observation time. It cannot pin a cap.
            for reset in (now, now + 5 * hour + 1):
                records[1][0]['windows'][0]['resets_at'] = reset
                with self.subTest(provider=provider, ineligible_reset=reset):
                    self.assertEqual('admitted', calculate(admission_metadata, case)['reason_code'])

    def test_future_resetless_restrictions_survive_clock_rollback_for_admission_and_launch(self):
        cases = {case['name']: case for case in json.loads(FIXTURE.read_text())['cases']}
        for provider in ('codex', 'claude'):
            for entrypoint, suffix in (('evaluate_admission', 'admitted'), ('evaluate_launch', 'launch')):
                for mode in ('future-resetless', 'expired-resetless', 'future-known-reset'):
                    with self.subTest(provider=provider, entrypoint=entrypoint, mode=mode):
                        case = json.loads(json.dumps(cases[provider + '-' + suffix]))
                        arguments = case['arguments']
                        now = arguments['now_ms']
                        observed = now - admission_metadata.EVIDENCE_HORIZON_MS - 1 if mode == 'expired-resetless' else now + 1
                        record = validate_provider_headroom(providers._record('provider_headroom', {
                            'provider': provider, 'account_fingerprint': 'a' * 64, 'observed_at_ms': observed,
                            'status': 'rejected' if provider == 'claude' else None, 'run_id': None, 'phase': None,
                            'windows': [{'scope': 'five_hour' if provider == 'claude' else None,
                                         'window_minutes': 300, 'used_percent': 95 if provider == 'codex' else None,
                                         'resets_at': now + 10000 if mode == 'future-known-reset' else None}],
                        }))
                        arguments['headroom'].append([record, '9' * 64])
                        actual = calculate(admission_metadata, case)
                        reason = actual['reason_code'] if entrypoint == 'evaluate_admission' else actual
                        expected = ('admitted' if entrypoint == 'evaluate_admission' else None) if mode == 'expired-resetless' else 'provider_exhausted'
                        self.assertEqual(expected, reason)

    def test_empty_unified_windows_keep_rejection_or_warning_until_fallback_expiry(self):
        cases = {case['name']: case for case in json.loads(FIXTURE.read_text())['cases']}
        horizon = admission_metadata.EVIDENCE_HORIZON_MS
        for status in ('rejected', 'allowed_warning'):
            for delta in (0, 1, -horizon, -horizon - 1):
                for entrypoint, suffix in (('evaluate_admission', 'admitted'), ('evaluate_launch', 'launch')):
                    case = json.loads(json.dumps(cases['claude-' + suffix]))
                    arguments = case['arguments']
                    now = arguments['now_ms']
                    headroom, billing = providers.claude_rate_limit_records({
                        'status': status, 'unifiedWindows': {}, 'isUsingOverage': False,
                        'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled',
                    }, account_fingerprint='a' * 64, observed_at_ms=now + delta)
                    validate_provider_headroom(headroom)
                    self.assertEqual([], headroom['windows'])
                    arguments['headroom'].append([headroom, '9' * 64])
                    arguments['billing'].append([billing, '8' * 64])
                    active = delta >= -horizon
                    rating = ('exhausted' if status == 'rejected' else 'constrained') if active else 'ample'
                    with self.subTest(status=status, delta=delta, entrypoint=entrypoint, check='rating'):
                        self.assertEqual(rating, admission_metadata.headroom_state(
                            'claude', 'a' * 64, arguments['headroom'], arguments['billing'], now)[0])
                    with self.subTest(status=status, delta=delta, entrypoint=entrypoint, check='decision'):
                        actual = calculate(admission_metadata, case)
                        reason = actual['reason_code'] if entrypoint == 'evaluate_admission' else actual
                        expected = 'provider_exhausted' if active and status == 'rejected' else ('admitted' if entrypoint == 'evaluate_admission' else None)
                        self.assertEqual(expected, reason)
        # An explicit reset that has elapsed must not fall back to a day-long
        # restriction merely because the filtered live-window list is empty.
        record = providers.claude_rate_limit_records({
            'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 9999999,
            'isUsingOverage': False, 'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled',
        }, account_fingerprint='a' * 64, observed_at_ms=10_000_000_001)[0]
        self.assertIsNone(admission_metadata._headroom_state('claude', record, 10_000_000_000))

    def test_legacy_calculations_and_import_entrypoints_are_preserved(self):
        for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms'):
            self.assertIs(getattr(admission_metadata, name), getattr(providers, name))
        vectors = json.loads(FIXTURE.read_text())
        self.assertEqual(35, len(vectors['cases']))
        for case in vectors['cases']:
            with self.subTest(name=case['name']):
                self.assertEqual(case['expected'], calculate(admission_metadata, case))

    def test_cold_import_excludes_provider_persistence_and_all_calculation_io(self):
        program = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
with open(sys.argv[2]) as source:
    vectors = json.load(source)
allowed = {'codex_agents', 'codex_agents.contracts', 'codex_agents.learning_contracts',
           'codex_agents.admission_metadata'}
class ImportBoundary:
    def find_spec(self, name, path=None, target=None):
        if name.partition('.')[0] in {'sqlite3', '_sqlite3'} or (name.startswith('codex_agents.') and name not in allowed):
            raise AssertionError('metadata dependency boundary crossed')
sys.meta_path.insert(0, ImportBoundary())
from codex_agents import admission_metadata as metadata
loaded = {name for name in sys.modules if name == 'codex_agents' or name.startswith('codex_agents.')}
assert loaded <= allowed
assert 'sqlite3' not in sys.modules and '_sqlite3' not in sys.modules
def audit(event, arguments):
    if event == 'open' or event.startswith(('socket.', 'subprocess.', 'sqlite3.')) or event in {
        'os.system', 'os.fork', 'os.posix_spawn', 'os.chdir', 'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir'
    }:
        raise AssertionError('metadata calculation attempted external work')
sys.addaudithook(audit)
functions = {name: getattr(metadata, name) for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms')}
for case in vectors['cases']:
    arguments = case['arguments']
    if case['function'] == 'evaluate_admission':
        arguments['prior_claude_admissions'] = set(arguments['prior_claude_admissions'])
        if arguments.get('cited_digests') is not None:
            arguments['cited_digests'] = set(arguments['cited_digests'])
    actual = json.loads(json.dumps(functions[case['function']](**arguments)))
    assert actual == case['expected'], case['name']
print(json.dumps({'cases': len(vectors['cases']), 'application_modules': sorted(loaded)}))
'''
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-S', '-c', program, str(ORCHESTRATION_ROOT), str(FIXTURE)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
            env={'PATH': os.defpath, 'LC_ALL': 'C'}, close_fds=True,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        report = json.loads(result.stdout)
        self.assertEqual(35, report['cases'])
        self.assertEqual({'codex_agents', 'codex_agents.contracts', 'codex_agents.learning_contracts',
                          'codex_agents.admission_metadata'}, set(report['application_modules']))


class NewerRunRestrictsTests(unittest.TestCase):
    """LOW-1 and MED-1 (probe reuse fix, round 1): the shared predicate, directly."""

    FINGERPRINT = 'a' * 64
    START = 10_000_000_000
    NOW = START + 78_000
    CONSTRAINED = {'status': 'allowed_warning', 'isUsingOverage': False, 'overageStatus': 'rejected',
                   'overageDisabledReason': 'org_level_disabled',
                   'unifiedWindows': {'five_hour': {'utilization': 0.38, 'status': 'allowed'},
                                      'seven_day': {'utilization': 0.74, 'status': 'allowed_warning'}}}
    AMPLE = {'status': 'allowed', 'isUsingOverage': False, 'overageStatus': 'rejected',
             'overageDisabledReason': 'org_level_disabled'}

    def records(self, info, at, **options):
        return providers.claude_rate_limit_records(
            info, account_fingerprint=self.FINGERPRINT, observed_at_ms=at, **options)

    def restricts(self, own, newer, *, own_at=None, extra_headroom=()):
        """`own`: the probe's reading (a rate-limit info); `newer`: the child's, 30 s later."""
        own_head, own_bill = self.records(own, self.START, probe_id='a' * 64)
        run_head, run_bill = self.records(newer, self.START + 30_000, run_id='run-1')
        heads = [own_head, run_head, *extra_headroom]
        if own_at is not None:  # the probe's headroom stamped otherwise than its billing
            heads[0] = self.records(own, own_at, probe_id='b' * 64)[0]
        headroom = [(record, f'{number:064x}') for number, record in enumerate(heads)]
        billing = [(own_bill, '1' * 64), (run_bill, '2' * 64)]
        return admission_metadata.newer_run_restricts(
            own_bill, billing, headroom, self.FINGERPRINT, self.NOW, {})

    def test_a_run_more_restrictive_than_the_probe_s_own_reading_restricts(self):
        self.assertTrue(self.restricts(self.CONSTRAINED, {**self.CONSTRAINED, 'status': 'rejected'}))
        self.assertTrue(self.restricts(self.AMPLE, self.CONSTRAINED))

    def test_paid_usage_in_a_run_record_restricts_whatever_the_probe_read(self):
        paid = {**self.CONSTRAINED, 'isUsingOverage': True, 'overageStatus': 'allowed'}
        self.assertTrue(self.restricts(self.CONSTRAINED, paid))

    def test_a_run_as_constrained_as_the_probe_s_own_reading_does_not_restrict(self):
        self.assertFalse(self.restricts(self.CONSTRAINED, self.CONSTRAINED))

    def test_a_probe_with_no_associated_headroom_restricts_on_any_constrained_run(self):
        # Another probe owns the headroom, regardless of its timestamp.
        self.assertTrue(self.restricts(self.CONSTRAINED, self.CONSTRAINED, own_at=self.START + 30_000))

    def test_another_probe_s_equal_stamp_headroom_does_not_raise_an_ample_baseline(self):
        other = self.records(self.CONSTRAINED, self.START)[0]  # same stamp, other content than the ample own
        self.assertTrue(self.restricts(self.AMPLE, self.CONSTRAINED, extra_headroom=(other,)))

    def test_PC5_complementary_half_failed_probes_at_equal_stamps_restrict(self):
        p_bill = {**self.records(self.CONSTRAINED, self.START)[1], 'probe_id': 'a' * 64}
        q_head = {**self.records(self.CONSTRAINED, self.START)[0], 'probe_id': 'b' * 64}
        run = self.records(self.CONSTRAINED, self.START + 30_000, run_id='run-1')[0]
        self.assertTrue(admission_metadata.newer_run_restricts(
            p_bill, [(p_bill, '1'*64)], [(q_head, '2'*64), (run, '3'*64)], self.FINGERPRINT, self.NOW, {}))

    def test_PC5_legacy_null_id_is_strict(self):
        for explicit_null in (False, True):
            head, bill = self.records(self.CONSTRAINED, self.START)
            if explicit_null:
                head, bill = {**head, 'probe_id': None}, {**bill, 'probe_id': None}
            run = self.records(self.CONSTRAINED, self.START + 30_000, run_id='run-1')[0]
            self.assertTrue(admission_metadata.newer_run_restricts(
                bill, [(bill, '1'*64)], [(head, '2'*64), (run, '3'*64)], self.FINGERPRINT, self.NOW, {}))

    def test_PC5_probe_head_requires_one_id_match_and_ignores_stamps(self):
        head, bill = self.records(self.CONSTRAINED, self.START)
        head, bill = {**head, 'probe_id': 'a'*64}, {**bill, 'probe_id': 'a'*64}
        other = {**head, 'record_id': 'other', 'probe_id': 'b'*64, 'observed_at_ms': self.NOW}
        self.assertEqual(head, admission_metadata.probe_head([(head, ''), (other, '')], self.FINGERPRINT, bill))
        for heads in ([], [(other, '')], [(head, ''), ({**head, 'record_id': 'duplicate'}, '')]):
            self.assertIsNone(admission_metadata.probe_head(heads, self.FINGERPRINT, bill))
        shifted = {**head, 'observed_at_ms': self.START + 100}
        self.assertEqual(shifted, admission_metadata.probe_head([(shifted, '')], self.FINGERPRINT, bill))
