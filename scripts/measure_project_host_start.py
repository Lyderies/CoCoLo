#!/usr/bin/env python3
"""Observe the unchanged E2c-1 test, optionally beside the named --jobs 16 load."""
from __future__ import annotations

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 180, "data_mib": 128, "rss_mib": 1024}

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402


PROGRAM = r'''
import io, json, sys, time, unittest
from pathlib import Path
repo = Path(sys.argv[1])
sys.path[:0] = [str(repo / 'packages/codex-agents'), str(repo / 'scripts')]
from tests.test_project_start import StartTests
name = 'test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client'
case = StartTests(name)
observed = {}
launch, serving = case.launch, case.serving
def timed_launch(*args, **kwargs):
    reply = launch(*args, **kwargs)
    observed['collector_ready'] = time.monotonic()
    return reply
def timed_serving(project):
    answer = serving(project)
    now = time.monotonic()
    if answer and project in {'never', 'stopped'} and project not in observed:
        begun = case.events('begin', project)
        observed[project] = {
            'begin_to_serving_seconds': now - begun[0]['t'] if begun else None,
            'ready_to_serving_seconds': now - observed['collector_ready']}
    return answer
case.launch, case.serving = timed_launch, timed_serving
started = time.monotonic()
result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case]))
print(json.dumps({'passed': result.wasSuccessful(), 'test_seconds': time.monotonic() - started,
                  'projects': {k: v for k, v in observed.items() if k != 'collector_ready'},
                  'failures': len(result.failures), 'errors': len(result.errors)}))
'''


def run(repo, samples, load):
    # Only the explicitly named load modules; wait for their run before returning.
    with tempfile.TemporaryFile(mode='w+') as log:
        background = subprocess.Popen(
            [sys.executable, '-B', 'scripts/parallel_tests.py', '--jobs', '16',
             '--module', 'root:test_install', '--module', 'root:test_setup_wizard_direct'],
            cwd=repo, stdout=log, stderr=log) if load else None
        rows = []
        try:
            for _ in range(samples):
                active_before = background is not None and background.poll() is None
                child = subprocess.run([sys.executable, '-B', '-c', PROGRAM, str(repo)],
                                       cwd=repo, capture_output=True, text=True, timeout=90)
                if child.returncode:
                    raise RuntimeError('E2c-1 observation interpreter failed')
                row = json.loads(child.stdout)
                row['load_active_throughout'] = active_before and background.poll() is None if load else False
                rows.append(row)
        finally:
            load_code = background.wait() if background is not None else None
    return {'samples': rows, 'load_returncode': load_code,
            'all_passed': all(row['passed'] for row in rows),
            'loaded_gate_passed': bool(load and load_code == 0 and len(rows) >= 5
                                      and all(row['passed'] and row['load_active_throughout'] for row in rows))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', help='observe a temporary archive of this revision')
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--load', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    repo = Path(__file__).resolve().parent.parent
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='s13e-start-') as temporary:
        if args.base:
            archive = subprocess.run(['git', 'archive', args.base], cwd=repo,
                                     capture_output=True, check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
                tree.extractall(temporary, filter='data')
            repo = Path(temporary)
        report = {'base': args.base, 'loaded': args.load, 'python': sys.version.split()[0],
                  'test': 'tests.test_project_start.StartTests.'
                          'test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client',
                  **run(repo, args.samples, args.load)}
    report['elapsed_seconds'] = time.monotonic() - started
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['all_passed'] and (not args.load or report['loaded_gate_passed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
