"""Run the B7b measurement harness; failed budgets stay explicitly failed in its output."""
import importlib.util
import json
from pathlib import Path
import unittest
import tests.support  # noqa: F401
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements


class BudgetMeasurements(unittest.TestCase):
    @requirements('RS-B-B7-5', why_not_scenario='Actual store timing harness; full service measurements require primary socket privileges.')
    def test_measured_budgets_report_numbers_without_relabelling_failures(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'measure_b7b.py'
        spec = importlib.util.spec_from_file_location('measure_b7b', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        measurements = module.measure()
        self.assertEqual({'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'S3'}, set(measurements))
        for name, value in measurements.items():
            self.assertTrue(value['scope'])
            self.assertTrue(value['numbers'])
        print('B7B_MEASUREMENTS=' + json.dumps(measurements, sort_keys=True))
        for name in ('B3', 'B4', 'B5', 'S3'):
            self.assertEqual('met', measurements[name]['status'], (name, measurements[name]))
        self.assertLess(measurements['B3']['numbers']['maximum_ms'], 50)
        self.assertEqual(1000, measurements['B5']['numbers']['released'])
        self.assertLessEqual(measurements['B5']['numbers']['batches'], 18)
        comparisons = measurements['B4']['numbers']['comparisons']
        self.assertEqual([0, 100000], [row['tombstones'] for row in comparisons])
        for key in ('holds_vm_steps', 'context_sweep_vm_steps', 'reconcile_vm_steps',
                    'lookup_vm_steps', 'retirement_vm_steps', 'recovery_pending_vm_steps', 'has_holds_vm_steps'):
            self.assertLessEqual(abs(comparisons[0][key] - comparisons[1][key]), 16, key)
        for row in comparisons:
            self.assertEqual(10, row['retired'])
            self.assertLess(row['retirement_ms'], 50)
            self.assertLess(row['lookup_ms'], 5)
            self.assertEqual(row['tombstones'], row['released_leases'])
            self.assertEqual(row['tombstones'], row['released_reservations'])
            self.assertEqual(1, row['has_holds'])
            self.assertEqual(1, row['pending_settlement'])
            self.assertTrue(any('project_result_contexts_live' in str(plan) for plan in row['live_index_plan']))
        for name in ('B1', 'B2', 'B6'):
            self.assertEqual('unmeasured', measurements[name]['status'])
            self.assertTrue(measurements[name]['unmeasured_reason'])
        self.assertEqual('unmeasured', measurements['S3']['service_startup_status'])
        # The absent memory-kind lifecycle/scheduler is a real capability gap.
        # Fail when it ships: these rows then need full measurements, not stale excuses.
        from codex_agents.holds_report_contracts import UNTRACKED_KINDS
        self.assertEqual({'driver_entry', 'preparation', 'orchestration'}, set(UNTRACKED_KINDS),
                         'Memory tracking changed: implement full B1/B6 measurements.')
        # B2/S3's missing service measurement is an environment limitation,
        # not an absent service API. Stop hiding it when binding is available.
        import socket
        import tempfile
        from tests.support import SHORT_TMP
        with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory, \
             socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            with self.assertRaises(PermissionError,
                    msg='Socket binding is available: measure full B2/S3 service startup instead of store-open proxies.'):
                listener.bind(str(Path(directory) / 'b.sock'))

    @requirements('RS-B-B7-5', why_not_scenario='Successful authenticated store lookup with real account fixtures and 100,000 expired context tombstones; no service startup proxy.')
    def test_successful_outcome_lookup_at_zero_and_100000_tombstones(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'measure_b7b.py'
        spec = importlib.util.spec_from_file_location('measure_b7b_lookup', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = module.measure_lookup()
        self.assertEqual([0, 100000], [row['tombstones'] for row in values])
        self.assertTrue(all(row['lookup_ms'] >= 0 and row['lookup_vm_steps'] > 0 for row in values))
        for row in values:
            self.assertLess(row['lookup_ms'], 5)
        self.assertLessEqual(abs(values[0]['lookup_vm_steps'] - values[1]['lookup_vm_steps']), 16)
        print('B7B_SUCCESSFUL_LOOKUP=' + json.dumps(values, sort_keys=True))
