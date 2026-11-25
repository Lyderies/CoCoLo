"""B2 (decision 92 A): the project service records a finished child's outcome itself.

DESIGN-0.4.0-B2-outcome.md draft 2 with the s2 corrections. Three parts:

- `ServiceRecordAuthority`: the one final predicate that gates every first admission (K1) and every association
  repair (K3) of a result, at the service's own entry and at the primary's. It reads only the store, inside the
  admission transaction, so the admission, the orchestration's close, its interruption, the 24 h idle end and the
  result's expiry meet at one serialization point (the writer's `BEGIN IMMEDIATE`). K2 is the primary's read-only
  retrieval of what is already recorded: typing and the receipt/context association only.
- `project_result_recorders` (schema step 19 -> 20): who recorded a run's outcome, `service` or `primary`, keyed by the
  receipt's record id with no foreign key, kept for the receipt's lifetime. Receipts and records are unchanged.
- `record_pass`: the bounded, fair backstop sweep that finds committed results nobody has recorded (a restart, or a
  record the first try could not make) and records them, and `recording_status` for the status report.

Nothing here is reachable over a socket: the entry points are methods of the project's own writer.
"""
from __future__ import annotations

import re
import json
import threading
import time
from dataclasses import dataclass

from .history_contracts import HistoryError, matches

RECORDERS = ("service", "primary")
PHASES = ("K1", "K2", "K3")
_RECORD_ID = re.compile(r"host_result_admission:[0-9a-f]{64}")

# Step 19 -> 20 (owner decision 92.3 A): a small separate table; the receipt (versions 1 and 2) is unchanged. Keyed by
# the receipt's record id, no foreign key: attribution survives the reservation's retirement.
RECORDER_SCHEMA = """
CREATE TABLE project_result_recorders(
 project_id TEXT NOT NULL,record_id TEXT NOT NULL,
 recorded_by TEXT NOT NULL CHECK(recorded_by IN ('service','primary')),
 recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0),
 PRIMARY KEY(project_id,record_id),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
"""
RECORDER_COLUMNS = {"project_result_recorders": ("project_id", "record_id", "recorded_by", "recorded_at_ms")}

# Refusals the status names `unrecordable:<reason>`; each is a plain reason code of its own.
UNRECORDABLE = {
    "orchestration_closed": "history_unrecordable_orchestration_closed",
    "other_runtime": "history_unrecordable_other_runtime",
    "host_class": "history_unrecordable_host_class",
    "context_state": "history_unrecordable_context_state",
    "legacy_no_context": "history_unrecordable_legacy_no_context",
    "expired": "history_result_expired",
}
REASON_CODES = frozenset(code for code in UNRECORDABLE.values() if code.startswith("history_unrecordable_"))
# A sweep refusal that cannot change until a restart or a state change; the others are retried with backoff.
PERMANENT_REASONS = frozenset(UNRECORDABLE.values()) | {"history_conflict", "history_incompatible",
                                                        "history_not_authorized"}
BACKOFF_FIRST_MS, BACKOFF_LAST_MS, BACKOFF_MAP_LIMIT = 60_000, 1_800_000, 1_000
SWEEP_ATTEMPTS = 10
SWEEP_WINDOW = SWEEP_ATTEMPTS * 16
HEALTH_TRANSIENT_FAILURES = 5


def recorders_consistent(connection) -> bool:
    """The step's own validator: every row names a receipt record id and a supported recorder."""
    for record_id, recorded_by in connection.execute("SELECT record_id,recorded_by FROM project_result_recorders"):
        if not matches(record_id, _RECORD_ID) or recorded_by not in RECORDERS:
            return False
    return True


def insert_recorder(history, record_id: str, recorded_by: str, now_ms: int) -> None:
    """In the first admission's transaction; a plain INSERT, so a second row for one receipt cannot exist."""
    if recorded_by not in RECORDERS or not matches(record_id, _RECORD_ID):
        raise HistoryError("history_invalid_input")
    history._connection.execute("INSERT INTO project_result_recorders VALUES(?,?,?,?)",
                                (history.project_id, record_id, recorded_by, now_ms))


def recorder_of(history, record_id: str) -> str | None:
    """`service`, `primary`, or None (a receipt from before step 20, or none at all)."""
    row = history._connection.execute(
        "SELECT recorded_by FROM project_result_recorders WHERE project_id=? AND record_id=?",
        (history.project_id, record_id)).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True)
class ServiceRecordAuthority:
    """The final predicate (4.3). `runtime_digest` is the writer's own runtime: required at the service's own entry, and
    supplied by the primary's service path; None only for a store used alone (the primary's default instance, owner
    decision 114.2 still open). `service` is True at the service's own entry, where a run with no stored context
    cannot be recorded."""

    runtime_digest: str | None = None
    service: bool = False

    def __post_init__(self):
        if ((self.runtime_digest is not None and not matches(self.runtime_digest, re.compile(r"[0-9a-f]{64}")))
                or type(self.service) is not bool
                or (self.service and self.runtime_digest is None)):  # the service's own entry always has its runtime
            raise HistoryError("history_invalid_input")

    def check(self, history, binding, phase: str, *, reservation=None, receipt=None):
        """Raise a named HistoryError unless this admission may write (K1, K3) or may be read back (K2). K1 and K3
        return the one clock sample the decision was made on, so the admission uses the same instant (None for K2)."""
        from . import project_result_contexts as contexts

        if type(self) is not ServiceRecordAuthority or phase not in PHASES:
            raise HistoryError("history_invalid_input")
        history._require_owner()
        row = contexts._row(history, binding.run_handle, binding.attempt)
        if phase == "K2":
            # Authenticated read-only retrieval: typing and association only, never orchestration state or expiry.
            if row is not None and row["state"] == "recorded":
                if (not contexts._associated(row, binding) or receipt is None
                        or row["outcome_digest"] != receipt.get("host_outcome_digest")):
                    raise HistoryError("history_conflict")
            return
        state = None if row is None else row["state"]
        if row is None:
            if self.service:
                raise HistoryError(UNRECORDABLE["legacy_no_context"])
        elif state == "legacy_no_context":
            raise HistoryError(UNRECORDABLE["legacy_no_context"])
        elif state in ("retiring", "expired", "ended"):
            raise HistoryError(UNRECORDABLE["expired"])
        elif state != "committed" or not contexts._associated(row, binding):
            raise HistoryError(UNRECORDABLE["context_state"])
        if (self.runtime_digest is not None and binding.runtime_digest != self.runtime_digest
                and not self._migrated_finished(history, binding, row)):
            raise HistoryError(UNRECORDABLE["other_runtime"])
        self._orchestration(history, binding, row)
        return self._reservation(history, binding, phase, reservation)

    def _orchestration(self, history, binding, row) -> None:
        from .orchestration_api import HOST_CLIENT_VERSIONS, OrchestrationScope, SCOPE_FIELDS

        scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
        try:
            view = history._orchestration_owner()._current(scope, binding.orchestration_handle, missing_reference=True)
        except HistoryError as error:
            if error.reason_code in {"history_wrong_owner", "history_unavailable", "history_result_unavailable"}:
                raise
            view = False  # not verifiable: refused like a closed one (4.3d)
        if view is None:
            # No checkpoint is kept for this reference. The service never records into it, and neither does any run
            # with a stored context (it was reserved after orchestrations were kept in the store): a missing view is
            # never an open one (4.3d). Only a primary-path run with no stored context (before D1) is let through.
            if self.service or row is not None:
                raise HistoryError(UNRECORDABLE["orchestration_closed"])
            return
        if view is False or view["snapshot"]["status"] not in ("active", "paused"):
            raise HistoryError(UNRECORDABLE["orchestration_closed"])
        snapshot = view["snapshot"]
        if (snapshot["client_version"] not in HOST_CLIENT_VERSIONS
                or (row is not None and row["host_class"] != snapshot["client_version"])):
            raise HistoryError(UNRECORDABLE["host_class"])
        if (self.runtime_digest is not None and snapshot["scope"]["runtime_digest"] != self.runtime_digest
                and not (snapshot["scope"]["runtime_digest"] == binding.runtime_digest
                         and self._migrated_finished(history, binding, row))):
            raise HistoryError(UNRECORDABLE["other_runtime"])

    def _migrated_finished(self, history, binding, row) -> bool:
        """S6/129.2: custody classified under the migration fence permits current recording only.

        This never authorizes publication, an old caller, missing inputs, or a
        different attempt. K1/K3 still verify the context, body, terminal and
        original open orchestration through the ordinary admission path.
        """
        from .learning_contracts import canonical_json, keyed_digest

        if (row is None or row['state'] != 'committed'
                or self.runtime_digest != history._fence.digest):
            return False
        custody = history._connection.execute("SELECT evidence FROM project_holds WHERE project_id=? "
            "AND kind='result_custody' AND identity=? AND state='held'",
            (history.project_id, f'{binding.run_handle}:{binding.attempt}')).fetchone()
        if custody is None:
            return False
        try:
            evidence = json.loads(custody['evidence'])
            _, reservation = history._result_row(binding)
            return (evidence.get('classified') == 22 and evidence.get('reason') == 'legacy_finished_result'
                and evidence.get('binding_digest') == keyed_digest(history._result_key, canonical_json(binding.as_data()))
                and evidence.get('context_digest') == row['context_digest']
                and evidence.get('result_digest') == reservation.result_digest)
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _reservation(history, binding, phase, before) -> None:
        """Re-read inside the transaction (4.2 item 3): committed, the same terminal and result as before the body I/O,
        and `now`, sampled here once, before its deadline and not before the terminal was observed."""
        _, current = history._result_row(binding)
        if current.state == "expired":
            raise HistoryError(UNRECORDABLE["expired"])
        if current.state != "committed" or (before is not None and (
                current.terminal_record_id != before.terminal_record_id or current.result_digest != before.result_digest)):
            raise HistoryError("history_conflict")
        now = history._result_now()
        if now >= current.expires_at_ms:
            raise HistoryError(UNRECORDABLE["expired"])
        return now


# The sweep ---------------------------------------------------------------------------------------------------------

# The selection window: this project's contexts still `committed` whose reservation is committed and live, in row
# order from a cursor, through the live-state index (an ended or expired context is never visited).
WINDOW_SQL = (
    "SELECT c.rowid,c.run_handle,c.attempt FROM project_result_contexts c INDEXED BY project_result_contexts_live "
    "JOIN result_reservations s ON s.project_id=c.project_id AND s.run_handle=c.run_handle AND s.attempt=c.attempt "
    "WHERE c.state='committed' AND c.state NOT IN ('ended','expired') AND c.project_id=? AND c.rowid>? AND s.state='committed' AND s.expires_at_ms>? "
    "ORDER BY c.rowid LIMIT ?")


def record_pass(history, authority: ServiceRecordAuthority, *, limit: int = SWEEP_ATTEMPTS, clock=None,
                notes=None) -> dict:
    """One bounded pass: at most `limit` (never more than SWEEP_ATTEMPTS) recording attempts over a window of
    `limit*16` committed contexts (never more than SWEEP_WINDOW), from a cursor that advances on every attempt (refusals
    too, and a failure too) and wraps. Selection input is only this project's contexts still `committed` with a live
    reservation; the binding comes only from the sealed lease, the reference only from the stored terminal (4.6).

    Each candidate is isolated: a refusal is counted by its reason, and any other error is a transient `unexpected_error`
    (backoff, a health signal) that never stops the pass or loses its progress. The in-memory `permanent` set replaces
    the design 4.6 orchestration prefilter: a candidate refused for a reason that cannot change (a closed orchestration,
    another runtime, ...) is not tried again until its own state changes (its fingerprint: context, reservation and
    orchestration checkpoint), which also frees a refusal that later heals. A selected permanent candidate costs one
    fingerprint query and no admission work. A candidate that turns permanent loses its retry wait and its transient and
    error signals (an integrity signal stays). Transient refusals back off 1 min doubling to 30 min. Returns counts and
    the refusals met."""
    from . import project_result_contexts as contexts

    history._require_owner()
    if type(authority) is not ServiceRecordAuthority or not authority.service:
        raise HistoryError("history_invalid_input")
    if type(limit) is not int or limit < 1:
        raise HistoryError("history_invalid_input")
    limit = min(limit, SWEEP_ATTEMPTS)
    size = min(SWEEP_WINDOW, limit * 16)
    now_ms = (clock or (lambda: time.time_ns() // 1_000_000))()
    state = history._record_sweep
    after = history._purge_cursors.get("record") or 0
    result = {"attempted": 0, "recorded": 0, "refused": {}, "already": 0}
    window = history._connection.execute(WINDOW_SQL, (history.project_id, after, history._result_now(),
                                                      size)).fetchall()
    wrapped = False
    if not window and after:
        wrapped = True
        history._purge_cursors["record"] = None
        window = history._connection.execute(WINDOW_SQL, (history.project_id, 0, history._result_now(),
                                                          size)).fetchall()
    last = None
    for position, run_handle, attempt in window:
        if result["attempted"] >= limit:
            break
        key = (run_handle, attempt)
        last = position
        history._purge_cursors["record"] = position  # progress is kept whatever happens to this candidate
        counted = False
        try:  # the whole candidate operation, its fingerprint reads and refusal handling included, is isolated
            waiting = state["backoff"].get(key)
            if waiting is not None and now_ms < waiting[0]:
                continue
            if key in state["permanent"]:
                if state["permanent"][key][1] == _fingerprint(history, run_handle, attempt):
                    continue
                _forget_permanent(state, key)  # its state changed: it may be recordable now
            result["attempted"] += 1
            counted = True
            try:
                row = contexts._row(history, run_handle, attempt)
                binding = contexts._lease_binding(history, row)
                history.admit_service_result(binding, final_check=authority)
            except HistoryError as error:
                reason = error.reason_code
                if reason in PERMANENT_REASONS:
                    fingerprint = _fingerprint(history, run_handle, attempt)  # read first: a failure here is no refusal
                    result["refused"][reason] = result["refused"].get(reason, 0) + 1
                    while len(state["permanent"]) >= BACKOFF_MAP_LIMIT:
                        _forget_permanent(state, next(iter(state["permanent"])))
                    state["permanent"][key] = (reason, fingerprint)
                    # It will not be retried as it is: its retry state and transient signals end with the change.
                    state["backoff"].pop(key, None)
                    state["health"].difference_update({("transient", key), ("error", key)})
                    if reason in {"history_conflict", "history_incompatible"}:
                        _signal(state, ("integrity", key))
                else:
                    result["refused"][reason] = result["refused"].get(reason, 0) + 1
                    _backoff(state, key, now_ms)
                continue
            state["backoff"].pop(key, None)
            state["health"].difference_update({("transient", key), ("error", key)})  # it recovered
            result["recorded"] += 1
        except Exception:  # any other failure of one candidate: a signal and a later retry, never the pass's end
            if not counted:
                result["attempted"] += 1
            result["refused"]["unexpected_error"] = result["refused"].get("unexpected_error", 0) + 1
            _backoff(state, key, now_ms)
            _signal(state, ("error", key))
    if notes is not None:
        notes.publish(history.project_id, health_signals(history))
    if last is not None:
        history._purge_cursors["record"] = last
    elif not window and not wrapped:
        history._purge_cursors["record"] = None
    return result


def report_unexpected(history, binding, notes) -> None:
    """The publish-time record failed in a way that is not a refusal: keep the run's `error` signal (cleared when the
    sweep records it) and publish the notes, so the health check names it. The publication already stands."""
    _signal(history._record_sweep, ("error", (binding.run_handle, binding.attempt)))
    if notes is not None:
        notes.publish(history.project_id, health_signals(history))


def _fingerprint(history, run_handle, attempt):
    """What a permanent refusal depended on, read cheaply: the context's and the reservation's state and the
    orchestration's latest checkpoint. A change in any of them lets the candidate be tried again."""
    row = history._connection.execute(
        "SELECT c.state,s.state,s.terminal_record_id,s.result_digest,o.sequence,o.checkpoint_digest "
        "FROM project_result_contexts c LEFT JOIN result_reservations s ON s.project_id=c.project_id "
        "AND s.run_handle=c.run_handle AND s.attempt=c.attempt LEFT JOIN project_orchestrations o "
        "ON o.project_id=s.project_id AND o.orchestration_handle=s.orchestration_handle "
        "WHERE c.project_id=? AND c.run_handle=? AND c.attempt=?", (history.project_id, run_handle, attempt)).fetchone()
    return None if row is None else tuple(row)


def _forget_permanent(state, key) -> None:
    state["permanent"].pop(key, None)
    state["health"].discard(("integrity", key))


def _signal(state, signal) -> None:
    """A health signal, kept within the same bound as the other in-memory sweep state."""
    while len(state["health"]) >= BACKOFF_MAP_LIMIT and signal not in state["health"]:
        state["health"].discard(next(iter(state["health"])))
    state["health"].add(signal)


def _backoff(state, key, now_ms) -> None:
    previous = state["backoff"].get(key)
    count = 1 if previous is None else previous[1] + 1
    delay = min(BACKOFF_LAST_MS, BACKOFF_FIRST_MS * (2 ** (count - 1)))
    if previous is None and len(state["backoff"]) >= BACKOFF_MAP_LIMIT:
        dropped = next(iter(state["backoff"]))
        state["backoff"].pop(dropped)
        state["health"].difference_update({("transient", dropped), ("error", dropped)})
    state["backoff"][key] = (now_ms + delay, count)
    if count >= HEALTH_TRANSIENT_FAILURES:
        _signal(state, ("transient", key))


def health_signals(history) -> list:
    """HM-S8 inputs (proposal): an integrity refusal once per claim, an unexpected error once per claim, a transient
    failure after five in a row. A recovered run's signal is cleared; the set is bounded."""
    return sorted(f"{kind}:{key[0]}:{key[1]}" for kind, key in history._record_sweep["health"])


# Health (B2-18, HM-0) ----------------------------------------------------------------------------------------------

class RecordingNotes:
    """What the sweep last published for the health check: a plain list under a lock, so the check never touches the
    store or any ledger."""

    def __init__(self):
        self._lock = threading.Lock()
        self._published = (None, [])

    def publish(self, project_id, signals) -> None:
        with self._lock:
            self._published = (project_id, list(signals))

    def snapshot(self):
        with self._lock:
            return self._published[0], list(self._published[1])


def _health_check_class():
    from .health_monitor import Check, Finding

    class RecordingHealthCheck(Check):
        """HM-0 check `recording`: an integrity refusal of a result is reported once per run, and a run the service
        could not record five times in a row is reported as stuck. Ordinary refusals (a closed orchestration, an
        expired result) are normal outcomes shown in the status, not findings."""

        name = "recording"

        def __init__(self, notes, project_id):
            self.notes, self.project_id = notes, project_id

        def run(self, context):
            project, signals = self.notes.snapshot()
            findings = []
            for signal in signals:
                kind, run_handle, attempt = signal.split(":")
                subject = f"{run_handle}:{attempt}"
                if kind == "integrity":
                    findings.append(Finding(self.name, "project", subject, "reported", "recording_integrity",
                                            "A finished run's result could not be recorded because its stored "
                                            "records do not agree: run learning-status and keep the project's folder as it is.",
                                            self.project_id))
                elif kind == "error":
                    findings.append(Finding(self.name, "project", subject, "reported", "recording_error",
                                            "A finished run's result could not be recorded because of an unexpected "
                                            "error: the service tries again; run learning-status, or restart it.",
                                            self.project_id))
                else:
                    findings.append(Finding(self.name, "project", subject, "reported", "recording_stuck",
                                            "A finished run's result could not be recorded after several tries: "
                                            "run learning-status; the service tries again, or restart it.",
                                            self.project_id))
            return findings
    return RecordingHealthCheck


def __getattr__(name):
    if name == "RecordingHealthCheck":  # built on first use: health_monitor stays out of the writer's imports
        value = _health_check_class()
        globals()[name] = value
        return value
    raise AttributeError(name)


# Status ------------------------------------------------------------------------------------------------------------

def recording_status(history, authority: ServiceRecordAuthority, orchestration_handle: str | None = None) -> dict:
    """Who recorded each committed run, and what awaits a record: `recorded` rows with their recorder (`service`,
    `primary`, or `unknown` for a post-step receipt with no row), `awaiting_record` (committed, none yet) and
    `unrecordable:<reason>` counts. Receipts from before step 20 read `primary (before B2)`. Read-only; bounded by
    the project's contexts that are still held."""
    from . import project_result_contexts as contexts

    history._require_owner()
    status = {"recorded": [], "awaiting_record": 0, "unrecordable": {}}
    rows = history._connection.execute(
        "SELECT run_handle,attempt,state,outcome_digest FROM project_result_contexts INDEXED BY "
        "project_result_contexts_live WHERE project_id=? AND state IN ('committed','recorded') "
        "AND state NOT IN ('ended','expired') LIMIT 1025",
        (history.project_id,)).fetchall()
    if len(rows) > 1024:
        raise HistoryError("history_result_capacity")
    for run_handle, attempt, state, outcome_digest in rows:
        row = contexts._row(history, run_handle, attempt)
        try:
            binding = contexts._lease_binding(history, row)
        except HistoryError:
            continue
        if orchestration_handle is not None and binding.orchestration_handle != orchestration_handle:
            continue
        identity = _receipt_id(history, binding)
        if state == "recorded":
            who = recorder_of(history, identity)
            status["recorded"].append({
                "run_handle": run_handle, "attempt": attempt, "outcome_digest": outcome_digest,
                "recorded_by": who if who is not None else _legacy_label(history, identity),
                "automatic": who == "service",
                "note": "recorded by the service, not reviewed by the primary" if who == "service" else None})
            continue
        try:
            authority.check(history, binding, "K1", reservation=history._result_row(binding)[1])
            status["awaiting_record"] += 1
        except HistoryError as error:
            label = next((name for name, code in UNRECORDABLE.items() if code == error.reason_code), error.reason_code)
            status["unrecordable"][label] = status["unrecordable"].get(label, 0) + 1
    status["lines"] = _lines(status)
    return status


_PLAIN = {
    "orchestration_closed": "its orchestration has ended, so it stays unrecorded",
    "expired": "its result passed its retention time",
    "other_runtime": "it was run by another version of the service",
    "host_class": "it belongs to the other host (Codex or Claude)",
    "context_state": "its stored inputs are not in a state that can be recorded",
    "legacy_no_context": "it was stored by an older version without its inputs",
}


def _lines(status) -> list:
    """Plain sentences for the status: who recorded, what waits, and the real reason something cannot be recorded."""
    lines = []
    service = sum(1 for entry in status["recorded"] if entry["automatic"])
    if service:
        lines.append(f"{service} result(s) recorded by the service, not reviewed by the primary.")
    mine = [entry for entry in status["recorded"] if not entry["automatic"]]
    if mine:
        lines.append(f"{len(mine)} result(s) recorded by the primary.")
    if status["awaiting_record"]:
        lines.append(f"{status['awaiting_record']} finished result(s) waiting to be recorded; the service records them on its next pass.")
    for label, count in sorted(status["unrecordable"].items()):
        lines.append(f"{count} finished result(s) cannot be recorded: {_PLAIN.get(label, label)}.")
    return lines


def _receipt_id(history, binding) -> str:
    from .learning_contracts import canonical_json, keyed_digest

    return "host_result_admission:" + keyed_digest(history._result_key, b"project-host-result-v1\0"
                                                   + canonical_json(binding.as_data()))


def _legacy_label(history, identity: str) -> str:
    """`primary (before B2)` only for a receipt older than step 20; one written since with no row is `unknown`."""
    row = history._connection.execute(
        "SELECT ingested_at_ms FROM records WHERE project_id=? AND record_id=?", (history.project_id, identity)).fetchone()
    stamp = history._connection.execute(
        "SELECT MIN(recorded_at_ms) FROM project_result_recorders WHERE project_id=?", (history.project_id,)).fetchone()[0]
    if row is not None and stamp is not None and row[0] >= stamp:
        return "unknown"
    return "primary (before B2)"
