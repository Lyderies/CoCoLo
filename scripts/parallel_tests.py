#!/usr/bin/env python3
"""Run every test suite of this repository in parallel and report one combined result.

Each suite (the repository root, packages/codex-agents, packages/claude-agents) is split
into units: a test module, or for a module that takes long, small groups of its test
classes. Units run in separate Python processes, at most --jobs at a time, longest first,
each with its own temporary folder, so no two units share files, sockets or state.

Usage: parallel_tests.py [--jobs N] [--suite root|codex|claude ...] [--stage unit|integration|e2e ...]
                         [--module SUITE:MODULE ...] [--save-timings FILE]

Every test module has a stage in scripts/test-stages.json ({"suite:module": stage}):
unit (no process, socket or sleep; enforced: such a test fails), integration (real
services, children, sockets, git, sandboxes) or e2e (the setup wizard and the installer
run as programs). --stage runs only those stages; by default every stage runs. --module
runs only the named modules, whatever their stage, and is not combined with --suite or --stage.

The schedule comes from scripts/test-timings.json ({"suite:unit": seconds} from an earlier
run). --save-timings writes this run's measurements to FILE (pass that path again to
refresh the committed schedule). A failed unit's log tail is printed. Exit status is 0 only
when every unit passed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import test_framework as framework

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"root": ROOT, "codex": ROOT / "packages" / "codex-agents", "claude": ROOT / "packages" / "claude-agents"}
EXTRA_ENV = {"claude": {"PYTHONPATH": "src"}}
FRAMEWORK_SCRIPTS = Path(__file__).resolve().parent
REVIEW_BASE = None
_CURRENT_DISCOVERY = None
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
PROGRESS_SECONDS = 60
# With COCOLO_TEST_STAGE=unit, starting a process, opening a socket or sleeping raises, so
# a unit test that does any of them fails and names what it did.
_RUNNER = (
    "import sys\n"
    f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
    "from test_worker import main\n"
    "raise SystemExit(main())\n"
)


def discover(suite: str) -> dict[str, list[str]]:
    """Module name -> its test ids, named as a discovery of the suite's tests folder names them."""
    if _CURRENT_DISCOVERY is not None and suite in _CURRENT_DISCOVERY:
        return _CURRENT_DISCOVERY[suite]
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


def units(suite: str, timings: dict[str, float],
          only: set[str] | None = None) -> list[tuple[float, str, list[str]]]:
    """(expected seconds, unit name, unittest names) for one suite; with `only`, just those modules."""
    planned = []
    for module, tests in sorted(discover(suite).items()):
        if only is not None and module not in only:
            continue
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
STAGES = ROOT / "scripts" / "test-stages.json"
STAGE_NAMES = ("unit", "integration", "e2e")


def inventory():
    found = {}
    for suite, cwd in SUITES.items():
        code = (f"import sys,json; sys.path.insert(0,{str(FRAMEWORK_SCRIPTS)!r}); sys.path.insert(0,'.'); "
                f"from test_framework import inventory_suite; print(json.dumps(inventory_suite({suite!r})))")
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=cwd,
                                env={**os.environ, **EXTRA_ENV.get(suite, {})}, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'{suite}: test discovery failed: {result.stderr[-1500:]}')
        found.update(framework.decode_json(result.stdout))
    return found


def legacy_versions(root, current, ceiling):
    """Compare working files with committed introduction and every historical shrink transition."""
    errors = []
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    def version(commit, name):
        return framework.decode_json(git('show', commit + ':scripts/' + name))
    try:
        commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/test-legacy-ceiling.json').split()
        if not commits:
            raise ValueError('initial ceiling is not committed')
        original = version(commits[0], 'test-legacy-ceiling.json')
        if ceiling != original:
            errors.append('legacy initial ceiling changed')
        previous = version(REVIEW_BASE or 'HEAD', 'test-legacy.json')
        for commit in git('rev-list', 'HEAD', '--', 'scripts/test-legacy.json').split():
            this = version(commit, 'test-legacy.json')
            parent_ids = git('rev-list', '--parents', '-n', '1', commit).split()[1:]
            for parent in parent_ids:
                try:
                    before = version(parent, 'test-legacy.json')
                except subprocess.CalledProcessError:
                    before = original
                if this['baseline'] != before['baseline'] or any(before['tests'].get(k) != v for k, v in this['tests'].items()):
                    errors.append('legacy history grew or changed fingerprints at ' + commit[:12])
        return original, previous, errors
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return ceiling, {'baseline': current['baseline'], 'tests': {}}, ['cannot verify committed legacy history/review base']


def metadata():
    discovered = inventory()
    def read(name):
        return framework.decode_json((ROOT / 'scripts' / name).read_text())
    stages, layers = read('test-stages.json'), read('test-layers.json')
    catalogue, legacy, ceiling = read('test-requirements.json'), read('test-legacy.json'), read('test-legacy-ceiling.json')
    ceiling, previous, errors = legacy_versions(ROOT, legacy, ceiling)
    # Lifecycle checks use the same catalogue and remain required even in a module-only run.
    lifecycle = ROOT / 'scripts/e2e_lifecycle.py'
    checked = {}
    if lifecycle.exists():
        for node in ast.walk(ast.parse(lifecycle.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for tag in node.decorator_list:
                    if isinstance(tag, ast.Call) and isinstance(tag.func, ast.Name) and tag.func.id == 'check':
                        checked['lifecycle:' + node.name] = {'requirements': [arg.value for arg in tag.args],
                                                           'rationale': 'Canonical lifecycle scenario', 'fingerprint': ''}
    checked_inventory = dict(discovered)
    checked_stages, checked_layers = dict(stages), dict(layers)
    if checked:
        checked_inventory['lifecycle:checks'] = checked
        checked_stages['lifecycle:checks'], checked_layers['lifecycle:checks'] = 'e2e', 'scenario'
    errors += framework.validate_metadata(inventory=checked_inventory, stages=checked_stages, layers=checked_layers,
                                         catalogue=catalogue, legacy=legacy, ceiling=ceiling, previous=previous)
    return discovered, stages, layers, catalogue, errors


def stage_of(suite: str, unit: str, stages: dict[str, str]) -> str:
    """An undeclared module is rejected; selection never grants an implicit resource layer."""
    return stages[f"{suite}:{unit.split('#')[0]}"]


def select(planned: list[tuple[float, str, list[str]]], suite: str, wanted: set[str],
           stages: dict[str, str] | None = None) -> list[tuple[float, str, list[str]]]:
    """Only the units whose module is in one of the wanted stages."""
    stages = json.loads(STAGES.read_text()) if stages is None else stages
    return [unit for unit in planned if stage_of(suite, unit[1], stages) in wanted]


def shard(queue: list, index: int, count: int) -> list:
    """The units of shard `index` of `count` (PLAN T4a): longest first into the shard with the least
    recorded time, ties by name, so every runner computes the same split. Order is kept within a shard."""
    totals, members = [0.0] * count, [[] for _ in range(count)]
    for unit in sorted(queue, key=lambda unit: (-unit[0], unit[1], unit[2])):
        lightest = min(range(count), key=lambda position: (totals[position], position))
        totals[lightest] += unit[0]
        members[lightest].append(unit)
    return sorted(members[index - 1], reverse=True)


def parse_shard(text: str) -> tuple[int, int]:
    index, _, count = text.partition("/")
    if not (index.isdigit() and count.isdigit()) or not 1 <= int(index) <= int(count) <= 16:
        raise argparse.ArgumentTypeError(f"--shard wants K/N with 1 <= K <= N <= 16, got {text!r}")
    return int(index), int(count)


def run(jobs: int, suites: list[str], save: Path | None, keep: bool,
        wanted: set[str] = frozenset(STAGE_NAMES), modules: dict[str, set[str]] | None = None,
        part: tuple[int, int] = (1, 1), profile: str = 'ci') -> int:
    """Run the suites' units in the wanted stages; with `modules` ({suite: module names}), only those."""
    global _CURRENT_DISCOVERY
    _CURRENT_DISCOVERY = None
    timings = json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {}
    discovered, stages, layers, catalogue, metadata_errors = metadata()
    _CURRENT_DISCOVERY = {suite: {module.split(':', 1)[1]: [key.split(':', 1)[1] for key in cases]
                                 for module, cases in discovered.items() if module.startswith(suite + ':')}
                          for suite in SUITES}
    eligible = framework.select_layers(layers, profile)
    for error in metadata_errors:
        print('METADATA: ' + error, flush=True)
    if modules is not None:
        unknown = sorted(f"{suite}:{module}" for suite in suites for module in modules[suite] - set(discover(suite)))
        if unknown:
            print(f"no such test module: {', '.join(unknown)}", file=sys.stderr)
            return 2
    # Invalid layer/stage declarations fail preflight and never execute a body.
    runnable = {key for key in eligible if layers[key] in framework.STAGES and stages.get(key) in framework.STAGES[layers[key]]}
    for suite in _CURRENT_DISCOVERY:
        _CURRENT_DISCOVERY[suite] = {m: names for m, names in _CURRENT_DISCOVERY[suite].items() if suite + ':' + m in runnable}
    queue = sorted(((seconds, suite, name, names) for suite in suites
                    for seconds, name, names in select(
                        units(suite, timings, None if modules is None else modules[suite]), suite, wanted, stages)),
                   reverse=True)
    if part != (1, 1):
        queue = shard(queue, *part)
    selection = sorted(f'{suite}:{module}' for suite in suites for module in
                       (modules[suite] if modules is not None else discover(suite))
                       if f'{suite}:{module}' in runnable and stages[f'{suite}:{module}'] in wanted)
    base = Path(tempfile.mkdtemp(prefix="pt", dir="/private/tmp" if sys.platform == "darwin" else None))
    running, results, started = {}, [], time.monotonic()
    environment = {k: v for k, v in os.environ.items()
                   if not any(marker in k.upper() for marker in ("API_KEY", "ACCESS_TOKEN", "SECRET"))}
    counter, total, reported = 0, len(queue), time.monotonic()
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
                env={**environment, **EXTRA_ENV.get(suite, {}), "TMPDIR": str(folder),
                     "COCOLO_TEST_STAGE": stage_of(suite, name, stages),
                     "COCOLO_TEST_LAYER": layers[f'{suite}:{name.split("#")[0]}'],
                     "COCOLO_TEST_MODULE": f'{suite}:{name.split("#")[0]}',
                     "COCOLO_TEST_PROFILE": profile, "HYPOTHESIS_STORAGE_DIRECTORY": str(folder / 'hypothesis'),
                     "COCOLO_TEST_RESULTS": str(folder.with_suffix('.results.json'))},
                stdout=handle, stderr=subprocess.STDOUT)
            running[process] = (suite, name, names, log, handle, time.monotonic(), folder)
        time.sleep(0.05)
        if time.monotonic() - reported >= PROGRESS_SECONDS:
            # A passing run otherwise prints nothing until the end, which looks like a hang.
            reported = time.monotonic()
            now = ", ".join(f"{entry[0]}:{entry[1]} {reported - entry[5]:.0f}s"
                            for entry in sorted(running.values(), key=lambda e: e[5]))
            print(f"[{reported - started:.0f}s] {len(results)}/{total} units done, "
                  f"{sum(1 for r in results if r['returncode'] != 0)} failed; running: {now}", flush=True)
        for process, entry in running.items():
            if process.poll() is None and time.monotonic() - entry[5] > UNIT_SECONDS + 60:
                process.kill()  # It did not stop itself.
        for process in [p for p in running if p.poll() is not None]:
            suite, name, names, log, handle, began, folder = running.pop(process)
            handle.close()
            output = log.read_text(errors="replace")
            result_file = folder.with_suffix('.results.json')
            try:
                record = framework.decode_json(result_file.read_text())
                if not isinstance(record['tests'], int) or not isinstance(record['outcomes'], dict) or not isinstance(record['violations'], list):
                    raise ValueError('invalid worker result')
                returncode = process.returncode
            except (OSError, ValueError, KeyError, TypeError):
                record = {'tests': 0, 'outcomes': {}, 'violations': []}
                returncode = process.returncode or 1
            ran = record['tests']
            outcome = _RESULT.findall(output)
            results.append({"suite": suite, "unit": name, "names": names, "seconds": time.monotonic() - began,
                            "returncode": returncode, "tests": ran,
                            "summary": outcome[-1][1] if outcome else "", "log": str(log),
                            "timed_out": 'Timeout (' in output,
                            "outcomes": {suite + ':' + key: value for key, value in record['outcomes'].items()},
                            "violations": record['violations']})
            if returncode != 0:
                timed_out = "Timeout (" in output
                tail = "\n".join(output.strip().splitlines()[-(80 if timed_out else 25):])
                print(f"FAILED {suite}:{name} (exit {returncode}) -> {log}\n{tail}\n", flush=True)
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)
    elapsed = time.monotonic() - started
    fingerprints = {key: value.get('fingerprint', 'import_error') for cases in discovered.values() for key, value in cases.items()}
    detail = framework.measurements(results, layers, stages, jobs=jobs, elapsed=elapsed, selection=selection, part=part,
                                    profile=profile, catalogue=catalogue, fingerprints=fingerprints)
    detail['profile'], detail['metadata_errors'] = profile, metadata_errors
    outcomes = {key: value for row in results for key, value in row['outcomes'].items()}
    detail['requirements'] = framework.coverage(catalogue, outcomes)
    requirement_failures = [key for key, value in detail['requirements'].items() if value == 'failed' and
                            any(name in outcomes for name in catalogue[key]['checks'])]
    for entry in detail['over_budget']:
        print(f'OVER BUDGET: {entry["module"]} {entry["stage"]}/{entry["layer"]} '
              f'{entry["seconds"]:.3f}s > {entry["budget"]:.3f}s ({entry["fragments"]} fragments)', flush=True)
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
        Path(str(save) + '.run.json').write_text(json.dumps(detail, indent=1, sort_keys=True) + '\n')
    failed = [r for r in results if r["returncode"] != 0]
    totals = {suite: sum(r["tests"] for r in results if r["suite"] == suite) for suite in suites}
    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "jobs": jobs, "stages": sorted(wanted),
                      "job_seconds": round(detail['job_seconds'], 3), "over_budget": detail['over_budget'],
                      "metadata_errors": metadata_errors, "partial": detail['partial'], "profile": profile,
                      "requirement_failures": requirement_failures,
                      "units": len(results),
                      "tests": totals, "failed_units": [f"{r['suite']}:{r['unit']}" for r in failed],
                      "slowest_units": [(f"{r['suite']}:{r['unit']}", round(r["seconds"], 1))
                                        for r in sorted(results, key=lambda r: -r["seconds"])[:8]],
                      "logs": str(base)}, indent=1))
    if not failed and not keep:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if failed or metadata_errors or detail['over_budget'] or requirement_failures else 0


def main(argv: list[str] | None = None) -> int:
    global REVIEW_BASE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--suite", action="append", choices=sorted(SUITES))
    parser.add_argument("--stage", action="append", choices=STAGE_NAMES, help="run only these stages")
    parser.add_argument("--save-timings", type=Path, help="write this run's measurements here")
    parser.add_argument("--keep", action="store_true", help="keep every unit's folder and log")
    parser.add_argument('--profile', choices=('dev', 'ci', 'soak'), default='ci')
    parser.add_argument('--base', help='trusted review-base commit for the shrink-only legacy check')
    parser.add_argument('--combine-timings', nargs='+', type=Path, help='combine complete disjoint shard .run.json files')
    parser.add_argument("--shard", type=parse_shard, default=(1, 1), metavar="K/N",
                        help="run only shard K of N, balanced by recorded time (CI)")
    parser.add_argument("--module", action="append", metavar="SUITE:MODULE",
                        help="run only this test module (repeatable), for example codex:tests.test_bridge")
    args = parser.parse_args(argv)
    REVIEW_BASE = args.base
    if args.combine_timings:
        report = framework.combine_measurements([framework.decode_json(path.read_text()) for path in args.combine_timings])
        print(json.dumps(report, indent=1))
        return 1 if report['over_budget'] or report['metadata_errors'] or any(row['returncode'] for row in report['workers']) or 'failed' in report['requirements'].values() else 0
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    suites = args.suite or sorted(SUITES)
    modules = None
    if args.module:
        if args.suite or args.stage:
            parser.error("--module names its own suite and runs whatever its stage; do not combine it with --suite or --stage")
        modules = {suite: set() for suite in SUITES}
        for item in args.module:
            suite, _, module = item.partition(":")
            if suite not in SUITES or not module:
                parser.error(f"--module wants SUITE:MODULE, got {item!r}")
            modules[suite].add(module)
        suites = [suite for suite in suites if modules[suite]]
    return run(args.jobs, suites, args.save_timings, args.keep, set(args.stage or STAGE_NAMES), modules, args.shard, args.profile)


if __name__ == "__main__":
    sys.exit(main())
