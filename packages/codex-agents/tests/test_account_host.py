"""The unmodified CLI reaches the account services the collector process hosts.

The collector and project host are the product processes, started by the CLI
itself; the test supplies only a registered disposable state and owner key.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import generate_test_owner_key

from codex_agents.account_api import AccountError, AccountRequest, decode_account_response, encode_account_request
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from codex_agents.collector_paths import CollectorPaths
from codex_agents import account_host
from codex_agents.hosts import OWNER_CLIENT
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.service_enrollment import EnrollmentError, enrollment_preamble, proof_directory, read_capability


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                     'account signature workers need the reviewed macOS sandbox')
class AccountHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.projects = []
        ledger = LearningLedger(self.state)
        try:
            for name in ('first', 'second'):
                root = self.base / name
                (root / '.git').mkdir(parents=True)
                self.projects.append((root, ledger.register_project(root)))
        finally:
            ledger.close()  # The collector process must own the ledger.
        self.paths = CollectorPaths(self.state.root)
        self.addCleanup(self.stop_services)

    def processes(self, entry):
        listed = subprocess.run(['pgrep', '-f', entry + ' --state-root ' + str(self.state.root)],
                                stdout=subprocess.PIPE, text=True, check=False)
        return sorted(int(value) for value in listed.stdout.split())

    def stop_services(self):
        for pid in self.processes('project_host.py'):
            os.kill(pid, signal.SIGTERM)
        for pid in self.processes('collector_host.py'):
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while self.processes('project_host.py') or self.processes('collector_host.py'):
            self.assertLess(time.monotonic(), end, 'service processes did not stop on SIGTERM')
            time.sleep(.05)

    def cli(self, command, *args, project=None, expected=0, terminal=False):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        if not terminal:  # The owner's own terminal carries no client's markers.
            env['CODEX_THREAD_ID'] = 'account-primary'
        done = subprocess.run([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), command,
            '--state-root', str(self.state.root), '--project-root', str(project or self.projects[0][0]), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=40, check=False)
        self.assertEqual(expected, done.returncode, (done.stdout + done.stderr).decode(errors='replace'))
        return json.loads(done.stdout)

    def account_endpoint(self, root):
        hint = json.loads(runtime_hint_path(self.paths, root).read_bytes())
        self.assertEqual(2, hint['schema_version'])
        return hint['account_endpoint']

    def raw(self, endpoint, project_id, subject, *, operation='account.owner_key', label='codex-host-v1'):
        request = AccountRequest(1, operation, 'raw-' + os.urandom(8).hex(), project_id, COLLECTOR_RUNTIME_DIGEST, {})
        frame = encode_account_request(request)
        line = enrollment_preamble(read_capability(self.paths, None, 'account'), 'account', label, 1, frame,
                                   proof_directory(endpoint), subject=str(subject))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(endpoint)
            client.sendall(line + b'\n' + frame + b'\n')
            received = bytearray()
            while b'\n' not in received:
                chunk = client.recv(65_536)
                if not chunk:
                    break
                received.extend(chunk)
        return request, bytes(received).split(b'\n', 1)[0]

    def refusal(self, request, reply):
        with self.assertRaises(AccountError) as refused:
            decode_account_response(reply, request)
        return refused.exception.reason_code

    def test_status_reads_owner_key_through_the_enrolled_account_host(self):
        root, project_id = self.projects[0]
        opened = self.cli('learning-open-orchestration', '--request-id', 'account-open')
        self.assertEqual('active', opened['status'])
        self.assertEqual(1, len(self.processes('collector_host.py')))
        status = self.cli('learning-status')
        self.assertTrue(status['ready'], status)
        self.assertEqual('ok', status['owner_key']['state'])
        self.assertTrue(status['owner_key']['fingerprint'].startswith('SHA256:'))
        self.assertTrue(CollectorClient(self.paths.socket, timeout=2).health(root)['ingestion'])

        endpoint = self.account_endpoint(root)
        request, reply = self.raw(endpoint, project_id, root)
        self.assertEqual('ok', decode_account_response(reply, request)['state'])
        # The other registered project cannot be reached by naming this root, nor this one by naming that root.
        other_root, other_id = self.projects[1]
        for project, subject in ((other_id, root), (project_id, other_root)):
            with self.subTest(project=project, subject=subject.name):
                self.assertEqual('account_not_authorized', self.refusal(*self.raw(endpoint, project, subject)))
        # Owner-only operations are never offered to an enrolled primary.
        self.assertEqual('account_not_authorized',
                         self.refusal(*self.raw(endpoint, project_id, root, operation='account.owner_public_key')))

    def test_the_owners_terminal_reads_status_and_nothing_that_spends(self):
        # Setup, status and attestation run in the owner's own terminal, with no client's markers.
        root, project_id = self.projects[0]
        self.cli('learning-open-orchestration', '--request-id', 'terminal-open')
        status = self.cli('learning-status', terminal=True, expected=0)
        self.assertTrue(status['ready'], status)
        self.assertEqual('ok', status['owner_key']['state'])
        endpoint = self.account_endpoint(root)
        request, reply = self.raw(endpoint, project_id, root, label=OWNER_CLIENT)
        self.assertEqual('ok', decode_account_response(reply, request)['state'])
        # The terminal label cannot open, claim or probe: those stay with an enrolled primary.
        self.assertEqual({'account.health', 'account.project', 'account.owner_key', 'account.capacity_status',
                          'account.observe_provider'}, set(account_host.OWNER_OPERATIONS))
        for operation in ('account.billing_probe', 'account.owner_public_key'):
            with self.subTest(operation=operation):
                self.assertEqual('account_not_authorized', self.refusal(
                    *self.raw(endpoint, project_id, root, operation=operation, label=OWNER_CLIENT)))
        # A run's project role never takes the terminal label.
        with self.assertRaises(EnrollmentError):
            enrollment_preamble(read_capability(self.paths, None, 'account'), 'project', OWNER_CLIENT, 1, b'{}',
                                proof_directory(endpoint), subject=str(root))
        # Nor does the label reach another project.
        other_root, other_id = self.projects[1]
        self.assertEqual('account_not_authorized',
                         self.refusal(*self.raw(endpoint, other_id, root, label=OWNER_CLIENT)))

    def test_a_key_reader_without_write_access_and_a_stopped_collector_get_nothing(self):
        root, project_id = self.projects[0]
        self.cli('learning-open-orchestration', '--request-id', 'reader-open')
        endpoint = self.account_endpoint(root)
        request = AccountRequest(1, 'account.owner_key', 'reader', project_id, COLLECTOR_RUNTIME_DIGEST, {})
        frame = encode_account_request(request)
        line = enrollment_preamble(read_capability(self.paths, None, 'account'), 'account', 'codex-host-v1', 1, frame,
                                   proof_directory(endpoint), subject=str(root), nonce='e' * 32)
        (proof_directory(endpoint) / ('e' * 32)).unlink()  # Read the key, but could not leave the proof.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(endpoint)
            client.sendall(line + b'\n' + frame + b'\n')
            self.assertEqual(b'', client.recv(1))

        for pid in self.processes('collector_host.py'):
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while self.processes('collector_host.py'):
            self.assertLess(time.monotonic(), end)
            time.sleep(.05)
        self.assertFalse(Path(endpoint).exists())
        self.assertFalse((self.paths.root / 'client-runtimes' / 'account.cap').exists())

    def test_the_collector_releases_the_ledger_promptly_after_a_shutdown_request(self):
        # Maintenance stops the collector and starts another; the old process must be
        # gone, with its writer lock, before the new one opens the ledger.
        self.cli('learning-open-orchestration', '--request-id', 'shutdown-open')
        (pid,) = self.processes('collector_host.py')
        started = time.monotonic()
        CollectorClient(self.paths.socket, timeout=2).shutdown()
        while self.processes('collector_host.py'):
            self.assertLess(time.monotonic() - started, 3, 'collector process outlived its shutdown')
            time.sleep(.02)
        self.assertFalse((self.paths.root / 'client-runtimes' / 'account.cap').exists())


if __name__ == '__main__':
    unittest.main()
