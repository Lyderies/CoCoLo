"""The unmodified CLI starts and reuses a real project host on demand.

A disposable ledger and collector stand in for the registry owner. The CLI,
the detached host it starts, their enrollment and the project history are the
product paths, with no fixture context, hint or capability supplied by the test.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import generate_test_owner_key

from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.project_service import history_directory, serve_project, supervisor_scope
from codex_agents.service_enrollment import capability_path


class ProjectServiceStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.project = self.base / 'project'
        (self.project / '.git').mkdir(parents=True)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.project_id = self.ledger.register_project(self.project)
        self.collector = CollectorServer(self.ledger)
        self.collector.start()
        self.addCleanup(self.collector.stop)
        self.paths = CollectorPaths(self.state.root)
        self.addCleanup(self.stop_hosts)

    def hosts(self):
        listed = subprocess.run(['pgrep', '-f', 'project_host.py --state-root ' + str(self.state.root)],
                                stdout=subprocess.PIPE, text=True, check=False)
        return sorted(int(value) for value in listed.stdout.split())

    def stop_hosts(self):
        for pid in self.hosts():
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 15
        while self.hosts():
            self.assertLess(time.monotonic(), end, 'project host did not stop on SIGTERM')
            time.sleep(.02)

    def cli(self, command, *args, expected=0, project=None):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        env['CODEX_THREAD_ID'] = 'startup-primary'
        done = subprocess.run([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), command,
            '--state-root', str(self.state.root), '--project-root', str(project or self.project), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30, check=False)
        self.assertEqual(expected, done.returncode, (done.stdout + done.stderr).decode(errors='replace'))
        return json.loads(done.stdout)

    def test_cli_starts_one_host_reuses_it_and_recovers_after_it_stops(self):
        self.assertEqual([], self.hosts())
        opened = self.cli('learning-open-orchestration', '--request-id', 'startup-open')
        self.assertEqual(('active', 1, 'codex-host-v1'), (opened['status'], opened['sequence'], opened['client_version']))
        scope = supervisor_scope(self.paths, self.project_id)
        self.assertEqual(scope.as_data(), opened['scope'])
        first = self.hosts()
        self.assertEqual(1, len(first))
        self.assertTrue((history_directory(self.paths, self.project_id) / 'history.sqlite3').exists())
        target = ('--orchestration-handle', opened['orchestration_handle'])
        paused = self.cli('learning-pause-orchestration', *target, '--request-id', 'startup-pause')
        self.assertEqual(('paused', 2), (paused['status'], paused['sequence']))
        self.assertEqual(first, self.hosts())  # The live host is reused, not replaced.

        hint, capability = runtime_hint_path(self.paths, self.project), capability_path(self.paths, self.project, 'host')
        before = capability.read_bytes()
        self.stop_hosts()
        self.assertFalse(hint.exists())
        self.assertFalse(capability.exists())

        # The next command starts a new host; the paused orchestration is still there.
        resumed = self.cli('learning-resume-orchestration', *target, '--request-id', 'startup-resume')
        self.assertEqual(('active', 3), (resumed['status'], resumed['sequence']))
        second = self.hosts()
        self.assertEqual(1, len(second))
        self.assertNotEqual(first, second)
        self.assertNotEqual(before, capability.read_bytes())

    def test_S1_U8a_the_owner_command_starts_this_projects_service_once(self):
        self.assertEqual([], self.hosts())
        started = self.cli('learning-start-project-service')
        self.assertEqual((True, True), (started['ready'], started['serving']))
        first = self.hosts()
        self.assertEqual(1, len(first))
        self.assertTrue(runtime_hint_path(self.paths, self.project).exists())
        again = self.cli('learning-start-project-service')
        self.assertEqual((True, True), (again['ready'], again['serving']))
        self.assertEqual(first, self.hosts())  # an existing service is reused, not replaced

    def test_a_killed_host_is_replaced_and_an_unregistered_project_gets_none(self):
        opened = self.cli('learning-open-orchestration', '--request-id', 'crash-open')
        (pid,) = self.hosts()
        os.kill(pid, signal.SIGKILL)
        end = time.monotonic() + 10
        while self.hosts():
            self.assertLess(time.monotonic(), end)
            time.sleep(.02)
        # The dead host's hint, sockets and capability are still on disk.
        self.assertTrue(runtime_hint_path(self.paths, self.project).exists())
        status = self.cli('learning-orchestration-status', '--orchestration-handle', opened['orchestration_handle'])
        self.assertEqual(('active', 1), (status['status'], status['sequence']))
        self.assertEqual(1, len(self.hosts()))

        other = self.base / 'unregistered'
        (other / '.git').mkdir(parents=True)
        refused = self.cli('learning-open-orchestration', '--request-id', 'no-project', project=other, expected=2)
        self.assertEqual('orchestration_client_unavailable', refused['reason_code'])
        self.assertEqual(1, len(self.hosts()))
        self.assertFalse(runtime_hint_path(self.paths, other).exists())

    def test_a_host_stops_when_its_hint_is_withdrawn_or_it_has_been_idle(self):
        self.cli('learning-open-orchestration', '--request-id', 'withdraw-open')
        self.assertEqual(1, len(self.hosts()))
        runtime_hint_path(self.paths, self.project).unlink()
        end = time.monotonic() + 10
        while self.hosts():
            self.assertLess(time.monotonic(), end, 'host kept serving after its hint was removed')
            time.sleep(.05)

        reports = []
        idle = threading.Thread(target=lambda: reports.append(serve_project(
            self.state.root, self.project, stopped=lambda: False, idle_stop_ns=300_000_000)))
        idle.start()
        idle.join(15)
        self.assertFalse(idle.is_alive(), 'idle host did not stop')
        self.assertEqual([{'ready': True, 'stopped': True}], reports)
        self.assertFalse(runtime_hint_path(self.paths, self.project).exists())


if __name__ == '__main__':
    unittest.main()
