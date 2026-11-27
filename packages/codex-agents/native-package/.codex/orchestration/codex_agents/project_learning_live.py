"""Live learning for one project, run by that project's own persistence owner.

After a result or its QA is recorded, the project's service derives evaluation
cases from verified outcomes, analyses every outcome and proposes inert
model/effort candidates: the calculation the legacy ledger ran under its writer
lock, now on the project's own history.

Every SQL statement runs in a short call on the store's owner thread. Parsing,
validation and the calculation (`learning_cycle_core`, which never sees a key)
run on one worker thread and stop themselves at the cycle's deadline. A cycle
advances one step each time a request for it is handled, so nothing waits on the
owner thread. Derived records get their keyed identities from
`project_learning_identity` on the owner thread and are appended idempotently.

A cycle is a pure function of the durable corpus: one that fails, is abandoned
or loses its host leaves nothing half-written that the next cycle does not
repeat. Its report describes what this cycle added and never claims more.
The corpus is this project's live records, preceded by its imported legacy
records once `project_learning_activation` says they were brought over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from .contracts import ContractError
from .history_contracts import HistoryError, _DIGEST, matches, valid_integer
from .learning_contracts import (
    MAX_RECORD_BYTES, canonical_json, content_digest, parse_bounded_json, validate_learning_record,
)
from .learning_cycle_core import (
    LEARNING_RETENTION_MS, MAX_CASES, MAX_OUTCOMES, MAX_SNAPSHOT_BYTES, LearningCycleBoundsError,
    plan_learning_cycle,
    select_mining_outcomes,
)
from .learning_eligibility import EligibilityError, check_candidate_eligibility, outcome_lineage
from .project_learning_activation import activation
from .project_learning_identity import key_domain_digest, prepare_candidate, prepare_cases
from .project_learning_owner import PROMOTED_SQL, rejected_run_ids

MAX_JOBS = 8
CYCLE_SECONDS = 120.0
MINING_BATCH = 16  # Outcomes whose cases are derived per keyed preparation.
MINING_BATCHES_PER_STEP = 32  # So one request never holds the owner thread for long.
REPORT_CHUNK_BYTES = 49_152
MAX_AGE_MS = 10 * 365 * 24 * 60 * 60 * 1000
MAX_QA_ROWS = 20_000
RETENTION_MS = LEARNING_RETENTION_MS  # The one retention window of learning.
LEARNING_REASONS = frozenset({
    "learning_busy", "learning_capacity", "learning_deadline", "learning_invalid", "learning_unavailable",
})
_OTHER_TYPES = ("evaluation_case", "candidate", "feedback")


class _Stopped(Exception):
    pass


@dataclass(repr=False)
class _Job:
    request_id: str
    max_age_ms: int
    as_of_ms: int
    deadline: float
    rows: dict
    state: str = "waiting"  # waiting, selecting, mining, planning, finalizing, ready, failed
    reason: str | None = None
    thread: threading.Thread | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    outcomes: list = field(default_factory=list)  # (record, content digest, sequence), in sequence order
    cases: list = field(default_factory=list)
    candidates: set = field(default_factory=set)
    invalidated: frozenset = frozenset()
    accepted_qa: frozenset = frozenset()
    imported: str | None = None  # The activated legacy import whose records join the corpus.
    lineage: dict | None = None
    runs: dict = field(default_factory=dict)  # Outcome record identity to its run.
    mining: list = field(default_factory=list)
    mined: list = field(default_factory=list)
    plan: dict | None = None
    report: bytes | None = None
    summary: dict | None = None
    failure: str | None = None


def read_cycle_rows(history, now_ms=0):
    """Owner thread: the raw corpus for one cycle, bounded, with no parsing here.

    The corpus is this project's live records and, once its legacy learning has
    been brought over and switched on, that import's records before them. An
    outcome's sequence is (0, n) when imported and (1, n) when live. Outcomes
    and feedback past retention are left out, as the legacy ledger expired them; so are unpromoted
    cases and candidates, which the project service's cleanup removes (O0c P7). Promoted ones are read
    however old, being policy.
    Rows are read one at a time and the read stops at the first bound exceeded,
    so no more than the snapshot limit is ever held.
    """
    history._require_owner()
    if history._connection.in_transaction:
        raise HistoryError("history_result_unavailable")
    connection, project = history._connection, history.project_id
    horizon = now_ms - RETENTION_MS
    cutover = activation(history)
    imported = None if cutover is None else cutover["import_id"]
    rows = {"outcome": [], "qa": [], "accepted_qa": [], "import_id": imported,
            **{kind: [] for kind in _OTHER_TYPES}}
    used = {"bytes": 0, "outcome": 0, "other": 0, "qa": 0}

    def take(statement, parameters, kind, limit):
        """Rows one at a time, refused as soon as a count or the byte total is over its bound."""
        cursor = connection.execute(statement, (*parameters, limit + 1 - used[kind]))
        try:
            for row in cursor:
                content = row[-1]
                if type(content) is not bytes:
                    raise HistoryError("history_incompatible")
                used[kind] += 1
                used["bytes"] += len(content)
                if used[kind] > limit or used["bytes"] > MAX_SNAPSHOT_BYTES:
                    raise HistoryError("history_learning_capacity")
                yield row
        finally:
            cursor.close()

    def outcome(row, origin):
        identity, digest, sequence, content = row
        if type(identity) is not str or not matches(digest, _DIGEST) or not valid_integer(sequence, minimum=1):
            raise HistoryError("history_incompatible")
        rows["outcome"].append((identity, digest, content, (origin, sequence)))

    def other(row):
        if row[0] not in _OTHER_TYPES:
            raise HistoryError("history_incompatible")
        rows[row[0]].append(row[1])

    def review(row):
        if not matches(row[0], _DIGEST):
            raise HistoryError("history_incompatible")
        rows["qa"].append((row[0], row[1]))

    if imported is not None:
        current = "(expires_at_ms IS NULL OR expires_at_ms>?)"
        for row in take(
                "SELECT record_id,content_digest,source_sequence,content FROM project_learning_records "
                "WHERE project_id=? AND import_id=? AND record_type='outcome' AND " + current +
                " ORDER BY source_sequence LIMIT ?", (project, imported, now_ms), "outcome", MAX_OUTCOMES):
            outcome(row, 0)
        for row in take(
                "SELECT record_type,content FROM project_learning_records WHERE project_id=? AND import_id=? "
                "AND record_type IN (?,?,?) AND " + current + " AND (record_type='feedback' OR ingested_at_ms>? OR "
                + PROMOTED_SQL + ") ORDER BY source_sequence LIMIT ?",
                (project, imported, *_OTHER_TYPES, now_ms, horizon), "other", MAX_CASES + MAX_OUTCOMES):
            other(row)
        # Accepted QA of the old ledger: its operational QA rows, and the
        # owner-attested formal QA records it had admitted.
        accepted = connection.execute(
            "SELECT qa_digest FROM project_learning_qa_records WHERE project_id=? AND import_id=? "
            "AND accepted=1 LIMIT ?", (project, imported, MAX_QA_ROWS + 1)).fetchall()
        if len(accepted) > MAX_QA_ROWS:
            raise HistoryError("history_learning_capacity")
        for (digest,) in accepted:
            if not matches(digest, _DIGEST):
                raise HistoryError("history_incompatible")
            rows["accepted_qa"].append(digest)
        for row in take(
                "SELECT r.content_digest,r.content FROM project_learning_records r "
                "JOIN project_learning_attestations a ON a.project_id=r.project_id AND a.import_id=r.import_id "
                "AND a.subject_record_id=r.record_id WHERE r.project_id=? AND r.import_id=? "
                "AND r.record_type='qa_evidence' LIMIT ?", (project, imported), "qa", MAX_QA_ROWS):
            review(row)
    for row in take(
            "SELECT record_id,content_digest,source_sequence,content FROM records "
            "INDEXED BY records_learning_outcomes "
            "WHERE project_id=? AND record_type='outcome' AND ingested_at_ms>? ORDER BY source_sequence LIMIT ?",
            (project, horizon), "outcome", MAX_OUTCOMES):
        outcome(row, 1)
    for row in take(
            "SELECT record_type,content FROM records WHERE project_id=? AND record_type IN (?,?,?) "
            "AND (ingested_at_ms>? OR " + PROMOTED_SQL + ") ORDER BY source_sequence LIMIT ?",
            (project, *_OTHER_TYPES, horizon), "other", MAX_CASES + MAX_OUTCOMES):
        other(row)
    for row in take("SELECT qa_digest,qa FROM project_operational_qa WHERE project_id=? LIMIT ?",
                    (project,), "qa", MAX_QA_ROWS):
        review(row)
    return rows


def accepted_reviews(rows, checkpoint=lambda: None):
    """The digests of this project's accepted operational QA, from its own QA rows."""
    accepted = set()
    for digest, content in rows:
        checkpoint()
        review = parse_bounded_json(content)
        if type(review) is not dict or content_digest(review) != digest:
            raise ValueError()
        if review.get("accepted") is True:
            accepted.add(digest)
    return frozenset(accepted)


def append_learning_records(history, records, *, ingested_at_ms):
    """Owner thread: append owner-derived cases or candidates; returns the ones that were new.

    Identities are keyed on their sources, so a repeated cycle appends nothing.
    The contract validator, canonical bytes and size are checked again here;
    these record types are never accepted from a client.
    """
    history._require_owner()
    prepared = []
    for record in records:
        try:
            checked = validate_learning_record(record)
            content = canonical_json(checked)
        except (ContractError, TypeError, ValueError, RecursionError):
            raise HistoryError("history_invalid_input") from None
        if checked["record_type"] not in {"evaluation_case", "candidate"} or len(content) > MAX_RECORD_BYTES:
            raise HistoryError("history_invalid_input")
        prepared.append((checked, content))
    created = []
    with history._transaction():
        for checked, content in prepared:
            existing = history._connection.execute(
                "SELECT content,source FROM records WHERE project_id=? AND record_id=?",
                (history.project_id, checked["record_id"]),
            ).fetchone()
            if existing is not None:
                if existing[0] != content or existing[1] != "evaluator":
                    raise HistoryError("history_conflict")
                continue
            # These records carry no observation time of their own: the cycle's is theirs.
            stamped = {**checked, "observed_at_ms": checked.get("observed_at_ms", ingested_at_ms)}
            history._append_locked(stamped, content, "evaluator", ingested_at_ms)
            created.append(checked)
    return created


class ProjectLearning:
    """The cycles of one project service; every method runs on the store's owner thread."""

    def __init__(self, *, clock_ms, monotonic=time.monotonic):
        self._clock_ms, self._monotonic = clock_ms, monotonic
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._closed = False

    # Worker thread ---------------------------------------------------------------
    def _checkpoint(self, job):
        def check():
            if job.cancelled.is_set() or self._monotonic() >= job.deadline:
                raise _Stopped()
        return check

    def _work(self, job, step):
        try:
            step(job, self._checkpoint(job))
        except _Stopped:
            job.failure = "learning_deadline"
        except LearningCycleBoundsError:
            job.failure = "learning_capacity"
        except Exception:
            # No message, input or traceback is kept: the corpus stays as it was.
            job.failure = "learning_invalid"

    @staticmethod
    def _select(job, checkpoint):
        outcomes = []
        seen = set()
        for identity, digest, content, sequence in job.rows["outcome"]:
            checkpoint()
            record = parse_bounded_json(content)
            if record.get("record_id") != identity or record.get("record_type") != "outcome":
                raise ValueError()
            if identity in seen:  # One record under one identity, whichever store holds it.
                continue
            seen.add(identity)
            outcomes.append((record, digest, sequence))
        job.accepted_qa = accepted_reviews(job.rows["qa"], checkpoint) | frozenset(job.rows["accepted_qa"])
        cases = [parse_bounded_json(content) for content in job.rows["evaluation_case"]]
        job.candidates = {parse_bounded_json(content)["candidate_digest"] for content in job.rows["candidate"]}
        job.invalidated = frozenset(
            feedback["run_id"] for feedback in map(parse_bounded_json, job.rows["feedback"])
            if feedback["label"] in {"rejected", "corrected"})
        mining = select_mining_outcomes(
            [record for record, _, _ in outcomes], existing_cases=cases,
            owner_invalidated_run_ids=job.invalidated, checkpoint=checkpoint)
        digests = {record["record_id"]: digest for record, digest, _ in outcomes}
        job.outcomes, job.cases = outcomes, cases
        job.mining = [(record["record_id"], digests[record["record_id"]]) for record in mining]
        job.runs = {record["record_id"]: record["run_id"] for record in mining}
        job.imported = job.rows["import_id"]
        job.rows = {}

    @staticmethod
    def _plan(job, checkpoint):
        job.plan = plan_learning_cycle(
            [record for record, _, _ in job.outcomes], all_cases=job.cases, as_of_ms=job.as_of_ms,
            max_age_ms=job.max_age_ms, owner_invalidated_run_ids=job.invalidated, checkpoint=checkpoint)
        # Retry ancestry for the candidate gate, built once here rather than per candidate.
        job.lineage = outcome_lineage((record, sequence) for record, _, sequence in job.outcomes)

    def _start(self, job, step):
        job.thread = threading.Thread(target=self._work, args=(job, step), daemon=True,
                                      name="project-learning")
        job.thread.start()

    # Owner thread ----------------------------------------------------------------
    def _fail(self, job, reason):
        job.state, job.reason = "failed", reason
        job.rows, job.outcomes, job.cases, job.plan, job.lineage, job.runs = {}, [], [], None, None, {}
        job.accepted_qa = frozenset()

    def _advance(self, job, history):
        if job.state in {"ready", "failed"} or (job.thread is not None and job.thread.is_alive()):
            return
        if job.failure is not None:
            self._fail(job, job.failure)
            return
        if job.state == "waiting":
            # One cycle at a time holds a corpus: the others wait their turn with nothing read.
            if any(other.state not in {"waiting", "ready", "failed"} for other in self._jobs.values()):
                return
            job.as_of_ms = self._clock_ms()
            job.rows = read_cycle_rows(history, job.as_of_ms)
            job.state = "selecting"
        if job.state == "selecting":
            if job.thread is None:
                self._start(job, self._select)
                return
            job.thread, job.state = None, "mining"
        if job.state == "mining":
            domain = key_domain_digest(history)
            # The owner may have rejected a run since the snapshot: nothing is derived from it.
            rejected = rejected_run_ids(history)
            job.invalidated = job.invalidated | rejected  # The plan leaves those runs out too.
            job.mining = [item for item in job.mining if job.runs.get(item[0]) not in rejected]
            for _ in range(MINING_BATCHES_PER_STEP):
                if not job.mining:
                    break
                batch, job.mining = job.mining[:MINING_BATCH], job.mining[MINING_BATCH:]
                cases = prepare_cases(history, batch, expected_domain_digest=domain, import_id=job.imported)
                created = append_learning_records(history, cases, ingested_at_ms=job.as_of_ms)
                job.mined.extend(case["record_id"] for case in created)
                known = {case["record_id"] for case in job.cases}
                job.cases.extend(case for case in cases if case["record_id"] not in known)
            if job.mining:
                return
            job.state = "planning"
        if job.state == "planning":
            if job.thread is None:
                self._start(job, self._plan)
                return
            job.thread, job.state = None, "finalizing"
        if job.state == "finalizing":
            self._finalize(job, history)

    def _finalize(self, job, history):
        from .cli_reports import learning_cycle_summary

        domain = key_domain_digest(history)
        by_digest = {digest: (record, sequence) for record, digest, sequence in job.outcomes}
        invalidated = job.invalidated | rejected_run_ids(history)  # As of now, not of the snapshot.
        proposed = []
        for spec in job.plan["candidate_specs"]:
            try:
                evidence = [by_digest[digest] for digest in spec["evidence_digests"]]
                references = [(record["record_id"], digest)
                              for digest, (record, _) in zip(spec["evidence_digests"], evidence)]
                record = prepare_candidate(history, canonical_json(spec), references,
                                           expected_domain_digest=domain, import_id=job.imported)
                if record["candidate_digest"] in job.candidates:
                    continue
                if any(outcome["run_id"] in invalidated for outcome, _ in evidence):
                    continue
                # The same gate the legacy ledger applies before it stores a candidate.
                check_candidate_eligibility(record, evidence, accepted_qa=job.accepted_qa, lineage=job.lineage)
                if append_learning_records(history, [record], ingested_at_ms=job.as_of_ms):
                    proposed.append(record["candidate_digest"])
                job.candidates.add(record["candidate_digest"])
            except (HistoryError, KeyError, EligibilityError):
                # Proposal generation is opportunistic: evidence that cannot be
                # bound again or is not yet eligible stays evidence and never
                # fails the cycle.
                continue
        report = {"mined_case_ids": job.mined, "proposed_candidate_digests": proposed,
                  "analysis": job.plan["analysis"], "proposal_status": job.plan["proposal_status"]}
        job.report, job.summary = canonical_json(report), learning_cycle_summary(report)
        job.outcomes, job.cases, job.plan, job.lineage, job.runs = [], [], None, None, {}
        job.state = "ready"

    def _view(self, job):
        if job.state == "failed":
            return {"state": "failed", "reason_code": job.reason, "summary": None, "report_bytes": None}
        if job.state == "ready":
            return {"state": "ready", "reason_code": None, "summary": job.summary,
                    "report_bytes": len(job.report)}
        return {"state": "pending", "reason_code": None, "summary": None, "report_bytes": None}

    def cycle(self, history, request_id, max_age_ms):
        """Start the named cycle or advance it one step; returns its current view."""
        if self._closed or not valid_integer(max_age_ms, minimum=1, maximum=MAX_AGE_MS):
            raise HistoryError("history_invalid_input")
        job = self._jobs.get(request_id)
        if job is not None and job.max_age_ms != max_age_ms:
            raise HistoryError("history_conflict")
        # Cycles whose callers left are expired first, so they never keep new ones out.
        for other in self._jobs.values():
            if other.state not in {"ready", "failed"} and self._monotonic() >= other.deadline:
                other.cancelled.set()
                if other.thread is None or not other.thread.is_alive():
                    self._fail(other, "learning_deadline")
        if job is None:
            for known in [key for key, old in self._jobs.items() if old.state in {"ready", "failed"}][:-2]:
                del self._jobs[known]
            if len(self._jobs) >= MAX_JOBS:
                return {"state": "failed", "reason_code": "learning_busy", "summary": None, "report_bytes": None}
            # The deadline covers the wait for its turn and the read of the corpus.
            job = self._jobs[request_id] = _Job(
                request_id, max_age_ms, self._clock_ms(), self._monotonic() + CYCLE_SECONDS, {})
        # Any request moves every cycle on, so one whose caller left still finishes.
        for other in list(self._jobs.values()):
            if other.cancelled.is_set():
                continue
            try:
                self._advance(other, history)
            except HistoryError as error:
                self._fail(other, "learning_capacity" if error.reason_code == "history_learning_capacity"
                           else "learning_unavailable")
        return self._view(job)

    def report_chunk(self, request_id, offset):
        """One slice of a finished cycle's full report (canonical JSON, ASCII)."""
        job = self._jobs.get(request_id)
        if (job is None or job.state != "ready" or not valid_integer(offset, maximum=len(job.report))):
            raise HistoryError("history_not_found")
        chunk = job.report[offset:offset + REPORT_CHUNK_BYTES]
        return {"chunk": chunk.decode("ascii"), "next_offset": offset + len(chunk), "report_bytes": len(job.report)}

    def close(self):
        self._closed = True
        for job in self._jobs.values():
            job.cancelled.set()
        self._jobs = {}
