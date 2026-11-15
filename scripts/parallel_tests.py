#!/usr/bin/env python3
"""Run every test suite of this repository in parallel and report one combined result.

Each suite (the repository root, packages/codex-agents, packages/claude-agents) is split
into units: a test module, or for a module that takes long, small groups of its test
classes. Units run in separate Python processes, at most --jobs at a time, longest first,
each with its own temporary folder, so no two units share files, sockets or state.

Usage: parallel_tests.py [--jobs N] [--suite root|codex|claude ...] [--save-timings FILE]

The schedule comes from scripts/test-timings.json ({"suite:unit": seconds} from an earlier
run). --save-timings writes this run's measurements to FILE (pass that path again to
refresh the committed schedule). A failed unit's log tail is printed. Exit status is 0 only
when every unit passed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"root": ROOT, "codex": ROOT / "packages" / "codex-agents", "claude": ROOT / "packages" / "claude-agents"}
EXTRA_ENV = {"claude": {"PYTHONPATH": "src"}}
# A module expected to take longer than this is split into groups of its classes.
SPLIT_SECONDS = 20.0
UNKNOWN_SECONDS = 5.0  # Assumed for a unit with no earlier measurement.
_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_RESULT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?$", re.M)


# Loads and runs the named tests the way ci_unittest.py discovers them: with the suite's own
# folder and its tests folder importable, so module names match a discovery run.
# A unit that runs this long is stuck: it prints every thread's stack and exits, so the
# run names it and shows where it waits instead of hanging silently (the slowest takes ~1 min).
UNIT_SECONDS = 600
_RUNNER = (
    "import faulthandler, sys, unittest\n"
    f"faulthandler.dump_traceback_later({UNIT_SECONDS}, exit=True)\n"
    "sys.path.insert(0, '.')\n"
    "sys.path.insert(0, 'tests')\n"
    "unittest.main(module=None, argv=['tests', *sys.argv[1:]])\n"
)


def discover(suite: str) -> dict[str, list[str]]:
    """Module name -> its test ids, named as a discovery of the suite's tests folder names them."""
    cwd = SUITES[suite]
    code = (
        "import json, os, sys, unittest\n"
        "sys.path.insert(0, '.')\n"
        "top = '.' if os.path.exists('tests/__init__.py') else 'tests'\n"
        "found = {}\n"
        "def walk(s):\n"
        "    for t in s:\n"
        "        if isinstance(t, unittest.TestSuite):\n"
        "            walk(t)\n"
        "        elif type(t).__name__ == '_FailedTest':\n"
        "            found.setdefault(t.id().split('.')[-1], [])\n"
        "        else:\n"
        "            found.setdefault(type(t).__module__, []).append(t.id())\n"
        "walk(unittest.defaultTestLoader.discover('tests', top_level_dir=top))\n"
        "print(json.dumps(found))\n"
    )
    environment = {**os.environ, **EXTRA_ENV.get(suite, {})}
    output = subprocess.run([sys.executable, "-B", "-c", code], cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    return json.loads(output)


def units(suite: str, timings: dict[str, float]) -> list[tuple[float, str, list[str]]]:
    """(expected seconds, unit name, unittest names) for one suite."""
    planned = []
    for module, tests in sorted(discover(suite).items()):
        expected = timings.get(f"{suite}:{module}")
        if expected is not None and expected > SPLIT_SECONDS and len(tests) > 1:
            # Split into groups of whole tests near SPLIT_SECONDS each, slowest packed first.
            per_test = expected / len(tests)
            sized = sorted(((timings.get(f"{suite}:{test}", per_test), test) for test in tests), reverse=True)
            count = max(2, round(expected / SPLIT_SECONDS))
            groups = [[0.0, []] for _ in range(count)]
            for guess, test in sized:
                smallest = min(groups, key=lambda group: group[0])
                smallest[0] += guess
                smallest[1].append(test)
            for index, (size, names) in enumerate(g for g in groups if g[1]):
                planned.append((size, f"{module}#{index}", sorted(names)))
        else:
            planned.append((expected if expected is not None else UNKNOWN_SECONDS, module, [module]))
    return planned


TIMINGS = ROOT / "scripts" / "test-timings.json"


def run(jobs: int, suites: list[str], save: Path | None, keep: bool) -> int:
    timings = json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {}
    queue = sorted(((seconds, suite, name, names) for suite in suites
                    for seconds, name, names in units(suite, timings)), reverse=True)
    base = Path(tempfile.mkdtemp(prefix="pt", dir="/private/tmp" if sys.platform == "darwin" else None))
    running, results, started = {}, [], time.monotonic()
    environment = {k: v for k, v in os.environ.items()
                   if not any(marker in k.upper() for marker in ("API_KEY", "ACCESS_TOKEN", "SECRET"))}
    counter = 0
    while queue or running:
        while queue and len(running) < jobs:
            expected, suite, name, names = queue.pop(0)
            counter += 1
            # Short: Unix socket paths built under it must stay under the platform limit.
            folder = base / str(counter)
            folder.mkdir()
            log = folder.with_suffix(".log")
            handle = log.open("wb")
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", _RUNNER, *names], cwd=SUITES[suite],
                env={**environment, **EXTRA_ENV.get(suite, {}), "TMPDIR": str(folder)},
                stdout=handle, stderr=subprocess.STDOUT)
            running[process] = (suite, name, names, log, handle, time.monotonic(), folder)
        time.sleep(0.05)
        for process, entry in running.items():
            if process.poll() is None and time.monotonic() - entry[5] > UNIT_SECONDS + 60:
                process.kill()  # It did not stop itself.
        for process in [p for p in running if p.poll() is not None]:
            suite, name, names, log, handle, began, folder = running.pop(process)
            handle.close()
            output = log.read_text(errors="replace")
            ran = sum(int(n) for n in _RAN.findall(output))
            outcome = _RESULT.findall(output)
            results.append({"suite": suite, "unit": name, "names": names, "seconds": time.monotonic() - began,
                            "returncode": process.returncode, "tests": ran,
                            "summary": outcome[-1][1] if outcome else "", "log": str(log)})
            if process.returncode != 0:
                timed_out = "Timeout (" in output
                tail = "\n".join(output.strip().splitlines()[-(80 if timed_out else 25):])
                print(f"FAILED {suite}:{name} (exit {process.returncode}) -> {log}\n{tail}\n", flush=True)
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)
    elapsed = time.monotonic() - started
    measured = dict(timings)
    for item in results:
        if "#" not in item["unit"]:
            measured[f"{item['suite']}:{item['unit']}"] = round(item["seconds"], 2)
        else:  # Spread a group's time over its tests, for the next split.
            for test in item["names"]:
                measured[f"{item['suite']}:{test}"] = round(item["seconds"] / len(item["names"]), 2)
            module = item["unit"].split("#")[0]
            measured[f"{item['suite']}:{module}"] = round(sum(
                r["seconds"] for r in results if r["unit"].split("#")[0] == module and r["suite"] == item["suite"]), 2)
    if save is not None:
        save.write_text(json.dumps(measured, indent=1, sort_keys=True) + "\n")
    failed = [r for r in results if r["returncode"] != 0]
    totals = {suite: sum(r["tests"] for r in results if r["suite"] == suite) for suite in suites}
    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "jobs": jobs, "units": len(results),
                      "tests": totals, "failed_units": [f"{r['suite']}:{r['unit']}" for r in failed],
                      "slowest_units": [(f"{r['suite']}:{r['unit']}", round(r["seconds"], 1))
                                        for r in sorted(results, key=lambda r: -r["seconds"])[:8]],
                      "logs": str(base)}, indent=1))
    if not failed and not keep:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--suite", action="append", choices=sorted(SUITES))
    parser.add_argument("--save-timings", type=Path, help="write this run's measurements here")
    parser.add_argument("--keep", action="store_true", help="keep every unit's folder and log")
    args = parser.parse_args(argv)
    return run(args.jobs, args.suite or sorted(SUITES), args.save_timings, args.keep)


if __name__ == "__main__":
    sys.exit(main())
