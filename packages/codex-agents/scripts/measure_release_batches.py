#!/usr/bin/env python3
"""O0b RS-A round 1 F15: measure the account hold release against RS-A-7's budget.

A real ledger (the test_run_holds fixture) gets 1,000 or more due account holds: claimed runs past their
deadline on both clocks (each a run slot and its claimed binding) and, if asked, unclaimed bindings past
their expiry. The collector's sweep then clears them as its loop does (one full cycle, then release
batches alone with the gap between them), timing each batch's work and every hold of the ledger lock.
Budget (RS-A-7): a batch's work under 50 ms; no ledger lock hold over 750 ms. Takes about a minute.
No model, network or provider call; the fixture's admission and capacity checks are patched only so one
project can hold this many claims at once.
"""

from __future__ import annotations

import argparse
import json
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


def build(case: RunHoldTests, claims: int, bindings: int) -> None:
    original = admission_metadata._admission_limits

    def no_run_cap(settings):
        limits = original(settings)
        return None if limits is None else (limits[0], limits[1], None)
    with mock.patch.object(admission_metadata, "_admission_limits", side_effect=no_run_cap), \
            mock.patch.object(case.ledger, "_check_active_capacity_locked",
                              return_value=learning_store.INSTALLED_ACTIVE_RUN_LIMITS):
        for index in range(claims):
            case.claim(case.new_attempt(), f"measure-{index}")
        for _ in range(bindings):
            case.new_attempt()


def measure(claims: int, bindings: int) -> dict:
    case = RunHoldTests("test_RS_A_5_an_identical_sweep_releases_nothing_twice")
    case.setUp()
    try:
        started = time.monotonic()
        build(case, claims, bindings)
        built_s = time.monotonic() - started
        ledger, connection = case.ledger, case.ledger._connection
        held = connection.execute("SELECT COUNT(*) FROM run_holds WHERE state='held'").fetchone()[0]
        rows = case.rows()
        expiry = connection.execute("SELECT MAX(expires_at_ms) FROM host_run_bindings").fetchone()[0]
        now = max([row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS for row in rows] + [expiry]) + 1
        monotonic = min([row["clock_reference_ns"] for row in rows] + [10 ** 18]) - 1  # after a reboot
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
            "due_holds": held, "left_held": left, "build_s": round(built_s, 1),
            "batch_limit": learning_store.ACCOUNT_HOLD_RELEASE_BATCH, "batches": len(batches),
            "batch_max_ms": round(max(batches), 3), "batch_mean_ms": round(sum(batches) / len(batches), 3),
            "lock_hold_max_ms": round(max(lock.holds), 3), "lock_holds": len(lock.holds),
            "drain_s": round(drained_s, 3),
            "within_budget": max(batches) < BATCH_BUDGET_MS and max(lock.holds) < HOLD_BUDGET_MS,
        }
    finally:
        case.doCleanups()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claims", type=int, default=500, help="claimed runs past their deadline (two holds each)")
    parser.add_argument("--bindings", type=int, default=0, help="unclaimed bindings past their expiry")
    arguments = parser.parse_args()
    result = measure(arguments.claims, arguments.bindings)
    print(json.dumps(result, indent=1))
    return 0 if result["within_budget"] and result["left_held"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
