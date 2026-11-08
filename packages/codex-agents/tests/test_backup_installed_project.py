"""Standalone owner-backup tool compatibility; only disposable signed fixtures."""
import runpy
import hashlib
from pathlib import Path
import unittest

from tests.support import REPOSITORY_ROOT
from codex_agents import learning_store
from codex_agents.learning_contracts import content_digest, validate_learning_record
import tests.test_account_claim_operation as fixture_module


TOOL = runpy.run_path(str(REPOSITORY_ROOT / "scripts/backup_installed_project.py"))


class InstalledBackupManifestTests(unittest.TestCase):
    def test_exact_version_manifests_match_owner_reader_and_keep_old_tables_absent(self):
        for version in (5, 6, 7, 8, 9):
            with self.subTest(version=version):
                tables = TOOL["_tables"](version)
                self.assertEqual(learning_store._backup_tables(version), tables)
                self.assertEqual(version >= 7, "launch_claims" in tables)
                self.assertEqual(version >= 8, "launch_receipts" in tables)
                self.assertEqual(version >= 9, "active_run_capacity" in tables)
                for table in tables:
                    self.assertEqual(learning_store._backup_expected_columns(version, table),
                                     TOOL["_expected_columns"](version, table))
                    self.assertEqual(learning_store._BACKUP_ORDER_COLUMNS[table], TOOL["_TABLE_ORDER"][table])
        for version in (0, 4, 10):
            with self.subTest(unsupported=version), self.assertRaises(RuntimeError):
                TOOL["_tables"](version)

    def fixture(self):
        case = fixture_module.AccountClaimOperationTests("test_handler_claims_with_owner_deadline_and_returns_known_state")
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        self.addCleanup(case.tearDown)
        return case

    def restore(self, case, version):
        body = learning_store._snapshot_project_partition(case.ledger._connection, case.project,
                                                         ledger_schema_version=version)
        TOOL["_verify_relational_restore"](case.ledger._connection, body,
                                            validate_learning_record=validate_learning_record,
                                            content_digest=content_digest)
        return body

    def test_schema_eight_claim_and_receipt_restore_with_parent_uniqueness(self):
        case = self.fixture()
        self.assertEqual("claimed", case.claim()["state"])
        body = self.restore(case, 8)
        self.assertEqual(1, body["counts"]["launch_claims"])
        self.assertEqual(1, body["counts"]["launch_receipts"])

    def test_schema_nine_restores_capacity_and_rejects_erased_reservation(self):
        case = self.fixture()
        self.assertEqual("claimed", case.claim()["state"])
        body = self.restore(case, 9)
        self.assertEqual(1, body["counts"]["active_run_capacity"])
        body["tables"]["active_run_capacity"] = []
        body["counts"]["active_run_capacity"] = 0
        with self.assertRaisesRegex(RuntimeError, "capacity coverage is incomplete"):
            TOOL["_verify_relational_restore"](case.ledger._connection, body,
                                                validate_learning_record=validate_learning_record,
                                                content_digest=content_digest)

    def test_schema_seven_claim_restore_uses_exact_older_manifest(self):
        case = self.fixture()
        case.legacy_seven()
        body = self.restore(case, 7)
        self.assertEqual(1, body["counts"]["launch_claims"])
        self.assertNotIn("launch_receipts", body["columns"])
        self.assertNotIn("launch_receipts", body["counts"])

    def test_unicode_project_backup_envelope_is_accepted_by_owner_verifier(self):
        case = self.fixture()
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
