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

# The S2 scenes and the building blocks they share with S1 (e2e_checks_shared.py: step, Ctl, capacity_read; batch3).
SOURCE = (ROOT / "scripts" / "e2e_checks_shared.py").read_text() + (ROOT / "scripts" / "e2e_checks_recovery.py").read_text()
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
        self.assertEqual({"G1"}, set(rec.FAULT_ALSO_RED))  # decision 191 (was empty)

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
        # Decision 180: the second dispatch's refusal is expected only with a replaced service (judged again once known).
        self.assertIn("dispatch_in_background(other, refuses=True)", collector)
        self.assertIn("dispatch_g1_breaks(", collector)

    def test_the_stand_in_python_boot_only_applies_listed_rewrites(self):
        self.assertIn("patches.json", e2e_support.BOOT)


L8_2_STATEMENT = ("After the collector is killed, the project service survives: the run either completes normally with "
                  "capacity released once the collector returns, or settles run_lost; never in limbo, never counted twice.")


def _function_source(name: str) -> str:
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node)
    raise AssertionError(name)


class FixRoundTests(unittest.TestCase):
    """Fix round 1 (Codex review t7s2, NOT-READY): each guard below was written red before its fix."""

    def test_world_cli_timeout_is_captured_with_partial_output_never_raised_or_printed(self):
        captured, steps, outputs = [], [], []
        stub = type("Stub", (), {})()
        stub.installed_cli, stub.shim = Path("/x/cli.py"), Path("/x/python")
        stub.project, stub.base, stub.env = Path("/x/secret-project"), Path("/x"), {}
        stub.steps, stub.outputs = steps, outputs
        stub._capture = lambda text, **kw: captured.append((text, kw))
        real_run, real_realpath = e2e_support.subprocess.run, e2e_support.os.path.realpath
        import subprocess

        def timing_out(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1, output=b"partial", stderr=b"more")
        e2e_support.subprocess.run = timing_out
        e2e_support.os.path.realpath = lambda path: str(path)
        try:
            code, reply, text = e2e_support.World.cli(stub, "learning-status", timeout=1)
        finally:
            e2e_support.subprocess.run, e2e_support.os.path.realpath = real_run, real_realpath
        self.assertEqual(-1, code)
        self.assertIsNone(reply)
        self.assertEqual("partialmore", text)
        self.assertTrue(captured and captured[0][1]["timed_out"] is True)
        self.assertTrue(steps[0]["timed_out"] and not steps[0]["refuses"])
        self.assertNotIn("secret-project", text)

    def test_refusal_is_expected_only_at_named_call_sites(self):
        found = set()
        tree = ast.parse(SOURCE)
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [n for n in ast.walk(function) if isinstance(n, ast.Call)]:
                flagged = [k for k in call.keywords if k.arg in ("refuses", "expect_refusal")
                           and isinstance(k.value, ast.Constant) and (k.value.value is True or isinstance(k.value.value, str))]
                if not flagged:
                    continue
                label = next((a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)),
                             getattr(call.func, "attr", None) or getattr(call.func, "id", "?"))
                found.add((function.name, label))
        self.assertEqual({("service_scene", "dispatch_in_background"),
                          ("collector_scene", "dispatch_in_background"),
                          ("wait_running", "status"),
                          ("lost_and_released", "learning-run-status"),
                          ("service_scene", "learning-run-status"),
                          ("service_scene", "phase_view"), ("service_scene", "status")}, found)  # the nested poll, seen from its scene

    def test_an_expected_refusal_names_its_reason_and_a_different_reason_breaks_g1(self):
        # decision 191: refuses may be an expected reason_code text; another reason is a G1 break
        from e2e_support import capture_breaks
        kw = dict(kind="output", private={}, timed_out=False)
        self.assertEqual([], capture_breaks('{"reason_code": "run_lost"}', code=2, refuses="run_lost", **kw))
        self.assertEqual(["g1_refusal_reason_unexpected"],
                         capture_breaks('{"reason_code": "other"}', code=2, refuses="run_lost", **kw))
        self.assertEqual(["g1_unexpected_exit"], capture_breaks('{}', code=2, refuses=False, **kw))

    def test_only_the_run_status_of_an_unstarted_run_may_refuse_in_phase_view(self):
        text = _function_source("phase_view")
        self.assertIn("expect_refusal", text)
        self.assertNotRegex(text, r"learning-orchestration-report[^\n]*\n?[^\n]*refuses=True")

    def test_l8_2_terminal_is_exactly_run_lost_or_completed_with_capacity_released(self):
        self.assertEqual("run_lost", rec.persisted_terminal(("abandoned", "service_lost", 1)))
        self.assertEqual("completed", rec.persisted_terminal(("terminal", None, 1), "completed"))
        for row in (None, ("pending", None, 0), ("pending", None, 1), ("abandoned", "service_lost", 0),
                    ("abandoned", "other_reason", 1), ("terminal", None, 0), ("terminal", None, None),
                    ("released", None, 1), ("abandoned", None, 1)):
            self.assertIsNone(rec.persisted_terminal(row), row)

    def test_l8_2_statement_is_reworded_in_both_requirement_files(self):
        document = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())
        statement = [i["statement"] for i in document["requirements"] if i["id"] == "L8.2"]
        self.assertEqual([L8_2_STATEMENT], statement)
        other = json.loads((ROOT / "scripts" / "test-requirements.json").read_text())
        found = other.get("requirements", other)
        self.assertEqual(L8_2_STATEMENT, found["L8.2"]["statement"])

    def test_l8_2_check_judges_the_persisted_terminal_and_service_survival(self):
        text = _function_source("l8_2_killed_collector_run_settles_once_back_slot_freed_nothing_twice")
        self.assertIn("persisted_terminal", text)
        self.assertIn("service_during", text)
        self.assertNotIn("settled(", text)

    def test_l8_3_check_uses_the_current_runs_slot_and_the_replaced_service(self):
        text = _function_source("l8_3_second_projects_setup_during_a_dispatch_leaves_no_run_in_limbo")
        self.assertNotIn("scene.slot_before", text)
        self.assertIn("other_running", text)
        self.assertIn("other_slot_before", text)
        self.assertIn("service_replaced", text)
        self.assertNotIn('"collector_kept"', text)

    def test_service_replaced_needs_a_new_pid_and_the_old_one_gone(self):
        gone = lambda pid: False  # noqa: E731
        self.assertTrue(rec.service_replaced([10], [11], alive_fn=gone))
        self.assertFalse(rec.service_replaced([10], [10], alive_fn=gone))
        self.assertFalse(rec.service_replaced([10], [11], alive_fn=lambda pid: True))
        self.assertFalse(rec.service_replaced([10], [], alive_fn=gone))
        self.assertFalse(rec.service_replaced([], [11], alive_fn=gone))
        self.assertFalse(rec.service_replaced([10, 12], [11], alive_fn=gone))

    def test_wait_running_result_is_required_to_show_a_running_child(self):
        self.assertTrue(rec.is_running({"liveness": {"last_activity_category": "x"}, "terminal_observed": False}))
        for reply in (None, {}, {"liveness": {"last_activity_category": "x"}},
                      {"liveness": {"last_activity_category": "x"}, "terminal_observed": True},
                      {"liveness": {}, "terminal_observed": False}):
            self.assertFalse(rec.is_running(reply), reply)

    def test_a_close_is_ok_only_with_an_explicit_ready_true(self):
        self.assertTrue(rec.closed_ok((0, {"ready": True}, "")))
        for result in ((0, {}, ""), (0, None, ""), (0, {"ready": None}, ""), (0, {"ready": False}, ""),
                       (2, {"ready": True}, "")):
            self.assertFalse(rec.closed_ok(result), result)
        self.assertNotIn("is not False", _function_source("l8_3_replaced_service_leaves_no_run_in_limbo_cli_says_so_plainly_orchestration_closes"))

    def test_l8_1_reads_the_next_childs_completion_from_persisted_history(self):
        text = _function_source("l8_1_killed_service_next_service_settles_run_lost_slot_freed_nothing_twice")
        self.assertNotIn("nxt.reply", text)
        self.assertIn("next_binding", text)

    def test_expect_each_judges_every_assertion_before_failing(self):
        phase = rec.Phase("x", Path("."))
        with self.assertRaises(rec.CheckFailed):
            rec.expect_each(phase, [(False, "first one"), (True, "second one"), (False, "third one")])
        self.assertEqual(3, phase.expectations)
        phase = rec.Phase("x", Path("."))
        rec.expect_each(phase, [(True, "a"), (True, "b")])
        self.assertEqual(2, phase.expectations)

    def test_l14_1_judges_a_phase_after_the_close_attempt_and_every_migration_status(self):
        text = _function_source("l14_1_status_commands_give_the_right_values_at_each_phase_and_none_is_private")
        self.assertIn('phases["closed"]', text)
        self.assertIn("expect_each", text)
        self.assertIn('"migration"', text)
        self.assertIn('phases["closed"]', _function_source("service_scene"))

    def test_a_tombstone_is_a_deleted_registration_with_no_root_fields(self):
        gone = {"project_id": "p", "canonical_root": None, "root_digest": None, "status": "deleted",
                "created_at_ms": 5, "deleted_at_ms": 9}
        self.assertTrue(rec.is_tombstone(gone, "p"))
        for bad in (None, {**gone, "canonical_root": "/x"}, {**gone, "root_digest": "ab"}, {**gone, "status": "active"},
                    {**gone, "deleted_at_ms": None}, {**gone, "project_id": "q"}, {**gone, "deleted_at_ms": "9"}):
            self.assertFalse(rec.is_tombstone(bad, "p"), bad)

    def test_retained_observations_hold_no_project_path_and_no_run_identifier(self):
        paths = ["/base/second", "/private/base/second"]
        clean = [{"request_key": "k", "provider": "codex", "run_id": None, "handle_digest": None, "phase": None,
                  "references_json": '{"a": 1}'}]
        self.assertEqual([], rec.observation_problems(clean, paths))
        self.assertTrue(rec.observation_problems([{**clean[0], "references_json": '{"p": "/base/second/x"}'}], paths))
        self.assertTrue(rec.observation_problems([{**clean[0], "run_id": "r1"}], paths))
        self.assertTrue(rec.observation_problems([{**clean[0], "handle_digest": "d"}], paths))

    def test_survivor_identity_changes_when_a_record_is_substituted_at_the_same_count(self):
        one = [("r1", "d1"), ("r2", "d2")]
        self.assertEqual(rec.identity_digest(one), rec.identity_digest(list(reversed(one))))
        self.assertNotEqual(rec.identity_digest(one), rec.identity_digest([("r1", "d1"), ("r3", "d2")]))
        self.assertNotEqual(rec.identity_digest(one), rec.identity_digest([("r1", "d1"), ("r2", "dX")]))

    def test_l15_1_checks_service_identity_scrub_and_survivor_ids(self):
        text = _function_source("l15_1_delete_project_removes_records_and_service_leaves_the_other_untouched")
        for needed in ("is_tombstone", "observation_problems", "deleted_service_final", "kept_identity"):
            self.assertIn(needed, text)
        self.assertNotIn("KEPT_BY_DESIGN", text)


class FinalFixTests(unittest.TestCase):
    """Decision 180: the one final targeted fix (L8.3 against G1, L8.2/L8.1 outcome, L14.1 phase guard)."""

    def test_l8_3_second_dispatch_g1_depends_on_the_observed_service(self):
        replaced = rec.dispatch_g1_breaks(True, 1)
        self.assertNotIn("g1_unexpected_exit", replaced)  # an EXPECTED refusal is not a G1 failure
        self.assertNotIn("g1_unexpected_exit", rec.dispatch_g1_breaks(True, 0))
        self.assertNotIn("g1_unexpected_exit", rec.dispatch_g1_breaks(False, 0))
        self.assertIn("g1_unexpected_exit", rec.dispatch_g1_breaks(False, 1))  # untouched stays expected-success
        self.assertIn("g1_unexpected_exit", rec.dispatch_g1_breaks(False, 2))

    def test_l8_3_scene_passes_the_dispatch_through_that_judge(self):
        collector = SOURCE[SOURCE.index("def collector_scene"):]
        self.assertIn("dispatch_in_background(other, refuses=True)", collector)
        self.assertIn("dispatch_g1_breaks(", collector)
        self.assertIn("CAPTURE_BREAKS", collector)

    def test_a_completed_terminal_needs_the_persisted_outcome_read_explicitly(self):
        done = ("terminal", None, 1)
        self.assertEqual("completed", rec.persisted_terminal(done, "completed"))
        for outcome in (None, "", "running", "unknown", "failed", "COMPLETED"):
            self.assertIsNone(rec.persisted_terminal(done, outcome), outcome)
        self.assertIsNone(rec.persisted_terminal(done))  # never inferred from the released status
        self.assertEqual("run_lost", rec.persisted_terminal(("abandoned", "service_lost", 1)))

    def test_the_run_status_must_agree_with_an_explicit_terminal(self):
        lost = {"reason_code": "run_lost"}
        ended = {"ready": True, "terminal_observed": True}
        self.assertTrue(rec.status_agrees("run_lost", lost))
        self.assertTrue(rec.status_agrees("completed", ended))
        for terminal, status in (("completed", {}), ("completed", None), ("completed", {"terminal_observed": False}),
                                 ("completed", {"ready": True}), ("completed", lost), ("run_lost", {}),
                                 ("run_lost", ended), (None, ended), (None, lost)):
            self.assertFalse(rec.status_agrees(terminal, status), (terminal, status))

    def test_l8_1_and_l8_2_read_the_outcome_from_persisted_history(self):
        for name in ("l8_1_killed_service_next_service_settles_run_lost_slot_freed_nothing_twice",
                     "l8_2_killed_collector_run_settles_once_back_slot_freed_nothing_twice"):
            text = _function_source(name)
            self.assertIn("status_agrees", text) if "l8_2" in name else None
            self.assertTrue("outcome" in text, name)
        self.assertIn("persisted_outcome", SOURCE[SOURCE.index("def service_scene"):])
        self.assertIn("persisted_outcome", SOURCE[SOURCE.index("def collector_scene"):])

    def test_l14_1_fails_when_any_one_migration_phase_is_left_out(self):
        collected = {name: {"migration": {}} for name in rec.MIGRATION_PHASES}
        collected["idle"] = {"lifecycle": {}}
        self.assertTrue(rec.migration_phases_judged(collected, rec.MIGRATION_PHASES))
        for left_out in rec.MIGRATION_PHASES:
            judged = tuple(n for n in rec.MIGRATION_PHASES if n != left_out)
            self.assertFalse(rec.migration_phases_judged(collected, judged), left_out)
        self.assertFalse(rec.migration_phases_judged(collected, rec.MIGRATION_PHASES + ("idle",)))
        self.assertIn("migration_phases_judged", _function_source(
            "l14_1_status_commands_give_the_right_values_at_each_phase_and_none_is_private"))
        self.assertNotIn("every phase", (ROOT / "docs" / "requirements" / "t7" / "assertions.md").read_text())

    def test_the_assertions_file_states_decision_178_as_decided(self):
        text = (ROOT / "docs" / "requirements" / "t7" / "assertions.md").read_text()
        self.assertIn("decision 178", text)
        self.assertNotIn("PRIMARY DECISION NEEDED", text)


if __name__ == "__main__":
    unittest.main()
