"""T7 S7 (L16, the sole ledger owner under load): the checks module is strict by construction, without starting a World.

Guards: each judge goes red on the failure it names and stays green on the good run; fault selection is by explicit
argument only; every fault rewrite applies to the product exactly once; the three requirement IDs each have a check;
the module waits on ends of processes and threads and on a prober line, never on a fixed delay."""
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
import e2e_checks_owner_load as load  # noqa: E402

SOURCE = (ROOT / "scripts" / "e2e_checks_owner_load.py").read_text()
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
BIG, SMALL = "prj_big", "prj_small"


class JudgeTests(unittest.TestCase):
    def test_events_written_exactly_once(self):
        self.assertEqual([], load.written_exactly_once(3, ["a", "b", "c"]))
        self.assertEqual(["events_lost"], load.written_exactly_once(3, ["a", "b"]))
        self.assertEqual(["events_extra"], load.written_exactly_once(2, ["a", "b", "c"]))
        self.assertEqual(["events_duplicated"], load.written_exactly_once(3, ["a", "a", "c"]))
        self.assertEqual(["events_extra", "events_duplicated"], load.written_exactly_once(2, ["a", "a", "c"]))

    def test_fair_turns_are_judged_by_where_the_small_projects_events_land(self):
        fair = [SMALL, BIG, SMALL, BIG, BIG, BIG, BIG, BIG]
        self.assertEqual([], load.served_among(fair, SMALL, BIG, 2, 6))
        after_all = [BIG] * 6 + [SMALL] * 2  # first in, first out: the small project waits for the whole backlog
        self.assertEqual(["small_project_last", "small_project_waited_for_half"], load.served_among(after_all, SMALL, BIG, 2, 6))
        late_start = [BIG] * 4 + [SMALL, BIG, BIG, SMALL]
        self.assertIn("small_project_waited_for_half", load.served_among(late_start, SMALL, BIG, 2, 6))
        self.assertEqual(["events_missing"], load.served_among([SMALL, BIG], SMALL, BIG, 2, 6))

    def test_health_is_judged_on_answers_and_time(self):
        good = {"plain": {"answered": 30, "refused": 0, "max_ms": 80.0}, "project": {"answered": 28, "refused": 2, "max_ms": 300.0}}
        self.assertEqual([], load.health_verdict(good, 2000))
        slow = {**good, "project": {"answered": 30, "refused": 0, "max_ms": 2600.0}}
        self.assertEqual(["project_health_slow"], load.health_verdict(slow, 2000))
        refused = {**good, "plain": {"answered": 29, "refused": 1, "max_ms": 80.0}}
        self.assertEqual(["plain_health_refused"], load.health_verdict(refused, 2000))
        self.assertIn("plain_health_barely_asked", load.health_verdict({**good, "plain": {"answered": 0, "refused": 0, "max_ms": 0}}, 2000))
        self.assertIn("project_health_barely_asked", load.health_verdict({"plain": good["plain"]}, 2000))  # a prober that never reported

    def test_account_calls_are_judged_on_answers_and_on_what_was_recorded(self):
        def reply(request_id, at=1, **changes):
            return {"code": 0, "request_id": request_id, "provider": "codex", "ready": True, "observed_at_ms": at, **changes}
        good = [reply("a", 1), reply("a", 1), reply("b", 2), reply("b", 2)]
        self.assertEqual([], load.account_verdict(good, 2, 2))
        self.assertEqual(["account_request_lost"], load.account_verdict(good, 2, 1))
        self.assertEqual(["account_request_duplicated"], load.account_verdict(good, 2, 3))
        self.assertEqual(["account_call_not_answered"], load.account_verdict([*good, reply("c", code=1)], 2, 2))
        self.assertEqual(["account_call_not_answered"], load.account_verdict([*good, reply("c", ready=None)], 2, 2))
        self.assertEqual(["account_request_answered_twice_differently"],
                         load.account_verdict([reply("a", 1), reply("a", 9), reply("b", 2), reply("b", 2)], 2, 2))


class StrictnessTests(unittest.TestCase):
    def test_no_environment_variable_selects_the_fault(self):
        self.assertNotRegex(SOURCE, r"environ(\.get|\[)[^\n]*(E2E|FAULT)")
        self.assertNotRegex(SOURCE, r"getenv")
        self.assertIsNone(load.FAULT)

    def test_every_fault_rewrite_applies_to_the_product_exactly_once(self):
        self.assertEqual({"ingest_lost", "health_stalls"}, set(load.FAULTS))
        for name, patches in load.FAULTS.items():
            for item in patches:
                path = ORCHESTRATION / "codex_agents" / (item["module"].split(".", 1)[1] + ".py")
                self.assertEqual(1, path.read_text().count(item["old"]), (name, item["module"]))

    def test_the_runner_offers_this_modules_faults(self):
        self.assertTrue(set(load.FAULTS) <= set(lifecycle.FAULTS))
        self.assertIn(load, lifecycle.FAULT_MODULES)

    def test_each_requirement_has_a_check_and_the_flow_is_listed(self):
        document = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())
        self.assertIn("L16", document["flows"])
        declared = {name: ids for name, (ids, _) in lifecycle.CHECKS.items() if name.startswith("l16_")}
        self.assertEqual(set(load.S7_IDS), {i for ids in declared.values() for i in ids})
        for item in document["requirements"]:
            if item["flow"] == "L16":
                self.assertTrue(item["checks"] and all(name in declared for name in item["checks"]), item["id"])

    def test_the_module_waits_for_ends_and_lines_never_for_a_fixed_delay(self):
        # The only sleep named is inside a fault's rewritten product text (a stalled health, by design).
        self.assertEqual(1, SOURCE.count("sleep("))
        self.assertTrue(SOURCE.index("FAULTS = {") < SOURCE.index("sleep(") < SOURCE.index("def fault_patches"))
        self.assertIn("barrier.wait", SOURCE)
        self.assertIn("process.stdout.readline()", SOURCE)

    def test_every_load_step_is_the_installed_product(self):
        # hooks: the installed hook command; account calls and status: the installed CLI; health: the installed client library.
        self.assertIn("run_hook(", SOURCE)
        self.assertNotRegex(SOURCE, r"cli\.py\b")
        self.assertNotRegex(SOURCE, r"\bAccountClient\b|\bLearningLedger\b|_dispatch\(")
        self.assertIn("view.shim", SOURCE)
        self.assertTrue(re.search(r'"provider-observe", "--provider", "codex", "--request-id"', SOURCE))


if __name__ == "__main__":
    unittest.main()
