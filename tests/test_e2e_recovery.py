"""T7 S2 (recovery, status, deletion): the checks module is strict by construction, without starting a World.

Guards: fault selection by explicit argument only; every subprocess names an environment (E2E_* stripped); the
fault's source rewrites apply to the product exactly once; the per-run capacity read is a bounded poll; a settled
run is never judged from missing data; the comparator accepts only S2 IDs; each ID has a check; the module never
passes `refuses=True` on a step whose success is required."""
import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("e2e_lifecycle", ROOT / "scripts" / "e2e_lifecycle.py")
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
import e2e_checks_recovery as rec  # noqa: E402
import e2e_support  # noqa: E402

SOURCE = (ROOT / "scripts" / "e2e_checks_recovery.py").read_text()
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"


class FaultTests(unittest.TestCase):
    def test_no_environment_variable_selects_the_fault(self):
        self.assertNotRegex(SOURCE, r"environ(\.get|\[)[^\n]*(E2E|FAULT)")
        self.assertNotRegex(SOURCE, r"getenv")
        self.assertIsNone(rec.FAULT)

    def test_every_fault_rewrite_applies_to_the_product_exactly_once(self):
        for name, patches in rec.FAULTS.items():
            for item in patches:
                path = ORCHESTRATION / (item["module"].replace(".", "/") + ".py")
                self.assertEqual(1, path.read_text().count(item["old"]), (name, item["module"]))

    def test_the_fault_names_the_runner_can_pick_are_the_modules_own(self):
        self.assertEqual({"recovery_broken"}, set(rec.FAULTS))

    def test_a_fault_run_must_turn_exactly_the_s2_ids_red(self):
        ids = rec.S2_IDS
        normal = {"requirements": {**{i: {"passed": i not in ("L8.3", "L15.1")} for i in ids}, "G3": {"passed": True}}}
        fault = {"requirements": {**{i: {"passed": False} for i in ids}, "G3": {"passed": True}}}
        self.assertTrue(rec.fault_isolated(normal, fault)["isolated"])
        outside = {"requirements": {**fault["requirements"], "G3": {"passed": False}}}
        self.assertFalse(rec.fault_isolated(normal, outside)["isolated"])
        one_green = {"requirements": {**fault["requirements"], "L8.2": {"passed": True}}}
        self.assertFalse(rec.fault_isolated(normal, one_green)["isolated"])
        missing = {"requirements": {i: v for i, v in fault["requirements"].items() if i != "L14.1"}}
        self.assertFalse(rec.fault_isolated(normal, missing)["isolated"])
        self.assertEqual(set(), set(rec.FAULT_ALSO_RED))

    def test_the_runner_takes_the_fault_only_as_an_argument(self):
        text = (ROOT / "scripts" / "e2e_lifecycle.py").read_text()
        self.assertIn('"--fault"', text)
        self.assertNotRegex(text, r"environ[^\n]*FAULT")

    def test_every_subprocess_names_its_environment(self):
        tree = ast.parse(SOURCE)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen", "check_output")
                 and getattr(node.func.value, "id", "") == "subprocess"]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("env", {keyword.arg for keyword in call.keywords}, ast.get_source_segment(SOURCE, call)[:80])


class ReadTests(unittest.TestCase):
    def test_a_missing_or_null_reply_is_never_a_settled_run(self):
        for reply in (None, {}, {"ready": True}, {"ready": True, "terminal_observed": True},
                      {"reason_code": "run_lost", "terminal_state": "abandoned"},
                      {"reason_code": "run_lost", "terminal_state": "abandoned", "capacity_state": "held"}):
            self.assertFalse(rec.settled(reply), reply)
        self.assertTrue(rec.settled({"ready": True, "terminal_observed": True, "cleanup_confirmed": True}))
        self.assertTrue(rec.settled({"reason_code": "run_lost", "terminal_state": "abandoned",
                                     "capacity_state": "released"}))

    def test_plain_needs_words_a_next_step_and_never_the_authorization_refusal(self):
        good = {"error": "dispatch unavailable", "next_step": "run learning-run-status for this run",
                "reason_code": "dispatch_unavailable"}
        self.assertTrue(rec.plain(good, ""))
        for bad in (None, {}, {**good, "error": None}, {**good, "next_step": ""}, {**good, "reason_code": None},
                    {**good, "reason_code": "dispatch_not_authorized"}):
            self.assertFalse(rec.plain(bad, ""), bad)
        self.assertFalse(rec.plain(good, "Traceback (most recent call last)"))
        self.assertFalse(rec.plain(good, "dispatch_not_authorized"))

    def test_the_released_read_is_a_bounded_poll_not_one_immediate_read(self):
        reads = []

        def fake(view, run, *, timeout=60):
            reads.append(1)
            return {"state": "held"} if len(reads) < 3 else {"state": "released"}
        original, rec.capacity_read, rec.time.sleep = rec.capacity_read, fake, lambda seconds: None
        try:
            self.assertEqual("released", rec.wait_released(None, None, seconds=30)["state"])
            self.assertEqual(3, len(reads))
            reads.clear()
            never = lambda view, run, *, timeout=60: {"state": "held"}  # noqa: E731
            rec.capacity_read = never
            clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
            original_monotonic, rec.time.monotonic = rec.time.monotonic, lambda: next(clock)
            try:
                self.assertEqual("held", rec.wait_released(None, None, seconds=5)["state"])
            finally:
                rec.time.monotonic = original_monotonic
        finally:
            rec.capacity_read = original
            rec.time.sleep = __import__("time").sleep

    def test_a_capacity_read_that_times_out_is_captured_and_fixed(self):
        text = SOURCE[SOURCE.index("def step("):SOURCE.index("class Ctl")]
        self.assertIn("TimeoutExpired", text)
        self.assertIn("_capture", text)
        self.assertIn("timed_out", text)
        self.assertIn('"capacity_read_timed_out"', SOURCE)

    def test_the_boot_patches_are_never_a_billing_constant(self):
        for patches in rec.FAULTS.values():
            for item in patches:
                self.assertNotRegex(item["old"] + item["new"], r"BILLING|REUSE|COOLDOWN|reuse|cooldown")


class RequirementTests(unittest.TestCase):
    def test_each_s2_id_has_a_check_that_names_it(self):
        mapping = {}
        for name, (ids, _function) in lifecycle.CHECKS.items():
            for requirement in ids:
                mapping.setdefault(requirement, []).append(name)
        for requirement in rec.S2_IDS:
            self.assertTrue([n for n in mapping.get(requirement, []) if lifecycle.CHECK_GROUPS[n].startswith("rec_")],
                            requirement)
        document = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())
        listed = {item["id"]: item["checks"] for item in document["requirements"]}
        for requirement in rec.S2_IDS:
            self.assertEqual(sorted(mapping[requirement]), sorted(listed[requirement]), requirement)

    def test_refusals_are_marked_only_on_the_steps_that_are_expected_to_refuse(self):
        marked = re.findall(r"refuses=True", SOURCE)
        self.assertLessEqual(len(marked), 24)
        # The dispatches whose success is required never pass the flag.
        for line in SOURCE.splitlines():
            if "ctl.dispatch(" in line:
                self.assertNotIn("refuses=True", line)
        self.assertIn("ctl.dispatch_in_background(run, refuses=True)  # the stimulus", SOURCE)
        # The collector scene's dispatches are judged by G1 (the product must not fail them).
        collector = SOURCE[SOURCE.index("def collector_scene"):]
        self.assertNotIn("dispatch_in_background(run, refuses=True)", collector)
        self.assertNotIn("dispatch_in_background(other, refuses=True)", collector)

    def test_the_stand_in_python_boot_only_applies_listed_rewrites(self):
        self.assertIn("patches.json", e2e_support.BOOT)


if __name__ == "__main__":
    unittest.main()
