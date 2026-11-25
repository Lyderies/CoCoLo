"""S7 (S1-U9): a newly started collector answers its first request within one second of its sockets being up.

The real collector host is started the way the product starts it (`learning-serve`, detached, fixed environment).
The clock starts when the collector's socket and the account service's endpoint both exist, and stops when the
first answer arrives; the process start and the ledger open are not in it (the collector's own phase timings,
`collector_startup_ms`, report those). ANALYSIS-0.4.0-e0b-v2 measured 9 to 12 ms for the plain answer on a
large ledger with a warm file cache; the 1 s bound is the requirement (S1-U9), which leaves two orders of
magnitude for a loaded machine. The wait for each socket is a wait for the file, bounded, never a fixed sleep.
"""
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP  # noqa: F401  (bootstraps the import path)
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_framework import requirements
from tests.learning_support import generate_test_owner_key
from codex_agents.collector_client import CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.collector_paths import CollectorPaths
from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.service_enrollment import account_endpoint_path

FIRST_RESPONSE_SECONDS = 1.0  # S1-U9
SOCKETS_UP_SECONDS = 30  # a bound on the wait for the sockets (process start included), not part of the budget


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'account signature workers need the reviewed macOS sandbox')
class CollectorFirstResponseTests(unittest.TestCase):
    __why_not_scenario__ = "The real collector host process and its sockets; no model launch is involved."

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP, prefix='s7fr')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.root = self.base / 'project'
        (self.root / '.git').mkdir(parents=True)
        ledger = LearningLedger(self.state)
        try:
            ledger.register_project(self.root)
        finally:
            ledger.close()  # The collector process owns the ledger from here.
        self.paths = CollectorPaths(self.state.root)
        self.account = account_endpoint_path(self.paths)

    def start_collector(self):
        """The product's own start: `learning-serve` through the checked CLI, detached, with a fixed environment."""
        process = subprocess.Popen(
            [sys.executable, *PYTHON_ISOLATION_FLAGS, str(ORCHESTRATION_ROOT / 'cli.py'), 'learning-serve',
             '--state-root', str(self.paths.root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd='/', close_fds=True,
            env={'PATH': os.defpath, 'xcrun_nocache': '1'}, start_new_session=True)

        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(30)
                except subprocess.TimeoutExpired:
                    process.kill()  # this exact process, started here; never a pattern
                    process.wait(10)
        self.addCleanup(stop)
        return process

    def wait_for_sockets(self, process):
        """Return the monotonic time at which the collector's socket and the account endpoint both exist."""
        end = time.monotonic() + SOCKETS_UP_SECONDS
        while not (self.paths.socket.exists() and self.account.exists()):
            self.assertIsNone(process.poll(), 'the collector ended before its sockets were up')
            self.assertLess(time.monotonic(), end, 'the collector sockets did not appear')
            time.sleep(0.001)  # waiting for a file another process makes; nothing here proves timing
        return time.monotonic()

    def first_answer(self, client, *, project=None):
        """Ask until one answer arrives (a refused connection before the listener is up is retried); the answer."""
        end = time.monotonic() + 10
        while True:
            try:
                return client.health(project)
            except CollectorError as error:
                if error.code == 'ledger_busy' or time.monotonic() >= end:
                    raise
                time.sleep(0.001)

    @requirements('S1-U9')
    def test_S1_U9_first_answer_comes_within_one_second_of_the_sockets_being_up(self):
        process = self.start_collector()
        up = self.wait_for_sockets(process)
        health = self.first_answer(CollectorClient(self.paths.socket, timeout=5))
        elapsed = time.monotonic() - up
        self.assertTrue(health['ok'], health)
        self.assertLess(elapsed, FIRST_RESPONSE_SECONDS, f'first health took {elapsed:.3f} s')

    @requirements('S1-U9')
    def test_S1_U9_first_project_answer_comes_within_one_second_of_the_sockets_being_up(self):
        process = self.start_collector()
        up = self.wait_for_sockets(process)
        health = self.first_answer(CollectorClient(self.paths.socket, timeout=5), project=self.root)
        elapsed = time.monotonic() - up
        self.assertTrue(health['ok'], health)
        self.assertNotIn('ledger_busy', health)
        self.assertEqual('active', health['ingestion']['registered_project_state'])
        self.assertLess(elapsed, FIRST_RESPONSE_SECONDS, f'first project health took {elapsed:.3f} s')


if __name__ == '__main__':
    unittest.main()
