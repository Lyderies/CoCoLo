"""The verified-evidence gate every stored candidate must pass.

One pure check shared by the legacy ledger and a project's own history, so a
candidate is eligible by the same rule wherever its evidence lives. The caller
reads the rows; nothing here touches storage, a key or a clock.
"""

from __future__ import annotations

from typing import Any, Iterable

MIN_MATCHED_FIXTURES = 20
MIN_INDEPENDENT_ROOTS = 40
_REQUIRED_USAGE = (
    "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
    "model_calls", "tool_calls", "latency_ms",
)
_CONFIGURATION_FIELDS = (
    "model", "effort", "role", "topology", "max_parallel",
    "verification_required", "verifier_digest", "bounds_digest",
    "package_digest", "policy_digest", "agent_digest", "skill_digests",
    "prompt_digest", "client_digest",
)


class EligibilityError(ValueError):
    """Candidate evidence fails the gate; the message names which part."""


def _verified(outcome: dict[str, Any], task_slice: str, accepted_qa: frozenset[str] | set[str]) -> bool:
    sources = outcome.get("usage_sources")
    return not (
        outcome["task_slice"] != task_slice
        or outcome["accepted"] is not True
        or outcome.get("execution_state") != "completed"
        or outcome.get("admission_state") != "accepted"
        or outcome.get("semantic_verdict") not in {"pass", "ready", "ready-with-corrections"}
        or not isinstance(outcome.get("retry_attempt"), int)
        or outcome["verification_required"] is not True
        or outcome["verifier_digest"] is None
        or outcome["verifier_disagreement"] is not False
        or outcome["deterministic_pass"] is not True
        or outcome["security_pass"] is not True
        or outcome["required_criteria_passed"] != outcome["required_criteria_total"]
        or outcome["coverage"] != "end_to_end_complete"
        or not (set(outcome["evidence_digests"]) & accepted_qa)
        or any(outcome["usage"][field] is None for field in _REQUIRED_USAGE)
        or not isinstance(sources, dict)
        or any(
            sources.get(field, {}).get("source") == "unavailable"
            or sources.get(field, {}).get("coverage") != "end_to_end_complete"
            for field in _REQUIRED_USAGE
        )
    )


def outcome_lineage(project_outcomes: Iterable[tuple[dict[str, Any], Any]]) -> dict[str, list]:
    """Every outcome of a project, in sequence order, grouped by assignment: retry ancestry."""

    lineage: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for outcome, sequence in project_outcomes:
        lineage.setdefault(str(outcome["assignment_id"]), []).append((outcome, sequence))
    return lineage


def check_candidate_eligibility(
    candidate: dict[str, Any],
    evidence: list[tuple[dict[str, Any], Any]],
    *,
    accepted_qa: frozenset[str] | set[str],
    project_outcomes: Iterable[tuple[dict[str, Any], Any]] | None = None,
    lineage: dict[str, list] | None = None,
) -> None:
    """Raise `EligibilityError` unless the candidate's evidence is a verified matched cohort.

    `evidence` is the candidate's outcome records with their store sequence,
    one per evidence digest. Retry ancestry is resolved against every outcome
    of the project: `project_outcomes` in sequence order, or the `lineage`
    already built from them by `outcome_lineage`. Sequences only need to be
    mutually comparable.
    """

    if len(evidence) != len(candidate["evidence_digests"]):
        raise EligibilityError("candidate evidence is not a complete outcome cohort")
    outcomes = [outcome for outcome, _ in evidence]
    if not all(_verified(outcome, candidate["task_slice"], accepted_qa) for outcome in outcomes):
        raise EligibilityError("candidate evidence fails a verified eligibility gate")
    versions = {
        (outcome["package_digest"], outcome["policy_digest"], outcome["client_digest"], outcome["task_slice"])
        for outcome in outcomes
    }
    if len(versions) != 1:
        raise EligibilityError("candidate evidence is version or task-mix confounded")

    def configuration(outcome: dict[str, Any]) -> dict[str, Any]:
        return {field: outcome[field] for field in _CONFIGURATION_FIELDS}

    champion = [item for item in outcomes if configuration(item) == candidate["champion_configuration"]]
    challenger = [item for item in outcomes if configuration(item) == candidate["candidate_configuration"]]
    matched = {item["fixture_digest"] for item in champion} & {item["fixture_digest"] for item in challenger}
    if (
        len(champion) + len(challenger) != len(outcomes)
        or len(matched) < MIN_MATCHED_FIXTURES
        or any(
            sum(item["fixture_digest"] == fixture for item in cohort) != 1
            for fixture in matched
            for cohort in (champion, challenger)
        )
        or any(item["fixture_digest"] not in matched for item in outcomes)
        or candidate["eligibility_receipt"]["independent_case_count"] != len(matched)
    ):
        raise EligibilityError("candidate evidence lacks a matched task cohort")

    # Retry claims are only authoritative when their complete ancestry already
    # exists in this project. Multiple records for one assignment are expected
    # while an unverified host outcome is superseded by accepted QA, but every
    # version must agree on its lineage and task fixture.
    if lineage is None:
        lineage = outcome_lineage(project_outcomes or ())
    for versions_of_one in lineage.values():
        claims = {
            (item.get("retry_attempt", 1), item.get("retry_of_assignment_id"), item["task_slice"],
             item["fixture_digest"])
            for item, _ in versions_of_one
        }
        if len(claims) != 1:
            raise EligibilityError("persisted retry lineage is internally inconsistent")

    def retry_root(outcome: dict[str, Any], sequence: int) -> str:
        current, current_sequence, seen = outcome, sequence, set()
        while True:
            assignment_id = str(current["assignment_id"])
            if assignment_id in seen:
                raise EligibilityError("candidate retry lineage is cyclic")
            seen.add(assignment_id)
            predecessor, attempt = current.get("retry_of_assignment_id"), current.get("retry_attempt")
            if predecessor is None:
                if attempt != 1:
                    raise EligibilityError("candidate retry lineage is incomplete")
                return assignment_id
            prior = [item for item in lineage.get(str(predecessor), []) if item[1] < current_sequence]
            if not prior:
                raise EligibilityError("candidate retry predecessor is not an earlier persisted outcome")
            parent, parent_sequence = prior[-1]
            if (
                parent["task_slice"] != current["task_slice"]
                or parent["fixture_digest"] != current["fixture_digest"]
                or parent.get("retry_attempt", 1) != attempt - 1
            ):
                raise EligibilityError("candidate retry predecessor changes task lineage")
            current, current_sequence = parent, parent_sequence

    roots = {retry_root(outcome, sequence) for outcome, sequence in evidence}
    if len(roots) != len(evidence) or len(roots) < MIN_INDEPENDENT_ROOTS:
        raise EligibilityError("candidate evidence is not independently retry-deduplicated")


def check_evaluation_case(
    case: dict[str, Any],
    *,
    source_outcome: dict[str, Any] | None,
    previous_case: dict[str, Any] | None,
    related_cases: Iterable[dict[str, Any]],
    invalidated_run_ids: frozenset[str] | set[str],
) -> None:
    """Raise `EligibilityError` unless the case may join the project's corpus.

    `source_outcome` is the outcome the case names, `previous_case` the case it
    supersedes (both None when absent), and `related_cases` every other case of
    the project: all but the superseded one, or all but this one when it
    supersedes nothing.
    """

    if source_outcome is None:
        raise EligibilityError("evaluation case source outcome is missing")
    if (
        source_outcome["task_slice"] != case["task_slice"]
        or source_outcome["fixture_digest"] != case["fixture_digest"]
    ):
        raise EligibilityError("evaluation case differs from its source outcome")
    if source_outcome["run_id"] in invalidated_run_ids:
        raise EligibilityError("evaluation case source was invalidated by owner feedback")
    previous_id = case["supersedes_case_id"]
    if previous_id is None:
        if case["corpus_state"] != "development":
            raise EligibilityError("controlled evaluation case must supersede a development case")
    else:
        if previous_case is None:
            raise EligibilityError("superseded evaluation case is missing")
        immutable = ("source_outcome_id", "task_slice", "fixture_digest", "criteria_digest", "lineage_digest")
        if any(previous_case[field] != case[field] for field in immutable):
            raise EligibilityError("evaluation case revision changes immutable evidence")
        if previous_case["similarity_group_digest"] not in {None, case["similarity_group_digest"]}:
            raise EligibilityError("evaluation case revision changes its similarity group")
    related = list(related_cases)
    controlled = [
        record for record in related
        if record["corpus_state"] != "development" and record["record_id"] != case["record_id"]
    ]
    if previous_id is not None and any(record["supersedes_case_id"] == previous_id for record in controlled):
        raise EligibilityError("development case already entered a controlled corpus")
    split_key = (case["source_outcome_id"], case["fixture_digest"], case["criteria_digest"], case["lineage_digest"])
    if case["corpus_state"] != "development" and any(
        (record["source_outcome_id"], record["fixture_digest"], record["criteria_digest"],
         record["lineage_digest"]) == split_key
        for record in controlled
    ):
        raise EligibilityError("evaluation evidence already belongs to a controlled corpus")
    if any(
        record["corpus_state"] != case["corpus_state"]
        for record in related
        if record["similarity_group_digest"] is not None
        and record["similarity_group_digest"] == case["similarity_group_digest"]
    ):
        raise EligibilityError("reviewed similarity group already exists in the project corpus")
