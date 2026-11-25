"""T7 S5 (update safety and rollback): the checks module is strict by construction, without starting a World.

Guards: the fault is chosen by explicit argument only; its source rewrites (and the forced part-way failure) apply to the
product exactly once; the module never stops, kills or signals a process (an update needing a hand stop must fail, not be
helped); every expected refusal is marked and every other update is judged by G1; the pure judges (previous version,
what changed, who was replaced, what a plain refusal says) are exact; each S5 ID has its own check; the comparator
accepts only S5 IDs."""
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
import e2e_checks_update_safety as us  # noqa: E402

SOURCE = (ROOT / "scripts" / "e2e_checks_update_safety.py").read_text()
PACKAGES = ROOT / "packages"
ORCHESTRATION = PACKAGES / "codex-agents" / "native-package" / ".codex" / "orchestration"
CLAUDE_SOURCE = PACKAGES / "claude-agents" / "src"
REGISTRY = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]


def source_of(module: str) -> str:
    base = CLAUDE_SOURCE if module.startswith("claude_agents") else ORCHESTRATION
    return (base / (module.replace(".", "/") + ".py")).read_text()


class FaultTests(unittest.TestCase):
    def test_no_environment_variable_selects_the_fault(self):
        self.assertNotRegex(SOURCE, r"environ(\.get|\[)[^\n]*(E2E|FAULT)")
        self.assertNotRegex(SOURCE, r"getenv")
        self.assertIsNone(us.FAULT)

    def test_every_rewrite_applies_to_the_product_exactly_once(self):
        for name, patches in {**us.FAULTS, "forced_failure": us.FORCED_FAILURE}.items():
            for item in patches:
                self.assertEqual(1, source_of(item["module"]).count(item["old"]), (name, item["module"]))

    def test_the_fault_names_the_runner_can_pick_are_the_modules_own(self):
        self.assertEqual({"update_handover_broken"}, set(us.FAULTS))
        self.assertIn("update_handover_broken", lifecycle.FAULTS)
        self.assertIn(us, lifecycle.FAULT_MODULES)

    def test_the_forced_failure_is_not_a_fault_and_is_only_added_for_one_update(self):
        self.assertNotIn("forced_failure", us.FAULTS)
        us.FAULT = None
        self.assertEqual([], us.fault_patches())
        self.assertEqual(us.FORCED_FAILURE, us.fault_patches(forced=True))
        us.FAULT = "update_handover_broken"
        try:
            self.assertEqual(us.FAULTS["update_handover_broken"] + us.FORCED_FAILURE, us.fault_patches(forced=True))
        finally:
            us.FAULT = None

    def test_the_runner_takes_the_fault_only_as_an_argument(self):
        text = (ROOT / "scripts" / "e2e_lifecycle.py").read_text()
        self.assertIn('"--fault"', text)
        self.assertNotRegex(text, r"environ[^\n]*FAULT")

    def test_a_fault_run_must_turn_exactly_the_s5_ids_red(self):
        ids = us.S5_IDS
        normal = {"flows": ["L11", "L12"], "requirements": {**{i: {"passed": i != "L12.3"} for i in ids},
                                                          "G3": {"passed": True}}}
        fault = {"flows": ["L11", "L12"], "requirements": {**{i: {"passed": False} for i in ids},
                                                          "G3": {"passed": True}}}
        self.assertTrue(us.fault_isolated(normal, fault, expected_ids=set(normal["requirements"]))["isolated"])
        outside = {"flows": ["L11", "L12"], "requirements": {**fault["requirements"], "G3": {"passed": False}}}
        self.assertFalse(us.fault_isolated(normal, outside, expected_ids=set(normal["requirements"]))["isolated"])
        one_green = {"flows": ["L11", "L12"], "requirements": {**fault["requirements"], "L12.4": {"passed": True}}}
        self.assertFalse(us.fault_isolated(normal, one_green, expected_ids=set(normal["requirements"]))["isolated"])
        missing = {"flows": ["L11", "L12"], "requirements": {i: v for i, v in fault["requirements"].items() if i != "G6"}}
        self.assertFalse(us.fault_isolated(normal, missing, expected_ids=set(normal["requirements"]))["isolated"])
        self.assertEqual({"G1"}, set(us.FAULT_ALSO_RED["update_handover_broken"]))

    def test_every_subprocess_names_its_environment(self):
        tree = ast.parse(SOURCE)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen", "check_output")
                 and getattr(node.func.value, "id", "") == "subprocess"]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("env", {keyword.arg for keyword in call.keywords}, ast.get_source_segment(SOURCE, call)[:80])


class NoHandStopTests(unittest.TestCase):
    def test_the_module_never_stops_or_signals_a_process(self):
        """An update that needs a hand stop must fail its check: the harness never helps it."""
        for word in ("os.kill", "killpg", "signal.", "pkill", "killall", "terminate(", ".kill(", "shutdown", "stop_processes",
                     "withdraw", "kill_hard"):
            self.assertNotIn(word, SOURCE, word)

    def test_only_an_update_marked_as_expected_to_refuse_passes_refuses(self):
        tree = ast.parse(SOURCE)
        refusing = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "do_update":
                flag = {k.arg: k.value for k in node.keywords}.get("refuses")
                refusing.append(isinstance(flag, ast.Constant) and flag.value is True)
        self.assertEqual(3, sum(refusing))  # the child refusal, the uncommitted refusal and the forced failure

    def test_update_run_defaults_to_an_update_that_must_succeed(self):
        function = next(n for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef) and n.name == "update_run")
        defaults = {a.arg: d for a, d in zip(function.args.kwonlyargs, function.args.kw_defaults)}
        self.assertFalse(defaults["refuses"].value)


class JudgeTests(unittest.TestCase):
    def test_previous_version_is_the_release_before(self):
        self.assertEqual("0.8.59", us.previous_version("0.8.60"))
        self.assertEqual("0.8.0", us.previous_version("0.8.1"))
        self.assertEqual("0.7.99", us.previous_version("0.8.0"))
        with self.assertRaises(ValueError):
            us.previous_version("0.0.0")
        with self.assertRaises(ValueError):
            us.previous_version("0.8")

    def snapshot(self, **changes):
        base = {"files": {"project/a": "1", "home/b": "2"}, "head": "h1", "commits": "2", "dirty": "",
                "records": {"status": "x"}, "ledger": {"records": 3}}
        base.update(changes)
        return base

    def test_what_an_update_changed_is_named_exactly(self):
        before = self.snapshot()
        self.assertEqual([], us.changed_anything(before, self.snapshot()))
        self.assertEqual(["files"], us.changed_anything(before, self.snapshot(files={"project/a": "9", "home/b": "2"})))
        self.assertEqual(["files"], us.changed_anything(before, self.snapshot(files={"project/a": "1"})))
        self.assertEqual(["commits", "head"], us.changed_anything(before, self.snapshot(head="h2", commits="3")))
        self.assertEqual(["dirty"], us.changed_anything(before, self.snapshot(dirty=" M x\n")))
        self.assertEqual([], us.changed_anything(self.snapshot(dirty=" M x\n"), self.snapshot(dirty=" M x\n")))
        self.assertEqual(["records"], us.changed_anything(before, self.snapshot(records={"status": "y"})))
        self.assertEqual(["tables"], us.changed_anything(before, self.snapshot(ledger={"records": 4})))

    def test_a_collector_replaced_by_the_update_needs_the_old_gone_and_one_new(self):
        alive = {1: False, 2: True}
        self.assertTrue(us.replaced([1], [2], lambda pid: alive[pid]))
        self.assertFalse(us.replaced([1], [1], lambda pid: True))  # the same process
        self.assertFalse(us.replaced([1], [], lambda pid: False))  # nothing runs now
        self.assertFalse(us.replaced([1], [2, 3], lambda pid: pid != 1))  # two collectors
        self.assertFalse(us.replaced([1], [2], lambda pid: True))  # the old one is still there
        self.assertFalse(us.replaced([], [2], lambda pid: True))  # there was none to replace

    def test_a_refusal_is_plain_when_it_names_the_reason_and_says_nothing_was_changed(self):
        child = "FAIL Collector registration failed (exit 2)\n Reason: a child run is active; nothing was changed, try again when none is running\n"
        self.assertTrue(us.child_refusal_is_plain(child))
        self.assertFalse(us.child_refusal_is_plain("The update stopped at step 5 (Collector): registration failed."))
        self.assertFalse(us.child_refusal_is_plain(""))
        dirty = "The update stopped (managed_files_uncommitted): Managed paths have uncommitted changes. Nothing was changed."
        self.assertTrue(us.dirty_refusal_is_plain(dirty))
        self.assertFalse(us.dirty_refusal_is_plain("The update stopped (managed_files_uncommitted)."))
        self.assertFalse(us.dirty_refusal_is_plain("Nothing was changed."))

    def test_the_version_before_is_taken_from_the_release_not_assumed(self):
        text = (ROOT / "packages" / "codex-agents" / "native-package" / "VERSION").read_text().strip()
        self.assertRegex(us.previous_version(text), r"^\d+\.\d+\.\d+$")
        self.assertNotEqual(text, us.previous_version(text))


class RegistryTests(unittest.TestCase):
    def test_each_s5_id_has_its_own_check_in_the_module(self):
        declared = {name: ids for name, (ids, _f) in lifecycle.CHECKS.items() if name in us.CHECK_NAMES}
        for requirement in us.S5_IDS:
            self.assertTrue([n for n, ids in declared.items() if requirement in ids], requirement)
        row = {item["id"]: item["checks"] for item in REGISTRY}
        for requirement in us.S5_IDS:
            self.assertTrue(row[requirement], requirement)
            self.assertTrue(set(row[requirement]) <= set(declared), requirement)

    def test_the_module_declares_only_s5_ids(self):
        for name in us.CHECK_NAMES:
            ids, _ = lifecycle.CHECKS[name]
            self.assertTrue(set(ids) <= set(us.S5_IDS), (name, ids))

    def test_the_scene_groups_are_known_to_the_runner_for_ordering(self):
        for name in us.CHECK_NAMES:
            self.assertIn(lifecycle.CHECK_GROUPS[name], lifecycle.GROUP_SECONDS, name)

    def test_the_previous_build_is_a_copy_with_only_its_version_changed(self):
        self.assertIn("VERSION", SOURCE)
        self.assertRegex(SOURCE, r"ls-files")  # copied from the tracked files, not from a Git history that may be shallow

    def test_the_module_reads_no_value_from_the_command_that_made_it(self):
        """The refusal text is judged for plainness, but what changed is read from the snapshots, the process table and
        the collector's own status (a different path than the update's screen)."""
        for word in ("ps", "snapshot", "learning-status"):
            self.assertIn(word, SOURCE)


if __name__ == "__main__":
    unittest.main()
