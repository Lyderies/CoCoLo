"""S13: the RS-B-9 budgets B1 and B6 and the RS-10 budget for 1,000 due holds, measured on product operations.

Small scale only: the 100,000-tombstone numbers come from scripts/measure_s13.py (docs/requirements/s13.md).

Protocol (so a scheduling pause on a loaded machine cannot fail a run without a product regression):
  - the gates that decide a result are counts and work: releases per batch (at most 64), batches, the rows a pass
    selects, SQLite VM steps, which hold rows were backed off;
  - wall-clock is a loose bound: each scenario is repeated on fresh stores and the figure is the median at each batch
    position (B1: one warm-up run, then the median of five), compared with the budget itself (5 ms, 50 ms, 60 s);
  - regression controls patch the product in this test only and must turn the same checks red.
S13a now requires the B6 mechanism; production drain still pins its measured miss (see docs/requirements/s13/STATUS.md).
"""
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
WHY = 'Real project store and register operations on disposable stores; no service boundary is patched.'
WHY_CONTROL = 'A regression control: the product is patched in this test only, to show the check goes red.'


def measurements():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location('measure_s13', SCRIPTS / 'measure_s13.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def check_b1(test, value):
    test.assertEqual(1, value['warmup_runs'])
    test.assertEqual(value['runs'], len(value['register_samples_ms']))
    # Every register call of a normal run was seen: takes, the arm, the release and the obligation release (the
    # reservation/hold JOIN is inside `_release_obligation`, which is timed whole).
    test.assertTrue({'take_hold', 'arm_hold', 'release_hold', '_release_obligation'} <= set(value['register_calls']), value)
    test.assertEqual(value['runs'] + 1, value['reservations_released_completed'])  # one per run, warm-up included
    test.assertEqual(4, len(value['memory_transactions_samples_ms'][0]))
    test.assertLess(value['register_ms'], 5)
    test.assertLess(value['memory_writer_ms'], 5)
    test.assertLess(value['total_ms'], value['limit_ms'], value)


def check_b6_budget(test, module, row):
    test.assertEqual('expired', row['target_state'])  # the eligible lease was released
    test.assertEqual((128, 50), (row['blocked'], row['arrivals_per_batch']))
    test.assertLessEqual(row['batches'], module.B6_LIMIT_BATCHES, row)
    test.assertEqual(row['batches'], len(row['batch_released']))
    test.assertTrue(all(count <= 64 for count in row['batch_released']), row)  # releases per batch
    test.assertLess(max(row['batch_ms']), 50)  # median over the repeats at each batch position


def b6_mechanism_violations(row):
    """RS-B draft 4 section 5: a batch selects at most 64 rows (from the partial index, oldest check first) and a blocked
    row gets a back-off in the batch that finds it blocked, so it is not selected again before it is due. Violations
    seen in one measured row (`measure_b6`); an empty list means the mechanism is met."""
    found = []
    if max(row['batch_selected']) > 64:
        found.append('selection unbounded: %d rows selected in one batch' % max(row['batch_selected']))
    if not row['batch_backed_off'] or min(row['batch_backed_off']) < 1:
        found.append('no back-off: blocked rows are examined again by every batch')
    return found


def check_due(test, value):
    test.assertEqual(1000, value['released'])
    test.assertLessEqual(value['batches'], 18)  # ceil((1000 + 64) / 64) + 1
    test.assertTrue(all(count <= 64 for count in value['batch_released']), value['batch_released'])  # per batch
    test.assertLess(value['maximum_batch_ms'], value['batch_limit_ms'], value)
    test.assertLess(value['total_seconds'], value['limit_seconds'], value)


def production_violations(value):
    """RS-10 on the production path: 1,000 due holds released within 60 s by the service's own maintenance cadence."""
    found = []
    if value['released'] != value['due']:
        found.append('not all released')
    elif value['seconds_until_drained'] >= value['limit_seconds']:
        found.append('drain took %.0f s (60 s budget): %d passes of at most %d, one every %.0f s; %d released by 60 s' % (
            value['seconds_until_drained'], value['passes'], value['pass_limit'], value['cadence_seconds'],
            value['released_within_60_s']))
    return found


class S13Budgets(unittest.TestCase):
    @requirements('RS-B-9-B1', why_not_scenario=WHY)
    def test_b1_register_and_memory_writer_work_of_a_normal_run_is_under_5_ms(self):
        value = measurements().measure_b1()
        check_b1(self, value)
        # The commit cost the register adds is measured as a difference, not left out.
        self.assertEqual(9, value['commit_delta']['pairs'])
        self.assertGreaterEqual(value['commit_ms'], 0)

    @requirements('RS-B-9-B6', why_not_scenario=WHY)
    def test_b6_one_eligible_row_behind_128_blocked_and_50_arrivals_per_batch_within_4_batches(self):
        module = measurements()
        row, = module.measure_b6((0,))
        check_b6_budget(self, module, row)

    @requirements('RS-B-9-B6-MECH', why_not_scenario=WHY)
    def test_b6_finding_the_lease_start_pass_has_no_bounded_selection_and_no_back_off(self):
        # S13a implements the section 5 mechanism; keep S13's measurement and require no violations.
        row, = measurements().measure_b6((0,), repeats=1)
        self.assertEqual([], b6_mechanism_violations(row), row)

    @requirements('RS-B-9-B6', 'RS-10-FLAT', why_not_scenario=WHY)
    def test_b6_start_pass_work_is_equal_with_and_without_released_leases_of_history(self):
        module = measurements()
        empty, history = module.measure_b6((0, 3000), repeats=1)  # below the 4,096 lease-row cap; 100,000 is in the script
        self.assertEqual('expired', history['target_state'])
        self.assertLessEqual(history['batches'], module.B6_LIMIT_BATCHES)
        self.assertLessEqual(abs(sum(empty['batch_vm_steps']) - sum(history['batch_vm_steps'])), module.VM_TOLERANCE)

    @requirements('RS-10-DUE', why_not_scenario=WHY)
    def test_1000_due_holds_drain_in_batches_of_64_each_under_50_ms_and_well_inside_60_s(self):
        check_due(self, measurements().measure_due_holds(1000))

    @requirements('RS-10-DUE-PROD', why_not_scenario=WHY)
    def test_rs10_finding_the_service_maintenance_cadence_misses_the_60_s_drain_of_1000_due_holds(self):
        # FINDING (S13 fix round): production runs the lease pass with limit=10 once every 10 s (driver
        # `_expire_results`, writer `expire` pass), so 1,000 due holds take about 100 passes, near 990 s. This pins the
        # measured miss; when the proposed step lands, this assertion is replaced by `[] == violations`.
        value = measurements().measure_due_holds_production(1000)
        self.assertEqual(1000, value['released'])
        self.assertEqual(10, value['pass_limit'])
        self.assertEqual(100, value['passes'])
        self.assertEqual(60, value['released_within_60_s'])
        self.assertEqual(1, len(production_violations(value)), value)
        self.assertGreater(value['seconds_until_drained'], 900)


class S13RegressionControls(unittest.TestCase):
    """Each control patches the product here only and must turn the matching check red."""

    @requirements('RS-S13-CONTROLS', why_not_scenario=WHY_CONTROL)
    def test_control_a_pass_that_releases_128_rows_per_call_fails_the_per_batch_limit(self):
        from codex_agents.project_history import ProjectHistory
        original = ProjectHistory.release_earlier_unarmed_leases
        def twice(self, live, *, limit=10):
            return original(self, live, limit=limit) + original(self, live, limit=limit)
        with mock.patch.object(ProjectHistory, 'release_earlier_unarmed_leases', twice):
            value = measurements().measure_due_holds(1000, repeats=1)
        self.assertEqual(128, max(value['batch_released']))
        with self.assertRaises(AssertionError):
            check_due(self, value)

    @requirements('RS-S13-CONTROLS', why_not_scenario=WHY_CONTROL)
    def test_control_a_slowed_reservation_hold_join_fails_b1(self):
        from codex_agents import project_release_ack
        original = project_release_ack._release_obligation
        def slow(store, lease, digest):
            time.sleep(0.006)  # stands for a JOIN that costs 6 ms more
            return original(store, lease, digest)
        with mock.patch.object(project_release_ack, '_release_obligation', slow):
            value = measurements().measure_b1(runs=3)
        self.assertGreater(value['register_ms'], 5)
        with self.assertRaises(AssertionError):
            check_b1(self, value)

    @requirements('RS-S13-CONTROLS', why_not_scenario=WHY_CONTROL)
    def test_control_the_mechanism_check_separates_bounded_back_off_from_unbounded_and_none(self):
        # The control is on the check: bounded selection with back-off is clean; removing either part turns it red.
        met = {'batch_selected': [64, 64], 'batch_backed_off': [64, 128]}
        self.assertEqual([], b6_mechanism_violations(met))
        self.assertEqual(['no back-off: blocked rows are examined again by every batch'],
                         b6_mechanism_violations({**met, 'batch_backed_off': [0, 0]}))
        self.assertEqual(['selection unbounded: 128 rows selected in one batch'],
                         b6_mechanism_violations({**met, 'batch_selected': [64, 128]}))
        self.assertEqual(2, len(b6_mechanism_violations({'batch_selected': [179], 'batch_backed_off': [0]})))

    @requirements('RS-S13-CONTROLS', why_not_scenario=WHY_CONTROL)
    def test_control_a_pass_that_drains_everything_clears_the_production_finding(self):
        from codex_agents.project_history import ProjectHistory
        original = ProjectHistory.release_earlier_unarmed_leases
        def everything(self, live, *, limit=10):
            total = 0
            while True:
                released = original(self, live, limit=64)
                total += released
                if not released:
                    return total
        with mock.patch.object(ProjectHistory, 'release_earlier_unarmed_leases', everything):
            value = measurements().measure_due_holds_production(1000)
        self.assertEqual([], production_violations(value), value)


if __name__ == '__main__':
    unittest.main()
