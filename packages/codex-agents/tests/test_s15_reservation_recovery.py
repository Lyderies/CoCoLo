"""S15 B4s (owner decisions 132/377): socket-free reproduction at the real recovery writer boundary.

Use the existing real-store settlement fixture: reserve, arm and bind through
ProjectHistory, reopen the store as a successor, arbitrate, open the durable
outbox, then record the product-authenticated account closure. No direct SQL
writes, synthetic terminal, or replacement release implementation.
The service-level counterpart remains in test_hold_lifecycle for the primary.
"""
import unittest

from test_framework import requirements
from tests.test_account_settlement import SettlementFixture
from tests.test_publication_replay import claim_binding


class ReservationRecoveryTests(SettlementFixture):
    @requirements('S15-B0-2', why_not_scenario='Real project-store successor recovery and authenticated closure recording isolate the same lost-reservation release path without service sockets.')
    def test_successor_settlement_releases_once_frees_capacity_and_writes_no_terminal(self):
        target = claim_binding(1)
        baseline = self.store._result_counts()[0]
        self.bound(1)
        self.assertEqual(baseline + 1, self.store._result_counts()[0])
        self.assertIsNone(self.reservation(1).terminal_record_id)

        self.successor()
        self.open(1)
        counts = self.counts()
        raw = self.closure(1)
        receipt = self.record_settlement(1, raw)
        # B4s ends the obligation; the ordinary expiry pass retires its body slot.
        self.assertEqual(('lost', False), (receipt['release_kind'], receipt['known']))
        self.assertEqual(('released', 'lost'), self.hold('reservation', target.claim_id)[:2])
        # Read without triggering result_reservation()'s lazy retirement, just
        # as the original service test's reservation_terminal() does.
        row = self.store._connection.execute(
            'SELECT state, terminal_record_id FROM result_reservations WHERE run_handle=?',
            (target.run_handle,)).fetchone()
        self.assertEqual('retiring', row['state'])
        self.consistent()
        self.assertIsNone(row['terminal_record_id'])
        self.assertEqual(counts, self.counts())
        self.assertEqual((target.claim_id, 'lost'), self.settlement(1)[:2])
        before = self.snapshot()
        self.successor()
        again = self.record_settlement(1, raw)
        self.assertEqual((receipt['receipt_digest'], True), (again['receipt_digest'], again['known']))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, self.store._connection.execute(
            'SELECT COUNT(*) FROM project_account_settlements').fetchone()[0])
        self.assertGreaterEqual(self.store.expire_results(), 1)
        self.assertEqual(baseline, self.store._result_counts()[0])
        self.bound(2)  # Released capacity admits fresh work without a client acknowledgement.
        self.assertEqual(baseline + 1, self.store._result_counts()[0])
        self.assertEqual(counts, self.counts())
        self.consistent()



def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ReservationRecoveryTests)


if __name__ == '__main__':
    unittest.main()
