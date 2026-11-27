"""T7 S1 (scripts/e2e_checks_run_control.py): the test-only source rewrites the run-control scenes ask the stand-in
Python for must each match the installed product's source exactly once, or the scene would fail loudly at import."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_checks_run_control as control  # noqa: E402

ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "codex_agents"


class PatchTests(unittest.TestCase):
    def source(self, module: str) -> str:
        return (ORCHESTRATION / (module.split(".")[-1] + ".py")).read_text()

    def test_every_rewrite_matches_the_product_source_exactly_once(self):
        patches = [control.liveness_patch(*control.SILENCE), *[patch for patches in control.FAULTS.values() for patch in patches],
                   {"module": "codex_agents.account_billing_admission", "old": "PROBE_REUSE_MS = 300_000"},
                   {"module": "codex_agents.account_billing_admission", "old": "PROBE_COOLDOWN_MS = 300_000"}]
        for patch in patches:
            with self.subTest(module=patch["module"], old=patch["old"][:40]):
                self.assertEqual(1, self.source(patch["module"]).count(patch["old"]))

    def test_scaled_limits_stay_in_the_clock_s_order(self):
        startup, quiet, stale, silence = control.SILENCE
        self.assertTrue(quiet < stale < silence)
        self.assertLess(silence, 30)  # the silent child's own sleep (45 s) outlasts the cutoff

    def test_the_stand_in_python_applies_a_listed_rewrite_and_refuses_one_that_misses(self):
        import subprocess
        import tempfile
        import e2e_support
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "mod_under_test.py").write_text("VALUE = 1\n")
            boot = e2e_support.BOOT.replace("%HOME%", repr(str(base))).replace("%SHIM%", repr("/x"))
            (base / ".e2e-bin").mkdir()
            (base / "boot.py").write_text(boot)
            script = base / "run.py"
            script.write_text("import sys\nsys.path.insert(0, %r)\nimport mod_under_test\nprint(mod_under_test.VALUE)\n" % str(base))
            patches = base / ".e2e-bin" / "patches.json"
            patches.write_text(json.dumps([{"module": "mod_under_test", "old": "VALUE = 1", "new": "VALUE = 7"}]))
            done = subprocess.run([sys.executable, "-B", str(base / "boot.py"), str(script)], capture_output=True, text=True)
            self.assertEqual("7", done.stdout.strip(), done.stderr)
            patches.write_text(json.dumps([{"module": "mod_under_test", "old": "VALUE = 2", "new": "VALUE = 7"}]))
            done = subprocess.run([sys.executable, "-B", str(base / "boot.py"), str(script)], capture_output=True, text=True)
            self.assertNotEqual(0, done.returncode)
            self.assertIn("does not apply", done.stderr)



# ---------------------------------------------------------------------------------------------
# T7 S1 review round 1 (NOT-READY): one strictness test per harness fix, each red on 1ea7bf8b.

import os  # noqa: E402
import tempfile  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

import e2e_core  # noqa: E402
import e2e_support  # noqa: E402


def run_check(function):
    """Run one check against the scene the test put in _SCENES; True when it passes."""
    phase = e2e_core.Phase(function.__name__, Path("/nonexistent"))
    try:
        function(phase)
    except e2e_core.CheckFailed:
        return False
    return phase.expectations > 0


class SceneTest(unittest.TestCase):
    def tearDown(self):
        e2e_core._SCENES.clear()


class FaultIsolationTests(SceneTest):
    """HIGH fault isolation: a fault is chosen only by an explicit worker argument, never inherited."""

    def test_an_inherited_variable_never_selects_a_fault(self):
        with mock.patch.dict(os.environ, {"E2E_T7S1_FAULT": "slot_not_released"}), \
                mock.patch.object(control, "FAULT", None, create=True):
            self.assertEqual([], control.fault_patches())

    def test_the_fault_is_selected_by_the_worker_argument(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("e2e_lifecycle_t", ROOT / "scripts" / "e2e_lifecycle.py")
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        seen = []
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(lifecycle, "_execute", side_effect=lambda names, root: seen.append(control.FAULT) or []):
            lifecycle.main(["--execute", "x", "--results", str(Path(folder) / "r.json"), "--fault", "slot_not_released"])
            lifecycle.main(["--execute", "x", "--results", str(Path(folder) / "r.json")])
        self.assertEqual(["slot_not_released", None], seen)
        launched = []
        done = SimpleNamespace(returncode=0)
        with mock.patch.object(lifecycle.subprocess, "run", side_effect=lambda argv, **kw: launched.append(argv) or done):
            lifecycle._execute_in_processes(["l7_1_cancel_run_stops_a_running_child_ends_cancelled_capacity_released"],
                                            1, fault="slot_not_released")
        self.assertEqual(["--fault", "slot_not_released"], launched[0][-2:])

    def test_no_child_environment_carries_a_harness_variable(self):
        env = e2e_support.child_environment({"PATH": "/bin", "E2E_T7S1_FAULT": "slot_not_released", "E2E_X": "1"})
        self.assertEqual({"PATH": "/bin"}, env)

    def test_a_fault_run_must_turn_exactly_the_s1_ids_red(self):
        s1 = control.S1_IDS
        normal = {"requirements": {**{i: {"passed": i != "L6.1"} for i in s1}, "G1": {"passed": True}}}
        fault = {"requirements": {**{i: {"passed": i in ("L9.1", "L6.2")} for i in s1}, "G1": {"passed": True}}}
        self.assertTrue(control.fault_isolated(normal, fault)["isolated"])
        g1_red = {"requirements": {**fault["requirements"], "G1": {"passed": False}}}
        self.assertTrue(control.fault_isolated(normal, g1_red)["isolated"])  # G1 is declared (a refused expected-success dispatch)
        other_red = {"requirements": {**fault["requirements"], "G3": {"passed": False}}}
        self.assertFalse(control.fault_isolated({"requirements": {**normal["requirements"], "G3": {"passed": True}}},
                                                other_red)["isolated"])
        missing = {"requirements": {i: v for i, v in fault["requirements"].items() if i != "L7.3"}}
        self.assertFalse(control.fault_isolated(normal, missing)["isolated"])
        one_green = {"requirements": {**fault["requirements"], "L7.1": {"passed": True}}}
        self.assertFalse(control.fault_isolated(normal, one_green)["isolated"])
        # L9.1 and L6.2 (refusals before any admission or release) are the S1 IDs the release fault cannot reach
        l9_1_green = {"requirements": {**fault["requirements"], "L9.1": {"passed": True}}}
        self.assertTrue(control.fault_isolated(normal, l9_1_green)["isolated"])
        l9_1_red = {"requirements": {**fault["requirements"], "L9.1": {"passed": False}}}
        self.assertFalse(control.fault_isolated(normal, l9_1_red)["isolated"])  # not a declared target
        self.assertEqual(set(control.S1_IDS) - {"L9.1", "L6.2"}, set(control.FAULT_TARGETS["slot_not_released"]))


class BillingConstantsTests(unittest.TestCase):
    """HIGH billing: decision 128 allows only the silence limits to be scaled; no scene rewrites billing reuse or
    cooldown (or anything else in the billing admission)."""

    def test_no_scene_patches_the_billing_admission(self):
        source = (ROOT / "scripts" / "e2e_checks_run_control.py").read_text()
        self.assertNotIn("PROBE_REUSE_MS", source)
        self.assertNotIn("PROBE_COOLDOWN_MS", source)
        self.assertNotIn("account_billing_admission", source)


def client_denied_scene(rows):
    report = {"permission_denials": {"count": 1, "tools": ["Write"]}, "read_containment": "enforced"}
    result = {"execution_state": "failed", "summary": "claude_tool_not_allowed: the client denied Write"}
    run = SimpleNamespace(open_code=0, code=0, reply={"bridge_report": report, "result": result},
                          opened={"run_id": "r"}, project_id="p", recorded=(0, {}, ""))
    e2e_core._SCENES["run_ctl"] = SimpleNamespace(world=None, client_denied=run)
    return mock.patch.object(control, "history_rows", return_value=rows)


class L61Tests(SceneTest):
    """HIGH L6.1: a client-denied tool passes only with a persisted stopped/failed result that names the denial."""

    def test_a_result_with_no_terminal_state_fails(self):
        for rows in ([{"terminal_state": None}], [{}], []):
            with self.subTest(rows=rows), client_denied_scene(rows):
                self.assertFalse(run_check(control.l6_1_tool_the_client_denied_is_reported_in_the_result))

    def test_a_failed_result_that_does_not_name_the_reason_or_was_not_recorded_fails(self):
        for change in ({"summary": "it failed"}, {"execution_state": "completed"}, "unrecorded"):
            with self.subTest(change=change), client_denied_scene([{"terminal_state": "failed"}]):
                run = e2e_core._SCENES["run_ctl"].client_denied
                run.reply["bridge_report"]["outcome"] = "claude_tool_not_allowed"
                if change == "unrecorded":
                    run.recorded = (2, {}, "")
                else:
                    run.reply["result"].update(change)
                self.assertFalse(run_check(control.l6_1_tool_the_client_denied_is_reported_in_the_result))

    def test_a_stopped_or_completed_result_fails(self):
        for state in ("stopped", "completed"):
            with self.subTest(state=state), client_denied_scene([{"terminal_state": state,
                                                                  "reason_code": "claude_tool_not_allowed"}]):
                self.assertFalse(run_check(control.l6_1_tool_the_client_denied_is_reported_in_the_result))

    def test_the_fixed_outcome_passes(self):
        # fix/l6-findings a876f6f0: failed, claude_tool_not_allowed, in the reply and the stored result
        with client_denied_scene([{"terminal_state": "failed"}]):
            e2e_core._SCENES["run_ctl"].client_denied.reply["bridge_report"]["outcome"] = "claude_tool_not_allowed"
            self.assertTrue(run_check(control.l6_1_tool_the_client_denied_is_reported_in_the_result))


def uncommitted_scene(**changes):
    history = {"host_open_provenance": 2, "host_run_bindings": 2, "active_run_capacity": 2}
    scene = SimpleNamespace(
        world=None,
        uncommitted_preflight=(2, {"ready": False, "admission_created": False,
                                   "reason": "assignment read path is not committed"}, ""),
        uncommitted=SimpleNamespace(open_code=2, opened={"ready": False, "admission": "denied",
                                                                 "reason_code": "read_path_not_committed"}, code=2,
                                    reply={"ready": False, "reason_code": "read_path_not_committed"}),
        uncommitted_preflight_again=(2, {"ready": False, "admission_created": False,
                                         "reason": "assignment read path is not committed"}, ""),
        admissions_before=dict(history), admissions_after_preflight=dict(history),
        admissions_after_open=dict(history), admissions_after_dispatch=dict(history),
        # the fields the 1ea7bf8b check read, so the old check runs on the same scene
        uncommitted_before=(2, 2), uncommitted_after=(2, 2), uncommitted_slots=2, uncommitted_slots_after=2)
    for key, value in changes.items():
        setattr(scene, key, value)
    e2e_core._SCENES["run_ctl"] = scene
    return scene


class L62Tests(SceneTest):
    """HIGH L6.2: open must refuse, the admission history is re-read after the open, admission_created must be False."""

    def test_the_refusal_passes(self):
        uncommitted_scene()
        self.assertTrue(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_a_missing_admission_created_fails(self):
        uncommitted_scene(uncommitted_preflight=(2, {"ready": False, "reason": "assignment read path is not committed"}, ""))
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_an_open_that_admits_fails(self):
        scene = uncommitted_scene()
        scene.uncommitted.open_code, scene.uncommitted.opened = 0, {"ready": True, "admission": "admitted"}
        scene.uncommitted.reply = {"ready": False, "reason_code": "preparation_export_failed"}
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_an_admission_recorded_by_the_open_fails(self):
        uncommitted_scene(admissions_after_open={"host_open_provenance": 3, "host_run_bindings": 3,
                                                 "active_run_capacity": 2})
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))


def capacity_tree(folder):
    state = Path(folder) / ".local" / "state" / "codex-agents"
    for project in ("pa", "pb", "pc"):
        (state / "projects" / project).mkdir(parents=True)
        (state / "projects" / project / "history.sqlite3").write_text("")
    (state / "collector.sock").write_text("")
    return SimpleNamespace(home=Path(folder))


def opened(run_id, code=0):
    return SimpleNamespace(opened={"run_id": run_id, "run_handle": "h-" + run_id}, code=code, open_code=0,
                           reply={"ready": code == 0})


class L93Tests(SceneTest):
    """MED L9.3: each project's history holds exactly the run IDs opened for it (counts alone pass a swap)."""

    def scene(self, folder):
        a = [opened("a1"), opened("a2"), opened("a3"), opened("a4", code=2), opened("a5")]
        b = [opened("b1"), opened("b2"), opened("b3")]
        c = [opened("c1", code=2), opened("c2")]
        refused = [a[3], c[0]]
        ctls = [SimpleNamespace(project_id=p, runs=runs) for p, runs in (("pa", a), ("pb", b), ("pc", c))]
        slots = [{"project_id": p, "bridge_run_id": f"{p}{i}", "run_id": None, "released_at_ms": None}
                 for p in ("pa", "pb") for i in range(3)]
        e2e_core._SCENES["run_capacity"] = SimpleNamespace(
            world=capacity_tree(folder), ctls=ctls, slots_full=slots, refused=refused,
            reports=[{"runs": {"admitted": n}} for n in (5, 3, 2)])
        return {"pa": ["a1", "a2", "a3", "a5"], "pb": ["b1", "b2", "b3"], "pc": ["c2"]}

    def records(self, histories):
        return lambda world, project_id, record_type: [{"run_id": r} for r in histories.get(project_id, [])]

    def test_exact_run_ids_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            histories = self.scene(folder)
            with mock.patch.object(control, "project_records", side_effect=self.records(histories)):
                self.assertTrue(run_check(control.l9_3_two_projects_share_one_collector_each_with_its_own_runs_and_capacity))

    def test_a_result_exchanged_between_projects_fails_with_every_count_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            histories = self.scene(folder)
            histories["pa"][0], histories["pb"][0] = histories["pb"][0], histories["pa"][0]
            with mock.patch.object(control, "project_records", side_effect=self.records(histories)):
                self.assertFalse(run_check(control.l9_3_two_projects_share_one_collector_each_with_its_own_runs_and_capacity))


class G5Tests(SceneTest):
    """HIGH G5: close first, then ONE probe whose launch the account ledger shows, then the eight reads."""

    def scene(self, **changes):
        reads = [(None, None, {"state": "released", "read_at_ms": 300}) for _ in range(8)]
        scene = SimpleNamespace(
            world=None, closed_at_ms=100, reports=[{"runs": {"current_active": 0, "pending": 0}}] * 3,
            probe_free=(0, {"ready": True, "probe": "launched"}, ""), probe_started_ms=150,
            probe_admissions=[{"decision": "pass", "observed_at_ms": 200}],
            probe_billing=[{"observed_at_ms": 210}], reads_started_ms=250, final_reads=reads,
            slots_final=[{"released_at_ms": 1} for _ in range(8)])
        for key, value in changes.items():
            setattr(scene, key, value)
        e2e_core._SCENES["run_capacity"] = scene

    def test_the_ordered_scene_passes(self):
        self.scene()
        self.assertTrue(run_check(control.g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active))

    def test_a_probe_before_close_fails(self):
        self.scene(probe_started_ms=50, probe_admissions=[{"decision": "pass", "observed_at_ms": 60}])
        self.assertFalse(run_check(control.g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active))

    def test_reads_before_the_probe_fail(self):
        self.scene(reads_started_ms=120)
        self.assertFalse(run_check(control.g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active))

    def test_a_launch_the_ledger_does_not_show_fails(self):
        for admissions in ([], [{"decision": "pass", "observed_at_ms": 200}] * 2):
            with self.subTest(n=len(admissions)):
                self.scene(probe_admissions=admissions)
                self.assertFalse(run_check(control.g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active))
        self.scene(probe_billing=[])
        self.assertFalse(run_check(control.g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active))


class AccountReadTimeoutTests(unittest.TestCase):
    """MED G8: an account read that times out is judged through capture and answers a fixed failure."""

    def test_a_timed_out_read_is_captured_and_fails_plainly(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "python3"
            shim.write_text("#!/bin/sh\necho partial-output\nexec sleep 5\n")
            shim.chmod(0o755)
            captured = []
            view = SimpleNamespace(project=Path(folder), home=Path(folder), shim=shim, env=dict(os.environ),
                                   CLAUDE_HOST={}, outputs=[], steps=[],
                                   _capture=lambda text, **kw: captured.append((text, kw)))
            run = SimpleNamespace(opened={"run_handle": "h"}, reply={"bridge_report": {"bridge_run_id": "b"}})
            read = control.capacity_read(view, run, timeout=0.5)
        self.assertEqual({"failure": "capacity_read_timed_out"}, read)
        self.assertEqual(1, len(captured))
        self.assertIn("partial-output", captured[0][0])
        self.assertTrue(captured[0][1]["timed_out"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------
# Decision 157 (final targeted fix): each red on 3f190429.

import ast  # noqa: E402


class DispatchExitJudgingTests(SceneTest):
    """NEW HIGH: only a step EXPECTED to refuse is marked refuses=True; every other dispatch is judged by G1."""

    def ctl(self):
        ctl = object.__new__(control.Ctl)
        ctl.calls = []
        ctl.host = lambda *args, **kw: ctl.calls.append(kw) or (0, {}, "")
        return ctl

    def test_a_plain_dispatch_is_not_marked_as_an_expected_refusal(self):
        ctl = self.ctl()
        run = SimpleNamespace(common=[], route_file="r", opened={"run_handle": "h"}, code=None, reply=None, seconds=None)
        ctl.dispatch(run)
        self.assertFalse(ctl.calls[-1].get("refuses"))
        ctl.dispatch(run, refuses=True)
        self.assertTrue(ctl.calls[-1].get("refuses"))

    def _calls(self, function_name):
        tree = ast.parse((ROOT / "scripts" / "e2e_checks_run_control.py").read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
        return {ast.get_source_segment((ROOT / "scripts" / "e2e_checks_run_control.py").read_text(), n):
                any(k.arg == "refuses" and getattr(k.value, "value", None) is True for k in n.keywords)
                for n in ast.walk(function) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("dispatch", "probe_as_second_account")}

    def test_only_the_expected_refusals_of_the_capacity_scene_are_marked(self):
        calls = self._calls("capacity_scene")
        refusing = sorted(text for text, flag in calls.items() if flag)
        self.assertEqual(["a.dispatch(scene.fourth, refuses=True)", "c.dispatch(scene.c_run, refuses=True)",
                          "probe_as_second_account(world, views[2], refuses=True)"], refusing)
        self.assertIn("a.dispatch(scene.fourth_again)", calls)
        self.assertIn("c.dispatch(scene.c_again)", calls)
        source = (ROOT / "scripts" / "e2e_checks_run_control.py").read_text()
        self.assertNotIn("scene.probe_free = probe_as_second_account(world, views[2], refuses=True)", source)
        self.assertIn("scene.probe_full = probe_as_second_account(world, views[2], refuses=True)", source)

    def test_the_dispatch_helper_has_no_blanket_refuses(self):
        source = (ROOT / "scripts" / "e2e_checks_shared.py").read_text()  # Ctl.dispatch moved there (batch3)
        body = source.split("def dispatch(self, run", 1)[1].split("def record", 1)[0]
        self.assertNotIn("refuses=True", body)

    def test_the_six_first_wave_dispatches_must_exit_zero(self):
        runs = [SimpleNamespace(code=0, reply={"ready": True}) for _ in range(6)]
        for bad in (None, 3):
            with self.subTest(code=bad):
                runs[0].code = bad
                e2e_core._SCENES["run_capacity"] = SimpleNamespace(
                    a_runs=runs[:3] + [SimpleNamespace(code=2)], b_runs=runs[3:], world=None,
                    reads={str(i): {"state": "released", "released_at_ms": 1} for i in range(6)},
                    slots_ended=[{"released_at_ms": 1}] * 6,
                    fourth_again=SimpleNamespace(code=0, reply={"ready": True}), c_again=SimpleNamespace(code=0, reply={"ready": True}))
                with mock.patch.object(control, "terminal_state", return_value="failed"):
                    self.assertFalse(run_check(control.l9_2_capacity_released_after_each_terminal_shown_by_per_run_read))
        runs[0].code = 0
        with mock.patch.object(control, "terminal_state", return_value="failed"):
            self.assertTrue(run_check(control.l9_2_capacity_released_after_each_terminal_shown_by_per_run_read))


class L93ScheduledTests(L93Tests):
    """MED L9.3: expected results derive from the runs SCHEDULED to succeed; the 4/3/1 counts are kept."""

    def test_a_scheduled_run_whose_dispatch_failed_and_whose_result_is_missing_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            histories = self.scene(folder)
            histories["pa"].remove("a5")  # a5 was scheduled to succeed but its dispatch failed
            e2e_core._SCENES["run_capacity"].ctls[0].runs[4].code = 2
            with mock.patch.object(control, "project_records", side_effect=self.records(histories)):
                self.assertFalse(run_check(control.l9_3_two_projects_share_one_collector_each_with_its_own_runs_and_capacity))

    def test_the_refused_run_must_leave_no_result(self):
        with tempfile.TemporaryDirectory() as folder:
            histories = self.scene(folder)
            histories["pa"].append("a4")
            with mock.patch.object(control, "project_records", side_effect=self.records(histories)):
                self.assertFalse(run_check(control.l9_3_two_projects_share_one_collector_each_with_its_own_runs_and_capacity))


class L62ReasonTests(SceneTest):
    """HIGH L6.2: a crashed open with an empty reply is no refusal; the dispatch must refuse with the expected reason."""

    def test_a_crashed_open_with_an_empty_reply_fails(self):
        scene = uncommitted_scene()
        scene.uncommitted.open_code, scene.uncommitted.opened = 1, {}
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_an_open_refused_for_another_reason_fails(self):
        scene = uncommitted_scene()
        scene.uncommitted.opened = {"ready": False, "admission": "denied", "reason_code": "run_unavailable"}
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_a_dispatch_refused_for_another_reason_still_passes(self):
        # decision 172: the dispatch after a refused open has no run handle; its reason is no longer judged
        scene = uncommitted_scene()
        scene.uncommitted.reply = {"ready": False, "reason_code": "dispatch_unavailable"}
        self.assertTrue(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))

    def test_a_repeat_preflight_refused_for_another_reason_fails(self):
        _scene = uncommitted_scene(uncommitted_preflight_again=(2, {"ready": False, "admission_created": False, "reason": "other"}, ""))
        self.assertFalse(run_check(control.l6_2_uncommitted_read_path_refused_before_admission))


class ChildEnvironmentTests(unittest.TestCase):
    """HIGH fault isolation: no helper child inherits an E2E_* variable."""

    def test_every_subprocess_call_names_its_environment(self):
        for name in ("e2e_support.py", "e2e_lifecycle.py"):
            tree = ast.parse((ROOT / "scripts" / name).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and getattr(node.func.value, "id", "") == "subprocess" and node.func.attr in ("run", "Popen", "check_output")):
                    self.assertTrue(any(k.arg == "env" for k in node.keywords), f"{name}:{node.lineno}")
