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
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    if module.startswith("setup_wizard"):
        return (ROOT / "scripts" / (module.replace(".", "/") + ".py")).read_text()
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
        self.assertEqual({"update_handover_broken", "failed_update_leaves_files", "failed_update_alters_history",
                          "update_ignores_uncommitted_files", "update_stops_other_projects"}, set(us.FAULTS))
        for name in us.FAULTS:
            self.assertIn(name, lifecycle.FAULTS)
        self.assertIn(us, lifecycle.FAULT_MODULES)

    def test_each_fault_has_its_own_target_and_the_targets_are_distinct(self):
        """Decision 259 / review: L11.2, L12.4 and L12.5 each have a red proof on their OWN assertion, not through G6's
        handover fault; L12.3 has none (it is red by a product bug, so no green-to-red transition can be shown)."""
        self.assertEqual(set(us.FAULTS), set(us.FAULT_TARGETS))
        self.assertEqual({"G6"}, set(us.FAULT_TARGETS["update_handover_broken"]))
        self.assertEqual(("L11.2",), us.FAULT_TARGETS["failed_update_leaves_files"])
        self.assertEqual(("L11.2",), us.FAULT_TARGETS["failed_update_alters_history"])  # L11.2's second, history-store proof
        self.assertEqual(("L12.4",), us.FAULT_TARGETS["update_ignores_uncommitted_files"])
        self.assertEqual(("L12.5",), us.FAULT_TARGETS["update_stops_other_projects"])
        every = [i for name, targets in us.FAULT_TARGETS.items() for i in targets if name != "failed_update_alters_history"]
        self.assertEqual(len(every), len(set(every)))  # one fault per ID, except L11.2's second (history) proof
        self.assertNotIn("L12.3", every)
        for name in ("failed_update_leaves_files", "failed_update_alters_history", "update_ignores_uncommitted_files",
                     "update_stops_other_projects"):
            self.assertEqual((), us.FAULT_ALSO_RED[name], name)  # the narrow faults turn nothing else red

    def test_the_failed_update_fault_replaces_the_plain_forced_failure_only_for_the_forced_update(self):
        us.FAULT = "failed_update_leaves_files"
        try:
            self.assertEqual([], us.fault_patches())
            forced = us.fault_patches(forced=True)
            self.assertEqual(us.FAULTS["failed_update_leaves_files"], forced)
            self.assertIn("e2e_forced_failure", forced[0]["new"])  # it still fails the update
            self.assertIn("e2e-left-by-the-failed-update.txt", forced[0]["new"])
        finally:
            us.FAULT = None

    def test_the_history_fault_changes_only_a_non_content_column_and_replaces_the_forced_failure(self):
        us.FAULT = "failed_update_alters_history"
        try:
            self.assertEqual([], us.fault_patches())
            forced = us.fault_patches(forced=True)
            self.assertEqual(us.FAULTS["failed_update_alters_history"], forced)
            new = forced[0]["new"]
            self.assertIn("e2e_forced_failure", new)  # it still fails the update
            self.assertIn("ingested_at_ms = ingested_at_ms + 1", new)
            self.assertNotIn("content", new)  # the content column is left alone
            self.assertNotIn("e2e-left-by-the-failed-update.txt", new)  # and no file is left
        finally:
            us.FAULT = None

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

    def reports(self, normal_red=(), fault_red=(), extra_ids=("G3",)):
        ids = [*us.CHECKED_IDS, *extra_ids]
        make = lambda red: {"flows": ["L11", "L12"], "requirements": {i: {"passed": i not in red} for i in ids}}
        return make(normal_red), make(fault_red), set(ids)

    def test_a_fault_run_needs_a_real_green_to_red_transition_per_targeted_id(self):
        for name, targets in us.FAULT_TARGETS.items():
            normal, fault, ids = self.reports(normal_red=("L12.3",), fault_red=("L12.3", *targets))
            self.assertTrue(us.fault_isolated(normal, fault, name, expected_ids=ids)["isolated"], name)
            # a targeted ID that is red in BOTH runs is no proof
            both_red = self.reports(normal_red=("L12.3", *targets), fault_red=("L12.3", *targets))
            result = us.fault_isolated(both_red[0], both_red[1], name, expected_ids=both_red[2])
            self.assertFalse(result["isolated"], name)
            self.assertEqual(sorted(targets), result["targets_not_transitioned"])
            # a targeted ID still green under the fault is no proof either
            none = self.reports(normal_red=("L12.3",), fault_red=("L12.3",))
            self.assertFalse(us.fault_isolated(none[0], none[1], name, expected_ids=none[2])["isolated"], name)

    def test_a_fault_may_not_change_another_id_or_lose_one(self):
        normal, fault, ids = self.reports(normal_red=("L12.3",), fault_red=("L12.3", "L12.5", "G3"))
        self.assertFalse(us.fault_isolated(normal, fault, "update_stops_other_projects", expected_ids=ids)["isolated"])
        normal, fault, ids = self.reports(normal_red=("L12.3",), fault_red=("L12.3", "L12.4", "L12.5"))
        self.assertFalse(us.fault_isolated(normal, fault, "update_stops_other_projects", expected_ids=ids)["isolated"])
        normal, fault, ids = self.reports(normal_red=("L12.3",), fault_red=("L12.3", "G6"))
        del fault["requirements"]["L12.3"]  # an ID the fault run lost (L12.3 is no named collateral)
        self.assertFalse(us.fault_isolated(normal, fault, "update_handover_broken", expected_ids=ids)["isolated"])
        # the handover fault may also turn its named collateral IDs red
        normal, fault, ids = self.reports(normal_red=("L12.3",), fault_red=("L12.3", "G6", "L11.2", "L12.4", "L12.5"))
        self.assertTrue(us.fault_isolated(normal, fault, "update_handover_broken", expected_ids=ids)["isolated"])
        self.assertEqual({"G1", "G8", "L11.2", "L12.4", "L12.5"}, set(us.FAULT_ALSO_RED["update_handover_broken"]))

    def test_requirement_map_reads_the_workers_entries(self):
        entries = [{"requirements": ["L12.3"], "passed": True, "reasons": ["a"]},
                   {"requirements": ["L12.3"], "passed": False, "reasons": ["b"]},
                   {"requirements": ["G6", "G1"], "passed": True}]
        verdict = us.requirement_map(entries)["requirements"]
        self.assertEqual({"L12.3": {"passed": False, "reasons": ["a", "b"]}, "G6": {"passed": True, "reasons": []},
                          "G1": {"passed": True, "reasons": []}}, verdict)

    def test_g6_red_by_the_product_bug_proves_the_fault_only_by_an_update_that_turned_from_ok_to_failed(self):
        """G6 is red in a normal run until UPDFIX lands (the retry after the refusal). Its fault proof is then a per-update
        reason code: an update `_ok` in the normal run and `_failed` in the fault run. Red in both with no such change is no proof."""
        ids = {*us.CHECKED_IDS, "G3"}

        def doc(g6_reasons, g6=False):
            reqs = {i: {"passed": True, "reasons": []} for i in ids}
            reqs["L12.3"]["passed"] = False
            reqs["G6"] = {"passed": g6, "reasons": g6_reasons}
            return {"flows": [], "requirements": reqs}
        normal = doc(["g6_first_update_ok", "g6_retry_after_refusal_failed"])
        fault = doc(["g6_first_update_failed", "g6_retry_after_refusal_failed"])
        fault["requirements"].update({k: {"passed": False, "reasons": []} for k in ("L11.2", "L12.4", "L12.5")})
        self.assertTrue(us.fault_isolated(normal, fault, "update_handover_broken", expected_ids=ids)["isolated"])
        same = doc(["g6_first_update_ok", "g6_retry_after_refusal_failed"])
        same["requirements"].update({k: {"passed": False, "reasons": []} for k in ("L11.2", "L12.4", "L12.5")})
        result = us.fault_isolated(normal, same, "update_handover_broken", expected_ids=ids)
        self.assertFalse(result["isolated"])
        self.assertEqual(["G6"], result["targets_not_transitioned"])
        # only G6 has this latitude: another targeted ID red in both runs is rejected
        both = doc([])
        both["requirements"]["L12.4"]["passed"] = False
        fault_l124 = doc([])
        fault_l124["requirements"]["L12.4"]["passed"] = False
        self.assertEqual(["L12.4"], us.fault_isolated(both, fault_l124, "update_ignores_uncommitted_files",
                                                      expected_ids=ids)["targets_not_transitioned"])

    def test_g6_exception_is_restricted_to_g6_the_named_updates_and_the_known_baseline_bug(self):
        """Review t7s5-r2c MED: an unrelated `_ok` -> `_failed` pair, another baseline failure or an unnamed failing update
        in the fault run must not count as G6's transition."""
        ids = {*us.CHECKED_IDS, "G3"}

        def doc(g6_reasons):
            reqs = {i: {"passed": True, "reasons": []} for i in ids}
            reqs["L12.3"]["passed"] = False
            reqs["G6"] = {"passed": False, "reasons": g6_reasons}
            for k in ("L11.2", "L12.4", "L12.5"):
                reqs[k]["passed"] = False
            return {"flows": [], "requirements": reqs}
        base = ["g6_retry_after_refusal_failed"]

        def isolated(normal, fault):
            return us.fault_isolated(doc(normal), doc(fault), "update_handover_broken", expected_ids=ids)["isolated"]
        self.assertTrue(isolated(["g6_first_update_ok", *base], ["g6_first_update_failed", *base]))
        for name in us.G6_UPDATES:
            if name != "retry_after_refusal":
                self.assertTrue(isolated([f"g6_{name}_ok", *base], [f"g6_{name}_failed", *base]), name)
        # an unrelated pair is no proof
        self.assertFalse(isolated(["unrelated_ok", *base], ["unrelated_failed", *base]))
        self.assertFalse(isolated(["g6_first_update_ok", "unrelated_ok", *base], ["g6_first_update_ok", "unrelated_failed", *base]))
        self.assertFalse(isolated(["g6_unrelated_ok", *base], ["g6_unrelated_failed", *base]))
        # a failing update in the fault run that G6 does not judge is rejected even with a real named transition
        self.assertFalse(isolated(["g6_first_update_ok", *base], ["g6_first_update_failed", "g6_unrelated_failed", *base]))
        # the baseline's only failing G6 code must be the known product bug
        self.assertFalse(isolated(["g6_first_update_ok"], ["g6_first_update_failed"]))  # no baseline failure at all
        self.assertFalse(isolated(["g6_first_update_ok", "g6_second_update_failed"],
                                  ["g6_first_update_failed", "g6_second_update_failed"]))  # another update fails in the baseline
        self.assertFalse(isolated(["g6_first_update_ok", *base, "g6_second_update_failed"],
                                  ["g6_first_update_failed", *base, "g6_second_update_failed"]))  # more than the known failure
        # the known failure alone changes nothing
        self.assertFalse(isolated(["g6_first_update_ok", *base], ["g6_first_update_ok", *base]))

    def test_a_named_collateral_id_may_appear_in_only_one_run(self):
        ids = {*us.CHECKED_IDS}
        normal = {"flows": [], "requirements": {i: {"passed": i != "L12.3", "reasons": []} for i in ids}}
        fault = {"flows": [], "requirements": {i: {"passed": False, "reasons": []} for i in ids}}
        fault["requirements"]["G8"] = {"passed": False, "reasons": []}
        fault["requirements"]["G1"] = {"passed": False, "reasons": []}
        fault["requirements"]["G6"] = {"passed": False, "reasons": []}
        result = us.fault_isolated(normal, fault, "update_handover_broken", expected_ids=ids)
        self.assertEqual([], result["ids_differ"])
        self.assertEqual([], result["changed_outside_targets"])
        stray = {"flows": [], "requirements": {**fault["requirements"], "G5": {"passed": False, "reasons": []}}}
        self.assertEqual(["G5"], us.fault_isolated(normal, stray, "update_handover_broken", expected_ids=ids)["ids_differ"])

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

    def test_history_is_kept_only_when_records_exist_and_are_unchanged(self):
        before = {"rows": {"outcome:1": "a", "dispatch_result:2": "b"}, "integrity": "ok"}
        same = {"rows": dict(before["rows"]), "integrity": "ok"}
        self.assertTrue(us.history_kept(before, same, exact=True))
        self.assertTrue(us.history_kept(before, same, exact=False))
        grown = {"rows": {**before["rows"], "outcome:3": "c"}, "integrity": "ok"}
        self.assertFalse(us.history_kept(before, grown, exact=True))  # right after a failed update nothing may be added
        self.assertTrue(us.history_kept(before, grown, exact=False))  # after a successful update later records may follow
        edited = {"rows": {**before["rows"], "outcome:1": "x"}, "integrity": "ok"}
        self.assertFalse(us.history_kept(before, edited, exact=False))  # a changed record
        lost = {"rows": {"outcome:1": "a"}, "integrity": "ok"}
        self.assertFalse(us.history_kept(before, lost, exact=False))  # a lost record
        self.assertFalse(us.history_kept(before, {**same, "integrity": "database disk image is malformed"}, exact=False))
        self.assertFalse(us.history_kept({"rows": {}, "integrity": "ok"}, {"rows": {}, "integrity": "ok"}, exact=True))  # no history seeded
        self.assertFalse(us.history_kept(before, {"rows": {}, "integrity": "unreadable"}, exact=False))

    def test_the_history_dump_reads_every_column_of_every_row_of_every_table(self):
        """Review t7s5-r2c HIGH: a rollback that changes a non-content column (`ingested_at_ms`, `late`, any other table's
        column) must not look like an unchanged store."""
        with tempfile.TemporaryDirectory() as tmp:
            world = SimpleNamespace(home=Path(tmp))
            path = us.shared.project_history(world, "p1")
            path.parent.mkdir(parents=True)
            db = sqlite3.connect(path)
            db.executescript(
                "create table records(project_id text, record_id text primary key, record_type text, source text,"
                " source_sequence integer, observed_at_ms integer, ingested_at_ms integer, content blob, content_digest text,"
                " late integer);"
                "create table history_identity(project_id text primary key, next_source_sequence integer);"
                "create table result_identity(project_id text primary key, key_check text, last_observed_at_ms integer);"
                "create table no_rowid(a text primary key, b text) without rowid;"
                "insert into records values('p1','r1','outcome','s',1,10,20,x'0102','d',0);"
                "insert into records values('p1','r2','dispatch_result','s',2,11,21,x'03','e',0);"
                "insert into history_identity values('p1',3);"
                "insert into result_identity values('p1','kc',1000);"
                "insert into no_rowid values('k','v');")
            db.commit()
            before = us.history_dump(world, "p1")
            self.assertEqual("ok", before["integrity"])
            self.assertEqual({"outcome:1", "dispatch_result:2", "history_identity:1", "result_identity:1", "no_rowid:0"}, set(before["rows"]))
            self.assertTrue(us.history_kept(before, us.history_dump(world, "p1"), exact=True))
            for name, statement in (("ingested_at_ms", "update records set ingested_at_ms = 21 where record_id='r1'"),
                                    ("late", "update records set late = 1 where record_id='r2'"),
                                    ("observed_at_ms", "update records set observed_at_ms = 99"),
                                    ("record_id", "update records set record_id = 'z' where record_id='r1'"),
                                    ("content", "update records set content = x'ff' where record_id='r1'"),
                                    ("another table", "update history_identity set next_source_sequence = 4"),
                                    ("a result_identity column other than the clock", "update result_identity set key_check = 'x'"),
                                    ("the clock floor going back", "update result_identity set last_observed_at_ms = 999"),
                                    ("a table without a rowid", "update no_rowid set b = 'w'")):
                db.execute(statement)
                db.commit()
                changed = us.history_dump(world, "p1")
                self.assertFalse(us.history_kept(before, changed, exact=True), name)
                self.assertFalse(us.history_kept(before, changed, exact=False), name)  # an earlier row changed
                db.rollback()
                db.executescript("delete from records; delete from history_identity; delete from no_rowid; delete from result_identity;"
                                 "insert into result_identity values('p1','kc',1000);"
                                 "insert into records values('p1','r1','outcome','s',1,10,20,x'0102','d',0);"
                                 "insert into records values('p1','r2','dispatch_result','s',2,11,21,x'03','e',0);"
                                 "insert into history_identity values('p1',3); insert into no_rowid values('k','v');")
                db.commit()
                self.assertTrue(us.history_kept(before, us.history_dump(world, "p1"), exact=True), name)
            db.execute("update result_identity set last_observed_at_ms = 1010")  # the live service's clock floor advances alone
            db.commit()
            moved = us.history_dump(world, "p1")
            self.assertTrue(us.history_kept(before, moved, exact=True))
            self.assertTrue(us.history_kept(before, moved, exact=False))
            self.assertFalse(us.history_kept(moved, before, exact=True))  # but never back
            self.assertEqual([], us.history_differences(before, moved))
            db.execute("update records set ingested_at_ms = 22 where record_id='r1'")
            db.commit()
            self.assertEqual(["records:outcome"], us.history_differences(before, us.history_dump(world, "p1")))
            db.execute("update records set ingested_at_ms = 20 where record_id='r1'")
            db.execute("create table extra(a)")  # a changed schema is not "exactly as before"
            db.commit()
            self.assertFalse(us.history_kept(before, us.history_dump(world, "p1"), exact=True))
            db.close()
            self.assertEqual({"rows": {}, "integrity": "unreadable"}, us.history_dump(world, "missing"))

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
    def test_each_checked_s5_id_has_its_own_check_in_the_module(self):
        declared = {name: ids for name, (ids, _f) in lifecycle.CHECKS.items() if name in us.CHECK_NAMES}
        row = {item["id"]: item for item in REGISTRY}
        for requirement in us.CHECKED_IDS:
            self.assertTrue([n for n, ids in declared.items() if requirement in ids], requirement)
            self.assertTrue(row[requirement]["checks"], requirement)
            self.assertTrue(set(row[requirement]["checks"]) <= set(declared), requirement)

    def test_l12_2_is_unchecked_on_purpose_with_its_reason(self):
        """Decision 259: no 0.2.x leftovers are planted, so no check may claim the ID."""
        self.assertEqual(("L12.2",), tuple(us.UNCHECKED))
        item = next(i for i in REGISTRY if i["id"] == "L12.2")
        self.assertEqual([], item["checks"])
        self.assertIs(True, item["deferred"])
        self.assertTrue(item["reason"].startswith("0.2.x leftovers need a historical ledger: slice S4 / L12.6"))
        for name, (ids, _f) in lifecycle.CHECKS.items():
            self.assertNotIn("L12.2", ids, name)
        self.assertNotIn("L12.2", us.CHECKED_IDS)

    def test_l11_2_names_the_installers_automatic_rollback_not_learning_rollback(self):
        """Decision 260: the statement says the installer's automatic rollback of a failed update (same intent)."""
        item = next(i for i in REGISTRY if i["id"] == "L11.2")
        self.assertNotIn("learning-rollback", item["statement"])
        self.assertIn("the installer's automatic rollback of a failed update works", item["statement"])
        self.assertTrue(item["statement"].startswith("A failed upgrade forced part-way leaves the records unchanged"))
        self.assertTrue(item["statement"].endswith("and the next attempt succeeds."))

    def test_every_check_of_the_slice_shares_one_group_so_g6_sees_every_update_once(self):
        self.assertEqual({us.GROUP}, {lifecycle.CHECK_GROUPS[name] for name in us.CHECK_NAMES})

    def test_g6_judges_every_update_of_the_slice_including_the_retry_after_the_refusal(self):
        function = next(n for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef)
                        and n.name.startswith("g6_update_never_needs"))
        text = ast.get_source_segment(SOURCE, function)
        for needle in ("main.u1", "main.u2", "child.final", "dirty.final", "fail.nxt", "child_scene()", "dirty_scene()",
                       "fail_scene()", "main_scene()"):
            self.assertIn(needle, text, needle)

    def test_l11_2_seeds_real_child_history_and_compares_the_stores_contents(self):
        function = next(n for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef) and n.name == "fail_scene")
        text = ast.get_source_segment(SOURCE, function)
        self.assertLess(text.index("run_a_child("), text.index("do_update(world, refuses=True)"))
        self.assertLess(text.index("history_before"), text.index("do_update(world, refuses=True)"))
        for needle in ("history_failed", "history_next", "persisted_outcome", "capacity_read"):
            self.assertIn(needle, text)
        check = next(n for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef) and n.name.startswith("l11_2_"))
        body = ast.get_source_segment(SOURCE, check)
        self.assertIn("history_kept(scene.history_before, scene.history_failed, exact=True)", body)
        self.assertIn("history_kept(scene.history_before, scene.history_next, exact=False)", body)

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
