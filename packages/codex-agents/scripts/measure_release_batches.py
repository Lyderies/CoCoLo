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

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 300, "data_mib": 256, "rss_mib": 2048}  # drain target is 60 s

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
from threading import local as thread_local  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

import tests.support  # noqa: E402,F401  (puts the package on the path)
from tests.test_run_holds import RunHoldTests  # noqa: E402

from codex_agents import admission_metadata, learning_store  # noqa: E402
from codex_agents.learning_contracts import content_digest  # noqa: E402
from codex_agents.run_holds_schema import DECISION_RELEASED, check_run_holds_consistency  # noqa: E402

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

    slot = next(row for row in rows["run_holds"] if row["kind"] == "run_slot")
    if slot["evidence"] is None:
        raise AssertionError("the drained run's decision has no evidence")

    def mapping(index):
        """Each copy's own identifiers; its decision's evidence names them, so its receipt (the slot's release
        digest) is recomputed from the copied evidence, as the validator checks (G1, G10)."""
        replaced = {token: hashlib.sha256(b"%d:" % index + token.encode()).hexdigest()[:len(token)]
                    for token in tokens}
        replaced[slot["receipt"]] = content_digest(json.loads(copied(slot["evidence"], replaced)))
        return replaced

    def copied(value, replaced):
        if type(value) is not str:
            return value
        for token, replacement in replaced.items():
            if token in value:
                value = value.replace(token, replacement)
        return value
    maps = [mapping(index) for index in range(count)]
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in RUN_TABLES:
            for row in rows[table]:
                columns = list(row)
                verb = "INSERT OR REPLACE" if table == "run_holds" else "INSERT"
                connection.executemany(
                    f"{verb} INTO {table}({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                    (tuple(copied(row[c], replaced) for c in columns) for replaced in maps))
        # O0b RS-A round 3: the history is checked by the ledger's own validator before anything is measured
        # on it (copies whose decision evidence does not hash to its receipt would be refused at open).
        if not check_run_holds_consistency(connection):
            raise AssertionError("the copied history is not valid ledger history (the run hold validator "
                                 "refuses it)")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def startup_ms(case: RunHoldTests) -> tuple[float, list[tuple[float, str]]]:
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
    statements = open_statements(case)
    ledger = learning_store.LearningLedger(fixture.state, client_capabilities=fixture.clients,
                                           allow_schema_upgrade=True)
    case.case.ledger = ledger
    case.addCleanup(ledger.close)
    clock = mock.patch.object(ledger, "_clock_ms", side_effect=lambda: case.case.stamp)  # the fixture's clock
    clock.start()
    case.addCleanup(clock.stop)
    return statistics.median(times), statements


def median_ms(call) -> float:
    """Warm: one call discarded, then the median of five."""
    times = []
    for start in range(STARTS + 1):
        begun = time.perf_counter()
        call()
        if start:
            times.append((time.perf_counter() - begun) * 1000)
    return statistics.median(times)


def open_statements(case: RunHoldTests) -> list[tuple[float, str]]:
    """Every statement one ledger open runs, with its time (the open's connection is traced and timed)."""
    from codex_agents import sql_read_budget
    fixture, timed, real = case.fixture, [], sql_read_budget.SQLReadConnection.execute

    def execute(self, sql, *arguments):
        begun = time.perf_counter()
        try:
            return real(self, sql, *arguments)
        finally:
            timed.append(((time.perf_counter() - begun) * 1000, " ".join(sql.split())))
    with mock.patch.object(sql_read_budget.SQLReadConnection, "execute", execute):
        learning_store.LearningLedger(fixture.state, client_capabilities=fixture.clients,
                                      allow_schema_upgrade=True).close()
    return timed


def startup_parts(case: RunHoldTests, statements: list[tuple[float, str]]) -> dict:
    """G11: RS-A's own open-time work (the register checks, and its decision exemption in the completion
    coverage check) apart from the older checks, each the warm median of five on the open ledger."""
    connection = case.ledger._connection
    exemption = f"AND NOT ({DECISION_RELEASED} AND (c.terminal_record_id IS NULL OR e.owner_digest IS NOT NULL)) "
    coverage = next(sql for _, sql in statements if "LEFT JOIN capacity_completions c" in sql
                    and "FROM active_run_capacity a" in sql)
    claims = next(sql for _, sql in statements if sql.startswith("SELECT 1 FROM launch_claims c LEFT JOIN "
                                                                  "active_run_capacity a"))
    older = " ".join(coverage.replace(" ".join(exemption.split()), "").split())
    if older == coverage:
        raise AssertionError("the completion coverage check no longer carries RS-A's exemption")

    def run(sql):
        return lambda: connection.execute(sql).fetchone()
    with_exemption, without_exemption = median_ms(run(coverage)), median_ms(run(older))
    register = median_ms(lambda: check_run_holds_consistency(connection))
    return {
        "register_checks_ms": round(register, 1),
        "completion_coverage_ms": round(with_exemption, 1),
        "completion_coverage_without_rs_a_exemption_ms": round(without_exemption, 1),
        "rs_a_exemption_ms": round(max(0.0, with_exemption - without_exemption), 1),
        "rs_a_added_ms": round(register + max(0.0, with_exemption - without_exemption), 1),
        "older_claim_coverage_ms": round(median_ms(run(claims)), 1),
        "slowest_open_statements": [[round(ms, 1), sql[:140]] for ms, sql in sorted(statements, reverse=True)[:8]],
    }


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
        startup_before, statements = startup_ms(case)
        parts_before = startup_parts(case, statements)
        begun = time.monotonic()
        if history:
            add_tombstones(case, history, now)
            add_expired_claimed_runs(case, history, case.case.stamp - 1)
        history_s = time.monotonic() - begun
        startup_after, statements = startup_ms(case)
        parts_after = startup_parts(case, statements)
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
            # G11: RS-A's own open-time checks with the history, at most the start-up budget on their own
            "rs_a_startup_checks_at_most_200_ms": parts_after["rs_a_added_ms"] <= STARTUP_BUDGET_MS,
        }
        return {
            "batch_limit": learning_store.ACCOUNT_HOLD_RELEASE_BATCH, "build_s": round(built_s, 1),
            "history_tombstones": history, "history_expired_claimed_runs": history,
            "history_build_s": round(history_s, 1), "rebuild_with_history_s": round(rebuilt_s, 1),
            "without_history": without, "with_history": with_history,
            "startup_warm_median_ms_without_history": round(startup_before, 1),
            "startup_warm_median_ms_with_history": round(startup_after, 1),
            "startup_parts_without_history": parts_before, "startup_parts_with_history": parts_after,
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
