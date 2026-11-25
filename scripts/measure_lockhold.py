#!/usr/bin/env python3
"""Synthetic LOCKHOLD VM/row measurements, outside the fast suite.

Run from the repository root with the test Python:
  python -B scripts/measure_lockhold.py --out docs/requirements/lockhold/measurement.json
No live ledger, service or provider is contacted; owner keys are temporary fixtures.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--small-events', type=int, default=1000)
    parser.add_argument('--large-events', type=int, default=100_000)
    parser.add_argument('--expired-events', type=int, default=20_000)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.small_events < args.large_events or args.expired_events < 1:
        parser.error('event counts must be positive and large must exceed small')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'packages' / 'codex-agents'))
    from tests.test_lockhold import LockHoldReadTests, LockHoldRetentionTests, former_provider_records, measured
    from codex_agents import learning_store

    report = {'base': '51623740', 'synthetic_only': True, 'reads': [], 'retention': {}}
    fixture = LockHoldReadTests(methodName='runTest')
    fixture.setUp()
    try:
        fixture.admit()
        for total in (args.small_events, args.large_events):
            fixture.history(total)
            calls = {
                'provider_attestation_status': lambda: fixture.ledger.provider_attestation_status(fixture.project),
                'health_with_project': fixture.health,
                'admission_evaluation': lambda: fixture.ledger.evaluate_admission(
                    fixture.project, provider='codex', environ={}, now_ms=fixture.now),
            }
            sample = {'unrelated_events': total, 'vm_steps': {}, 'former_scan_vm_steps': {}, 'answers_equal': {}}
            for name, call in calls.items():
                steps, answer = measured(fixture.db, call)
                with mock.patch.object(fixture.ledger, '_provider_records_locked',
                                       side_effect=lambda *a: former_provider_records(fixture.ledger, *a)):
                    old_steps, old_answer = measured(fixture.db, call)
                sample['vm_steps'][name] = steps
                sample['former_scan_vm_steps'][name] = old_steps
                sample['answers_equal'][name] = answer == old_answer
            report['reads'].append(sample)
    finally:
        fixture.doCleanups()

    fixture = LockHoldRetentionTests(methodName='runTest')
    fixture.setUp()
    try:
        fixture.db.execute('DELETE FROM records')
        fixture.base._seed(args.expired_events, fixture.now)
        initial = fixture.rows()
        # Reproduce the former sweep: transaction batches under one outer hold.
        with fixture.counted_holds() as counter:
            start = time.perf_counter()
            with fixture.ledger._lock:
                while fixture.ledger.sweep_event_retention() >= learning_store.EVENT_SWEEP_BATCH:
                    pass
            former_ms = (time.perf_counter() - start) * 1000
        expected = fixture.rows()
        report['retention']['former_rows_per_hold'] = [n for n in counter.holds if n]
        report['retention']['former_hold_ms'] = round(former_ms, 3)
        fixture.db.execute('DELETE FROM records')
        fixture.db.executemany('INSERT INTO records VALUES(' + ','.join('?' for _ in initial[0]) + ')', initial)
        server = learning_store.CollectorServer(fixture.ledger)
        elapsed = []
        with fixture.counted_holds() as counter:
            while True:
                start = time.perf_counter()
                outcome = server._sweep_cycle(holds_only=bool(elapsed))
                elapsed.append(round((time.perf_counter() - start) * 1000, 3))
                if outcome != 'more':
                    break
        report['retention'].update({
            'expired_events': args.expired_events,
            'rows_per_hold': [n for n in counter.holds if n],
            'cycle_ms': elapsed,
            'final_records_equal': fixture.rows() == expected,
            'transaction_left_open': fixture.db.in_transaction,
        })
    finally:
        fixture.doCleanups()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return int(not all(equal for sample in report['reads'] for equal in sample['answers_equal'].values())
               or not report['retention']['final_records_equal'])


if __name__ == '__main__':
    raise SystemExit(main())
