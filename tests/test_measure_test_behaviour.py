"""PLAN T8d: the test-behaviour measurement says what kind of process each start is, and sums CPU.

Unit stage: pure functions only, no process, socket or sleep.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_test_behaviour as measure  # noqa: E402

ORCHESTRATION = "/repo/packages/codex-agents/native-package/.codex/orchestration"


class StartKindTests(unittest.TestCase):
    """T8d-2: each start is one kind."""

    def test_a_product_script_with_the_products_flags_is_product(self):
        argv = ["/usr/bin/python3", "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", f"{ORCHESTRATION}/cli.py",
                "learning-status"]
        self.assertEqual("product", measure.start_kind(argv))
        self.assertEqual("product", measure.start_kind([sys.executable, f"{ORCHESTRATION}/collector_host.py"]))

    def test_a_repository_script_is_product(self):
        self.assertEqual("product", measure.start_kind([sys.executable, "-I", "/repo/scripts/attest.py", "x"]))

    def test_a_tests_module_is_a_fixture(self):
        self.assertEqual("fixture", measure.start_kind([sys.executable, "-B", "-m", "tests.dispatch_cli_fixture"]))

    def test_any_other_python_is_python(self):
        self.assertEqual("python", measure.start_kind([sys.executable, "-c", "print(1)"]))
        self.assertEqual("python", measure.start_kind(["python3", "/tmp/elsewhere/tool.py"]))

    def test_git_by_name_and_by_path_is_git(self):
        self.assertEqual("git", measure.start_kind(["git", "-C", "/x", "commit", "-qm", "m"]))
        self.assertEqual("git", measure.start_kind(["/usr/bin/git", "status"]))

    def test_the_wizard_and_the_installer_are_wizard(self):
        self.assertEqual("wizard", measure.start_kind(["bash", "/repo/setup.sh"]))
        self.assertEqual("wizard", measure.start_kind(["/repo/setup.sh"]))
        self.assertEqual("wizard", measure.start_kind([sys.executable, "/repo/scripts/install.py", "--apply"]))

    def test_a_shell_tool_is_other(self):
        self.assertEqual("other", measure.start_kind(["/bin/sh", "-c", "exit 0"]))
        self.assertEqual("other", measure.start_kind(["ssh-keygen", "-q"]))

    def test_a_string_command_is_split_like_a_shell(self):
        self.assertEqual("git", measure.start_kind("git status --short"))


class SummaryTests(unittest.TestCase):
    """T8d-3: totals and the modules that use the most CPU."""

    def rows(self):
        return {
            "root:test_a": {"tests": 2, "cpu_seconds": 5.0, "processes": 3,
                            "starts": {"product": 2, "fixture": 0, "python": 0, "git": 1, "wizard": 0, "other": 0}},
            "codex:tests.test_b": {"tests": 9, "cpu_seconds": 12.5, "processes": 1,
                                   "starts": {"product": 0, "fixture": 1, "python": 0, "git": 0, "wizard": 0,
                                              "other": 0}},
            "codex:tests.test_c": {"error": "no measurement"},
        }

    def test_the_summary_totals_cpu_and_starts_and_ranks_by_cpu(self):
        summary = measure.cpu_summary(self.rows())
        self.assertEqual(17.5, summary["cpu_seconds"])
        self.assertEqual({"product": 2, "fixture": 1, "python": 0, "git": 1, "wizard": 0, "other": 0},
                         summary["starts"])
        self.assertEqual(["codex:tests.test_b", "root:test_a"], [row["module"] for row in summary["top_cpu"]])
        self.assertEqual({"module": "codex:tests.test_b", "cpu_seconds": 12.5, "tests": 9,
                          "starts": self.rows()["codex:tests.test_b"]["starts"]}, summary["top_cpu"][0])

    def test_the_summary_keeps_at_most_twenty_modules(self):
        rows = {f"root:test_{n}": {"tests": 1, "cpu_seconds": float(n), "processes": 0,
                                   "starts": dict.fromkeys(measure.START_KINDS, 0)} for n in range(30)}
        top = measure.cpu_summary(rows)["top_cpu"]
        self.assertEqual(20, len(top))
        self.assertEqual("root:test_29", top[0]["module"])


if __name__ == "__main__":
    unittest.main()
