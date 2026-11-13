from __future__ import annotations

import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.learning_support import (
    DIGEST_A, champion_configuration, full_usage, heldout_cases, verification_outcome,
)

from codex_agents import project_learning_live as live
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_eligibility import EligibilityError, check_candidate_eligibility
from codex_agents.project_history import ProjectHistory
from codex_agents.project_qa_commit import QA_COLUMNS
from codex_agents.result_contracts import ResultCapacity

PROJECT = "prj_" + "a" * 32
KEY = b"K" * 32
MAX_AGE_MS = 1_000_000


def project_store(directory, project_id=PROJECT, key=KEY):
    directory.mkdir(mode=0o700)
    return ProjectHistory(
        directory, project_id, result_key=key,
        result_capacity=ResultCapacity(1, 1_048_576, 120_000, 120_000),
    )


def fixture_record(store, record, source, timestamp):
    """Synthetic trusted-owner fixture; no generic verified-write, feedback or QA authority."""
    raw = canonical_json(record)
    with store._transaction():
        return store._append_locked(
            {**record, "observed_at_ms": record.get("observed_at_ms", timestamp)}, raw, source, timestamp)


def fixture_review(store, index, review):
    """One synthetic operational QA row; only its digest and content are read by learning."""
    digest = content_digest(review)
    row = {
        "project_id": store.project_id, "worker_outcome_digest": f"{index + 10_000:064x}",
        "verifier_outcome_digest": f"{index + 20_000:064x}", "worker_admission_digest": "1" * 64,
        "verifier_admission_digest": "2" * 64, "check_receipt_id": f"oqc_{index:048x}",
        "scope_digest": "3" * 64, "qa_digest": digest, "qa": canonical_json(review),
        "outcome_record_id": f"qa-outcome-{index}", "outcome_digest": "4" * 64,
        "source_sequence": index + 1, "ingested_at_ms": 1, "late": 0, "consumed_at_ms": 1,
        "record_seal": "5" * 64,
    }
    fields = QA_COLUMNS["project_operational_qa"]
    with store._transaction():
        store._connection.execute(
            "INSERT INTO project_operational_qa VALUES(" + ",".join("?" for _ in fields) + ")",
            tuple(row[name] for name in fields))
    return digest


def run_cycle(learning, store, request, max_age_ms=MAX_AGE_MS, seconds=60.0):
    limit = time.monotonic() + seconds
    while True:
        view = learning.cycle(store, request, max_age_ms)
        if view["state"] != "pending":
            return view
        if time.monotonic() > limit:
            raise AssertionError("learning cycle did not finish")
        time.sleep(0.002)


def full_report(learning, request):
    data, offset = b"", 0
    while True:
        part = learning.report_chunk(request, offset)
        data += part["chunk"].encode("ascii")
        offset = part["next_offset"]
        if offset >= part["report_bytes"]:
            return json.loads(data)


def stored(store, kind):
    return [bytes(row[0]) for row in store._connection.execute(
        "SELECT content FROM records WHERE project_id=? AND record_type=? ORDER BY source_sequence",
        (store.project_id, kind))]


def live_cycle(directory, project_id, key, *, outcomes, cases, candidates, feedback, accepted_qa,
               as_of_ms, max_age_ms):
    """Run one live cycle over a synthetic copy of a legacy corpus.

    Returns the full report and the canonical bytes of every record it added,
    for comparison with the unchanged legacy cycle on the same corpus.
    """
    store = project_store(directory, project_id, key)
    try:
        for outcome in outcomes:
            fixture_record(store, outcome, "verifier", as_of_ms)
        for record in [*cases, *candidates]:
            fixture_record(store, record, "evaluator", as_of_ms)
        for record in feedback:
            fixture_record(store, record, "owner", as_of_ms)
        before = {kind: set(stored(store, kind)) for kind in ("evaluation_case", "candidate")}
        learning = live.ProjectLearning(clock_ms=lambda: as_of_ms)
        # The legacy fixture's QA digests are opaque; the live QA rows have their own test.
        with mock.patch.object(live, "accepted_reviews", lambda rows, checkpoint=None: frozenset(accepted_qa)):
            view = run_cycle(learning, store, "parity", max_age_ms)
        if view["state"] != "ready":
            raise AssertionError(view)
        report = full_report(learning, "parity")
        added = [content for kind in ("evaluation_case", "candidate")
                 for content in stored(store, kind) if content not in before[kind]]
        learning.close()
        return report, view["summary"], added
    finally:
        store.close()


class ProjectLearningLiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.store = project_store(Path(self.temporary.name) / "history")
        self.addCleanup(self.store.close)
        self.learning = live.ProjectLearning(clock_ms=lambda: 500)
        self.addCleanup(self.learning.close)

    def outcome(self, name="first", timestamp=10):
        return fixture_record(self.store, verification_outcome(name), "verifier", timestamp)

    def test_verified_outcome_is_mined_once_and_reported(self):
        self.outcome()
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("ready", view["state"])
        self.assertEqual("compact-learning-cycle-v1", view["summary"]["format"])
        self.assertEqual("available", view["summary"]["summary_state"])
        self.assertEqual(1, view["summary"]["mined_case_count"])
        report = full_report(self.learning, "one")
        self.assertEqual({"analysis", "mined_case_ids", "proposed_candidate_digests", "proposal_status"},
                         set(report))
        cases = [json.loads(content) for content in stored(self.store, "evaluation_case")]
        self.assertEqual(report["mined_case_ids"], [case["record_id"] for case in cases])
        self.assertEqual("development", cases[0]["corpus_state"])
        self.assertEqual("first", cases[0]["source_outcome_id"])
        # The same request is the same cycle; a new one finds nothing left to add.
        self.assertEqual(view, self.learning.cycle(self.store, "one", MAX_AGE_MS))
        again = run_cycle(self.learning, self.store, "two")
        self.assertEqual(0, again["summary"]["mined_case_count"])
        self.assertEqual([], full_report(self.learning, "two")["mined_case_ids"])
        self.assertEqual(1, len(stored(self.store, "evaluation_case")))

    def test_unverified_outcome_is_analysed_but_never_mined(self):
        outcome = verification_outcome("pending")
        outcome.update(status="unverified", accepted=False, admission_state="unverified")
        fixture_record(self.store, outcome, "host", 10)
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("ready", view["state"])
        self.assertEqual(0, view["summary"]["mined_case_count"])
        self.assertEqual([], stored(self.store, "evaluation_case"))

    def test_request_cannot_change_its_window_and_bad_input_is_refused(self):
        self.outcome()
        self.learning.cycle(self.store, "one", MAX_AGE_MS)
        with self.assertRaises(HistoryError) as raised:
            self.learning.cycle(self.store, "one", MAX_AGE_MS + 1)
        self.assertEqual("history_conflict", raised.exception.reason_code)
        for bad in (0, -1, True, "1", live.MAX_AGE_MS + 1):
            with self.subTest(bad=bad), self.assertRaises(HistoryError):
                self.learning.cycle(self.store, "other", bad)
        run_cycle(self.learning, self.store, "one")

    def test_report_is_served_in_bounded_chunks(self):
        self.outcome()
        with mock.patch.object(live, "REPORT_CHUNK_BYTES", 7):
            view = run_cycle(self.learning, self.store, "one")
            first = self.learning.report_chunk("one", 0)
            self.assertEqual(7, len(first["chunk"]))
            self.assertEqual(view["report_bytes"], first["report_bytes"])
            report = full_report(self.learning, "one")
        self.assertEqual(1, len(report["mined_case_ids"]))
        for request, offset in (("missing", 0), ("one", -1), ("one", view["report_bytes"] + 1), ("one", True)):
            with self.subTest(request=request, offset=offset), self.assertRaises(HistoryError):
                self.learning.report_chunk(request, offset)

    def test_deadline_fails_the_cycle_and_leaves_the_next_one_whole(self):
        self.outcome()
        ticks = iter([0.0] + [live.CYCLE_SECONDS + 1.0] * 1000)
        late = live.ProjectLearning(clock_ms=lambda: 500, monotonic=lambda: next(ticks))
        self.addCleanup(late.close)
        view = late.cycle(self.store, "one", MAX_AGE_MS)
        self.assertEqual({"state": "failed", "reason_code": "learning_deadline", "summary": None,
                          "report_bytes": None}, view)
        self.assertEqual([], stored(self.store, "evaluation_case"))
        with self.assertRaises(HistoryError):
            late.report_chunk("one", 0)
        self.assertEqual(1, run_cycle(self.learning, self.store, "two")["summary"]["mined_case_count"])

    def test_corpus_over_its_bound_is_refused_not_truncated(self):
        self.outcome("a", 10)
        self.outcome("b", 11)
        with mock.patch.object(live, "MAX_OUTCOMES", 1):
            view = self.learning.cycle(self.store, "one", MAX_AGE_MS)
        self.assertEqual("learning_capacity", view["reason_code"])
        with mock.patch.object(live, "MAX_SNAPSHOT_BYTES", 10):
            view = self.learning.cycle(self.store, "two", MAX_AGE_MS)
        self.assertEqual("learning_capacity", view["reason_code"])
        self.assertEqual([], stored(self.store, "evaluation_case"))

    def test_too_many_unfinished_cycles_are_refused_and_close_stops_everything(self):
        self.outcome()
        blocked = live.ProjectLearning(clock_ms=lambda: 500)
        with mock.patch.object(blocked, "_advance", lambda job, history: None):
            for index in range(live.MAX_JOBS):
                self.assertEqual("pending", blocked.cycle(self.store, f"job-{index}", MAX_AGE_MS)["state"])
            self.assertEqual("learning_busy", blocked.cycle(self.store, "extra", MAX_AGE_MS)["reason_code"])
        blocked.close()
        with self.assertRaises(HistoryError):
            blocked.cycle(self.store, "job-0", MAX_AGE_MS)

    def test_damaged_stored_record_fails_the_cycle_without_writing(self):
        self.outcome()
        with self.store._transaction():
            self.store._connection.execute("UPDATE records SET content=? WHERE record_type='outcome'", (b"{",))
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("learning_invalid", view["reason_code"])
        self.assertEqual([], stored(self.store, "evaluation_case"))

    def test_only_owner_derived_cases_and_candidates_can_be_appended(self):
        entry = self.outcome()
        with self.assertRaises(HistoryError):
            live.append_learning_records(self.store, [json.loads(entry.content)], ingested_at_ms=20)
        with self.assertRaises(HistoryError):
            live.append_learning_records(self.store, [{"record_type": "evaluation_case"}], ingested_at_ms=20)
        run_cycle(self.learning, self.store, "one")
        case = json.loads(stored(self.store, "evaluation_case")[0])
        self.assertEqual([], live.append_learning_records(self.store, [case], ingested_at_ms=99))
        changed = {**case, "criteria_digest": "f" * 64}
        with self.assertRaises(HistoryError) as raised:
            live.append_learning_records(self.store, [changed], ingested_at_ms=99)
        self.assertEqual("history_conflict", raised.exception.reason_code)

    def test_accepted_reviews_come_only_from_intact_accepted_rows(self):
        accepted = fixture_review(self.store, 1, {"accepted": True, "name": "a"})
        fixture_review(self.store, 2, {"accepted": False, "name": "b"})
        fixture_review(self.store, 3, {"accepted": 1, "name": "c"})
        rows = live.read_cycle_rows(self.store)["qa"]
        self.assertEqual(frozenset({accepted}), live.accepted_reviews(rows))
        with self.assertRaises(ValueError):
            live.accepted_reviews([(accepted, canonical_json({"accepted": True, "name": "other"}))])
        with self.store._transaction():
            self.store._connection.execute(
                "UPDATE project_operational_qa SET qa=? WHERE qa_digest=?",
                (canonical_json({"accepted": True, "name": "forged"}), accepted))
        self.outcome()
        self.assertEqual("learning_invalid", run_cycle(self.learning, self.store, "one")["reason_code"])


class ProjectLearningCandidateTests(unittest.TestCase):
    """A cheaper configuration is proposed only on verified, QA-accepted, matched evidence."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.store = project_store(Path(self.temporary.name) / "history")
        self.addCleanup(self.store.close)
        self.learning = live.ProjectLearning(clock_ms=lambda: 500)
        self.addCleanup(self.learning.close)
        for index, reviewed in enumerate(heldout_cases()):
            source = verification_outcome(reviewed["source_outcome_id"])
            source.update(**champion_configuration(), task_slice=reviewed["task_slice"],
                          fixture_digest=reviewed["fixture_digest"])
            source["configuration_digest"] = content_digest(champion_configuration())
            fixture_record(self.store, source, "verifier", 30 + index)
            development = {**reviewed, "record_id": reviewed["supersedes_case_id"], "supersedes_case_id": None,
                           "corpus_state": "development", "similarity_group_digest": None, "reviewed": False,
                           "review_digest": None, "frozen": False}
            live.append_learning_records(self.store, [development, reviewed], ingested_at_ms=30 + index)

    def cohort(self, *, accepted, count=20):
        outcomes = []
        for index in range(count):
            for model, tokens in (("gpt-5.6-terra", 200), ("gpt-5.6-luna", 100)):
                outcome = verification_outcome(f"dominant-{index}-{model}")
                configuration = {**champion_configuration(), "model": model}
                review = fixture_review(self.store, len(outcomes), {"accepted": accepted, "for": outcome["record_id"]})
                outcome.update(
                    **configuration, task_slice="coding-medium-low", fixture_digest=f"{index + 2000:064x}",
                    configuration_digest=content_digest(configuration),
                    usage=full_usage(input_tokens=tokens, output_tokens=tokens, model_calls=2),
                    evidence_digests=[DIGEST_A, review],
                )
                fixture_record(self.store, outcome, "verifier", 100 + index)
                outcomes.append(outcome)
        return outcomes

    def test_accepted_matched_cohort_produces_one_inert_proposal_once(self):
        outcomes = self.cohort(accepted=True)
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("ready", view["state"])
        self.assertEqual(40, view["summary"]["mined_case_count"])
        self.assertEqual(1, view["summary"]["proposed_candidate_count"])
        report = full_report(self.learning, "one")
        candidates = [json.loads(content) for content in stored(self.store, "candidate")]
        self.assertEqual(report["proposed_candidate_digests"], [item["candidate_digest"] for item in candidates])
        self.assertEqual(sorted(content_digest(item) for item in outcomes), candidates[0]["evidence_digests"])
        self.assertEqual("gpt-5.6-luna", candidates[0]["change_manifest"][0]["after_value"]["model"])
        again = run_cycle(self.learning, self.store, "two")
        self.assertEqual(0, again["summary"]["proposed_candidate_count"])
        self.assertEqual(1, len(stored(self.store, "candidate")))

    def test_cohort_without_accepted_qa_is_mined_but_never_proposed(self):
        self.cohort(accepted=False)
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("ready", view["state"])
        self.assertEqual(40, view["summary"]["mined_case_count"])
        self.assertEqual(0, view["summary"]["proposed_candidate_count"])
        self.assertEqual([], stored(self.store, "candidate"))

    def test_owner_rejected_evidence_is_never_proposed(self):
        outcomes = self.cohort(accepted=True)
        feedback = {"schema_version": 1, "record_type": "feedback", "record_id": "feedback-1",
                    "run_id": outcomes[0]["run_id"], "label": "rejected"}
        fixture_record(self.store, feedback, "owner", 200)
        view = run_cycle(self.learning, self.store, "one")
        self.assertEqual("ready", view["state"])
        self.assertEqual(0, view["summary"]["proposed_candidate_count"])
        self.assertEqual([], stored(self.store, "candidate"))


class CandidateEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.evidence = []
        for index in range(20):
            for model in ("gpt-5.6-terra", "gpt-5.6-luna"):
                outcome = verification_outcome(f"cohort-{index}-{model}")
                outcome.update(**{**champion_configuration(), "model": model}, task_slice="coding-medium-low",
                               fixture_digest=f"{index + 2000:064x}", evidence_digests=[DIGEST_A, "9" * 64])
                self.evidence.append((outcome, len(self.evidence) + 1))
        self.candidate = {
            "task_slice": "coding-medium-low",
            "champion_configuration": {**champion_configuration(), "model": "gpt-5.6-terra"},
            "candidate_configuration": {**champion_configuration(), "model": "gpt-5.6-luna"},
            "evidence_digests": [content_digest(outcome) for outcome, _ in self.evidence],
            "eligibility_receipt": {"independent_case_count": 20},
        }

    def check(self, evidence=None, accepted_qa=frozenset({"9" * 64}), project_outcomes=None):
        evidence = self.evidence if evidence is None else evidence
        check_candidate_eligibility(
            self.candidate, evidence, accepted_qa=accepted_qa,
            project_outcomes=evidence if project_outcomes is None else project_outcomes)

    def test_complete_matched_cohort_is_eligible(self):
        self.check()

    def test_each_gate_refuses_with_its_own_reason(self):
        def changed(index, **fields):
            evidence = deepcopy(self.evidence)
            evidence[index][0].update(fields)
            return evidence

        cases = (
            ("complete outcome cohort", {"evidence": self.evidence[:-1]}),
            ("verified eligibility gate", {"accepted_qa": frozenset()}),
            ("verified eligibility gate", {"evidence": changed(0, verifier_disagreement=True)}),
            ("verified eligibility gate", {"evidence": changed(0, coverage="partial")}),
            ("version or task-mix confounded", {"evidence": changed(0, policy_digest="8" * 64)}),
            ("matched task cohort", {"evidence": changed(0, fixture_digest="7" * 64)}),
            ("matched task cohort", {"evidence": changed(0, effort="low")}),
            ("retry lineage is incomplete", {"evidence": changed(0, retry_attempt=2)}),
            ("not an earlier persisted outcome",
             {"evidence": changed(0, retry_attempt=2, retry_of_assignment_id="assignment_missing")}),
        )
        for message, arguments in cases:
            with self.subTest(message=message), self.assertRaisesRegex(EligibilityError, message):
                self.check(**arguments)

    def test_retry_of_another_evidence_outcome_is_not_independent(self):
        evidence = deepcopy(self.evidence)
        parent, child = evidence[0][0], evidence[2][0]
        child.update(retry_attempt=2, retry_of_assignment_id=parent["assignment_id"],
                     fixture_digest=parent["fixture_digest"])
        with self.assertRaises(EligibilityError):
            self.check(evidence=evidence)


if __name__ == "__main__":
    unittest.main()
