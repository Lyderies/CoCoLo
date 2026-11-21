#!/usr/bin/env python3
"""COCOLO's strict lifecycle end-to-end test (PLAN-0.4.0.md T7), on a fake project.

One command, no prompts. Each flow (L1 to L15) is a named phase whose checks prove the
requirements in tests/e2e/requirements.json; `--flows L5,L9` runs only those, `--all` every one.
Strict by construction:

- every check must assert at least once (`Phase.expect`), or it fails;
- nothing is skipped: a missing tool, fake client or platform feature fails the check;
- every requirement of a selected flow must be decided by a check that ran;
- the report (G9) holds phases, seconds, reason codes and pass or fail per requirement, and no
  value from the run: it is checked for private values (G8) before it is written.

Usage: python3 -I -B scripts/e2e_lifecycle.py (--all | --flows L1,L5) [--report FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402

REQUIREMENTS = ROOT / "tests" / "e2e" / "requirements.json"
ORCHESTRATION_VERSION = ROOT / "packages" / "codex-agents" / "native-package" / "VERSION"
CHECKS: dict[str, tuple[tuple[str, ...], object]] = {}
# A reason code: what the report may carry from a run, besides counts and seconds.
REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")


class CheckFailed(AssertionError):
    """A check's expectation did not hold."""


def check(*requirement_ids: str):
    """Declare a check and the requirements it proves (read by tests/test_e2e_requirements.py)."""
    def register(function):
        CHECKS[function.__name__] = (requirement_ids, function)
        return function
    return register


class Phase:
    """What one check sees: its isolated environment, and the expectations it records."""

    def __init__(self, name: str, home: Path):
        self.name, self.home, self.expectations, self.reasons = name, home, 0, []

    def expect(self, condition: bool, what: str) -> None:
        """One assertion. `what` is fixed text naming it, never a value from the run."""
        self.expectations += 1
        if not condition:
            raise CheckFailed(what)

    def reason(self, code: str) -> None:
        """Note a reason code a step answered with, for the report."""
        if not REASON.fullmatch(code or ""):
            raise CheckFailed("a step answered with something that is not a reason code")
        self.reasons.append(code)


def requirements() -> dict:
    return json.loads(REQUIREMENTS.read_text())


def run_checks(flows: set[str], *, home_root: Path | None = None, keep_scenes: bool = False) -> dict:
    """Run every check of the selected flows; the report, as written by main()."""
    doc = requirements()
    selected = [item for item in doc["requirements"] if item["flow"] in flows or item["flow"] == "all"]
    wanted = sorted({name for item in selected for name in item["checks"]})
    results, decided = [], {}
    try:
        _run_each(wanted, selected, results, decided, home_root)
    finally:
        if not keep_scenes:
            close_scenes()
    return _verdicts(flows, selected, results, decided)


def _run_each(wanted, selected, results, decided, home_root) -> None:
    for name in wanted:
        entry = {"check": name, "requirements": [], "passed": False, "seconds": 0.0, "reasons": [],
                 "failure": None}
        started = time.monotonic()
        if name not in CHECKS:
            entry["failure"] = "check_missing"
        else:
            ids, function = CHECKS[name]
            entry["requirements"] = list(ids)
            with tempfile.TemporaryDirectory(prefix="cocolo-e2e-", dir=home_root) as home:
                phase = Phase(name, Path(home))
                try:
                    function(phase)
                    if phase.expectations == 0:
                        entry["failure"] = "check_asserted_nothing"
                    else:
                        entry["passed"] = True
                except CheckFailed as error:
                    entry["failure"] = "expectation_failed: " + _fixed(str(error))
                except Exception as error:  # Nothing is optional: anything else fails the check.
                    entry["failure"] = "check_error: " + type(error).__name__
                    traceback.print_exc(file=sys.stderr)
                entry["reasons"] = phase.reasons
        entry["seconds"] = round(time.monotonic() - started, 3)
        results.append(entry)
        for requirement in entry["requirements"] or [i["id"] for i in selected if name in i["checks"]]:
            decided[requirement] = decided.get(requirement, True) and entry["passed"]


def _verdicts(flows, selected, results, decided) -> dict:
    verdicts = {}
    for item in selected:
        if not item["checks"]:
            verdicts[item["id"]] = {"passed": False, "decided_by": [], "failure": "no_check"}
        else:
            verdicts[item["id"]] = {"passed": decided.get(item["id"], False), "decided_by": item["checks"],
                                    "failure": None if decided.get(item["id"], False) else "a_check_failed"}
    return {"schema_version": 1, "flows": sorted(flows), "checks": results, "requirements": verdicts,
            "passed": bool(verdicts) and all(v["passed"] for v in verdicts.values())}


def _fixed(text: str) -> str:
    """Only plain fixed text reaches the report."""
    return re.sub(r"[^A-Za-z0-9 ,.'_-]", "", text)[:160]


def private_values_in(report_text: str, home_roots: list[str], values=()) -> list[str]:
    """G8: names of private values found in the report (never the values themselves).

    `values` are the exact private values a run held (its test key, passphrase and scratch paths)."""
    found = []
    if any(root and root in report_text for root in home_roots):
        found.append("temporary_home_path")
    if any(value and value in report_text for value in values):
        found.append("run_private_value")
    if re.search(r"/Users/|/home/", report_text):
        found.append("home_path")
    if re.search(r"SHA256:[A-Za-z0-9+/]{20,}|ssh-(ed25519|rsa) ", report_text):
        found.append("key_or_fingerprint")
    return found


# ---------------------------------------------------------------------------------------------
# Scenes: the runs several checks read. Each is made once per process, on its own isolated World, and
# every World is stopped and removed when the run ends (run_checks).

_SCENES: dict = {}
SETUP_STEPS = {1: "Checks", 2: "Project", 3: "Providers", 4: "Install", 5: "Collector", 6: "Billing",
               7: "Profile", 8: "Commands", 9: "Done"}
SUMMARY_LINE = re.compile(r"^\s*(OK|FAIL|SKIP)\s+([1-9])\. ([A-Za-z]+): ?(.*)$", re.MULTILINE)
# What a setup run again may add: the observations its own provider checks make, and nothing else.
# `records` is the ledger's own total over its record kinds (observations, admission evidence, attestations
# and the rest), so it is judged through the kinds, not on its own.
OWN_CHECK_TABLES = ("account_observation_origins", "account_observation_requests", "admission_evidence", "records")
OWN_CHECK_RECORDS = ("account_capacity.observations",)


def close_scenes() -> None:
    for scene in _SCENES.values():
        scene.world.__exit__(None, None, None)
    _SCENES.clear()


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

@check("L1.1")
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


@check("L1.2")
def l1_setup_again_with_answers_changes_nothing(phase: Phase) -> None:
    scene = again_scene()
    run = scene.again
    phase.expect(not run.timed_out and run.code == 0, "setup run again exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the second run failed")
    expect_unchanged(phase, scene.snapshot, scene.again_snapshot)


@check("L1.2")
def l1_update_on_installed_project_changes_nothing(phase: Phase) -> None:
    scene = update_scene()
    run = scene.update
    phase.expect(not run.timed_out and run.code == 0, "setup --update on the installed project exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the update failed")
    expect_unchanged(phase, scene.before_update, scene.after_update)


# ---------------------------------------------------------------------------------------------
# Every flow: G1, G8, G9

@check("G1")
def g1_every_step_exits_zero_or_refuses_as_expected(phase: Phase) -> None:
    scene = update_scene()
    world = scene.world
    # A refusal that is expected: an answers file missing an answer is refused by name before anything changes.
    broken = world.answers(commit=None)
    refused = world.setup(broken, refuses=True)
    phase.expect(refused.code == 2, "an answers file missing an answer is refused with exit 2")
    phase.expect("answers file: commit: " in refused.text and "Nothing was changed." in refused.text,
                 "the refusal names the missing answer and says nothing changed")
    phase.reason("answers_file_refused")
    steps = world.steps
    phase.expect(len(steps) >= 6, "the run has setup runs and owner commands to judge")
    for step in steps:
        if step["refuses"]:
            continue
        phase.expect(not step["timed_out"], "no step waited for ever")
        phase.expect(step["code"] == 0, "every step that is not an expected refusal exits 0")
    seen = e2e_support.bare_unavailable("\n".join(world.outputs))
    for code in seen:
        phase.reason("bare_" + code)
    phase.expect(not seen, "no step answered with a bare run_unavailable, account_unavailable or probe_unavailable")
    # The detector itself: it flags a bare code and leaves a code with a cause alone.
    phase.expect(e2e_support.bare_unavailable('{"reason_code": "run_unavailable"}') == ["run_unavailable"],
                 "the check sees a bare run_unavailable")
    phase.expect(not e2e_support.bare_unavailable('{"reason_code": "run_unavailable_busy_probe"}'),
                 "the check leaves a reason that carries its cause")


@check("G8")
def g8_no_private_value_in_any_output(phase: Phase) -> None:
    scene = update_scene()
    world = scene.world
    leaked = set()
    for output in world.outputs:
        leaked.update(world.leaks(output))
    for name in sorted(leaked):
        phase.reason("leaks_" + name)
    phase.expect(len(world.outputs) >= 6, "the outputs of every step were collected")
    phase.expect(not leaked, "no output carries a key, a fingerprint, the passphrase or a path under the temporary home")
    phase.expect(world.passphrase not in "\n".join(world.outputs), "the passphrase is never shown")


@check("G9")
def g9_report_is_content_free_json_with_phases_seconds_reasons(phase: Phase) -> None:
    def passing(inner: Phase) -> None:
        inner.expect(True, "it holds")
        inner.reason("claim_busy")

    def failing(inner: Phase) -> None:
        inner.expect(False, "it does not hold")

    document = {"flows": {"L1": "x"}, "requirements": [
        {"id": "L1.1", "flow": "L1", "source": "s", "statement": "t", "checks": ["passing"]},
        {"id": "L1.2", "flow": "L1", "source": "s", "statement": "t", "checks": ["failing"]}]}
    saved = dict(CHECKS)
    try:
        CHECKS.clear()
        CHECKS.update(passing=(("L1.1",), passing), failing=(("L1.2",), failing))
        original = globals()["requirements"]
        globals()["requirements"] = lambda: document
        try:
            report = run_checks({"L1"}, keep_scenes=True)
        finally:
            globals()["requirements"] = original
    finally:
        CHECKS.clear()
        CHECKS.update(saved)
    destination = phase.home / "report.json"
    text = write_report(report, destination)
    written = json.loads(destination.read_text())
    phase.expect(written == json.loads(text) and written["schema_version"] == 1, "the report is written as JSON")
    phase.expect(sorted(written) == ["checks", "flows", "passed", "requirements", "schema_version"],
                 "the report has its fixed keys")
    phase.expect([c["check"] for c in written["checks"]] == ["failing", "passing"], "the report lists every phase")
    phase.expect(all(isinstance(c["seconds"], float) and c["seconds"] >= 0 for c in written["checks"]),
                 "every phase has its seconds")
    phase.expect(written["checks"][1]["reasons"] == ["claim_busy"], "reason codes are carried")
    phase.expect({k: v["passed"] for k, v in written["requirements"].items()} == {"L1.1": True, "L1.2": False},
                 "pass or fail is given per requirement")
    phase.expect(written["passed"] is False, "one failing requirement fails the report")
    phase.expect(not private_values_in(text, [tempfile.gettempdir(), str(phase.home)], e2e_support.PRIVATE_VALUES),
                 "the report carries no private value")
    # A report that did carry one is replaced by a refusal that names the kind only.
    leaky = dict(report, extra=str(phase.home))
    phase.expect(json.loads(write_report(leaky, None)).get("failure") == "private_values_in_report",
                 "a report with a private value in it is never written")
    # Kept as the CI artifact: a workflow step runs this script and uploads its report.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    phase.expect("e2e_lifecycle.py" in workflow and "--report" in workflow, "CI runs the lifecycle script with a report")
    phase.expect("upload-artifact" in workflow.split("e2e_lifecycle.py", 1)[-1], "CI keeps the report as an artifact")


def write_report(report: dict, destination: Path | None) -> str:
    """G9: the content-free JSON report, scanned for private values (G8) before it is written or shown."""
    text = json.dumps(report, indent=1, sort_keys=True)
    home_roots = [tempfile.gettempdir(), str(Path.home()), os.path.realpath(Path.home())]
    leaked = private_values_in(text, home_roots, e2e_support.PRIVATE_VALUES)
    if leaked:
        report = {"schema_version": 1, "passed": False, "failure": "private_values_in_report", "found": leaked}
        text = json.dumps(report, indent=1, sort_keys=True)
    if destination:
        destination.write_text(text + "\n")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true")
    which.add_argument("--flows")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    known = set(requirements()["flows"])
    flows = known if args.all else {flow.strip() for flow in args.flows.split(",")}
    if not flows <= known:
        parser.error("unknown flow: " + ",".join(sorted(flows - known)))
    report = run_checks(flows)
    text = write_report(report, args.report)
    print(text)
    return 0 if json.loads(text)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
