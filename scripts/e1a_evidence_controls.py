#!/usr/bin/env python3
"""Run only E1a evidence tests with reversible in-memory wiring mutations.

The optional direct-signatures adapter validates test wiring when the platform
signature helper cannot run. It really verifies signatures using ssh-keygen,
but bypasses helper confinement/process-tree checks: it is supplementary evidence,
never a replacement for an unmodified scenario run. No runtime files are edited.
"""
import argparse
import contextlib
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', default='green', choices=(
        'green', 'claim-completion', 'claim-lane', 'recapture', 'initial-open',
        'open-completion', 'open-provenance', 'run-provenance', 'publication',
        'restoration', 'account-entry', 'collector-frame', 'counts'))
    parser.add_argument('--direct-signatures', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'packages/codex-agents'))
    from tests import test_e1a_evidence as evidence
    from codex_agents import account_service, ledger_waits, signature_execution
    from codex_agents.learning_engine import OwnerGrantAuthority
    from codex_agents.signature_input_wire import encode_signature_inputs
    from codex_agents.signature_payloads import decode_signature_payloads

    def direct_signatures(inputs, limits, deadline_ns, cancelled, poll_ns, cleanup_ns):
        # Keep the actual fixed pool, captures, preparation and fresh SQL checks.
        # Replace only the confinement/birth-check adapter. All reported roots
        # are real children, waited for by their actual subprocess handles.
        wire = encode_signature_inputs(inputs, os.urandom(16), limits)
        prepare = ('import sys; sys.path.insert(0, sys.argv[1]); '
                   'from codex_agents.signature_input_wire import SignatureWireLimits; '
                   'from codex_agents.signature_payloads import prepare_signature_payloads; '
                   'limits=SignatureWireLimits(int(sys.argv[2]),int(sys.argv[3])); '
                   'sys.stdout.buffer.write(prepare_signature_payloads(sys.stdin.buffer.read(),limits))')
        response = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', prepare,
            str(signature_execution._HELPER_ROOT), str(limits.max_rows), str(limits.max_field_bytes)],
            input=wire, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=5).stdout
        batch = decode_signature_payloads(response, wire, limits)
        indices = []
        with tempfile.TemporaryDirectory(prefix='e1a-public-signatures-') as folder:
            signers = Path(folder) / 'allowed-signers'
            signers.write_bytes(inputs.signer or b'')
            signers.chmod(0o600)
            verifier = OwnerGrantAuthority(signers, signer_line=inputs.signer, raise_verifier_failures=True)
            for index, payload in batch.payloads:
                if cancelled.is_set() or time.monotonic_ns() >= deadline_ns:
                    raise AssertionError('supplementary verifier exceeded its original budget')
                if verifier._verify_payload(payload, inputs.rows[index][5]):
                    indices.append(index)
        return signature_execution.SignatureExecutionResult(
            'signature_execution_completed', tuple(indices), time.monotonic_ns(), 1 + len(batch.payloads), False)

    tests = {
        'claim-completion': 'ClaimContentionTests.test_claim_completion_busy_then_retry',
        'claim-lane': 'ClaimContentionTests.test_claim_completion_busy_then_retry',
        'recapture': 'ClaimContentionTests.test_real_stale_recapture_busy_then_retry',
        'initial-open': 'OpenContentionTests.test_initial_open_real_host_authorization_busy_then_retry',
        'open-completion': 'OpenContentionTests.test_open_completion_busy_then_retry',
        'open-provenance': 'ProvenanceContentionTests.test_open_provenance_real_contention_then_retry',
        'run-provenance': 'ProvenanceContentionTests.test_run_provenance_real_contention_then_retry',
        'publication': 'OpenContentionTests.test_exhausted_deferred_publication_restores_next_request_and_owner_pump',
        'restoration': 'OpenContentionTests.test_exhausted_deferred_publication_restores_next_request_and_owner_pump',
        'account-entry': 'RequestDeadlineEvidenceTests.test_real_handle_and_begin_share_successive_waits_and_refresh',
        'collector-frame': 'RequestDeadlineEvidenceTests.test_slow_collector_receipt_and_next_frame_get_fresh_budgets',
        'counts': 'ConcurrentRefusalEvidenceTests.test_event_coordinated_exact_counts_for_all_acquisition_paths_during_health',
    }
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromModule(evidence) if args.control == 'green' else
             loader.loadTestsFromName('tests.test_e1a_evidence.' + tests[args.control]))
    with contextlib.ExitStack() as patches:
        if args.direct_signatures:
            patches.enter_context(mock.patch.object(signature_execution, '_execute_signature_job', direct_signatures))
        if args.control in {'claim-completion', 'recapture'}:
            patches.enter_context(mock.patch.object(evidence.account_claim_jobs, 'completion_reason',
                side_effect=lambda error, kind: 'open_failed' if kind == 'open' else 'claim_unavailable'))
        elif args.control == 'claim-lane':
            patches.enter_context(mock.patch.object(evidence.account_claim_jobs, 'completion_fault', return_value=True))
        elif args.control == 'open-completion':
            from codex_agents.account_open_contracts import OpenError
            finish_open = evidence.account_original_open.finish_open_from_signature_job

            def drop_open_busy(*args, **kwargs):
                try:
                    return finish_open(*args, **kwargs)
                except OpenError as error:
                    if error.reason_code == 'open_account_busy':
                        raise OpenError('open_failed') from None
                    raise

            patches.enter_context(mock.patch.object(evidence.account_original_open,
                'finish_open_from_signature_job', drop_open_busy))
        elif args.control == 'initial-open':
            original = evidence.AccountServiceHost._authorize

            def swallow_busy(self, *args, **kwargs):
                try:
                    return original(self, *args, **kwargs)
                except evidence.SQLReadError:
                    return None

            patches.enter_context(mock.patch.object(evidence.AccountServiceHost, '_authorize', swallow_busy))
        elif args.control in {'open-provenance', 'run-provenance'}:
            module = evidence.account_host_open if args.control == 'open-provenance' else evidence.account_run_provenance
            patches.enter_context(mock.patch.object(module, 'passes_busy', return_value=False))
        elif args.control == 'publication':
            patches.enter_context(mock.patch.object(account_service.AccountService, 'take_claim_reply',
                lambda self, ticket, **kwargs: self._take_claim_reply(ticket, **kwargs)))
        elif args.control == 'restoration':
            @contextlib.contextmanager
            def leak_deadline():
                ledger_waits._request.deadline = ledger_waits.time.monotonic() + .8
                yield
            patches.enter_context(mock.patch.object(ledger_waits, 'request', leak_deadline))
        elif args.control == 'account-entry':
            patches.enter_context(mock.patch.object(account_service.AccountService, '_handle',
                lambda self, raw, token, *, deferred, ingress=None:
                    self._handle_request(raw, token, deferred=deferred, ingress=ingress)))
        elif args.control == 'collector-frame':
            patches.enter_context(mock.patch.object(ledger_waits, 'request', contextlib.nullcontext))
        elif args.control == 'counts':
            original = ledger_waits.LockWaits.refused
            patches.enter_context(mock.patch.object(ledger_waits.LockWaits, 'refused',
                lambda self, label: None if label == 'read_call' else original(self, label)))
        started = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        seconds = time.monotonic() - started
    # Never leak the intentionally corrupted thread-local state past the harness.
    ledger_waits._request.deadline = None
    expected_red = args.control != 'green'
    failure_text = '\n'.join(detail for _, detail in result.failures)
    expected_assertion = {
        'claim-completion': 'test.assertEqual(reason, raised.exception.reason_code)',
        'claim-lane': "test.assertIsNone(health['last_failure'])",
        'recapture': 'test.assertEqual(reason, raised.exception.reason_code)',
        'initial-open': 'test.assertEqual(reason, raised.exception.reason_code)',
        'open-completion': 'test.assertEqual(reason, raised.exception.reason_code)',
        'open-provenance': 'test.assertEqual(reason, raised.exception.reason_code)',
        'run-provenance': 'test.assertEqual(reason, raised.exception.reason_code)',
        'publication': 'self.assertEqual(0, recording.timeouts[-1])',
        'restoration': "self.assertIsNone(getattr(ledger_waits._request, 'deadline', None))",
        'account-entry': 'self.assertAlmostEqual(.3, timeouts[1])',
        'collector-frame': 'self.assertAlmostEqual(expected, actual)',
        'counts': 'self.assertEqual([], errors)',
    }.get(args.control, '')
    observed_red = (len(result.failures) == 1 and not result.errors
                    and expected_assertion in failure_text
                    and not any(message in failure_text for message in (
                        'did not finish', 'did not drain', 'not less than 10',
                        'holder did not', 'health blocked', 'BrokenBarrierError')))
    print(f'control={args.control} direct_signatures={args.direct_signatures} '
          f'tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} '
          f'intended_assertion={observed_red if expected_red else "n/a"} seconds={seconds:.3f}', flush=True)
    return int(not (observed_red if expected_red else result.wasSuccessful()))


if __name__ == '__main__':
    raise SystemExit(main())
