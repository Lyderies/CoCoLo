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


def honest_scene():
    before = {"tables": {"host_run_bindings": 2, "run_provenance": 2, "owner_attestations": 2, "records": 26,
                         "account_observation_requests": 9},
              "types": {"admission_decision": 2, "provider_identity": 9, "event": 5}}
    after = {"tables": {"host_run_bindings": 2, "run_provenance": 2, "owner_attestations": 2, "records": 30,
                        "account_observation_requests": 11, "run_holds": 6},
             "types": {"admission_decision": 2, "provider_identity": 11, "event": 5}}
    return SimpleNamespace(before=before, after=after)


class HistoryKeptTests(unittest.TestCase):
    def test_the_honest_update_keeps_history(self):
        self.assertTrue(judged(uh.history_kept, honest_scene()))

    def test_a_table_that_lost_a_row_fails(self):
        scene = honest_scene()
        scene.after["tables"]["run_provenance"] = 1
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_table_the_setup_does_not_add_to_but_gained_a_row_fails(self):
        scene = honest_scene()
        scene.after["tables"]["host_run_bindings"] = 3
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_table_the_setup_adds_to_may_not_gain_more_than_its_own_checks(self):
        scene = honest_scene()
        scene.after["tables"]["records"] = 26 + uh.SETUP_GROWTH_LIMIT + 1
        self.assertFalse(judged(uh.history_kept, scene))

    def test_a_record_kind_that_changed_or_vanished_fails(self):
        for kind, value in (("event", 4), ("admission_decision", None)):
            scene = honest_scene()
            if value is None:
                del scene.after["types"][kind]
            else:
                scene.after["types"][kind] = value
            with self.subTest(kind):
                self.assertFalse(judged(uh.history_kept, scene))

    def test_an_empty_earlier_ledger_proves_nothing(self):
        scene = honest_scene()
        scene.before = {"tables": {}, "types": {}}
        self.assertFalse(judged(uh.history_kept, scene))


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
