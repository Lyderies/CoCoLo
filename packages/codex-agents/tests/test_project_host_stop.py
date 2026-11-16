"""C4: an update stops an earlier version's project service that does not stop when asked.

local-routing, 2026-11-16: 0.8.22's service held the lease of a run it could not close, so it
counted as holding a run, never stopped when its hint was withdrawn, and never went idle; every
replacement gave up after 5 s, and only `kill` (SIGTERM) ended it. With the owner's approval the
replacement now stops exactly that process, as a signal does, when no child can be running.
"""
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import project_host_client

STAND_IN = textwrap.dedent("""
    import fcntl, os, signal, sys, time
    lock = os.open(sys.argv[sys.argv.index('--lock') + 1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    open(sys.argv[sys.argv.index('--ready') + 1], 'w').close()
    while True:
        time.sleep(0.05)
""")


class EarlierHostStopTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.base)]))
        self.project = self.base / 'project'
        self.project.mkdir()
        self.directory = self.base / 'host'
        self.directory.mkdir()

    def stand_in(self, project, runtime='agent-orchestration/abc/' + 'e' * 64):
        """A service of an earlier version: holds its host lock and ignores a withdrawn hint."""
        entry = self.base / runtime / '.codex' / 'orchestration' / 'project_host.py'
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(STAND_IN)
        ready = self.base / ('ready-' + str(len(list(self.base.glob('ready-*')))))
        process = subprocess.Popen([sys.executable, str(entry), '--state-root', str(self.base),
                                    '--project-root', str(project), '--lock', str(self.directory / 'host.lock'),
                                    '--ready', str(ready)])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        return process

    def test_C4_the_earlier_service_of_this_project_is_stopped(self):
        process = self.stand_in(self.project)
        self.assertIs(True, project_host_client._host_alive(self.directory))
        self.assertTrue(project_host_client.stop_earlier_host(self.project, self.directory, timeout=10))
        self.assertEqual(0, process.wait(5))  # It stopped as a signal stops it.
        self.assertIsNot(True, project_host_client._host_alive(self.directory))

    def test_C4_another_projects_service_is_never_signalled(self):
        other = self.base / 'other'
        other.mkdir()
        process = self.stand_in(other)
        self.assertFalse(project_host_client.stop_earlier_host(self.project, self.directory, timeout=1))
        self.assertIsNone(process.poll())

    def test_C4_this_versions_own_service_is_never_signalled(self):
        runtime = Path(project_host_client.__file__).resolve().parents[1]
        with mock.patch.object(project_host_client, 'RUNTIME_ORCHESTRATION', self.base / 'mine' / '.codex' / 'orchestration'):
            process = self.stand_in(self.project, runtime='mine')
            self.assertFalse(project_host_client.stop_earlier_host(self.project, self.directory, timeout=1))
        self.assertIsNone(process.poll())
        self.assertTrue(runtime.is_dir())


class ReplaceEarlierHostTests(unittest.TestCase):
    """The CLI stops it only after asking, and only when the account shows no child may run."""

    def replace(self, *, withdrawn, drained):
        import cli
        maintenance = mock.Mock()
        ledger = mock.Mock()
        ledger.drained.return_value = drained
        with mock.patch.object(cli, 'LearningState'), \
                mock.patch.object(cli.CollectorMaintenance, 'acquire', return_value=maintenance), \
                mock.patch.object(cli, 'other_version_host', return_value=Path('/srv/host')), \
                mock.patch.object(cli, 'LearningLedger', return_value=ledger), \
                mock.patch.object(cli, 'withdraw_project_host', return_value=withdrawn), \
                mock.patch.object(cli, 'stop_earlier_host', return_value=True) as stop:
            replaced = cli._replace_other_version_host(mock.Mock(state_root='/srv/state', project_root='/srv/project'))
        return replaced, stop

    def test_C4_a_service_that_does_not_stop_when_asked_is_stopped(self):
        replaced, stop = self.replace(withdrawn=False, drained=True)
        self.assertTrue(replaced)
        stop.assert_called_once()

    def test_C4_nothing_is_stopped_while_a_child_may_run(self):
        replaced, stop = self.replace(withdrawn=False, drained=False)
        self.assertFalse(replaced)
        stop.assert_not_called()

    def test_C4_a_service_that_stops_when_asked_is_left_alone(self):
        replaced, stop = self.replace(withdrawn=True, drained=True)
        self.assertTrue(replaced)
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
