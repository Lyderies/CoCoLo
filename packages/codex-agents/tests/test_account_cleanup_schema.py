"""Exact schema10 upgrade to the prelaunch cleanup journal; no native drain claim."""
import hashlib
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_open_schema as fixtures
from tests.test_account_launch_claims import drop_account_cleanup_for_legacy_fixture
from codex_agents import learning_store
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger, LedgerError


class AccountCleanupSchemaTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AccountOpenSchemaTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def old_ten(self, *, backup=True):
        ledger = self.base.ledger
        with ledger._account_transaction():
            drop_account_cleanup_for_legacy_fixture(ledger)
            ledger._connection.execute('PRAGMA user_version=10')
        body = learning_store._snapshot_project_partition(ledger._connection, self.base.project, ledger_schema_version=10)
        self.assertNotIn('account_cleanup_receipts', body['tables'])
        self.assertNotIn('cleanup_reservation_digest', body['columns']['capacity_executions'])
        if backup:
            raw = canonical_json({'backup_sha256':hashlib.sha256(canonical_json(body)).hexdigest(),'body':body})
            path = ledger._write_owner_file('backup',self.base.project,'old_ten',raw)
            ledger._register_stored_file(self.base.project,file_id='old_ten',kind='backup',path=path,
                digest=hashlib.sha256(raw).hexdigest(),expires_at_ms=time.time_ns()//1_000_000+60_000)
        self.base.close()

    def test_old_schema_refuses_before_writes_and_explicit_upgrade_needs_registered_backup(self):
        self.old_ten(backup=False)
        before = self.base.state.database.read_bytes()
        with self.assertRaisesRegex(LedgerError,'explicit drained-owner authorization'):
            LearningLedger(self.base.state)
        self.assertEqual(before,self.base.state.database.read_bytes())
        with self.assertRaisesRegex(LedgerError,'registered verified schema 10'):
            LearningLedger(self.base.state,allow_schema_upgrade=True)
        self.assertEqual(before,self.base.state.database.read_bytes())

    def test_verified_upgrade_preserves_old_manifest_and_invents_no_cleanup(self):
        self.old_ten()
        self.base.ledger = LearningLedger(self.base.state,allow_schema_upgrade=True)
        self.assertEqual(15,self.base.ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertTrue(self.base.ledger.verify_sanitized_backup(self.base.project,file_id='old_ten')['verified'])
        self.assertEqual(0,self.base.ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])
        self.assertEqual('cleanup_reservation_digest',self.base.ledger._connection.execute('PRAGMA table_info(capacity_executions)').fetchall()[-1][1])

    def test_interrupted_additive_ddl_rolls_back_complete_new_layout(self):
        self.old_ten()
        original = learning_store.install_cleanup_schema
        class StopFixture(BaseException):
            pass
        def interrupted(connection):
            original(connection)
            raise StopFixture()
        with mock.patch.object(learning_store,'install_cleanup_schema',side_effect=interrupted),self.assertRaises(StopFixture):
            LearningLedger(self.base.state,allow_schema_upgrade=True)
        self.assertEqual([(10,)],self.base.offline('PRAGMA user_version'))
        self.assertEqual([],self.base.offline("SELECT name FROM sqlite_master WHERE name='account_cleanup_receipts'"))
        self.assertNotIn('cleanup_reservation_digest',{r[1] for r in self.base.offline('PRAGMA table_info(capacity_executions)')})


if __name__ == '__main__':
    unittest.main()
