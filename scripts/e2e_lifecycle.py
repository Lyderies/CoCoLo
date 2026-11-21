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
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "tests" / "e2e" / "requirements.json"
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


def run_checks(flows: set[str], *, home_root: Path | None = None) -> dict:
    """Run every check of the selected flows; the report, as written by main()."""
    doc = requirements()
    selected = [item for item in doc["requirements"] if item["flow"] in flows or item["flow"] == "all"]
    wanted = sorted({name for item in selected for name in item["checks"]})
    results, decided = [], {}
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


def private_values_in(report_text: str, home_roots: list[str]) -> list[str]:
    """G8: names of private values found in the report (never the values themselves)."""
    found = []
    if any(root and root in report_text for root in home_roots):
        found.append("temporary_home_path")
    if re.search(r"/Users/|/home/", report_text):
        found.append("home_path")
    if re.search(r"SHA256:[A-Za-z0-9+/]{20,}|ssh-(ed25519|rsa) ", report_text):
        found.append("key_or_fingerprint")
    return found


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
    text = json.dumps(report, indent=1, sort_keys=True)
    leaked = private_values_in(text, [tempfile.gettempdir()])
    if leaked:
        report = {"schema_version": 1, "passed": False, "failure": "private_values_in_report", "found": leaked}
        text = json.dumps(report, indent=1, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
