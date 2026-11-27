#!/usr/bin/env python3
"""Compare fresh host-service/driver imports; this does not measure socket readiness."""
from __future__ import annotations

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 120, "data_mib": 128, "rss_mib": 1024}

import argparse  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402


PROGRAM = '''
import sys, time
sys.path.insert(0, sys.argv[1])
started = time.perf_counter()
from codex_agents import project_service, project_driver
print(time.perf_counter() - started)
'''
FLAGS = ('-I', '-B', '-S', '-X', 'pycache_prefix=/dev/null')
BUNDLE = 'packages/codex-agents/native-package'


def measure(root, jobs, samples):
    def sample(_):
        started = time.perf_counter()
        child = subprocess.run([sys.executable, *FLAGS, '-c', PROGRAM, str(root)],
                               env={'PATH': os.defpath}, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30, check=True)
        return {'import_seconds': round(float(child.stdout), 6),
                'process_seconds': round(time.perf_counter() - started, 6)}

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        rows = list(pool.map(sample, range(samples)))
    summary = {name: {'min': min(values), 'median': statistics.median(values), 'max': max(values)}
               for name in ('import_seconds', 'process_seconds')
               for values in ([row[name] for row in rows],)}
    return {'summary': summary, 'samples': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='ce81bd96')
    parser.add_argument('--jobs', type=int, choices=(1, 16), default=1)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < args.jobs:
        parser.error('--samples must be at least --jobs')
    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='s13e-imports-') as temporary:
        archive = subprocess.run(['git', 'archive', args.base, BUNDLE], cwd=repo,
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(temporary, filter='data')
        report = {'base': args.base, 'jobs': args.jobs, 'samples_per_build': args.samples,
                  'measurement': 'fresh host service and driver imports; excludes host initialization and readiness',
                  'python': sys.version.split()[0],
                  'before': measure(Path(temporary) / BUNDLE / '.codex/orchestration', args.jobs, args.samples),
                  'after': measure(repo / BUNDLE / '.codex/orchestration', args.jobs, args.samples)}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({name: report[name]['summary'] for name in ('before', 'after')}, indent=2))


if __name__ == '__main__':
    main()
