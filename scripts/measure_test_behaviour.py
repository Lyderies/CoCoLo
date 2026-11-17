#!/usr/bin/env python3
"""Measure what every test module does, to sort the suite into unit, integration and e2e stages.

Each module runs in its own Python process with an audit hook that counts the
processes it starts (and how many are git, or the setup wizard and installer),
the sockets it connects or binds, and its sleeps and their total requested time.
Each row also has the CPU the module used (its process and the children it waited
for) and its process starts by kind (start_kind: product, fixture, python, git, wizard,
other). The result, one row per module, is written to scripts/test-behaviour.json with a
proposed stage:

- unit: no process, socket or sleep at all;
- e2e: drives the setup wizard or the installer as a real program;
- integration: everything else (real services, children, sockets, git, sandboxes).

Usage: measure_test_behaviour.py [--jobs N] [--suite root|codex|claude ...]
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parallel_tests import EXTRA_ENV, ROOT, SUITES, discover  # noqa: E402

OUTPUT = ROOT / "scripts" / "test-behaviour.json"
START_KINDS = ("product", "fixture", "python", "git", "wizard", "other")
TOP_CPU = 20


def start_kind(argv) -> str:
    """What one process start is (PLAN T8d-2): a product script, a test fixture, other Python, git,
    the setup wizard or installer, or anything else."""
    words = shlex.split(argv) if isinstance(argv, str) else [str(word) for word in argv]
    if not words:
        return "other"
    program = os.path.basename(words[0])
    if program == "git":
        return "git"
    if any(word.endswith("setup.sh") or os.path.basename(word) == "install.py" for word in words):
        return "wizard"
    if not program.lower().startswith("python"):
        return "other"
    index = 1
    while index < len(words):
        word = words[index]
        if word == "-c":
            return "python"
        if word == "-m":
            module = words[index + 1] if index + 1 < len(words) else ""
            return "fixture" if module.startswith("tests.") else "python"
        if word in ("-X", "-W"):
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        script = word.replace(os.sep, "/")
        if "/.codex/orchestration/" in script or "/scripts/" in script:
            return "product"
        return "python"
    return "python"


def cpu_summary(rows: dict) -> dict:
    """Total CPU, starts by kind, and the modules that used the most CPU (PLAN T8d-3)."""
    measured = {name: row for name, row in rows.items() if "error" not in row}
    starts = dict.fromkeys(START_KINDS, 0)
    for row in measured.values():
        for kind, count in row.get("starts", {}).items():
            starts[kind] = starts.get(kind, 0) + count
    top = sorted(measured.items(), key=lambda item: -item[1].get("cpu_seconds", 0.0))[:TOP_CPU]
    return {"cpu_seconds": round(sum(row.get("cpu_seconds", 0.0) for row in measured.values()), 2),
            "starts": starts,
            "top_cpu": [{"module": name, "cpu_seconds": row.get("cpu_seconds", 0.0), "tests": row.get("tests", 0),
                         "starts": row.get("starts", {})} for name, row in top]}
_PROBE = r'''
import json, os, resource, shlex, sys, time, unittest
from threading import local as thread_local
counts = {"processes": 0, "git": 0, "wizard": 0, "sockets": 0, "sleeps": 0, "sleep_seconds": 0.0,
          "starts": dict.fromkeys(%(kinds)r, 0)}
%(start_kind)s
_spawning = thread_local()
def hook(event, args):
    if event in ("subprocess.Popen", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.system"):
        if event == "subprocess.Popen":
            argv = args[1] if len(args) > 1 else args[0]
            _spawning.argv = [str(word) for word in argv] if isinstance(argv, (list, tuple)) else argv
        elif event == "os.posix_spawn" and len(args) > 1 and getattr(_spawning, "argv", None) is not None:
            same = [str(word) for word in args[1]] == _spawning.argv
            _spawning.argv = None
            if same:
                return  # The Popen just counted starts this process.
        counts["processes"] += 1
        if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
            kind = start_kind(args[1] if len(args) > 1 and isinstance(args[1], (list, tuple)) else args[0])
        elif event == "os.system":
            kind = start_kind(args[0])
        else:
            kind = "other"
        counts["starts"][kind] += 1
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
used = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
counts["cpu_seconds"] = round(sum(usage.ru_utime + usage.ru_stime for usage in used), 2)
with open(sys.argv[2], "w") as handle:
    json.dump(counts, handle)
'''


def stage(counts: dict) -> str:
    if counts.get("wizard"):
        return "e2e"
    if counts["processes"] == 0 and counts["sockets"] == 0 and counts["sleeps"] == 0:
        return "unit"
    return "integration"


def probe_source() -> str:
    return _PROBE % {"kinds": START_KINDS, "start_kind": inspect.getsource(start_kind)}


def measure_module(cwd: Path, module: str, extra_env: dict, scratch: Path) -> dict:
    """Run one test module in its own process from `cwd` and return what it did."""
    folder = Path(tempfile.mkdtemp(prefix="mb", dir=scratch))
    out = folder / "counts.json"
    environment = {**os.environ, **extra_env, "TMPDIR": str(folder)}
    subprocess.run([sys.executable, "-B", "-c", probe_source(), module, str(out)], cwd=cwd,
                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    try:
        return json.loads(out.read_text())
    except (OSError, ValueError):
        return {"error": "no measurement"}


def measure(suite: str, module: str, scratch: Path) -> tuple[str, dict]:
    return f"{suite}:{module}", measure_module(SUITES[suite], module, EXTRA_ENV.get(suite, {}), scratch)


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
    print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 1), "stages": summary,
                      "cpu": cpu_summary(rows)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
