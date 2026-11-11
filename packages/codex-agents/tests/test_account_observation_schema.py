"""Schema11 layout/rows and verified backups survive explicit schema12 upgrade."""
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from tests import test_account_api as fixtures
from tests.learning_support import event_record, fake_provider_tools, trusted_tools
from codex_agents import learning_store
from codex_agents.learning_store import LearningLedger, LedgerError


class AccountObservationSchemaTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountApiTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger, self.state = self.fixture.ledger, self.fixture.state
        stamp = time.time_ns() // 1_000_000
        tools = self.fixture.base / 'legacy-tools'
        fake_provider_tools(tools)
        with trusted_tools(tools), mock.patch.object(self.ledger, '_clock_ms', return_value=stamp):
            for index, project in enumerate(self.fixture.projects):
                self.ledger.observe_provider(project, 'codex', project_root=self.fixture.roots[index], environ={})
                self.ledger.append(project, event_record('preserved_legacy_' + str(index), observed_at_ms=stamp),
                                   producer='hook', now_ms=stamp)

    def eleven(self, *, backups=True):
        db = self.ledger._connection
        db.execute('DROP TABLE account_observation_requests')
        db.execute('DROP TABLE account_observation_origins')
        db.execute('PRAGMA user_version=11')
        expected = json.loads((Path(__file__).parent / 'fixtures/learning/schema11-layout.json').read_text())
        actual = {kind + ':' + name: hashlib.sha256(' '.join(sql.replace('"', '').split()).encode()).hexdigest()
                  for kind, name, sql in db.execute('SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
        self.assertEqual(11, expected['schema_version'])
        self.assertEqual(expected['objects'], actual, 'fixture must match genuine prior-source layout')
        if backups:
            # The old manifest is independently restored by the current decoder;
            # no schema12 table or new observation authority enters that backup.
            with mock.patch.object(learning_store, 'LEDGER_SCHEMA_VERSION', 11):
                for index, project in enumerate(self.fixture.projects):
                    self.ledger.create_sanitized_backup(project, file_id='schema11_' + str(index),
                        expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
                    verified = self.ledger.verify_sanitized_backup(project, file_id='schema11_' + str(index))
                    self.assertEqual(11, verified['ledger_schema_version'])
                    self.assertNotIn('account_observation_requests', verified['counts'])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        self.before = {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)] for name in tables}
        self.ledger.close()

    def assert_old_rows(self, ledger):
        for name, rows in self.before.items():
            self.assertEqual(rows, [tuple(row) for row in ledger._connection.execute('SELECT * FROM ' + name)], name)

    def test_default_refuses_without_writes_and_explicit_upgrade_keeps_all_original_cells(self):
        self.eleven()
        original = hashlib.sha256(self.state.database.read_bytes()).hexdigest()
        with self.assertRaises(LedgerError):
            LearningLedger(self.state)
        self.assertEqual(original, hashlib.sha256(self.state.database.read_bytes()).hexdigest())
        upgraded = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = upgraded
        self.assertEqual(12, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(upgraded)
        for name in ('account_observation_origins', 'account_observation_requests'):
            self.assertEqual(0, upgraded._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0])
        project = self.fixture.projects[0]
        upgraded.create_sanitized_backup(project, file_id='schema12', expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
        verified = upgraded.verify_sanitized_backup(project, file_id='schema12')
        self.assertEqual(12, verified['ledger_schema_version'])
        self.assertEqual(0, verified['counts']['account_observation_requests'])

    def test_upgrade_requires_each_projects_current_verified_backup(self):
        self.eleven(backups=False)
        original = hashlib.sha256(self.state.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(LedgerError, 'backup'):
            LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual(original, hashlib.sha256(self.state.database.read_bytes()).hexdigest())

    def test_interrupted_upgrade_rolls_back_both_tables_and_releases_original_writer(self):
        self.eleven()
        class StopFixture(BaseException):
            pass
        class Cut(LearningLedger):
            def _initialize_account_observations_locked(self):
                super()._initialize_account_observations_locked()
                raise StopFixture()
        with self.assertRaises(StopFixture):
            Cut(self.state, allow_schema_upgrade=True)
        recovered = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = recovered
        self.assertEqual(12, recovered._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(recovered)
