"""Primary-only RSF-S4 collector socket and CLI checks; do not run in the restricted worktree."""
import base64
import json
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_rsf_s4_slots as store
from tests.learning_support import sign_owner_payload
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from codex_agents import collector_client, learning_store
from codex_agents.learning_contracts import run_slot_confirmation
from codex_agents.learning_engine import OwnerGrantAuthority


class LegacySlotTransportTests(unittest.TestCase):
    def setUp(self):
        self.case = store.LegacySlotEvidenceTests()
        with mock.patch.object(store.legacy, 'TEMP_ROOT', SHORT_TMP):
            self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.seed(native=True, two=True)
        self.start()

    def start(self):
        self.server = learning_store.CollectorServer(self.case.ledger, client_capabilities=self.case.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = collector_client.CollectorClient(self.case.fixture.state.socket, timeout=2)

    def signed(self):
        record = run_slot_confirmation('legacy-run-0')
        payload = self.client.owner_record_payload(self.case.fixture.project_a, record)
        self.assertEqual(OwnerGrantAuthority.owner_record_payload(self.case.project, record), payload)
        signature = sign_owner_payload(self.case.fixture.owner_private_key, payload, 's4-wire')
        return record, signature

    def cli(self, *arguments):
        completed = subprocess.run([sys.executable, '-B', str(ORCHESTRATION_ROOT / 'cli.py'),
            'learning-confirm-run-slot', '--project-root', str(self.case.fixture.project_a),
            '--state-root', str(self.case.fixture.state.root), '--run-id', 'legacy-run-0', *arguments],
            capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def test_socket_status_owner_authentication_scope_runtime_gate_and_restart(self):
        root = self.case.fixture.project_a
        holds = self.client.health(root)['account_capacity']['holds']['run_slot']
        self.assertEqual(2, holds['held'])
        self.assertIsNone(holds['next_eligible'])
        self.assertIn('waiting for process evidence or owner confirmation', holds['reasons'])
        self.assertEqual(2, len(holds['owner_confirmations']))
        record, path = self.signed()
        with mock.patch.object(collector_client, 'COLLECTOR_RUNTIME_DIGEST', '0'*64):
            with self.assertRaises(collector_client.CollectorUpgradeRequired):
                self.client.confirm_legacy_slot(root, record, path.read_bytes())
        with self.assertRaises(collector_client.CollectorError):
            self.client.confirm_legacy_slot(root, record, b'forged')
        with self.assertRaises(collector_client.CollectorError):
            self.client.confirm_legacy_slot(self.case.fixture.project_b, record, path.read_bytes())
        self.client.confirm_legacy_slot(root, record, path.read_bytes())
        self.assertEqual(1, self.case.count())
        receipt = self.case.ledger._connection.execute(
            "SELECT release_digest FROM active_run_capacity WHERE run_id='legacy-run-0'").fetchone()[0]
        self.server.stop()
        self.case.reopen()
        self.start()
        self.client.confirm_legacy_slot(root, record, path.read_bytes())
        self.assertEqual(1, self.case.count())
        self.assertEqual(receipt, self.case.ledger._connection.execute(
            "SELECT release_digest FROM active_run_capacity WHERE run_id='legacy-run-0'").fetchone()[0])

    def test_cli_unsigned_request_then_signed_confirmation_and_retry(self):
        request = self.cli()
        self.assertTrue(request['requires_signature'])
        self.assertEqual(2, self.case.count())
        signature = sign_owner_payload(self.case.fixture.owner_private_key,
            base64.b64decode(request['signing_payload_b64']), 's4-cli')
        self.assertTrue(self.cli('--signature', str(signature))['ready'])
        self.assertEqual(1, self.case.count())
        self.assertTrue(self.cli('--signature', str(signature))['ready'])
        self.assertEqual(1, self.case.count())
