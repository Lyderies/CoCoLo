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


HISTORY_VALIDATION_SECONDS = 0.


def measured_history(function):
    def measured(*args, **kwargs):
        global HISTORY_VALIDATION_SECONDS
        began = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            HISTORY_VALIDATION_SECONDS += time.monotonic() - began
    return measured


@measured_history
def legacy_versions(root, current, ceiling):
    """Compare working files with committed introduction and every historical shrink transition."""
    errors = []
    if not REVIEW_BASE:
        return ceiling, {'baseline': current['baseline'], 'tests': {}}, ['legacy list requires an explicit trusted --base']
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    def version(commit, name):
        return framework.decode_json(git('show', commit + ':scripts/' + name))
    try:
        commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/test-legacy-ceiling.json').split()
        if not commits:
            raise ValueError('initial ceiling is not committed')
        original = version(commits[0], 'test-legacy-ceiling.json')
        dep_commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/test-legacy-dependencies.json').split()
        if not dep_commits or framework.decode_json((root / 'scripts/test-legacy-dependencies.json').read_text()) != version(dep_commits[0], 'test-legacy-dependencies.json'):
            errors.append('legacy fixture/helper baseline changed or is uncommitted')
        for value_name in ('test-legacy-values.json', 'test-legacy-portable-values.json', 'test-legacy-bound-values.json', 'test-legacy-captured-values.json'):
            value_commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/' + value_name).split()
            if not value_commits or framework.decode_json((root / 'scripts' / value_name).read_text()) != version(value_commits[0], value_name):
                errors.append(value_name + ': legacy constants/source baseline changed or is uncommitted')
        if ceiling != original:
            errors.append('legacy initial ceiling changed')
        try:
            previous = version(REVIEW_BASE, 'test-legacy.json')
        except subprocess.CalledProcessError:
            git('merge-base', '--is-ancestor', framework.BASELINE, REVIEW_BASE)
            if REVIEW_BASE != framework.RELEASE_BASE:
                git('merge-base', '--is-ancestor', REVIEW_BASE, commits[0])
            previous = original  # Explicit trusted base before the manifest was introduced.
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



@measured_history
def original_budget_versions(root, current):
    """Baseline ceilings are derived from committed measurements, never candidate estimates."""
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    if not REVIEW_BASE:
        return ['budget debt requires an explicit trusted --base']
    try:
        origin = git('log', '--reverse', '--format=%H', 'HEAD', '--',
                     'measurements/test-framework/baseline-timings.json').split()[0]
        baseline = framework.decode_json(git('show', origin + ':measurements/test-framework/baseline-timings.json'))
        layer_origin = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/test-layers.json').split()[0]
        initial_layers = framework.decode_json(git('show', layer_origin + ':scripts/test-layers.json'))
        ceiling = {'baseline': framework.BASELINE, 'modules': {name: baseline[name] for name, layer in initial_layers.items()
                   if name in baseline and baseline[name] > framework.BUDGETS[layer]}}
        # A base before this file's introduction has the full generated debt ceiling.
        git('rev-parse', '--verify', REVIEW_BASE + '^{commit}')
        def version(commit):
            try:
                return framework.decode_json(git('show', commit + ':scripts/test-budget-debt.json'))
            except subprocess.CalledProcessError:
                return ceiling
        errors = framework.validate_budget_debt(current, ceiling, version(REVIEW_BASE))
        for commit in git('rev-list', 'HEAD', '--', 'scripts/test-budget-debt.json').split():
            for parent in git('rev-list', '--parents', '-n', '1', commit).split()[1:]:
                errors += framework.validate_budget_debt(version(commit), ceiling, version(parent))
        return errors
    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError):
        return ['cannot verify budget debt baseline/trusted base']


def release_versions(root, current, immutable_name, current_name):
    """Read an immutable introduction plus every shrink transition, never HEAD as a waiver."""
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    def version(commit, name):
        return framework.decode_json(git('show', commit + ':scripts/' + name))
    origin = git('log', '--reverse', '--format=%H', 'HEAD', '--', 'scripts/' + immutable_name).split()[0]
    ceiling = version(origin, immutable_name)
    if framework.decode_json((root/'scripts'/immutable_name).read_text()) != ceiling:
        raise ValueError(immutable_name + ': immutable release baseline changed')
    git('merge-base', '--is-ancestor', framework.RELEASE_BASE, 'HEAD')
    if not REVIEW_BASE:
        raise ValueError('release baseline requires an explicit trusted --base')
    git('rev-parse', '--verify', REVIEW_BASE + '^{commit}')
    def previous(commit):
        try:
            return version(commit, current_name)
        except subprocess.CalledProcessError:
            git('merge-base', '--is-ancestor', commit, origin)
            return ceiling
    versions = [previous(REVIEW_BASE)]
    for commit in git('rev-list', 'HEAD', '--', 'scripts/' + current_name).split():
        this = version(commit, current_name)
        for parent in git('rev-list', '--parents', '-n', '1', commit).split()[1:]:
            before = previous(parent)
            key = 'tests' if 'tests' in this else 'modules'
            if this['baseline'] != before['baseline'] or any(
                name not in before[key] or (value != before[key][name] if key == 'tests' else value > before[key][name])
                for name, value in this[key].items()):
                raise ValueError(current_name + ': release history grew at ' + commit[:12])
    key = 'tests' if 'tests' in current else 'modules'
    for prior in [ceiling, *versions]:
        if current['baseline'] != prior['baseline'] or any(
            name not in prior[key] or (value != prior[key][name] if key == 'tests' else value > prior[key][name])
            for name, value in current[key].items()):
            raise ValueError(current_name + ': release list grew or ceiling changed')
    return ceiling, versions[0]


def budget_versions(root, current):
    original = framework.decode_json((root/'scripts/test-budget-debt.json').read_text())
    errors = original_budget_versions(root, original)
    if not (root/'scripts/test-budget-debt-0850-ceiling.json').exists():
        return errors + framework.validate_budget_debt(current, original, original)
    try:
        ceiling, previous = release_versions(root, current, 'test-budget-debt-0850-ceiling.json', 'test-budget-debt-0850.json')
        errors += framework.validate_budget_debt(current, ceiling, previous)
    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError) as error:
        errors.append('cannot verify release budget debt: ' + str(error))
    return errors



DISCOVERY_SECONDS = 0.

def metadata():
    global DISCOVERY_SECONDS
    DISCOVERY_SECONDS = 0.
    def read(name):
        return framework.decode_json((ROOT / 'scripts' / name).read_text())
    stages, layers = read('test-stages.json'), read('test-layers.json')
    catalogue, legacy, ceiling = read('test-requirements.json'), read('test-legacy.json'), read('test-legacy-ceiling.json')
    ceiling, previous, errors = legacy_versions(ROOT, legacy, ceiling)
    release = None
    if (ROOT/'scripts/test-legacy-release-0850.json').exists():
        release = read('test-legacy-release-0850.json')
        release_current = read('test-legacy-0850.json')
        try:
            release_ceiling, release_previous = release_versions(ROOT, release_current,
                'test-legacy-0850-ceiling.json', 'test-legacy-0850.json')
            # The detailed dependency snapshot is separately immutable.
            commits = subprocess.check_output(['git','log','--reverse','--format=%H','HEAD','--',
                'scripts/test-legacy-release-0850.json'],cwd=ROOT,text=True).split()
            committed = framework.decode_json(subprocess.check_output(['git','show',commits[0]+':scripts/test-legacy-release-0850.json'],cwd=ROOT,text=True))
            if committed != release or release['source_revision'] != framework.RELEASE_BASE:
                raise ValueError('immutable release dependency baseline changed')
            legacy, ceiling, previous = framework.release_legacy(legacy, ceiling, previous, release,
                release_current, release_ceiling, release_previous)
        except (subprocess.CalledProcessError, ValueError, KeyError, IndexError) as error:
            errors.append('cannot verify release legacy baseline: '+str(error))
    if 'legacy list requires an explicit trusted --base' in errors:
        raise ValueError('legacy list requires an explicit trusted --base')
    discovery_started = time.monotonic()
    try:
        discovered = inventory()
    finally:
        DISCOVERY_SECONDS = time.monotonic() - discovery_started
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
    registry_path = ROOT / 'scripts/test-checks.json'
    if registry_path.exists():
        registry = framework.decode_json(registry_path.read_text())
        checked_inventory['registered:checks'] = {
            name: {'requirements': check['requirements'], 'rationale': 'Registered executable or review evidence', 'fingerprint': ''}
            for name, check in registry.items()}
        checked_stages['registered:checks'], checked_layers['registered:checks'] = 'e2e', 'scenario'
        for name, check in registry.items():
            if check.get('kind') not in ('executable', 'review'):
                errors.append(name + ': unknown registered check kind')
    errors += framework.validate_sources(ROOT, catalogue)
    errors += framework.validate_metadata(inventory=checked_inventory, stages=checked_stages, layers=checked_layers,
                                         catalogue=catalogue, legacy=legacy, ceiling=ceiling, previous=previous)
    for module, cases in discovered.items():
        if all(name in legacy['tests'] for name in cases):
            continue
        suite, name = module.split(':', 1)
        source = SUITES[suite] / (('tests/' if suite == 'root' else '') + name.replace('.', '/') + '.py')
        if source.exists():
            errors += [module + ': ' + error for error in framework.golden_bypasses(source.read_text(), filename=source)]
    dependency_file = ROOT / 'scripts/test-legacy-dependencies.json'
    if dependency_file.exists():
        dependencies = framework.decode_json(dependency_file.read_text())
        if release:
            dependencies.update({name: record['dependencies'] for name, record in release['tests'].items()})
        for cases in discovered.values():
            for name, record in cases.items():
                if name in legacy['tests'] and record.get('dependencies') != dependencies.get(name):
                    errors.append(name + ': legacy fixture/helper dependency changed; migrate its requirement proof')
    else:
        errors.append('missing legacy fixture/helper dependency baseline')
    value_file = ROOT / 'scripts/test-legacy-captured-values.json'
    values = framework.decode_json(value_file.read_text()) if value_file.exists() else {}
    if release:
        values.update({name: {'fingerprint': record['dependency_values']} for name, record in release['tests'].items()})
    errors += framework.validate_legacy_values({key: rec for cases in discovered.values() for key, rec in cases.items()}, legacy, values)
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
        part: tuple[int, int] = (1, 1), profile: str = 'ci') -> int:
    """Run the suites' units in the wanted stages; with `modules` ({suite: module names}), only those."""
    global _CURRENT_DISCOVERY, HISTORY_VALIDATION_SECONDS
    _CURRENT_DISCOVERY = None
    HISTORY_VALIDATION_SECONDS = 0.
    timings = json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {}
    preflight_started = time.monotonic()
    try:
        discovered, stages, layers, catalogue, metadata_errors = metadata()
        debt_path = ROOT/'scripts/test-budget-debt-0850.json'
        if not debt_path.exists():
            debt_path = ROOT/'scripts/test-budget-debt.json'
        budget_debt = framework.decode_json(debt_path.read_text())
        metadata_errors += budget_versions(ROOT, budget_debt)
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
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", _RUNNER, *names], cwd=SUITES[suite],
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
    fingerprints = {key: value.get('fingerprint', 'import_error') for cases in discovered.values() for key, value in cases.items()}
    detail = framework.measurements(results, layers, stages, jobs=jobs, elapsed=elapsed, selection=selection, part=part,
                                    profile=profile, catalogue=catalogue, fingerprints=fingerprints,
                                    budget_debt=budget_debt, discovery_seconds=DISCOVERY_SECONDS, preflight_seconds=preflight_seconds,
                                    expected_fragments=expected_fragments)
    detail['history_validation_seconds'] = HISTORY_VALIDATION_SECONDS
    detail['profile'], detail['metadata_errors'] = profile, metadata_errors
    outcomes = {key: value for row in results for key, value in row['outcomes'].items()}
    registry_path = ROOT / 'scripts/test-checks.json'
    if registry_path.exists():
        registered = framework.registered_outcomes(framework.decode_json(registry_path.read_text()),
                                                   framework.decode_json((ROOT / 'scripts/test-review-evidence.json').read_text()))
        outcomes.update(registered)
        detail['registered_outcomes'] = registered
    detail['requirements'] = framework.coverage(catalogue, outcomes)
    requirement_failures = [key for key, value in detail['requirements'].items() if value == 'failed' and
                            any(name in outcomes for name in catalogue[key]['checks'])]
    if modules is None and set(suites) == set(SUITES) and wanted == set(STAGE_NAMES) and part == (1, 1):
        requirement_failures = sorted(key for key, value in detail['requirements'].items() if value != 'passed')
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
    return 1 if failed or metadata_errors or detail['budget_failures'] or detail['debt_retirement_required'] or requirement_failures else 0


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
    parser.add_argument('--base', help='trusted review-base commit for the shrink-only legacy check')
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
    if args.requirement_gate:
        try:
            report = framework.combine_run_records([framework.decode_json(path.read_text()) for path in args.requirement_gate])
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(report, indent=1))
        return int(not report.get('requirements') or any(value != 'passed' for value in report['requirements'].values()))
    if args.combine_timings:
        report = framework.combine_measurements([framework.decode_json(path.read_text()) for path in args.combine_timings])
        print(json.dumps(report, indent=1))
        return 1 if report['budget_failures'] or report['debt_retirement_required'] or report['metadata_errors'] or any(row['returncode'] for row in report['workers']) or (not args.budgets_only and any(value != 'passed' for value in report['requirements'].values())) else 0
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
