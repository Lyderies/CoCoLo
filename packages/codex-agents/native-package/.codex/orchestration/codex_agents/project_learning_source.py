"""Inert full original-row archive, never migration or learning authority.

Only an explicit offline ProjectHistory owner uses these bounded operations.
The coordinator obtains the image from the actual legacy owner. A stored digest,
body, state or caller manifest alone proves no native/execution/check provenance,
retained private artifacts, cutover, writer retirement or activation.
"""
from __future__ import annotations

import hashlib

from . import project_learning_corpus as corpus
from .history_contracts import HistoryError, _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, parse_bounded_json

CHUNK_BYTES = 65_536
MAX_BYTES = 128 * 1_048_576
MAX_CHUNKS = MAX_BYTES // CHUNK_BYTES
_FIELDS = frozenset({"project_id", "ledger_schema_version", "backup_digest", "manifest_digest",
                     "key_domain_digest", "source_identity_digest", "total_bytes"})
SOURCE_SCHEMA = """
CREATE TABLE project_learning_source_images(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,metadata BLOB NOT NULL,
 next_chunk INTEGER NOT NULL,total_bytes INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staging','staged')),
 PRIMARY KEY(project_id,import_id),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_imports(project_id,import_id));
CREATE TABLE project_learning_source_chunks(
 project_id TEXT NOT NULL,import_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,
 content BLOB NOT NULL,content_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,import_id,chunk_index),
 FOREIGN KEY(project_id,import_id) REFERENCES project_learning_source_images(project_id,import_id));
"""
SOURCE_COLUMNS = {
    "project_learning_source_images": ("project_id", "import_id", "metadata", "next_chunk", "total_bytes", "state"),
    "project_learning_source_chunks": ("project_id", "import_id", "chunk_index", "content", "content_digest"),
}


def _one(history, sql, parameters):
    cursor = history._connection.execute(sql, parameters)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _access(history, import_id):
    corpus._owner(history, import_id)
    if history._connection.in_transaction:
        raise HistoryError("history_conflict")


def _metadata(value, project_id):
    if (type(value) is not dict or set(value) != _FIELDS or type(value["project_id"]) is not str
            or value["project_id"] != project_id
            or type(value["ledger_schema_version"]) is not int or value["ledger_schema_version"] != 13
            or not valid_integer(value["total_bytes"], minimum=1, maximum=MAX_BYTES)
            or not all(matches(value[name], _DIGEST) for name in _FIELDS - {
                "project_id", "ledger_schema_version", "total_bytes"})):
        raise HistoryError("history_invalid_input")
    return dict(value)


def _parent(history, import_id):
    row = _one(history,
        "SELECT CASE WHEN typeof(metadata)='blob' AND length(metadata) BETWEEN 1 AND 4096 "
        "THEN metadata END,next_chunk,total_bytes,state FROM project_learning_source_images "
        "WHERE project_id=? AND import_id=?", (history.project_id, import_id))
    if row is None:
        raise HistoryError("history_not_found")
    try:
        raw = row[0]
        if type(raw) is not bytes:
            raise ValueError()
        metadata = _metadata(parse_bounded_json(raw), history.project_id)
        count = (metadata["total_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
        if (canonical_json(metadata) != raw or not valid_integer(row[1], maximum=count)
                or not valid_integer(row[2], maximum=metadata["total_bytes"])
                or row[2] != min(row[1] * CHUNK_BYTES, metadata["total_bytes"])
                or row[3] not in {"staging", "staged"}
                or (row[3] == "staged" and row[2] != metadata["total_bytes"])):
            raise ValueError()
        parent, manifest = corpus._row(history, import_id)
        if any(metadata[name] != manifest[name] for name in ("source_identity_digest", "key_domain_digest")):
            raise ValueError()
        return metadata, row[1], row[2], row[3]
    except Exception:
        raise HistoryError("history_incompatible") from None


def begin_image(history, import_id, metadata):
    _access(history, import_id)
    metadata = _metadata(metadata, history.project_id)
    raw = canonical_json(metadata)
    with history._transaction():
        parent, manifest = corpus._row(history, import_id)
        if any(metadata[name] != manifest[name] for name in ("source_identity_digest", "key_domain_digest")):
            raise HistoryError("history_conflict")
        row = _one(history, "SELECT 1 FROM project_learning_source_images WHERE project_id=? AND import_id=?",
                   (history.project_id, import_id))
        if row is None:
            history._connection.execute("INSERT INTO project_learning_source_images VALUES(?,?,?,0,0,'staging')",
                                        (history.project_id, import_id, raw)).close()
        elif _parent(history, import_id)[0] != metadata:
            raise HistoryError("history_conflict")
    return image_status(history, import_id)


def stage_image_chunk(history, import_id, index, raw):
    _access(history, import_id)
    if (not valid_integer(index, maximum=MAX_CHUNKS - 1) or type(raw) is not bytes
            or not 0 < len(raw) <= CHUNK_BYTES):
        raise HistoryError("history_invalid_input")
    digest = hashlib.sha256(raw).hexdigest()
    with history._transaction():
        metadata, next_chunk, size, state = _parent(history, import_id)
        expected_size = min(CHUNK_BYTES, metadata["total_bytes"] - index * CHUNK_BYTES)
        if len(raw) != expected_size:
            raise HistoryError("history_conflict")
        if index < next_chunk:
            if _read_chunk(history, import_id, index) != raw:
                raise HistoryError("history_conflict")
        else:
            if index != next_chunk or state != "staging":
                raise HistoryError("history_conflict")
            history._connection.execute("INSERT INTO project_learning_source_chunks VALUES(?,?,?,?,?)",
                (history.project_id, import_id, index, raw, digest)).close()
            history._connection.execute("UPDATE project_learning_source_images SET next_chunk=?,total_bytes=? "
                "WHERE project_id=? AND import_id=?", (next_chunk + 1, size + len(raw), history.project_id, import_id)).close()
    return image_status(history, import_id)


def _read_chunk(history, import_id, index):
    corpus._owner(history, import_id)
    metadata, count, size, state = _parent(history, import_id)
    if not valid_integer(index, maximum=MAX_CHUNKS - 1):
        raise HistoryError("history_invalid_input")
    if index >= count:
        raise HistoryError("history_not_found")
    row = _one(history,
        "SELECT CASE WHEN typeof(content)='blob' AND length(content) BETWEEN 1 AND ? THEN content END,"
        "CASE WHEN typeof(content_digest)='text' AND length(content_digest)=64 THEN content_digest END "
        "FROM project_learning_source_chunks WHERE project_id=? AND import_id=? AND chunk_index=?",
        (CHUNK_BYTES, history.project_id, import_id, index))
    if (row is None or type(row[0]) is not bytes or not matches(row[1], _DIGEST)
            or len(row[0]) != min(CHUNK_BYTES, metadata["total_bytes"] - index * CHUNK_BYTES)
            or hashlib.sha256(row[0]).hexdigest() != row[1]):
        raise HistoryError("history_incompatible")
    return row[0]


def read_image_chunk(history, import_id, index):
    _access(history, import_id)
    return _read_chunk(history, import_id, index)


def finish_image(history, import_id):
    _access(history, import_id)
    metadata, count, size, state = _parent(history, import_id)
    if size != metadata["total_bytes"]:
        raise HistoryError("history_learning_incomplete")
    # No transaction spans hashing the complete archive. Supported operations
    # never alter original chunks, and parent state is rechecked at the commit.
    digest = hashlib.sha256()
    for index in range(count):
        digest.update(read_image_chunk(history, import_id, index))
    if digest.hexdigest() != metadata["manifest_digest"]:
        raise HistoryError("history_conflict")
    if state == "staged":
        return image_status(history, import_id)
    with history._transaction():
        if _parent(history, import_id) != (metadata, count, size, state):
            raise HistoryError("history_conflict")
        history._connection.execute("UPDATE project_learning_source_images SET state='staged' "
            "WHERE project_id=? AND import_id=?", (history.project_id, import_id)).close()
    return image_status(history, import_id)


def image_status(history, import_id):
    _access(history, import_id)
    metadata, count, size, state = _parent(history, import_id)
    return {**metadata, "import_id": import_id, "next_chunk": count,
            "stored_bytes": size, "state": state, "learning_ready": False}
