"""Bounded learning-record staging used only by a drained migration owner.

The ProjectHistory owner supplies the connection. No normal API exposes these
operations. An import retains canonical records, original metadata and signature
bytes; neither a source label nor a staged signature is authenticated authority.
Even a complete staging receipt is NOT corpus activation, a key-domain proof,
verified QA, an empty-corpus proof or permission to resume a legacy writer.

The separate migration coordinator must prove the drain, full dependency closure,
historical attestations, key-domain equivalence and old-writer retirement before
any live learning operation may use this data. This module never creates/copies
keys, verifies signatures or waits for another service under the project writer.
"""

from __future__ import annotations

import base64
import hashlib

from .contracts import ContractError
from .history_contracts import HistoryError, MAX_SEQUENCE, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import (
    MAX_RECORD_BYTES, SOURCES, canonical_json, parse_bounded_json, validate_learning_record,
)

MAX_IMPORTS = 2
# A busy project's history: a 0.2.x ledger held 192,000 records (312 MB) for one.
MAX_IMPORT_RECORDS = 10_000_000
MAX_IMPORT_BYTES = 16 * 1024 ** 3
MAX_CHUNK_ENTRIES = 64
MAX_CHUNK_BYTES = 262_144
MAX_SIGNATURE_BYTES = 16_384
EMPTY_CHAIN_DIGEST = "0" * 64
_RECORD_TYPES = frozenset({
    "event", "outcome", "feedback", "evaluation_case", "evaluation_assignment",
    "qa_evidence", "evidence_review", "evaluation_trial", "candidate", "comparison",
    "canary_report", "grader_calibration", "owner_grant", "promotion", "learning_export",
    "verification_binding", "host_run_profile", "dispatch_intent", "dispatch_result",
    "observation_unavailable", "provider_usage", "execution_provenance", "host_result_admission",
    # Keep the complete original project partition, including historical
    # account-owned evidence. These are inert source-image bytes: importing
    # them never grants account authority or replaces the live account owner.
    "provider_identity", "provider_headroom", "provider_billing", "billing_attestation",
    "admission_decision", "claim_denial_settlement", "host_open_provenance", "probe_admission",
})
_SIGNED_RECORD_TYPES = frozenset({"feedback", "grader_calibration", "qa_evidence", "billing_attestation"})
_ENTRY_FIELDS = frozenset({
    "content", "source", "source_sequence", "observed_at_ms", "ingested_at_ms", "expires_at_ms", "late",
})
_ATTESTATION_FIELDS = frozenset({"subject_record_id", "signature", "observed_at_ms"})
_MANIFEST_FIELDS = frozenset({
    "schema_version", "scope", "project_id", "source_identity_digest", "key_domain_digest",
    "record_count", "attestation_count", "total_bytes", "chain_digest",
})
_RECEIPT_FIELDS = frozenset({
    "import_id", "manifest_digest", "chunk_index", "chunk_digest", "record_count",
    "attestation_count", "total_bytes", "chain_digest", "learning_ready",
})

LEARNING_SCHEMA = """
CREATE TABLE project_learning_imports(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,manifest_digest TEXT NOT NULL,manifest BLOB NOT NULL,
 next_chunk INTEGER NOT NULL,record_count INTEGER NOT NULL,attestation_count INTEGER NOT NULL,
 total_bytes INTEGER NOT NULL,last_source_sequence INTEGER NOT NULL,chain_digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staging','staged')),created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,import_id),FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE TABLE project_learning_records(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,record_id TEXT NOT NULL,record_type TEXT NOT NULL,
 source TEXT NOT NULL,source_sequence INTEGER NOT NULL,observed_at_ms INTEGER NOT NULL,
 ingested_at_ms INTEGER NOT NULL,expires_at_ms INTEGER,late INTEGER NOT NULL CHECK(late IN (0,1)),
 content BLOB NOT NULL,content_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,import_id,record_id),UNIQUE(project_id,import_id,source_sequence),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_imports(project_id,import_id));
CREATE INDEX project_learning_record_type ON project_learning_records(project_id,import_id,record_type,source_sequence);
CREATE TABLE project_learning_attestations(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,subject_record_id TEXT NOT NULL,
 signature BLOB NOT NULL,signature_digest TEXT NOT NULL,observed_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,import_id,subject_record_id),
 FOREIGN KEY(project_id,import_id,subject_record_id) REFERENCES project_learning_records(project_id,import_id,record_id));
CREATE TABLE project_learning_import_chunks(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,
 chunk_digest TEXT NOT NULL,receipt BLOB NOT NULL,
 PRIMARY KEY(project_id,import_id,chunk_index),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_imports(project_id,import_id));
"""
LEARNING_COLUMNS = {
    "project_learning_imports": ("project_id", "import_id", "manifest_digest", "manifest", "next_chunk",
        "record_count", "attestation_count", "total_bytes", "last_source_sequence", "chain_digest",
        "state", "created_at_ms", "updated_at_ms"),
    "project_learning_records": ("project_id", "import_id", "record_id", "record_type", "source",
        "source_sequence", "observed_at_ms", "ingested_at_ms", "expires_at_ms", "late", "content", "content_digest"),
    "project_learning_attestations": ("project_id", "import_id", "subject_record_id", "signature",
        "signature_digest", "observed_at_ms"),
    "project_learning_import_chunks": ("project_id", "import_id", "chunk_index", "chunk_digest", "receipt"),
}


def _manifest(value, project_id):
    if (type(value) is not dict or set(value) != _MANIFEST_FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["scope"] != "learning-record-staging-v1" or value["project_id"] != project_id
            or not all(matches(value[n], _DIGEST) for n in (
                "source_identity_digest", "key_domain_digest", "chain_digest"))
            or not valid_integer(value["record_count"], maximum=MAX_IMPORT_RECORDS)
            or not valid_integer(value["attestation_count"], maximum=value["record_count"])
            or not valid_integer(value["total_bytes"], maximum=MAX_IMPORT_BYTES)):
        raise HistoryError("history_invalid_input")
    # Capture caller data before any authorization callback or transaction.
    return parse_bounded_json(canonical_json(value))


def _prepare_chunk(records, attestations):
    if (type(records) not in (list, tuple) or type(attestations) not in (list, tuple)
            or not 1 <= len(records) + len(attestations) <= MAX_CHUNK_ENTRIES):
        raise HistoryError("history_invalid_input")
    checked_records, checked_attestations = [], []
    material_records, material_attestations = [], []
    size, previous = 0, 0
    try:
        for entry in records:
            if (type(entry) is not dict or set(entry) != _ENTRY_FIELDS
                    or type(entry["content"]) is not bytes or not 0 < len(entry["content"]) <= MAX_RECORD_BYTES
                    or type(entry["source"]) is not str or entry["source"] not in SOURCES
                    or not valid_integer(entry["source_sequence"], minimum=previous + 1, maximum=MAX_SEQUENCE - 1)
                    or not valid_integer(entry["observed_at_ms"]) or not valid_integer(entry["ingested_at_ms"])
                    or (entry["expires_at_ms"] is not None and not valid_integer(entry["expires_at_ms"]))
                    or type(entry["late"]) is not bool):
                raise HistoryError("history_invalid_input")
            size += len(entry["content"])
            if size > MAX_CHUNK_BYTES:
                raise HistoryError("history_learning_capacity")
            record = parse_bounded_json(entry["content"])
            if (type(record) is not dict or record.get("record_type") not in _RECORD_TYPES
                    or type(record.get("schema_version")) is not int):
                raise HistoryError("history_invalid_input")
            checked = validate_learning_record(record)
            if (canonical_json(checked) != entry["content"]
                    or ("observed_at_ms" in checked and checked["observed_at_ms"] != entry["observed_at_ms"])):
                raise HistoryError("history_invalid_input")
            previous = entry["source_sequence"]
            captured = dict(entry)
            checked_records.append((captured, checked))
            material_records.append({**captured, "content": captured["content"].decode("utf-8")})
        seen = set()
        for entry in attestations:
            if (type(entry) is not dict or set(entry) != _ATTESTATION_FIELDS
                    or not matches(entry["subject_record_id"], _ID)
                    or entry["subject_record_id"] in seen or type(entry["signature"]) is not bytes
                    or not 1 <= len(entry["signature"]) <= MAX_SIGNATURE_BYTES
                    or not valid_integer(entry["observed_at_ms"])):
                raise HistoryError("history_invalid_input")
            seen.add(entry["subject_record_id"])
            size += len(entry["signature"])
            if size > MAX_CHUNK_BYTES:
                raise HistoryError("history_learning_capacity")
            captured = dict(entry)
            checked_attestations.append(captured)
            material_attestations.append({
                "subject_record_id": captured["subject_record_id"], "observed_at_ms": captured["observed_at_ms"],
                "signature_b64": base64.b64encode(captured["signature"]).decode("ascii"),
            })
    except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
        raise HistoryError("history_invalid_input") from None
    digest = hashlib.sha256(b"learning-record-chunk-v1\0" + canonical_json({
        "records": material_records, "attestations": material_attestations,
    })).hexdigest()
    return checked_records, checked_attestations, size, digest


def chunk_digest(records, attestations):
    """Compute the exact bounded source chunk's digest outside the writer."""
    return _prepare_chunk(records, attestations)[3]


def next_chain_digest(previous, digest):
    if not matches(previous, _DIGEST) or not matches(digest, _DIGEST):
        raise HistoryError("history_invalid_input")
    return hashlib.sha256(b"learning-record-chain-v1\0" + bytes.fromhex(previous) + bytes.fromhex(digest)).hexdigest()


def _owner(history, import_id):
    history._require_owner()
    if history._allow_schema_upgrade is not True:
        raise HistoryError("history_not_authorized")
    if not matches(import_id, _ID):
        raise HistoryError("history_invalid_input")


def _authorization(before_commit):
    if not callable(before_commit):
        raise HistoryError("history_invalid_input")
    # Trusted, nonblocking drain/authority recheck, never socket/SQL/signature work.
    before_commit()


def _row(history, import_id):
    cursor = history._connection.execute(
        "SELECT * FROM project_learning_imports WHERE project_id=? AND import_id=?",
        (history.project_id, import_id),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None:
        raise HistoryError("history_not_found")
    try:
        manifest = _manifest(parse_bounded_json(history._stored_blob(row["manifest"], maximum=MAX_RECORD_BYTES)), history.project_id)
        if (hashlib.sha256(canonical_json(manifest)).hexdigest() != row["manifest_digest"]
                or row["state"] not in {"staging", "staged"}
                or not matches(row["chain_digest"], _DIGEST)
                or not valid_integer(row["record_count"], maximum=manifest["record_count"])
                or not valid_integer(row["attestation_count"], maximum=manifest["attestation_count"])
                or not valid_integer(row["total_bytes"], maximum=manifest["total_bytes"])
                or not valid_integer(row["last_source_sequence"], maximum=MAX_SEQUENCE - 1)
                or not valid_integer(row["next_chunk"], maximum=2 * MAX_IMPORT_RECORDS)
                or not valid_integer(row["created_at_ms"])
                or not valid_integer(row["updated_at_ms"], minimum=row["created_at_ms"])):
            raise ValueError()
    except Exception:
        raise HistoryError("history_incompatible") from None
    return dict(row), manifest


def _summary(row):
    return {name: row[name] for name in (
        "import_id", "manifest_digest", "state", "next_chunk", "record_count", "attestation_count", "total_bytes", "chain_digest",
    )} | {"learning_ready": False}


def begin_import(history, import_id, manifest, *, now_ms, before_commit):
    _owner(history, import_id)
    manifest = _manifest(manifest, history.project_id)
    if not valid_integer(now_ms):
        raise HistoryError("history_invalid_input")
    raw = canonical_json(manifest)
    digest = hashlib.sha256(raw).hexdigest()
    _authorization(before_commit)
    with history._transaction():
        known = history._connection.execute("SELECT 1 FROM project_learning_imports WHERE project_id=? AND import_id=?",
            (history.project_id, import_id)).fetchone()
        if known is not None:
            row, _ = _row(history, import_id)
            if row["manifest_digest"] != digest or bytes(row["manifest"]) != raw:
                raise HistoryError("history_conflict")
        else:
            if len(history._connection.execute("SELECT import_id FROM project_learning_imports WHERE project_id=? LIMIT ?",
                    (history.project_id, MAX_IMPORTS)).fetchall()) >= MAX_IMPORTS:
                raise HistoryError("history_learning_capacity")
            history._connection.execute("INSERT INTO project_learning_imports VALUES(?,?,?,?,0,0,0,0,0,?,'staging',?,?)",
                (history.project_id, import_id, digest, raw, EMPTY_CHAIN_DIGEST, now_ms, now_ms))
            row, _ = _row(history, import_id)
        _authorization(before_commit)
        return _summary(row)


def stage_chunk(history, import_id, index, records, attestations, *, now_ms, before_commit):
    _owner(history, import_id)
    if not valid_integer(index, maximum=2 * MAX_IMPORT_RECORDS - 1) or not valid_integer(now_ms):
        raise HistoryError("history_invalid_input")
    records, attestations, size, digest = _prepare_chunk(records, attestations)
    _authorization(before_commit)
    with history._transaction():
        row, manifest = _row(history, import_id)
        known = history._connection.execute(
            "SELECT chunk_digest,receipt FROM project_learning_import_chunks WHERE project_id=? AND import_id=? AND chunk_index=?",
            (history.project_id, import_id, index),
        ).fetchone()
        if known is not None:
            if known["chunk_digest"] != digest:
                raise HistoryError("history_conflict")
            raw = history._stored_blob(known["receipt"], maximum=16_384)
            try:
                value = parse_bounded_json(raw)
            except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
                raise HistoryError("history_incompatible") from None
            if (type(value) is not dict or set(value) != _RECEIPT_FIELDS or canonical_json(value) != raw
                    or value.get("manifest_digest") != row["manifest_digest"] or value.get("chunk_digest") != digest
                    or value.get("chunk_index") != index or value.get("import_id") != import_id
                    or not valid_integer(value.get("chunk_index"), maximum=row["next_chunk"] - 1)
                    or not matches(value.get("chain_digest"), _DIGEST)
                    or not valid_integer(value.get("record_count"), maximum=row["record_count"])
                    or not valid_integer(value.get("attestation_count"), maximum=row["attestation_count"])
                    or not valid_integer(value.get("total_bytes"), maximum=row["total_bytes"])
                    or (index == row["next_chunk"] - 1 and any(value[n] != row[n] for n in (
                        "record_count", "attestation_count", "total_bytes", "chain_digest")))
                    or value.get("learning_ready") is not False):
                raise HistoryError("history_incompatible")
            from .project_learning_chunks import record_chunk_membership
            record_chunk_membership(history, import_id, index, records, attestations, digest)
            _authorization(before_commit)
            return value
        if row["state"] != "staging" or row["next_chunk"] != index or now_ms < row["updated_at_ms"]:
            raise HistoryError("history_conflict")
        if records and records[0][0]["source_sequence"] <= row["last_source_sequence"]:
            raise HistoryError("history_conflict")
        counts = {"record_count": row["record_count"] + len(records),
                  "attestation_count": row["attestation_count"] + len(attestations), "total_bytes": row["total_bytes"] + size}
        if any(counts[n] > manifest[n] for n in counts):
            raise HistoryError("history_learning_capacity")
        for entry, record in records:
            history._connection.execute("INSERT INTO project_learning_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                history.project_id, import_id, record["record_id"], record["record_type"], entry["source"],
                entry["source_sequence"], entry["observed_at_ms"], entry["ingested_at_ms"], entry["expires_at_ms"],
                int(entry["late"]), entry["content"], hashlib.sha256(entry["content"]).hexdigest(),
            ))
        for entry in attestations:
            source = history._connection.execute(
                "SELECT record_type FROM project_learning_records WHERE project_id=? AND import_id=? AND record_id=?",
                (history.project_id, import_id, entry["subject_record_id"]),
            ).fetchone()
            if source is None or source[0] not in _SIGNED_RECORD_TYPES:
                raise HistoryError("history_conflict")
            history._connection.execute("INSERT INTO project_learning_attestations VALUES(?,?,?,?,?,?)", (
                history.project_id, import_id, entry["subject_record_id"], entry["signature"],
                hashlib.sha256(entry["signature"]).hexdigest(), entry["observed_at_ms"],
            ))
        chain = next_chain_digest(row["chain_digest"], digest)
        last = records[-1][0]["source_sequence"] if records else row["last_source_sequence"]
        history._connection.execute("UPDATE project_learning_imports SET next_chunk=?,record_count=?,attestation_count=?,"
            "total_bytes=?,last_source_sequence=?,chain_digest=?,updated_at_ms=? WHERE project_id=? AND import_id=?",
            (index + 1, counts["record_count"], counts["attestation_count"], counts["total_bytes"], last, chain, now_ms,
             history.project_id, import_id))
        receipt = {"import_id": import_id, "manifest_digest": row["manifest_digest"], "chunk_index": index,
                   "chunk_digest": digest, **counts, "chain_digest": chain, "learning_ready": False}
        history._connection.execute("INSERT INTO project_learning_import_chunks VALUES(?,?,?,?,?)",
            (history.project_id, import_id, index, digest, canonical_json(receipt)))
        from .project_learning_chunks import record_chunk_membership
        record_chunk_membership(history, import_id, index, records, attestations, digest)
        _authorization(before_commit)
        return receipt


def finish_import(history, import_id, *, now_ms, before_commit):
    _owner(history, import_id)
    if not valid_integer(now_ms):
        raise HistoryError("history_invalid_input")
    _authorization(before_commit)
    with history._transaction():
        row, manifest = _row(history, import_id)
        if any(row[n] != manifest[n] for n in ("record_count", "attestation_count", "total_bytes", "chain_digest")):
            raise HistoryError("history_learning_incomplete")
        if row["state"] != "staged":
            if now_ms < row["updated_at_ms"]:
                raise HistoryError("history_conflict")
            history._connection.execute("UPDATE project_learning_imports SET state='staged',updated_at_ms=? "
                "WHERE project_id=? AND import_id=?", (now_ms, history.project_id, import_id))
            row["state"] = "staged"
        _authorization(before_commit)
        return _summary(row)


def import_records(history, import_id, *, after_source_sequence=0, limit=32):
    _owner(history, import_id)
    if not valid_integer(after_source_sequence) or not valid_integer(limit, minimum=1, maximum=MAX_CHUNK_ENTRIES):
        raise HistoryError("history_invalid_input")
    _row(history, import_id)
    cursor = history._connection.execute("SELECT * FROM project_learning_records WHERE project_id=? AND import_id=? "
        "AND source_sequence>? ORDER BY source_sequence LIMIT ?", (history.project_id, import_id, after_source_sequence, limit))
    result, size = [], 0
    for row in cursor:
        content = history._stored_blob(row["content"], maximum=MAX_RECORD_BYTES)
        if size + len(content) > MAX_CHUNK_BYTES:
            break
        entry = {name: row[name] for name in _ENTRY_FIELDS - {"content", "late"}}
        entry.update(content=content, late=bool(row["late"]))
        try:
            _, record = _prepare_chunk([entry], [])[0][0]
            if (row["late"] not in (0, 1) or type(row["late"]) is not int
                    or row["record_id"] != record["record_id"] or row["record_type"] != record["record_type"]
                    or row["content_digest"] != hashlib.sha256(content).hexdigest()):
                raise ValueError()
        except Exception:
            raise HistoryError("history_incompatible") from None
        result.append(entry)
        size += len(content)
    return tuple(result)


def import_attestations(history, import_id, *, after_subject_record_id="", limit=32):
    _owner(history, import_id)
    if (not (after_subject_record_id == "" or matches(after_subject_record_id, _ID))
            or not valid_integer(limit, minimum=1, maximum=MAX_CHUNK_ENTRIES)):
        raise HistoryError("history_invalid_input")
    _row(history, import_id)
    cursor = history._connection.execute("SELECT * FROM project_learning_attestations WHERE project_id=? AND import_id=? "
        "AND subject_record_id>? ORDER BY subject_record_id LIMIT ?", (history.project_id, import_id, after_subject_record_id, limit))
    result, size = [], 0
    for row in cursor:
        signature = history._stored_blob(row["signature"], maximum=MAX_SIGNATURE_BYTES)
        if size + len(signature) > MAX_CHUNK_BYTES:
            break
        entry = {"subject_record_id": row["subject_record_id"], "signature": signature,
                 "observed_at_ms": row["observed_at_ms"]}
        try:
            _prepare_chunk([], [entry])
            if row["signature_digest"] != hashlib.sha256(signature).hexdigest():
                raise ValueError()
        except Exception:
            raise HistoryError("history_incompatible") from None
        result.append(entry)
        size += len(signature)
    return tuple(result)
