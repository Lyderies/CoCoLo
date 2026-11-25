#!/usr/bin/env python3
"""S13 budget measurements: RS-B B1 (writer work of a normal run) and B6 (recurring arrivals), RS-10 (1,000 due holds).

Real product operations on disposable stores; no persistence at import time. The suite calls these at the
default scale; run this file directly for the 100,000-tombstone numbers:

    python -B scripts/measure_s13.py [--tombstones 100000]
"""
import argparse
import contextlib
import json
import statistics
import sqlite3
import sys
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_b7b as base  # noqa: E402

B1_LIMIT_MS = 5
B6_LIMIT_BATCHES = 4
DUE_HOLDS_LIMIT_SECONDS = 60
BATCH = 64
VM_TOLERANCE = 16


class RegisterTimer:
    """Time every outermost call into the hold register (project_holds_schema) made by a normal run."""

    NAMES = ('take_hold', 'arm_hold', 'release_hold', 'wake_before', 'wake_settled')

    def __init__(self):
        self.calls = {}
        self.depth = 0
        self.total_ns = 0

    @contextlib.contextmanager
    def installed(self):
        from codex_agents import project_holds_schema as hs
        import time
        patches = []
        for name in self.NAMES:
            original = getattr(hs, name)
            def wrapper(*args, _original=original, _name=name, **kwargs):
                if self.depth:
                    return _original(*args, **kwargs)
                self.depth += 1
                start = time.perf_counter_ns()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.total_ns += time.perf_counter_ns() - start
                    self.calls[_name] = self.calls.get(_name, 0) + 1
                    self.depth -= 1
            patches.append(mock.patch.object(hs, name, wrapper))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            yield self

    @property
    def milliseconds(self):
        return self.total_ns / 1_000_000


def measure_b1(runs=3):
    """B1: all register transactions of one normal run, in the writer: reserve, arm, bind, commit, then the
    release of the reservation obligation the account's release acknowledgement makes (`_release_obligation`).
    The register writes share the existing transactions, so their cost is the time spent inside the register
    calls; memory-hold transactions (register, preparation, driver entry, release) are added on top."""
    from tests import test_result_leases as leases
    from codex_agents import project_release_ack
    samples, call_counts = [], []
    with base.fixture() as test:
        for number in range(1, runs + 2):  # run 1 is the unrecorded warm-up
            timer = RegisterTimer()
            with timer.installed():
                test.bound(number)
                test.publish(number)
                target = leases.lease_binding(number)
                with test.store._transaction():
                    lease = test.store._lease_row(target)
                    project_release_ack._release_obligation(test.store, lease, 'a' * 64)
            if number > 1:
                samples.append(timer.milliseconds)
                call_counts.append(dict(timer.calls))
        released = test.store._connection.execute(
            "SELECT COUNT(*) FROM project_holds WHERE kind='reservation' AND state='released' "
            "AND release_kind='completed'").fetchone()[0]
    memory = base.memory_writer_samples()
    register_ms = statistics.median(samples)
    return {'register_samples_ms': samples, 'register_ms': register_ms, 'register_calls': call_counts[0],
            'reservations_released_completed': released, 'warmup_runs': 1,
            'memory_writer_ms': memory['memory_writer_ms'],
            'memory_transactions_samples_ms': memory['memory_transactions_samples_ms'],
            'total_ms': register_ms + memory['memory_writer_ms'], 'limit_ms': B1_LIMIT_MS}


def blocked_start_pass(test, leases, assignment, blocked_count, arrivals, tombstone_count=0):
    """One eligible unarmed lease behind `blocked_count` live (blocked) leases; each batch brings `arrivals` more.

    A blocked (live) lease is never released and never counts against the batch limit; every batch looks at
    all of them again (they are re-due on every pass: the start pass keeps no back-off). Returns the number of
    batches until the eligible lease is released, and the work of the passes."""
    live, number = set(), 10_000_000 if tombstone_count else 0
    def add_live(count):
        nonlocal number
        for _ in range(count):
            number += 1
            test.store.reserve_result_lease(leases.lease_binding(number), assignment())
            live.add((leases.lease_binding(number).run_handle, 1))
    add_live(blocked_count)
    number += 1
    target = number
    test.store.reserve_result_lease(leases.lease_binding(target), assignment())
    db = test.store._connection
    batches, released, times, steps = 0, 0, [], []
    while not released and batches < 40:
        add_live(arrivals)  # new blocked rows arrive before every batch, the first included
        batches += 1
        step, (ms, released) = base.work(db, lambda: base.elapsed(
            lambda: test.store.release_earlier_unarmed_leases(frozenset(live), limit=BATCH)))
        times.append(ms)
        steps.append(step)
    state = db.execute("SELECT state FROM result_leases WHERE run_handle=?",
                       (leases.lease_binding(target).run_handle,)).fetchone()[0]
    return {'batches': batches, 'target_state': state, 'blocked': blocked_count, 'arrivals_per_batch': arrivals,
            'batch_ms': times, 'batch_vm_steps': steps}


def measure_b6(tombstone_counts=(0,)):
    from tests import test_result_leases as leases
    from tests import test_b6_unattended_close as b6
    from tests.learning_support import assignment
    rows = []
    for count in tombstone_counts:
        with base.fixture() as test:
            if count:
                b6.valid_neighbours(test.store, 'orc_' + 'c' * 48, count)
            # Setup only (as B5 does): past the 4,096-lease-row cap every reserve is refused by design.
            with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
                row = blocked_start_pass(test, leases, assignment, 128, 50, count)
            row['tombstones'] = count
            rows.append(row)
    return rows


def measure_due_holds(due=1000):
    """RS-10: `due` unarmed leases, all due at once, none blocked: the start pass drains them in batches of 64."""
    from tests import test_result_leases as leases
    from tests.learning_support import assignment
    with base.fixture() as test:
        with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
            for number in range(1, due + 1):
                test.store.reserve_result_lease(leases.lease_binding(number), assignment())
        batches, total, times = 0, 0, []
        while total < due and batches < due:
            ms, released = base.elapsed(lambda: test.store.release_earlier_unarmed_leases(frozenset(), limit=BATCH))
            batches += 1
            total += released
            times.append(ms)
            if not released:
                break
    return {'due': due, 'released': total, 'batches': batches, 'total_seconds': sum(times) / 1000,
            'maximum_batch_ms': max(times), 'limit_seconds': DUE_HOLDS_LIMIT_SECONDS, 'batch_limit_ms': 50}


def vm_flat(rows, key):
    values = [row[key] for row in rows]
    return max(values) - min(values) <= VM_TOLERANCE if len(values) > 1 else True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tombstones', type=int, default=100000)
    args = parser.parse_args()
    b6_rows = measure_b6((0, args.tombstones))
    print(json.dumps({'B1': measure_b1(), 'B6': b6_rows,
                      'B6_start_pass_work_flat': vm_flat([{'v': sum(r['batch_vm_steps'])} for r in b6_rows], 'v'),
                      'due_holds': measure_due_holds()}, indent=2, sort_keys=True))
