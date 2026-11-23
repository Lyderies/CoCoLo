"""Content-free statistics for one orchestration the project's own service ran.

The ledger answered `learning-orchestration-report` for orchestrations it
held. Orchestrations, their runs, outcomes and QA now live in the project
store, so the same report is computed here: counts, usage totals and
durations only, never task text, results or paths.
"""

from __future__ import annotations

import re

from .history_contracts import HistoryError, matches
from .learning_adapters import TOKEN_USAGE_FIELDS, empty_usage
from .learning_contracts import MAX_RECORD_BYTES, external_id, parse_bounded_json

MAX_REPORTED_CHECKPOINTS = 64
MAX_RUNS = 512  # Runs of one orchestration.
_PAGE = 1_024  # handles read per page while finding one orchestration
_ORCHESTRATION_ID = re.compile(r"orc:[0-9a-f]{64}")


def _json(history, blob, maximum=MAX_RECORD_BYTES):
    return parse_bounded_json(history._stored_blob(blob, maximum=maximum))


def _orchestration(history, orchestration_id):
    """The named orchestration's handle and its latest snapshots. O0c P10: the handle is found by its keyed id
    (external_id of the handle), paging through handles with no count of the project's orchestrations; no
    checkpoint is parsed until the match."""
    latest = "SELECT content FROM project_orchestration_checkpoints WHERE project_id=? " \
             "AND orchestration_handle=? ORDER BY sequence DESC LIMIT ?"
    key, after = history._result_key, ""
    while True:
        handles = history._connection.execute(
            "SELECT orchestration_handle FROM project_orchestrations WHERE project_id=? AND orchestration_handle>? "
            "ORDER BY orchestration_handle LIMIT ?", (history.project_id, after, _PAGE)).fetchall()
        for (handle,) in handles:
            if key is not None:
                if external_id(key, "orc", handle) != orchestration_id:
                    continue
            else:  # no key (never for a running service): the checkpoint says
                row = history._connection.execute(latest, (history.project_id, handle, 1)).fetchone()
                if row is None or _json(history, row[0])["snapshot"].get("orchestration_id") != orchestration_id:
                    continue
            rows = history._connection.execute(
                latest, (history.project_id, handle, MAX_REPORTED_CHECKPOINTS)).fetchall()
            if not rows or _json(history, rows[0][0])["snapshot"].get("orchestration_id") != orchestration_id:
                continue
            count = history._connection.execute(
                "SELECT COUNT(*) FROM project_orchestration_checkpoints WHERE project_id=? "
                "AND orchestration_handle=?", (history.project_id, handle)).fetchone()[0]
            return handle, [_json(history, item[0])["snapshot"] for item in reversed(rows)], count
        if len(handles) < _PAGE:
            raise HistoryError("history_not_found")
        after = handles[-1][0]


def orchestration_report(history, orchestration_id, runtime_digest=None):
    """Counts for one orchestration. The work in this process is bounded by the
    orchestration's own runs (at most `MAX_RUNS`): rows are selected by SQLite and
    only this orchestration's are parsed here; a larger one is refused."""
    history._require_owner()
    if not matches(orchestration_id, _ORCHESTRATION_ID):
        raise HistoryError("history_invalid_input")
    try:
        handle, snapshots, checkpoint_count = _orchestration(history, orchestration_id)
        current = snapshots[-1]
        # With each run's result lease state: an expired lease with no result is a run that closed
        # without one (denied at its claim, fenced, or closed by the next service; 0.3.3, C7).
        opens = history._connection.execute(
            "SELECT json_extract(CAST(o.binding AS TEXT),'$.run_id'),o.ingested_at_ms,o.source_sequence,l.state "
            "FROM project_host_opens o LEFT JOIN result_leases l ON l.project_id=o.project_id "
            "AND l.run_handle=o.run_handle AND l.attempt=o.attempt WHERE o.project_id=? "
            "AND json_extract(CAST(o.binding AS TEXT),'$.orchestration_handle')=? ORDER BY o.source_sequence LIMIT ?",
            (history.project_id, handle, MAX_RUNS + 1)).fetchall()
        if len(opens) > MAX_RUNS:
            raise HistoryError("history_learning_capacity")
        runs = {run_id: opened_at for run_id, opened_at, _, _ in opens if type(run_id) is str}
        # S1-R6: runs the driver registered; one with no host open either failed before it (its fixed
        # reason) or is still unresolved. A run with a host open is counted by its open only.
        registered = history._connection.execute(
            "SELECT run_id,attempt,failed_reason FROM project_registered_runs INDEXED BY "
            "project_registered_runs_orchestration WHERE project_id=? AND orchestration_handle=? LIMIT ?",
            (history.project_id, handle, MAX_RUNS + 1)).fetchall()
        if len(registered) > MAX_RUNS:
            raise HistoryError("history_learning_capacity")
        # R6-11: a run's attempts combine the same in any row order: any unresolved attempt keeps it
        # pending (None); else it failed with its highest attempt's reason.
        attempts = {}
        for run_id, attempt, reason in registered:
            if type(run_id) is str and run_id not in runs:
                attempts.setdefault(run_id, []).append((attempt, reason))
        before_open = {run_id: (None if any(reason is None for _, reason in found) else max(found)[1])
                       for run_id, found in attempts.items()}
        if len(runs) + len(before_open) > MAX_RUNS:  # R6-12: opens and registrations together
            raise HistoryError("history_learning_capacity")
        # Every attempt's lease expired: one still open could yet record a result.
        expired = {run_id for run_id, _, _, _ in opens if type(run_id) is str} - {
            run_id for run_id, _, _, state in opens if state != 'expired'}
        outcomes, recorded_at, reviewed = {}, {}, {}
        if runs:
            names = ",".join("?" for _ in runs)
            # A run's first outcome is the host's; an accepted QA supersedes it with a verified
            # one. Its records come after its open, and it has at most a few.
            rows = history._connection.execute(
                "SELECT content,ingested_at_ms FROM records INDEXED BY records_learning_outcomes "
                "WHERE project_id=? AND record_type='outcome' AND source_sequence>? "
                f"AND json_extract(CAST(content AS TEXT),'$.run_id') IN ({names}) "
                "ORDER BY source_sequence LIMIT ?",
                (history.project_id, opens[0][2], *runs, 4 * MAX_RUNS + 1)).fetchall()
            if len(rows) > 4 * MAX_RUNS:
                raise HistoryError("history_learning_capacity")
            for content, ingested in rows:
                outcome = _json(history, content)
                recorded_at.setdefault(outcome["run_id"], ingested)
                outcomes[outcome["run_id"]] = outcome
            for qa, run_id in history._connection.execute(
                    "SELECT q.qa,json_extract(CAST(r.content AS TEXT),'$.run_id') FROM project_operational_qa q "
                    "JOIN records r ON r.project_id=q.project_id AND r.record_id=q.outcome_record_id "
                    f"WHERE q.project_id=? AND json_extract(CAST(r.content AS TEXT),'$.run_id') IN ({names}) "
                    "LIMIT ?", (history.project_id, *runs, MAX_RUNS + 1)):
                reviewed[run_id] = _json(history, qa).get("accepted") is True
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise HistoryError("history_incompatible") from None
    # B2-10: who recorded each outcome, what waits, what cannot be recorded (plain words in `lines`).
    try:
        from .service_record import ServiceRecordAuthority, recording_status
        # The writer's own runtime decides what this service could record; a store read alone has none.
        authority = ServiceRecordAuthority(runtime_digest=runtime_digest, service=runtime_digest is not None)
        recording = recording_status(history, authority, handle)
    except HistoryError:
        recording = None  # too many held results to list: the counts above still stand
    usage_fields = tuple(empty_usage())
    totals = {field: 0 for field in usage_fields}
    unknown = {field: 0 for field in usage_fields}
    by_model, by_effort, by_role, statuses = {}, {}, {}, {}
    repairs = no_progress = astra = passed = failed = pending = 0
    durations = []
    for run_id, opened_at in runs.items():
        outcome = outcomes.get(run_id)
        if outcome is None and run_id in expired:
            # Closed without a result: counted as abandoned below, and its usage is not known.
            for field in usage_fields:
                unknown[field] += 1
            continue
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
    abandoned = len(expired - set(outcomes))
    failed_reasons = {}
    for reason in before_open.values():
        if reason is not None:
            failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
    failed_before_open = sum(failed_reasons.values())
    unresolved = len(before_open) - failed_before_open
    goal_known = current["goal_tokens_used"] is not None or current["goal_time_used_seconds"] is not None
    return {
        "recording": recording,
        "orchestration_id": orchestration_id, "status": str(current["status"]),
        "started_at_ms": int(current["started_at_ms"]), "ended_at_ms": ended,
        "duration_ms": None if ended is None else int(ended) - int(current["started_at_ms"]),
        "runs": {"opened": len(runs), "admitted": len(runs) + len(before_open), "terminal": recorded,
                 "pending": len(runs) - recorded - abandoned + unresolved,
                 "abandoned": abandoned + failed_before_open, "failed_before_open": failed_before_open,
                 "failed_before_open_reasons": dict(sorted(failed_reasons.items())),
                 "current_active": (len(runs) - recorded - abandoned + unresolved
                                    if current["status"] == "active" else 0),
                 "agent_bound": len(runs), "deadlines_observed": 0},
        "checkpoint_count": checkpoint_count,
        "checkpoints": [{"sequence": int(item["sequence"]), "status": str(item["status"]),
                         "goal_tokens_used": item["goal_tokens_used"],
                         "goal_time_used_seconds": item["goal_time_used_seconds"],
                         "observed_at_ms": int(item["observed_at_ms"])}
                        for item in snapshots],
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
                  "child_attribution": "complete" if runs and all(unknown[field] == 0 for field in TOKEN_USAGE_FIELDS)
                  else "unknown"},
    }
