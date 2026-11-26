"""S13d-1: an idle collector and an idle project host cost almost no CPU.

Real, separate host processes (no substitute loop) are measured by scripts/measure_idle_hosts.py: each process's
own user + system CPU per wall second over 10 idle seconds, after readiness. The collector and project-host loops
used to run a full pass every 2 ms with nothing to do.

Budgets come from the measurement (CPU seconds per idle second; this machine was busy with other work, so the spread
is wide). Base 0.8.67 loops, 9 runs: collector 0.062-0.119, project 0.016-0.033. After the passes back off to 50 ms
once no account client or work has been held for 400 passes, runs interleaved with base runs: collector 0.014-0.020,
project 0.007-0.010. Each budget sits above the changed loop and below the lowest base run (CPU time, not wall time,
so load moves it little).
"""
from tests.support import REPOSITORY_ROOT, SHORT_TMP  # noqa: F401  (bootstraps the import path)
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, str(REPOSITORY_ROOT.parents[1] / 'scripts'))
from test_framework import requirements

COLLECTOR_BUDGET = 0.04
PROJECT_BUDGET = 0.015
TOOL = REPOSITORY_ROOT.parents[1] / 'scripts' / 'measure_idle_hosts.py'


class IdleCpuBudgetTests(unittest.TestCase):
    @requirements('S13d-1', why_not_scenario='Measures the CPU of real disposable host processes through the measurement tool; no client scenario is involved.')
    def test_S13d_1_idle_collector_and_project_host_stay_within_their_cpu_budgets(self):
        done = subprocess.run(
            [sys.executable, '-B', str(TOOL), '--seconds', '10', '--collector-budget', str(COLLECTOR_BUDGET),
             '--project-budget', str(PROJECT_BUDGET)],
            capture_output=True, text=True, timeout=180, env={**os.environ, 'TMPDIR': SHORT_TMP})
        rates = {}
        for line in done.stdout.splitlines():
            if line.startswith('{'):
                row = json.loads(line)
                rates[row['host']] = round(row['cpu_seconds_per_idle_second'], 4)
        self.assertEqual({'collector', 'project'}, set(rates), done.stderr[-2000:])
        self.assertLessEqual(rates['collector'], COLLECTOR_BUDGET, rates)
        self.assertLessEqual(rates['project'], PROJECT_BUDGET, rates)
        self.assertEqual(0, done.returncode, (rates, done.stderr[-2000:]))


if __name__ == '__main__':
    unittest.main()
