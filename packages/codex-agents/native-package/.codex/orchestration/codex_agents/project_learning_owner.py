"""The owner's own changes to a project's learning corpus, on the project store.

Feedback (the owner rejects or corrects a run) and case review (a development
case enters a controlled corpus) were ledger commands. For a project whose
learning lives in its own store they are these two writes, made by the
project's persistence owner. Feedback is authorised by the owner's detached
signature, verified by the service adapter before this is called and kept
beside the record; a review is checked with the ledger's own corpus rules
(`learning_eligibility.check_evaluation_case`).

The corpus is the project's live records and its activated legacy import, as
for the learning cycle. Nothing here launches, admits or releases anything.
"""

from __future__ import annotations

import hashlib

from .contracts import ContractError
from .history_contracts import HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, parse_bounded_json, validate_learning_record
from .learning_eligibility import EligibilityError, check_evaluation_case
from .learning_engine import review_evaluation_case
from .project_learning_activation import activation
from .project_learning_corpus import MAX_SIGNATURE_BYTES

MAX_CASES = 20_000
MAX_FEEDBACK = 20_000
OWNER_REASONS = frozenset({
    "feedback_run_unknown", "case_unknown", "case_review_refused", "owner_record_conflict",
})


class OwnerRecordRefused(HistoryError):
    """A well-formed owner request the corpus rules refuse; `detail` is a fixed rule text."""

    def __init__(self, reason, detail=None):
        super().__init__("history_conflict")
        self.owner_reason, self.detail = reason, detail


def _import_id(history):
    cutover = activation(history)
    return None if cutover is None else cutover["import_id"]


def _parsed(history, content):
    return parse_bounded_json(history._stored_blob(content, maximum=MAX_RECORD_BYTES))


def corpus_record(history, record_id, kind):
    """One record of this kind by identity: a live one, else one of the activated import."""
    row = history._connection.execute(
        "SELECT content FROM records WHERE project_id=? AND record_id=? AND record_type=?",
        (history.project_id, record_id, kind)).fetchone()
    imported = _import_id(history)
    if row is None and imported is not None:
        row = history._connection.execute(
            "SELECT content FROM project_learning_records WHERE project_id=? AND import_id=? "
            "AND record_id=? AND record_type=?", (history.project_id, imported, record_id, kind)).fetchone()
    return None if row is None else _parsed(history, row[0])


def corpus_records(history, kind, limit):
    """Every record of this kind, imported ones first; refuses rather than truncates."""
    rows, imported = [], _import_id(history)
    if imported is not None:
        rows = history._connection.execute(
            "SELECT content FROM project_learning_records WHERE project_id=? AND import_id=? "
            "AND record_type=? ORDER BY source_sequence LIMIT ?",
            (history.project_id, imported, kind, limit + 1)).fetchall()
    rows = [*rows, *history._connection.execute(
        "SELECT content FROM records WHERE project_id=? AND record_type=? ORDER BY source_sequence LIMIT ?",
        (history.project_id, kind, limit + 1 - len(rows) if len(rows) <= limit else 1))]
    if len(rows) > limit:
        raise HistoryError("history_learning_capacity")
    seen, records = set(), []
    for (content,) in rows:
        record = _parsed(history, content)
        if record["record_id"] not in seen:
            seen.add(record["record_id"])
            records.append(record)
    return records


def rejected_run_ids(history):
    """Runs the owner rejected or corrected."""
    return frozenset(record["run_id"] for record in corpus_records(history, "feedback", MAX_FEEDBACK)
                     if record["label"] in {"rejected", "corrected"})


def _run_known(history, run_id):
    statement = ("SELECT 1 FROM {table} WHERE project_id=? {scope}AND record_type='outcome' "
                 "AND json_extract(CAST(content AS TEXT),'$.run_id')=? LIMIT 1")
    if history._connection.execute(statement.format(table="records", scope=""),
                                   (history.project_id, run_id)).fetchone() is not None:
        return True
    imported = _import_id(history)
    return imported is not None and history._connection.execute(
        statement.format(table="project_learning_records", scope="AND import_id=? "),
        (history.project_id, imported, run_id)).fetchone() is not None


def _append(history, checked, content, source, now_ms):
    """Idempotent append of a learning record no client may write; True when it was new."""
    existing = history._connection.execute(
        "SELECT content,source FROM records WHERE project_id=? AND record_id=?",
        (history.project_id, checked["record_id"])).fetchone()
    if existing is not None:
        if existing[0] != content or existing[1] != source:
            raise OwnerRecordRefused("owner_record_conflict")
        return False
    stamped = {**checked, "observed_at_ms": checked.get("observed_at_ms", now_ms)}
    history._append_locked(stamped, content, source, now_ms)
    return True


def record_feedback(history, record, signature, *, now_ms):
    """Store the owner's signed feedback. The caller has verified the signature."""
    history._require_owner()
    if (type(signature) is not bytes or not 0 < len(signature) <= MAX_SIGNATURE_BYTES
            or not valid_integer(now_ms)):
        raise HistoryError("history_invalid_input")
    try:
        checked = validate_learning_record(record)
        content = canonical_json(checked)
    except (ContractError, TypeError, ValueError, RecursionError):
        raise HistoryError("history_invalid_input") from None
    if checked["record_type"] != "feedback" or len(content) > MAX_RECORD_BYTES:
        raise HistoryError("history_invalid_input")
    digest = hashlib.sha256(signature).hexdigest()
    with history._transaction():
        if not _run_known(history, checked["run_id"]):
            raise OwnerRecordRefused("feedback_run_unknown")
        created = _append(history, checked, content, "owner", now_ms)
        kept = history._connection.execute(
            "SELECT signature_digest FROM project_owner_attestations WHERE project_id=? AND subject_record_id=?",
            (history.project_id, checked["record_id"])).fetchone()
        if kept is None:
            history._connection.execute("INSERT INTO project_owner_attestations VALUES(?,?,?,?,?)", (
                history.project_id, checked["record_id"], signature, digest, now_ms))
        elif kept[0] != digest:
            raise OwnerRecordRefused("owner_record_conflict")
    return {"stored": created, "record_id": checked["record_id"],
            "content_digest": hashlib.sha256(content).hexdigest()}


def review_case(history, case_id, *, review_digest, similarity_group_digest, destination, freeze, now_ms):
    """Move one development case into a controlled corpus, by the ledger's rules."""
    from .project_learning_identity import _key

    key = _key(history)
    if (not matches(case_id, _ID) or not matches(review_digest, _DIGEST)
            or not matches(similarity_group_digest, _DIGEST) or type(destination) is not str
            or type(freeze) is not bool or not valid_integer(now_ms)):
        raise HistoryError("history_invalid_input")
    with history._transaction():
        source_case = corpus_record(history, case_id, "evaluation_case")
        if source_case is None:
            raise OwnerRecordRefused("case_unknown")
        try:
            reviewed = review_evaluation_case(
                source_case, digest_key=key, review_digest=review_digest,
                similarity_group_digest=similarity_group_digest, destination=destination, freeze=freeze)
            content = canonical_json(reviewed)
        except ContractError as error:
            raise OwnerRecordRefused("case_review_refused", str(error)) from None
        related = [case for case in corpus_records(history, "evaluation_case", MAX_CASES)
                   if case["record_id"] != case_id]
        try:
            check_evaluation_case(
                reviewed, source_outcome=corpus_record(history, reviewed["source_outcome_id"], "outcome"),
                previous_case=source_case, related_cases=related, invalidated_run_ids=rejected_run_ids(history))
        except EligibilityError as error:
            raise OwnerRecordRefused("case_review_refused", str(error)) from None
        created = _append(history, reviewed, content, "evaluator", now_ms)
    return {"stored": created, "record": reviewed}
