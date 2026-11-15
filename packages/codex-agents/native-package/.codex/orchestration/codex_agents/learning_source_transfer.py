"""Offline transfer from the actual original owner into inert project staging.

Preserve the full source-row image, not just learning subsets. The original
writer, native provenance, private artifacts and account restrictions are not
retired, adopted or activated here. No normal client endpoint exposes this path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from . import project_learning_corpus as corpus
from . import project_learning_qa as qa
from . import project_learning_source as archive
from .history_contracts import HistoryError, _ID, matches
from .learning_contracts import content_digest


def _groups(items, size, *, maximum=64, byte_limit=262_144):
    group, used = [], 0
    for item in items:
        length = size(item)
        if length > byte_limit:
            raise HistoryError("history_learning_capacity")
        if group and (len(group) == maximum or used + length > byte_limit):
            yield group
            group, used = [], 0
        group.append(item)
        used += length
    if group:
        yield group


def _material(image):
    from .learning_store import _backup_decode_value as decode

    body = json.loads(image["body"])
    records = [{name: decode(row[name]) for name in corpus._ENTRY_FIELDS - {"late"}} | {"late": bool(row["late"])}
               for row in sorted(body["tables"]["records"], key=lambda row: row["source_sequence"])]
    signatures = [{"subject_record_id": row["subject_record_id"], "signature": decode(row["signature"]),
                   "observed_at_ms": row["attested_at_ms"]} for row in body["tables"]["owner_attestations"]]
    rows = [{name: decode(row[name]) for name in qa._FIELDS}
            for row in sorted(body["tables"]["operational_qa"], key=lambda row: row["qa_id"])]
    if (len(records) > corpus.MAX_IMPORT_RECORDS or len(signatures) > len(records)
            or len(rows) > qa.MAX_QA):
        raise HistoryError("history_learning_capacity")
    return records, signatures, rows


def _record_groups(records, signatures):
    for group in _groups(records, lambda row: len(row["content"])):
        yield group, []
    for group in _groups(signatures, lambda row: len(row["signature"])):
        yield [], group


def _transfer(ledger, history, project_id, file_id, import_id, work_limits):
    from .learning_store import LearningLedger
    from .learning_source_image import capture_source_image
    from .project_history import ProjectHistory

    if type(ledger) is not LearningLedger or type(history) is not ProjectHistory:
        raise HistoryError("history_wrong_owner")
    history._require_owner()
    if history.project_id != project_id or not matches(import_id, _ID):
        raise HistoryError("history_invalid_input")
    if history._allow_schema_upgrade is not True:
        raise HistoryError("history_not_authorized")
    if history._connection.in_transaction:
        raise HistoryError("history_conflict")
    with ledger._lock:
        domain = history.learning_key_domain_digest()
        image = capture_source_image(ledger, project_id, file_id,
            expected_domain_digest=domain, work_limits=work_limits)
        if len(image["body"]) > archive.MAX_BYTES:
            raise HistoryError("history_learning_capacity")
        changes = ledger._connection.total_changes

        def guard():
            # Nonblocking actual-owner checks only. No source SQL, key/file I/O,
            # signature process or second store transaction under target writes.
            history._require_owner()
            if (ledger._connection_origin != (os.getpid(), threading.current_thread())
                    or ledger._collector_active is not False or ledger._active_readers != 0
                    or ledger._connection.total_changes != changes):
                raise HistoryError("history_conflict")

        records, signatures, rows = _material(image)
        chain = corpus.EMPTY_CHAIN_DIGEST
        for batch, attestations in _record_groups(records, signatures):
            chain = corpus.next_chain_digest(chain, corpus.chunk_digest(batch, attestations))
        manifest = {"schema_version": 1, "scope": "learning-record-staging-v1", "project_id": project_id,
            "source_identity_digest": image["source_identity_digest"], "key_domain_digest": domain,
            "record_count": len(records), "attestation_count": len(signatures),
            "total_bytes": sum(len(row["content"]) for row in records) + sum(len(row["signature"]) for row in signatures),
            "chain_digest": chain}
        now = ledger._clock_ms()
        guard()
        history.begin_learning_import(import_id, manifest, now_ms=now, before_commit=guard)
        metadata = {name: image[name] for name in archive._FIELDS - {"total_bytes"}}
        metadata["total_bytes"] = len(image["body"])
        archive.begin_image(history, import_id, metadata)
        for index, offset in enumerate(range(0, len(image["body"]), archive.CHUNK_BYTES)):
            guard()
            archive.stage_image_chunk(history, import_id, index, image["body"][offset:offset + archive.CHUNK_BYTES])
        for index, (batch, attestations) in enumerate(_record_groups(records, signatures)):
            history.stage_learning_import_chunk(import_id, index, batch, attestations,
                                                 now_ms=now, before_commit=guard)
        record_receipt = history.finish_learning_import(import_id, now_ms=now, before_commit=guard)
        chain = qa.ZERO
        for batch in _groups(rows, lambda row: len(row["content"])):
            chain = qa.qa_chain_digest(chain, qa.qa_chunk_digest(batch))
        qa_manifest = {"schema_version": 1, "scope": "learning-operational-qa-staging-v1", "project_id": project_id,
            "records_manifest_digest": content_digest(manifest), "source_identity_digest": image["source_identity_digest"],
            "key_domain_digest": domain, "qa_count": len(rows),
            "total_bytes": sum(len(row["content"]) for row in rows), "chain_digest": chain}
        history.begin_learning_qa_import(import_id, qa_manifest, now_ms=now, before_commit=guard)
        for index, batch in enumerate(_groups(rows, lambda row: len(row["content"]))):
            history.stage_learning_qa_import_chunk(import_id, index, batch, now_ms=now, before_commit=guard)
        qa_receipt = history.finish_learning_qa_import(import_id, now_ms=now, before_commit=guard)
        guard()
        # A partial crash/revocation leaves only bounded inert staging. Exact
        # replay recovers the same bytes; a changed source or import conflicts.
        current = capture_source_image(ledger, project_id, file_id,
            expected_domain_digest=history.learning_key_domain_digest(), work_limits=work_limits)
        if image != current:
            raise HistoryError("history_conflict")
        source_receipt = archive.finish_image(history, import_id)
        guard()
        return {"project_id": project_id, "import_id": import_id,
            "source_identity_digest": image["source_identity_digest"], "counts": image["counts"],
            "source": source_receipt, "records": record_receipt, "qa": qa_receipt,
            "learning_ready": False}


def stage_source(ledger, history, project_id, file_id, *, import_id, work_limits):
    """Transfer an actual current image, without any live-corpus authority."""
    reason = "history_unavailable"
    try:
        return _transfer(ledger, history, project_id, file_id, import_id, work_limits)
    except HistoryError as error:
        reason = error.reason_code
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    raise HistoryError(reason)
