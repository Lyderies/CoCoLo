"""Slice B3 (W5, driver stopping): a stopped service's never-claimed open is withdrawn by the next service.

Moved out of test_open_withdrawal so that module stays inside its time budget: this class inherits the lost-run
settlement tests, each of which runs again here (see TESTING-framework.md).
"""
from __future__ import annotations

import json
import unittest

import tests.support  # noqa: F401
from tests.test_lost_run_settlement import LostRunAfterStopTests
from tests.test_open_withdrawal import WithdrawReads


@__import__('test_framework').requirements('RS-B-5', why_not_scenario='B3 checks the real codec, store, or enrolled service path; direct fault and custody controls expose the withdrawal boundary.')
class SuccessorWithdrawalTests(WithdrawReads, LostRunAfterStopTests):
    """W5 (driver stopping): a stopped service's never-claimed open is withdrawn by the next service's start pass."""

    def test_W5_a_stopped_services_never_claimed_open_is_withdrawn_by_the_next_service(self):
        *_, run_handle = self.opened()
        self.assertEqual('held', self.project_row(run_handle)[0])
        self.restart_host()
        self.wait_for(lambda: (self.project_row(run_handle) or ('held',))[0] == 'released', seconds=30)
        self.assertEqual(('released', 'withdrawn'), self.project_row(run_handle)[:2])
        self.assertEqual('service_stopped', json.loads(self.project_row(run_handle)[4])['reason'])
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(SuccessorWithdrawalTests))
    return suite


if __name__ == '__main__':
    unittest.main()
