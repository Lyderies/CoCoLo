"""Offline transfer from the actual original owner into inert project staging.

Preserve the full source-row image, not just learning subsets. The original
writer, native provenance, private artifacts and account restrictions are not
retired, adopted or activated here. No normal client endpoint exposes this path.
"""
from __future__ import annotations

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


class _Material:
    """The image's records, signatures and QA rows, read again from its backup for each pass.

    Each pass streams one table in a fixed order (records by source sequence,
    signatures by subject, QA by identifier), so nothing holds a whole table.
    """

    def __init__(self, image):
        from .learning_source_image import archive_rows

        self._rows, self._image = archive_rows, image
        counts = dict(image["counts"])
        self.record_count, self.attestation_count = counts["records"], counts["owner_attestations"]
        self.qa_count = counts["operational_qa"]
        if (self.record_count > corpus.MAX_IMPORT_RECORDS or self.attestation_count > self.record_count
                or self.qa_count > qa.MAX_QA):
            raise HistoryError("history_learning_capacity")

    def _read(self, table, columns, order):
        return self._rows(self._image["archive"], self._image["project_id"], table, columns, order)

    def records(self):
        names = ("content", "source", "source_sequence", "observed_at_ms", "ingested_at_ms", "expires_at_ms", "late")
        for row in self._read("records", names, "source_sequence"):
            entry = dict(zip(names, row))
            yield {**entry, "late": bool(entry["late"])}

    def signatures(self):
        names = ("subject_record_id", "signature", "attested_at_ms")
        for subject, signature, attested in self._read("owner_attestations", names, "subject_record_id"):
            yield {"subject_record_id": subject, "signature": signature, "observed_at_ms": attested}

    def qa_rows(self):
        for row in self._read("operational_qa", qa._FIELDS, "qa_id"):
            yield dict(zip(qa._FIELDS, row))


def _record_groups(material):
    for group in _groups(material.records(), lambda row: len(row["content"])):
        yield group, []
    for group in _groups(material.signatures(), lambda row: len(row["signature"])):
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
        if image["archive"].size > archive.MAX_BYTES:
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

        material = _Material(image)
        chain, total = corpus.EMPTY_CHAIN_DIGEST, 0
        for batch, attestations in _record_groups(material):
            chain = corpus.next_chain_digest(chain, corpus.chunk_digest(batch, attestations))
            total += sum(len(row["content"]) for row in batch) + sum(len(row["signature"]) for row in attestations)
        manifest = {"schema_version": 1, "scope": "learning-record-staging-v1", "project_id": project_id,
            "source_identity_digest": image["source_identity_digest"], "key_domain_digest": domain,
            "record_count": material.record_count, "attestation_count": material.attestation_count,
            "total_bytes": total, "chain_digest": chain}
        now = ledger._clock_ms()
        guard()
        history.begin_learning_import(import_id, manifest, now_ms=now, before_commit=guard)
        metadata = {name: image[name] for name in archive._FIELDS - {"total_bytes"}}
        metadata["total_bytes"] = image["archive"].size
        archive.begin_image(history, import_id, metadata)
        from .learning_source_image import archive_chunks
        for index, chunk in enumerate(archive_chunks(image["archive"], archive.CHUNK_BYTES)):
            guard()
            archive.stage_image_chunk(history, import_id, index, chunk)
        for index, (batch, attestations) in enumerate(_record_groups(material)):
            history.stage_learning_import_chunk(import_id, index, batch, attestations,
                                                 now_ms=now, before_commit=guard)
        record_receipt = history.finish_learning_import(import_id, now_ms=now, before_commit=guard)
        chain, total = qa.ZERO, 0
        for batch in _groups(material.qa_rows(), lambda row: len(row["content"])):
            chain = qa.qa_chain_digest(chain, qa.qa_chunk_digest(batch))
            total += sum(len(row["content"]) for row in batch)
        qa_manifest = {"schema_version": 1, "scope": "learning-operational-qa-staging-v1", "project_id": project_id,
            "records_manifest_digest": content_digest(manifest), "source_identity_digest": image["source_identity_digest"],
            "key_domain_digest": domain, "qa_count": material.qa_count,
            "total_bytes": total, "chain_digest": chain}
        history.begin_learning_qa_import(import_id, qa_manifest, now_ms=now, before_commit=guard)
        for index, batch in enumerate(_groups(material.qa_rows(), lambda row: len(row["content"]))):
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
