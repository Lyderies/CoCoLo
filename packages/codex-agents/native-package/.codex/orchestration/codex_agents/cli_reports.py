"""Presentation of optional learning-cycle metadata, never result/QA admission.

Only the three commands that attach an incidental learning cycle use this
projection. Full mode returns the original object. This module performs no I/O.
"""

from __future__ import annotations

MAX_ANALYSIS_GROUPS = 10_000
MAX_CYCLE_RECORDS = 20_000
MAX_DIAGNOSES_PER_GROUP = 16
DIAGNOSES = (
    "small_sample", "stale_evidence", "telemetry_incomplete", "version_confounding",
)
COMPACT_CYCLE_COMMANDS = frozenset({
    "learning-record-host-result", "learning-record-operational-qa", "learning-review-case",
})


class _Unavailable(Exception):
    pass


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise _Unavailable
    return value


def _analysis_summary(analysis):
    if (type(analysis) is not dict or len(analysis) != 4 or set(analysis) != {
        "schema_version", "as_of_ms", "owner_feedback_excluded", "slices",
    } or type(analysis["schema_version"]) is not int or analysis["schema_version"] != 1):
        raise _Unavailable
    timestamp = _integer(analysis["as_of_ms"])
    excluded = _integer(analysis["owner_feedback_excluded"])
    slices = analysis["slices"]
    if type(slices) is not dict or len(slices) > MAX_ANALYSIS_GROUPS:
        raise _Unavailable
    configurations = other_diagnoses = 0
    counts = dict.fromkeys(DIAGNOSES, 0)
    for section in slices.values():
        if type(section) is not dict or type(section.get("configurations")) is not dict:
            raise _Unavailable
        groups = section["configurations"]
        configurations += len(groups)
        if configurations > MAX_ANALYSIS_GROUPS:
            raise _Unavailable
        for group in groups.values():
            if type(group) is not dict or type(group.get("diagnoses")) is not list:
                raise _Unavailable
            diagnoses = group["diagnoses"]
            if len(diagnoses) > MAX_DIAGNOSES_PER_GROUP:
                raise _Unavailable
            for diagnosis in diagnoses:
                if type(diagnosis) is not str or len(diagnosis) > 80:
                    raise _Unavailable
                if diagnosis in counts:
                    counts[diagnosis] += 1
                else:
                    other_diagnoses += 1
    return {
        "as_of_ms": timestamp, "owner_feedback_excluded": excluded,
        "slice_count": len(slices), "configuration_count": configurations,
        "diagnosis_counts": counts, "other_diagnosis_count": other_diagnoses,
    }


def learning_cycle_summary(cycle):
    """Fixed-key summary; unsupported metadata is unknown, never zero/success."""
    base = {
        "format": "compact-learning-cycle-v1", "full_report_option": "--full-report",
        "omitted_fields": ["analysis.slices", "mined_case_ids", "proposed_candidate_digests"],
    }
    try:
        if type(cycle) is not dict or len(cycle) != 4 or set(cycle) != {
            "analysis", "mined_case_ids", "proposed_candidate_digests", "proposal_status",
        }:
            raise _Unavailable
        analysis = _analysis_summary(cycle["analysis"])
        lengths = {}
        for key in ("mined_case_ids", "proposed_candidate_digests"):
            if type(cycle[key]) is not list or len(cycle[key]) > MAX_CYCLE_RECORDS:
                raise _Unavailable
            if any(type(item) is not str or not item or len(item) > 160 for item in cycle[key]):
                raise _Unavailable
            lengths[key] = len(cycle[key])
        status = cycle["proposal_status"]
        if type(status) is not str or status not in {"complete", "configuration_limit"}:
            raise _Unavailable
        return base | {
            "summary_state": "available", "analysis_summary": analysis,
            "mined_case_count": lengths["mined_case_ids"],
            "proposed_candidate_count": lengths["proposed_candidate_digests"],
            "proposal_status": status,
        }
    except _Unavailable:
        # A presentation failure after a successful commit cannot become a
        # false commit failure or an invented zero count/acceptance decision.
        return base | {
            "summary_state": "unavailable", "reason_code": "learning_summary_unavailable",
            "analysis_summary": None, "mined_case_count": None,
            "proposed_candidate_count": None, "proposal_status": None,
        }


def unavailable_cycle(reason_code="learning_summary_unavailable"):
    """The fixed shape for a cycle that produced no report; never zero counts or success."""
    return {
        "format": "compact-learning-cycle-v1", "full_report_option": "--full-report",
        "omitted_fields": ["analysis.slices", "mined_case_ids", "proposed_candidate_digests"],
        "summary_state": "unavailable", "reason_code": reason_code,
        "analysis_summary": None, "mined_case_count": None,
        "proposed_candidate_count": None, "proposal_status": None,
    }


def command_report(command, report, *, full=False):
    """Preserve every field outside the optional learning_cycle verbatim."""
    if (full or command not in COMPACT_CYCLE_COMMANDS
            or type(report) is not dict or "learning_cycle" not in report):
        return report
    cycle = report["learning_cycle"]
    if type(cycle) is dict and cycle.get("format") == "compact-learning-cycle-v1":
        return report  # The project's learning owner already summarised it.
    return report | {"learning_cycle": learning_cycle_summary(report["learning_cycle"])}
