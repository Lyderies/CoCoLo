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

from codex_agents.client_runtime import read_runtime_hint, runtime_hint_path
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.project_host_client import _host_alive, project_host_serving
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

    @staticmethod
    def env():
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        env['CODEX_THREAD_ID'] = 'startup-primary'
        return env

    def cli_command(self, command, *args, project=None):
        return [sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), command,
                '--state-root', str(self.state.root), '--project-root', str(project or self.project), *args]

    def cli(self, command, *args, expected=0, project=None):
        done = subprocess.run(self.cli_command(command, *args, project=project),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), timeout=30, check=False)
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

    def _kill_host_and_start_one_held_before_it_withdraws_the_dead_hint(self):
        """CI2: a host is killed and is gone (its lock released); a new host then starts in this process and
        is held just after it took the project's host lock, before it withdrew the dead host's hint.

        On a loaded runner that moment lasts (CI run 44804716252, and 44673087746 before it): the hint
        on disk is still the dead host's, with its sockets, while the host lock is the new host's.
        Returns the orchestration's handle and the release event; the host serves once released,
        until the test ends."""
        from unittest import mock
        handle = self.cli('learning-open-orchestration', '--request-id', 'ci2-open')['orchestration_handle']
        (pid,) = self.hosts()
        directory = read_runtime_hint(self.paths, self.project)[1].parent
        os.kill(pid, signal.SIGKILL)
        end = time.monotonic() + 10
        while self.hosts() or _host_alive(directory) is not False:  # gone, by the kernel's lock too
            self.assertLess(time.monotonic(), end)
            time.sleep(.02)
        hint = runtime_hint_path(self.paths, self.project)
        blocked, release, original, held = threading.Event(), threading.Event(), Path.unlink, []

        def unlink(path, *args, **kwargs):
            if not held and threading.current_thread().name == 'ci2-host' and str(path) == str(hint):
                held.append(True)
                blocked.set()
                release.wait(20)
            return original(path, *args, **kwargs)
        patch = mock.patch.object(Path, 'unlink', unlink)
        patch.start()
        self.addCleanup(patch.stop)
        stop = threading.Event()
        host = threading.Thread(name='ci2-host', daemon=True, target=lambda: serve_project(
            self.state.root, self.project, stopped=stop.is_set, idle_stop_ns=3600 * 10**9))
        self.addCleanup(host.join, 15)
        self.addCleanup(stop.set)
        self.addCleanup(release.set)
        host.start()
        self.assertTrue(blocked.wait(15), 'the new host did not reach the dead hint')
        self.assertIs(True, _host_alive(directory))  # the new host's lock
        self.assertTrue(hint.exists())               # the dead host's hint
        return handle, release

    def test_CI2_a_starting_host_does_not_make_the_dead_hosts_hint_look_served(self):
        """CI2: a host that holds the project's lock but has not yet withdrawn the dead host's hint
        does not serve that hint: a client that took the pair for a live service read the dead
        host's hint and sockets and failed with orchestration_client_unavailable."""
        handle, release = self._kill_host_and_start_one_held_before_it_withdraws_the_dead_hint()
        self.assertFalse(project_host_serving(self.state.root, self.project),
                         "the dead host's hint was taken for the starting host's service")
        release.set()
        end = time.monotonic() + 10
        while not project_host_serving(self.state.root, self.project):
            self.assertLess(time.monotonic(), end, 'the new host never published its own hint')
            time.sleep(.02)
        status = self.cli('learning-orchestration-status', '--orchestration-handle', handle)
        self.assertEqual(('active', 1), (status['status'], status['sequence']))

    def test_CI2_a_command_during_a_hosts_start_waits_for_it_and_succeeds(self):
        """CI2: a command that comes while another start's host is between taking the project's lock
        and serving waits for that host and is answered by it; it never reads the dead host's hint,
        and a host it starts itself that finds the project taken does not end its wait."""
        handle, release = self._kill_host_and_start_one_held_before_it_withdraws_the_dead_hint()
        command = subprocess.Popen(self.cli_command('learning-orchestration-status', '--orchestration-handle', handle),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        try:
            # The host stays held while the command is under way (4 s), or until the command gave up.
            try:
                command.wait(4)
            except subprocess.TimeoutExpired:
                pass
            release.set()
            out, err = command.communicate(timeout=30)
        finally:
            if command.poll() is None:
                command.kill()
        self.assertEqual(0, command.returncode, (out + err).decode(errors='replace'))
        status = json.loads(out)
        self.assertEqual(('active', 1), (status['status'], status['sequence']))

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


    def test_O10_D13_a_faulted_lane_writer_stops_the_service_and_the_next_one_settles(self):
        """O10 D13: a service whose result-persistence writer faulted stops itself at once, by its normal
        stop path, with an exit record naming the cause. It settles nothing itself; the next start does.

        Before D13 it served on for ever while holding (`holding` counted the lane's jobs), so its
        lost-run settlement never began: bounded here so the test fails, not hangs."""
        from unittest import mock
        from codex_agents import project_driver, project_history, service_host

        class WriterFault(BaseException):
            """Escapes the writer's per-request handling, so the lane faults."""

        def fault(history, *args, **kwargs):
            raise WriterFault('the writer stopped')
        settled, original = [], project_driver.RunDriver._settle_lost

        def settle_lost(driver, own):
            settled.append(own)
            return original(driver, own)
        stop, reports = threading.Event(), []
        self.addCleanup(stop.set)  # Never left serving if the service does not stop by itself.
        with mock.patch.object(project_history.ProjectHistory, 'expire_results', fault), \
                mock.patch.object(project_driver.RunDriver, '_settle_lost', settle_lost), \
                mock.patch.object(service_host.ProjectServiceHost, 'holding', lambda host: True):
            served = threading.Thread(daemon=True, target=lambda: reports.append(serve_project(
                self.state.root, self.project, stopped=stop.is_set, idle_stop_ns=3600 * 10**9)))
            served.start()
            served.join(15)
            alive = served.is_alive()
            stop.set()
            served.join(15)
        self.assertFalse(alive, 'a service whose writer faulted kept serving')
        self.assertEqual([{'ready': True, 'stopped': True, 'reason_code': 'writer_fault'}], reports)
        self.assertNotIn(True, settled, 'the faulted service settled its own runs itself')
        self.assertFalse(runtime_hint_path(self.paths, self.project).exists())
        self.assertFalse(capability_path(self.paths, self.project, 'host').exists())

        # The next start takes the project's locks, settles the stopped instance's runs, and idles out.
        settled.clear()
        with mock.patch.object(project_driver.RunDriver, '_settle_lost', settle_lost):
            again = threading.Thread(daemon=True, target=lambda: reports.append(serve_project(
                self.state.root, self.project, stopped=lambda: False,
                idle_stop_ns=1_500_000_000)))
            again.start()
            again.join(15)
        self.assertFalse(again.is_alive(), 'the next service did not start and idle out')
        self.assertEqual({'ready': True, 'stopped': True}, reports[-1])
        self.assertIn(False, settled, "the next service swept the stopped instance's runs")


if __name__ == '__main__':
    unittest.main()
