"""Content-free statistics for one orchestration the project's own service ran.

The ledger answered `learning-orchestration-report` for orchestrations it
held. Orchestrations, their runs, outcomes and QA now live in the project
store, so the same report is computed here: counts, usage totals and
durations only, never task text, results or paths.
"""

from __future__ import annotations

import re

from .history_contracts import HistoryError, matches
from .learning_adapters import empty_usage
from .learning_contracts import MAX_RECORD_BYTES, parse_bounded_json

MAX_REPORTED_CHECKPOINTS = 64
MAX_RUNS = 4_096
_ORCHESTRATION_ID = re.compile(r"orc:[0-9a-f]{64}")


def _json(history, blob, maximum=MAX_RECORD_BYTES):
    return parse_bounded_json(history._stored_blob(blob, maximum=maximum))


def _orchestration(history, orchestration_id):
    for (handle,) in history._connection.execute(
            "SELECT orchestration_handle FROM project_orchestrations WHERE project_id=?", (history.project_id,)):
        rows = history._connection.execute(
            "SELECT content FROM project_orchestration_checkpoints WHERE project_id=? AND orchestration_handle=? "
            "ORDER BY sequence", (history.project_id, handle)).fetchall()
        snapshots = [_json(history, row[0])["snapshot"] for row in rows]
        if snapshots and snapshots[-1].get("orchestration_id") == orchestration_id:
            return handle, snapshots
    raise HistoryError("history_not_found")


def orchestration_report(history, orchestration_id):
    history._require_owner()
    if not matches(orchestration_id, _ORCHESTRATION_ID):
        raise HistoryError("history_invalid_input")
    try:
        handle, snapshots = _orchestration(history, orchestration_id)
        current = snapshots[-1]
        opens = history._connection.execute(
            "SELECT binding,ingested_at_ms FROM project_host_opens WHERE project_id=? ORDER BY source_sequence LIMIT ?",
            (history.project_id, MAX_RUNS + 1)).fetchall()
        if len(opens) > MAX_RUNS:
            raise HistoryError("history_learning_capacity")
        runs = {}
        for binding, opened_at in opens:
            value = _json(history, binding)
            if value.get("orchestration_handle") == handle:
                runs[value["run_id"]] = opened_at
        outcomes, recorded_at = {}, {}
        # A run's first outcome is the host's; an accepted QA supersedes it with a verified one.
        for content, ingested in history._connection.execute(
                "SELECT content,ingested_at_ms FROM records INDEXED BY records_learning_outcomes "
                "WHERE project_id=? AND record_type='outcome' ORDER BY source_sequence", (history.project_id,)):
            outcome = _json(history, content)
            if outcome["run_id"] in runs:
                recorded_at.setdefault(outcome["run_id"], ingested)
                outcomes[outcome["run_id"]] = outcome
        reviewed = {}
        for qa, outcome_record_id in history._connection.execute(
                "SELECT qa,outcome_record_id FROM project_operational_qa WHERE project_id=?", (history.project_id,)):
            row = history._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=?",
                (history.project_id, outcome_record_id)).fetchone()
            if row is not None:
                reviewed[_json(history, row[0])["run_id"]] = _json(history, qa).get("accepted") is True
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HistoryError("history_incompatible") from None
    usage_fields = tuple(empty_usage())
    totals = {field: 0 for field in usage_fields}
    unknown = {field: 0 for field in usage_fields}
    by_model, by_effort, by_role, statuses = {}, {}, {}, {}
    repairs = no_progress = astra = passed = failed = pending = 0
    durations = []
    for run_id, opened_at in runs.items():
        outcome = outcomes.get(run_id)
        if outcome is None:
            statuses["pending"] = statuses.get("pending", 0) + 1
            pending += 1
            for field in usage_fields:
                unknown[field] += 1
            continue
        durations.append(max(0, recorded_at[run_id] - opened_at))
        if outcome["task_slice"].startswith("verification-"):
            pass
        elif run_id not in reviewed:
            pending += 1
        elif reviewed[run_id]:
            passed += 1
        else:
            failed += 1
        for field, target in (("model", by_model), ("effort", by_effort), ("role", by_role)):
            target[str(outcome[field])] = target.get(str(outcome[field]), 0) + 1
        statuses[str(outcome["status"])] = statuses.get(str(outcome["status"]), 0) + 1
        repairs += int(outcome["repairs"])
        no_progress += int(outcome["no_progress_events"])
        astra += int(outcome["model"] == "gpt-6-astra")
        for field in usage_fields:
            value = outcome["usage"][field]
            if value is None:
                unknown[field] += 1
                totals[field] = None
            elif totals[field] is not None:
                totals[field] += int(value)
    ended = current["ended_at_ms"]
    recorded = len(outcomes)
    goal_known = current["goal_tokens_used"] is not None or current["goal_time_used_seconds"] is not None
    return {
        "orchestration_id": orchestration_id, "status": str(current["status"]),
        "started_at_ms": int(current["started_at_ms"]), "ended_at_ms": ended,
        "duration_ms": None if ended is None else int(ended) - int(current["started_at_ms"]),
        "runs": {"opened": len(runs), "terminal": recorded, "pending": len(runs) - recorded, "abandoned": 0,
                 "current_active": len(runs) - recorded if current["status"] == "active" else 0,
                 "agent_bound": len(runs), "deadlines_observed": 0},
        "checkpoint_count": len(snapshots),
        "checkpoints": [{"sequence": int(item["sequence"]), "status": str(item["status"]),
                         "goal_tokens_used": item["goal_tokens_used"],
                         "goal_time_used_seconds": item["goal_time_used_seconds"],
                         "observed_at_ms": int(item["observed_at_ms"])}
                        for item in snapshots[-MAX_REPORTED_CHECKPOINTS:]],
        "by_model": dict(sorted(by_model.items())), "by_effort": dict(sorted(by_effort.items())),
        "by_role": dict(sorted(by_role.items())), "by_agent_type": {"bridge": len(runs)} if runs else {},
        "outcome_status": dict(sorted(statuses.items())), "repairs": repairs,
        "no_progress_events": no_progress, "astra_runs": astra,
        "qa": {"passed": passed, "failed": failed, "pending": pending,
               "attention_required": failed > 0 or pending > 0},
        "run_duration_ms": {"total": sum(durations), "minimum": min(durations) if durations else None,
                            "maximum": max(durations) if durations else None},
        "usage": {"host_run_totals": totals, "unknown_runs": unknown,
                  "goal_total_tokens": current["goal_tokens_used"],
                  "goal_time_used_seconds": current["goal_time_used_seconds"],
                  "goal_usage_source": "host_reported" if goal_known else "unavailable",
                  "child_attribution": "complete" if runs and all(count == 0 for count in unknown.values())
                  else "unknown"},
    }
