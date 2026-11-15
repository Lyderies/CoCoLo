"""Golden legacy persistence behavior for the future split-store importer.

These disposable backend fixtures establish compatibility requirements. They do
not enable an importer, authorize a normal API caller or validate a split layout.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import TEMP_ROOT

from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import (
    DEFAULT_RETENTION_MS, LearningLedger, LearningState, LedgerError, load_retention_policy,
)

from codex_agents.providers import headroom_record
from tests.learning_support import event_record

# A hook event is kept as long as its code's weight in the installed policy says.
HOOK_KEEP_MS = load_retention_policy().event_keep_ms["turn_started"]


class LegacyHistoryCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / "state")
        self.ledger = LearningLedger(self.state)
        self.addCleanup(lambda: self.ledger.close())
        self.projects = []
        for name in ("project-a", "project-b"):
            path = self.base / name
            path.mkdir()
            self.projects.append(self.ledger.register_project(path, now_ms=1))

    def row(self, project: str, identifier: str) -> dict | None:
        # Inspect only the fixture's own persistence metadata, which the legacy
        # public reader does not expose in full. This is no client SQL fallback.
        row = self.ledger._connection.execute(
            "SELECT * FROM records WHERE project_id=? AND record_id=?", (project, identifier),
        ).fetchone()
        return dict(row) if row is not None else None

    def reopen(self) -> None:
        self.ledger.close()
        self.ledger = LearningLedger(self.state)

    def test_cross_producer_duplicate_preserves_first_bytes_and_all_metadata(self) -> None:
        project = self.projects[0]
        record = event_record("first-source", observed_at_ms=100)
        first = self.ledger.append(project, record, producer="hook", now_ms=200)
        original = self.row(project, record["record_id"])
        self.assertEqual(canonical_json(record), original["content"])
        self.assertEqual("hook", original["source"])
        self.assertEqual(200, original["ingested_at_ms"])
        self.assertEqual(200 + HOOK_KEEP_MS, original["expires_at_ms"])
        self.reopen()
        for producer in ("app_server", "host", "hook"):
            with self.subTest(producer=producer):
                duplicate = self.ledger.append(project, record, producer=producer, now_ms=999)
                self.assertEqual(
                    {"stored": False, "duplicate": True, "source_sequence": first["source_sequence"]},
                    duplicate,
                )
                self.assertEqual(original, self.row(project, record["record_id"]))
        with self.assertRaisesRegex(LedgerError, "conflicts"):
            self.ledger.append(project, {**record, "observed_at_ms": 101}, producer="host", now_ms=1000)
        # Duplicate acceptance never bypasses the producer's record authority.
        with self.assertRaisesRegex(LedgerError, "not authorized"):
            self.ledger.append(project, record, producer="owner", now_ms=1000)
        self.assertEqual(original, self.row(project, record["record_id"]))

    def test_ordinary_expiry_frees_record_id_and_restarts_empty_project_sequence(self) -> None:
        project, other = self.projects
        record = event_record("reusable", observed_at_ms=100)
        self.ledger.append(project, record, producer="hook", now_ms=200)
        self.ledger.append(other, record, producer="host", now_ms=300)
        other_original = self.row(other, record["record_id"])
        self.assertEqual(1, self.ledger.expire(project, now_ms=200 + DEFAULT_RETENTION_MS))
        self.assertIsNone(self.row(project, record["record_id"]))
        self.reopen()
        changed = {**record, "observed_at_ms": 101}
        timestamp = 201 + DEFAULT_RETENTION_MS
        self.assertEqual(
            {"stored": True, "duplicate": False, "source_sequence": 1, "late": False},
            self.ledger.append(project, changed, producer="host", now_ms=timestamp),
        )
        stored = self.row(project, record["record_id"])
        self.assertEqual(canonical_json(changed), stored["content"])
        self.assertEqual("host", stored["source"])
        self.assertEqual(timestamp, stored["ingested_at_ms"])
        self.assertEqual(timestamp + DEFAULT_RETENTION_MS, stored["expires_at_ms"])
        self.assertEqual(other_original, self.row(other, record["record_id"]))

    @staticmethod
    def headroom(observed_at_ms: int) -> dict:
        return headroom_record(
            provider="codex", account_fingerprint="a" * 64, windows=[], status=None,
            run_id=None, phase=None, observed_at_ms=observed_at_ms,
        )

    def test_controlled_account_records_share_event_namespace_and_order(self) -> None:
        project, other = self.projects
        account_record = self.headroom(300)
        identifier = account_record["record_id"]
        event = event_record(identifier, observed_at_ms=100)
        self.ledger.append(project, event, producer="hook", now_ms=1000)
        with self.assertRaisesRegex(LedgerError, "conflicts"):
            self.ledger._store_provider_observations(project, [account_record], timestamp=1001)
        # Exercise the opposite insertion order in another project too. An ID
        # prefix does not partition the event and account-record namespaces.
        self.ledger._store_provider_observations(other, [account_record], timestamp=1000)
        with self.assertRaisesRegex(LedgerError, "conflicts"):
            self.ledger.append(other, event, producer="hook", now_ms=1001)
        separate = self.headroom(400)
        stored = self.ledger._store_provider_observations(project, [separate], timestamp=1002)
        self.assertEqual(2, stored[0]["source_sequence"])
        late = self.ledger.append(
            project, event_record("after-account", observed_at_ms=200), producer="hook", now_ms=1003,
        )
        self.assertEqual((3, True), (late["source_sequence"], late["late"]))
        self.assertEqual("provider", self.row(project, separate["record_id"])["source"])
        with self.ledger.repository(project) as repository:
            self.assertEqual([1, 2, 3], [record["_source_sequence"] for record in repository.records()])
        with self.ledger.repository(other) as repository:
            self.assertEqual([1], [record["_source_sequence"] for record in repository.records()])

    def test_lineage_expiry_recomputes_sequence_and_late_watermark_from_retained_rows(self) -> None:
        project = self.projects[0]
        for identifier, observed, timestamp in (("expired", 100, 1000), ("retained", 200, 2000),
                                                ("derived-tail", 900, 3000)):
            self.ledger.append(project, event_record(identifier, observed_at_ms=observed),
                               producer="hook", now_ms=timestamp)
        self.ledger.link(project, "expired", "derived-tail", "derived_from")
        self.assertEqual(2, self.ledger.expire(project, now_ms=1000 + HOOK_KEEP_MS))
        self.reopen()
        self.assertEqual(
            {"stored": True, "duplicate": False, "source_sequence": 3, "late": False},
            self.ledger.append(project, event_record("new-tail", observed_at_ms=300),
                               producer="app_server", now_ms=1001 + HOOK_KEEP_MS),
        )
        self.assertIsNone(self.row(project, "derived-tail"))
        self.assertEqual(2, self.row(project, "retained")["source_sequence"])

    def test_legacy_equal_schema_versions_keep_exact_bytes_and_reader_types(self) -> None:
        project = self.projects[0]
        records = []
        for identifier, version in (("integer", 1), ("boolean", True), ("float", 1.0)):
            record = {**event_record(identifier), "schema_version": version}
            records.append(record)
            self.ledger.append(project, record, producer="hook", now_ms=1000)
        self.reopen()
        with self.ledger.repository(project) as repository:
            read = repository.records()
        for original, actual in zip(records, read, strict=True):
            with self.subTest(identifier=original["record_id"]):
                self.assertIs(type(original["schema_version"]), type(actual["schema_version"]))
                self.assertEqual(canonical_json(original), self.row(project, original["record_id"])["content"])
                self.assertEqual(original, {key: value for key, value in actual.items() if not key.startswith("_")})
