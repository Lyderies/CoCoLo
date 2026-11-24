#!/usr/bin/env python3
"""Run every test suite of this repository in parallel and report one combined result.

Each suite (the repository root, packages/codex-agents, packages/claude-agents) is split
into units: a test module, or for a module that takes long, small groups of its test
classes. Units run in separate Python processes, at most --jobs at a time, longest first,
each with its own temporary folder, so no two units share files, sockets or state.

Usage: parallel_tests.py [--jobs N] [--suite root|codex|claude ...] [--stage unit|integration|e2e ...]
                         [--module SUITE:MODULE ...] [--save-timings FILE]
                         [--record-coverage | --affected BASE]

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
import signal
import subprocess
import sys
import tempfile
import time
import uuid

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


WORKER_ENVIRONMENT = frozenset({
    'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LANGUAGE', 'TZ',
    'LC_ALL', 'LC_CTYPE', 'LC_COLLATE', 'LC_MESSAGES', 'LC_MONETARY', 'LC_NUMERIC', 'LC_TIME',
    'PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONHASHSEED', 'PYTHONDONTWRITEBYTECODE',
    'COCOLO_WORKER_TIMEOUT', 'HYPOTHESIS_STORAGE_DIRECTORY',
    # Keep coverage.py's subprocess patch active through nested test runners.
    'COVERAGE_PROCESS_CONFIG',
})


def worker_environment():
    """Decision 100: shared local/CI discovery and worker allowlist."""
    return {key: value for key, value in os.environ.items()
            if key in WORKER_ENVIRONMENT or key.startswith('COCOLO_TEST_')}


def run_record_path(save=None):
    if save is not None:
        return Path(str(save) + '.run.json')
    explicit = os.environ.get('COCOLO_TEST_RUN_RECORD')
    if explicit:
        return Path(explicit)
    return Path(tempfile.mkdtemp(prefix='cocolo-test-record-')) / 'run.json'


def persist_record(detail, save=None):
    path = run_record_path(save)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(detail, indent=1, sort_keys=True) + '\n')
    print('RUN RECORD: ' + str(path), flush=True)
    return path


def discover(suite: str) -> dict[str, list[str]]:
    """Module name -> its test ids, named as a discovery of the suite's tests folder names them."""
    if _CURRENT_DISCOVERY is not None and suite in _CURRENT_DISCOVERY:
        return _CURRENT_DISCOVERY[suite]
    cwd = SUITES[suite]
    code = (
        "import json, os, sys, unittest\n"
        f"sys.path.insert(0, {str(FRAMEWORK_SCRIPTS)!r})\n"
        "sys.path.insert(0, '.')\n"
        "top = '.' if os.path.exists('tests/__init__.py') else 'tests'\n"
        "found = {}\n"
        "def walk(s):\n"
        "    for t in s:\n"
        "        if isinstance(t, unittest.TestSuite):\n"
        "            walk(t)\n"
        "        elif type(t).__name__ == '_FailedTest':\n"
        "            found.setdefault(t._testMethodName, [])\n"
        "        else:\n"
        "            found.setdefault(type(t).__module__, []).append(t.id())\n"
        "walk(unittest.defaultTestLoader.discover('tests', top_level_dir=top))\n"
        "print(json.dumps(found))\n"
    )
    environment = {**worker_environment(), **EXTRA_ENV.get(suite, {})}
    output = subprocess.run([sys.executable, "-B", "-c", code], cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    return json.loads(output)


# Real persistence campaigns are I/O intensive. Preserve their entire module in one
# worker and give it an empty pool; default job capacity remains available elsewhere.
def exclusive(module):
    return module.split('#')[0].startswith('codex:tests.test_merge_library_')


def can_launch(module, running, jobs):
    return len(running) < jobs and (not running or
        not exclusive(module) and not any(exclusive(name) for name in running))


def units(suite: str, timings: dict[str, float],
          only: set[str] | None = None) -> list[tuple[float, str, list[str]]]:
    """(expected seconds, unit name, unittest names) for one suite; with `only`, just those modules."""
    planned = []
    for module, tests in sorted(discover(suite).items()):
        if only is not None and module not in only:
            continue
        expected = timings.get(f"{suite}:{module}")
        if not exclusive(suite + ":" + module) and expected is not None and expected > SPLIT_SECONDS and len(tests) > 1:
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
    layers = framework.decode_json((ROOT / 'scripts/test-layers.json').read_text())
    for suite, cwd in SUITES.items():
        code = (f"import sys,json; sys.path.insert(0,{str(FRAMEWORK_SCRIPTS)!r}); sys.path.insert(0,'.'); "
                f"from test_framework import inventory_suite; print(json.dumps(inventory_suite({suite!r}, {layers!r})))")
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=cwd,
                                env={**worker_environment(), **EXTRA_ENV.get(suite, {})}, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'{suite}: test discovery failed: {result.stderr[-1500:]}')
        found.update(framework.decode_json(result.stdout))
    return found


DISCOVERY_SECONDS = 0.

def lifecycle_checks():
    """{check name: requirement IDs} from the @check(...) tags of scripts/e2e_lifecycle.py and its
    scripts/e2e_checks_<group>.py modules, read without running anything."""
    found = {}
    for lifecycle in (ROOT / 'scripts/e2e_lifecycle.py', *sorted((ROOT / 'scripts').glob('e2e_checks_*.py'))):
        if lifecycle.exists():
            for node in ast.walk(ast.parse(lifecycle.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for tag in node.decorator_list:
                        if isinstance(tag, ast.Call) and isinstance(tag.func, ast.Name) and tag.func.id == 'check':
                            if node.name in found:
                                found[node.name] = found[node.name] + ['<duplicate check name>']
                            else:
                                found[node.name] = [arg.value for arg in tag.args if isinstance(arg, ast.Constant)]
    return found


def lifecycle_link_errors(declared=None, rows=None):
    """Reciprocal links between the lifecycle checks and tests/e2e/requirements.json, the lifecycle authority."""
    declared = lifecycle_checks() if declared is None else declared
    if rows is None:
        rows = framework.decode_json((ROOT / 'tests/e2e/requirements.json').read_text())['requirements']
    by_id = {row['id']: row for row in rows}
    errors = []
    for name, ids in sorted(declared.items()):
        if not ids:
            errors.append(f'lifecycle:{name}: names no requirement')
        for requirement in ids:
            if requirement not in by_id:
                errors.append(f'lifecycle:{name}: unknown lifecycle requirement {requirement!r}')
            elif name not in by_id[requirement]['checks']:
                errors.append(f'lifecycle:{name}: {requirement} does not list its check')
    for row in rows:
        if len(row['checks']) != len(set(row['checks'])):
            errors.append(f"{row['id']}: duplicate lifecycle check")
        for name in row['checks']:
            if row['id'] not in declared.get(name, []):
                errors.append(f"{row['id']}: missing or unlinked lifecycle check {name}")
    return errors


def metadata():
    global DISCOVERY_SECONDS
    DISCOVERY_SECONDS = 0.
    def read(name):
        return framework.decode_json((ROOT / 'scripts' / name).read_text())
    stages, layers = read('test-stages.json'), read('test-layers.json')
    catalogue = read('test-requirements.json')
    errors = []
    discovery_started = time.monotonic()
    try:
        discovered = inventory()
    finally:
        DISCOVERY_SECONDS = time.monotonic() - discovery_started
    # Lifecycle checks are linked in tests/e2e/requirements.json, their own authority, and validated against it
    # apart from the shared catalogue: the required jobs never run them (the lifecycle CI job is advisory), so
    # their links must not decide the required jobs' metadata validation (T7 S0 review finding 1).
    checked_inventory = dict(discovered)
    checked_stages, checked_layers = dict(stages), dict(layers)
    errors += lifecycle_link_errors()
    registry_path = ROOT / 'scripts/test-checks.json'
    if registry_path.exists():
        registry = framework.decode_json(registry_path.read_text())
        checked_inventory['registered:checks'] = {
            name: {'requirements': check['requirements'], 'rationale': 'Registered executable or review evidence'}
            for name, check in registry.items()}
        checked_stages['registered:checks'], checked_layers['registered:checks'] = 'e2e', 'scenario'
        for name, check in registry.items():
            if check.get('kind') not in ('executable', 'review'):
                errors.append(name + ': unknown registered check kind')
    errors += framework.validate_sources(ROOT, catalogue)
    errors += framework.validate_metadata(inventory=checked_inventory, stages=checked_stages, layers=checked_layers,
                                         catalogue=catalogue)
    for module, cases in discovered.items():
        if not cases:
            continue
        suite, name = module.split(':', 1)
        source = SUITES[suite] / (('tests/' if suite == 'root' else '') + name.replace('.', '/') + '.py')
        if source.exists():
            # A lint finding for review, printed every run, not a run failure (decision 126.4).
            for error in framework.golden_bypasses(source.read_text(), filename=source):
                print('GOLDEN WARNING: ' + module + ': ' + error, flush=True)
    entrypoints = ROOT / 'scripts/test-entrypoints.json'
    entries = framework.decode_json(entrypoints.read_text()) if entrypoints.exists() else {}
    check_layers = {name: layers.get(module) for module, cases in discovered.items() for name in cases}
    errors += framework.validate_entrypoints(ROOT, entries, check_layers)
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




def reap_worker_group(process):
    """Only signal the process group created for this worker; teardown is bounded."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

def preflight_failure(message, jobs, save, started):
    elapsed = time.monotonic() - started
    detail = framework.measurements([], {}, {}, jobs=jobs, elapsed=elapsed, selection=[],
                                    discovery_seconds=DISCOVERY_SECONDS,
                                    preflight_seconds=max(0., elapsed - DISCOVERY_SECONDS))
    detail.update(metadata_errors=[message], failed_units=[], tests={}, requirements={}, units=0)
    persist_record(detail, save)
    print(json.dumps(detail, indent=1), flush=True)
    return 1

def run(jobs: int, suites: list[str], save: Path | None, keep: bool,
        wanted: set[str] = frozenset(STAGE_NAMES), modules: dict[str, set[str]] | None = None,
        part: tuple[int, int] = (1, 1), profile: str = 'ci', *,
        record_coverage: bool = False, affected_base: str | None = None) -> int:
    """Run the suites' units in the wanted stages; with `modules` ({suite: module names}), only those."""
    global _CURRENT_DISCOVERY
    recording_started = time.monotonic() if record_coverage else None
    _CURRENT_DISCOVERY = None
    timings = json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {}
    preflight_started = time.monotonic()
    try:
        discovered, stages, layers, catalogue, metadata_errors = metadata()
        debt_path = ROOT/'scripts/test-budget-debt-0850.json'
        if not debt_path.exists():
            debt_path = ROOT/'scripts/test-budget-debt.json'
        budget_debt = framework.decode_json(debt_path.read_text())
    except (ValueError, OSError, KeyError, TypeError) as error:
        return preflight_failure(str(error), jobs, save, preflight_started)
    preflight_seconds = time.monotonic() - preflight_started - DISCOVERY_SECONDS
    _CURRENT_DISCOVERY = {suite: {module.split(':', 1)[1]: [key.split(':', 1)[1] for key in cases]
                                 for module, cases in discovered.items() if module.startswith(suite + ':')}
                          for suite in SUITES}
    eligible = framework.select_layers(layers, profile)
    for error in metadata_errors:
        print('METADATA: ' + error, flush=True)
    if modules is not None:
        unknown = sorted(f"{suite}:{module}" for suite in suites for module in modules[suite] - set(discover(suite)))
        if unknown:
            return preflight_failure('no such test module: ' + ', '.join(unknown), jobs, save, preflight_started)
        contradictory = sorted(f'{suite}:{module}' for suite in suites for module in modules[suite]
                               if layers.get(f'{suite}:{module}') == 'soak' and profile != 'soak')
        if contradictory:
            return preflight_failure('explicit soak module requires --profile soak: ' + ', '.join(contradictory),
                                     jobs, save, preflight_started)
    # Invalid layer/stage declarations fail preflight and never execute a body.
    runnable = {key for key in eligible if layers[key] in framework.STAGES and stages.get(key) in framework.STAGES[layers[key]]}
    for suite in _CURRENT_DISCOVERY:
        _CURRENT_DISCOVERY[suite] = {m: names for m, names in _CURRENT_DISCOVERY[suite].items() if suite + ':' + m in runnable}
    if affected_base is not None:
        from test_affected import affected
        sources = {key: (SUITES[suite] / (('tests/' if suite == 'root' else '') +
                                        name.replace('.', '/') + '.py')).relative_to(ROOT).as_posix()
                   for key in runnable & discovered.keys() for suite, name in [key.split(':', 1)]}
        selected, full_reasons = affected(ROOT, affected_base, sources)
        for reason in full_reasons:
            print('AFFECTED FULL RUN: ' + reason, flush=True)
        for module, reasons in sorted(selected.items()):
            print('AFFECTED ' + module + ': ' + '; '.join(reasons), flush=True)
        if not selected:
            print('AFFECTED: no test modules selected (no recorded dependencies changed)', flush=True)
        modules = {suite: {key.split(':', 1)[1] for key in selected if key.startswith(suite + ':')}
                   for suite in suites}
    queue = sorted(((seconds, suite, name, names) for suite in suites
                    for seconds, name, names in select(
                        units(suite, timings, None if modules is None else modules[suite]), suite, wanted, stages)),
                   reverse=True)
    expected_fragments = {}
    for _, suite, name, names in queue:
        expected_fragments.setdefault(suite + ':' + name.split('#')[0], []).append(name)
    if part != (1, 1):
        queue = shard(queue, *part)
    selection = sorted(f'{suite}:{module}' for suite in suites for module in
                       (modules[suite] if modules is not None else discover(suite))
                       if f'{suite}:{module}' in runnable and stages[f'{suite}:{module}'] in wanted)
    base = Path(tempfile.mkdtemp(prefix="pt", dir="/private/tmp" if sys.platform == "darwin" else None))
    recorder = None
    if record_coverage:
        from test_affected import CoverageRecorder, git
        try:
            recording_commit = git(ROOT, 'rev-parse', 'HEAD').strip()
            recorder = CoverageRecorder(ROOT, base, selection)
        except (ImportError, OSError, subprocess.CalledProcessError) as error:
            return preflight_failure('cannot record coverage: ' + str(error), jobs, save, preflight_started)
    running, results, started = {}, [], time.monotonic()
    environment = worker_environment()
    worker_timeout = float(os.environ.get('COCOLO_WORKER_TIMEOUT', UNIT_SECONDS))
    timed_out_workers = set()
    counter, total, reported = 0, len(queue), time.monotonic()
    try:
        while queue or running:
            while queue and can_launch(queue[0][1] + ':' + queue[0][2],
                                      [entry[0] + ':' + entry[1] for entry in running.values()], jobs):
                expected, suite, name, names = queue.pop(0)
                counter += 1
                # Short: Unix socket paths built under it must stay under the platform limit.
                folder = base / str(counter)
                folder.mkdir()
                log = folder.with_suffix(".log")
                handle = log.open("wb")
                command = (recorder.command(folder, names) if recorder is not None else
                           [sys.executable, "-B", "-c", _RUNNER, *names])
                process = subprocess.Popen(
                    command, cwd=SUITES[suite],
                    env={**environment, **EXTRA_ENV.get(suite, {}), "TMPDIR": str(folder),
                         "COCOLO_TEST_STAGE": stage_of(suite, name, stages),
                         "COCOLO_TEST_LAYER": layers[f'{suite}:{name.split("#")[0]}'],
                         "COCOLO_TEST_MODULE": f'{suite}:{name.split("#")[0]}',
                         "COCOLO_WORKER_TIMEOUT": str(worker_timeout),
                         "COCOLO_TEST_PROFILE": profile, "HYPOTHESIS_STORAGE_DIRECTORY": str(folder / 'hypothesis'),
                         "COCOLO_TEST_RESULTS": str(folder.with_suffix('.results.json'))},
                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
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
                if process.poll() is None and time.monotonic() - entry[5] > worker_timeout + .1:
                    timed_out_workers.add(process.pid)
                    reap_worker_group(process)  # Includes owned children if the worker could not stop itself.
                    entry[4].write(f"\nTIMED OUT {entry[0]}:{entry[1]} after {worker_timeout:g}s; owned process group reaped\n".encode())
                    entry[4].flush()
            for process in [p for p in running if p.poll() is not None]:
                suite, name, names, log, handle, began, folder = running.pop(process)
                reap_worker_group(process)
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
                                "timed_out": process.pid in timed_out_workers or (time.monotonic() - began >= worker_timeout and not result_file.exists()),
                                "log_tail": "\n".join(output.splitlines()[-80:]),
                                "outcomes": {suite + ':' + key: value for key, value in record['outcomes'].items()},
                                "violations": record['violations']})
                if returncode != 0:
                    timed_out = results[-1]['timed_out']
                    tail = "\n".join(output.strip().splitlines()[-(80 if timed_out else 25):])
                    print(f"FAILED {suite}:{name} (exit {returncode}) -> {log}\n{tail}\n", flush=True)
                if recorder is not None:
                    if returncode != 0:
                        # A module failing under coverage (e.g. it checks that a child loads no sqlite) may
                        # record too few files: it is selected on every Python change (review MED-2).
                        recorder.opaque.add(suite + ':' + name.split('#')[0])
                    try:
                        recorder.collect(folder, suite + ':' + name.split('#')[0])
                    except (OSError, ValueError, recorder.coverage.exceptions.CoverageException) as error:
                        metadata_errors.append(f'coverage collection failed for {suite}:{name}: {error}')
                if not keep:
                    shutil.rmtree(folder, ignore_errors=True)
    except BaseException as error:
        detail = framework.measurements(results, layers, stages, jobs=jobs, elapsed=time.monotonic() - started,
                                        selection=selection, expected_fragments=expected_fragments)
        detail['metadata_errors'] = ['runner interrupted: ' + type(error).__name__]
        detail['pending_workers'] = [{'suite': e[0], 'unit': e[1], 'log': str(e[3])} for e in running.values()]
        persist_record(detail, save)
        raise
    finally:
        for process, entry in running.items():
            reap_worker_group(process)
            process.wait(timeout=2)
            entry[4].close()
    elapsed = time.monotonic() - started
    recording_seconds = None
    if recorder is not None:
        recording_seconds = time.monotonic() - recording_started
        if not metadata_errors:
            try:
                destination = recorder.write(recording_commit, recording_seconds)
                print('COVERAGE MAP: ' + str(destination), flush=True)
            except (OSError, ValueError) as error:
                metadata_errors.append('cannot write coverage map: ' + str(error))
        print(f'COVERAGE RECORDING: {recording_seconds:.3f} seconds (including preflight)', flush=True)
    detail = framework.measurements(results, layers, stages, jobs=jobs, elapsed=elapsed, selection=selection, part=part,
                                    profile=profile, catalogue=catalogue,
                                    budget_debt=budget_debt, discovery_seconds=DISCOVERY_SECONDS, preflight_seconds=preflight_seconds,
                                    expected_fragments=expected_fragments)
    detail['profile'], detail['metadata_errors'] = profile, metadata_errors
    if recording_seconds is not None:
        detail['recording_seconds'] = recording_seconds
    outcomes = {key: value for row in results for key, value in row['outcomes'].items()}
    detail['requirements'] = framework.coverage(catalogue, outcomes)
    unchecked = sorted(key for key, row in catalogue.items() if not row['checks'])
    detail['unchecked_requirement_ids'] = unchecked
    detail['unchecked_requirement_count'] = len(unchecked)
    requirement_failures = [key for key, value in detail['requirements'].items() if value == 'failed' and
                            any(name in outcomes for name in catalogue[key]['checks'])]
    for entry in detail['budget_warnings']:
        print(f'BUDGET DEBT: {entry["module"]} ceiling={entry["ceiling"]:.3f}s '
              f'{entry["status"]} seconds={entry["seconds"]}', flush=True)
    for name in detail['debt_retirement_required']:
        print(f'RETIRE BUDGET DEBT: {name} is now within its layer budget', flush=True)
    for entry in detail['budget_failures']:
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
    persist_record(detail, save)
    failed = [r for r in results if r["returncode"] != 0]
    totals = {suite: sum(r["tests"] for r in results if r["suite"] == suite) for suite in suites}
    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "jobs": jobs, "stages": sorted(wanted),
                      "job_seconds": round(detail['job_seconds'], 3), "over_budget": detail['over_budget'],
                      "budget_failures": detail['budget_failures'], "budget_warnings": detail['budget_warnings'],
                      "debt_retirement_required": detail['debt_retirement_required'],
                      "discovery_seconds": DISCOVERY_SECONDS, "preflight_seconds": preflight_seconds,
                      "metadata_errors": metadata_errors, "partial": detail['partial'], "profile": profile,
                      "requirement_failures": requirement_failures,
                      "units": len(results),
                      "tests": totals, "failed_units": [f"{r['suite']}:{r['unit']}" for r in failed],
                      "slowest_units": [(f"{r['suite']}:{r['unit']}", round(r["seconds"], 1))
                                        for r in sorted(results, key=lambda r: -r["seconds"])[:8]],
                      "logs": str(base)}, indent=1))
    if not failed and not keep and not metadata_errors and not detail['budget_failures'] and not requirement_failures:
        shutil.rmtree(base, ignore_errors=True)
    print(f'Unchecked requirements ({len(unchecked)}): ' + ', '.join(unchecked), flush=True)
    skipped = sorted(key for key, value in detail['requirements'].items() if value == 'skipped')
    if skipped:
        print(f'Requirements proved only by skipped checks here ({len(skipped)}): ' + ', '.join(skipped), flush=True)
    # A module now under its budget is a note to shrink the debt file, not a failed run (decision 126.3).
    return 1 if failed or metadata_errors or detail['budget_failures'] or requirement_failures else 0


def main(argv: list[str] | None = None) -> int:
    global REVIEW_BASE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--suite", action="append", choices=sorted(SUITES))
    parser.add_argument("--stage", action="append", choices=STAGE_NAMES, help="run only these stages")
    parser.add_argument("--save-timings", type=Path, help="write this run's measurements here")
    parser.add_argument("--keep", action="store_true", help="keep every unit's folder and log")
    parser.add_argument('--profile', choices=('dev', 'ci', 'soak'), action='append')
    parser.add_argument('--invocation-id', help='shared identity for every shard/stage in one run')
    parser.add_argument('--base', help='accepted for CI compatibility; unused')
    dependency_mode = parser.add_mutually_exclusive_group()
    dependency_mode.add_argument('--record-coverage', action='store_true', help='record executed-file dependencies in a full run')
    dependency_mode.add_argument('--affected', metavar='BASE', help='run tests affected by changes since BASE, or all when uncertain')
    parser.add_argument('--requirement-gate', nargs='+', type=Path, help='combine all stages and enforce every requirement')
    parser.add_argument('--budgets-only', action='store_true', help='only check complete shard budgets; final requirement gate remains mandatory')
    parser.add_argument('--combine-timings', nargs='+', type=Path, help='combine complete disjoint shard .run.json files')
    parser.add_argument("--shard", type=parse_shard, default=(1, 1), metavar="K/N",
                        help="run only shard K of N, balanced by recorded time (CI)")
    parser.add_argument("--module", action="append", metavar="SUITE:MODULE",
                        help="run only this test module (repeatable), for example codex:tests.test_bridge")
    args = parser.parse_args(argv)
    REVIEW_BASE = args.base
    if args.invocation_id:
        os.environ['COCOLO_TEST_RUN_ID'] = args.invocation_id
    if args.profile and len(args.profile) != 1:
        parser.error('conflicting profile selectors; choose one explicit profile')
    args.profile = args.profile[0] if args.profile else 'ci'
    if args.record_coverage or args.affected is not None:
        if (args.module or args.suite or args.stage or args.shard != (1, 1) or args.profile != 'ci'
                or args.requirement_gate or args.combine_timings):
            parser.error('--record-coverage/--affected require an unfiltered, unsharded ci-profile run')
    if args.requirement_gate:
        try:
            report = framework.combine_run_records([framework.decode_json(path.read_text()) for path in args.requirement_gate])
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(report, indent=1))
        return int(not report.get('requirements') or any(value not in ('passed', 'unchecked') for value in report['requirements'].values()))
    if args.combine_timings:
        report = framework.combine_measurements([framework.decode_json(path.read_text()) for path in args.combine_timings])
        print(json.dumps(report, indent=1))
        return 1 if report['budget_failures'] or report['metadata_errors'] or any(row['returncode'] for row in report['workers']) or (not args.budgets_only and any(value not in ('passed', 'unchecked', 'skipped') for value in report['requirements'].values())) else 0
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
    options = {'record_coverage': True} if args.record_coverage else {}
    if args.affected is not None:
        options['affected_base'] = args.affected
    return run(args.jobs, suites, args.save_timings, args.keep, set(args.stage or STAGE_NAMES), modules, args.shard, args.profile,
               **options)


if __name__ == "__main__":
    sys.exit(main())
