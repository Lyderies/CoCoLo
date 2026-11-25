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
            self.assertIn(value['status'], ('met', 'failed', 'unmeasured'))
            self.assertTrue(value['scope'])
            self.assertTrue(value['numbers'])
        print('B7B_MEASUREMENTS=' + json.dumps(measurements, sort_keys=True))

    @requirements('RS-B-B7-5', why_not_scenario='Successful authenticated store lookup with real account fixtures and 100,000 expired context tombstones; no service startup proxy.')
    def test_successful_outcome_lookup_at_zero_and_100000_tombstones(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'measure_b7b.py'
        spec = importlib.util.spec_from_file_location('measure_b7b_lookup', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = module.measure_lookup()
        self.assertEqual([0, 100000], [row['tombstones'] for row in values])
        self.assertTrue(all(row['lookup_ms'] >= 0 and row['lookup_vm_steps'] > 0 for row in values))
        print('B7B_SUCCESSFUL_LOOKUP=' + json.dumps(values, sort_keys=True))
