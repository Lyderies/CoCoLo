"""Every test module has a stage, and a unit test that starts a process, socket or sleep fails.

The stages decide what CI runs where: unit tests on every pull request on Ubuntu
and macOS, integration tests on every pull request on Ubuntu, end-to-end tests
(the setup wizard and the installer as programs) on main, tags and nightly.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import parallel_tests  # noqa: E402

STAGES = ROOT / "scripts" / "test-stages.json"


class TestStageTests(unittest.TestCase):
    def test_every_test_module_has_exactly_one_known_stage(self):
        stages = json.loads(STAGES.read_text())
        self.assertTrue(set(stages.values()) <= {"unit", "integration", "e2e"})
        missing = [f"{suite}:{module}" for suite in parallel_tests.SUITES
                   for module in parallel_tests.discover(suite) if f"{suite}:{module}" not in stages]
        self.assertEqual([], missing, "give each new test module a stage in scripts/test-stages.json "
                                      "(scripts/measure_test_behaviour.py proposes one)")

    def run_unit(self, body):
        with tempfile.TemporaryDirectory() as folder:
            tests = Path(folder) / "tests"
            tests.mkdir()
            (tests / "test_probe.py").write_text(textwrap.dedent(f"""
                import unittest
                class Probe(unittest.TestCase):
                    def test_it(self):
                        {body}
                """))
            completed = subprocess.run(
                [sys.executable, "-B", "-c", parallel_tests._RUNNER, "test_probe"], cwd=folder,
                env={**os.environ, "COCOLO_TEST_STAGE": "unit"}, capture_output=True, text=True, check=False)
            return completed.returncode, completed.stdout + completed.stderr

    def test_a_unit_test_that_starts_a_process_opens_a_socket_or_sleeps_fails(self):
        for body in ("import subprocess; subprocess.run(['true'])",
                     "import socket; s = socket.socket(socket.AF_UNIX); s.bind('/nonexistent/unit.sock')",
                     "import time; time.sleep(0.01)"):
            with self.subTest(body=body):
                code, output = self.run_unit(body)
                self.assertNotEqual(0, code, output)
                self.assertIn("unit test", output)
        code, output = self.run_unit("self.assertEqual(2, 1 + 1)")
        self.assertEqual(0, code, output)

    def test_stage_selects_only_that_stages_modules(self):
        stages = json.loads(STAGES.read_text())
        selected = parallel_tests.select(
            [(1.0, "test_test_stages", ["test_test_stages"]), (1.0, "test_setup_sh#3", ["x"])], "root", {"e2e"})
        self.assertEqual(["test_setup_sh#3"], [unit[1] for unit in selected])
        self.assertEqual("e2e", stages["root:test_setup_sh"])


class ShardTests(unittest.TestCase):
    """PLAN T4a: one of N shards, deterministic and balanced by recorded time."""

    def queue(self):
        return sorted(((float(seconds), "codex", f"tests.m{index}", [f"tests.m{index}"])
                       for index, seconds in enumerate([90, 70, 60, 40, 30, 30, 20, 10, 5, 1])), reverse=True)

    def test_every_unit_is_in_exactly_one_shard_and_the_split_is_stable(self):
        queue = self.queue()
        shards = [parallel_tests.shard(queue, index, 3) for index in (1, 2, 3)]
        names = [unit[2] for part in shards for unit in part]
        self.assertEqual(sorted(unit[2] for unit in queue), sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(shards, [parallel_tests.shard(list(reversed(queue)), index, 3) for index in (1, 2, 3)])
        self.assertEqual(queue, parallel_tests.shard(queue, 1, 1))

    def test_no_shard_has_more_than_its_share_plus_the_longest_unit(self):
        queue = self.queue()
        total, longest = sum(unit[0] for unit in queue), max(unit[0] for unit in queue)
        for index in (1, 2, 3):
            self.assertLessEqual(sum(unit[0] for unit in parallel_tests.shard(queue, index, 3)), total / 3 + longest)

    def test_shard_is_refused_unless_k_of_n(self):
        for bad in ("0/3", "4/3", "1/0", "x", "1/17"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parallel_tests.main(["--shard", bad, "--stage", "unit"])


if __name__ == "__main__":
    unittest.main()
