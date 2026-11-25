"""Run the B7b measurement harness; failed budgets stay explicitly failed in its output."""
import importlib.util
import json
from pathlib import Path
import unittest
import statistics
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
        for first, second in zip(comparisons[0]['retirement_vm_steps_samples'], comparisons[1]['retirement_vm_steps_samples']):
            self.assertLessEqual(abs(first - second), 16)
        for row in comparisons:
            self.assertEqual(10, row['retired'])
            self.assertLess(row['retirement_ms'], 50)
            self.assertLess(row['lookup_ms'], 5)
            self.assertEqual(row['tombstones'], row['released_leases'])
            self.assertEqual(row['tombstones'], row['released_reservations'])
            self.assertEqual(1, row['has_holds'])
            self.assertEqual(1, row['pending_settlement'])
            self.assertTrue(any('project_result_contexts_live' in str(plan) for plan in row['live_index_plan']))
        self.assertEqual(1, measurements['B3']['numbers']['warmup_runs'])
        for samples, median in zip(measurements['B3']['numbers']['batch_samples_ms'], measurements['B3']['numbers']['batch_ms']):
            self.assertEqual(3, len(samples))
            self.assertEqual(statistics.median(samples), median)
        for row in comparisons:
            self.assertEqual(1, row['warmup_runs'])
            for operation in ('lookup', 'retirement'):
                self.assertEqual(3, len(row[operation + '_samples_ms']))
                self.assertEqual(statistics.median(row[operation + '_samples_ms']), row[operation + '_ms'])
        for name in ('B1', 'B6'):
            self.assertEqual('unmeasured', measurements[name]['status'])
            self.assertTrue(measurements[name]['unmeasured_reason'])
        # The absent memory-kind lifecycle/scheduler is a real capability gap.
        from codex_agents.holds_report_contracts import UNTRACKED_KINDS
        self.assertEqual({'driver_entry', 'preparation', 'orchestration'}, set(UNTRACKED_KINDS),
                         'Memory tracking changed: implement full B1/B6 measurements.')
        # A sandbox skips only service startup, after all store assertions ran.
        with self.subTest(operation='real_service_startup'):
            if measurements['B2']['service_startup_status'] == 'unmeasured':
                for name in ('B2', 'S3'):
                    self.assertEqual('unmeasured', measurements[name]['service_startup_status'])
                    self.assertTrue(measurements[name]['service_startup_unmeasured_reason'])
                self.assertEqual('unmeasured', measurements['B2']['status'])
                self.assertTrue(measurements['B2']['unmeasured_reason'])
                self.skipTest(measurements['B2']['service_startup_unmeasured_reason'])
            for name, limit in (('B2', 100), ('S3', 200)):
                self.assertEqual('met', measurements[name]['service_startup_status'], measurements[name])
                numbers = measurements[name]['numbers']
                for key in ('empty_service_startup_ms', 'populated_service_startup_ms'):
                    self.assertEqual(3, len(numbers[key]))
                self.assertEqual(statistics.median(numbers['populated_service_startup_ms']) -
                    statistics.median(numbers['empty_service_startup_ms']), numbers['service_start_overhead_ms'])
                self.assertLessEqual(numbers['service_start_overhead_ms'], limit)
            self.assertEqual('met', measurements['B2']['status'])

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
            self.assertEqual(1, row['warmup_runs'])
            self.assertEqual(3, len(row['lookup_samples_ms']))
            self.assertEqual(statistics.median(row['lookup_samples_ms']), row['lookup_ms'])
        self.assertLessEqual(abs(values[0]['lookup_vm_steps'] - values[1]['lookup_vm_steps']), 16)
        print('B7B_SUCCESSFUL_LOOKUP=' + json.dumps(values, sort_keys=True))

    @requirements('RS-B-B7-5', why_not_scenario='Real service host and writer startup/shutdown; only listener binding is disabled for sandbox coverage, never used as timing evidence.')
    def test_service_startup_owns_real_writer_and_driver_and_cleans_up(self):
        from unittest import mock
        from codex_agents.service_host import EnrolledFrontend, ProjectServiceHost
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'measure_b7b.py'
        spec = importlib.util.spec_from_file_location('measure_b7b_startup', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hosts = []
        original = ProjectServiceHost._publish
        def publish(host):
            hosts.append(host)
            return original(host)
        with module.fixture() as test:
            test.bound()
            before = list(test.store._connection.execute('SELECT state FROM result_leases'))
            with mock.patch.object(EnrolledFrontend, 'listen') as listen, \
                 mock.patch.object(ProjectServiceHost, '_publish', publish), \
                 module.service_startup_timer(test) as sample:
                for _ in range(2):
                    sample()  # A leaked writer or journal lock refuses the next start.
                    # Check before temporary-directory cleanup can remove evidence.
                    self.assertTrue(hosts[-1].threads_stopped())
                    self.assertFalse(hosts[-1]._hint.exists())
                    self.assertTrue(hosts[-1]._driver._journal._released)
            self.assertEqual([tuple(row) for row in before], [tuple(row) for row in
                test.store._connection.execute('SELECT state FROM result_leases')])
        self.assertEqual(6, listen.call_count)  # Three product endpoints per start.
        self.assertEqual(2, len(hosts))
        for host in hosts:
            self.assertIsNotNone(host._driver)
            self.assertTrue(host.threads_stopped())
            self.assertFalse(host._hint.exists())
