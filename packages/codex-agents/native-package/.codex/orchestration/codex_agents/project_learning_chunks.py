"""Exact original chunk membership; inert migration-owner staging only."""
from contextlib import contextmanager
import hashlib
import sqlite3

from . import project_learning_corpus as corpus
from .history_contracts import HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, parse_bounded_json

CHUNK_SCHEMA = """
CREATE TABLE project_learning_chunk_memberships(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,
 chunk_digest TEXT NOT NULL,membership BLOB NOT NULL,membership_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,import_id,chunk_index),
 FOREIGN KEY(project_id,import_id,chunk_index)
 REFERENCES project_learning_import_chunks(project_id,import_id,chunk_index));
"""
CHUNK_COLUMNS = {
    "project_learning_chunk_memberships": (
        "project_id", "import_id", "chunk_index", "chunk_digest", "membership", "membership_digest"),
}
MAX_MEMBERSHIP_BYTES = 16_384
_TEXT = {
    "project_id": 36, "import_id": 160, "record_id": 160, "subject_record_id": 160,
    "record_type": 64, "source": 64, "state": 7, "manifest_digest": 64,
    "content_digest": 64, "signature_digest": 64, "chain_digest": 64,
    "chunk_digest": 64, "membership_digest": 64,
}
_BLOB = {
    "manifest": MAX_RECORD_BYTES, "content": MAX_RECORD_BYTES,
    "signature": corpus.MAX_SIGNATURE_BYTES, "receipt": 16_384,
    "membership": MAX_MEMBERSHIP_BYTES,
}


def _one(history, table, columns, import_id, suffix="", extra=()):
    # All identifiers and suffixes are module constants, never caller SQL.
    projection = []
    for name in columns:
        if name in _TEXT:
            guard = "typeof(%s)='text' AND length(CAST(%s AS BLOB)) BETWEEN 1 AND %d" % (
                name, name, _TEXT[name])
        elif name in _BLOB:
            guard = "typeof(%s)='blob' AND length(%s) BETWEEN 1 AND %d" % (
                name, name, _BLOB[name])
        elif name == "expires_at_ms":
            guard = "typeof(expires_at_ms) IN ('integer','null')"
        else:
            guard = "typeof(%s)='integer'" % name
        fallback = " ELSE -1" if name == "expires_at_ms" else ""
        projection.append("CASE WHEN " + guard + " THEN " + name + fallback + " END AS " + name)
    cursor = history._connection.execute(
        "SELECT " + ",".join(projection) + " FROM " + table
        + " WHERE project_id=? AND import_id=?" + suffix,
        (history.project_id, import_id, *extra))
    try:
        row = cursor.fetchone()
        if row is None:
            return None
        value = dict(row)
        if any(v is None for n, v in value.items() if n != "expires_at_ms"):
            raise HistoryError("history_incompatible")
        return value
    finally:
        cursor.close()


def _index(index):
    if not valid_integer(index, maximum=2 * corpus.MAX_IMPORT_RECORDS - 1):
        raise HistoryError("history_invalid_input")


def _parent(history, import_id):
    # Guard stored values in SQL before corpus._row materializes/parses them.
    guarded = _one(history, "project_learning_imports",
                   corpus.LEARNING_COLUMNS["project_learning_imports"], import_id)
    if guarded is None:
        raise HistoryError("history_not_found")
    row, manifest = corpus._row(history, import_id)
    if canonical_json(manifest) != guarded["manifest"]:
        raise HistoryError("history_incompatible")
    return row


def _receipt(history, import_id, index, parent):
    stored = _one(history, "project_learning_import_chunks",
                  corpus.LEARNING_COLUMNS["project_learning_import_chunks"], import_id,
                  " AND chunk_index=?", (index,))
    if stored is None:
        raise HistoryError("history_not_found")
    try:
        raw = stored["receipt"]
        value = parse_bounded_json(raw)
        if (type(value) is not dict or set(value) != corpus._RECEIPT_FIELDS
                or canonical_json(value) != raw
                or value["import_id"] != import_id
                or value["manifest_digest"] != parent["manifest_digest"]
                or value["chunk_index"] != index
                or not valid_integer(value["chunk_index"], maximum=parent["next_chunk"] - 1)
                or not matches(stored["chunk_digest"], _DIGEST)
                or value["chunk_digest"] != stored["chunk_digest"]
                or not matches(value["chain_digest"], _DIGEST)
                or any(not valid_integer(value[n], maximum=parent[n]) for n in (
                    "record_count", "attestation_count", "total_bytes"))
                or (index == parent["next_chunk"] - 1 and any(value[n] != parent[n] for n in (
                    "record_count", "attestation_count", "total_bytes", "chain_digest")))
                or value["learning_ready"] is not False):
            raise ValueError()
        return value
    except Exception:
        raise HistoryError("history_incompatible") from None


def _lists(value):
    if (type(value) is not dict or set(value) != {"record_ids", "attestation_subject_ids"}
            or any(type(v) is not list for v in value.values())
            or not 1 <= sum(len(v) for v in value.values()) <= corpus.MAX_CHUNK_ENTRIES):
        raise HistoryError("history_incompatible")
    for ids in value.values():
        if any(not matches(identity, _ID) for identity in ids) or len(set(ids)) != len(ids):
            raise HistoryError("history_incompatible")
    return value


def _membership_digest(history, import_id, index, digest, membership):
    return hashlib.sha256(b"learning-chunk-membership-v1\0" + canonical_json({
        "project_id": history.project_id, "import_id": import_id, "chunk_index": index,
        "chunk_digest": digest, "membership": membership,
    })).hexdigest()


def _membership(history, import_id, index, digest):
    row = _one(history, "project_learning_chunk_memberships",
               CHUNK_COLUMNS["project_learning_chunk_memberships"], import_id,
               " AND chunk_index=?", (index,))
    if row is None:
        return None
    try:
        value = _lists(parse_bounded_json(row["membership"], max_bytes=MAX_MEMBERSHIP_BYTES))
        if (canonical_json(value) != row["membership"] or row["chunk_digest"] != digest
                or row["membership_digest"] != _membership_digest(
                    history, import_id, index, digest, value)):
            raise ValueError()
        return value
    except Exception:
        raise HistoryError("history_incompatible") from None


def _record(history, import_id, identity, remaining):
    row = _one(history, "project_learning_records",
               corpus.LEARNING_COLUMNS["project_learning_records"], import_id,
               " AND record_id=? AND length(content)<=?", (identity, remaining))
    if row is None:
        raise HistoryError("history_incompatible")
    try:
        if type(row["late"]) is not int or row["late"] not in (0, 1):
            raise ValueError()
        entry = {n: row[n] for n in corpus._ENTRY_FIELDS}
        entry["late"] = bool(row["late"])
        checked = corpus._prepare_chunk([entry], [])[0][0][1]
        if (checked["record_id"] != identity or row["record_type"] != checked["record_type"]
                or row["content_digest"] != hashlib.sha256(entry["content"]).hexdigest()):
            raise ValueError()
        return entry
    except Exception:
        raise HistoryError("history_incompatible") from None


def _signature(history, import_id, identity, remaining):
    row = _one(history, "project_learning_attestations",
               corpus.LEARNING_COLUMNS["project_learning_attestations"], import_id,
               " AND subject_record_id=? AND length(signature)<=?", (identity, remaining))
    if row is None:
        raise HistoryError("history_incompatible")
    entry = {n: row[n] for n in corpus._ATTESTATION_FIELDS}
    try:
        corpus._prepare_chunk([], [entry])
        if row["signature_digest"] != hashlib.sha256(entry["signature"]).hexdigest():
            raise ValueError()
        # Signature-only chunks may refer to a record in an earlier chunk.
        # Do not fetch that unlisted record's content or scan the partition.
        subject = _one(history, "project_learning_records", ("record_type",), import_id,
                       " AND record_id=?", (identity,))
        if subject is None or subject["record_type"] not in corpus._SIGNED_RECORD_TYPES:
            raise ValueError()
        return entry
    except Exception:
        raise HistoryError("history_incompatible") from None


def _material(history, import_id, index, membership, receipt, parent):
    records, signatures, size = [], [], 0
    for identity in membership["record_ids"]:
        entry = _record(history, import_id, identity, corpus.MAX_CHUNK_BYTES - size)
        records.append(entry)
        size += len(entry["content"])
    for identity in membership["attestation_subject_ids"]:
        entry = _signature(history, import_id, identity, corpus.MAX_CHUNK_BYTES - size)
        signatures.append(entry)
        size += len(entry["signature"])
    try:
        digest = corpus.chunk_digest(records, signatures)
        previous = (_receipt(history, import_id, index - 1, parent) if index else {
            "record_count": 0, "attestation_count": 0, "total_bytes": 0,
            "chain_digest": corpus.EMPTY_CHAIN_DIGEST,
        })
        if (digest != receipt["chunk_digest"]
                or receipt["record_count"] != previous["record_count"] + len(records)
                or receipt["attestation_count"] != previous["attestation_count"] + len(signatures)
                or receipt["total_bytes"] != previous["total_bytes"] + size
                or receipt["chain_digest"] != corpus.next_chain_digest(previous["chain_digest"], digest)):
            raise ValueError()
    except Exception:
        raise HistoryError("history_incompatible") from None
    return tuple(records), tuple(signatures)


def record_chunk_membership(history, import_id, index, records, attestations, digest):
    """Only inside the original stage transaction, with already captured material."""
    corpus._owner(history, import_id)
    _index(index)
    if not matches(digest, _DIGEST):
        raise HistoryError("history_invalid_input")
    if not history._connection.in_transaction:
        raise HistoryError("history_conflict")
    try:
        entries = [entry for entry, checked in records]
        # Recheck the captured material, never the caller's mutable input.
        if corpus.chunk_digest(entries, attestations) != digest:
            raise HistoryError("history_conflict")
        membership = _lists({
            "record_ids": [checked["record_id"] for entry, checked in records],
            "attestation_subject_ids": [entry["subject_record_id"] for entry in attestations],
        })
        raw = canonical_json(membership)
        if len(raw) > MAX_MEMBERSHIP_BYTES:
            raise HistoryError("history_incompatible")
        parent = _parent(history, import_id)
        receipt = _receipt(history, import_id, index, parent)
        if receipt["chunk_digest"] != digest:
            raise HistoryError("history_conflict")
        known = _membership(history, import_id, index, digest)
        if known is not None and known != membership:
            raise HistoryError("history_conflict")
        original_records, original_signatures = _material(
            history, import_id, index, membership, receipt, parent)
        if original_records != tuple(entries) or original_signatures != tuple(attestations):
            raise HistoryError("history_incompatible")
        if known is None:
            cursor = history._connection.execute(
                "INSERT INTO project_learning_chunk_memberships VALUES(?,?,?,?,?,?)",
                (history.project_id, import_id, index, digest, raw,
                 _membership_digest(history, import_id, index, digest, membership)))
            cursor.close()
    except sqlite3.IntegrityError:
        raise HistoryError("history_conflict") from None
    except sqlite3.Error:
        raise HistoryError("history_incompatible") from None


def _command(history, sql):
    history._connection.execute(sql).close()


@contextmanager
def _snapshot(history):
    _command(history, "BEGIN")
    try:
        yield
        _command(history, "COMMIT")
    except BaseException:
        try:
            _command(history, "ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def read_original_chunk(history, import_id, index):
    """One owner-only bounded snapshot; no writes, backfill or time advancement."""
    corpus._owner(history, import_id)
    _index(index)
    if history._connection.in_transaction:
        raise HistoryError("history_conflict")
    try:
        with _snapshot(history):
            parent = _parent(history, import_id)
            receipt = _receipt(history, import_id, index, parent)
            membership = _membership(history, import_id, index, receipt["chunk_digest"])
            if membership is None:
                raise HistoryError("history_learning_incomplete")
            records, attestations = _material(
                history, import_id, index, membership, receipt, parent)
            return {"records": records, "attestations": attestations, "receipt": receipt}
    except sqlite3.Error:
        raise HistoryError("history_incompatible") from None
