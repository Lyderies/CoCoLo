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


class SceneCleanupTests(unittest.TestCase):
    """Finding 2: every scene lives in the one e2e_core._SCENES, so close_scenes() stops and removes every World."""

    def test_no_checks_module_rebinds_the_shared_scenes(self):
        import ast
        for path in sorted(SCRIPTS.glob("e2e_checks_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                targets = (node.targets if isinstance(node, ast.Assign) else
                           [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
                for target in targets:
                    with self.subTest(module=path.name, line=node.lineno):
                        self.assertFalse(isinstance(target, ast.Name) and target.id == "_SCENES",
                                         "a checks module rebinds _SCENES, so close_scenes() misses its worlds")

    def test_every_checks_module_shares_the_core_scenes(self):
        import e2e_core
        for name in ("e2e_checks_setup", "e2e_checks_owner_key", "e2e_checks_billing", "e2e_checks_routing",
                     "e2e_checks_run"):
            with self.subTest(module=name):
                self.assertIs(e2e_core._SCENES, sys.modules[name]._SCENES)

    def test_close_scenes_closes_a_setup_scene(self):
        import e2e_core
        import e2e_checks_setup
        world = mock.MagicMock()
        e2e_checks_setup._SCENES["fresh"] = mock.Mock(world=world)
        try:
            e2e_core.close_scenes()
        finally:
            e2e_checks_setup._SCENES.clear()
            e2e_core._SCENES.clear()
        world.__exit__.assert_called_once()


class DigestTests(unittest.TestCase):
    """Finding 5: two missing digests are not the same digest."""

    def test_missing_digests_never_match(self):
        import e2e_checks_run
        self.assertFalse(e2e_checks_run.same_digest(None, None))
        self.assertFalse(e2e_checks_run.same_digest("", ""))
        self.assertFalse(e2e_checks_run.same_digest("a" * 64, "b" * 64))
        self.assertTrue(e2e_checks_run.same_digest("a" * 64, "a" * 64))

    def test_g4_fails_when_the_stored_qa_and_the_result_have_no_digest(self):
        import e2e_checks_run
        world = mock.Mock()
        world.history_qa.return_value = [{"accepted": True, "deterministic_pass": True, "security_pass": True}]
        world.history_records.side_effect = lambda kind: (
            [{"host_outcome_digest": "d", "run_id": "r"}] if kind == "host_result_admission" else
            [{"run_id": "r", "accepted": True, "status": "accepted"}] if kind == "outcome" else
            [{"run_id": "r"}])
        scene = mock.Mock(world=world, setup=mock.Mock(code=0), digests={"": "d"},
                          steps={"report": (0, {"qa": {"passed": 1, "failed": 0, "attention_required": False}})})
        phase = e2e.Phase("g4", Path("/nonexistent"))
        with self.assertRaises(e2e.CheckFailed):
            e2e_checks_run.qa_accepted(phase, scene)


FINGERPRINT = "SHA256:" + "Q" * 43
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAGYmNyeXB0\n-----END OPENSSH PRIVATE KEY-----"


class CaptureRuleTests(unittest.TestCase):
    """Findings 6 (G8) and G1: the rules are judged as each output is captured, in every World."""

    PRIVATE = {"owner_key_fingerprint": FINGERPRINT, "owner_key_material": "AAAAC3NzaC1lZDI1NTE5AAAAIpublicblob",
               "owner_private_key_0": "b3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAGYmNyeXB0",
               "passphrase": "pass-phrase-e2e", "temporary_home_path": "/Users/x/.cache/e2e-1/h"}

    def breaks(self, text, kind, code=0, refuses=False, timed_out=False):
        import e2e_support
        return e2e_support.capture_breaks(text, kind=kind, private=self.PRIVATE, code=code, refuses=refuses,
                                          timed_out=timed_out)

    def test_a_saved_output_may_carry_no_private_value(self):
        self.assertEqual(["g8_output_leaks_owner_key_fingerprint"], self.breaks("key " + FINGERPRINT, "output"))
        self.assertEqual(["g8_output_leaks_temporary_home_path"], self.breaks("/Users/x/.cache/e2e-1/h/x", "output"))
        self.assertEqual([], self.breaks('{"ok": true}', "output"))

    def test_an_owner_screen_may_show_the_fingerprint_and_paths_but_no_secret(self):
        self.assertEqual([], self.breaks("Fingerprint " + FINGERPRINT + " in /Users/x/.cache/e2e-1/h", "screen"))
        self.assertEqual(["g8_screen_leaks_passphrase"], self.breaks("typed pass-phrase-e2e", "screen"))
        self.assertIn("g8_screen_leaks_private_key_material", self.breaks(PRIVATE_KEY, "screen"))
        self.assertIn("g8_screen_leaks_owner_private_key", self.breaks(PRIVATE_KEY, "screen"))

    def test_private_key_material_is_found_without_its_header_too(self):
        self.assertEqual(["g8_screen_leaks_owner_private_key"],
                         self.breaks("b3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAGYmNyeXB0", "screen"))
        self.assertIn("g8_output_leaks_private_key_material",
                      self.breaks("-----BEGIN RSA PRIVATE KEY-----", "output"))

    def test_g1_an_unexpected_exit_a_timeout_or_a_bare_reason_breaks_the_rule(self):
        self.assertEqual(["g1_unexpected_exit"], self.breaks("", "output", code=2))
        self.assertEqual([], self.breaks("", "output", code=2, refuses=True))
        self.assertEqual(["g1_step_timed_out"], self.breaks("", "screen", code=0, timed_out=True))
        self.assertEqual(["g1_bare_run_unavailable"], self.breaks('{"reason_code": "run_unavailable"}', "output"))

    def test_a_check_whose_capture_broke_a_rule_fails_and_decides_g8(self):
        import e2e_support

        def leaking(phase):
            phase.expect(True, "it holds")
            e2e_support.CAPTURE_BREAKS.append("g8_output_leaks_passphrase")
        document = {"flows": {"L1": "x"}, "requirements": [
            {"id": "L1.1", "flow": "L1", "source": "s", "statement": "t", "checks": ["leaking"]},
            {"id": "G8", "flow": "all", "source": "s", "statement": "t", "checks": ["fine"]}]}

        def fine(phase):
            phase.expect(True, "it holds")
        with mock.patch.object(e2e, "requirements", return_value=document), \
                mock.patch.dict(e2e.CHECKS, {"leaking": (("L1.1",), leaking), "fine": (("G8",), fine)}, clear=True):
            report = e2e.run_checks({"L1"})
        entry = [c for c in report["checks"] if c["check"] == "leaking"][0]
        self.assertFalse(entry["passed"])
        self.assertEqual("capture_rule_broken", entry["failure"])
        self.assertIn("g8_output_leaks_passphrase", entry["reasons"])
        self.assertFalse(report["requirements"]["G8"]["passed"])


class WorkerResultTests(unittest.TestCase):
    """Findings 4 and 7: a --jobs worker's entries are taken only whole and only from a clean exit."""

    def entry(self, name, passed=True, reasons=()):
        return {"check": name, "requirements": ["L1.1"], "passed": passed, "seconds": 1.0, "reasons": list(reasons),
                "failure": None}

    def test_a_leaking_worker_entry_is_replaced_whole(self):
        leaky = [self.entry("a", reasons=["leaks_x"]) | {"extra": "/Users/x/.cache/e2e-1"}]
        redacted = e2e.redact_entries(leaky, ["a"])
        self.assertEqual([{"check": "a", "requirements": [], "passed": False, "seconds": 0.0, "reasons": [],
                           "failure": "private_values_in_report"}], redacted)

    def test_a_duplicate_entry_or_a_failed_exit_fails_every_check_of_the_group(self):
        names = ["a", "b"]
        good = [self.entry("a"), self.entry("b")]
        self.assertEqual(good, e2e.accept_worker(good, names, 0))
        for entries, code in (([self.entry("a"), self.entry("a"), self.entry("b")], 0), (good, 1),
                              ([self.entry("a")], 0), ("nonsense", 0)):
            with self.subTest(code=code):
                accepted = e2e.accept_worker(entries, names, code)
                self.assertEqual(names, [e["check"] for e in accepted])
                self.assertFalse(any(e["passed"] for e in accepted))


class MetadataTests(unittest.TestCase):
    """Finding 1: lifecycle check links are validated against tests/e2e/requirements.json, apart from the
    shared catalogue, so the required CI jobs' metadata validation passes."""

    def test_the_real_lifecycle_links_are_valid(self):
        import parallel_tests
        self.assertEqual([], parallel_tests.lifecycle_link_errors())

    def test_a_planted_unlinked_check_is_an_error(self):
        import parallel_tests
        rows = [{"id": "L1.1", "checks": ["one"]}, {"id": "L1.2", "checks": ["gone"]}]
        errors = parallel_tests.lifecycle_link_errors({"one": ["L1.1"], "two": ["L1.1"], "three": ["ZZ"]}, rows)
        self.assertIn("lifecycle:two: L1.1 does not list its check", errors)
        self.assertIn("lifecycle:three: unknown lifecycle requirement 'ZZ'", errors)
        self.assertIn("L1.2: missing or unlinked lifecycle check gone", errors)

    def test_the_shared_catalogue_matches_its_sources(self):
        import parallel_tests
        import test_framework
        catalogue = test_framework.decode_json((SCRIPTS / "test-requirements.json").read_text())
        self.assertEqual([], test_framework.validate_sources(ROOT, catalogue))
        self.assertTrue(parallel_tests)


if __name__ == "__main__":
    unittest.main()
