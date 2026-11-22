"""Pure deterministic outcome analysis shared by the core and persistence owner."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .learning_contracts import validate_outcome

COMPARISON_MIN_CASES = 20


def analyze_outcomes(
    outcomes: Iterable[dict[str, Any]],
    *,
    as_of_ms: int,
    max_age_ms: int,
    owner_invalidated_run_ids: Iterable[str] = (),
) -> dict[str, Any]:
    invalidated = frozenset(owner_invalidated_run_ids)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for raw in outcomes:
        outcome = validate_outcome(raw, stored=True)
        if outcome["run_id"] in invalidated:
            excluded += 1
            continue
        grouped[(outcome["task_slice"], outcome["configuration_digest"])].append(outcome)
    slices: dict[str, Any] = {}
    configuration_counts: dict[str, int] = defaultdict(int)
    for task_slice, _ in grouped:
        configuration_counts[task_slice] += 1
    for (task_slice, configuration_digest), records in sorted(grouped.items()):
        usage_complete = all(
            record["coverage"] == "end_to_end_complete"
            and record["usage"]["input_tokens"] is not None
            and record["usage"]["output_tokens"] is not None
            and record["usage"]["model_calls"] is not None
            for record in records
        )
        token_volume = (
            sum(
                int(record["usage"]["input_tokens"])
                + int(record["usage"]["output_tokens"])
                for record in records
            )
            if usage_complete
            else None
        )
        model_calls = (
            sum(int(record["usage"]["model_calls"]) for record in records)
            if usage_complete
            else None
        )
        latency_complete = all(record["usage"]["latency_ms"] is not None for record in records)
        tool_complete = all(record["usage"]["tool_calls"] is not None for record in records)
        workflow_latency = (
            sum(int(record["usage"]["latency_ms"]) for record in records)
            if latency_complete
            else None
        )
        tool_calls = (
            sum(int(record["usage"]["tool_calls"]) for record in records)
            if tool_complete
            else None
        )
        stale = any(as_of_ms - record["observed_at_ms"] > max_age_ms for record in records)
        confounded = configuration_counts[task_slice] > 1
        insufficient = len(records) < COMPARISON_MIN_CASES
        diagnoses = []
        alternatives = []
        if not usage_complete or not latency_complete or not tool_complete:
            diagnoses.append("telemetry_incomplete")
            alternatives.append("client_coverage_or_collection_gap")
        if stale:
            diagnoses.append("stale_evidence")
            alternatives.append("historical_behavior_may_not_match_current_package")
        if insufficient:
            diagnoses.append("small_sample")
            alternatives.append("task_mix_variance")
        if confounded:
            diagnoses.append("version_confounding")
            alternatives.append("configuration_change")
        section = {
            "observations": {
                "trials": len(records),
                "accepted": sum(record["accepted"] for record in records),
                "terminal_failures": sum(record["terminal_failure"] for record in records),
                "repairs": sum(record["repairs"] for record in records),
                "verifier_disagreements": sum(
                    record["verifier_disagreement"] for record in records
                ),
                "mean_quality_micros": round(
                    statistics.mean(record["quality_micros"] for record in records)
                ),
                "workflow_token_volume": token_volume,
                "workflow_model_calls": model_calls,
                "workflow_latency_ms": workflow_latency,
                "workflow_tool_calls": tool_calls,
                "delegation_overhead_tokens": sum(
                    record["delegation_overhead_tokens"] for record in records
                ),
                "no_progress_events": sum(record["no_progress_events"] for record in records),
            },
            "evidence_flags": {
                "usage_complete": usage_complete,
                "stale": stale,
                "latency_complete": latency_complete,
                "tool_usage_complete": tool_complete,
                "insufficient_sample": insufficient,
                "version_confounding": confounded,
            },
            "diagnoses": diagnoses,
            "alternative_explanations": alternatives,
        }
        slices.setdefault(task_slice, {"configurations": {}})["configurations"][
            configuration_digest
        ] = section
    return {
        "schema_version": 1,
        "as_of_ms": as_of_ms,
        "owner_feedback_excluded": excluded,
        "slices": slices,
    }
