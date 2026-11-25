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

Usage: python3 -I -B scripts/e2e_lifecycle.py (--all | --flows L1,L5) [--report FILE] [--jobs N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import CHECK_GROUPS, CHECKS, GROUP_SECONDS, REASON, CheckFailed, Phase, check, close_scenes  # noqa: E402,F401

# The checks modules: importing them declares their checks (scripts/e2e_checks_<group>.py).
import e2e_checks_setup  # noqa: E402,F401
import e2e_checks_owner_key  # noqa: E402,F401
import e2e_checks_billing  # noqa: E402,F401
import e2e_checks_routing  # noqa: E402,F401
import e2e_checks_run  # noqa: E402,F401
import e2e_checks_general  # noqa: E402,F401
import e2e_checks_run_control  # noqa: E402,F401
import e2e_checks_recovery  # noqa: E402,F401
import e2e_checks_learning_guard  # noqa: E402,F401
import e2e_checks_core_api  # noqa: E402,F401
import e2e_checks_core_records  # noqa: E402,F401
import e2e_checks_update_safety  # noqa: E402,F401
import e2e_checks_update_history  # noqa: E402,F401

# Every slice's test-only product faults (T7 S1 run control, S2 recovery, S3 learning and guard, S5 update safety). A fault is chosen ONLY by the runner's
# explicit --fault argument; it is handed to the one module that owns it, every other module runs with none.
FAULT_MODULES = (e2e_checks_run_control, e2e_checks_recovery, e2e_checks_learning_guard, e2e_checks_core_api,
                 e2e_checks_core_records, e2e_checks_update_safety, e2e_checks_update_history)
FAULTS = sorted(name for module in FAULT_MODULES for name in module.FAULTS)


def select_fault(fault: str | None) -> None:
    """Hand `fault` (or None) to the checks module that declares it; clear it everywhere else."""
    if fault is not None and fault not in FAULTS:
        raise ValueError("unknown fault")
    for module in FAULT_MODULES:
        module.FAULT = fault if fault in module.FAULTS else None

REQUIREMENTS = ROOT / "tests" / "e2e" / "requirements.json"


def requirements() -> dict:
    return json.loads(REQUIREMENTS.read_text())


def run_checks(flows: set[str], *, home_root: Path | None = None, keep_scenes: bool = False, jobs: int = 1,
               unbuilt_all: bool = True, fault: str | None = None) -> dict:
    """Run every check of the selected flows; the report, as written by main().

    `jobs` above 1 runs the checks in that many processes, one scene group per process. `unbuilt_all` false
    leaves out the every-flow requirements that no check proves yet (a partial run's own decision; the
    caller says so), never a requirement of a selected flow."""
    doc = requirements()
    selected = [item for item in doc["requirements"] if (item["flow"] in flows or item["flow"] == "all")
                and (unbuilt_all or item["flow"] != "all" or item["checks"])]
    wanted = sorted({name for item in selected for name in item["checks"]})
    if jobs > 1:
        results = _execute_in_processes(wanted, jobs, fault=fault)
    else:
        select_fault(fault)
        try:
            results = _execute(wanted, home_root)
        finally:
            select_fault(None)
            if not keep_scenes:
                try:
                    close_scenes()
                finally:
                    e2e_support.close_worlds()
    decided = {}
    for entry in results:
        for requirement in entry["requirements"] or [i["id"] for i in selected if entry["check"] in i["checks"]]:
            decided[requirement] = decided.get(requirement, True) and entry["passed"]
    return _verdicts(flows, selected, results, decided)


def _execute(wanted, home_root) -> list:
    """Run the named checks in this process, in order; one entry each."""
    results = []
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
                mark = len(e2e_support.CAPTURE_BREAKS)
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
                # G1 and G8 at capture: a step this check ran broke a rule (in any World): the check fails and the
                # rule's requirement is decided failed with it.
                broken = sorted(set(e2e_support.CAPTURE_BREAKS[mark:]))
                if broken:
                    entry["passed"] = False
                    entry["failure"] = entry["failure"] or "capture_rule_broken"
                    entry["reasons"] = entry["reasons"] + broken
                    for prefix, requirement in (("g1_", "G1"), ("g8_", "G8")):
                        if any(code.startswith(prefix) for code in broken) and requirement not in entry["requirements"]:
                            entry["requirements"].append(requirement)
        entry["seconds"] = round(time.monotonic() - started, 3)
        results.append(entry)
    return results


def _execute_in_processes(wanted, jobs: int, *, fault: str | None = None) -> list:
    """Run the checks in `jobs` processes: the checks of one scene group stay together (they share one World),
    the groups that take longest start first. A process that dies fails its checks, never skips them."""
    groups: dict[str, list] = {}
    for name in wanted:
        groups.setdefault(CHECK_GROUPS.get(name, name), []).append(name)
    queue = sorted(groups.items(), key=lambda item: -GROUP_SECONDS.get(item[0], GROUP_SECONDS["default"]))
    with tempfile.TemporaryDirectory(prefix="cocolo-e2e-jobs-") as folder:
        owner = Path(folder) / "owner"
        owner.mkdir()
        e2e_support.e2e_processes.initialize(owner)
        guardian = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", str(Path(e2e_support.e2e_processes.__file__)), str(owner)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=e2e_support.child_environment(os.environ), start_new_session=True)

        def terminate(signum, frame):
            # Pipe EOF wakes the guardian before ThreadPoolExecutor waits for its workers.
            # Avoid acquiring cleanup locks in the interrupted frame.
            guardian.stdin.close()
            raise SystemExit(128 + signum)

        previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
        def run_group(item):
            _group, names = item
            out = Path(folder) / (re.sub(r"[^a-z0-9_]", "_", _group) + ".json")
            # The fault is explicit; the registry is harness metadata, stripped from product envs.
            done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                   "--execute", ",".join(names), "--results", str(out),
                                   "--owner-world", str(owner),
                                   *(["--fault", fault] if fault else [])],
                                  stdin=subprocess.DEVNULL,
                                  env={**e2e_support.child_environment(os.environ),
                                       "COCOLO_E2E_REGISTRY": str(owner / "worlds")})
            try:
                entries = json.loads(out.read_text())
            except (OSError, ValueError):
                entries = []
            return accept_worker(entries, names, done.returncode)
        try:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                gathered = [entry for entries in pool.map(run_group, queue) for entry in entries]
        finally:
            try:
                e2e_support.e2e_processes.cleanup(owner, remove=False)
                guardian.stdin.close()
                guardian.wait(timeout=8)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    return sorted(gathered, key=lambda entry: entry["check"])


def _failed_entry(name: str, failure: str) -> dict:
    return {"check": name, "requirements": list(CHECKS[name][0]) if name in CHECKS else [], "passed": False,
            "seconds": 0.0, "reasons": [], "failure": failure}


def accept_worker(entries, names: list[str], returncode: int) -> list:
    """A --jobs worker's entries, taken only from a clean exit and only whole: exactly one well-formed entry per
    check it was given. Anything else fails every check of the group (none is skipped or counted twice)."""
    if returncode != 0:
        failure = f"check_process_failed_{returncode}".replace("-", "signal_")
        return [_failed_entry(name, failure) for name in names]
    fields = {"check", "requirements", "passed", "seconds", "reasons", "failure"}
    if (not isinstance(entries, list) or not all(isinstance(e, dict) and set(e) == fields for e in entries)
            or sorted(e["check"] for e in entries) != sorted(names) or len(set(names)) != len(names)):
        return [_failed_entry(name, "check_results_invalid") for name in names]
    return entries


def redact_entries(entries: list, names: list[str]) -> list:
    """A worker's results that carried a private value are replaced whole: fixed fields, no reasons, no
    requirements the parent would decide passed (G8; the parent never sees this worker's private values)."""
    return [{"check": name, "requirements": [], "passed": False, "seconds": 0.0, "reasons": [],
             "failure": "private_values_in_report"} for name in names]


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
    job = re.split(r"\n  \S", workflow.split("e2e_lifecycle.py", 1)[-1], maxsplit=1)[0]
    phase.expect("if-no-files-found: error" in job.split("upload-artifact", 1)[-1],
                 "a missing report fails its CI step")


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
    which.add_argument("--execute", help=argparse.SUPPRESS)  # a --jobs worker: run these checks, write --results
    parser.add_argument("--report", type=Path)
    parser.add_argument("--results", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--jobs", type=int, default=1, help="processes to run the scene groups in (default 1)")
    parser.add_argument("--fault", choices=FAULTS,
                        help="inject this test-only product fault into its slice's scenes (T7 S1 run control, S2 recovery, S3 learning and guard, S5 update safety)")
    parser.add_argument("--owner-world", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.execute is not None:
        if args.owner_world is not None:
            e2e_support.e2e_processes.register(args.owner_world, os.getpid())
        return _worker(args.execute.split(","), args.results, fault=args.fault)
    known = set(requirements()["flows"])
    flows = known if args.all else {flow.strip() for flow in args.flows.split(",")}
    if not flows <= known:
        parser.error("unknown flow: " + ",".join(sorted(flows - known)))
    report = run_checks(flows, jobs=max(1, args.jobs), unbuilt_all=args.all, fault=args.fault)
    if not args.all:
        unbuilt = [i["id"] for i in requirements()["requirements"] if i["flow"] == "all" and not i["checks"]]
        if unbuilt:
            print("every-flow requirements with no check yet (not judged in a partial run; --all fails them): "
                  + ", ".join(unbuilt), file=sys.stderr)
    text = write_report(report, args.report)
    print(text)
    return 0 if json.loads(text)["passed"] else 1


def _worker(names: list[str], results: Path | None, *, fault: str | None = None) -> int:
    """One --jobs process: run the named checks and write their entries (content-free, scanned for private values).
    `fault` is the runner's explicit --fault argument, the only way a fault is selected."""
    select_fault(fault)
    try:
        entries = _execute(names, None)
    finally:
        select_fault(None)
        try:
            close_scenes()
        finally:
            e2e_support.close_worlds()
    text = json.dumps(entries)
    home_roots = [tempfile.gettempdir(), str(Path.home()), os.path.realpath(Path.home())]
    if private_values_in(text, home_roots, e2e_support.PRIVATE_VALUES):
        entries = redact_entries(entries, names)
    if results:
        results.write_text(json.dumps(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
