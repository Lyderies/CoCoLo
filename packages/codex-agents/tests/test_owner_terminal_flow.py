"""The owner's own terminal, end to end: what setup and attestation run, with no client's markers.

Setup's status check and the billing attestation are not run by a primary. They
run in the owner's terminal, which carries neither Codex's nor Claude Code's
markers, and the first real install failed there while every suite passed,
because the suites always ran as a primary. These tests take every marker away
and walk the owner's steps through the real CLI and the real services.
"""
import base64
import json
import os
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401 -- the source CLI import path
from tests.learning_support import sign_owner_payload
from tests.service_harness import ServiceHarness
from tests.test_bridge import CliTestCase

MARKERS = ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_SANDBOX', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID')


class OwnerTerminalFlowTests(ServiceHarness, CliTestCase):
    def setUp(self):
        super().setUp()
        self.start_services()
        terminal = mock.patch.dict(os.environ)
        terminal.start()
        self.addCleanup(terminal.stop)
        for name in MARKERS:
            os.environ.pop(name, None)

    def attest(self, provider, fingerprint):
        """What attest.py does: observe, prepare the statement, sign it, record it."""
        grant = self.write(provider + '-grant.json', {
            'version': 1, 'provider': provider, 'account_fingerprint': fingerprint,
            'no_paid_overage': True, 'no_usage_credits': True, 'issued_at': int(time.time() * 1000)})
        base = ('learning-record-billing-attestation', '--provider', provider, '--grant', grant)
        prepared = self.cli(*base)
        self.assertTrue(prepared['requires_signature'], prepared)
        signature = sign_owner_payload(self.owner_key, base64.b64decode(prepared['signing_payload_b64']),
                                       provider + '-statement')
        self.addCleanup(signature.unlink, missing_ok=True)
        return self.cli(*base, '--signature', str(signature))

    def test_status_observation_and_attestation_work_from_the_owners_terminal(self):
        status = self.cli('learning-status')
        self.assertTrue(status['ready'], status)
        self.assertEqual('ok', status['owner_key']['state'])
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                observed = self.cli('provider-observe', '--provider', provider)
                self.assertTrue(observed['ready'], observed)
                identity = next(r for r in observed['records'] if r['record_type'] == 'provider_identity')
                recorded = self.attest(provider, identity['account_fingerprint'])
                self.assertTrue(recorded['ready'], recorded)
        status = self.cli('learning-status')
        self.assertEqual({'codex': 'present', 'claude': 'present'},
                         {name: value['state'] for name, value in status['provider_attestations'].items()})

    def test_the_owners_terminal_cannot_run_the_lifecycle(self):
        # Opening, routing children and billing probes stay with a primary.
        refused = self.cli('learning-open-orchestration', expected=2)
        self.assertEqual('host_client_unknown', refused['reason_code'])
        probe = self.cli('provider-observe', '--provider', 'claude', '--billing-probe', expected=2)
        self.assertFalse(probe['ready'], probe)
        self.assertNotIn('records', json.loads(json.dumps(probe)))


if __name__ == '__main__':
    unittest.main()
