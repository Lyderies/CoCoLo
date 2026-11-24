"""T7: the lifecycle e2e script is strict by construction (PLAN-0.4.0.md T7).

A check that asserts nothing fails; a check that raises anything fails; a requirement with no check
fails; a selected flow's requirement left undecided fails; and the report carries no private value.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("e2e_lifecycle", ROOT / "scripts" / "e2e_lifecycle.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


def doc(*requirements):
    return {"flows": {"L1": "x"}, "requirements": [
        {"id": i, "flow": "L1", "source": "s", "statement": "t", "checks": list(checks)} for i, checks in requirements]}


class StrictnessTests(unittest.TestCase):
    def run_with(self, document, checks):
        with mock.patch.object(e2e, "requirements", return_value=document), \
                mock.patch.dict(e2e.CHECKS, checks, clear=True):
            return e2e.run_checks({"L1"})

    def test_a_check_that_asserts_nothing_fails(self):
        report = self.run_with(doc(("L1.1", ["quiet"])), {"quiet": (("L1.1",), lambda phase: None)})
        self.assertFalse(report["passed"])
        self.assertEqual("check_asserted_nothing", report["checks"][0]["failure"])

    def test_a_check_that_raises_fails_and_is_never_skipped(self):
        def missing_tool(phase):
            raise FileNotFoundError("tool")
        report = self.run_with(doc(("L1.1", ["needs_tool"])), {"needs_tool": (("L1.1",), missing_tool)})
        self.assertFalse(report["passed"])
        self.assertEqual("check_error: FileNotFoundError", report["checks"][0]["failure"])

    def test_a_requirement_with_no_check_or_a_missing_check_fails(self):
        report = self.run_with(doc(("L1.1", []), ("L1.2", ["gone"])), {})
        self.assertFalse(report["passed"])
        self.assertEqual("no_check", report["requirements"]["L1.1"]["failure"])
        self.assertEqual("check_missing", report["checks"][0]["failure"])

    def test_a_passing_check_decides_its_requirement(self):
        def fine(phase):
            phase.expect(True, "it holds")
            phase.reason("claim_busy")
        report = self.run_with(doc(("L1.1", ["fine"])), {"fine": (("L1.1",), fine)})
        self.assertTrue(report["passed"])
        self.assertEqual(["claim_busy"], report["checks"][0]["reasons"])

    def test_only_reason_codes_and_fixed_text_reach_the_report(self):
        def leaky(phase):
            phase.reason("/srv/private/path")
        report = self.run_with(doc(("L1.1", ["leaky"])), {"leaky": (("L1.1",), leaky)})
        self.assertFalse(report["passed"])
        self.assertNotIn("/srv/private/path", json.dumps(report))

    def test_private_values_in_a_report_are_named_not_shown(self):
        found = e2e.private_values_in('{"x": "/srv/tmp/cocolo-e2e-1 SHA256:' + "A" * 43 + '"}', ["/srv/tmp"])
        self.assertEqual(["temporary_home_path", "key_or_fingerprint"], found)


SCRIPTS = ROOT / "scripts"


def e2e_sources():
    """Every module of the lifecycle test: the script, its harness and each checks module."""
    return [SCRIPTS / "e2e_lifecycle.py", SCRIPTS / "e2e_support.py", SCRIPTS / "e2e_core.py",
            *sorted(SCRIPTS.glob("e2e_checks_*.py"))]


class InstalledCliTests(unittest.TestCase):
    """Decision 128d: every check runs the project's INSTALLED cli.py, never the source tree's copy.

    The source copy has no install manifest, so a check run through it cannot see an install-only defect
    (0.8.54: an installed CLI routed not_checked while every source-tree check passed)."""

    def test_no_e2e_module_names_the_source_tree_cli(self):
        import ast
        for path in e2e_sources():
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                with self.subTest(module=path.name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, {"CLI", "ORCHESTRATION"}, "the source-tree CLI path is named")
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, {"CLI", "ORCHESTRATION"}, "the source-tree CLI path is named")
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertFalse("native-package" in node.value and "cli.py" in node.value,
                                         "a literal path to the source tree's cli.py")
                    if isinstance(node, ast.keyword) and path.name != "e2e_support.py":
                        self.assertNotEqual("program", node.arg, "a check chose which CLI to run")

    def test_the_harness_runs_the_installed_cli_by_default_and_refuses_any_other(self):
        import e2e_support
        installed = Path("/proj/.codex/orchestration/cli.py")
        world = mock.Mock(project=Path("/proj"), shim=Path("/shim"), env={}, base=Path("/base"),
                          outputs=[], steps=[], installed_cli=installed)
        done = mock.Mock(stdout="{}", stderr="", returncode=0)
        with mock.patch.object(e2e_support.subprocess, "run", return_value=done) as run:
            e2e_support.World.cli(world, "learning-status")
        self.assertIn(str(installed), run.call_args.args[0])
        source = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "cli.py"
        with mock.patch.object(e2e_support.subprocess, "run") as run:
            with self.assertRaises(e2e_support.HarnessError):
                e2e_support.World.cli(world, "learning-status", program=source)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
