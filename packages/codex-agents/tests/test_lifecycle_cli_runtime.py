"""Fresh lifecycle CLI to product owner process; native enrollment is synthetic."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

from tests.support import TEMP_ROOT
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.orchestration_api import OrchestrationScope


class LifecycleCliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project, self.state = self.root/'project', self.root/'state'
        self.project.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.key = b'q'*32  # Disposable fixture state only, never native authority.
        (self.state/'digest.key').write_bytes(self.key)
        (self.state/'digest.key').chmod(0o600)
        (self.state/'history').mkdir(mode=0o700)
        self.scope = OrchestrationScope('prj_'+'a'*32, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
        self.server = None
        self.clients = []
        self.addCleanup(self.stop)

    def read_server(self, stage):
        self.assertTrue(select.select([self.server.stdout], [], [], 10)[0], 'owner fixture response expired')
        raw = self.server.stdout.readline(16_385)
        if not raw:
            self.server.wait(timeout=5)
            self.fail(self.server.stderr.read().decode(errors='replace'))
        result = json.loads(raw)
        self.assertEqual(stage, result['stage'])
        return result

    def start(self, *, host='codex', authorized=True, drop_identity=None, drop_first_open=False):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        self.server = subprocess.Popen([sys.executable, '-B', '-m', 'tests.project_runtime_fixture'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.server.stdin.write(canonical_json({'scope': asdict(self.scope), 'root': str(self.state),
            'fixture_key': self.key.hex(), 'client_version': host+'-host-v1', 'authorized': authorized,
            'drop_identity': drop_identity, 'drop_first_open': drop_first_open})+b'\n')
        self.server.stdin.flush()
        ready = self.read_server('ready')
        self.assertEqual((1, 0), (ready['startup_store_opens'], ready['tickets']))
        state = CollectorPaths(self.state)
        hint = runtime_hint_path(state, self.project)
        hint.parent.mkdir(mode=0o700)
        hint.write_bytes(canonical_json({'schema_version': 1, 'project_root_digest': keyed_digest(self.key, str(self.project)),
            'scope': ready['scope'], 'dispatch_endpoint': ready['dispatch_endpoint'], 'result_endpoint': ready['result_endpoint']}))
        hint.chmod(0o600)

    def stop(self):
        for client in self.clients:
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=5)
            client.stdout.close()
            client.stderr.close()
        if self.server is not None:
            if self.server.poll() is None:
                self.server.stdin.write(b'{"stop":true}\n')
                self.server.stdin.flush()
                stopped = self.read_server('stopped')
                self.assertEqual((1, 0, True), (stopped['store_opens'], stopped['tickets'], stopped['threads_stopped']))
                self.assertEqual(0, self.server.wait(timeout=8), self.server.stderr.read().decode(errors='replace'))
            self.server.stdin.close()
            self.server.stdout.close()
            self.server.stderr.close()

    def cli(self, command, *args, host='codex', expected=0):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
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
        output, errors = process.communicate(timeout=12)
        self.assertEqual(b'', errors)
        self.assertEqual(expected, process.returncode, output.decode(errors='replace'))
        return json.loads(output)

    def test_original_lifecycle_and_lost_reply_reconciliation_never_take_database_or_rewind_state(self):
        self.start(drop_identity='lost-checkpoint')
        opened = self.cli('learning-open-orchestration', '--request-id', 'original-open')
        handle = opened['orchestration_handle']
        target = ('--orchestration-handle', handle)
        self.assertEqual(('active', 1, False), (opened['status'], opened['sequence'], opened['known']))
        lost = self.cli('learning-checkpoint-orchestration', *target, '--goal-tokens-used', '10',
            '--goal-time-used-seconds', '2', '--request-id', 'lost-checkpoint', expected=2)
        self.assertEqual(('lost-checkpoint', 1), (lost['request_id'], lost['expected_sequence']))
        known = self.cli('learning-checkpoint-orchestration', *target, '--goal-tokens-used', '10',
            '--goal-time-used-seconds', '2', '--request-id', lost['request_id'], '--expected-sequence', '1')
        self.assertEqual((2, 10, True), (known['sequence'], known['goal_tokens_used'], known['known']))
        paused = self.cli('learning-pause-orchestration', *target, '--request-id', 'original-pause')
        self.assertEqual(('paused', 3), (paused['status'], paused['sequence']))
        wrong = self.cli('learning-resume-orchestration', *target, '--request-id', 'wrong-host', host='claude', expected=2)
        self.assertEqual('host_client_mismatch', wrong['reason_code'])
        resumed = self.cli('learning-resume-orchestration', *target, '--request-id', 'original-resume')
        self.assertEqual(('active', 4), (resumed['status'], resumed['sequence']))
        old = self.cli('learning-pause-orchestration', *target, '--request-id', 'original-pause', '--expected-sequence', '2')
        self.assertTrue(old['known'])
        self.assertEqual(('active', 4), (old['status'], old['sequence']))
        self.assertEqual('paused', old['receipt']['snapshot']['status'])
        closed = self.cli('learning-close-orchestration', *target, '--status', 'completed',
            '--goal-tokens-used', '14', '--goal-time-used-seconds', '3', '--request-id', 'original-close')
        self.assertEqual(('completed', 5), (closed['status'], closed['sequence']))
        denied = self.cli('learning-resume-orchestration', *target, '--request-id', 'cannot-reopen', expected=2)
        self.assertEqual('history_orchestration_state', denied['reason_code'])
        old_open = self.cli('learning-open-orchestration', '--request-id', 'original-open')
        self.assertEqual(('completed', 5, True), (old_open['status'], old_open['sequence'], old_open['known']))
        current = self.cli('learning-orchestration-status', *target)
        self.assertEqual(('completed', 5), (current['status'], current['sequence']))

    def test_claude_primary_lifecycle_uses_same_original_owner_without_sql(self):
        self.start(host='claude')
        opened = self.cli('learning-open-orchestration', '--request-id', 'claude-open', host='claude')
        current = self.cli('learning-orchestration-status', '--orchestration-handle', opened['orchestration_handle'], host='claude')
        self.assertEqual('claude-host-v1', current['client_version'])
        self.assertEqual(1, current['sequence'])

    def test_lost_open_reports_generated_identity_for_exact_explicit_reconciliation(self):
        self.start(drop_first_open=True)
        lost = self.cli('learning-open-orchestration', expected=2)
        self.assertTrue(lost['request_id'].startswith('cli-orc-'))
        known = self.cli('learning-open-orchestration', '--request-id', lost['request_id'])
        self.assertEqual((True, 1, 'active'), (known['known'], known['sequence'], known['status']))

    def test_unconfigured_frontend_and_missing_runtime_have_no_direct_fallback(self):
        absent = self.cli('learning-open-orchestration', '--request-id', 'absent', expected=2)
        self.assertEqual('orchestration_client_unavailable', absent['reason_code'])
        self.start(authorized=False)
        denied = self.cli('learning-open-orchestration', '--request-id', 'denied', expected=2)
        self.assertFalse(denied['ready'])
        malformed = self.cli('learning-open-orchestration', '--request-id', 'PRIVATE INVALID ID', expected=2)
        self.assertNotIn('PRIVATE', json.dumps(malformed))
        self.assertNotIn('request_id', malformed)
