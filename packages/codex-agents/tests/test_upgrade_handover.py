"""UPG: setup of this build hands over from a RUNNING earlier collector (owner-seen failure, 0.8.55).

Real processes: the earlier collector is a git archive of 0.8.55 (8d4a7ce3) started as its own
process on a temporary state root; this build's `learning-register` must stop it (drained stop in
the shape it accepts) and register. Needs git and the archive commit in this repository.
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

from codex_agents.collector_client import CollectorClient, CollectorError
from codex_agents.collector_client import canonical_json, exchange_frame
from codex_agents.collector_contracts import COLLECTOR_MUTATING_OPERATIONS

OLD_COMMIT = "8d4a7ce3"
HERE = Path(__file__).resolve().parent
ORCHESTRATION = HERE.parent / "native-package" / ".codex" / "orchestration"


class UpgradeHandoverTests(unittest.TestCase):
    def setUp(self):
        top = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        if subprocess.run(["git", "-C", top, "cat-file", "-e", OLD_COMMIT + "^{commit}"]).returncode != 0:
            self.skipTest("the earlier release commit is not in this repository")
        self.base = Path(tempfile.mkdtemp(prefix="upg", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        archive = subprocess.run(["git", "-C", top, "archive", OLD_COMMIT, "packages/codex-agents/native-package"],
                                 capture_output=True, check=True).stdout
        old = self.base / "old"
        old.mkdir()
        subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
        self.old_cli = old / "packages/codex-agents/native-package/.codex/orchestration/cli.py"
        self.state = self.base / "s"
        self.state.mkdir(mode=0o700)
        self.project = self.base / "p"
        self.project.mkdir()
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("CODEX_")},
                    "TMPDIR": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}
        # The earlier release's own setup registers the project and starts its collector, as it did live.
        started = subprocess.run(
            [sys.executable, "-B", str(self.old_cli), "learning-register",
             "--state-root", str(self.state), "--project-root", str(self.project)],
            env=self.env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop_collector)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)
        self.socket = self.state / "collector.sock"

    def stop_collector(self):
        """Whatever collector is left: a plain frame, which every release accepts."""
        for operation in ("shutdown",):
            try:
                exchange_frame(self.socket, canonical_json({"protocol": 1, "op": operation}), timeout=2.0,
                               request_limit=4096, response_limit=16_384)
            except Exception:
                pass

    def reported_version(self):
        raw = exchange_frame(self.socket, canonical_json({"protocol": 1, "op": "health"}), timeout=2.0,
                             request_limit=4096, response_limit=16_384)
        return json.loads(raw)["runtime_version"]

    def register(self):
        return subprocess.run(
            [sys.executable, "-B", str(ORCHESTRATION / "cli.py"), "learning-register",
             "--state-root", str(self.state), "--project-root", str(self.project)],
            env=self.env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def test_UPG_1_registering_stops_a_running_earlier_collector_and_completes(self):
        self.assertEqual("0.8.55", self.reported_version())
        done = self.register()
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report["ready"], report)
        self.assertTrue(report["project_id"].startswith("prj_"))
        self.assertNotEqual("0.8.55", self.reported_version())

    def test_UPG_2_same_build_mutating_operations_still_carry_the_runtime_digest(self):
        for operation in ("shutdown", "shutdown_if_drained", "register", "learning_moved_if_empty"):
            self.assertIn(operation, COLLECTOR_MUTATING_OPERATIONS)
        from codex_agents import collector_client
        sent = []

        def capture(path, payload, **options):
            sent.append(json.loads(payload))
            return b'{"ok":true}'
        original = collector_client.exchange_frame
        collector_client.exchange_frame = capture
        try:
            CollectorClient(self.socket).shutdown(only_if_drained=True)
        finally:
            collector_client.exchange_frame = original
        self.assertEqual(collector_client.COLLECTOR_RUNTIME_DIGEST, sent[0]["runtime_digest"])


if __name__ == "__main__":
    unittest.main()
