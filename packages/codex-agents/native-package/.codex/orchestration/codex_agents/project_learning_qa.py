"""Original QA bytes are staging data, never provenance or live authority.
Drain, source/key-domain/dependency closure, execution/check receipts and old-writer
retirement remain external release gates. Every receipt stays unavailable.
"""
from contextlib import contextmanager
import hashlib
import sqlite3

from . import project_learning_corpus as corpus
from .history_contracts import HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, parse_bounded_json
from .operational_qa_contracts import checked_operational_qa

MAX_QA = 100_000
MAX_ROWS = 64
MAX_BYTES = 262_144
ZERO = "0" * 64
_FIELDS = ("qa_id", "qa_digest", "host_outcome_digest",
      "verifier_host_outcome_digest", "accepted", "observed_at_ms", "content")
_MANIFEST = frozenset(("schema_version", "scope", "project_id",
  "records_manifest_digest", "source_identity_digest", "key_domain_digest",
  "qa_count", "total_bytes", "chain_digest"))
_RECEIPT = frozenset(("import_id", "manifest_digest", "chunk_index", "chunk_digest",
           "qa_count", "total_bytes", "chain_digest", "learning_ready"))

QA_SCHEMA = """
CREATE TABLE project_learning_qa_imports(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,manifest_digest TEXT NOT NULL,manifest BLOB NOT NULL,
 next_chunk INTEGER NOT NULL,qa_count INTEGER NOT NULL,total_bytes INTEGER NOT NULL,
 last_qa_id TEXT NOT NULL,chain_digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staging','staged')),
 created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,import_id),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_imports(project_id,import_id));
CREATE TABLE project_learning_qa_records(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,qa_id TEXT NOT NULL,qa_digest TEXT NOT NULL,
 host_outcome_digest TEXT NOT NULL,verifier_host_outcome_digest TEXT NOT NULL,
 accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),observed_at_ms INTEGER NOT NULL,content BLOB NOT NULL,
 PRIMARY KEY(project_id,import_id,qa_id),UNIQUE(project_id,import_id,host_outcome_digest),
 UNIQUE(project_id,import_id,qa_digest),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_qa_imports(project_id,import_id));
CREATE TABLE project_learning_qa_chunks(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,
 chunk_digest TEXT NOT NULL,receipt BLOB NOT NULL,
 PRIMARY KEY(project_id,import_id,chunk_index),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_qa_imports(project_id,import_id));
"""
QA_COLUMNS = {
  "project_learning_qa_imports": ("project_id", "import_id", "manifest_digest",
    "manifest", "next_chunk", "qa_count", "total_bytes", "last_qa_id",
    "chain_digest", "state", "created_at_ms", "updated_at_ms"),
  "project_learning_qa_records": ("project_id", "import_id", *_FIELDS),
  "project_learning_qa_chunks": ("project_id", "import_id", "chunk_index", "chunk_digest", "receipt"),
}
_TEXT = {"project_id": 36, "import_id": 160, "manifest_digest": 64,
  "last_qa_id": 160, "qa_id": 160, "chain_digest": 64, "state": 7,
  "qa_digest": 64, "host_outcome_digest": 64, "verifier_host_outcome_digest": 64,
  "chunk_digest": 64}
_BLOB = {"manifest": MAX_RECORD_BYTES, "content": MAX_RECORD_BYTES, "receipt": 4096}

def _projection(columns):
  parts = []
  for name in columns:
    if name in _TEXT:
      guard = "typeof(%s)='text' AND length(CAST(%s AS BLOB))<=%d" % (
        name, name, _TEXT[name])
    elif name in _BLOB:
      guard = "typeof(%s)='blob' AND length(%s) BETWEEN 1 AND %d" % (
        name, name, _BLOB[name])
    else:
      guard = "typeof(%s)='integer'" % name
    parts.append("CASE WHEN " + guard + " THEN " + name + " END AS " + name)
  return ",".join(parts)

def _one(history, table, columns, import_id, suffix="", extra=()):
  cursor = history._connection.execute(
    "SELECT " + _projection(columns) + " FROM " + table +
    " WHERE project_id=? AND import_id=?" + suffix,
    (history.project_id, import_id, *extra))
  try:
    result = cursor.fetchone()
    if result is None:
      return None
    value = dict(result)
    if any(v is None for v in value.values()):
      raise HistoryError("history_incompatible")
    return value
  finally:
    cursor.close()

def _access(history, import_id):
  corpus._owner(history, import_id)
  if history._connection.in_transaction:
    raise HistoryError("history_conflict")

def _auth(history, import_id, callback):
  corpus._authorization(callback)
  corpus._owner(history, import_id)

@contextmanager
def _transaction(history):
  try:
    with history._transaction():
      yield
  except sqlite3.IntegrityError:
    raise HistoryError("history_conflict") from None
  except sqlite3.Error:
    raise HistoryError("history_incompatible") from None

def _manifest(value, project):
  if (type(value) is not dict or set(value) != _MANIFEST
      or type(value["schema_version"]) is not int or value["schema_version"] != 1
      or type(value["scope"]) is not str
      or value["scope"] != "learning-operational-qa-staging-v1"
      or type(value["project_id"]) is not str or value["project_id"] != project
      or not all(matches(value[n], _DIGEST) for n in (
        "records_manifest_digest", "source_identity_digest", "key_domain_digest", "chain_digest"))
      or not valid_integer(value["qa_count"], maximum=MAX_QA)
      or not valid_integer(value["total_bytes"], maximum=corpus.MAX_IMPORT_BYTES)):
    raise HistoryError("history_invalid_input")
  return dict(value)

def _entry(value):
  try:
    if (type(value) is not dict or set(value) != set(_FIELDS)
        or not matches(value["qa_id"], _ID)
        or not all(matches(value[n], _DIGEST) for n in (
          "qa_digest", "host_outcome_digest", "verifier_host_outcome_digest"))
        or not valid_integer(value["accepted"], maximum=1)
        or not valid_integer(value["observed_at_ms"])
        or type(value["content"]) is not bytes
        or not 1 <= len(value["content"]) <= MAX_RECORD_BYTES):
      raise ValueError()
    captured = dict(value)
    body = checked_operational_qa(parse_bounded_json(captured["content"]))
    if (canonical_json(body) != captured["content"]
        or captured["qa_id"] != body["record_id"]
        or captured["qa_digest"] != hashlib.sha256(captured["content"]).hexdigest()
        or any(captured[n] != body[n] for n in (
          "host_outcome_digest", "verifier_host_outcome_digest", "observed_at_ms"))
        or captured["accepted"] != int(body["accepted"])):
      raise ValueError()
    return captured
  except Exception:
    raise HistoryError("history_invalid_input") from None

def _prepare(rows):
  if type(rows) not in (list, tuple) or not 1 <= len(rows) <= MAX_ROWS:
    raise HistoryError("history_invalid_input")
  captured, size, previous = [], 0, ""
  for row in rows:
    entry = _entry(row)
    if entry["qa_id"] <= previous:
      raise HistoryError("history_invalid_input")
    previous = entry["qa_id"]
    size += len(entry["content"])
    if size > MAX_BYTES:
      raise HistoryError("history_learning_capacity")
    captured.append(entry)
  material = [{**r, "content": r["content"].decode("utf-8")} for r in captured]
  digest = hashlib.sha256(b"learning-operational-qa-chunk-v1\0" +
              canonical_json({"rows": material})).hexdigest()
  return tuple(captured), size, digest

def qa_chunk_digest(rows):
  """Bounded source helper; no writer, provenance or authority."""
  return _prepare(rows)[2]

def qa_chain_digest(previous, digest):
  if not matches(previous, _DIGEST) or not matches(digest, _DIGEST):
    raise HistoryError("history_invalid_input")
  return hashlib.sha256(b"learning-operational-qa-chain-v1\0" +
             bytes.fromhex(previous) + bytes.fromhex(digest)).hexdigest()

def _parent(history, import_id, manifest):
  guarded = _one(history, "project_learning_imports",
          corpus.LEARNING_COLUMNS["project_learning_imports"], import_id)
  if guarded is None:
    raise HistoryError("history_not_found")
  parent, source = corpus._row(history, import_id)
  if guarded["manifest"] != canonical_json(source):
    raise HistoryError("history_incompatible")
  if (parent["state"] != "staged" or any(parent[n] != source[n] for n in (
      "record_count", "attestation_count", "total_bytes", "chain_digest"))):
    raise HistoryError("history_learning_incomplete")
  if (manifest["records_manifest_digest"] != parent["manifest_digest"]
      or any(manifest[n] != source[n] for n in ("source_identity_digest", "key_domain_digest"))):
    raise HistoryError("history_conflict")
  if parent["total_bytes"] + manifest["total_bytes"] > corpus.MAX_IMPORT_BYTES:
    raise HistoryError("history_learning_capacity")

def _receipt(history, row, index):
  stored = _one(history, "project_learning_qa_chunks",
         QA_COLUMNS["project_learning_qa_chunks"], row["import_id"],
         " AND chunk_index=?", (index,))
  try:
    if stored is None:
      raise ValueError()
    raw = history._stored_blob(stored["receipt"], maximum=4096)
    value = parse_bounded_json(raw)
    if (type(value) is not dict or set(value) != _RECEIPT
        or canonical_json(value) != raw or value["learning_ready"] is not False
        or value["import_id"] != row["import_id"]
        or value["manifest_digest"] != row["manifest_digest"]
        or not valid_integer(value["chunk_index"], maximum=row["next_chunk"] - 1)
        or value["chunk_index"] != index
        or stored["project_id"] != row["project_id"]
        or stored["import_id"] != row["import_id"] or stored["chunk_index"] != index
        or not matches(value["chunk_digest"], _DIGEST)
        or value["chunk_digest"] != stored["chunk_digest"]
        or not matches(value["chain_digest"], _DIGEST)
        or not valid_integer(value["qa_count"], minimum=index + 1,
          maximum=min(row["qa_count"], MAX_ROWS * (index + 1)))
        or not valid_integer(value["total_bytes"], minimum=value["qa_count"],
          maximum=min(row["total_bytes"], value["qa_count"] * MAX_RECORD_BYTES))
        or (index == row["next_chunk"] - 1 and any(value[n] != row[n]
          for n in ("qa_count", "total_bytes", "chain_digest")))):
      raise ValueError()
    return value
  except Exception:
    raise HistoryError("history_incompatible") from None

def _row(history, import_id):
  row = _one(history, "project_learning_qa_imports",
        QA_COLUMNS["project_learning_qa_imports"], import_id)
  if row is None:
    raise HistoryError("history_not_found")
  try:
    raw = history._stored_blob(row["manifest"], maximum=MAX_RECORD_BYTES)
    manifest = _manifest(parse_bounded_json(raw), history.project_id)
    if (raw != canonical_json(manifest)
        or row["project_id"] != history.project_id or row["import_id"] != import_id
        or hashlib.sha256(raw).hexdigest() != row["manifest_digest"]
        or row["state"] not in ("staging", "staged")
        or not valid_integer(row["next_chunk"], maximum=MAX_QA)
        or not valid_integer(row["qa_count"], minimum=row["next_chunk"],
          maximum=min(manifest["qa_count"], MAX_ROWS * row["next_chunk"]))
        or not valid_integer(row["total_bytes"], minimum=row["qa_count"],
          maximum=min(manifest["total_bytes"], MAX_BYTES * row["next_chunk"]))
        or not matches(row["chain_digest"], _DIGEST)
        or not valid_integer(row["created_at_ms"])
        or not valid_integer(row["updated_at_ms"], minimum=row["created_at_ms"])
        or (row["qa_count"] > 0 and not matches(row["last_qa_id"], _ID))
        or (row["qa_count"] == 0 and
          (row["last_qa_id"] != "" or row["chain_digest"] != ZERO))
        or (row["state"] == "staged" and any(row[n] != manifest[n]
          for n in ("qa_count", "total_bytes", "chain_digest")))):
      raise ValueError()
    if row["next_chunk"]:
      _receipt(history, row, row["next_chunk"] - 1)
    last = _one(history, "project_learning_qa_records",
      QA_COLUMNS["project_learning_qa_records"], import_id, " ORDER BY qa_id DESC LIMIT 1")
    if row["qa_count"] == 0:
      if last is not None:
        raise ValueError()
    elif last is None or last["qa_id"] != row["last_qa_id"]:
      raise ValueError()
    else:
      _entry({n: last[n] for n in _FIELDS})
  except Exception:
    raise HistoryError("history_incompatible") from None
  _parent(history, import_id, manifest)
  return row, manifest

def _summary(row):
  return {n: row[n] for n in ("import_id", "manifest_digest", "state", "next_chunk",
      "qa_count", "total_bytes", "chain_digest")} | {"learning_ready": False}

def _start(history, import_id, now_ms, callback):
  if not valid_integer(now_ms):
    raise HistoryError("history_invalid_input")
  _auth(history, import_id, callback)
  _access(history, import_id)

def begin_qa_import(history, import_id, manifest, *, now_ms, before_commit):
  _access(history, import_id)
  manifest = _manifest(manifest, history.project_id)
  raw = canonical_json(manifest)
  digest = hashlib.sha256(raw).hexdigest()
  _start(history, import_id, now_ms, before_commit)
  with _transaction(history):
    _parent(history, import_id, manifest)
    known = _one(history, "project_learning_qa_imports",
           QA_COLUMNS["project_learning_qa_imports"], import_id)
    if known is not None:
      row, _ = _row(history, import_id)
      if row["manifest_digest"] != digest or row["manifest"] != raw:
        raise HistoryError("history_conflict")
    else:
      history._connection.execute(
        "INSERT INTO project_learning_qa_imports VALUES(?,?,?,?,0,0,0,?,?,'staging',?,?)",
        (history.project_id, import_id, digest, raw, "", ZERO, now_ms, now_ms))
      row, _ = _row(history, import_id)
    _auth(history, import_id, before_commit)
    return _summary(row)

def _page(history, import_id, after, limit):
  columns = QA_COLUMNS["project_learning_qa_records"]
  cursor = history._connection.execute(
    "SELECT " + _projection(columns) +
    " FROM project_learning_qa_records WHERE project_id=? AND import_id=? AND qa_id>?" +
    " ORDER BY qa_id LIMIT ?",
    (history.project_id, import_id, after, limit))
  result, size, previous = [], 0, after
  try:
    for stored in cursor:
      try:
        if (stored["project_id"] != history.project_id or stored["import_id"] != import_id
            or not matches(stored["qa_id"], _ID) or stored["qa_id"] <= previous):
          raise ValueError()
        entry = _entry({n: stored[n] for n in _FIELDS})
      except Exception:
        raise HistoryError("history_incompatible") from None
      if size + len(entry["content"]) > MAX_BYTES:
        break
      result.append(entry)
      size += len(entry["content"])
      previous = entry["qa_id"]
  finally:
    cursor.close()
  return tuple(result)

def stage_qa_chunk(history, import_id, index, rows, *, now_ms, before_commit):
  _access(history, import_id)
  if not valid_integer(index, maximum=MAX_QA - 1):
    raise HistoryError("history_invalid_input")
  rows, size, digest = _prepare(rows)
  _start(history, import_id, now_ms, before_commit)
  with _transaction(history):
    row, manifest = _row(history, import_id)
    if index < row["next_chunk"]:
      receipt = _receipt(history, row, index)
      if receipt["chunk_digest"] != digest:
        raise HistoryError("history_conflict")
      previous = (_receipt(history, row, index - 1) if index else
            {"qa_count": 0, "total_bytes": 0, "chain_digest": ZERO})
      if (receipt["qa_count"] != previous["qa_count"] + len(rows)
          or receipt["total_bytes"] != previous["total_bytes"] + size
          or receipt["chain_digest"] != qa_chain_digest(previous["chain_digest"], digest)):
        raise HistoryError("history_incompatible")
      if index + 1 < row["next_chunk"]:
        following = _receipt(history, row, index + 1)
        if (following["chain_digest"] != qa_chain_digest(
            receipt["chain_digest"], following["chunk_digest"])
            or not 1 <= following["qa_count"] - receipt["qa_count"] <= MAX_ROWS
            or not 1 <= following["total_bytes"] - receipt["total_bytes"] <= MAX_BYTES):
          raise HistoryError("history_incompatible")
      captured = []
      for entry in rows:
        stored = _one(history, "project_learning_qa_records",
          QA_COLUMNS["project_learning_qa_records"], import_id,
          " AND qa_id=?", (entry["qa_id"],))
        try:
          captured.append(_entry({n: stored[n] for n in _FIELDS}))
        except Exception:
          raise HistoryError("history_incompatible") from None
      if tuple(captured) != rows:
        raise HistoryError("history_incompatible")
    else:
      if (row["state"] != "staging" or index != row["next_chunk"]
          or now_ms < row["updated_at_ms"] or rows[0]["qa_id"] <= row["last_qa_id"]):
        raise HistoryError("history_conflict")
      count, total = row["qa_count"] + len(rows), row["total_bytes"] + size
      if count > manifest["qa_count"] or total > manifest["total_bytes"]:
        raise HistoryError("history_learning_capacity")
      for entry in rows:
        history._connection.execute(
          "INSERT INTO project_learning_qa_records VALUES(?,?,?,?,?,?,?,?,?)",
          (history.project_id, import_id, *(entry[n] for n in _FIELDS)))
      chain = qa_chain_digest(row["chain_digest"], digest)
      history._connection.execute(
        "UPDATE project_learning_qa_imports SET next_chunk=?,qa_count=?,total_bytes=?,"
        "last_qa_id=?,chain_digest=?,updated_at_ms=? WHERE project_id=? AND import_id=?",
        (index + 1, count, total, rows[-1]["qa_id"], chain, now_ms, history.project_id, import_id))
      receipt = {"import_id": import_id, "manifest_digest": row["manifest_digest"],
        "chunk_index": index, "chunk_digest": digest, "qa_count": count,
        "total_bytes": total, "chain_digest": chain, "learning_ready": False}
      history._connection.execute("INSERT INTO project_learning_qa_chunks VALUES(?,?,?,?,?)",
        (history.project_id, import_id, index, digest, canonical_json(receipt)))
    _auth(history, import_id, before_commit)
    return receipt

def finish_qa_import(history, import_id, *, now_ms, before_commit):
  _access(history, import_id)
  _start(history, import_id, now_ms, before_commit)
  with _transaction(history):
    row, manifest = _row(history, import_id)
    if any(row[n] != manifest[n] for n in ("qa_count", "total_bytes", "chain_digest")):
      raise HistoryError("history_learning_incomplete")
    if row["state"] != "staged":
      if now_ms < row["updated_at_ms"]:
        raise HistoryError("history_conflict")
      history._connection.execute(
        "UPDATE project_learning_qa_imports SET state='staged',updated_at_ms=?"
        " WHERE project_id=? AND import_id=?", (now_ms, history.project_id, import_id))
      row["state"] = "staged"
    _auth(history, import_id, before_commit)
    return _summary(row)

def import_qa_rows(history, import_id, *, after_qa_id="", limit=32):
  _access(history, import_id)
  if ((type(after_qa_id) is not str or (after_qa_id != "" and not matches(after_qa_id, _ID)))
      or not valid_integer(limit, minimum=1, maximum=MAX_ROWS)):
    raise HistoryError("history_invalid_input")
  with _transaction(history):
    _row(history, import_id)
    return _page(history, import_id, after_qa_id, limit)
