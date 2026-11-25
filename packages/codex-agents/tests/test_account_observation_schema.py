"""Original schema11/12 rows and verified backups survive explicit schema13 upgrade."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from tests import test_account_api as fixtures
from tests.learning_support import event_record, fake_provider_tools, trusted_tools
from tests.test_account_launch_claims import drop_admission_evidence_for_legacy_fixture
from codex_agents import learning_store
from codex_agents.learning_store import LearningLedger, LedgerError


def drop_launch_and_wake_for_legacy_fixture(db):
    """RS-E E2a-1 / E2c (0.8.60 full-run fixes, setup only): the additive launch columns and the machine-local
    wake/starter tables are newer than schemas 11 and 12; a ledger those releases wrote had none of them."""
    for table in ('project_starts', 'project_wake_instances', 'project_wake'):
        if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
            if db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
                raise AssertionError('cannot discard wake or starter state in a legacy fixture')
            db.execute('DROP TABLE ' + table)
    for column in ('launch_digest', 'launch_copy'):
        if column in {row[1] for row in db.execute('PRAGMA table_info(projects)')}:
            if db.execute(f'SELECT 1 FROM projects WHERE {column} IS NOT NULL').fetchone():
                raise AssertionError('cannot discard a launch record in a legacy fixture')
            db.execute(f'ALTER TABLE projects DROP COLUMN {column}')


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

    def add_legacy_outcome(self):
        """OR-3: an outcome a 0.8.42 host wrote, with the since-split `inconsistent_axes` repair code.
        Inserted as stored cells, the way an older build left it (today's writer is not asked to make it)."""
        from codex_agents.learning_contracts import canonical_json, content_digest
        from tests.learning_support import verification_outcome
        record = verification_outcome('legacy_outcome')
        record['repairs'] = 1
        record['repair_reason_codes'] = ['inconsistent_axes']
        db = self.ledger._connection
        project = self.fixture.projects[0]
        sequence = db.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?', (project,)).fetchone()[0] + 1
        content = canonical_json(record)
        row = (project, record['record_id'], 'outcome', 'host', sequence, 1, 1, None, content, content_digest(record), 0)
        db.execute('INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                   'ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,?)', row)
        db.commit()
        return project, content

    def eleven(self, *, backups=True):
        db = self.ledger._connection
        drop_admission_evidence_for_legacy_fixture(db)
        db.execute('DROP TABLE account_billing_probe_requests')
        db.execute('DROP TABLE account_billing_probe_origins')
        db.execute('DROP TABLE account_observation_requests')
        db.execute('DROP TABLE account_observation_origins')
        db.execute('DROP INDEX IF EXISTS records_observed_at')  # O10 AP: 0.4.0's additive index is newer
        db.execute('DROP INDEX IF EXISTS records_type_project')  # O10 AP5: likewise
        db.execute('DROP INDEX IF EXISTS records_project_digest')  # 3.8 AP5-2: likewise
        for name in ('records_run', 'records_candidate', 'records_type_observed', 'records_foreign_admissions',
                     'account_cleanup_receipts_unreleased'):
            db.execute('DROP INDEX IF EXISTS ' + name)  # U3 additive indexes postdate schema 11.
        db.execute('DROP INDEX IF EXISTS host_run_bindings_live')  # O0b B0-1: likewise
        db.execute('DROP INDEX IF EXISTS billing_probes_in_progress')  # O0b B0-5: likewise
        db.execute('DROP INDEX IF EXISTS account_open_requests_in_flight')  # OP-1: likewise
        db.execute('DROP INDEX IF EXISTS account_open_requests_finished')  # OP-2: likewise
        db.execute('DROP INDEX IF EXISTS account_open_requests_unfinished')  # O0c P0: likewise
        db.execute('DROP INDEX IF EXISTS account_open_requests_committed_expiry')  # O0c P0 (M1): likewise
        from codex_agents.run_holds_schema import RUN_HOLDS_OBJECTS  # O0b RS-A: schema 15's register, likewise
        for kind, name, _ in RUN_HOLDS_OBJECTS:
            if kind == 'trigger':
                db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute('DROP TABLE IF EXISTS run_holds')
        drop_launch_and_wake_for_legacy_fixture(db)
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
            columns = [r[1] for r in ledger._connection.execute('PRAGMA table_info(' + name + ')')
                       if not (name == 'records' and r[1] == 'probe_id')
                       and not (name == 'projects' and r[1] in ('launch_copy', 'launch_digest'))]
            self.assertEqual(rows, [tuple(row) for row in ledger._connection.execute(
                'SELECT ' + ','.join(columns) + ' FROM ' + name)], name)
        self.assertIsNone(ledger._connection.execute('SELECT 1 FROM records WHERE probe_id IS NOT NULL').fetchone())
        # E2a-1 (0.8.60 full-run fixes): the upgrade adds the launch columns empty; it never invents a launch record.
        self.assertIsNone(ledger._connection.execute(
            'SELECT 1 FROM projects WHERE launch_copy IS NOT NULL OR launch_digest IS NOT NULL').fetchone())

    def test_default_refuses_without_writes_and_explicit_upgrade_keeps_all_original_cells(self):
        self.eleven()
        original = hashlib.sha256(self.state.database.read_bytes()).hexdigest()
        with self.assertRaises(LedgerError):
            LearningLedger(self.state)
        self.assertEqual(original, hashlib.sha256(self.state.database.read_bytes()).hexdigest())
        upgraded = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = upgraded
        self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(upgraded)
        for name in ('account_observation_origins', 'account_observation_requests'):
            self.assertEqual(0, upgraded._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0])
        project = self.fixture.projects[0]
        upgraded.create_sanitized_backup(project, file_id='schema13', expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
        verified = upgraded.verify_sanitized_backup(project, file_id='schema13')
        self.assertEqual(17, verified['ledger_schema_version'])  # O0b RS-A (requirement)
        self.assertEqual(0, verified['counts']['account_observation_requests'])
        self.assertEqual(0, verified['counts']['account_billing_probe_origins'])
        self.assertEqual(0, verified['counts']['account_billing_probe_requests'])

    def test_or3_upgrade_backup_verify_keep_a_legacy_inconsistent_axes_outcome_byte_identical(self):
        """OR-3: the owner's real ledger held outcomes with the old `inconsistent_axes` repair code; the
        15 -> 16 upgrade refused with 'project backup contains a corrupt learning record'."""
        project, content = self.add_legacy_outcome()
        self.eleven()
        upgraded = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = upgraded
        self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(upgraded)
        kept = upgraded._connection.execute(
            'SELECT content FROM records WHERE project_id=? AND record_id=?', (project, 'legacy_outcome')).fetchone()[0]
        self.assertEqual(content, bytes(kept))
        upgraded.create_sanitized_backup(project, file_id='after_upgrade', expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
        verified = upgraded.verify_sanitized_backup(project, file_id='after_upgrade')
        self.assertEqual(17, verified['ledger_schema_version'])
        self.assertEqual(content, bytes(upgraded._connection.execute(
            'SELECT content FROM records WHERE project_id=? AND record_id=?', (project, 'legacy_outcome')).fetchone()[0]))

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
        self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(recovered)

    def twelve(self, *, backups=True):
        db = self.ledger._connection
        drop_admission_evidence_for_legacy_fixture(db)
        db.execute('DROP TABLE account_billing_probe_requests')
        db.execute('DROP TABLE account_billing_probe_origins')
        drop_launch_and_wake_for_legacy_fixture(db)
        db.execute('PRAGMA user_version=12')
        if backups:
            with mock.patch.object(learning_store, 'LEDGER_SCHEMA_VERSION', 12):
                for index, project in enumerate(self.fixture.projects):
                    file_id = 'schema12_' + str(index)
                    self.ledger.create_sanitized_backup(project, file_id=file_id,
                        expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
                    verified = self.ledger.verify_sanitized_backup(project, file_id=file_id)
                    self.assertEqual(12, verified['ledger_schema_version'])
                    self.assertNotIn('account_billing_probe_requests', verified['counts'])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        self.before = {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)] for name in tables}
        self.ledger.close()

    def test_schema_twelve_upgrade_preserves_original_cells_and_installs_empty_probe_state(self):
        self.twelve()
        original = self.state.database.read_bytes()
        with self.assertRaises(LedgerError):
            LearningLedger(self.state)
        self.assertEqual(original, self.state.database.read_bytes())
        upgraded = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = upgraded
        self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(upgraded)
        for name in ('account_billing_probe_origins', 'account_billing_probe_requests'):
            self.assertEqual(0, upgraded._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0])

    def test_schema_twelve_requires_current_verified_project_backups(self):
        self.twelve(backups=False)
        original = self.state.database.read_bytes()
        with self.assertRaisesRegex(LedgerError, 'backup'):
            LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual(original, self.state.database.read_bytes())

    def test_schema_twelve_interruption_rolls_back_probe_objects_and_version(self):
        self.twelve()
        class StopFixture(BaseException):
            pass
        class Cut(LearningLedger):
            def _initialize_billing_probes_locked(self):
                super()._initialize_billing_probes_locked()
                raise StopFixture()
        with self.assertRaises(StopFixture):
            Cut(self.state, allow_schema_upgrade=True)
        import sqlite3
        with closing(sqlite3.connect(self.state.database)) as db:
            self.assertEqual(12, db.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual([], db.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('account_billing_probe_origins','account_billing_probe_requests',"
                "'billing_probe_active_machine','billing_probe_active_account')").fetchall())
        recovered = LearningLedger(self.state, allow_schema_upgrade=True)
        self.fixture.ledger = recovered
        self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assert_old_rows(recovered)
