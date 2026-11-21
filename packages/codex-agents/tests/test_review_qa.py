from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import unittest

from tests.support import *  # noqa: F401,F403
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import build_operational_qa_records
from tests.learning_support import DIGEST_KEY
from tests.test_learning_engine import verified_qa_inputs

REVIEW_SLICE = "coding-low-low-review-known-narrow-independent_judgment"
TESTS_NOT_RUN = "Tests were not run: this review is read-only."


def bind(inputs: dict) -> dict:
    inputs["host_outcome"]["evidence_digests"] = sorted({
        keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_assignment"])),
        keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_result"])),
    })
    inputs["verifier_assignment"]["context"] = [content_digest(inputs["host_outcome"])]
    inputs["verifier_host_outcome"]["evidence_digests"] = sorted({
        keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_assignment"])),
        keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
    })
    return inputs


def qa_inputs(*, review: bool, worker_verdict="not-ready", verifier_verdict="ready-with-corrections",
              worker_outcome="observed", verifier_outcome="observed", claims=(TESTS_NOT_RUN,),
              worker_risks=("Defect remains",), verifier_risks=("Defect remains",)) -> dict:
    inputs = verified_qa_inputs()
    inputs.pop("evaluation_assignment")
    inputs.pop("evaluation_case")
    host = inputs["host_outcome"]
    host["evaluation_candidate_digest"] = None
    host["evaluation_case_digest"] = None
    if review:
        host["task_slice"] = REVIEW_SLICE
    for key, verdict, outcome, risks in (
        ("evaluator_result", worker_verdict, worker_outcome, worker_risks),
        ("verifier_result", verifier_verdict, verifier_outcome, verifier_risks),
    ):
        value = inputs[key]
        value.update(
            execution_state="completed", semantic_verdict=verdict, admission_state="unverified",
            retry={"attempt": 1, "retry_of_assignment_id": None},
            remaining_risks=list(risks), unverified_claims=list(claims),
        )
        value["evidence"][0]["outcome"] = outcome
    for key, verdict in (("host_outcome", worker_verdict), ("verifier_host_outcome", verifier_verdict)):
        inputs[key].update(
            execution_state="completed", semantic_verdict=verdict, admission_state="unverified",
            retry_attempt=1, retry_of_assignment_id=None,
        )
    return bind(inputs)


class ReviewQaTests(unittest.TestCase):
    def test_review_is_accepted_when_verifier_confirms_findings_whatever_the_verdicts(self) -> None:
        qa, record = build_operational_qa_records(**qa_inputs(review=True))
        self.assertTrue(qa["accepted"])
        self.assertFalse(qa["verifier_disagreement"])
        self.assertTrue(record["accepted"])
        self.assertEqual("accepted", record["status"])

    def test_review_with_a_refuted_finding_is_a_disagreement(self) -> None:
        qa, record = build_operational_qa_records(**qa_inputs(review=True, verifier_outcome="fail"))
        self.assertTrue(qa["verifier_disagreement"])
        self.assertFalse(qa["accepted"])
        self.assertEqual("rejected", record["status"])

    def test_review_with_an_unexpected_unverified_claim_is_not_accepted(self) -> None:
        qa, _ = build_operational_qa_records(
            **qa_inputs(review=True, claims=("The benchmark numbers are probably right.",))
        )
        self.assertFalse(qa["accepted"])

    def test_non_review_task_keeps_the_strict_rule(self) -> None:
        qa, _ = build_operational_qa_records(**qa_inputs(review=False))
        self.assertFalse(qa["accepted"])
        qa, _ = build_operational_qa_records(
            **qa_inputs(review=False, worker_verdict="ready", verifier_verdict="ready",
                        claims=(TESTS_NOT_RUN,), worker_risks=(), verifier_risks=())
        )
        self.assertFalse(qa["accepted"])


if __name__ == "__main__":
    unittest.main()
