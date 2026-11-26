#!/usr/bin/env python3
"""U3 1k/100k SQL growth gate on disposable synthetic stores.

Run from the repository root with the test interpreter. No live services or
owner ledger are read. Fixtures and assertions are shared with the fast suite.
"""
from __future__ import annotations

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 300, "data_mib": 256, "rss_mib": 2048}  # 1k vs 100k rows

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='optional structured evidence file')
    parser.add_argument('--case', action='append', help='run only this test name (repeatable)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'packages/codex-agents'))
    sys.path.insert(0, str(root / 'scripts'))
    from tests import test_read_growth as growth
    growth.SMALL, growth.LARGE = 1_000, 100_000
    report = {'history_rows': [growth.SMALL, growth.LARGE], 'cases': [], 'passed': True}
    names = unittest.defaultTestLoader.getTestCaseNames(growth.ReadGrowthTests)
    if args.case and set(args.case) - set(names):
        parser.error('unknown case: ' + ', '.join(sorted(set(args.case) - set(names))))
    for name in names:
        if name.startswith('test_U3_additive') or args.case and name not in args.case:
            continue
        case = growth.ReadGrowthTests(name)
        result = unittest.TestResult()
        started = time.monotonic()
        case.run(result)
        passed = not (result.failures or result.errors)
        row = {'test': name, 'passed': passed, 'seconds': round(time.monotonic() - started, 3),
               'work': getattr(case, 'measurements', []),
               'production_budget_samples': getattr(case, 'budget_samples', []),
               'plans': [{'query_sha256': hashlib.sha256(sql.encode()).hexdigest(),
                          'sql': ' '.join(sql.split()), 'plan': plan}
                         for (sql, _), plan in getattr(case, 'plans', {}).items()]}
        # Store only assertion/error type and summary, never fixture record bytes,
        # user paths or raw exception tracebacks in this review artifact.
        if not passed:
            row['failure_count'] = len(result.failures)
            row['error_count'] = len(result.errors)
            for _, detail in result.failures + result.errors:
                print(detail, file=sys.stderr)
        report['cases'].append(row)
        report['passed'] &= passed
        print(json.dumps({'test': name, 'passed': passed, 'seconds': row['seconds'],
                          'work': row['work'], 'plans': len(row['plans']),
                          'max_budget_seconds': max((s['seconds'] for s in row['production_budget_samples']), default=0)}), flush=True)
    from tests.test_read_plans import ReadPlanTests
    guard = ReadPlanTests('test_U3_6_every_budgeted_read_that_runs_uses_a_live_or_selective_index')
    result = unittest.TestResult()
    guard.run(result)
    report['plan_inventory_passed'] = not (result.failures or result.errors)
    report['budgeted_read_plans'] = getattr(guard, 'budgeted_plans', [])
    report['passed'] &= report['plan_inventory_passed']
    print(json.dumps({'plan_inventory_passed': report['plan_inventory_passed'],
                      'distinct_budgeted_reads': len(report['budgeted_read_plans'])}), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
