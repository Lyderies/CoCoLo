"""Synthetic staging bytes only; primary applies schema14/wrapper before running."""
import hashlib
import json
import threading
import unittest
from unittest import mock

from tests import test_project_learning_corpus as fixtures
from codex_agents import project_learning_chunks as chunks
from codex_agents import project_learning_corpus as corpus
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_engine import build_feedback
from codex_agents.project_history import ProjectHistory


class LearningChunkMembershipTests(unittest.TestCase):
    entry = staticmethod(fixtures.ProjectLearningCorpusTests.entry)
    manifest = fixtures.ProjectLearningCorpusTests.manifest
    begin = fixtures.ProjectLearningCorpusTests.begin
    chunk = fixtures.ProjectLearningCorpusTests.chunk

    def setUp(self):
        fixtures.ProjectLearningCorpusTests.setUp(self)
        second = build_feedback(
            digest_key=b"K" * 32, run_id=self.outcome["run_id"], label="rejected",
            reason_code="incorrect", correction="second synthetic correction", observed_at_ms=11)
        self.records.append(self.entry(
            second, 11, source="owner", observed=11, ingested=40, expires=None, late=True))
        self.attestations.append({
            "subject_record_id": second["record_id"], "signature": b"second-inert-signature",
            "observed_at_ms": 40})
        self.attestations.sort(key=lambda entry: entry["subject_record_id"], reverse=True)
        self.assertNotEqual(self.attestations, sorted(
            self.attestations, key=lambda entry: entry["subject_record_id"]))

    def one(self, sql, args=()):
        cursor = self.store._connection.execute(sql, args)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def write(self, sql, args=()):
        self.store._connection.execute(sql, args).close()

    def snapshot(self):
        result = {}
        for table in ("history_identity", "project_learning_imports", "project_learning_records",
                      "project_learning_attestations", "project_learning_import_chunks",
                      "project_learning_chunk_memberships"):
            cursor = self.store._connection.execute("SELECT * FROM " + table + " ORDER BY 1,2,3")
            try:
                result[table] = tuple(tuple(row) for row in cursor)
            finally:
                cursor.close()
        return result

    def restore(self, baseline):
        # Synthetic corruption is committed so public read tests reach validation.
        for table, rows in baseline.items():
            if table == "history_identity":
                continue
            columns = (corpus.LEARNING_COLUMNS if table in corpus.LEARNING_COLUMNS
                       else chunks.CHUNK_COLUMNS)[table]
            for row in rows:
                self.write("INSERT OR REPLACE INTO " + table + " VALUES("
                           + ",".join("?" for _ in columns) + ")", row)

    def read(self, index=0):
        return self.store.learning_import_chunk("import-1", index)

    def reopen(self, **kwargs):
        self.store.close()
        self.store = ProjectHistory(self.directory, fixtures.PROJECT, **kwargs)
        self.addCleanup(self.store.close)

    def assert_fixed(self, operation, reason="history_incompatible"):
        with self.assertRaises(HistoryError) as caught:
            operation()
        self.assertEqual(reason, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def membership(self):
        return json.loads(self.one("SELECT membership FROM project_learning_chunk_memberships")[0])

    def put_membership(self, membership, *, project=None, raw=None):
        digest = corpus.chunk_digest(self.records, self.attestations)
        material = {"project_id": fixtures.PROJECT if project is None else project,
                    "import_id": "import-1", "chunk_index": 0, "chunk_digest": digest,
                    "membership": membership}
        bound = hashlib.sha256(b"learning-chunk-membership-v1\0" + canonical_json(material)).hexdigest()
        self.write("UPDATE project_learning_chunk_memberships SET membership=?,membership_digest=?",
                   (canonical_json(membership) if raw is None else raw, bound))

    def test_mixed_reversed_signatures_exact_after_finish_and_reopen(self):
        self.begin()
        receipt = self.chunk()
        membership = self.membership()
        self.assertEqual([entry["subject_record_id"] for entry in self.attestations],
                         membership["attestation_subject_ids"])
        self.assertEqual([json.loads(entry["content"])["record_id"] for entry in self.records],
                         membership["record_ids"])
        self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)
        before = self.snapshot()
        self.reopen(allow_schema_upgrade=True)
        changes = self.store._connection.total_changes
        restored = self.read()
        self.assertEqual({"records", "attestations", "receipt"}, set(restored))
        self.assertEqual(tuple(self.records), restored["records"])
        self.assertEqual(tuple(self.attestations), restored["attestations"])
        self.assertEqual(receipt, restored["receipt"])
        self.assertEqual(receipt["chunk_digest"], corpus.chunk_digest(
            restored["records"], restored["attestations"]))
        self.assertIs(False, restored["receipt"]["learning_ready"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(changes, self.store._connection.total_changes)
        self.assertEqual(1, self.one("SELECT next_source_sequence FROM history_identity")[0])
        self.assert_fixed(lambda: self.store.record(self.feedback["record_id"]), "history_not_found")

    def test_multiple_chunks_and_signature_only_reopen_in_original_order(self):
        source = [(self.records[:2], []), (self.records[2:], self.attestations[:1]),
                  ([], self.attestations[1:])]
        self.begin(self.manifest(source))
        receipts = []
        for index, (records, signatures) in enumerate(source):
            receipts.append(self.chunk(index=index, records=records, attestations=signatures))
        self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)
        self.reopen(allow_schema_upgrade=True)
        for index, (records, signatures) in enumerate(source):
            restored = self.read(index)
            self.assertEqual((tuple(records), tuple(signatures), receipts[index]),
                             (restored["records"], restored["attestations"], restored["receipt"]))
        self.assertEqual((), self.read(2)["records"])

    def test_known_replay_is_read_only_and_changed_signature_order_conflicts(self):
        self.begin()
        receipt = self.chunk()
        before = self.snapshot()
        changes = self.store._connection.total_changes
        replay = self.store.stage_learning_import_chunk(
            "import-1", 0, self.records, self.attestations, now_ms=999, before_commit=lambda: None)
        self.assertEqual(receipt, replay)
        self.assertEqual(changes, self.store._connection.total_changes)
        self.assertEqual(before, self.snapshot())
        self.assert_fixed(lambda: self.chunk(attestations=list(reversed(self.attestations))),
                          "history_conflict")

    def test_missing_legacy_membership_refuses_until_exact_original_replay(self):
        self.begin()
        receipt = self.chunk()
        self.store.finish_learning_import("import-1", now_ms=120, before_commit=lambda: None)
        self.write("DELETE FROM project_learning_chunk_memberships")
        before = self.snapshot()
        self.reopen(allow_schema_upgrade=True)
        self.assert_fixed(self.read, "history_learning_incomplete")
        self.assert_fixed(lambda: self.chunk(attestations=list(reversed(self.attestations))),
                          "history_conflict")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(receipt, self.chunk())
        after = self.snapshot()
        for table in before:
            if table != "project_learning_chunk_memberships":
                self.assertEqual(before[table], after[table])
        self.assertEqual(1, len(after["project_learning_chunk_memberships"]))
        self.assertEqual(tuple(self.attestations), self.read()["attestations"])

    def test_authorization_revocation_rolls_back_new_and_backfilled_membership(self):
        self.begin()
        baseline = self.snapshot()
        calls = []

        def revoke():
            calls.append(1)
            if len(calls) == 2:
                self.assertEqual(1, self.one(
                    "SELECT COUNT(*) FROM project_learning_chunk_memberships")[0])
                raise HistoryError("history_not_authorized")

        self.assert_fixed(lambda: self.chunk(before_commit=revoke), "history_not_authorized")
        self.assertEqual(2, len(calls))
        self.assertEqual(baseline, self.snapshot())
        self.chunk()
        self.write("DELETE FROM project_learning_chunk_memberships")
        baseline = self.snapshot()
        calls.clear()
        self.assert_fixed(lambda: self.chunk(before_commit=revoke), "history_not_authorized")
        self.assertEqual(2, len(calls))
        self.assertEqual(baseline, self.snapshot())
        self.assert_fixed(self.read, "history_learning_incomplete")

    def test_source_mutation_during_authorization_uses_captured_membership(self):
        self.begin()
        records = [dict(entry) for entry in self.records]
        signatures = [dict(entry) for entry in self.attestations]

        def mutate():
            self.records[0]["content"] = b"CANARY"
            self.records[0]["source_sequence"] = 999
            self.attestations[0]["signature"] = b"CANARY"
            self.attestations.reverse()

        receipt = self.chunk(before_commit=mutate)
        restored = self.read()
        self.assertEqual(tuple(records), restored["records"])
        self.assertEqual(tuple(signatures), restored["attestations"])
        self.assertEqual(corpus.chunk_digest(records, signatures), receipt["chunk_digest"])

    def test_membership_corruption_is_closed_canonical_scoped_and_immutable(self):
        self.begin()
        self.chunk()
        original = self.membership()
        bad = [
            {**original, "private": "CANARY"},
            {**original, "record_ids": original["record_ids"] * 2},
            {**original, "record_ids": ["CANARY/invalid"]},
            {**original, "record_ids": "CANARY"},
            {"record_ids": [], "attestation_subject_ids": []},
            {**original, "attestation_subject_ids": list(reversed(original["attestation_subject_ids"]))},
            {**original, "record_ids": list(reversed(original["record_ids"]))},
            {**original, "record_ids": ["missing-record"]},
            {**original, "record_ids": ["record-" + str(i) for i in range(65)]},
        ]
        baseline = self.snapshot()
        for membership in bad:
            with self.subTest(membership=membership):
                try:
                    self.put_membership(membership)
                    before = self.snapshot()
                    self.assert_fixed(self.read)
                    with self.assertRaises(HistoryError) as caught:
                        self.chunk()
                    self.assertIn(str(caught.exception), ("history_conflict", "history_incompatible"))
                    self.assertEqual(before, self.snapshot())
                finally:
                    self.restore(baseline)

    def test_membership_blob_digest_scope_and_receipt_corruption_do_not_leak(self):
        self.begin()
        receipt = self.chunk()
        original = self.membership()
        mutations = [
            ("UPDATE project_learning_chunk_memberships SET membership=?", (b"CANARY",)),
            ("UPDATE project_learning_chunk_memberships SET membership=?", ("CANARY",)),
            ("UPDATE project_learning_chunk_memberships SET membership=zeroblob(16385)", ()),
            ("UPDATE project_learning_chunk_memberships SET membership=1000000000", ()),
            ("UPDATE project_learning_chunk_memberships SET membership_digest=?", ("0" * 64,)),
            ("UPDATE project_learning_chunk_memberships SET chunk_digest=?", ("0" * 64,)),
            ("UPDATE project_learning_import_chunks SET receipt=?",
             (canonical_json({**receipt, "private": "CANARY"}),)),
            ("UPDATE project_learning_import_chunks SET receipt=?",
             (canonical_json({**receipt, "record_count": True}),)),
            ("UPDATE project_learning_import_chunks SET receipt=?",
             (canonical_json({**receipt, "chain_digest": "f" * 64}),)),
            ("UPDATE project_learning_import_chunks SET receipt=zeroblob(16385)", ()),
            ("UPDATE project_learning_imports SET manifest=zeroblob(65537)", ()),
        ]
        baseline = self.snapshot()
        for sql, args in mutations:
            with self.subTest(sql=sql, args=args):
                try:
                    self.write(sql, args)
                    self.assert_fixed(self.read)
                    self.assert_fixed(self.chunk)
                finally:
                    self.restore(baseline)
        self.put_membership(original, project="prj_" + "b" * 32)
        self.assert_fixed(self.read)
        self.put_membership(original, raw=json.dumps(original, indent=2).encode())
        self.assert_fixed(self.read)
        self.put_membership(original, raw=b'{"record_ids":[],"record_ids":[],"attestation_subject_ids":[]}')
        self.assert_fixed(self.read)

    def test_record_signature_metadata_and_body_corruption_refuse_read_replay_backfill(self):
        self.begin()
        self.chunk()
        mutations = [
            ("UPDATE project_learning_records SET content=?", (b"CANARY",)),
            ("UPDATE project_learning_records SET content_digest=?", ("f" * 64,)),
            ("UPDATE project_learning_records SET content=zeroblob(65537)", ()),
            ("UPDATE project_learning_records SET content=1000000000", ()),
            ("UPDATE project_learning_records SET ingested_at_ms=999", ()),
            ("UPDATE project_learning_records SET source_sequence=source_sequence+100", ()),
            ("UPDATE project_learning_records SET observed_at_ms=999", ()),
            ("UPDATE project_learning_records SET expires_at_ms=999", ()),
            ("UPDATE project_learning_records SET source='hook'", ()),
            ("UPDATE project_learning_records SET expires_at_ms='CANARY'", ()),
            ("UPDATE project_learning_records SET late=1-late", ()),
            ("UPDATE project_learning_attestations SET signature=?", (b"CANARY",)),
            ("UPDATE project_learning_attestations SET signature_digest=?", ("f" * 64,)),
            ("UPDATE project_learning_attestations SET signature=1000000000", ()),
            ("UPDATE project_learning_attestations SET signature=zeroblob(16385)", ()),
            ("UPDATE project_learning_attestations SET observed_at_ms=999", ()),
        ]
        baseline = self.snapshot()
        for sql, args in mutations:
            with self.subTest(sql=sql):
                # Restore exact rows after each committed synthetic corruption.
                self.write(sql, args)
                self.assert_fixed(self.read)
                self.assert_fixed(self.chunk)
                self.write("DELETE FROM project_learning_chunk_memberships")
                self.assert_fixed(self.chunk)
                self.assertEqual(0, self.one(
                    "SELECT COUNT(*) FROM project_learning_chunk_memberships")[0])
                self.restore(baseline)

    def test_missing_chunk_member_and_membership_are_distinct(self):
        self.begin()
        self.assert_fixed(self.read, "history_not_found")
        self.chunk()
        self.assert_fixed(lambda: self.read(1), "history_not_found")
        self.write("DELETE FROM project_learning_attestations")
        self.assert_fixed(self.read)
        self.assert_fixed(self.chunk)
        self.write("DELETE FROM project_learning_records WHERE source_sequence=5")
        self.assert_fixed(self.read)
        self.assert_fixed(self.chunk)
        self.write("DELETE FROM project_learning_chunk_memberships")
        self.assert_fixed(self.read, "history_learning_incomplete")

    def test_owner_process_thread_upgrade_and_transaction_guards(self):
        self.begin()
        self.chunk()
        self.assert_fixed(lambda: self.read(True), "history_invalid_input")
        self.assert_fixed(lambda: self.read(2 * corpus.MAX_IMPORT_RECORDS), "history_invalid_input")
        self.write("BEGIN")
        try:
            self.assert_fixed(self.read, "history_conflict")
        finally:
            self.write("ROLLBACK")
        prepared, signatures, _, digest = corpus._prepare_chunk(self.records, self.attestations)
        self.assert_fixed(lambda: chunks.record_chunk_membership(
            self.store, "import-1", 0, prepared, signatures, digest), "history_conflict")
        with mock.patch.object(self.store, "_pid", -1):
            self.assert_fixed(self.read, "history_wrong_owner")
        failures = []

        def other():
            try:
                self.read()
            except HistoryError as error:
                failures.append(error.reason_code)
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["history_wrong_owner"], failures)
        self.reopen()
        self.assert_fixed(self.read, "history_not_authorized")

    def test_exact_64_entry_boundary_is_supported_and_65_refuses(self):
        self.records = [self.entry(fixtures.event_record(
            "membership-" + str(i), observed_at_ms=1), i + 1) for i in range(64)]
        self.attestations = []
        self.begin()
        self.chunk()
        self.assertEqual(tuple(self.records), self.read()["records"])
        self.assert_fixed(lambda: corpus.chunk_digest(
            self.records + [dict(self.records[-1], source_sequence=65)], []),
            "history_invalid_input")

    def test_raw_byte_envelope_and_pk_queries_close_every_cursor(self):
        self.begin()
        self.chunk()
        with mock.patch.object(corpus, "MAX_CHUNK_BYTES", 1):
            self.assert_fixed(self.read)
        connection = self.store._connection
        cursors, queries = [], []

        class Cursor:
            def __init__(self, inner):
                self.inner, self.closed = inner, False
                cursors.append(self)

            def fetchone(self):
                return self.inner.fetchone()

            def close(self):
                self.closed = True
                self.inner.close()

        class Connection:
            @property
            def in_transaction(self):
                return connection.in_transaction

            def execute(self, sql, args=()):
                queries.append((sql, args))
                return Cursor(connection.execute(sql, args))

        try:
            self.store._connection = Connection()
            self.read()
            self.assertTrue(all(cursor.closed for cursor in cursors))
            self.assertLessEqual(len(queries), 135)  # <=64 entries, <=2 PK reads per entry
        finally:
            self.store._connection = connection
        for sql, args in queries:
            if not sql.startswith("SELECT"):
                continue
            self.assertNotIn("OFFSET", sql.upper())
            self.assertIn("project_id=? AND import_id=?", sql)
            cursor = connection.execute("EXPLAIN QUERY PLAN " + sql, args)
            try:
                plan = " ".join(str(row[3]).upper() for row in cursor)
            finally:
                cursor.close()
            self.assertIn("SEARCH", plan)
            self.assertNotIn("SCAN", plan)
