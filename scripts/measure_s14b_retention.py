#!/usr/bin/env python3
"""Run S14b's named persistence/size check; optionally substitute signature execution.

The stand-in mode measures real account persistence, maintenance and backup
coverage. It does not verify signatures or prove signature-worker confinement.
It never opens a socket or reads an installed owner ledger.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stand-in-signatures', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--index-scale', action='store_true', help='Measure index build and lookup at five years of summaries')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root/'packages/codex-agents'))
    from tests import test_run_retention_integration as tests
    from codex_agents import signature_execution as execution

    if args.index_scale:
        case = tests.retention.RunRetentionTests('test_typed_history_and_years_of_summaries_use_indexed_membership')
        result = unittest.TextTestRunner().run(unittest.TestSuite([case]))
        report = {**getattr(case, 'scale_measurements', {}), 'passed': result.wasSuccessful()}
        if args.output:
            args.output.write_text(json.dumps(report, indent=1)+'\n')
        print(json.dumps(report, indent=1))
        return int(not result.wasSuccessful())

    measurements = []
    original = tests.column_bytes
    def measured(db, table, mark):
        size = original(db, table, mark)
        measurements.append((table, size))
        return size
    def stand_in(inputs, *_):
        # Test-only synthetic completion. No real job authority leaves this
        # disposable test process, and no runtime setting is changed.
        return execution.SignatureExecutionResult('signature_execution_completed', tuple(range(len(inputs.rows))),
                                                   time.monotonic_ns(), len(inputs.rows)+1, False)
    case = tests.RunRetentionIntegrationTests('test_real_open_claim_footprint_retires_to_measured_metadata_and_summary')
    with mock.patch.object(tests, 'column_bytes', side_effect=measured):
        if args.stand_in_signatures:
            with mock.patch.object(execution, '_execute_signature_job', side_effect=stand_in):
                result = unittest.TextTestRunner().run(unittest.TestSuite([case]))
        else:
            result = unittest.TextTestRunner().run(unittest.TestSuite([case]))
    half = len(measurements)//2
    before = sum(size for _, size in measurements[:half])
    after = sum(size for _, size in measurements[half:])
    entries = getattr(case, 'membership_entries_per_run', 0)
    membership = 774*1825*entries*40
    projected = 1.745*.972*774*(after*1825+before*92)+774*((103*7+3*30)*1367+90*1536)
    report = {'passed': result.wasSuccessful(), 'signature_execution': 'stand-in' if args.stand_in_signatures else 'real',
              'before_column_bytes': before, 'after_column_bytes': after,
              'retained_columns': dict(measurements[half:]),
              'runs_per_day': 774, 'horizon_years': 5, 'projected_gib': round(projected/1024**3, 3) if before and after else None,
              'margin_gib': round(16-projected/1024**3, 3) if before and after else None,
              'membership_entries_per_run': entries, 'membership_index_reserved_bytes_per_entry': 40,
              'projected_live_database_gib': round((projected/.972+membership)/1024**3, 3) if before and after else None,
              'backup_membership_index_bytes': 0,
              'membership_index_projected_gib': round(membership/1024**3, 3),
              'overhead_source': 'accepted S14: 1.745 database/column, 0.972 planned-backup/database',
              'includes': 'all backed-up tables, released hold receipts, actual partial summaries, 92 days recent detail, weighted events; derived membership excluded from backup/source images/transfers',
              'live_size_basis': 'planned backup / accepted .972 backup-to-database factor + compact TEMP index reserve'}
    if args.output:
        args.output.write_text(json.dumps(report, indent=1)+'\n')
    print(json.dumps(report, indent=1))
    return int(not result.wasSuccessful())


if __name__ == '__main__':
    raise SystemExit(main())
