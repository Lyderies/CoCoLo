"""Disposable account migration; no real native drain or provider work."""
from tests.support import SHORT_TMP
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import learning_store
from codex_agents.account_open_contracts import AccountOpenInput, OpenError, checked_account_open_reply, decode_account_open_input
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger, LearningState, LedgerError
from codex_agents.orchestration_api import OrchestrationScope


class AccountOpenSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='account-open-', dir=SHORT_TMP)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = LearningState(self.root/'state')
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.close)
        project_root = self.root/'project'
        project_root.mkdir()
        self.project = self.ledger.register_project(project_root)

    def close(self):
        if self.ledger is not None:
            ledger, self.ledger = self.ledger, None
            ledger.close()

    def offline(self, query):
        connection = sqlite3.connect(self.state.database.as_uri()+'?mode=ro&immutable=1', uri=True)
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def old_nine(self, *, backup=True):
        with self.ledger._account_transaction():
            from tests.test_account_launch_claims import drop_account_cleanup_for_legacy_fixture
            drop_account_cleanup_for_legacy_fixture(self.ledger)
            self.ledger._connection.execute('DROP TABLE account_open_requests')
            self.ledger._connection.execute('DROP TABLE account_open_receipts')
            self.ledger._connection.execute('ALTER TABLE host_run_bindings DROP COLUMN history_owner')
            self.ledger._connection.execute('PRAGMA user_version=9')
        if backup:
            body = learning_store._snapshot_project_partition(self.ledger._connection, self.project, ledger_schema_version=9)
            self.assertNotIn('account_open_receipts', body['tables'])
            self.assertNotIn('history_owner', body['columns']['host_run_bindings'])
            raw = canonical_json({'backup_sha256': hashlib.sha256(canonical_json(body)).hexdigest(), 'body': body})
            path = self.ledger._write_owner_file('backup', self.project, 'old_nine', raw)
            self.ledger._register_stored_file(self.project, file_id='old_nine', kind='backup', path=path,
                digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=time.time_ns()//1_000_000+60_000)
        self.close()

    def test_fresh_layout_has_explicit_origin_and_independent_original_journals(self):
        db = self.ledger._connection
        self.assertEqual(15, db.execute('PRAGMA user_version').fetchone()[0])
        db.execute('INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,'
            'route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (self.project, 'a'*64, 'run:'+'b'*64, 'c'*64, 'd'*64, 'e'*64, 'f'*64, 'bridge-v1', 1, 2))
        self.assertEqual('legacy', db.execute('SELECT history_owner FROM host_run_bindings').fetchone()[0])
        for table in ('account_open_receipts', 'account_open_requests'):
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])

    def test_ordinary_old_open_refuses_before_database_writes_even_with_valid_backup(self):
        self.old_nine()
        before = self.state.database.read_bytes()
        with self.assertRaisesRegex(LedgerError, 'explicit drained-owner authorization'):
            LearningLedger(self.state)
        self.assertEqual(before, self.state.database.read_bytes())
        self.assertEqual([(9,)], self.offline('PRAGMA user_version'))
        self.assertNotIn('history_owner', {row[1] for row in self.offline('PRAGMA table_info(host_run_bindings)')})

    def test_explicit_old_upgrade_still_requires_exact_registered_backup(self):
        self.old_nine(backup=False)
        before = self.state.database.read_bytes()
        with self.assertRaisesRegex(LedgerError, 'registered verified schema 9'):
            LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual(before, self.state.database.read_bytes())
        self.assertEqual([], self.offline("SELECT name FROM sqlite_master WHERE name LIKE 'account_open_%'"))

    def test_explicit_old_upgrade_preserves_original_partition_and_never_invents_opens(self):
        self.old_nine()
        self.ledger = LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual(15, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertTrue(self.ledger.verify_sanitized_backup(self.project, file_id='old_nine')['verified'])
        for table in ('account_open_receipts', 'account_open_requests'):
            self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])

    def test_all_additive_ddl_and_version_roll_back_after_injected_interruption(self):
        class StopFixture(BaseException):
            pass
        self.old_nine()
        install = learning_store.install_open_schema
        def interrupted(connection):
            install(connection)
            raise StopFixture()
        with mock.patch.object(learning_store, 'install_open_schema', side_effect=interrupted), self.assertRaises(StopFixture):
            LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual([(9,)], self.offline('PRAGMA user_version'))
        self.assertEqual([], self.offline("SELECT name FROM sqlite_master WHERE name LIKE 'account_open_%'"))
        self.assertNotIn('history_owner', {row[1] for row in self.offline('PRAGMA table_info(host_run_bindings)')})
        self.ledger = LearningLedger(self.state, allow_schema_upgrade=True)
        self.assertEqual(15, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])

    def test_missing_or_changed_current_journal_schema_is_never_reconstructed(self):
        self.ledger._connection.execute('DROP TABLE account_open_requests')
        self.close()
        with self.assertRaisesRegex(LedgerError, 'original-open schema'):
            LearningLedger(self.state)
        self.assertEqual([], self.offline("SELECT name FROM sqlite_master WHERE name='account_open_requests'"))

    def test_latest_backup_preserves_new_manifests_and_original_origin(self):
        self.ledger.create_sanitized_backup(self.project, file_id='latest', expires_at_ms=time.time_ns()//1_000_000+60_000)
        checked = self.ledger.verify_sanitized_backup(self.project, file_id='latest')
        self.assertEqual(15, checked['ledger_schema_version'])  # O0b RS-A (requirement)
        self.assertEqual(0, checked['counts']['account_open_receipts'])
        self.assertEqual(0, checked['counts']['account_open_requests'])

    def test_non_boolean_upgrade_option_is_not_authorization(self):
        self.close()
        with self.assertRaisesRegex(LedgerError, 'option is invalid'):
            LearningLedger(self.state, allow_schema_upgrade=1)


class AccountOpenContractsTests(unittest.TestCase):
    def setUp(self):
        self.scope = OrchestrationScope('prj_'+'a'*32, 'primary', 'b'*64, 'supervisor', 1)
        self.request = AccountOpenInput(self.scope, 'orc_'+'c'*48, 'd'*64)

    def test_request_cannot_supply_assignments_permissions_clocks_or_passed_flags(self):
        for field, value in (('assignment', {}), ('parent_grant', {}), ('now_ms', 1), ('admitted', True)):
            with self.subTest(field=field), self.assertRaisesRegex(OpenError, 'open_invalid_request'):
                decode_account_open_input({**self.request.payload(), field: value}, self.scope.project_id, self.scope.runtime_digest)
        payload = self.request.payload()
        payload['scope']['supervisor_generation'] = True
        with self.assertRaisesRegex(OpenError, 'open_invalid_request'):
            decode_account_open_input(payload, self.scope.project_id, self.scope.runtime_digest)

    def test_opened_and_denied_replies_keep_closed_original_metadata_and_exact_types(self):
        reply = {'state': 'opened', 'run_handle': 'hrn_'+'e'*48, 'run_id': 'run:'+'f'*64,
            'inputs_digest': self.request.inputs_digest, 'scope_digest': '1'*64, 'receipt_digest': '2'*64,
            'admission_record_id': 'admission:fixture', 'admission_content_digest': '3'*64, 'reason_code': 'admitted',
            'recorded_at_ms': 1, 'observation_deadline_at_ms': 2, 'known': False}
        self.assertEqual(reply, checked_account_open_reply(reply, self.request))
        for name, value in (('recorded_at_ms', True), ('known', 1), ('receipt_digest', 'changed'), ('accepted', True)):
            changed = deepcopy(reply)
            changed[name] = value
            with self.subTest(name=name), self.assertRaisesRegex(OpenError, 'open_invalid_request'):
                checked_account_open_reply(changed, self.request)
        denied = {**reply, 'state': 'denied', 'run_handle': None, 'run_id': None,
            'observation_deadline_at_ms': None, 'reason_code': 'provider_identity_missing'}
        self.assertEqual(denied, checked_account_open_reply(denied, self.request))
