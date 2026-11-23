"""Standalone owner-backup tool compatibility; only disposable signed fixtures."""
import runpy
import hashlib
from pathlib import Path
import unittest

from tests.support import REPOSITORY_ROOT
from codex_agents import learning_store
import tests.test_account_claim_operation as fixture_module
from tests.test_account_launch_claims import drop_original_open_for_legacy_fixture


TOOL = runpy.run_path(str(REPOSITORY_ROOT / "scripts/backup_installed_project.py"))


class InstalledBackupManifestTests(unittest.TestCase):
    def test_exact_version_manifests_match_owner_reader_and_keep_old_tables_absent(self):
        for version in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16):
            with self.subTest(version=version):
                tables = TOOL["_tables"](version)
                self.assertEqual(learning_store._backup_tables(version), tables)
                self.assertEqual(version >= 7, "launch_claims" in tables)
                self.assertEqual(version >= 8, "launch_receipts" in tables)
                self.assertEqual(version >= 9, "active_run_capacity" in tables)
                self.assertEqual(version >= 10, 'account_open_requests' in tables)
                self.assertEqual(version >= 10, 'account_open_receipts' in tables)
                self.assertEqual(version >= 11, 'account_cleanup_receipts' in tables)
                self.assertEqual(version >= 12, 'account_observation_origins' in tables)
                self.assertEqual(version >= 12, 'account_observation_requests' in tables)
                self.assertEqual(version >= 13, 'account_billing_probe_origins' in tables)
                self.assertEqual(version >= 13, 'account_billing_probe_requests' in tables)
                self.assertEqual(version >= 15, 'run_holds' in tables)
                self.assertEqual(version >= 16, 'probe_id' in TOOL["_expected_columns"](version, 'records'))
                for table in tables:
                    self.assertEqual(learning_store._backup_expected_columns(version, table),
                                     TOOL["_expected_columns"](version, table))
                    self.assertEqual(learning_store._BACKUP_ORDER_COLUMNS[table], TOOL["_TABLE_ORDER"][table])
        for version in (0, 4, 17):  # PC-4 (requirement): 16 is now supported
            with self.subTest(unsupported=version), self.assertRaises(RuntimeError):
                TOOL["_tables"](version)

    def fixture(self):
        case = fixture_module.AccountClaimOperationTests("test_handler_claims_with_owner_deadline_and_returns_known_state")
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        self.addCleanup(case.tearDown)
        return case

    def test_unicode_project_backup_envelope_is_accepted_by_owner_verifier(self):
        case = self.fixture()
        drop_original_open_for_legacy_fixture(case.ledger)
        root = Path(case.fixture.fixture.temporary.name) / "caf\u00e9-\u65e5\u672c"
        root.mkdir()
        project = case.ledger.register_project(root, now_ms=case.stamp)
        body = learning_store._snapshot_project_partition(case.ledger._connection, project,
                                                         ledger_schema_version=8)
        encode = TOOL["_canonical_json"]
        payload = encode({"backup_sha256": hashlib.sha256(encode(body)).hexdigest(), "body": body})
        restored, _, _ = learning_store._verify_backup_envelope(payload, expected_project_id=project,
                                                               expected_ledger_schema_version=8)
        self.assertEqual(body, restored)


if __name__ == "__main__":
    unittest.main()
