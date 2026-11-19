from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.learning_support import event_record, verification_outcome
from tests import test_learning_store as legacy

from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_engine import build_feedback, mine_evaluation_case, OwnerGrantAuthority
from codex_agents.project_history import ProjectHistory
from codex_agents import project_history
from codex_agents import project_learning_corpus as corpus

PROJECT = "prj_" + "a" * 32


class ProjectLearningCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base / "history"
        self.directory.mkdir(mode=0o700)
        self.store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        self.addCleanup(self.store.close)
        self.outcome = verification_outcome("source")
        self.case = mine_evaluation_case(
            digest_key=b"K" * 32, outcome=self.outcome, fixture_digest="a" * 64,
            criteria_digest="b" * 64, lineage_digest="c" * 64,
        )
        self.feedback = build_feedback(
            digest_key=b"K" * 32, run_id=self.outcome["run_id"], label="rejected",
            reason_code="incorrect", correction="private correction", observed_at_ms=10,
        )
        self.records = [
            self.entry(self.outcome, 5, source="host", observed=1, ingested=10, expires=100),
            self.entry(self.case, 7, observed=20, ingested=20),
            self.entry(self.feedback, 9, source="owner", observed=10, ingested=30, expires=130, late=True),
        ]
        self.attestations = [{
            "subject_record_id": self.feedback["record_id"],
            "signature": b"synthetic-signature-not-live-authority", "observed_at_ms": 30,
        }]

    @staticmethod
    def entry(record, sequence, *, source="evaluator", observed=1, ingested=1, expires=None, late=False):
        return {"content": canonical_json(record), "source": source,
                "source_sequence": sequence, "observed_at_ms": observed,
                "ingested_at_ms": ingested, "expires_at_ms": expires, "late": late}

    def manifest(self, chunks=None):
        chunks = [(self.records, self.attestations)] if chunks is None else chunks
        chain = corpus.EMPTY_CHAIN_DIGEST
        for records, attestations in chunks:
            chain = corpus.next_chain_digest(chain, corpus.chunk_digest(records, attestations))
        return {"schema_version": 1, "scope": "learning-record-staging-v1",
                "project_id": PROJECT, "source_identity_digest": "d" * 64,
                "key_domain_digest": "e" * 64,
                "record_count": sum(len(rows) for rows, _ in chunks),
                "attestation_count": sum(len(rows) for _, rows in chunks),
                "total_bytes": sum(sum(len(row["content"]) for row in records)
                                   + sum(len(row["signature"]) for row in signatures)
                                   for records, signatures in chunks),
                "chain_digest": chain}

    def begin(self, manifest=None):
        return self.store.begin_learning_import(
            "import-1", self.manifest() if manifest is None else manifest,
            now_ms=100, before_commit=lambda: None,
        )

    def chunk(self, index=0, records=None, attestations=None, before_commit=lambda: None):
        return self.store.stage_learning_import_chunk(
            "import-1", index, self.records if records is None else records,
            self.attestations if attestations is None else attestations,
            now_ms=110, before_commit=before_commit,
        )

    def test_original_record_bytes_metadata_and_signature_survive_stage_and_reopen(self):
        self.begin()
        self.chunk()
        staged = self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)
        self.assertEqual("staged", staged["state"])
        self.assertIs(False, staged["learning_ready"])
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        self.addCleanup(self.store.close)
        self.assertEqual(self.records, list(self.store.learning_import_records("import-1")))
        self.assertEqual(self.attestations, list(self.store.learning_import_attestations("import-1")))
        self.assertEqual(staged, self.store.finish_learning_import(
            "import-1", now_ms=999, before_commit=lambda: None,
        ))
        self.assertEqual(1, self.store._connection.execute(
            "SELECT next_source_sequence FROM history_identity").fetchone()[0])
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record(self.case["record_id"])

    def test_exact_chunk_replay_is_identical_and_changed_replay_conflicts(self):
        self.begin()
        first = self.chunk()
        self.assertEqual(first, self.chunk())
        changed = [dict(self.records[0], ingested_at_ms=11), *self.records[1:]]
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.chunk(records=changed)
        self.assertEqual(self.records, list(self.store.learning_import_records("import-1")))

    def test_resumed_chunks_preserve_order_and_final_manifest_chain(self):
        chunks = [(self.records[:1], []), (self.records[1:], self.attestations)]
        self.begin(self.manifest(chunks))
        self.chunk(records=chunks[0][0], attestations=[])
        with self.assertRaisesRegex(HistoryError, "history_learning_incomplete"):
            self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        self.addCleanup(self.store.close)
        self.chunk(index=1, records=chunks[1][0], attestations=chunks[1][1])
        self.assertEqual("staged", self.store.finish_learning_import(
            "import-1", now_ms=120, before_commit=lambda: None,
        )["state"])

    def test_revocation_after_row_writes_rolls_back_rows_counters_and_receipt(self):
        self.begin()
        calls = []

        def revoke():
            calls.append(1)
            if len(calls) == 2:
                self.assertEqual(3, self.store._connection.execute(
                    "SELECT COUNT(*) FROM project_learning_records").fetchone()[0])
                raise HistoryError("history_not_authorized")

        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.chunk(before_commit=revoke)
        self.assertEqual(2, len(calls))
        self.assertEqual([], list(self.store.learning_import_records("import-1")))
        self.assertEqual(0, self.store._connection.execute(
            "SELECT COUNT(*) FROM project_learning_import_chunks").fetchone()[0])
        self.chunk()

    def test_wrong_project_manifest_or_changed_identity_cannot_resume(self):
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.begin({**self.manifest(), "project_id": "prj_" + "b" * 32})
        self.begin()
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.begin({**self.manifest(), "key_domain_digest": "f" * 64})

    def test_normal_owner_and_generic_append_cannot_import_controlled_records(self):
        for record in (self.case, self.feedback):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.store.append_record(canonical_json(record), source="owner", ingested_at_ms=10)
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT)
        self.addCleanup(self.store.close)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.begin()

    def test_missing_reordered_duplicate_and_unbound_signature_chunks_are_refused(self):
        self.begin()
        for index, rows, signatures in (
            (1, self.records, self.attestations),
            (0, list(reversed(self.records)), self.attestations),
            (0, [self.records[0], self.records[0]], []),
            (0, self.records[:1], self.attestations),
        ):
            with self.subTest(index=index, records=len(rows)), self.assertRaises(HistoryError):
                self.chunk(index=index, records=rows, attestations=signatures)
        self.assertEqual([], list(self.store.learning_import_records("import-1")))

    def test_bounds_and_noncanonical_records_fail_without_writes(self):
        self.begin()
        for rows in ([self.records[0]] * (corpus.MAX_CHUNK_ENTRIES + 1),
                     [dict(self.records[0], content=json.dumps(self.outcome, indent=2).encode())],
                     [dict(self.records[0], content=b"[]")],
                     [dict(self.records[0], content=b"x" * 65_537)]):
            with self.subTest(count=len(rows)), self.assertRaises(HistoryError):
                self.chunk(records=rows, attestations=[])
        with mock.patch.object(corpus, "MAX_CHUNK_BYTES", 1), self.assertRaises(HistoryError):
            self.chunk()
        self.assertEqual([], list(self.store.learning_import_records("import-1")))

    def test_wrong_final_chain_never_becomes_a_complete_or_ready_corpus(self):
        self.begin({**self.manifest(), "chain_digest": "f" * 64})
        self.chunk()
        with self.assertRaisesRegex(HistoryError, "history_learning_incomplete"):
            self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)

    def test_reads_are_bounded_and_resume_by_original_source_sequence(self):
        self.begin()
        self.chunk()
        first = self.store.learning_import_records("import-1", limit=1)
        self.assertEqual(self.records[:1], list(first))
        rest = self.store.learning_import_records("import-1", after_source_sequence=first[-1]["source_sequence"])
        self.assertEqual(self.records[1:], list(rest))
        with self.assertRaises(HistoryError):
            self.store.learning_import_records("import-1", limit=True)

    def test_changed_stored_receipt_cannot_return_extra_or_unbounded_metadata(self):
        self.begin()
        original = self.chunk()
        self.store._connection.execute(
            "UPDATE project_learning_import_chunks SET receipt=?",
            (canonical_json({**original, "private_content": "CANARY"}),),
        )
        with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
            self.chunk()
        self.assertNotIn("CANARY", str(caught.exception))
        self.store._connection.execute("UPDATE project_learning_import_chunks SET receipt=?", (b"CANARY",))
        with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
            self.chunk()
        self.assertIsNone(caught.exception.__cause__)

    def test_empty_staging_remains_unavailable_and_scope_capacity_is_finite(self):
        empty = self.manifest([])
        for index in range(corpus.MAX_IMPORTS):
            identity = "empty-import-" + str(index)
            self.store.begin_learning_import(identity, empty, now_ms=100, before_commit=lambda: None)
            finished = self.store.finish_learning_import(identity, now_ms=110, before_commit=lambda: None)
            self.assertIs(False, finished["learning_ready"])
        with self.assertRaisesRegex(HistoryError, "history_learning_capacity"):
            self.store.begin_learning_import("over-capacity", empty, now_ms=120, before_commit=lambda: None)

    def test_input_mutation_during_authorization_cannot_change_captured_record_bytes(self):
        self.begin()
        original = [dict(entry) for entry in self.records]

        def mutate_original():
            self.records[0]["content"] = b"CANARY"
            self.records[0]["ingested_at_ms"] = 999

        self.chunk(before_commit=mutate_original)
        self.assertEqual(original, list(self.store.learning_import_records("import-1")))

    def test_foreign_thread_cannot_stage_or_read(self):
        self.begin()
        errors = []

        def other():
            for operation in (self.chunk, lambda: self.store.learning_import_records("import-1")):
                try:
                    operation()
                except HistoryError as error:
                    errors.append(error.reason_code)

        worker = threading.Thread(target=other)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["history_wrong_owner"] * 2, errors)


class LearningCorpusUpgradeTests(unittest.TestCase):
    def test_v10_upgrade_is_explicit_preserves_existing_bytes_and_rolls_back_interruption(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary) / "history"
            directory.mkdir(mode=0o700)
            db = directory / "history.sqlite3"
            connection = sqlite3.connect(db)
            connection.executescript(project_history._SCHEMA_V10)
            connection.execute("INSERT INTO history_identity VALUES(?,18,1)", (PROJECT,))
            event = event_record("original-v10-event", observed_at_ms=1)
            original = (PROJECT, event["record_id"], "event", "hook", 17, 1, 2,
                        canonical_json(event), hashlib.sha256(canonical_json(event)).hexdigest(), 0)
            connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)", original)
            connection.execute(f"PRAGMA application_id={project_history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=10")
            connection.commit()
            connection.close()
            db.chmod(0o600)
            with self.assertRaisesRegex(HistoryError, "history_incompatible"):
                ProjectHistory(directory, PROJECT)
            with mock.patch.object(project_history, "LEARNING_SCHEMA", "CREATE TABLE staging_cut(x); INVALID SQL;"), \
                    self.assertRaises(HistoryError):
                ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            connection = sqlite3.connect(db)
            self.assertEqual(10, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='staging_cut'").fetchone())
            connection.close()
            store = ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            try:
                self.assertEqual(17, store._connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual((PROJECT, 18, 1), tuple(store._connection.execute("SELECT * FROM history_identity").fetchone()))
                self.assertEqual(original, tuple(store._connection.execute("SELECT * FROM records").fetchone()))
                self.assertEqual(canonical_json(event), store.record(event["record_id"]).content)
                self.assertEqual(0, store._connection.execute("SELECT COUNT(*) FROM project_learning_records").fetchone()[0])
            finally:
                store.close()


class SignedLegacyLearningStagingTests(unittest.TestCase):
    def test_current_and_legacy_signed_formal_qa_keep_exact_bytes_without_activation(self):
        from tests import test_learning_engine as engine_fixtures
        from codex_agents.learning_engine import build_verified_qa_records
        from codex_agents.learning_contracts import content_digest

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            private_key, public_key = engine_fixtures.generate_test_owner_key(root)
            allowed = root / 'allowed-signers'
            key_type, key_data = public_key.split()[:2]
            allowed.write_text(f'codex-agents-owner {key_type} {key_data}\n')
            allowed.chmod(0o600)
            authority = OwnerGrantAuthority(allowed)
            positive, _ = build_verified_qa_records(**engine_fixtures.verified_qa_inputs())
            inputs = engine_fixtures.verified_qa_inputs()
            inputs['check_report']['deterministic_checks'][0]['passed'] = False
            negative, _ = build_verified_qa_records(**inputs)
            # Preserve the supported historical body without adding modern axes.
            negative = {k: v for k, v in negative.items()
                        if k not in {'record_id', 'execution_state', 'semantic_verdict'}}
            negative['record_id'] = 'qa_evidence:' + content_digest(negative)
            records, signatures = [], []
            for index, qa in enumerate((positive, negative), 1):
                records.append(ProjectLearningCorpusTests.entry(
                    qa, index * 100, source='verifier', observed=qa['observed_at_ms'],
                    ingested=qa['observed_at_ms'] + 1))
                path = engine_fixtures.sign_owner_payload(
                    private_key, authority.qa_attestation_payload(PROJECT, qa), 'formal-' + str(index))
                signature = path.read_bytes()
                self.assertTrue(authority.verify_qa_attestation(PROJECT, qa, signature))
                signatures.append({'subject_record_id': qa['record_id'], 'signature': signature,
                                   'observed_at_ms': qa['observed_at_ms'] + 2})
            digest = corpus.chunk_digest(records, signatures)
            manifest = {'schema_version': 1, 'scope': 'learning-record-staging-v1', 'project_id': PROJECT,
                        'source_identity_digest': 'd' * 64, 'key_domain_digest': 'e' * 64,
                        'record_count': 2, 'attestation_count': 2,
                        'total_bytes': sum(len(r['content']) for r in records)
                                       + sum(len(r['signature']) for r in signatures),
                        'chain_digest': corpus.next_chain_digest(corpus.EMPTY_CHAIN_DIGEST, digest)}
            directory = root / 'formal-history'
            directory.mkdir(mode=0o700)
            store = ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            try:
                store.begin_learning_import('formal', manifest, now_ms=1000, before_commit=lambda: None)
                store.stage_learning_import_chunk('formal', 0, records, signatures,
                                                  now_ms=1001, before_commit=lambda: None)
                receipt = store.finish_learning_import('formal', now_ms=1002, before_commit=lambda: None)
                self.assertIs(False, receipt['learning_ready'])
            finally:
                store.close()
            store = ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            try:
                self.assertEqual(records, list(store.learning_import_records('formal')))
                self.assertEqual(signatures, list(store.learning_import_attestations('formal')))
                self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_operational_qa').fetchone()[0])
                for qa, row in zip((positive, negative), signatures):
                    self.assertTrue(authority.verify_qa_attestation(PROJECT, qa, row['signature']))
                    with self.assertRaisesRegex(HistoryError, '^history_not_found$'):
                        store.record(qa['record_id'])
            finally:
                store.close()

    def test_actual_signed_owner_feedback_and_all_selected_record_bytes_survive_closed_source_copy(self):
        fixture = legacy.LearningStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.test_operational_qa_supersedes_unverified_run_and_surfaces_no_owner_review()
        selected = fixture.ledger._connection.execute(
            "SELECT * FROM records WHERE project_id=? ORDER BY source_sequence", (fixture.project_a_id,),
        ).fetchall()
        records = [{"content": bytes(row["content"]), "source": row["source"],
                    "source_sequence": row["source_sequence"], "observed_at_ms": row["observed_at_ms"],
                    "ingested_at_ms": row["ingested_at_ms"], "expires_at_ms": row["expires_at_ms"],
                    "late": bool(row["late"])}
                   for row in selected if row["record_type"] in corpus._RECORD_TYPES]
        identifiers = {json.loads(row["content"])["record_id"] for row in records}
        signatures = [{"subject_record_id": row["subject_record_id"], "signature": bytes(row["signature"]),
                       "observed_at_ms": row["attested_at_ms"]}
                      for row in fixture.ledger._connection.execute(
                          "SELECT * FROM owner_attestations WHERE project_id=?", (fixture.project_a_id,),
                      ) if row["subject_record_id"] in identifiers]
        signatures.sort(key=lambda row: row['subject_record_id'], reverse=True)
        self.assertNotEqual(signatures, sorted(signatures, key=lambda row: row['subject_record_id']))
        self.assertEqual(2, len(signatures))
        authority = OwnerGrantAuthority(fixture.state.owner_allowed_signers())
        signed_records = {json.loads(row["content"])["record_id"]: json.loads(row["content"]) for row in records}
        self.assertEqual({"feedback", "billing_attestation"},
                         {signed_records[row["subject_record_id"]]["record_type"] for row in signatures})
        feedback = next(record for record in signed_records.values() if record["record_type"] == "feedback")
        feedback_signature = next(row["signature"] for row in signatures
                                  if row["subject_record_id"] == feedback["record_id"])
        self.assertTrue(authority.verify_owner_record(fixture.project_a_id, feedback, feedback_signature))
        fixture.ledger.close()
        directory = fixture.state_root.parent / "target-history"
        directory.mkdir(mode=0o700)
        target = ProjectHistory(directory, fixture.project_a_id, allow_schema_upgrade=True)
        self.addCleanup(target.close)
        digest = corpus.chunk_digest(records, signatures)
        manifest = {"schema_version": 1, "scope": "learning-record-staging-v1", "project_id": fixture.project_a_id,
                    "source_identity_digest": "d" * 64, "key_domain_digest": "e" * 64,
                    "record_count": len(records), "attestation_count": len(signatures),
                    "total_bytes": sum(len(row["content"]) for row in records) + sum(len(row["signature"]) for row in signatures),
                    "chain_digest": corpus.next_chain_digest(corpus.EMPTY_CHAIN_DIGEST, digest)}
        target.begin_learning_import("signed-source", manifest, now_ms=1000, before_commit=lambda: None)
        chunk_receipt = target.stage_learning_import_chunk("signed-source", 0, records, signatures,
                                                          now_ms=1001, before_commit=lambda: None)
        receipt = target.finish_learning_import("signed-source", now_ms=1002, before_commit=lambda: None)
        target.close()
        target = ProjectHistory(directory, fixture.project_a_id, allow_schema_upgrade=True)
        self.addCleanup(target.close)
        copied = list(target.learning_import_records("signed-source"))
        copied_signatures = list(target.learning_import_attestations("signed-source"))
        self.assertEqual(records, copied)
        self.assertEqual(sorted(signatures, key=lambda row: row['subject_record_id']), copied_signatures)
        original_chunk = target.learning_import_chunk('signed-source', 0)
        self.assertEqual(tuple(records), original_chunk['records'])
        self.assertEqual(tuple(signatures), original_chunk['attestations'])
        self.assertEqual(chunk_receipt, original_chunk['receipt'])
        self.assertEqual(chunk_receipt['chunk_digest'], corpus.chunk_digest(
            list(original_chunk['records']), list(original_chunk['attestations'])))
        copied_feedback_signature = next(row["signature"] for row in copied_signatures
                                        if row["subject_record_id"] == feedback["record_id"])
        self.assertTrue(authority.verify_owner_record(fixture.project_a_id, feedback, copied_feedback_signature))
        self.assertIs(False, receipt["learning_ready"])
