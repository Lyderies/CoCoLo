"""Original pre-admission cleanup storage; synthetic protected-owner fixtures."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from unittest import mock
import unittest

import tests.support  # noqa: F401
from tests import test_result_leases as fixtures
from tests.learning_support import assignment
from codex_agents import project_cleanup
from codex_agents.claim_storage import lease_data, lease_state_seal, verify_pending_storage_evidence
from codex_agents.history_contracts import HistoryError


class ProjectCleanupReservationTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    @property
    def store(self):
        return self.base.store

    def count(self):
        return self.store._connection.execute('SELECT COUNT(*) FROM project_cleanup_reservations').fetchone()[0]

    def test_original_slot_is_reserved_atomically_before_arming_and_survives_reopen(self):
        first = self.base.reserve()
        self.assertIsNotNone(first.cleanup_reservation_digest)
        self.assertEqual(1, self.count())
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM result_reservations').fetchone()[0])
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_cleanup_receipts').fetchone()[0])
        self.base.fixture.now += 100
        self.assertEqual(first, self.base.reserve())
        self.base.reopen()
        self.assertEqual(first, self.base.reserve())
        pending = self.store.arm_result_lease(fixtures.lease_binding())
        raw = self.store.attest_pending_result_lease(fixtures.lease_binding())
        self.assertEqual(pending, verify_pending_storage_evidence(fixtures.fixtures.KEY, fixtures.lease_binding(), raw))
        self.assertEqual(first.cleanup_reservation_digest, pending.cleanup_reservation_digest)

    def test_deleted_or_changed_original_reservation_refuses_known_lease_arm_and_evidence(self):
        self.base.reserve()
        row = tuple(self.store._connection.execute('SELECT * FROM project_cleanup_reservations').fetchone())
        for mutation in ('DELETE FROM project_cleanup_reservations',
                         "UPDATE project_cleanup_reservations SET lease_digest='" + 'f' * 64 + "'",
                         "UPDATE project_cleanup_reservations SET state_digest='" + 'f' * 64 + "'"):
            with self.subTest(mutation=mutation):
                self.store._connection.execute(mutation)
                for operation in (self.base.reserve, lambda: self.store.arm_result_lease(fixtures.lease_binding()),
                                  lambda: self.store.attest_pending_result_lease(fixtures.lease_binding())):
                    with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
                        operation()
                self.store._connection.execute('DELETE FROM project_cleanup_reservations')
                self.store._connection.execute('INSERT INTO project_cleanup_reservations VALUES(?,?,?,?,?,?,?,?)', row)
        self.assertEqual(1, self.count())

    def test_bounded_cleanup_slots_are_reserved_before_launch_and_not_reused_on_expiry(self):
        with mock.patch.object(project_cleanup, 'MAX_CLEANUP_RESERVATIONS', 1):
            self.base.reserve()
            self.base.fixture.now = 61_000
            self.assertEqual(1, self.store.expire_result_leases())
            with self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
                self.base.reserve(2)
        self.assertEqual(1, self.count())
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM result_leases').fetchone()[0])
        self.assertEqual('expired', self.base.reserve().state)

    def test_failure_after_reservation_insert_rolls_back_both_original_rows(self):
        original = project_cleanup.reserve_cleanup

        def fail(store, lease):
            original(store, lease)
            raise OSError('PRIVATE-WRITER-FAILURE')

        with mock.patch('codex_agents.project_history.reserve_cleanup', side_effect=fail):
            with self.assertRaisesRegex(HistoryError, 'history_unavailable'):
                self.base.reserve()
        self.assertEqual(0, self.count())
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM result_leases').fetchone()[0])
        self.base.reserve()
        self.assertEqual(1, self.count())

    def test_lost_commit_reply_recovers_same_original_reservation(self):
        original = self.store._transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-ACK')

        with mock.patch.object(self.store, '_transaction', lose_reply):
            with self.assertRaisesRegex(HistoryError, 'history_unavailable'):
                self.base.reserve()
        first = self.store.result_lease(fixtures.lease_binding())
        self.base.reopen()
        self.assertEqual(first, self.base.reserve())
        self.assertEqual(1, self.count())

    def test_real_v8_layout_upgrade_preserves_legacy_wire_seal_and_does_not_reserve_cleanup(self):
        lease = self.base.reserve()
        legacy = replace(lease, cleanup_reservation_digest=None)
        self.store._connection.execute('DELETE FROM project_cleanup_reservations')
        self.store._connection.execute('UPDATE result_leases SET cleanup_reservation_digest=NULL,state_digest=?',
                                       (lease_state_seal(fixtures.fixtures.KEY, legacy),))
        self.store._connection.execute('DROP TABLE project_cleanup_receipts')
        self.store._connection.execute('DROP TABLE project_cleanup_reservations')
        self.store._connection.execute('ALTER TABLE result_leases DROP COLUMN cleanup_reservation_digest')
        self.store._connection.execute('PRAGMA user_version=8')
        before = tuple(self.store._connection.execute('SELECT * FROM result_leases').fetchone())
        self.store.close()
        path = self.base.fixture.directory / 'history.sqlite3'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.base.fixture.open()
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.base.fixture.store = self.base.fixture.open(allow_schema_upgrade=True)
        after = tuple(self.store._connection.execute('SELECT * FROM result_leases').fetchone())
        self.assertEqual((*before, None), after)
        self.assertEqual(legacy, self.store.result_lease(fixtures.lease_binding()))
        self.assertNotIn('cleanup_reservation_digest', lease_data(legacy))
        self.assertEqual(0, self.count())
        self.assertEqual(legacy, self.store.reserve_result_lease(fixtures.lease_binding(), assignment()))


if __name__ == '__main__':
    unittest.main()
