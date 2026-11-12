"""Fresh CLI and raw clients against the product service host process.

Nothing here supplies a context to a service: the host's own listeners,
capability and enrollment step are the only way in.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import TEMP_ROOT
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_contracts import canonical_json
from codex_agents.orchestration_api import OrchestrationRequest, OrchestrationScope, encode_orchestration_request
from codex_agents.service_enrollment import EnrollmentError, capability_path, enrollment_preamble, read_capability


def _environment():
    return {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}


class ServiceHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project, self.state = self.root/'project', self.root/'state'
        self.project.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.key = b'h'*32  # Disposable state digest key, not a capability.
        (self.state/'digest.key').write_bytes(self.key)
        (self.state/'digest.key').chmod(0o600)
        (self.state/'history').mkdir(mode=0o700)
        self.paths = CollectorPaths(self.state)
        self.servers, self.clients = [], []
        self.addCleanup(self.stop_all)

    def scope(self, generation=1):
        return OrchestrationScope('prj_'+'b'*32, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', generation)

    def read_server(self, server, *stages):
        self.assertTrue(select.select([server.stdout], [], [], 10)[0], 'service host response expired')
        raw = server.stdout.readline(16_385)
        if not raw:
            server.wait(timeout=5)
            self.fail(server.stderr.read().decode(errors='replace'))
        result = json.loads(raw)
        self.assertIn(result['stage'], stages)
        return result

    def start(self, *, generation=1, expect='ready', **options):
        server = subprocess.Popen([sys.executable, '-B', '-m', 'tests.service_host_fixture'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_environment())
        self.servers.append(server)
        server.stdin.write(canonical_json({'scope': asdict(self.scope(generation)), 'state': str(self.state),
            'project': str(self.project), 'fixture_key': self.key.hex(), **options})+b'\n')
        server.stdin.flush()
        return server, self.read_server(server, expect)

    def stop(self, server):
        if server.poll() is None:
            server.stdin.write(b'{"stop":true}\n')
            server.stdin.flush()
            self.assertTrue(self.read_server(server, 'stopped')['threads_stopped'])
            self.assertEqual(0, server.wait(timeout=8), server.stderr.read().decode(errors='replace'))

    def stop_all(self):
        for client in self.clients:
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=5)
            client.stdout.close()
            client.stderr.close()
        for server in self.servers:
            try:
                self.stop(server)
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait(timeout=5)
                server.stdin.close()
                server.stdout.close()
                server.stderr.close()

    def cli(self, command, *args, host='codex', expected=0):
        env = _environment()
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        if host == 'codex':
            env['CODEX_THREAD_ID'] = 'fixture-primary'
        else:
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='fixture-primary')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.dispatch_cli_fixture', command,
            '--state-root', str(self.state), '--project-root', str(self.project), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.clients.append(process)
        output, errors = process.communicate(timeout=15)
        self.assertEqual(b'', errors)
        self.assertEqual(expected, process.returncode, output.decode(errors='replace'))
        return json.loads(output)

    def frame(self, operation, identity, payload=None, *, generation=1):
        return encode_orchestration_request(OrchestrationRequest(1, operation, identity, self.scope(generation), payload or {}))

    def send(self, endpoint, payload, *, wait=3):
        """Raw exchange: the reply line, or b'' when the host dropped the connection."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(wait)
            client.connect(endpoint)
            client.sendall(payload)
            received = bytearray()
            try:
                while b'\n' not in received:
                    chunk = client.recv(8192)
                    if not chunk:
                        break
                    received.extend(chunk)
            except (TimeoutError, ConnectionError):
                pass
            return bytes(received)

    def enrolled(self, key, frame, *, host='codex-host-v1', generation=1, **options):
        return enrollment_preamble(key, 'host', host, generation, frame, **options)+b'\n'+frame+b'\n'

    def test_fresh_cli_lifecycle_uses_only_what_the_host_published(self):
        server, ready = self.start()
        hint, capability = runtime_hint_path(self.paths, self.project), capability_path(self.paths, self.project, 'host')
        for path in (hint, capability):
            info = path.lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual((0o600, 1), (stat.S_IMODE(info.st_mode), info.st_nlink))
        self.assertEqual(32, capability.stat().st_size)
        for endpoint in (ready['control'], ready['project']):
            info = Path(endpoint).lstat()
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
            self.assertTrue(Path(endpoint).is_relative_to(self.state))
        self.assertNotIn(str(self.project).encode(), hint.read_bytes())

        opened = self.cli('learning-open-orchestration', '--request-id', 'original-open')
        target = ('--orchestration-handle', opened['orchestration_handle'])
        self.assertEqual(('active', 1, False, 'codex-host-v1'),
                         (opened['status'], opened['sequence'], opened['known'], opened['client_version']))
        checkpoint = self.cli('learning-checkpoint-orchestration', *target, '--goal-tokens-used', '10',
            '--goal-time-used-seconds', '2', '--request-id', 'original-checkpoint')
        self.assertEqual((2, 10), (checkpoint['sequence'], checkpoint['goal_tokens_used']))
        paused = self.cli('learning-pause-orchestration', *target, '--request-id', 'original-pause')
        self.assertEqual(('paused', 3), (paused['status'], paused['sequence']))
        # The owner itself refuses the other host class once enrollment carries it.
        wrong = self.cli('learning-resume-orchestration', *target, '--request-id', 'wrong-host', host='claude', expected=2)
        self.assertEqual('history_not_authorized', wrong['reason_code'])
        resumed = self.cli('learning-resume-orchestration', *target, '--request-id', 'original-resume')
        self.assertEqual(('active', 4), (resumed['status'], resumed['sequence']))
        closed = self.cli('learning-close-orchestration', *target, '--status', 'completed',
            '--goal-tokens-used', '14', '--goal-time-used-seconds', '3', '--request-id', 'original-close')
        self.assertEqual(('completed', 5), (closed['status'], closed['sequence']))

        claude = self.cli('learning-open-orchestration', '--request-id', 'claude-open', host='claude')
        self.assertEqual(('active', 'claude-host-v1'), (claude['status'], claude['client_version']))

        self.stop(server)
        for path in (hint, capability, Path(ready['control']), Path(ready['project'])):
            self.assertFalse(os.path.lexists(path))
        gone = self.cli('learning-orchestration-status', *target, expected=2)
        self.assertEqual('orchestration_client_unavailable', gone['reason_code'])

    def test_only_a_fresh_exact_single_use_preamble_reaches_the_owner(self):
        _, ready = self.start()
        endpoint, key = ready['project'], read_capability(self.paths, self.project, 'host')
        frame, now = self.frame('orchestration.open', 'guarded-open'), time.time_ns()//1_000_000
        refused = {
            'no preamble': frame+b'\n',
            'another key': self.enrolled(b'x'*32, frame),
            'another request': enrollment_preamble(key, 'host', 'codex-host-v1', 1,
                                                   self.frame('orchestration.open', 'other-open'))+b'\n'+frame+b'\n',
            'another generation': self.enrolled(key, frame, generation=2),
            'expired': self.enrolled(key, frame, now_ms=now-600_000),
            'postdated': self.enrolled(key, frame, now_ms=now+600_000),
            'altered label': self.enrolled(key, frame).replace(b'codex-host-v1', b'claude-host-v1', 1),
            'preamble only': enrollment_preamble(key, 'host', 'codex-host-v1', 1, frame)+b'\n',
        }
        for name, payload in refused.items():
            with self.subTest(name):
                self.assertEqual(b'', self.send(endpoint, payload, wait=2))

        accepted = self.enrolled(key, frame)
        reply = json.loads(self.send(endpoint, accepted))
        # First sight of this request identity: no refused attempt committed it.
        self.assertTrue(reply['ready'])
        self.assertEqual((1, False, 'codex-host-v1'), (reply['result']['current']['snapshot']['sequence'],
            reply['result']['known'], reply['result']['current']['snapshot']['client_version']))
        self.assertEqual(b'', self.send(endpoint, accepted, wait=2))  # Captured bytes cannot be replayed.

        # A correctly enrolled other host class still cannot change this orchestration.
        handle = reply['result']['current']['snapshot']['orchestration_handle']
        pause = self.frame('orchestration.pause', 'claude-pause', {'orchestration_handle': handle, 'expected_sequence': 1})
        denied = json.loads(self.send(endpoint, self.enrolled(key, pause, host='claude-host-v1')))
        self.assertFalse(denied['ready'])
        self.assertIn(denied['reason_code'], {'history_not_authorized', 'history_conflict'})
        status = self.cli('learning-orchestration-status', '--orchestration-handle', handle)
        self.assertEqual(('active', 1), (status['status'], status['sequence']))

    def test_second_host_refuses_without_disturbing_the_first(self):
        _, ready = self.start()
        hint = runtime_hint_path(self.paths, self.project)
        before, key = hint.read_bytes(), read_capability(self.paths, self.project, 'host')
        opened = self.cli('learning-open-orchestration', '--request-id', 'first-open')
        second, refused = self.start(expect='refused')
        self.assertEqual(0, second.wait(timeout=8))
        self.assertIn(refused['reason_code'], {'history_service_unavailable', 'history_conflict', 'history_wrong_owner'})
        self.assertEqual((before, key), (hint.read_bytes(), read_capability(self.paths, self.project, 'host')))
        for endpoint in (ready['control'], ready['project']):
            self.assertTrue(stat.S_ISSOCK(Path(endpoint).lstat().st_mode))
        status = self.cli('learning-orchestration-status', '--orchestration-handle', opened['orchestration_handle'])
        self.assertEqual(('active', 1), (status['status'], status['sequence']))

    def test_restart_replaces_the_capability_and_keeps_history(self):
        first, ready = self.start()
        old = read_capability(self.paths, self.project, 'host')
        opened = self.cli('learning-open-orchestration', '--request-id', 'before-restart')
        self.stop(first)
        with self.assertRaises(EnrollmentError):
            read_capability(self.paths, self.project, 'host')
        _, again = self.start()
        new = read_capability(self.paths, self.project, 'host')
        self.assertNotEqual(old, new)
        # A capability from before the restart is dead even inside its freshness window.
        self.assertEqual(b'', self.send(again['project'],
            self.enrolled(old, self.frame('orchestration.open', 'stale-open')), wait=2))
        status = self.cli('learning-orchestration-status', '--orchestration-handle', opened['orchestration_handle'])
        self.assertEqual(('active', 1, 'codex-host-v1'), (status['status'], status['sequence'], status['client_version']))

    def test_stalled_unenrolled_senders_are_dropped_and_do_not_hold_enrolled_clients(self):
        _, ready = self.start(pending=2, enrollment_timeout_ms=300)
        stalled = []
        for _ in range(2):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.connect(ready['project'])
            client.sendall(b'{"enrollment_version":1')  # No line end; never completes.
            stalled.append(client)
        started = time.monotonic()
        opened = self.cli('learning-open-orchestration', '--request-id', 'after-stall')
        self.assertEqual('active', opened['status'])
        self.assertLess(time.monotonic()-started, 8)
        for client in stalled:
            client.settimeout(3)
            self.assertEqual(b'', client.recv(1))


if __name__ == '__main__':
    unittest.main()
