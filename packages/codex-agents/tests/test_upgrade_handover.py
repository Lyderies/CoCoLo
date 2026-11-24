"""UPG: setup of this build hands over from a RUNNING earlier collector (owner-seen failure, 0.8.55).

Real processes: the earlier collector is a git archive of an earlier release (0.8.55 8d4a7ce3, 0.8.58
1c293c44) whose own `learning-register` starts it on a temporary state root; this build's
`learning-register` must stop it and register. UPG-5 checks this build's own collector the same way.
Needs git and those commits in this repository.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codex_agents import collector_client
from codex_agents.collector_client import CollectorClient, canonical_json, exchange_frame
from codex_agents.collector_contracts import COLLECTOR_MUTATING_OPERATIONS

HERE = Path(__file__).resolve().parent
ORCHESTRATION = HERE.parent / "native-package" / ".codex" / "orchestration"
CLI_RELATIVE = "packages/codex-agents/native-package/.codex/orchestration/cli.py"


class Fixture:
    def prepare(self):
        self.base = Path(tempfile.mkdtemp(prefix="upg", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = self.base / "s"
        self.state.mkdir(mode=0o700)
        self.project = self.base / "p"
        self.project.mkdir()
        self.socket = self.state / "collector.sock"
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("CODEX_")},
                    "TMPDIR": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}
        self.addCleanup(self.stop_collector)

    def run_cli(self, cli):
        return subprocess.run(
            [sys.executable, "-B", str(cli), "learning-register",
             "--state-root", str(self.state), "--project-root", str(self.project)],
            env=self.env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def frame(self, request):
        return json.loads(exchange_frame(self.socket, canonical_json(request), timeout=3.0,
                                         request_limit=4096, response_limit=16_384))

    def stop_collector(self):
        """Whatever collector is left: the plain stop every release accepts."""
        try:
            self.frame({"protocol": 1, "op": "shutdown"})
        except Exception:
            pass

    def reported_version(self):
        return self.frame({"protocol": 1, "op": "health"})["runtime_version"]


class EarlierCollectorMixin(Fixture):
    OLD_COMMIT = ""
    OLD_VERSION = ""

    def setUp(self):
        top = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        if subprocess.run(["git", "-C", top, "cat-file", "-e", self.OLD_COMMIT + "^{commit}"]).returncode != 0:
            self.skipTest("the earlier release commit is not in this repository")
        self.prepare()
        archive = subprocess.run(["git", "-C", top, "archive", self.OLD_COMMIT, "packages/codex-agents/native-package"],
                                 capture_output=True, check=True).stdout
        old = self.base / "old"
        old.mkdir()
        subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
        # The earlier release's own setup registers the project and starts its collector, as it did live.
        started = self.run_cli(old / CLI_RELATIVE)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)

    def test_registering_stops_a_running_earlier_collector_and_completes(self):
        self.assertEqual(self.OLD_VERSION, self.reported_version())
        done = self.run_cli(ORCHESTRATION / "cli.py")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report["ready"], report)
        self.assertTrue(report["project_id"].startswith("prj_"))
        self.assertNotEqual(self.OLD_VERSION, self.reported_version())


class Earlier0855Tests(EarlierCollectorMixin, unittest.TestCase):
    """UPG-1: 0.8.55 rejects the stop's runtime_digest key (operation_invalid)."""
    OLD_COMMIT = "8d4a7ce3"
    OLD_VERSION = "0.8.55"


class Earlier0858Tests(EarlierCollectorMixin, unittest.TestCase):
    """UPG-6: 0.8.58 refuses the stop of another runtime (runtime_upgrade_required); its own digest is echoed once."""
    OLD_COMMIT = "1c293c44"
    OLD_VERSION = "0.8.58"


class SameBuildCollectorTests(Fixture, unittest.TestCase):
    """UPG-2 and UPG-5 against this build's own collector, a real process."""
    FOREIGN = "ab" * 32

    def setUp(self):
        self.prepare()
        done = self.run_cli(ORCHESTRATION / "cli.py")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def alive(self):
        try:
            self.reported_version()
            return True
        except Exception:
            return False

    def wait_stopped(self):
        end = time.monotonic() + 15
        while self.alive():
            self.assertLess(time.monotonic(), end, "the collector did not stop")
            time.sleep(.1)

    def test_UPG_5_other_mutating_operations_keep_the_exact_digest_gate(self):
        for request in ({"op": "shutdown", "runtime_digest": self.FOREIGN},
                        {"op": "shutdown"},
                        {"op": "register", "project_root": str(self.project), "runtime_digest": self.FOREIGN},
                        {"op": "learning_moved_if_empty", "project_root": str(self.project),
                         "runtime_digest": self.FOREIGN},
                        {"op": "ingest", "runtime_digest": self.FOREIGN}):
            self.assertEqual("runtime_upgrade_required", self.frame({"protocol": 1, **request}).get("code"), request)
        # A malformed digest on the drained stop is refused too, and nothing stops.
        self.assertFalse(self.frame({"protocol": 1, "op": "shutdown_if_drained", "runtime_digest": "not-a-digest"})["ok"])
        self.assertTrue(self.alive())

    def test_UPG_5_the_drained_stop_from_another_runtime_stops_it(self):
        self.assertEqual({"ok": True}, self.frame(
            {"protocol": 1, "op": "shutdown_if_drained", "runtime_digest": self.FOREIGN}))
        self.wait_stopped()

    def test_UPG_5_the_drained_stop_without_a_digest_stops_it(self):
        self.assertEqual({"ok": True}, self.frame({"protocol": 1, "op": "shutdown_if_drained"}))
        self.wait_stopped()

    def test_UPG_2_this_builds_client_still_sends_its_digest_with_every_mutating_operation(self):
        for operation in ("shutdown", "shutdown_if_drained", "register", "learning_moved_if_empty"):
            self.assertIn(operation, COLLECTOR_MUTATING_OPERATIONS)
        sent = []
        original = collector_client.exchange_frame
        collector_client.exchange_frame = lambda path, payload, **options: sent.append(json.loads(payload)) or b'{"ok":true}'
        try:
            CollectorClient(self.socket).shutdown(only_if_drained=True)
        finally:
            collector_client.exchange_frame = original
        self.assertEqual(collector_client.COLLECTOR_RUNTIME_DIGEST, sent[0]["runtime_digest"])


if __name__ == "__main__":
    unittest.main()
