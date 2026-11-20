"""The unmodified CLI reaches the account services the collector process hosts.

The collector and project host are the product processes, started by the CLI
itself; the test supplies only a registered disposable state and owner key.
"""
from tests.support import assert_refused
import base64
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
from tests.learning_support import generate_test_owner_key, sign_owner_payload

from codex_agents.account_api import AccountError, AccountRequest, decode_account_response, encode_account_request
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from codex_agents.collector_paths import CollectorPaths
from codex_agents import account_host
from codex_agents.hosts import OWNER_CLIENT
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.service_enrollment import EnrollmentError, enrollment_preamble, proof_directory, read_capability


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
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

    def late_stop(self):
        """O10 SD: after a missed stop deadline, give the collector up to 30 s more, then report its
        exit record's step timings (stop_ms) so the slow step is named. Diagnostic only.
        O10 SD3-4: only a record this collector wrote is reported. The state root keeps the record
        of the collector before it (a test that stops one collector on purpose, then starts another),
        and SD1/SD2 were misled by that one. A record is this collector's when it started no earlier
        than the oldest collector process still running here."""
        from codex_agents import collector_exit
        started = [self.started_ms(pid) for pid in self.processes('collector_host.py')]
        oldest = None if not started or None in started else min(started)
        end = time.monotonic() + 30
        while self.processes('collector_host.py') and time.monotonic() < end:
            time.sleep(.1)
        record = collector_exit.read_exit(self.state.root)
        report = {'stopped_after_s': round(30 - max(0.0, end - time.monotonic()), 1)}
        if record is not None and (oldest is None or type(record.get('started_at_ms')) is not int
                                   or record['started_at_ms'] < oldest):
            return {**report, 'cause': 'no exit record from this collector',
                    'other_collector_record': record.get('cause')}
        return {**report, 'cause': record and record.get('cause'), 'stop_ms': record and record.get('stop_ms'),
                'stalled_in': record and record.get('stalled_in')}

    @staticmethod
    def started_ms(pid):
        """When process `pid` started, rounded down to the second (wall ms), from ps's elapsed time."""
        text = subprocess.run(['ps', '-o', 'etime=', '-p', str(pid)], stdout=subprocess.PIPE, text=True,
                              check=False).stdout.strip()
        try:
            days, _, clock = text.rpartition('-')
            parts = [int(part) for part in clock.split(':')]
            seconds = int(days or 0) * 86400 + sum(value * 60 ** index for index, value in enumerate(reversed(parts)))
        except ValueError:
            return None
        return time.time_ns() // 1_000_000 - (seconds + 1) * 1000

    def stop_services(self):
        for pid in self.processes('project_host.py'):
            os.kill(pid, signal.SIGTERM)
        for pid in self.processes('collector_host.py'):
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while self.processes('project_host.py') or self.processes('collector_host.py'):
            if time.monotonic() >= end:
                # O10: which process outlived the signal, and how long it had run, for the next report.
                left = {entry: self.processes(entry) for entry in ('project_host.py', 'collector_host.py')}
                ages = subprocess.run(['ps', '-o', 'pid=,etime=,stat=', '-p',
                                       ','.join(str(pid) for pids in left.values() for pid in pids) or '0'],
                                      stdout=subprocess.PIPE, text=True, check=False).stdout.split('\n')
                self.fail(f'service processes did not stop on SIGTERM: {left} {ages} {self.late_stop()}')
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
        if expected is None:  # The caller checks the exit status itself: (status, reply).
            return done.returncode, json.loads(done.stdout)
        self.assertEqual(expected, done.returncode, (done.stdout + done.stderr).decode(errors='replace'))
        return json.loads(done.stdout)

    def account_endpoint(self, root):
        hint = json.loads(runtime_hint_path(self.paths, root).read_bytes())
        self.assertEqual(2, hint['schema_version'])
        return hint['account_endpoint']

    def raw(self, endpoint, project_id, subject, *, operation='account.owner_key', label='codex-host-v1', payload=None):
        request = AccountRequest(1, operation, 'raw-' + os.urandom(8).hex(), project_id, COLLECTOR_RUNTIME_DIGEST,
                                 {} if payload is None else payload)
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
        # Nor may it refresh a run's launch evidence. (No positive control here: a primary's
        # request would reach the real provider client.)
        run_observation = {'provider': 'codex', 'run_handle': 'hrn_' + '0' * 48, 'phase': 'before'}
        self.assertEqual('account_not_authorized', self.refusal(*self.raw(
            endpoint, project_id, root, operation='account.observe_provider', label=OWNER_CLIENT,
            payload=run_observation)))
        # A run's project role never takes the terminal label.
        with self.assertRaises(EnrollmentError):
            enrollment_preamble(read_capability(self.paths, None, 'account'), 'project', OWNER_CLIENT, 1, b'{}',
                                proof_directory(endpoint), subject=str(root))
        # Nor does the label reach another project.
        other_root, other_id = self.projects[1]
        self.assertEqual('account_not_authorized',
                         self.refusal(*self.raw(endpoint, other_id, root, label=OWNER_CLIENT)))

    def test_a_key_added_after_the_collector_opened_the_ledger_is_recorded(self):
        # A new machine: setup registers first; the attestation adds the owner key afterwards,
        # while the collector already holds the ledger. Status must not call that a mismatch.
        fresh = LearningState(self.base / 'fresh')
        authorize_collector_runtime(fresh, ORCHESTRATION_ROOT / 'cli.py')
        root = self.base / 'newcomer'
        (root / '.git').mkdir(parents=True)
        ledger = LearningLedger(fresh)
        try:
            ledger.register_project(root)
        finally:
            ledger.close()
        self.state, self.paths = fresh, CollectorPaths(fresh.root)
        self.cli('learning-open-orchestration', '--request-id', 'newcomer-open', project=root)
        before = self.cli('learning-status', project=root, terminal=True)
        self.assertEqual('not_configured', before['owner_key']['state'])
        (self.base / 'second-key').mkdir()
        _, public_text = generate_test_owner_key(self.base / 'second-key')
        public = self.base / 'owner.pub'
        public.write_text(public_text)
        # What attest.py runs: this command names no project.
        configured = subprocess.run([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'),
            'learning-configure-owner-key', '--state-root', str(fresh.root), '--public-key', str(public)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40, check=False)
        self.assertEqual(0, configured.returncode, configured.stdout + configured.stderr)
        # Then the attestation signs its first statement with that key, as setup does next.
        grant = self.base / 'grant.json'
        grant.write_text(json.dumps({'version': 1, 'provider': 'codex', 'account_fingerprint': 'a' * 64,
                                     'no_paid_overage': True, 'no_usage_credits': True,
                                     'issued_at': int(time.time() * 1000)}))
        base = ('learning-record-billing-attestation', '--provider', 'codex', '--grant', str(grant))
        prepared = self.cli(*base, project=root, terminal=True)
        private = self.base / 'second-key' / 'test-owner-key'
        signature = sign_owner_payload(private, base64.b64decode(prepared['signing_payload_b64']), 'statement')
        self.cli(*base, '--signature', str(signature), project=root, terminal=True)
        after = self.cli('learning-status', project=root, terminal=True)
        self.assertEqual('ok', after['owner_key']['state'], after)
        self.assertIsNotNone(after['owner_key']['fingerprint'])

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
            assert_refused(self, client, 'service_refused_enrollment')  # R7b: refused with its fixed reason

        for pid in self.processes('collector_host.py'):
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while self.processes('collector_host.py'):
            self.assertLess(time.monotonic(), end, self.late_stop() if time.monotonic() >= end else '')
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
