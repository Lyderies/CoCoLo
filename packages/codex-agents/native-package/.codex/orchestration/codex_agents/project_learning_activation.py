"""Which learning corpus a project's own history reads: the cutover record.

One sealed row says this project's learning now lives in its project store, and
names the staged legacy import (if any) that joins the live records. Writing or
removing the row is offline owner work during a drained migration; the running
project service only reads it. The row is not account authority and changes no
launch, capacity or billing decision.

Removing it (rollback) is refused once learning records were written after the
cutover: those may depend on the imported corpus, so going back needs an
explicit reconciliation, never a silent drop.
"""

from __future__ import annotations

import secrets

from . import project_learning_corpus as corpus
from . import project_learning_qa as qa
from . import project_learning_source as archive
from .history_contracts import HistoryError, MAX_SEQUENCE, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest

ACTIVATION_SCHEMA = """
CREATE TABLE project_learning_activation(
 project_id TEXT NOT NULL PRIMARY KEY,import_id TEXT,source_identity_digest TEXT,
 records_manifest_digest TEXT,qa_manifest_digest TEXT,record_count INTEGER NOT NULL,
 qa_count INTEGER NOT NULL,live_sequence INTEGER NOT NULL,activated_at_ms INTEGER NOT NULL,
 activation_seal TEXT NOT NULL,
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
"""
ACTIVATION_COLUMNS = {
    "project_learning_activation": (
        "project_id", "import_id", "source_identity_digest", "records_manifest_digest",
        "qa_manifest_digest", "record_count", "qa_count", "live_sequence", "activated_at_ms",
        "activation_seal"),
}
_FIELDS = ACTIVATION_COLUMNS["project_learning_activation"]
_SEAL_DOMAIN = b"project-learning-activation-v1\0"
# Record types only the learning owner derives or the owner signs; any of them
# written after the cutover may depend on the imported corpus.
DERIVED_TYPES = ("evaluation_case", "candidate", "feedback")


def _key(history):
    history._require_owner()
    if type(history._result_key) is not bytes or len(history._result_key) != 32:
        raise HistoryError("history_invalid_input")
    return history._result_key


def _seal(key, material):
    return keyed_digest(key, _SEAL_DOMAIN + canonical_json(material))


def _stored(history):
    key = _key(history)
    cursor = history._connection.execute(
        "SELECT * FROM project_learning_activation WHERE project_id=?", (history.project_id,))
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None:
        return None
    try:
        if set(row.keys()) != set(_FIELDS):
            raise ValueError()
        value = dict(row)
        imported = value["import_id"] is not None
        digests = ("source_identity_digest", "records_manifest_digest", "qa_manifest_digest")
        if (value["project_id"] != history.project_id
                or (imported and not matches(value["import_id"], _ID))
                or any((value[name] is None) == imported for name in digests)
                or (imported and not all(matches(value[name], _DIGEST) for name in digests))
                or not valid_integer(value["record_count"], maximum=corpus.MAX_IMPORT_RECORDS)
                or not valid_integer(value["qa_count"], maximum=qa.MAX_QA)
                or (not imported and (value["record_count"] or value["qa_count"]))
                or not valid_integer(value["live_sequence"], minimum=1, maximum=MAX_SEQUENCE)
                or not valid_integer(value["activated_at_ms"])
                or not matches(value["activation_seal"], _DIGEST)):
            raise ValueError()
        material = {name: value[name] for name in _FIELDS if name != "activation_seal"}
        if not secrets.compare_digest(value["activation_seal"], _seal(key, material)):
            raise ValueError()
    except Exception:
        raise HistoryError("history_incompatible") from None
    return material


def activation(history):
    """The project's cutover record, or None while its learning is still in the legacy ledger."""
    return _stored(history)


def _offline(history):
    history._require_owner()
    if history._allow_schema_upgrade is not True:
        raise HistoryError("history_not_authorized")
    if history._connection.in_transaction:
        raise HistoryError("history_conflict")


def activate(history, import_id, *, now_ms):
    """Record the cutover. `import_id` names a completely staged import, or is None
    for a project that has no legacy learning to bring. Repeating it is the same
    cutover; a different one conflicts."""
    _offline(history)
    key = _key(history)
    if not valid_integer(now_ms) or (import_id is not None and not matches(import_id, _ID)):
        raise HistoryError("history_invalid_input")
    material = {"project_id": history.project_id, "import_id": import_id, "source_identity_digest": None,
                "records_manifest_digest": None, "qa_manifest_digest": None, "record_count": 0, "qa_count": 0}
    if import_id is not None:
        records, manifest = corpus._row(history, import_id)
        reviews, review_manifest = qa._row(history, import_id)
        image = archive.image_status(history, import_id)
        if (records["state"] != "staged" or reviews["state"] != "staged" or image["state"] != "staged"
                or any(records[name] != manifest[name] for name in (
                    "record_count", "attestation_count", "total_bytes", "chain_digest"))
                or review_manifest["records_manifest_digest"] != records["manifest_digest"]
                or image["source_identity_digest"] != manifest["source_identity_digest"]
                or review_manifest["source_identity_digest"] != manifest["source_identity_digest"]):
            raise HistoryError("history_learning_incomplete")
        material.update(source_identity_digest=manifest["source_identity_digest"],
                        records_manifest_digest=records["manifest_digest"],
                        qa_manifest_digest=reviews["manifest_digest"],
                        record_count=records["record_count"], qa_count=reviews["qa_count"])
    with history._transaction():
        known = _stored(history)
        if known is not None:
            if any(known[name] != material[name] for name in material):
                raise HistoryError("history_conflict")
            return known
        sequence = history._connection.execute(
            "SELECT next_source_sequence FROM history_identity WHERE project_id=?",
            (history.project_id,)).fetchone()
        if sequence is None or not valid_integer(sequence[0], minimum=1, maximum=MAX_SEQUENCE):
            raise HistoryError("history_incompatible")
        material.update(live_sequence=sequence[0], activated_at_ms=now_ms)
        value = {**material, "activation_seal": _seal(key, material)}
        history._connection.execute(
            "INSERT INTO project_learning_activation VALUES(" + ",".join("?" for _ in _FIELDS) + ")",
            tuple(value[name] for name in _FIELDS))
    return material


def written_since(history, record):
    """How many learning records were written after this cutover."""
    placeholders = ",".join("?" for _ in DERIVED_TYPES)
    return history._connection.execute(
        "SELECT COUNT(*) FROM records WHERE project_id=? AND source_sequence>=? "
        f"AND record_type IN ({placeholders})",
        (history.project_id, record["live_sequence"], *DERIVED_TYPES)).fetchone()[0]


def deactivate(history, *, expected):
    """Undo the cutover named by `expected` (its full record). Refused with
    `history_learning_reconciliation` once learning records were written after it."""
    _offline(history)
    with history._transaction():
        known = _stored(history)
        if known is None:
            return False
        if known != expected:
            raise HistoryError("history_conflict")
        if written_since(history, known):
            raise HistoryError("history_learning_reconciliation")
        history._connection.execute(
            "DELETE FROM project_learning_activation WHERE project_id=?", (history.project_id,))
    return True


# Children before parents, so every foreign key holds while an import is removed.
_IMPORT_TABLES = (
    "project_learning_source_chunks", "project_learning_source_images",
    "project_learning_qa_chunks", "project_learning_qa_records", "project_learning_qa_imports",
    "project_learning_chunk_memberships", "project_learning_import_chunks",
    "project_learning_attestations", "project_learning_records", "project_learning_imports",
)


def staged_imports(history):
    """The import IDs present in this store, oldest first."""
    _offline(history)
    return tuple(row[0] for row in history._connection.execute(
        "SELECT CASE WHEN typeof(import_id)='text' AND length(CAST(import_id AS BLOB))<=160 THEN import_id END "
        "FROM project_learning_imports WHERE project_id=? ORDER BY created_at_ms,import_id LIMIT ?",
        (history.project_id, corpus.MAX_IMPORTS + 1)))


def discard_import(history, import_id):
    """Remove one staged import that the cutover does not use (an abandoned attempt)."""
    _offline(history)
    if not matches(import_id, _ID):
        raise HistoryError("history_invalid_input")
    with history._transaction():
        known = _stored(history)
        if known is not None and known["import_id"] == import_id:
            raise HistoryError("history_conflict")
        for table in _IMPORT_TABLES:
            history._connection.execute(
                "DELETE FROM " + table + " WHERE project_id=? AND import_id=?", (history.project_id, import_id))
