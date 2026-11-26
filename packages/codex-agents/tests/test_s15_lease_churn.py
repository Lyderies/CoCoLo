"""O0c/P3, B0-6: failed-before-launch leases must leave capacity observable.

Reduce only the independent lease verification budget (4096 -> 2), as the R8
lifecycle test reduces its limits. All rows and releases use real store writers;
no SQL state planting, backdating, or replacement release implementation.
"""
import unittest
from unittest import mock

from test_framework import requirements
from tests.test_result_leases import ResultLeaseTests, lease_binding
from codex_agents import project_history


class LeaseChurnTests(unittest.TestCase):
    @requirements('S15-O0C-P3', why_not_scenario='Three actual reserve/release_unsent lifecycles cross a reduced verification limit in a real project store without sockets.')
    def test_failed_before_launch_past_lease_verification_limit_keeps_live_counts_readable(self):
        fixture = ResultLeaseTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.store
        baseline = store._result_counts()
        cap = 2
        with mock.patch.object(project_history, 'MAX_LEASE_ROWS', cap):
            for number in range(1, cap + 2):
                with self.subTest(lifecycle=number):
                    fixture.reserve(number)
                    released = store.release_unsent_result_lease(lease_binding(number))
                    self.assertEqual('expired', released.state)
                    self.assertEqual(released, store.release_unsent_result_lease(lease_binding(number)))
                    hold = store._connection.execute(
                        "SELECT state,release_kind FROM project_holds WHERE kind='lease' AND identity=?",
                        (f'{released.binding.run_handle}:{released.binding.attempt}',)).fetchone()
                    self.assertEqual(('released', 'withdrawn_unsent'), tuple(hold))
                    # Every local obligation has ended, including the extra lifecycle.
                    # Status/admission must still be able to read the live capacity.
                    self.assertEqual(baseline, store._result_counts())


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LeaseChurnTests)


if __name__ == '__main__':
    unittest.main()
