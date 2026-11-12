"""Explicit schema9 cleanup journal migration; no invented account/native ACK."""
from contextlib import contextmanager
import hashlib
import unittest

import tests.support  # noqa: F401
from tests import test_result_leases as fixtures
from tests.project_schema_fixtures import legacy_learning_layout
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory


class ProjectReleaseAckSchemaTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def old_nine(self):
        self.original = self.base.reserve()
        legacy_learning_layout(self.base.store)
        db = self.base.store._connection
        db.execute('DROP TABLE project_cleanup_acknowledgements')
        db.execute('ALTER TABLE project_cleanup_reservations DROP COLUMN account_release_digest')
        db.execute('PRAGMA user_version=9')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        self.before = {n: [tuple(r) for r in db.execute('SELECT * FROM '+n)] for n in tables}
        self.base.store.close()

    def test_old_schema_refuses_without_writes_and_explicit_upgrade_keeps_original_rows(self):
        self.old_nine()
        path = self.base.fixture.directory/'history.sqlite3'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError,'history_incompatible'):
            self.base.fixture.open()
        self.assertEqual(digest,hashlib.sha256(path.read_bytes()).hexdigest())
        self.base.fixture.store = self.base.fixture.open(allow_schema_upgrade=True)
        db = self.base.store._connection
        self.assertEqual(15,db.execute('PRAGMA user_version').fetchone()[0])
        for name, rows in self.before.items():
            after = [tuple(r) for r in db.execute('SELECT * FROM '+name)]
            self.assertEqual([(*r,None) for r in rows] if name=='project_cleanup_reservations' else rows, after)
        self.assertEqual(self.original,self.base.store.result_lease(fixtures.lease_binding()))
        self.assertEqual(0,db.execute('SELECT COUNT(*) FROM project_cleanup_acknowledgements').fetchone()[0])
        with self.assertRaisesRegex(HistoryError,'history_result_not_ready'):
            self.base.store.account_release_receipt(fixtures.lease_binding())

    def test_interrupted_schema_upgrade_rolls_back_new_ack_table_and_column(self):
        self.old_nine()
        class StopFixture(BaseException):
            pass
        class Cut(ProjectHistory):
            @contextmanager
            def _transaction(self):
                with super()._transaction():
                    yield
                    raise StopFixture()
        options = dict(result_key=fixtures.fixtures.KEY,result_capacity=self.base.fixture.capacity,
                       result_clock_ms=lambda:self.base.fixture.now,allow_schema_upgrade=True)
        with self.assertRaises(StopFixture):
            Cut(self.base.fixture.directory,self.base.store.project_id,**options)
        self.base.fixture.store = self.base.fixture.open(allow_schema_upgrade=True)
        self.assertEqual(self.original,self.base.reserve())


if __name__ == '__main__':
    unittest.main()
