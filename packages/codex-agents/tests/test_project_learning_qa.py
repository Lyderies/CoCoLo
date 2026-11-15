"""Disposable QA staging; original provenance and native/release gates remain separate."""
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests import test_learning_store as legacy
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.project_history import ProjectHistory
from codex_agents import project_learning_corpus as corpus
from codex_agents import project_learning_qa as qa

PROJECT = "prj_" + "a" * 32
def ALLOW():
  return None

def entry(body):
  raw = canonical_json(body)
  return {"qa_id": body["record_id"], "qa_digest": hashlib.sha256(raw).hexdigest(),
    "host_outcome_digest": body["host_outcome_digest"],
    "verifier_host_outcome_digest": body["verifier_host_outcome_digest"],
    "accepted": int(body["accepted"]), "observed_at_ms": body["observed_at_ms"], "content": raw}

def synthetic(index=1, accepted=True, host=None, checks=1):
  def digest(name):
    return hashlib.sha256((name + str(index)).encode()).hexdigest()
  body = {"schema_version": 1, "record_type": "operational_qa",
    "host_outcome_digest": host or digest("host"),
    "evaluator_assignment_digest": digest("assignment"), "evaluator_result_digest": digest("result"),
    "verifier_host_outcome_digest": digest("verifier"),
    "verifier_assignment_digest": digest("verifier-assignment"),
    "verifier_result_digest": digest("verifier-result"),
    "deterministic_check_digests": sorted(digest("d" + str(i)) for i in range(checks)),
    "security_check_digests": sorted(digest("s" + str(i)) for i in range(checks)),
    "deterministic_pass": accepted, "security_pass": True,
    "required_criteria_total": 1, "required_criteria_passed": int(accepted),
    "verifier_disagreement": False, "accepted": accepted, "observed_at_ms": index}
  body["record_id"] = "operational_qa:" + content_digest(body)
  return entry(body)

def parent_manifest(project, chunks):
  chain = corpus.EMPTY_CHAIN_DIGEST
  for records, signatures in chunks:
    chain = corpus.next_chain_digest(chain, corpus.chunk_digest(records, signatures))
  return {"schema_version": 1, "scope": "learning-record-staging-v1", "project_id": project,
    "source_identity_digest": "d" * 64, "key_domain_digest": "e" * 64,
    "record_count": sum(len(r) for r, _ in chunks),
    "attestation_count": sum(len(a) for _, a in chunks),
    "total_bytes": sum(sum(len(r["content"]) for r in rs) +
              sum(len(a["signature"]) for a in ats) for rs, ats in chunks),
    "chain_digest": chain}

def staged_parent(store, identity="copy", chunks=()):
  manifest = parent_manifest(store.project_id, chunks)
  store.begin_learning_import(identity, manifest, now_ms=10, before_commit=ALLOW)
  for index, (records, signatures) in enumerate(chunks):
    store.stage_learning_import_chunk(identity, index, records, signatures,
      now_ms=20 + index, before_commit=ALLOW)
  store.finish_learning_import(identity, now_ms=1000, before_commit=ALLOW)
  return manifest

def qa_manifest(project, parent, chunks):
  chain = qa.ZERO
  for rows in chunks:
    chain = qa.qa_chain_digest(chain, qa.qa_chunk_digest(rows))
  return {"schema_version": 1, "scope": "learning-operational-qa-staging-v1",
    "project_id": project, "records_manifest_digest": content_digest(parent),
    "source_identity_digest": parent["source_identity_digest"],
    "key_domain_digest": parent["key_domain_digest"],
    "qa_count": sum(len(rows) for rows in chunks),
    "total_bytes": sum(len(r["content"]) for rows in chunks for r in rows),
    "chain_digest": chain}

class ProjectLearningQATests(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
    self.addCleanup(temporary.cleanup)
    self.directory = Path(temporary.name) / "history"
    self.directory.mkdir(mode=0o700)
    self.store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
    self.addCleanup(self.store.close)
    self.parent = staged_parent(self.store)
    self.rows = sorted([synthetic(1), synthetic(2, False)], key=lambda r: r["qa_id"])

  def begin(self, chunks=None, manifest=None, callback=ALLOW):
    if manifest is None:
      manifest = qa_manifest(PROJECT, self.parent, [self.rows] if chunks is None else chunks)
    return self.store.begin_learning_qa_import("copy", manifest, now_ms=1100, before_commit=callback)

  def stage(self, index=0, rows=None, callback=ALLOW, now=1200):
    return self.store.stage_learning_qa_import_chunk("copy", index,
      self.rows if rows is None else rows, now_ms=now, before_commit=callback)

  def finish(self, now=1300, callback=ALLOW):
    return self.store.finish_learning_qa_import("copy", now_ms=now, before_commit=callback)

  def read(self, **kwargs):
    return self.store.learning_import_qa("copy", **kwargs)

  def reopen(self, owner=True):
    self.store.close()
    self.store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=owner)
    self.addCleanup(self.store.close)

  def counts(self):
    return tuple(self.store._connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
      for name in ("project_learning_qa_records", "project_learning_qa_chunks"))

  def test_original_negative_reopen_replay(self):
    started = self.begin()
    receipt = self.stage()
    finished = self.finish()
    self.assertTrue(any(r["accepted"] == 0 for r in self.rows))
    self.reopen()
    self.assertEqual(tuple(self.rows), self.read())
    self.assertEqual(receipt, self.stage(now=1))
    self.assertEqual(finished, self.finish(now=0))
    for summary in (started, receipt, finished):
      self.assertIs(False, summary["learning_ready"])
    self.assertEqual(1, self.store._connection.execute(
      "SELECT next_source_sequence FROM history_identity").fetchone()[0])
    with self.assertRaisesRegex(HistoryError, "history_not_found"):
      self.store.record(self.rows[0]["qa_id"])

  def test_empty_finished_still_unavailable(self):
    self.begin(chunks=[])
    self.assertIs(False, self.finish()["learning_ready"])
    self.assertEqual((), self.read())

  def test_restart_order_changed_replay_and_backwards_time(self):
    self.begin(chunks=[[self.rows[0]], [self.rows[1]]])
    with self.assertRaisesRegex(HistoryError, "history_conflict"):
      self.stage(index=1, rows=self.rows[1:])
    first = self.stage(rows=self.rows[:1])
    with self.assertRaisesRegex(HistoryError, "history_learning_incomplete"):
      self.finish()
    with self.assertRaisesRegex(HistoryError, "history_conflict"):
      self.stage(rows=self.rows[1:])
    self.reopen()
    self.assertEqual(first, self.stage(rows=self.rows[:1]))
    with self.assertRaisesRegex(HistoryError, "history_conflict"):
      self.stage(index=1, rows=self.rows[1:], now=1199)
    self.stage(index=1, rows=self.rows[1:], now=1201)
    self.assertEqual("staged", self.finish()["state"])

  def test_duplicate_host_rolls_back_all_rows_receipt_and_counters(self):
    rows = sorted([synthetic(10, host="a" * 64), synthetic(11, host="a" * 64)],
           key=lambda r: r["qa_id"])
    self.begin(chunks=[rows])
    with self.assertRaisesRegex(HistoryError, "history_conflict"):
      self.stage(rows=rows)
    self.assertEqual((0, 0), self.counts())
    self.assertEqual(0, self.begin(chunks=[rows])["next_chunk"])

  def test_callback_capture_and_revocation_rollback(self):
    manifest = qa_manifest(PROJECT, self.parent, [self.rows])
    self.begin(manifest=manifest, callback=lambda: manifest.update(qa_count=0))
    original = [dict(r) for r in self.rows]
    calls = []
    def revoke():
      calls.append(1)
      if len(calls) == 2:
        raise HistoryError("history_not_authorized")
    with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
      self.stage(callback=revoke)
    self.assertEqual((0, 0), self.counts())
    self.assertEqual(0, self.begin()["next_chunk"])
    self.stage(callback=lambda: self.rows[0].update(content=b"CANARY"))
    self.assertEqual(tuple(original), self.read())
    self.rows = original
    calls.clear()
    with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
      self.finish(callback=revoke)
    self.assertEqual("staging", self.begin()["state"])

  def test_parent_must_be_fully_staged_and_bound_on_every_operation(self):
    pending = parent_manifest(PROJECT, [])
    self.store.begin_learning_import("pending", pending, now_ms=10, before_commit=ALLOW)
    with self.assertRaisesRegex(HistoryError, "history_learning_incomplete"):
      self.store.begin_learning_qa_import("pending", qa_manifest(PROJECT, pending, []),
        now_ms=100, before_commit=ALLOW)
    good = qa_manifest(PROJECT, self.parent, [self.rows])
    for field in ("records_manifest_digest", "source_identity_digest", "key_domain_digest"):
      with self.subTest(field=field), self.assertRaisesRegex(HistoryError, "history_conflict"):
        self.begin(manifest={**good, field: "f" * 64})
    with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
      self.begin(manifest={**good, "project_id": "prj_" + "b" * 32})
    self.begin()
    self.stage()
    changed = {**self.parent, "source_identity_digest": "f" * 64}
    self.store._connection.execute("UPDATE project_learning_imports SET manifest=?,manifest_digest=?",
      (canonical_json(changed), content_digest(changed)))
    for operation in (self.begin, self.stage, self.finish, self.read):
      with self.subTest(operation=operation), self.assertRaisesRegex(HistoryError, "history_conflict"):
        operation()

  def test_manifest_caps_combined_budget_and_incomplete_chain(self):
    good = qa_manifest(PROJECT, self.parent, [self.rows])
    from tests.learning_support import verification_outcome
    from tests.test_project_learning_corpus import ProjectLearningCorpusTests
    record = ProjectLearningCorpusTests.entry(verification_outcome("budget"), 1)
    source = staged_parent(self.store, "budget", [([record], [])])
    manifest = qa_manifest(PROJECT, source, [self.rows])
    limit = max(manifest["total_bytes"], source["total_bytes"])
    self.assertGreater(manifest["total_bytes"] + source["total_bytes"], limit)
    with mock.patch.object(corpus, "MAX_IMPORT_BYTES", limit):
      with self.assertRaisesRegex(HistoryError, "history_learning_capacity"):
        self.store.begin_learning_qa_import("budget", manifest, now_ms=1100, before_commit=ALLOW)
    for field in ("qa_count", "total_bytes"):
      self.begin(manifest={**good, field: 0})
      with self.assertRaisesRegex(HistoryError, "history_learning_capacity"):
        self.stage()
      self.assertEqual((0, 0), self.counts())
      self.store._connection.execute("DELETE FROM project_learning_qa_imports")
    self.begin(manifest={**good, "chain_digest": "f" * 64})
    self.stage()
    with self.assertRaisesRegex(HistoryError, "history_learning_incomplete"):
      self.finish()

  def test_closed_types_canonical_metadata_and_bounds(self):
    class Dict(dict):
      pass
    row = self.rows[0]
    raw = json.dumps(json.loads(row["content"]), indent=2).encode()
    variants = [Dict(row), dict(row, extra=1), dict(row, accepted=True),
      dict(row, observed_at_ms=True), dict(row, qa_digest="f"*64), dict(row, qa_id="other"),
      dict(row, host_outcome_digest="f"*64), dict(row, verifier_host_outcome_digest="f"*64),
      dict(row, observed_at_ms=999), dict(row, accepted=1-row["accepted"]),
      dict(row, content=b"[]"), dict(row, content=b"\xff"), dict(row, content=b"x"*65537),
      dict(row, content=raw, qa_digest=hashlib.sha256(raw).hexdigest())]
    for value in variants:
      with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
        qa.qa_chunk_digest([value])
    for value in ([], [row]*65, iter([row]), [row, row], self.rows[::-1]):
      with self.assertRaises(HistoryError):
        qa.qa_chunk_digest(value)
    with mock.patch.object(qa, "MAX_BYTES", 1), self.assertRaises(HistoryError):
      qa.qa_chunk_digest([row])
    for field, value in (("qa_count", True), ("qa_count", 100001), ("total_bytes", -1),
               ("extra", 1), ("schema_version", True)):
      with self.assertRaises(HistoryError):
        self.begin(manifest={**qa_manifest(PROJECT, self.parent, []), field: value})
    self.begin()
    for index in (True, -1, 100000):
      with self.assertRaises(HistoryError):
        self.stage(index=index)
    self.assertEqual((0, 0), self.counts())

  def test_corrupt_receipt_and_content(self):
    self.begin()
    receipt = self.stage()
    for corrupt in (b"CANARY", canonical_json({**receipt, "private": "CANARY"}),
            canonical_json({**receipt, "qa_count": True}),
            canonical_json({**receipt, "total_bytes": receipt["total_bytes"] - 1}),
            b"x" * 10000, 999999999):
      self.store._connection.execute("UPDATE project_learning_qa_chunks SET receipt=?", (corrupt,))
      with self.assertRaisesRegex(HistoryError, "^history_incompatible$"):
        self.stage()
    self.store._connection.execute("UPDATE project_learning_qa_chunks SET receipt=?", (canonical_json(receipt),))
    for corrupt in (b"CANARY", b"x" * 1000000, 999999999, "CANARY"):
      # Corrupt a disposable image through a separate fixture connection.
      # The production owner's finite SQLite row limit stays unchanged.
      with closing(sqlite3.connect(self.directory / "history.sqlite3")) as injector, injector:
        injector.execute("UPDATE project_learning_qa_records SET content=?", (corrupt,))
      with self.assertRaisesRegex(HistoryError, "^history_incompatible$"):
        self.read()
      with self.assertRaisesRegex(HistoryError, "^history_incompatible$"):
        self.stage()

  def test_owner_and_outer_transaction_refusals(self):
    self.begin()
    operations = (self.begin, self.stage, self.finish, self.read)
    errors = []
    def worker():
      for operation in operations:
        try:
          operation()
        except HistoryError as error:
          errors.append(error.reason_code)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    self.assertFalse(thread.is_alive())
    self.assertEqual(["history_wrong_owner"] * 4, errors)
    with mock.patch.object(self.store, "_pid", -1):
      with self.assertRaisesRegex(HistoryError, "history_wrong_owner"):
        self.read()
    with self.store._transaction():
      for operation in operations:
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
          operation()
      self.assertTrue(self.store._connection.in_transaction)
    self.reopen(owner=False)
    for operation in (self.begin, self.stage, self.finish, self.read):
      with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
        operation()

  def test_bounded_indexed_pages_and_cursor_close(self):
    rows = sorted([synthetic(i, checks=256) for i in range(10, 30)], key=lambda r: r["qa_id"])
    chunks = [[row] for row in rows]
    self.begin(chunks=chunks)
    for index, chunk in enumerate(chunks):
      self.stage(index=index, rows=chunk, now=1200 + index)
    collected, after = [], ""
    while True:
      page = self.read(after_qa_id=after, limit=64)
      if not page:
        break
      self.assertLessEqual(sum(len(r["content"]) for r in page), 262144)
      self.assertLess(len(page), 64)
      collected.extend(page)
      after = page[-1]["qa_id"]
      self.assertFalse(self.store._connection.in_transaction)
    self.assertEqual(rows, collected)
    for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 65}, {"after_qa_id": True},
            {"after_qa_id": "x" * 161}):
      with self.assertRaises(HistoryError):
        self.read(**kwargs)
    plan = self.store._connection.execute(
      "EXPLAIN QUERY PLAN SELECT qa_id FROM project_learning_qa_records"
      " WHERE project_id=? AND import_id=? AND qa_id>? ORDER BY qa_id LIMIT ?",
      (PROJECT, "copy", "", 32)).fetchall()
    self.assertIn("SEARCH", " ".join(str(tuple(r)) for r in plan))
    self.assertNotIn("TEMP B-TREE", " ".join(str(tuple(r)) for r in plan))
    cursor = mock.Mock()
    cursor.__iter__ = mock.Mock(return_value=iter([]))
    proxy = mock.Mock()
    proxy.execute.return_value = cursor
    with mock.patch.object(self.store, "_connection", proxy):
      self.assertEqual((), qa._page(self.store, "copy", "", 1))
      cursor.close.assert_called_once()
    cursor.reset_mock()
    cursor.__iter__ = mock.Mock(return_value=iter([{"project_id": "CANARY"}]))
    with mock.patch.object(self.store, "_connection", proxy):
      with self.assertRaisesRegex(HistoryError, "^history_incompatible$"):
        qa._page(self.store, "copy", "", 1)
      cursor.close.assert_called_once()

class ActualLegacyOperationalQACopyTests(unittest.TestCase):
  def test_actual_legacy_source_copy(self):
    fixture = legacy.LearningStoreTests()
    fixture.setUp()
    self.addCleanup(fixture.tearDown)
    fixture.test_operational_qa_supersedes_unverified_run_and_surfaces_no_owner_review()
    project = fixture.project_a_id
    qa_rows = fixture.ledger._connection.execute(
      "SELECT * FROM operational_qa WHERE project_id=? ORDER BY qa_id", (project,)).fetchall()
    original = [{name: bytes(row[name]) if name == "content" else row[name]
           for name in qa._FIELDS} for row in qa_rows]
    self.assertTrue(original)
    selected = fixture.ledger._connection.execute(
      "SELECT * FROM records WHERE project_id=? ORDER BY source_sequence", (project,)).fetchall()
    self.assertTrue(all(row["record_type"] in corpus._RECORD_TYPES for row in selected))
    records = [{"content": bytes(row["content"]), "source": row["source"],
          "source_sequence": row["source_sequence"], "observed_at_ms": row["observed_at_ms"],
          "ingested_at_ms": row["ingested_at_ms"], "expires_at_ms": row["expires_at_ms"],
          "late": bool(row["late"])} for row in selected]
    signatures = [{"subject_record_id": row["subject_record_id"], "signature": bytes(row["signature"]),
            "observed_at_ms": row["attested_at_ms"]}
           for row in fixture.ledger._connection.execute(
             "SELECT * FROM owner_attestations WHERE project_id=? ORDER BY subject_record_id", (project,))]
    fixture.ledger.close()
    directory = fixture.state_root.parent / "qa-target"
    directory.mkdir(mode=0o700)
    target = ProjectHistory(directory, project, allow_schema_upgrade=True)
    self.addCleanup(target.close)
    chunks = [([r], []) for r in records] + [([], [a]) for a in signatures]
    parent = staged_parent(target, chunks=chunks)
    qa_chunks = [[r] for r in original]
    manifest = qa_manifest(project, parent, qa_chunks)
    target.begin_learning_qa_import("copy", manifest, now_ms=1100, before_commit=ALLOW)
    for index, chunk in enumerate(qa_chunks):
      target.stage_learning_qa_import_chunk("copy", index, chunk,
        now_ms=1200 + index, before_commit=ALLOW)
    summary = target.finish_learning_qa_import("copy", now_ms=2000, before_commit=ALLOW)
    self.assertIs(False, summary["learning_ready"])
    target.close()
    target = ProjectHistory(directory, project, allow_schema_upgrade=True)
    self.addCleanup(target.close)
    self.assertEqual(original, list(target.learning_import_qa("copy", limit=64)))
    self.assertEqual(records, list(target.learning_import_records("copy", limit=64)))
    self.assertEqual(signatures, list(target.learning_import_attestations("copy", limit=64)))
