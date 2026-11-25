"""T7 S4 (scripts/e2e_checks_update_history.py): strictness of the update-from-earlier-release checks.

The judges are run on a scene made by hand: they must pass on the one honest scene and fail on lost or invented
history, on a collector that is not this build and on missing data. The test-only faults must name source that
exists exactly once in the build they are meant to break. Written before the module's scenes were run (red first)."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_core  # noqa: E402
import e2e_checks_update_history as uh  # noqa: E402

ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"


def judged(function, *args) -> bool:
    phase = e2e_core.Phase(function.__name__, Path("/nonexistent"))
    try:
        function(phase, *args)
    except e2e_core.CheckFailed:
        return False
    return phase.expectations > 0


def ids(prefix: str, count: int) -> frozenset:
    return frozenset(f"{prefix}{n}" for n in range(count))


def side(tables: dict, record_ids: dict, keys: dict) -> dict:
    return {"tables": {**tables, "records": sum(len(v) for v in record_ids.values())},
            "types": {kind: len(v) for kind, v in record_ids.items()}, "keys": keys, "record_keys": record_ids}


def honest_scene():
    """Before: two runs, two attestations, nine provider identities. After: the same rows and the setup's own two new
    provider identities, two observation requests, a migration-made table."""
    old = {"admission_decision": ids("a", 2), "provider_identity": ids("p", 9), "event": ids("e", 5)}
    keys = {"host_run_bindings": ids("b", 2), "run_provenance": ids("r", 2), "owner_attestations": ids("o", 2),
            "account_observation_requests": ids("q", 9), "account_observation_origins": frozenset(),
            "stored_files": frozenset()}
    before = side({name: len(v) for name, v in keys.items()}, old, keys)
    new_ids = {**old, "provider_identity": old["provider_identity"] | ids("new", 2), "provider_billing": ids("pb", 1),
               "provider_headroom": ids("ph", 1)}
    new_keys = {**keys, "account_observation_requests": keys["account_observation_requests"] | ids("newq", 2),
                "account_observation_origins": ids("newo", 2), "stored_files": ids("sf", 1), "run_holds": ids("h", 6)}
    after = side({name: len(v) for name, v in new_keys.items()}, new_ids, new_keys)
    return SimpleNamespace(before=before, after=after)


class HistoryKeptTests(unittest.TestCase):
    def test_the_honest_update_keeps_history(self):
        self.assertTrue(judged(uh.history_kept, honest_scene()))

    def test_a_table_that_lost_a_row_fails(self):
        scene = honest_scene()
        scene.after["keys"]["run_provenance"] = ids("r", 1)
        scene.after["tables"]["run_provenance"] = 1
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_table_the_setup_does_not_add_to_but_gained_a_row_fails(self):
        scene = honest_scene()
        scene.after["keys"]["host_run_bindings"] = ids("b", 2) | {"extra"}
        scene.after["tables"]["host_run_bindings"] = 3
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_table_the_setup_adds_to_may_not_gain_more_than_its_own_checks(self):
        scene = honest_scene()
        scene.after["keys"]["account_observation_requests"] |= ids("more", 1)
        scene.after["tables"]["account_observation_requests"] = len(scene.after["keys"]["account_observation_requests"])
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_record_kind_that_changed_or_vanished_fails(self):
        for kind, value in (("event", ids("e", 4)), ("admission_decision", None)):
            scene = honest_scene()
            if value is None:
                del scene.after["record_keys"][kind]
                del scene.after["types"][kind]
            else:
                scene.after["record_keys"][kind] = value
                scene.after["types"][kind] = len(value)
            scene.after["tables"]["records"] = sum(len(v) for v in scene.after["record_keys"].values())
            with self.subTest(kind):
                self.assertFalse(judged(uh.history_kept, scene))

    def test_two_old_provider_identities_deleted_while_the_setup_adds_two_fails(self):
        """The review's case: the count of the kind is unchanged (nine plus two new, less two old, plus two...) so a
        count against an allowance passes it; the identity of each earlier record does not."""
        scene = honest_scene()
        kept = sorted(scene.before["record_keys"]["provider_identity"])[2:]
        scene.after["record_keys"]["provider_identity"] = frozenset(kept) | ids("new", 4)  # two old gone, four new
        scene.after["types"]["provider_identity"] = len(scene.after["record_keys"]["provider_identity"])
        scene.after["tables"]["records"] = sum(len(v) for v in scene.after["record_keys"].values())
        self.assertEqual(scene.before["types"]["provider_identity"] + 2, scene.after["types"]["provider_identity"])
        self.assertFalse(judged(uh.history_kept, scene))

    def test_an_old_row_replaced_by_a_new_one_in_a_growth_table_fails(self):
        scene = honest_scene()
        requests = set(scene.after["keys"]["account_observation_requests"])
        requests.discard("q0")
        scene.after["keys"]["account_observation_requests"] = frozenset(requests)
        scene.after["tables"]["account_observation_requests"] = len(requests)
        self.assertFalse(judged(uh.history_kept, scene))

    def test_an_empty_earlier_ledger_proves_nothing(self):
        scene = honest_scene()
        scene.before = side({}, {}, {})
        self.assertFalse(judged(uh.history_kept, scene))


def long_scene():
    kinds = uh.ADMISSION_KINDS
    keys = {kind: ids(kind, 300) for kind in kinds}
    scene = SimpleNamespace(before={"record_keys": dict(keys)}, after={"record_keys": dict(keys)},
                            final={"record_keys": {kind: ids | {"x"} for kind, ids in keys.items()}},
                            final_recent_admission=602, probe_code=0, probe={"ready": True, "probe": "launched"})
    return scene


class AdmissionPreservedTests(unittest.TestCase):
    def test_the_honest_long_history_passes(self):
        self.assertTrue(judged(uh.admission_preserved, long_scene()))

    def test_admission_records_lost_by_the_update_fail(self):
        scene = long_scene()
        scene.after["record_keys"]["provider_headroom"] = frozenset()
        self.assertFalse(judged(uh.admission_preserved, scene))

    def test_admission_records_gone_by_the_time_the_project_dispatched_fail(self):
        scene = long_scene()
        scene.final["record_keys"]["provider_billing"] = frozenset()
        self.assertFalse(judged(uh.admission_preserved, scene))

    def test_no_more_than_512_records_in_the_window_when_the_project_opened_fail(self):
        scene = long_scene()
        scene.final_recent_admission = 512
        self.assertFalse(judged(uh.admission_preserved, scene))

    def test_a_probe_that_was_refused_on_the_first_attempt_fails(self):
        scene = long_scene()
        scene.probe_code, scene.probe = 2, None  # a retry that later worked is not recorded as the probe
        self.assertFalse(judged(uh.admission_preserved, scene))

    def test_the_probe_is_made_once_without_a_retry(self):
        source = (ROOT / "scripts" / "e2e_checks_update_history.py").read_text()
        self.assertNotIn("probe_tries", source)
        self.assertNotIn("probe_after_wait", source)


class RequirementTests(unittest.TestCase):
    def test_l12_6_is_split_and_the_second_part_is_deferred_with_its_three_cases(self):
        rows = {row["id"]: row for row in json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]}
        self.assertNotIn("L12.6", rows)
        self.assertEqual(["l12_6a_over_512_admission_records_and_128_mib_still_open_dispatch_and_upgrade"], rows["L12.6a"]["checks"])
        self.assertTrue(rows["L12.6b"]["deferred"] and rows["L12.6b"]["checks"] == [])
        for case in ("0.2.x", "busy probe", "protected copy"):
            self.assertIn(case, rows["L12.6b"]["reason"])
        self.assertIn("more than 512 admission records and a ledger over 128 MiB", rows["L12.6a"]["statement"])


class ReleaseTests(unittest.TestCase):
    def test_each_earlier_release_is_in_the_repository_and_is_not_this_build(self):
        for tag in uh.TAGS:
            with self.subTest(tag):
                self.assertNotEqual(uh.HEAD_VERSION, uh.tag_version(tag))

    def test_an_update_that_is_not_from_an_earlier_release_fails(self):
        scene = honest_scene()
        scene.world, scene.tag, scene.old_version = SimpleNamespace(), uh.TAGS[0], uh.HEAD_VERSION
        self.assertFalse(judged(uh.updated_and_full_run_passes, scene))


class FaultTests(unittest.TestCase):
    def test_every_fault_names_source_that_exists_exactly_once(self):
        for name, patches in uh.FAULTS.items():
            for patch in patches:
                with self.subTest(name, module=patch["module"]):
                    source = (ORCHESTRATION / (patch["module"].replace(".", "/") + ".py")).read_text()
                    self.assertEqual(1, source.count(patch["old"]))
                    self.assertNotEqual(patch["old"], patch["new"])

    def test_a_fault_is_chosen_only_by_the_runner(self):
        self.assertIsNone(uh.FAULT)
        self.assertEqual([], uh.fault_patches())


if __name__ == "__main__":
    unittest.main()
