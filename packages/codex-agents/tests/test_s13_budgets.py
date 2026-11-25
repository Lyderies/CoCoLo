"""S13: the RS-B-9 budgets B1 and B6 and the RS-10 budget for 1,000 due holds, measured on product operations.

Small scale only: the 100,000-tombstone numbers come from scripts/measure_s13.py (docs/requirements/s13.md).
"""
import importlib.util
from pathlib import Path
import sys
import unittest

import tests.support  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
WHY = 'Real project store and register operations on disposable stores; no service boundary is patched.'


def measurements():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location('measure_s13', SCRIPTS / 'measure_s13.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


class S13Budgets(unittest.TestCase):
    @requirements('RS-B-9-B1', why_not_scenario=WHY)
    def test_b1_register_and_memory_writer_work_of_a_normal_run_is_under_5_ms(self):
        value = measurements().measure_b1()
        self.assertEqual(1, value['warmup_runs'])
        self.assertEqual(3, len(value['register_samples_ms']))
        # Every register call of a normal run was seen: takes, the arm, releases and the wake bookkeeping.
        self.assertTrue({'take_hold', 'arm_hold', 'release_hold'} <= set(value['register_calls']), value)
        self.assertEqual(4, value['reservations_released_completed'])  # one per run, warm-up included
        self.assertEqual(4, len(value['memory_transactions_samples_ms'][0]))
        self.assertLess(value['register_ms'], 5)
        self.assertLess(value['memory_writer_ms'], 5)
        self.assertLess(value['total_ms'], value['limit_ms'], value)

    @requirements('RS-B-9-B6', why_not_scenario=WHY)
    def test_b6_one_eligible_row_behind_128_blocked_and_50_arrivals_per_batch_within_4_batches(self):
        module = measurements()
        row, = module.measure_b6((0,))
        self.assertEqual('expired', row['target_state'])  # the eligible lease was released
        self.assertEqual((128, 50), (row['blocked'], row['arrivals_per_batch']))
        self.assertLessEqual(row['batches'], module.B6_LIMIT_BATCHES, row)
        self.assertLess(max(row['batch_ms']), 50)

    @requirements('RS-B-9-B6', 'RS-10-FLAT', why_not_scenario=WHY)
    def test_b6_start_pass_work_is_equal_with_and_without_released_leases_of_history(self):
        module = measurements()
        empty, history = module.measure_b6((0, 3000))  # below the 4,096 lease-row cap; 100,000 is in the script
        self.assertEqual('expired', history['target_state'])
        self.assertLessEqual(history['batches'], module.B6_LIMIT_BATCHES)
        self.assertLessEqual(abs(sum(empty['batch_vm_steps']) - sum(history['batch_vm_steps'])), module.VM_TOLERANCE)

    @requirements('RS-10-DUE', why_not_scenario=WHY)
    def test_1000_due_holds_drain_in_batches_of_64_each_under_50_ms_and_well_inside_60_s(self):
        module = measurements()
        value = module.measure_due_holds(1000)
        self.assertEqual(1000, value['released'])
        self.assertLessEqual(value['batches'], 18)  # ceil((1000 + 64) / 64) + 1
        self.assertLess(value['maximum_batch_ms'], value['batch_limit_ms'], value)
        self.assertLess(value['total_seconds'], value['limit_seconds'], value)


if __name__ == '__main__':
    unittest.main()
