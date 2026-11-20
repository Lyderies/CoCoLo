#!/usr/bin/env python3
"""O0b RS-A round 1 F15, round 2 G8: measure the account hold release against RS-A-7's budget.

A real ledger (the test_run_holds fixture) gets 1,000 or more due account holds: claimed runs past their
deadline on both clocks (each a run slot and its claimed binding) and, if asked, unclaimed bindings past
their expiry. The collector's sweep clears them as its loop does (one full cycle, then release batches
alone with the gap between them), timing each batch's work and every hold of the ledger lock. Round 2 G8:
the ledger then gets history, by default 100,000 register tombstones (bindings whose source was removed)
and 100,000 expired claimed runs (copies of a drained one: its slot released `crashed`, its binding's
register row released `expired` and the binding still pending for its settlement, with its claim), and
the same number of due holds is built and drained again. Empty batches (nothing due) are measured
without and with the history, by time and by SQLite VM steps, and so is the ledger's start-up (warm: one
open discarded, then the median of five).
Budget (RS-A-7, G8): a batch's work under 50 ms; no ledger lock hold over 750 ms; an empty batch's work the
same with the history; start-up no more than 200 ms slower with it; the due holds drained within 60 s.
Takes a few minutes. No model, network or provider call; the fixture's admission and capacity checks are
patched only so one project can hold this many claims at once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from threading import local as thread_local
import time
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

import tests.support  # noqa: E402,F401  (puts the package on the path)
from tests.test_run_holds import RunHoldTests  # noqa: E402

from codex_agents import admission_metadata, learning_store  # noqa: E402

BATCH_BUDGET_MS = 50.0
HOLD_BUDGET_MS = float(learning_store.LEDGER_LOCK_WAIT_MS)
DRAIN_BUDGET_S = 60.0          # G8: the drain target, an explicit check
STARTUP_BUDGET_MS = 200.0      # RS-A-7: start-up no more than this much slower with the history
EMPTY_STEP_SLACK = 200         # G8: an empty batch's VM steps with the history, at most this many more
STARTS = 5


class TimedLock:
    """The ledger's RLock, timing each outermost hold (re-entry by the holder is not a new hold)."""

    def __init__(self, inner):
        self._inner, self._depth, self._since, self.holds = inner, thread_local(), 0.0, []

    def acquire(self, blocking=True, timeout=-1):
        got = self._inner.acquire(blocking, timeout)
        if got:
            depth = getattr(self._depth, "value", 0)
            if depth == 0:
                self._since = time.perf_counter()
            self._depth.value = depth + 1
        return got

    def release(self):
        self._depth.value -= 1
        if self._depth.value == 0:
            self.holds.append((time.perf_counter() - self._since) * 1000)
        self._inner.release()

    __enter__ = acquire

    def __exit__(self, *_):
        self.release()


def build(case: RunHoldTests, claims: int, bindings: int, name: str = "measure") -> None:
    original = admission_metadata._admission_limits

    def no_run_cap(settings):
        limits = original(settings)
        return None if limits is None else (limits[0], limits[1], None)
    with mock.patch.object(admission_metadata, "_admission_limits", side_effect=no_run_cap), \
            mock.patch.object(case.ledger, "_check_active_capacity_locked",
                              return_value=learning_store.INSTALLED_ACTIVE_RUN_LIMITS):
        for index in range(claims):
            case.claim(case.new_attempt(), f"{name}-{index}")
        for _ in range(bindings):
            case.new_attempt()


def add_tombstones(case: RunHoldTests, count: int, at: int) -> None:
    """G8: `count` register tombstones, bindings released `removed` whose source rows are gone."""
    connection = case.ledger._connection
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(
        "INSERT INTO run_holds(kind,project_id,identity,state,generation,taken_at_ms,released_at_ms,release_kind) "
        "VALUES('binding',?,?,'released',2,?,?,'removed')",
        ((case.project, hashlib.sha256(b"tombstone-%d" % index).hexdigest(), at - 1, at) for index in range(count)))
    connection.execute("COMMIT")


# The rows one claimed run leaves, parents first (their triggers write the register rows, replaced below).
RUN_TABLES = ("host_run_bindings", "launch_claims", "launch_inputs", "launch_observations", "launch_receipts",
              "active_run_capacity", "capacity_completions", "capacity_executions", "run_holds")


def add_expired_claimed_runs(case: RunHoldTests, count: int, expired_at: int) -> None:
    """G8: `count` copies of a drained claimed run, its slot released `crashed` and its binding's register row
    released `expired` while the binding stays pending; each copy's identifiers are its own, and its binding
    expired at `expired_at` (history: before the fixture's clock, so only a clock rollback brings it back)."""
    connection = case.ledger._connection
    source = connection.execute(
        "SELECT h.project_id,h.handle_digest FROM host_run_bindings h JOIN run_holds r ON r.project_id=h.project_id "
        "AND r.kind='binding' AND r.identity=h.handle_digest AND r.state='released' AND r.release_kind='expired' "
        "WHERE h.binding_state='pending' LIMIT 1").fetchone()
    if source is None:
        raise AssertionError("no drained claimed run to copy")
    project, digest = source
    rows = {}
    for table in RUN_TABLES:
        column = "identity" if table == "run_holds" else "handle_digest"
        rows[table] = [dict(row) for row in connection.execute(
            f"SELECT * FROM {table} WHERE project_id=? AND {column}=?", (project, digest))]
    for row in rows["host_run_bindings"]:
        row["expires_at_ms"] = min(row["expires_at_ms"], expired_at)
    tokens = {digest}
    for row in rows["launch_claims"]:
        tokens.add(row["bridge_run_id"][4:])
    for row in rows["launch_receipts"]:
        tokens.update(row[name] for name in ("scope_digest", "request_digest", "receipt_digest") if row.get(name))
    for row in rows["capacity_completions"]:
        tokens.add(row["terminal_record_id"])

    def copied(value, index):
        if type(value) is not str:
            return value
        for token in tokens:
            if token in value:
                value = value.replace(token, hashlib.sha256(b"%d:" % index + token.encode()).hexdigest()[:len(token)])
        return value
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in RUN_TABLES:
            for row in rows[table]:
                columns = list(row)
                verb = "INSERT OR REPLACE" if table == "run_holds" else "INSERT"
                connection.executemany(
                    f"{verb} INTO {table}({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                    (tuple(copied(row[c], index) for c in columns) for index in range(count)))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def startup_ms(case: RunHoldTests) -> float:
    """The ledger's warm start-up: one open discarded, then the median of five (each closed after)."""
    fixture = case.fixture
    case.ledger.close()
    times = []
    for start in range(STARTS + 1):
        begun = time.perf_counter()
        ledger = learning_store.LearningLedger(fixture.state, client_capabilities=fixture.clients,
                                               allow_schema_upgrade=True)
        elapsed = (time.perf_counter() - begun) * 1000
        ledger.close()
        if start:
            times.append(elapsed)
    ledger = learning_store.LearningLedger(fixture.state, client_capabilities=fixture.clients,
                                           allow_schema_upgrade=True)
    case.case.ledger = ledger
    case.addCleanup(ledger.close)
    clock = mock.patch.object(ledger, "_clock_ms", side_effect=lambda: case.case.stamp)  # the fixture's clock
    clock.start()
    case.addCleanup(clock.stop)
    return statistics.median(times)


def empty_batch(case: RunHoldTests, now: int, monotonic: int) -> dict:
    """One batch with nothing due: its time and SQLite VM steps (a progress handler counting each)."""
    steps, released = case.release_steps(now, monotonic)
    if released:
        raise AssertionError("the empty batch released %d holds" % released)
    begun = time.perf_counter()
    case.sweep(now, monotonic)
    return {"ms": round((time.perf_counter() - begun) * 1000, 3), "vm_steps": steps}


def due_clocks(case: RunHoldTests) -> tuple[int, int]:
    """A wall time past every unreleased claim's crash grace and every binding's expiry, and a monotonic
    reading below every claim's reference (after a reboot)."""
    connection = case.ledger._connection
    rows = [row for row in case.rows() if row["released_at_ms"] is None]
    expiry = connection.execute("SELECT MAX(expires_at_ms) FROM host_run_bindings").fetchone()[0]
    now = max([row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS for row in rows] + [expiry]) + 1
    return now, min([row["clock_reference_ns"] for row in rows] + [10 ** 18]) - 1


def drain(case: RunHoldTests, now: int, monotonic: int) -> dict:
    ledger, connection = case.ledger, case.ledger._connection
    held = connection.execute("SELECT COUNT(*) FROM run_holds WHERE state='held'").fetchone()[0]
    batches = []
    real = ledger.release_due_account_holds

    def timed(*, limit):
        begun = time.perf_counter()
        try:
            return real(limit=limit)
        finally:
            batches.append((time.perf_counter() - begun) * 1000)
    lock = TimedLock(ledger._lock)
    server = learning_store.CollectorServer(ledger)
    with mock.patch.object(ledger, "_clock_ms", return_value=now), \
            mock.patch.object(ledger, "_capacity_clock_reference", return_value=monotonic), \
            mock.patch.object(ledger, "release_due_account_holds", side_effect=timed), \
            mock.patch.object(ledger, "_lock", lock):
        begun = time.monotonic()
        outcome = server._sweep_cycle()
        while outcome == "more":
            time.sleep(learning_store.SWEEP_BATCH_GAP_SECONDS)
            outcome = server._sweep_cycle(holds_only=True)
        drained_s = time.monotonic() - begun
    left = connection.execute("SELECT COUNT(*) FROM run_holds WHERE state='held'").fetchone()[0]
    return {
        "due_holds": held, "left_held": left, "batches": len(batches),
        "batch_ms": [round(b, 2) for b in batches],
        "batch_max_ms": round(max(batches), 3), "batch_mean_ms": round(sum(batches) / len(batches), 3),
        "lock_hold_max_ms": round(max(lock.holds), 3), "lock_holds": len(lock.holds),
        "drain_s": round(drained_s, 3),
    }


def measure(claims: int, bindings: int, history: int) -> dict:
    case = RunHoldTests("test_RS_A_5_an_identical_sweep_releases_nothing_twice")
    case.setUp()
    try:
        started = time.monotonic()
        build(case, claims, bindings)
        built_s = time.monotonic() - started
        now, monotonic = due_clocks(case)
        without = drain(case, now, monotonic)
        without["empty_batch_after_drain"] = empty_batch(case, now, monotonic)
        startup_before = startup_ms(case)
        begun = time.monotonic()
        if history:
            add_tombstones(case, history, now)
            add_expired_claimed_runs(case, history, case.case.stamp - 1)
        history_s = time.monotonic() - begun
        startup_after = startup_ms(case)
        empty_with_history = empty_batch(case, now, monotonic)
        begun = time.monotonic()
        build(case, claims, bindings, "again")
        rebuilt_s = time.monotonic() - begun
        with_history = drain(case, *due_clocks(case))
        with_history["empty_batch_before_drain"] = empty_with_history
        with_history["empty_batch_after_drain"] = empty_batch(case, *due_clocks(case))
        runs = (without, with_history)
        checks = {
            "batch_under_50_ms": all(run["batch_max_ms"] < BATCH_BUDGET_MS for run in runs),
            "lock_hold_under_750_ms": all(run["lock_hold_max_ms"] < HOLD_BUDGET_MS for run in runs),
            "drained_within_60_s": all(run["drain_s"] < DRAIN_BUDGET_S and run["left_held"] == 0 for run in runs),
            "empty_batch_same_work_with_history": all(
                run["vm_steps"] <= without["empty_batch_after_drain"]["vm_steps"] + EMPTY_STEP_SLACK
                for run in (empty_with_history, with_history["empty_batch_after_drain"])),
            "empty_batch_under_50_ms": all(run["ms"] < BATCH_BUDGET_MS for run in (
                without["empty_batch_after_drain"], empty_with_history, with_history["empty_batch_after_drain"])),
            "startup_at_most_200_ms_slower": startup_after - startup_before <= STARTUP_BUDGET_MS,
        }
        return {
            "batch_limit": learning_store.ACCOUNT_HOLD_RELEASE_BATCH, "build_s": round(built_s, 1),
            "history_tombstones": history, "history_expired_claimed_runs": history,
            "history_build_s": round(history_s, 1), "rebuild_with_history_s": round(rebuilt_s, 1),
            "without_history": without, "with_history": with_history,
            "startup_warm_median_ms_without_history": round(startup_before, 1),
            "startup_warm_median_ms_with_history": round(startup_after, 1),
            "checks": checks, "within_budget": all(checks.values()),
        }
    finally:
        case.doCleanups()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claims", type=int, default=500, help="claimed runs past their deadline (two holds each)")
    parser.add_argument("--bindings", type=int, default=0, help="unclaimed bindings past their expiry")
    parser.add_argument("--history", type=int, default=100_000,
                        help="historical register tombstones, and as many expired claimed bindings (0: none)")
    arguments = parser.parse_args()
    result = measure(arguments.claims, arguments.bindings, arguments.history)
    print(json.dumps(result, indent=1))
    return 0 if result["within_budget"] else 1


if __name__ == "__main__":
    sys.exit(main())
