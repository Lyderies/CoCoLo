#!/usr/bin/env python3
"""Measure what every test module does, to sort the suite into unit, integration and e2e stages.

Each module runs in its own Python process with an audit hook that counts the
processes it starts (and how many are git, or the setup wizard and installer),
the sockets it connects or binds, and its sleeps and their total requested time.
The result, one row per module, is written to scripts/test-behaviour.json with a
proposed stage:

- unit: no process, socket or sleep at all;
- e2e: drives the setup wizard or the installer as a real program;
- integration: everything else (real services, children, sockets, git, sandboxes).

Usage: measure_test_behaviour.py [--jobs N] [--suite root|codex|claude ...]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parallel_tests import EXTRA_ENV, ROOT, SUITES, discover  # noqa: E402

OUTPUT = ROOT / "scripts" / "test-behaviour.json"
_PROBE = r'''
import json, os, sys, time, unittest
counts = {"processes": 0, "git": 0, "wizard": 0, "sockets": 0, "sleeps": 0, "sleep_seconds": 0.0}
def hook(event, args):
    if event in ("subprocess.Popen", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.system"):
        counts["processes"] += 1
        words = " ".join(str(part) for part in (args[1] if event == "subprocess.Popen" and len(args) > 1
                                                and isinstance(args[1], (list, tuple)) else args))
        if "git" in words.split() or words.startswith("git ") or "/git " in words or words.endswith("/git"):
            counts["git"] += 1
        if "setup.sh" in words or "install.py" in words:
            counts["wizard"] += 1
    elif event in ("socket.connect", "socket.bind"):
        counts["sockets"] += 1
sys.addaudithook(hook)
real_sleep = time.sleep
def counted_sleep(seconds):
    counts["sleeps"] += 1
    counts["sleep_seconds"] += float(seconds)
    return real_sleep(seconds)
time.sleep = counted_sleep
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
started = time.monotonic()
result = unittest.main(module=None, argv=["tests", sys.argv[1]], exit=False).result
counts["seconds"] = round(time.monotonic() - started, 2)
counts["tests"] = result.testsRun
counts["failed"] = len(result.failures) + len(result.errors)
counts["sleep_seconds"] = round(counts["sleep_seconds"], 2)
with open(sys.argv[2], "w") as handle:
    json.dump(counts, handle)
'''


def stage(counts: dict) -> str:
    if counts.get("wizard"):
        return "e2e"
    if counts["processes"] == 0 and counts["sockets"] == 0 and counts["sleeps"] == 0:
        return "unit"
    return "integration"


def measure(suite: str, module: str, scratch: Path) -> tuple[str, dict]:
    folder = Path(tempfile.mkdtemp(prefix="mb", dir=scratch))
    out = folder / "counts.json"
    environment = {**os.environ, **EXTRA_ENV.get(suite, {}), "TMPDIR": str(folder)}
    subprocess.run([sys.executable, "-B", "-c", _PROBE, module, str(out)], cwd=SUITES[suite],
                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    try:
        counts = json.loads(out.read_text())
    except (OSError, ValueError):
        counts = {"error": "no measurement"}
    return f"{suite}:{module}", counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--suite", action="append", choices=sorted(SUITES))
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="measure", dir="/private/tmp" if sys.platform == "darwin" else None))
    work = [(suite, module) for suite in (args.suite or sorted(SUITES)) for module in sorted(discover(suite))]
    started = time.monotonic()
    with ThreadPoolExecutor(args.jobs) as pool:
        rows = dict(pool.map(lambda item: measure(*item, scratch), work))
    for row in rows.values():
        if "error" not in row:
            row["stage"] = stage(row)
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.is_file() else {}
    previous.update(rows)
    OUTPUT.write_text(json.dumps(dict(sorted(previous.items())), indent=1) + "\n")
    summary = {}
    for row in rows.values():
        key = row.get("stage", "error")
        entry = summary.setdefault(key, {"modules": 0, "tests": 0, "seconds": 0.0})
        entry["modules"] += 1
        entry["tests"] += row.get("tests", 0)
        entry["seconds"] = round(entry["seconds"] + row.get("seconds", 0.0), 1)
    print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 1), "stages": summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
