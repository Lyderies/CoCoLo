"""Database-trapped CLI and raw clients against the product service host process.

Nothing here supplies a context to a service: the host's own listeners,
capability and enrollment step are the only way in. The CLI wrapper used here
disables the protected-entry and own-roots checks, so those are not exercised;
`test_project_service_startup` drives the unmodified CLI.
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
from codex_agents.history_contracts import HistoryError
from codex_agents.service_enrollment import (
    EnrollmentError, EnrollmentVerifier, capability_path, enrollment_preamble, prepare_proof_directory,
    proof_directory, read_capability,
)
from codex_agents.service_host import EnrolledFrontend, ServiceHostLimits


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
        """Raw exchange: the reply line, b'' once the host closed, None if it never answered."""
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
            except TimeoutError:
                return None
            except ConnectionError:
                pass
            return bytes(received)

    def preamble(self, key, frame, endpoint, *, host='codex-host-v1', generation=1, **options):
        return enrollment_preamble(key, 'host', host, generation, frame, proof_directory(endpoint), **options)

    def enrolled(self, key, frame, endpoint, **options):
        return self.preamble(key, frame, endpoint, **options)+b'\n'+frame+b'\n'

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
        # The owner itself refuses the other host class once enrollment carries it; the CLI keeps the
        # documented lifecycle reason. A direct mutation answers conflict (see below).
        wrong = self.cli('learning-resume-orchestration', *target, '--request-id', 'wrong-host', host='claude', expected=2)
        self.assertEqual('host_client_mismatch', wrong['reason_code'])
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
        proofs = proof_directory(endpoint)
        reader = self.enrolled(key, frame, endpoint, nonce='a'*32)
        (proofs/('a'*32)).unlink()  # Holds the key but cannot write the state root.
        refused = {
            'no preamble': frame+b'\n',
            'key without write proof': reader,
            'another key': self.enrolled(b'x'*32, frame, endpoint),
            'another request': self.preamble(key, self.frame('orchestration.open', 'other-open'), endpoint)+b'\n'+frame+b'\n',
            'another generation': self.enrolled(key, frame, endpoint, generation=2),
            'expired': self.enrolled(key, frame, endpoint, now_ms=now-600_000),
            'beyond the clock tolerance': self.enrolled(key, frame, endpoint, now_ms=now+600_000),
            'altered label': self.enrolled(key, frame, endpoint).replace(b'codex-host-v1', b'claude-host-v1', 1),
        }
        for name, payload in refused.items():
            with self.subTest(name):
                self.assertEqual(b'', self.send(endpoint, payload, wait=4))  # Closed by the host, not timed out.
        # A verified preamble with no request behind it is held, never answered.
        self.assertIsNone(self.send(endpoint, self.preamble(key, frame, endpoint)+b'\n', wait=1))

        accepted = self.enrolled(key, frame, endpoint, nonce='c'*32)
        reply = json.loads(self.send(endpoint, accepted))
        # First sight of this request identity: no refused attempt committed it.
        self.assertTrue(reply['ready'])
        self.assertEqual((1, False, 'codex-host-v1'), (reply['result']['current']['snapshot']['sequence'],
            reply['result']['known'], reply['result']['current']['snapshot']['client_version']))
        self.assertEqual(b'', self.send(endpoint, accepted, wait=4))  # Captured bytes cannot be replayed.
        self.assertFalse((proofs/('c'*32)).exists())  # The accepted proof was consumed.

        # A correctly enrolled other host class still cannot change this orchestration.
        handle = reply['result']['current']['snapshot']['orchestration_handle']
        pause = self.frame('orchestration.pause', 'claude-pause', {'orchestration_handle': handle, 'expected_sequence': 1})
        denied = json.loads(self.send(endpoint, self.enrolled(key, pause, endpoint, host='claude-host-v1')))
        self.assertEqual((False, 'history_conflict'), (denied['ready'], denied['reason_code']))
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

    def test_second_host_with_another_history_still_cannot_take_the_same_root(self):
        _, ready = self.start()
        hint, key = runtime_hint_path(self.paths, self.project), read_capability(self.paths, self.project, 'host')
        before = hint.read_bytes()
        (self.state/'other-history').mkdir(mode=0o700)
        second, refused = self.start(expect='refused', history='other-history')
        self.assertEqual(0, second.wait(timeout=8))
        self.assertEqual('history_writer_busy', refused['reason_code'])
        self.assertEqual((before, key), (hint.read_bytes(), read_capability(self.paths, self.project, 'host')))
        reply = self.send(ready['project'], self.enrolled(key, self.frame('orchestration.open', 'still-first'), ready['project']))
        self.assertTrue(json.loads(reply)['ready'])

    def test_known_gap_a_new_supervisor_generation_cannot_read_earlier_orchestrations(self):
        # Recorded so the gap stays visible: succession between generations is not designed yet.
        first, _ = self.start()
        opened = self.cli('learning-open-orchestration', '--request-id', 'generation-one')
        self.stop(first)
        self.start(generation=2)
        orphaned = self.cli('learning-orchestration-status', '--orchestration-handle', opened['orchestration_handle'],
                            expected=2)
        self.assertEqual('host_client_mismatch', orphaned['reason_code'])  # Reported as such; the cause is the generation.

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
            self.enrolled(old, self.frame('orchestration.open', 'stale-open'), again['project']), wait=4))
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

    def test_finished_exchanges_free_their_context_slots(self):
        _, ready = self.start()
        endpoint, key = ready['project'], read_capability(self.paths, self.project, 'host')
        opened = json.loads(self.send(endpoint, self.enrolled(key, self.frame('orchestration.open', 'slots-open'), endpoint)))
        handle = opened['result']['current']['snapshot']['orchestration_handle']
        # The fixture retains 16 contexts for 30 seconds; 40 completed exchanges must all be served.
        for index in range(40):
            status = self.frame('orchestration.status', 'slots-status-%d' % index, {'orchestration_handle': handle})
            reply = self.send(endpoint, self.enrolled(key, status, endpoint))
            self.assertTrue(reply, index)
            self.assertTrue(json.loads(reply)['ready'], index)

    def test_unenrolled_control_connections_cannot_take_the_project_listeners_budget(self):
        _, ready = self.start(pending=2, enrollment_timeout_ms=5000)
        stalled = []
        for _ in range(6):  # More than the control budget; the rest fill or overflow its backlog.
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            try:
                client.connect(ready['control'])
                client.sendall(b'{"enrollment_version":1')
            except OSError:
                break
            stalled.append(client)
        self.assertGreaterEqual(len(stalled), 2)
        key = read_capability(self.paths, self.project, 'host')
        started = time.monotonic()
        reply = self.send(ready['project'], self.enrolled(key, self.frame('orchestration.open', 'beside-flood'), ready['project']))
        self.assertTrue(json.loads(reply)['ready'])
        self.assertLess(time.monotonic()-started, 2)  # Well inside the stalled senders' five-second deadline.


class EnrolledFrontendTests(unittest.TestCase):
    """The enrollment step alone, on socket pairs and a controlled clock."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proofs = self.root/'proofs'
        prepare_proof_directory(self.proofs)
        self.key, self.now, self.attached = b'k'*32, [1_000_000_000], []
        verifier = EnrollmentVerifier(self.key, 'host', 1, self.proofs)
        self.frontend = EnrolledFrontend(verifier, ServiceHostLimits(2, 1_000_000_000, 2, 60_000_000_000, 30_000, 64),
            lambda kind, connection, token: self.attached.append((kind, connection, token)), self.proofs,
            clock_ns=lambda: self.now[0])
        self.frontend.listen(self.root/'p.sock', 'project')
        self.addCleanup(self.frontend.close)
        self.clients = []

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.root/'p.sock'))
        self.clients.append(client)
        return client

    def line(self, message=b'{"request":1}'):
        return enrollment_preamble(self.key, 'host', 'codex-host-v1', 1, message, self.proofs)+b'\n'

    def test_a_line_completed_after_the_deadline_is_refused(self):
        client, line = self.connect(), self.line()
        client.sendall(line[:20])
        self.frontend.step()
        self.assertEqual([], self.attached)
        self.now[0] += 1_000_000_001  # Past the absolute deadline before the owner looks again.
        client.sendall(line[20:])
        self.frontend.step()
        self.assertEqual([], self.attached)
        client.settimeout(2)
        self.assertEqual(b'', client.recv(1))

    def test_a_closed_connection_releases_its_context_at_once(self):
        for _ in range(5):  # Two context slots; each exchange closes before the next enrolls.
            client = self.connect()
            client.sendall(self.line())
            self.frontend.step()
            kind, connection, token = self.attached[-1]
            self.assertEqual('project', kind)
            self.assertIsNotNone(self.frontend.context(token))
            connection.close()
        self.assertEqual(5, len(self.attached))

    def test_a_refused_attach_and_a_closed_frontend_leave_no_context(self):
        refusing = EnrolledFrontend(EnrollmentVerifier(self.key, 'host', 1, self.proofs),
            ServiceHostLimits(2, 1_000_000_000, 2, 60_000_000_000, 30_000, 64),
            lambda kind, connection, token: (_ for _ in ()).throw(HistoryError('history_service_unavailable')),
            self.proofs, clock_ns=lambda: self.now[0])
        refusing.listen(self.root/'r.sock', 'project')
        self.addCleanup(refusing.close)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.root/'r.sock'))
        client.sendall(self.line())
        refusing.step()
        client.settimeout(2)
        self.assertEqual(b'', client.recv(1))
        self.assertEqual({}, refusing._contexts)
        accepted = self.connect()
        accepted.sendall(self.line())
        self.frontend.step()
        token = self.attached[-1][2]
        self.assertIsNotNone(self.frontend.context(token))
        self.frontend.close()
        self.assertIsNone(self.frontend.context(token))
        self.assertFalse((self.root/'p.sock').exists())


if __name__ == '__main__':
    unittest.main()
