"""O0b B0-6: every hold a child run takes ends by itself (PLAN B0-1 to B0-6; DESIGN-0.4.0-B0-holds.md, B0-6
as revised in revision 3).

Integration stage: the in-process collector, account and project services (the test_result_lifetime fixture).
Each scenario records a baseline, shows it took the hold it targets, stops all client action at the failure
point, lets only production timers and sweeps run to a stated deadline, then checks every count before
teardown. Closing the orchestration stays the primary's step: scenarios check "closable" separately.

This module is written before any B0 code: each failure exit here fails on 0.8.37, by design, and is
listed with the B0 step that makes it pass.
"""
import json
import unittest
from unittest import mock

from tests.test_bridge import null_filled, result
from tests.test_result_lifetime import ResultLifetimeTests

from codex_agents import codex_preparation


class HoldLifecycleTests(ResultLifetimeTests):
    def held(self):
        """Every hold this harness watches, by name (revision 3's list grows with each B0 step)."""
        ledger = self.collector_ledger._connection
        now = self.collector_ledger_now_ms()
        return {
            'live_results': self.cli('learning-status')['project_capacity']['results']['live'],
            'account_slots': ledger.execute(
                'SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0],
            # B0-1: a pending binding the cap and the drain still count.
            'pending_bindings': ledger.execute(
                "SELECT COUNT(*) FROM host_run_bindings WHERE binding_state='pending' AND expires_at_ms>?",
                (now,)).fetchone()[0],
        }

    def collector_ledger_now_ms(self):
        import time
        return time.time_ns() // 1_000_000

    def test_B0_6_baseline_is_empty(self):
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    def test_B0_1_a_run_refused_before_launch_frees_its_account_binding_by_itself(self):
        """B0-1 (fails on 0.8.37): the binding stays pending for 24 h, held against the cap and every drain."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.assertEqual(1, self.held()['pending_bindings'])  # the hold was taken
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        # No further client action: only the service's own work may free it.
        self.wait_for(lambda: self.held()['pending_bindings'] == 0, seconds=30)
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    def test_B0_6_a_completed_run_frees_every_hold(self):
        """Control: today's successful lifecycle already frees what it took (once recorded)."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.record(context, route_path, run_handle,
                    self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle))
        self.wait_for(lambda: self.held()['account_slots'] == 0 and self.held()['live_results'] <= 1, seconds=30)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited lifetime tests run in test_result_lifetime."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(HoldLifecycleTests):
        if name in HoldLifecycleTests.__dict__:
            suite.addTest(HoldLifecycleTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
