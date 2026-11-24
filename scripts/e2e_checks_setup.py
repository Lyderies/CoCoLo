"""L1 (fresh install, setup again) and the scenes the G checks read too."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import ORCHESTRATION_VERSION, _SCENES, Phase, check  # noqa: E402


SETUP_STEPS = {1: "Checks", 2: "Project", 3: "Providers", 4: "Install", 5: "Collector", 6: "Billing",
               7: "Profile", 8: "Commands", 9: "Done"}
SUMMARY_LINE = re.compile(r"^\s*(OK|FAIL|SKIP)\s+([1-9])\. ([A-Za-z]+): ?(.*)$", re.MULTILINE)
# What a setup run again may add: the observations its own provider checks make, and nothing else.
# `records` is the ledger's own total over its record kinds (observations, admission evidence, attestations
# and the rest), so it is judged through the kinds, not on its own.
OWN_CHECK_TABLES = ("account_observation_origins", "account_observation_requests", "admission_evidence", "records")
OWN_CHECK_RECORDS = ("account_capacity.observations",)


def summary_states(text: str) -> dict:
    """The setup summary's last word on each step: {number: (state, label, note)}."""
    states = {}
    for state, number, label, note in SUMMARY_LINE.findall(text):
        states[int(number)] = (state, label, note.strip())
    return states


def fresh_scene():
    """L1: a fresh install through the scripted setup (S1), then what a run again is judged against."""
    if "fresh" not in _SCENES:
        world = e2e_support.World().__enter__()
        scene = SimpleNamespace(world=world)
        _SCENES["fresh"] = scene
        scene.run = world.setup(world.answers())
        scene.snapshot = world.snapshot()
    return _SCENES["fresh"]


def again_scene():
    """L1: setup run again with the same answers file on the installed project."""
    scene = fresh_scene()
    if not hasattr(scene, "again"):
        scene.again = scene.world.setup(scene.world.answers())
        scene.again_snapshot = scene.world.snapshot()
    return scene


def update_scene():
    """L1: `./setup.sh --update` on the installed project."""
    scene = again_scene()
    if not hasattr(scene, "update"):
        scene.before_update = scene.world.snapshot()
        scene.update = scene.world.update()
        scene.after_update = scene.world.snapshot()
    return scene


def changes_between(before: dict, after: dict) -> dict:
    """What a second run changed, as fixed names and counts: files, Git, and records by kind."""
    files = sorted(name for name in set(before["files"]) | set(after["files"])
                   if before["files"].get(name) != after["files"].get(name))
    records = sorted(kind for kind in set(before["records"]) | set(after["records"])
                     if before["records"].get(kind) != after["records"].get(kind)
                     and kind not in OWN_CHECK_RECORDS)
    tables = sorted(table for table in set(before["ledger"]) | set(after["ledger"])
                    if before["ledger"].get(table) != after["ledger"].get(table)
                    and table not in OWN_CHECK_TABLES)
    return {"files": files, "head": before["head"] != after["head"], "commits": before["commits"] != after["commits"],
            "dirty": after["dirty"] != "", "records": records, "tables": tables}


def expect_unchanged(phase: Phase, before: dict, after: dict) -> None:
    changed = changes_between(before, after)
    phase.expect(not changed["files"], "a second setup changed no file or digest")
    phase.expect(not changed["head"] and not changed["commits"], "a second setup made no commit")
    phase.expect(not changed["dirty"], "a second setup left the project clean")
    phase.expect(not changed["records"], "a second setup added no record the collector counts")
    for table in changed["tables"]:
        phase.reason("new_" + re.sub(r"[^a-z0-9_]", "_", table.lower()))
    phase.expect(not changed["tables"], "a second setup added no ledger record except its own checks")


# ---------------------------------------------------------------------------------------------
# L1: fresh install and setup again

@check("L1.1", group="fresh")
def l1_fresh_install_scripted_setup(phase: Phase) -> None:
    scene = fresh_scene()
    world, run = scene.world, scene.run
    phase.expect(not run.timed_out, "scripted setup finished by itself")
    phase.expect(run.code == 0, "scripted setup exits 0")
    states = summary_states(run.text)
    phase.expect(sorted(states) == list(SETUP_STEPS), "the summary names all nine steps")
    phase.expect(all(states[number][1] == name for number, name in SETUP_STEPS.items()), "the steps keep their names")
    failed = [number for number, (state, *_rest) in states.items() if state == "FAIL"]
    for number in failed:
        phase.reason("step_failed_" + SETUP_STEPS[number].lower())
    phase.expect(not failed, "no step failed")
    phase.expect({n: s[0] for n, s in states.items() if s[0] != "OK"} == {7: "SKIP"},
                 "every step is ok but the unchosen Codex profile")
    phase.expect(states[7][2] == "not chosen", "the profile step is skipped only because bridging was not chosen")
    phase.expect("installed and verified" in states[4][2] and "committed" in states[4][2],
                 "the install is verified and committed")
    # The protected command rule: its file, mode and protected copy of the runtime.
    rules = sorted((world.home / ".codex" / "rules").glob("agent-orchestration-*.rules"))
    phase.expect(len(rules) == 1, "exactly one protected command rule is installed")
    phase.expect(states[8][0] == "OK" and states[8][2] == "created", "setup says the rule was created")
    rule = rules[0]
    phase.expect(oct(rule.stat().st_mode & 0o777) == "0o600", "the rule file is private to its owner")
    phase.expect("prefix_rule" in rule.read_text(), "the rule file holds a command rule")
    copies = list((world.home / ".codex" / "agent-orchestration").glob("*/*/.codex/orchestration/cli.py"))
    phase.expect(len(copies) == 1, "the rule's protected copy of the orchestrator is in place")
    # The collector: ready on this build, with both providers attested.
    code, status, _ = world.cli("learning-status")
    phase.expect(code == 0 and status is not None, "the collector answers its status")
    version = (ORCHESTRATION_VERSION).read_text().strip()
    phase.expect(status.get("ready") is True, "the collector is ready")
    phase.expect(status.get("collector_runtime_version") == version, "the collector runs this build")
    phase.expect(status.get("collector_code_stale") is False, "the collector's code is not stale")
    attested = {name: (value or {}).get("state") for name, value in (status.get("provider_attestations") or {}).items()}
    phase.expect(attested == {"codex": "present", "claude": "present"}, "both providers are attested")
    phase.expect((status.get("owner_key") or {}).get("state") == "ok", "the owner key is enrolled")
    # The install itself: committed, clean, managed files present.
    phase.expect(scene.snapshot["dirty"] == "", "the project is clean after setup")
    phase.expect(scene.snapshot["commits"] == "2", "the install is one commit on the fixture's")
    manifest = world.project / ".codex" / "orchestration" / "install-manifest.json"
    phase.expect(manifest.is_file(), "the install manifest is in the project")


@check("L1.2", group="fresh")
def l1_setup_again_with_answers_changes_nothing(phase: Phase) -> None:
    scene = again_scene()
    run = scene.again
    phase.expect(not run.timed_out and run.code == 0, "setup run again exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the second run failed")
    expect_unchanged(phase, scene.snapshot, scene.again_snapshot)


@check("L1.2", group="fresh")
def l1_update_on_installed_project_changes_nothing(phase: Phase) -> None:
    scene = update_scene()
    run = scene.update
    phase.expect(not run.timed_out and run.code == 0, "setup --update on the installed project exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the update failed")
    expect_unchanged(phase, scene.before_update, scene.after_update)
