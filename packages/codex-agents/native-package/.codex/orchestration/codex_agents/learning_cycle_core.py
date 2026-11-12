"""Bounded, keyless planning for the complete deterministic learning cycle.

The persistence owner selects mining sources, derives actual keyed case identities,
then supplies the complete existing plus newly derived case corpus to the planner.
The planner never derives a case/candidate ID or accepts a digest key. Candidate
specifications are inert suggestions: the owner must recheck revision, authority,
all evidence links and durable eligibility before recording anything.

This module does not activate a service or certify a migrated corpus. It preserves
legacy calculations inside explicit resource envelopes; exhaustion raises rather
than truncating evidence, manufacturing an empty corpus or reporting success.
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractError
from .learning_contracts import (
    candidate_base_digest, canonical_json, content_digest,
    validate_evaluation_case, validate_outcome,
)
from .learning_engine import analyze_outcomes

MAX_OUTCOMES = 10_000
MAX_CASES = 20_000
MAX_INVALIDATED_RUNS = 20_000
MAX_SNAPSHOT_BYTES = 64 * 1_048_576
MAX_PLAN_BYTES = 32 * 1_048_576


class LearningCycleBoundsError(ContractError):
    """A complete calculation cannot fit its explicit resource envelope."""


def _snapshot(outcomes, cases, invalidated):
    for records, maximum, code in (
        (outcomes, MAX_OUTCOMES, "outcome_limit"),
        (cases, MAX_CASES, "case_limit"),
    ):
        if type(records) not in (list, tuple):
            raise ContractError("learning cycle records must be a finite sequence")
        if len(records) > maximum:
            raise LearningCycleBoundsError(code)
    if type(invalidated) not in (list, tuple, set, frozenset):
        raise ContractError("learning cycle invalidations must be a finite collection")
    if len(invalidated) > MAX_INVALIDATED_RUNS:
        raise LearningCycleBoundsError("invalidation_limit")
    if any(type(item) is not str or not 1 <= len(item) <= 256 for item in invalidated):
        raise ContractError("learning cycle invalidation identifier is invalid")
    size = sum(len(item.encode("utf-8")) for item in invalidated)
    for records, validate in ((outcomes, validate_outcome), (cases, validate_evaluation_case)):
        identifiers = set()
        for record in records:
            size += len(canonical_json(record))
            if size > MAX_SNAPSHOT_BYTES:
                raise LearningCycleBoundsError("snapshot_bytes")
            validate(record)
            if record["record_id"] in identifiers:
                raise ContractError("learning cycle record identifier is duplicated")
            identifiers.add(record["record_id"])
    verified = {outcome["run_id"] for outcome in outcomes if outcome["status"] != "unverified"}
    selected = [outcome for outcome in outcomes if not (
        outcome["status"] == "unverified" and outcome["run_id"] in verified
    )]
    return selected, frozenset(invalidated)


def select_mining_outcomes(
    outcomes: list[dict[str, Any]], *, existing_cases: list[dict[str, Any]],
    owner_invalidated_run_ids=(),
) -> list[dict[str, Any]]:
    """Select sources; the owner must derive IDs using the original key domain."""

    selected, invalidated = _snapshot(outcomes, existing_cases, owner_invalidated_run_ids)
    sources = {case["source_outcome_id"] for case in existing_cases}
    mining = [outcome for outcome in selected if not (
        outcome["record_id"] in sources
        or outcome["evaluation_candidate_digest"] is not None
        or outcome["run_id"] in invalidated
        or outcome["status"] == "unverified"
    )]
    if len(existing_cases) + len(mining) > MAX_CASES:
        raise LearningCycleBoundsError("case_limit")
    return mining


def plan_learning_cycle(
    outcomes: list[dict[str, Any]], *, all_cases: list[dict[str, Any]],
    as_of_ms: int, max_age_ms: int, owner_invalidated_run_ids=(),
) -> dict[str, Any]:
    """Analyze a complete owner-bound snapshot and return unkeyed candidate specs.

    all_cases includes actual owner-derived mining metadata, before any protected
    closure or proposal selection. No eligibility receipt substitutes for the
    persistence owner's durable admission, QA, ancestry and authority checks.
    """

    if any(type(value) is not int or value < 0 for value in (as_of_ms, max_age_ms)):
        raise ContractError("learning cycle clock or retention is invalid")
    outcomes, owner_invalidated_run_ids = _snapshot(
        outcomes, all_cases, owner_invalidated_run_ids,
    )
    sources = {case["source_outcome_id"] for case in all_cases}
    if any(
        outcome["record_id"] not in sources
        and outcome["evaluation_candidate_digest"] is None
        and outcome["run_id"] not in owner_invalidated_run_ids
        and outcome["status"] != "unverified"
        for outcome in outcomes
    ):
        raise ContractError("learning_cycle_mining_metadata_missing")
    outcome_pairs = [(outcome, {"content_digest": content_digest(outcome)}) for outcome in outcomes]
    analysis = analyze_outcomes(
        outcomes, as_of_ms=as_of_ms, max_age_ms=max_age_ms,
        owner_invalidated_run_ids=owner_invalidated_run_ids,
    )
    protected_lineages = {
        case["lineage_digest"]
        for case in all_cases
        if case["corpus_state"] != "development"
    }
    protected_similarities = {
        case["similarity_group_digest"]
        for case in all_cases
        if case["corpus_state"] != "development"
        and case["similarity_group_digest"] is not None
    }
    protected_case_ids = {
        identifier
        for case in all_cases
        if case["corpus_state"] != "development"
        for identifier in (case["record_id"], case["supersedes_case_id"])
        if identifier is not None
    }
    changed = True
    while changed:
        changed = False
        for case in all_cases:
            if (
                case["lineage_digest"] in protected_lineages
                or case["similarity_group_digest"] in protected_similarities
                or case["record_id"] in protected_case_ids
                or case["supersedes_case_id"] in protected_case_ids
            ):
                before = len(protected_case_ids)
                protected_lineages.add(case["lineage_digest"])
                if case["similarity_group_digest"] is not None:
                    protected_similarities.add(case["similarity_group_digest"])
                protected_case_ids.add(case["record_id"])
                if case["supersedes_case_id"] is not None:
                    protected_case_ids.add(case["supersedes_case_id"])
                changed = changed or len(protected_case_ids) != before
    controlled_source_ids = {
        case["source_outcome_id"]
        for case in all_cases
        if (
            case["lineage_digest"] in protected_lineages
            or case["similarity_group_digest"] in protected_similarities
            or case["record_id"] in protected_case_ids
        )
    }
    holdout_counts: dict[str, int] = {}
    for case in all_cases:
        if (
            case["corpus_state"] == "promotion_holdout"
            and case["reviewed"] is True
            and case["frozen"] is True
        ):
            holdout_counts[case["task_slice"]] = (
                holdout_counts.get(case["task_slice"], 0) + 1
            )
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    retry_parent = {
        str(outcome["assignment_id"]): outcome.get("retry_of_assignment_id")
        for outcome, _ in outcome_pairs
    }

    def retry_root(outcome: dict[str, Any]) -> str:
        current = str(outcome["assignment_id"])
        seen: set[str] = set()
        while retry_parent.get(current) is not None and current not in seen:
            seen.add(current)
            current = str(retry_parent[current])
        return current

    grouped_lineages: dict[tuple[str, str], set[str]] = {}
    for outcome, row in outcome_pairs:
        if (
            outcome["evaluation_candidate_digest"] is not None
            or outcome["record_id"] in controlled_source_ids
            or outcome["accepted"] is not True
            or outcome.get("semantic_verdict", "pass")
            not in {"pass", "ready", "ready-with-corrections"}
            or outcome["run_id"] in owner_invalidated_run_ids
        ):
            continue
        execution = {
            key: outcome[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                "client_digest",
            )
        }
        group_key = (outcome["task_slice"], content_digest(execution))
        lineage = retry_root(outcome)
        if lineage in grouped_lineages.setdefault(group_key, set()):
            continue
        grouped_lineages[group_key].add(lineage)
        groups.setdefault(group_key, []).append(
            (outcome, str(row["content_digest"]))
        )
    candidate_specs = []
    proposal_status = "configuration_limit" if len(groups) > 64 else "complete"
    groups_to_compare = {} if len(groups) > 64 else groups
    for (task_slice, _), champion_records in sorted(groups_to_compare.items()):
        if (
            holdout_counts.get(task_slice, 0) < 20
            or len(champion_records) < 20
        ):
            continue
        champion = champion_records[0][0]
        champion_config = {
            key: champion[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                "client_digest",
            )
        }
        for (other_slice, _), candidate_records in sorted(groups_to_compare.items()):
            if other_slice != task_slice or candidate_records is champion_records:
                continue
            if len(candidate_records) < 20:
                continue
            champion_by_fixture = {
                item[0]["fixture_digest"]: item for item in champion_records
            }
            candidate_by_fixture = {
                item[0]["fixture_digest"]: item for item in candidate_records
            }
            matched_fixtures = sorted(
                champion_by_fixture.keys() & candidate_by_fixture.keys()
            )
            if len(matched_fixtures) < 20:
                continue
            # Candidate records carry at most 64 evidence digests. Retain
            # one deterministic observation per configuration for each of
            # the most recent 32 matched fixtures.
            matched_fixtures = matched_fixtures[-32:]
            selected_champion_records = [
                champion_by_fixture[fixture] for fixture in matched_fixtures
            ]
            selected_candidate_records = [
                candidate_by_fixture[fixture] for fixture in matched_fixtures
            ]
            challenger = selected_candidate_records[0][0]
            challenger_config = {
                key: challenger[key] for key in champion_config
            }
            unchanged = set(champion_config) - {"model", "effort"}
            if any(champion_config[key] != challenger_config[key] for key in unchanged):
                continue
            champion_outcomes = [item[0] for item in selected_champion_records]
            challenger_outcomes = [item[0] for item in selected_candidate_records]
            both = champion_outcomes + challenger_outcomes
            if not all(
                item["coverage"] == "end_to_end_complete"
                and item["deterministic_pass"]
                and item["security_pass"]
                and item["required_criteria_passed"]
                == item["required_criteria_total"]
                and item["usage"]["input_tokens"] is not None
                and item["usage"]["output_tokens"] is not None
                and item["usage"]["model_calls"] is not None
                for item in both
            ):
                continue

            def mean(records: list[dict[str, Any]], field: str) -> float:
                if field == "tokens":
                    return sum(
                        item["usage"]["input_tokens"]
                        + item["usage"]["output_tokens"]
                        for item in records
                    ) / len(records)
                if field == "model_calls":
                    return sum(item["usage"][field] for item in records) / len(records)
                return sum(item[field] for item in records) / len(records)

            champion_tokens = mean(champion_outcomes, "tokens")
            challenger_tokens = mean(challenger_outcomes, "tokens")
            champion_calls = mean(champion_outcomes, "model_calls")
            challenger_calls = mean(challenger_outcomes, "model_calls")
            if (
                mean(challenger_outcomes, "quality_micros")
                < mean(champion_outcomes, "quality_micros")
                or challenger_tokens > champion_tokens
                or challenger_calls > champion_calls
                or not (
                    challenger_tokens <= champion_tokens * 0.9
                    or challenger_calls <= champion_calls * 0.9
                )
            ):
                continue
            before = {
                "model": champion_config["model"],
                "effort": champion_config["effort"],
            }
            after = {
                "model": challenger_config["model"],
                "effort": challenger_config["effort"],
            }
            if before == after:
                continue
            evidence = sorted(
                item[1] for item in selected_champion_records + selected_candidate_records
            )
            candidate_specs.append({
                "scope": "project", "task_slice": task_slice,
                "target": "model_effort", "effect": "optimize",
                "base_digest": candidate_base_digest(champion_config),
                "champion_configuration": champion_config,
                "change_manifest": [{
                    "path_code": "routing.model_effort",
                    "before_digest": content_digest(before), "after_digest": content_digest(after),
                    "before_value": before, "after_value": after,
                }],
                "evidence_digests": evidence,
                "eligibility_receipt": {
                    "task_slice": task_slice, "evidence_set_digest": content_digest(evidence),
                    "independent_case_count": len(matched_fixtures), "usage_complete": True,
                    "version_unconfounded": True, "task_mix_matched": True,
                    "retry_deduplicated": True, "verifier_evidence_complete": True,
                },
            })
    result = {
        "analysis": analysis, "proposal_status": proposal_status,
        "candidate_specs": candidate_specs,
    }
    if len(canonical_json(result)) > MAX_PLAN_BYTES:
        raise LearningCycleBoundsError("plan_bytes")
    return result
