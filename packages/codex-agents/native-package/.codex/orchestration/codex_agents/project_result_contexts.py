"""O0b run supervisor, slice D1 (RS-D D5): a run's record context, in the project store beside its result.

Until D1 the inputs a record needs (the run's assignment, task, route, the client capability it was prepared with and
the host class that opened it) lived only in the service's memory, so a stopped service could no longer record a
result whose body was still on disk (ANALYSIS-0.4.0-supervisor-approach.md D5). Now the project writer keeps them in
one private file per attempt, `.context-<keyed id>.json` (0600, partial then link, fsync, as the result body), with
its row in `project_result_contexts` and its `result_custody` hold (DESIGN-0.4.0-RS-D.md draft 2 §1-§5, draft 3 §3-§6;
DESIGN-0.4.0-RS-BD-shared.md SH-3):

- stored with the lease, in its transaction, before the lease can be armed (`leased`);
- verified at host open against the inputs the open is recorded with (`opened`);
- bound to the claim at bind (`bound`), committed with the result (`committed`), and associated with its outcome
  digest in the admission's own transaction (`recorded`);
- read back by every later reader through `stored_result_context`, `stored_worker_context` and
  `stored_attempt_context`, each checked against the lease, the claim association and the consumed grant;
- ended with its lease when the attempt ends unfinished (custody `lost_with_reservation`, context `ended`), and
  retired with its result body otherwise (custody `retention` or `orchestration_closed`, context `expired`).

A context is a file, never a SQLite blob. Each is at most MAX_RECORD_CONTEXT_BYTES, and the live ones together at
most MAX_LIVE_CONTEXT_BYTES, counted from the rows not `ended` or `expired` (SH-6). No reader is given bytes once a
context's retirement began. D3 (RS-D draft 3 §4-§6, draft 4 §1): a retirement unlinks the fixed names without
opening them (`retire_names`); only a directory under a fixed name needs repair (`repair_required`, still charged,
shown by `repairs`, retried every pass); `sweep` finishes the context-only custody (no reservation row) that the
reservation-backed retirement never reaches. Functions here take the owning ProjectHistory and run on its writer thread; the ones
marked "in the transaction" run inside the caller's store transaction.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import stat

from .history_contracts import AttemptBinding, HistoryError, _DIGEST, matches
from .host_result_contracts import MAX_HOST_RESULT_CONTEXT_BYTES, HostResultContext, RecordContext
from .learning_contracts import canonical_json, route_identity, content_digest, keyed_digest, parse_bounded_json
from . import project_holds_schema
from .project_holds_schema import attempt_identity, evidence_json

# Room for the binding a reader adds when it builds the HostResultContext (MAX_BINDING_BYTES, with labels).
MAX_RECORD_CONTEXT_BYTES = MAX_HOST_RESULT_CONTEXT_BYTES - 8_192
# RS-D-8 (owner question: 256 MiB proposed): what the live contexts of one project may hold together.
MAX_LIVE_CONTEXT_BYTES = 256 * 1_048_576
# RS-D-5 (owner decision 90.4 A): a verifier opens only when its worker's retention outlasts the verifier's lease deadline
# by this much. Checked once, at open; nothing is extended.
QA_MARGIN_MS = 3_600_000
# The states whose context file exists (or is being removed); `legacy_no_context` never had one.
FILE_STATES = ("leased", "opened", "bound", "committed", "recorded", "retiring", "repair_required")
_GONE = ("retiring", "expired", "ended", "repair_required")
_PREFIX = ".context-"


def names(history, run_handle: str, attempt: int) -> tuple[str, str]:
    identity = keyed_digest(history._result_key, b"project-result-context-v1\0"
                            + canonical_json([history.project_id, run_handle, attempt]))
    return f"{_PREFIX}{identity}.partial", f"{_PREFIX}{identity}.json"


def _row(history, run_handle: str, attempt: int):
    return history._connection.execute(
        "SELECT * FROM project_result_contexts WHERE project_id=? AND run_handle=? AND attempt=?",
        (history.project_id, run_handle, attempt)).fetchone()


def live_bytes(history) -> int:
    """SH-6: the context bytes charged now, from the source rows (never a counter)."""
    return history._connection.execute(
        "SELECT COALESCE(SUM(context_size),0) FROM project_result_contexts INDEXED BY project_result_contexts_live "
        "WHERE state NOT IN ('ended','expired')").fetchone()[0]


def _check_budget(history, size: int) -> None:
    if live_bytes(history) + size > MAX_LIVE_CONTEXT_BYTES:
        raise HistoryError("history_result_capacity")


# Files -------------------------------------------------------------------------------------------------------------

def _remove_entry(history, name: str) -> bool:
    """Unlink one fixed name without opening or following it (RS-D draft 3 §4); a directory is never removed."""
    try:
        info = os.stat(name, dir_fd=history._directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode):
        raise HistoryError("history_unsafe_path")
    os.unlink(name, dir_fd=history._directory_fd)
    return True


def _write(history, run_handle: str, attempt: int, content: bytes) -> None:
    partial, final = names(history, run_handle, attempt)
    for name in (partial, final):  # what an attempt that never committed its row left (O1)
        _remove_entry(history, name)
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=history._directory_fd)
    try:
        os.fchmod(fd, 0o600)
        pending = memoryview(content)
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("context write failed")
            pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.link(partial, final, src_dir_fd=history._directory_fd, dst_dir_fd=history._directory_fd, follow_symlinks=False)
    os.unlink(partial, dir_fd=history._directory_fd)
    os.fsync(history._directory_fd)


def _unlink(history, run_handle: str, attempt: int) -> None:
    for name in names(history, run_handle, attempt):
        _remove_entry(history, name)
    os.fsync(history._directory_fd)


def _read(history, row) -> dict:
    """The stored inputs, exactly as written: owner-only regular file, its size and keyed digest as recorded."""
    _, final = names(history, row["run_handle"], row["attempt"])
    try:
        fd = os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=history._directory_fd)
    except FileNotFoundError:
        raise HistoryError("history_result_unavailable") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1):
            raise HistoryError("history_unsafe_path")
        content = stream.read(row["context_size"] + 1)
    if (len(content) != row["context_size"] or not matches(row["context_digest"], _DIGEST)
            or not secrets.compare_digest(keyed_digest(history._result_key, content), row["context_digest"])):
        raise HistoryError("history_conflict")
    value = parse_bounded_json(content, max_bytes=MAX_RECORD_CONTEXT_BYTES)
    if (type(value) is not dict or value.keys() != {"assignment", "task", "route", "client_capability", "host_class",
                                                    "inputs_digest"}
            or value["host_class"] != row["host_class"] or canonical_json(value) != content):
        raise HistoryError("history_conflict")
    return value


# The lease: stored before the arm ----------------------------------------------------------------------------------

def prepare(history, encoded_assignment: bytes, record_context) -> tuple[bytes, str]:
    """Before the lease transaction: the exact bytes and keyed digest; an oversized context is refused (O5)."""
    if type(record_context) is not RecordContext:
        raise HistoryError("history_invalid_input")
    content = record_context.content(parse_bounded_json(encoded_assignment))
    if not 0 < len(content) <= MAX_RECORD_CONTEXT_BYTES:
        raise HistoryError("history_invalid_input")
    return content, keyed_digest(history._result_key, content)


def known(history, run_handle: str, attempt: int, digest: str | None) -> None:
    """A known lease is never given another context: a retry is exactly the same, or nothing (no second charge)."""
    row = _row(history, run_handle, attempt)
    if digest is None:
        return
    if row is None or row["context_digest"] is None or not secrets.compare_digest(row["context_digest"], digest):
        raise HistoryError("history_conflict")


def store(history, run_handle: str, attempt: int, content: bytes) -> None:
    """Before the lease transaction: the file, once the budget allows it (checked again inside the transaction)."""
    if _row(history, run_handle, attempt) is not None:
        raise HistoryError("history_conflict")
    try:
        _check_budget(history, len(content))
    except HistoryError:
        # RS-D-8: reclaim on allocation first, as for result bodies (0.3.4 R8): retired custody frees its bytes.
        from .project_history import RECLAIM_BATCH
        try:
            history.expire_results(limit=RECLAIM_BATCH)
        except HistoryError:
            pass
        _check_budget(history, len(content))
    _write(history, run_handle, attempt, content)


def insert(history, run_handle: str, attempt: int, digest: str, size: int, host_class: str, *,
           lease_deadline_ms: int, now_ms: int) -> None:
    """In the lease transaction: the row and its custody hold (SH-3 "taken"), charged against the live budget."""
    _check_budget(history, size)
    history._connection.execute(
        "INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_digest,context_size,"
        "host_class,lease_deadline_ms) VALUES(?,?,?,'leased',?,?,?,?)",
        (history.project_id, run_handle, attempt, digest, size, host_class, lease_deadline_ms))
    project_holds_schema.take_hold(history._connection, history.project_id, "result_custody",
                                   attempt_identity(run_handle, attempt), now_ms=now_ms, due_at_ms=lease_deadline_ms)


def check_verifier_margin(history, task: dict, lease_until_ms: int) -> None:
    """In the lease transaction, for a verifier (RS-D-5, owner decision 90.4 A): the worker it verifies, found by its
    outcome digest, must be kept at least `QA_MARGIN_MS` past the verifier lease's deadline; otherwise the verifier
    is refused (`history_worker_result_expiring`) and nothing is stored. An admission check, not a lifetime promise:
    an early close or a forward clock jump can still end either custody sooner. A worker with no stored deadline
    yet, or already gone, is left to the readers that refuse it."""
    digest = task.get("verification_for_outcome")
    if not matches(digest, _DIGEST):
        return
    row = history._connection.execute(
        "SELECT state,retained_until_ms FROM project_result_contexts INDEXED BY project_result_contexts_outcome "
        "WHERE project_id=? AND outcome_digest=?", (history.project_id, digest)).fetchone()
    if row is None or row["retained_until_ms"] is None or row["state"] in _GONE:
        return
    if row["retained_until_ms"] < lease_until_ms + QA_MARGIN_MS:
        raise HistoryError("history_worker_result_expiring")


def discard(history, run_handle: str, attempt: int) -> None:
    """A lease transaction that did not commit leaves no file (a failed allocation charges nothing)."""
    if _row(history, run_handle, attempt) is None:
        try:
            _unlink(history, run_handle, attempt)
        except (HistoryError, OSError):
            pass  # Removed at the next open instead (O1).


# Host open, bind, commit, admission --------------------------------------------------------------------------------

def verify_open(history, run_handle: str, attempt: int, inputs: dict) -> bool:
    """Before the host open's transaction: the inputs it is recorded with are exactly the stored ones (O4)."""
    row = _row(history, run_handle, attempt)
    if row is None:
        return False  # A lease stored without a context (not the driver's path): nothing to verify or move.
    if row["state"] not in ("leased", "opened", "bound"):
        raise HistoryError("history_conflict")
    value = _read(history, row)
    if any(canonical_json(value[name]) != canonical_json(inputs[name]) for name in ("assignment", "task", "route")):
        raise HistoryError("history_conflict")
    return True


def stored_state(history, binding: AttemptBinding):
    """D4: the state of the attempt's context row, or None when it has none (the test-only non-lease path)."""
    row = _row(history, binding.run_handle, binding.attempt)
    return None if row is None else row["state"]


def mark_opened(history, run_handle: str, attempt: int) -> None:
    """In the host open's transaction."""
    history._connection.execute(
        "UPDATE project_result_contexts SET state='opened' WHERE project_id=? AND run_handle=? AND attempt=? "
        "AND state='leased'", (history.project_id, run_handle, attempt))


def bind(history, binding: AttemptBinding) -> None:
    """In the bind's transaction: the claim association; every later read builds the binding from it."""
    row = _row(history, binding.run_handle, binding.attempt)
    if row is None or row["state"] == "legacy_no_context":
        return
    if row["state"] not in ("leased", "opened"):
        raise HistoryError("history_conflict")
    history._connection.execute(
        "UPDATE project_result_contexts SET state='bound',claim_id=?,claim_digest=? "
        "WHERE project_id=? AND run_handle=? AND attempt=?",
        (binding.claim_id, binding.claim_digest, history.project_id, binding.run_handle, binding.attempt))


def _associated(row, binding: AttemptBinding) -> bool:
    return row["claim_id"] == binding.claim_id and row["claim_digest"] == binding.claim_digest


def commit(history, binding: AttemptBinding, retained_until_ms: int) -> None:
    """In the commit's transaction: from here the retention deadline applies (RS-D draft 2 §1)."""
    row = _row(history, binding.run_handle, binding.attempt)
    if row is None or row["state"] == "legacy_no_context":
        return
    if row["state"] != "bound" or not _associated(row, binding):
        raise HistoryError("history_conflict")
    history._connection.execute(
        "UPDATE project_result_contexts SET state='committed',retained_until_ms=? "
        "WHERE project_id=? AND run_handle=? AND attempt=?",
        (retained_until_ms, history.project_id, binding.run_handle, binding.attempt))
    project_holds_schema.wake_before(history.project_id, retained_until_ms)  # E2a-7: a custody deadline is set
    history._connection.execute(
        "UPDATE project_holds SET due_at_ms=? WHERE project_id=? AND kind='result_custody' AND identity=? "
        "AND state='held'", (retained_until_ms, history.project_id,
                             attempt_identity(binding.run_handle, binding.attempt)))


def record_outcome(history, binding: AttemptBinding, outcome_digest: str) -> None:
    """In the admission's transaction (RS-D draft 2 §4): the outcome association, so a verifier's open, QA and
    routing evidence find this run by its outcome digest alone; `run.recorded` is only a hint."""
    row = _row(history, binding.run_handle, binding.attempt)
    if row is None or row["state"] in ("legacy_no_context", *_GONE):
        return
    if not _associated(row, binding) or not matches(outcome_digest, _DIGEST):
        raise HistoryError("history_conflict")
    if row["state"] == "recorded":
        if row["outcome_digest"] != outcome_digest:
            raise HistoryError("history_conflict")
        return
    if row["state"] != "committed":
        raise HistoryError("history_conflict")
    history._connection.execute(
        "UPDATE project_result_contexts SET state='recorded',outcome_digest=? "
        "WHERE project_id=? AND run_handle=? AND attempt=? AND state='committed'",
        (outcome_digest, history.project_id, binding.run_handle, binding.attempt))


# Release and retirement (SH-3) -------------------------------------------------------------------------------------

def _release_custody(history, run_handle: str, attempt: int, kind: str, now_ms: int, evidence=None) -> None:
    identity = attempt_identity(run_handle, attempt)
    held = history._connection.execute(
        "SELECT 1 FROM project_holds WHERE project_id=? AND kind='result_custody' AND identity=? AND state<>'released'",
        (history.project_id, identity)).fetchone()
    if held is not None:
        project_holds_schema.release_hold(history._connection, history.project_id, "result_custody", identity, kind,
                                          now_ms=now_ms, evidence=evidence)


def release_unfinished(history, run_handle: str, attempt: int, cause: str, now_ms: int) -> bool:
    """In the transaction of an RS-B lease release that ends the attempt unfinished (SH-3): custody released
    `lost_with_reservation`, with the cause in its evidence (SH-7's joint check); the context starts retiring.
    True when a file is then to be removed (`finish`)."""
    row = _row(history, run_handle, attempt)
    if row is None:
        return False
    _release_custody(history, run_handle, attempt, "lost_with_reservation", now_ms, evidence_json(cause=cause))
    if row["state"] in ("ended", "expired"):
        return False
    history._connection.execute(
        "UPDATE project_result_contexts SET state='retiring' WHERE project_id=? AND run_handle=? AND attempt=?",
        (history.project_id, run_handle, attempt))
    return True


def retire_names(history, fixed_names) -> str | None:
    """RS-D draft 3 §4: unlink each fixed name through the store's directory descriptor without opening or following
    it, then fsync the directory. An absent name is skipped. Anything but a directory (a regular file of any owner,
    mode or link count, a symlink, a FIFO, a socket) is unlinked: only the directory entry goes, so an extra hard link
    keeps its other name, outside the store. Returns None when every name is gone and the directory is synced,
    `directory` when a name is a directory (an operator must repair it), or `io` for an unlink or fsync error."""
    reason = None
    for name in fixed_names:
        try:
            info = os.stat(name, dir_fd=history._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            reason = reason or "io"
            continue
        if stat.S_ISDIR(info.st_mode):
            reason = "directory"
            continue
        try:
            os.unlink(name, dir_fd=history._directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            reason = reason or "io"
    try:
        os.fsync(history._directory_fd)
    except OSError:
        reason = reason or "io"
    return reason


def refuse_retirement(history, run_handle: str, attempt: int, reason: str) -> HistoryError:
    """After `retire_names` reported `reason`: a directory puts the context in `repair_required` (the row stays
    charged, shown and retried); the error returned is the failure the pass reports. Raise it outside any `except`."""
    if reason == "directory":
        with history._transaction():
            history._connection.execute(
                "UPDATE project_result_contexts SET state='repair_required',repair_reason='directory' "
                "WHERE project_id=? AND run_handle=? AND attempt=? AND state IN ('retiring','repair_required')",
                (history.project_id, run_handle, attempt))
        return HistoryError("history_unsafe_path")
    return HistoryError("history_unavailable")


def finish(history, run_handle: str, attempt: int) -> None:
    """After that transaction: the files go (`retire_names`), then the context is `ended` (a stop in between is
    finished by the next pass). A directory leaves it `repair_required`, any other failure `retiring`; either way it
    stays charged."""
    reason = retire_names(history, names(history, run_handle, attempt))
    if reason is not None:
        raise refuse_retirement(history, run_handle, attempt, reason)
    with history._transaction():
        history._connection.execute(
            "UPDATE project_result_contexts SET state='ended',repair_reason=NULL WHERE project_id=? AND run_handle=? "
            "AND attempt=? AND state IN ('retiring','repair_required')", (history.project_id, run_handle, attempt))


def finish_quietly(history, run_handle: str, attempt: int) -> None:
    try:
        finish(history, run_handle, attempt)
    except (HistoryError, OSError):
        pass  # Left `retiring` and charged; the next open finishes it.


def begin_retirement(history, binding: AttemptBinding, kind: str, evidence=None) -> None:
    """In `_retire_result`'s `retiring` transaction: custody released with `kind` (`retention`,
    `orchestration_closed`, or `lost_with_reservation` for a reserved row whose obligation RS-B released lost,
    crashed or unacknowledged; that kind carries `evidence`, the settlement cause) and no reader gets the context
    from here on."""
    row = _row(history, binding.run_handle, binding.attempt)
    if row is None or row["state"] in ("ended", "expired"):
        return
    _release_custody(history, binding.run_handle, binding.attempt, kind, history._hold_now(), evidence)
    history._connection.execute(  # a `repair_required` retry keeps its state (and its reason) until it is repaired
        "UPDATE project_result_contexts SET state='retiring' WHERE project_id=? AND run_handle=? AND attempt=? "
        "AND state<>'repair_required'", (history.project_id, binding.run_handle, binding.attempt))


def body_and_context_names(history, binding: AttemptBinding) -> tuple[str, ...]:
    """The four fixed names of one retirement: the body's (partial, final) and the context's (partial, final)."""
    return (*history._result_names(binding), *names(history, binding.run_handle, binding.attempt))


def end_retirement(history, binding: AttemptBinding) -> None:
    """In `_retire_result`'s `expired` transaction."""
    history._connection.execute(
        "UPDATE project_result_contexts SET state='expired',repair_reason=NULL WHERE project_id=? AND run_handle=? "
        "AND attempt=? AND state IN ('retiring','repair_required')",
        (history.project_id, binding.run_handle, binding.attempt))


def purgeable(history, run_handle: str, attempt: int) -> bool:
    """A finished attempt's rows go only once its context is finished too (RS-D draft 2 §5)."""
    row = _row(history, run_handle, attempt)
    return row is None or row["state"] in ("ended", "expired")


def _remove_body_partials(history, listing) -> None:
    """RS-D draft 3 §6, by directory listing and `lstat` alone (no byte is read): a lone body partial (a write that
    stopped before its link) goes, and so does the partial name of a link pair (the same inode under both fixed
    names, two links), which leaves the final name. A pair with any other link count or inode is unsafe: it is left
    for the read and retirement paths, which refuse and unlink it."""
    for name in listing:
        if not (name.startswith(".result-") and name.endswith(".partial")):
            continue
        final = name[:-len(".partial")] + ".json"
        try:
            left = os.stat(name, dir_fd=history._directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(left.st_mode):
                continue
            try:
                right = os.stat(final, dir_fd=history._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                right = None
            if right is not None and not (left.st_nlink == right.st_nlink == 2
                                          and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)):
                continue
            os.unlink(name, dir_fd=history._directory_fd)
        except OSError:
            continue  # Best effort; a body's readers and its retirement handle what is left.


def reconcile(history) -> None:
    """At open, under the writer lock, before anything is served (RS-D draft 3 §6): a context file with no live row
    (left by a lease transaction that never committed, O1) or a lone partial goes, so does a body's lone partial or
    the partial name of a link pair, and a context-only retirement a stop interrupted is finished. Reads directory
    entries, `lstat` and rows, never a body's or a context's bytes, and visits only live rows (the partial index), so
    its work does not grow with the tombstones (RS-D-9)."""
    connection = history._connection
    keep = set()
    for run_handle, attempt in connection.execute(
            "SELECT run_handle,attempt FROM project_result_contexts INDEXED BY project_result_contexts_live "
            f"WHERE state NOT IN ('ended','expired') AND state IN {project_holds_schema._in(FILE_STATES)}"):
        keep.add(names(history, run_handle, attempt)[1])
    listing = os.listdir(history._directory_fd)
    for name in listing:
        if name.startswith(_PREFIX) and name not in keep:
            try:
                _remove_entry(history, name)
            except HistoryError:
                pass  # A directory under a context's name is never removed (D3 reports it for repair).
    _remove_body_partials(history, listing)
    # Only one whose custody is already released: no retirement is started here, only finished.
    retiring = connection.execute(
        "SELECT c.run_handle,c.attempt FROM project_result_contexts c INDEXED BY project_result_contexts_live "
        "WHERE c.state NOT IN ('ended','expired') AND c.state IN ('retiring','repair_required') "
        "AND NOT EXISTS(SELECT 1 FROM result_reservations s WHERE s.project_id=c.project_id "
        "AND s.run_handle=c.run_handle AND s.attempt=c.attempt) AND NOT EXISTS(SELECT 1 FROM project_holds h "
        "WHERE h.project_id=c.project_id AND h.kind='result_custody' AND h.identity=c.run_handle||':'||c.attempt "
        "AND h.state<>'released')").fetchall()
    for run_handle, attempt in retiring:
        finish_quietly(history, run_handle, attempt)
    os.fsync(history._directory_fd)


# The context sweep and the repair report (D3) -----------------------------------------------------------------------

def _sweep_candidates(history, after, limit: int):
    """RS-D draft 4 §1: context rows with no reservation row whose custody is released, still not `ended` or
    `expired`; this includes their own `retiring` and `repair_required` retries. A context with a reservation (a
    bound attempt) is never selected: `_retire_result` retires it with its body. Uses the live partial index, so a
    tombstone costs nothing; the rowid cursor makes the pass fair."""
    statement = (
        "SELECT c.rowid,c.run_handle,c.attempt FROM project_result_contexts c INDEXED BY project_result_contexts_live "
        "WHERE c.project_id=? AND c.state NOT IN ('ended','expired') "
        "AND NOT EXISTS(SELECT 1 FROM result_reservations s WHERE s.project_id=c.project_id "
        "AND s.run_handle=c.run_handle AND s.attempt=c.attempt) "
        "AND EXISTS(SELECT 1 FROM project_holds h WHERE h.project_id=c.project_id AND h.kind='result_custody' "
        "AND h.identity=c.run_handle||':'||c.attempt AND h.state='released')")
    arguments = [history.project_id]
    if after is not None:
        statement += " AND c.rowid>?"
        arguments.append(after)
    return history._connection.execute(statement + " ORDER BY c.rowid LIMIT ?", (*arguments, limit)).fetchall()


def sweep(history, limit: int) -> int:
    """One bounded, cursor-fair pass of the context sweep, after the reservations' retirement in the same expire
    pass: each selected context is recorded `retiring`, its files unlinked and its directory synced, then `ended`.
    A failure leaves that context charged (`retiring`, or `repair_required` for a directory) and the pass moves on;
    the first failure is reported at the end. Returns how many contexts ended. No reservation row is read or
    written for a selected context (it has none)."""
    after = getattr(history, "_context_sweep_cursor", None)
    rows = _sweep_candidates(history, after, limit)
    if not rows and after is not None:
        after = None
        rows = _sweep_candidates(history, None, limit)
    failure = None
    ended = 0
    for position, run_handle, attempt in rows:
        after = position
        try:
            with history._transaction():
                history._connection.execute(
                    "UPDATE project_result_contexts SET state='retiring' WHERE project_id=? AND run_handle=? "
                    "AND attempt=? AND state NOT IN ('retiring','repair_required','ended','expired')",
                    (history.project_id, run_handle, attempt))
            finish(history, run_handle, attempt)
            ended += 1
        except HistoryError as exc:
            failure = failure or exc.reason_code
        except (sqlite3.Error, OSError):
            failure = failure or "history_unavailable"
    history._context_sweep_cursor = after
    if failure is not None:
        raise HistoryError(failure)
    return ended


class RepairReport(list):
    """The entries of one bounded page, in order, plus `truncated` and `cursor`: when `truncated`, pass `cursor` to
    `repairs` to get the entries after this page, so that every remaining entry stays discoverable."""

    truncated = False
    cursor = None


_TIERS = ("repair_required", "reservation", "retiring")  # the order of the report: operator-needed first


def _parse_cursor(cursor) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    if isinstance(cursor, str):
        tier, _, rowid = cursor.partition(":")
        if tier.isdigit() and rowid.isdigit() and int(tier) < len(_TIERS) and len(rowid) <= 18:
            return int(tier), int(rowid)
    raise HistoryError("history_invalid_input")


def repairs(history, limit: int = 20, cursor=None) -> RepairReport:
    """The retirements still waiting (RS-D draft 3 §4), at most `limit` per page, for status. Each stays charged until
    every fixed name is gone. Entries come in three groups, each in row order: a context in `repair_required` (shown
    with its stored reason, `directory`), then a reservation in `retiring` that has no context row (the non-lease
    path; `directory` is derived from the fixed names, since a reservation has no repair column and there is no
    schema step), then a context still `retiring` (an unlink or fsync error, or a stop, that the next pass retries;
    shown as `retrying`). So persistent retries never hide an entry an operator must repair. A page that does not
    reach the end is `truncated` and carries a `cursor` for the next page. Context entries carry
    `operator_needed` from their state, independently of the stored repair reason."""
    report = RepairReport()
    tier, after = _parse_cursor(cursor)

    def directories(fixed):
        found = []
        for name in fixed:
            try:
                if stat.S_ISDIR(os.stat(name, dir_fd=history._directory_fd, follow_symlinks=False).st_mode):
                    found.append(name)
            except OSError:
                continue
        return found

    def context_rows(state, room):
        return history._connection.execute(
            "SELECT rowid AS position,run_handle,attempt,state,repair_reason FROM project_result_contexts INDEXED BY "
            "project_result_contexts_live WHERE project_id=? AND state NOT IN ('ended','expired') AND state=? "
            "AND rowid>? ORDER BY rowid LIMIT ?", (history.project_id, state, after, room)).fetchall()

    def context_entry(row):
        fixed = list(names(history, row["run_handle"], row["attempt"]))
        reservation = history._connection.execute(
            "SELECT * FROM result_reservations WHERE project_id=? AND run_handle=? AND attempt=?",
            (history.project_id, row["run_handle"], row["attempt"])).fetchone()
        if reservation is not None:
            try:
                fixed += history._result_names(history._reservation(reservation).binding)
            except HistoryError:
                pass
        return {"run_handle": row["run_handle"], "attempt": row["attempt"],
                "reason": row["repair_reason"] if row["state"] == "repair_required" else "retrying",
                "operator_needed": row["state"] == "repair_required",
                "names": directories(fixed)}

    def reservation_rows(room):
        return history._connection.execute(
            "SELECT s.rowid AS position,s.* FROM result_reservations AS s WHERE s.project_id=? AND s.state='retiring' "
            "AND NOT EXISTS(SELECT 1 FROM project_result_contexts c WHERE c.project_id=s.project_id "
            "AND c.run_handle=s.run_handle AND c.attempt=s.attempt) AND s.rowid>? ORDER BY s.rowid LIMIT ?",
            (history.project_id, after, room)).fetchall()

    def reservation_entry(row):
        try:
            value = history._reservation(row)
        except HistoryError:
            return None
        found = directories(history._result_names(value.binding))
        return {"run_handle": value.binding.run_handle, "attempt": value.binding.attempt,
                "reason": "directory" if found else "retrying", "names": found}

    while tier < len(_TIERS) and limit > 0:
        room = limit - len(report)
        if _TIERS[tier] == "reservation":
            rows, entry = reservation_rows(room + 1), reservation_entry
        else:
            state = "repair_required" if _TIERS[tier] == "repair_required" else "retiring"
            rows, entry = context_rows(state, room + 1), context_entry
        for row in rows[:room]:
            made = entry(row)
            if made is not None:
                report.append(made)
        if len(rows) > room:  # this group has more: the page is full, and the next one starts after the last row shown
            report.truncated, report.cursor = True, f"{tier}:{rows[room - 1]['position']}"
            return report
        tier, after = tier + 1, 0
        if len(report) >= limit:  # full exactly at the end of a group: more only if a later group has an entry
            while tier < len(_TIERS):
                probe = reservation_rows(1) if _TIERS[tier] == "reservation" else context_rows(
                    "repair_required" if _TIERS[tier] == "repair_required" else "retiring", 1)
                if probe:
                    report.truncated, report.cursor = True, f"{tier}:0"
                    return report
                tier += 1
            return report
    return report


# Readers -----------------------------------------------------------------------------------------------------------

def _lease_binding(history, row) -> AttemptBinding:
    """The attempt's binding, from its sealed lease and the stored claim association."""
    from .result_contracts import LEASE_SCOPE_FIELDS

    lease_row = history._connection.execute(
        "SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?",
        (history.project_id, row["run_handle"], row["attempt"])).fetchone()
    if lease_row is None or row["claim_id"] is None:
        raise HistoryError("history_not_authorized")
    lease = history._lease(lease_row)
    if lease.state != "bound" or lease.claim_id != row["claim_id"] or lease.claim_digest != row["claim_digest"]:
        raise HistoryError("history_not_authorized")
    return AttemptBinding(**{name: getattr(lease.binding, name) for name in LEASE_SCOPE_FIELDS},
                          claim_id=row["claim_id"], claim_digest=row["claim_digest"])


def _readable(row, *, recorded: bool) -> None:
    if row is None:
        raise HistoryError("history_not_authorized")
    if row["state"] == "legacy_no_context":
        # M1: stored before 18 by a service that kept it in memory only; nothing is invented for it.
        raise HistoryError("history_worker_context_unavailable")
    if row["state"] in _GONE:
        raise HistoryError("history_result_expired")
    if row["state"] not in ("bound", "committed", "recorded") or (recorded and row["state"] != "recorded"):
        raise HistoryError("history_not_authorized")


def _checked(history, row, binding: AttemptBinding, host_class: str) -> HostResultContext:
    """The stored inputs as a HostResultContext, checked against the lease and the consumed grant (RS-D-2, -10)."""
    from .contracts import validate_task
    from .execution_grant import MAX_EXECUTION_GRANT_BYTES, verify_execution_grant
    from .learning_contracts import validate_execution_configuration
    from .result_contracts import assignment_bytes

    if row["host_class"] != host_class:
        raise HistoryError("history_not_authorized")
    _, reservation = history._result_row(binding)
    value = _read(history, row)
    assignment, task, route, capability = (value[name] for name in ("assignment", "task", "route",
                                                                    "client_capability"))
    consumed = history._connection.execute(
        "SELECT * FROM project_execution_consumptions WHERE project_id=? AND claim_id=?",
        (history.project_id, binding.claim_id)).fetchone()
    if consumed is None:
        raise HistoryError("history_execution_not_ready")
    invalid = False
    try:
        grant = verify_execution_grant(history._result_key, binding,
                                       history._stored_blob(consumed["grant"], maximum=MAX_EXECUTION_GRANT_BYTES))
        configuration = validate_execution_configuration(route["execution_configuration"])
        key = history._result_key
        invalid = (
            reservation.assignment_digest != keyed_digest(key, assignment_bytes(assignment, binding))
            or grant.assignment_digest != reservation.assignment_digest
            or grant.task_digest != keyed_digest(key, canonical_json(validate_task(task)))
            or grant.route_digest != keyed_digest(key, canonical_json(route_identity(route)))
            or grant.configuration_digest != content_digest(configuration)
            or configuration["client_digest"] != content_digest(capability)
        )
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError("history_conflict")
    return HostResultContext(binding, assignment, task, route, capability)


def stored_result_context(history, binding: AttemptBinding, *, host_class: str,
                          recorded: bool = False) -> HostResultContext:
    """B2's read (RS-D-10), and every reader's: this exact claimed attempt's inputs, for its own host class only."""
    binding = history._result_binding(binding)
    row = _row(history, binding.run_handle, binding.attempt)
    _readable(row, recorded=recorded)
    if not _associated(row, binding) or _lease_binding(history, row) != binding:
        raise HistoryError("history_not_authorized")
    return _checked(history, row, binding, host_class)


def stored_worker_context(history, outcome_digest: str, *, host_class: str,
                          orchestration_handle: str | None = None) -> HostResultContext:
    """A recorded run's inputs, found by its outcome digest (unique index), for a verifier, QA or routing evidence;
    the admission record must name exactly that outcome (a forged association is refused)."""
    if not matches(outcome_digest, _DIGEST):
        raise HistoryError("history_invalid_input")
    row = history._connection.execute(
        "SELECT * FROM project_result_contexts INDEXED BY project_result_contexts_outcome "
        "WHERE project_id=? AND outcome_digest=?", (history.project_id, outcome_digest)).fetchone()
    _readable(row, recorded=True)
    binding = _lease_binding(history, row)
    if orchestration_handle is not None and binding.orchestration_handle != orchestration_handle:
        raise HistoryError("history_not_authorized")
    context = _checked(history, row, binding, host_class)
    identity = "host_result_admission:" + keyed_digest(history._result_key, b"project-host-result-v1\0"
                                                        + canonical_json(binding.as_data()))
    try:
        receipt = parse_bounded_json(history.record(identity).content)
    except HistoryError:
        raise HistoryError("history_conflict") from None
    if receipt.get("host_outcome_digest") != outcome_digest:
        raise HistoryError("history_conflict")
    return context


def stored_attempt_context(history, run_handle: str, attempt: int, *, host_class: str,
                           inputs_digest: str) -> HostResultContext:
    """A published attempt named by its run handle, attempt and open inputs digest, for its own host class: what
    the record command needs to find its result once the service that ran it has gone."""
    row = _row(history, run_handle, attempt)
    if row is None:
        # O0d R1: no such dispatch. Named plainly, as the dispatch view named it; the run handle is a random
        # 48-hex name only an enrolled host of this project can ask about, so this adds no leak.
        raise HistoryError("history_not_found")
    if row["state"] in ("leased", "opened", "bound"):
        raise HistoryError("history_result_not_ready")  # not (yet) published: the record command says so
    _readable(row, recorded=False)
    if row["host_class"] != host_class:
        raise HistoryError("history_not_authorized")  # another host class's run: not this caller's to ask about
    if not matches(inputs_digest, _DIGEST):
        raise HistoryError("history_invalid_input")
    if not secrets.compare_digest(_read(history, row)["inputs_digest"], inputs_digest):
        raise HistoryError("history_conflict")  # O0d R1: this caller's run, but not with those original inputs
    binding = _lease_binding(history, row)
    return _checked(history, row, binding, host_class)


def committed_reference(history, binding: AttemptBinding) -> dict:
    """The published terminal of a committed attempt: its record and result digests."""
    _, reservation = history._result_row(binding)
    outbox = history._connection.execute("SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?",
                                         (history.project_id, binding.claim_id)).fetchone()
    if outbox is None or reservation.state not in ("committed", "retiring", "expired"):
        raise HistoryError("history_result_not_ready")
    delivery = history._delivery(outbox)
    if delivery.binding != binding:
        raise HistoryError("history_conflict")
    return {"binding": binding.as_data(), "delivery": {
        "record_id": delivery.record_id, "record_content_digest": delivery.record_content_digest,
        "result_digest": delivery.result_digest}}
