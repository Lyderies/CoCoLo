"""Whether a recorded outcome is trusted routing evidence for a task.

A task may cite earlier reconnaissance (`routing_evidence_refs`) to be routed
as hard end-to-end work. The citation counts only when the project itself
recorded that run for the same task, as reconnaissance, with a positive result
and accepted operational QA, and the owner has not rejected it. The legacy
ledger answered this for runs it held; this answers it for runs the project's
own service recorded.

The run's original task comes from the service's kept originals (a trusted
adapter supplies the context), never from the request. The answer grants no
launch, capacity or account authority: it only selects a route.
"""

from __future__ import annotations

from .contracts import ContractError
from .history_contracts import HistoryError, _DIGEST, _ID, matches
from .host_result_contracts import HostResultContext
from .learning_contracts import MAX_RECORD_BYTES, content_digest, parse_bounded_json
from .project_learning_owner import rejected_run_ids
from .project_worker_result import current_input_reference

_POSITIVE = frozenset({"pass", "ready", "ready-with-corrections"})


def trusted_reconnaissance(history, context, host_outcome_digest, *, task_id, family):
    """True only for accepted, positive reconnaissance this project recorded for the named task."""
    history._require_owner()
    if (type(context) is not HostResultContext or not matches(host_outcome_digest, _DIGEST)
            or not matches(task_id, _ID) or type(family) is not str):
        raise HistoryError("history_invalid_input")
    _, original = context.snapshot()
    task = original["task"]
    # The kept originals are checked against the run's consumed grant, and the
    # digest against the result that run actually returned.
    current_input_reference(history, context, host_outcome_digest)
    if (task.get("id") != task_id or task.get("family") != family
            or "unknown" not in (task.get("ambiguity"), task.get("context_breadth"))):
        return False
    row = history._connection.execute(
        "SELECT qa_digest,qa,outcome_record_id FROM project_operational_qa "
        "WHERE project_id=? AND worker_outcome_digest=?", (history.project_id, host_outcome_digest)).fetchone()
    if row is None:
        return False
    try:
        review = parse_bounded_json(history._stored_blob(row["qa"], maximum=MAX_RECORD_BYTES))
        if content_digest(review) != row["qa_digest"]:
            raise ValueError()
        outcome = parse_bounded_json(history.record(row["outcome_record_id"]).content)
        parts = outcome["task_slice"].split("-")
        return (review.get("accepted") is True and outcome["record_type"] == "outcome"
                and outcome["accepted"] is True
                and outcome.get("execution_state", "completed") == "completed"
                and outcome.get("semantic_verdict", "pass") in _POSITIVE
                and len(parts) == 7 and parts[0] == family and "unknown" in (parts[4], parts[5])
                and outcome["run_id"] not in rejected_run_ids(history))
    except (ContractError, KeyError, TypeError, ValueError, AttributeError):
        raise HistoryError("history_incompatible") from None
