#!/usr/bin/env python3
"""S13 budget measurements: RS-B B1 (writer work of a normal run) and B6 (recurring arrivals), RS-10 (1,000 due holds).

Real product operations on disposable stores; no persistence at import time. The suite calls these at the
default scale; run it through the runner for the 100,000-tombstone numbers:

    $PY -B scripts/run_measurement.py packages/codex-agents/scripts/measure_s13.py -- [--tombstones 100000]
"""

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 300, "data_mib": 256, "rss_mib": 2048}  # 100,000 tombstones, three repeats
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
B1_RUNS = 5  # after one unrecorded warm-up run; the median is the figure
B6_LIMIT_BATCHES = 4
DUE_HOLDS_LIMIT_SECONDS = 60
BATCH = 64
VM_TOLERANCE = 16
REPEATS = 3  # whole scenarios repeated on fresh stores; wall-clock figures are the median per batch position
PRODUCTION_LIMIT = 64  # result_persistence.py: history.release_earlier_unarmed_leases(live, limit=64)
EXPIRY_SECONDS = 10.0  # recovery_report.EXPIRY_SECONDS: the driver's maintenance cadence
SELECTION_TABLES = ('result_leases', 'project_holds')


class _Cursor:
    """A cursor whose fetchall sizes are recorded: the size of each selection a pass makes."""

    def __init__(self, cursor, owner):
        self._cursor, self._owner = cursor, owner

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._owner.selections.append(len(rows))
        return rows

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)


class InstrumentedConnection:
    """Wraps a store's SQLite connection: times every COMMIT and records the size of every selection made on the
    lease and hold tables. All other behaviour is the real connection's."""

    def __init__(self, connection):
        self._connection = connection
        self.selections, self.commit_ns = [], 0

    def execute(self, sql, *args):
        text = sql.lstrip()
        if text[:6].upper() == 'COMMIT':
            import time
            start = time.perf_counter_ns()
            try:
                return self._connection.execute(sql, *args)
            finally:
                self.commit_ns += time.perf_counter_ns() - start
        cursor = self._connection.execute(sql, *args)
        if text[:6].upper() == 'SELECT' and any(name in sql for name in SELECTION_TABLES):
            return _Cursor(cursor, self)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


@contextlib.contextmanager
def instrumented(store):
    wrapper = InstrumentedConnection(store._connection)
    real = store._connection
    store._connection = wrapper
    try:
        yield wrapper
    finally:
        store._connection = real


class RegisterTimer:
    """Time every outermost call into the hold register made by a normal run, and the whole of
    `project_release_ack._release_obligation` (its reservation/hold JOIN, the release and the receipt)."""

    def __init__(self):
        self.calls = {}
        self.depth = 0
        self.total_ns = 0

    @contextlib.contextmanager
    def installed(self):
        from codex_agents import project_holds_schema as hs, project_release_ack as ack
        import time
        targets = [(hs, name) for name in ('take_hold', 'arm_hold', 'release_hold', 'wake_before', 'wake_settled')]
        targets.append((ack, '_release_obligation'))
        patches = []
        for module, name in targets:
            original = getattr(module, name)
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
            patches.append(mock.patch.object(module, name, wrapper))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            yield self

    @property
    def milliseconds(self):
        return self.total_ns / 1_000_000


def measure_commit_delta(pairs=9):
    """The commit cost the register's rows add to a transaction. Register writes share the existing transactions,
    so a transaction's commit time cannot be split by row. Instead: pairs of the same transaction on one store, A (a
    single rewrite of an existing hold row, so one page is dirty) and B (A plus the register writes of one normal run:
    three takes, an arm and two releases), interleaved A,B,A,B...; each commit timed. The figure is the median of
    (B commit - A commit), never below zero."""
    from tests import test_result_leases as leases
    from codex_agents import project_holds_schema as hs
    from codex_agents.project_holds_schema import attempt_identity
    deltas, a_samples, b_samples = [], [], []
    with base.fixture() as test:
        test.bound(1)
        test.publish(1)
        store = test.store
        for pair in range(pairs + 1):  # pair 0 is the unrecorded warm-up
            commits = {}
            for kind in ('A', 'B'):
                with instrumented(store) as connection:
                    with store._transaction():
                        store._connection.execute("UPDATE project_holds SET next_check_ms=next_check_ms "
                                                  "WHERE rowid=(SELECT MIN(rowid) FROM project_holds)")
                        if kind == 'B':
                            now = store._hold_now()
                            lease = attempt_identity('hrn_' + f'{pair + 100:048x}', 1)
                            hs.take_hold(store._connection, store.project_id, 'lease', lease, now_ms=now,
                                         lease_case='unarmed', due_at_ms=now + 60_000)
                            hs.take_hold(store._connection, store.project_id, 'open_binding',
                                         attempt_identity('hrn_' + f'{pair + 100:048x}', 2), now_ms=now)
                            hs.arm_hold(store._connection, store.project_id, lease)
                            claim = 'clm_' + f'{pair + 100:048x}'
                            hs.take_hold(store._connection, store.project_id, 'reservation', claim, now_ms=now,
                                         due_at_ms=now + 60_000)
                            hs.release_hold(store._connection, store.project_id, 'lease', lease, 'bound', now_ms=now)
                            hs.release_hold(store._connection, store.project_id, 'reservation', claim, 'completed',
                                            now_ms=now, receipt='a' * 64)
                commits[kind] = connection.commit_ns / 1_000_000
            if pair:
                a_samples.append(commits['A'])
                b_samples.append(commits['B'])
                deltas.append(commits['B'] - commits['A'])
    return {'pairs': pairs, 'a_commit_ms': a_samples, 'b_commit_ms': b_samples, 'delta_samples_ms': deltas,
            'commit_delta_ms': max(0.0, statistics.median(deltas))}


def measure_b1(runs=B1_RUNS):
    """B1: all added writer work of one normal run: reserve, arm, bind, commit, then the release of the reservation
    obligation the account's release acknowledgement makes (`_release_obligation`).

    Counted, per run: (1) the time inside every outermost register call and inside the whole of
    `_release_obligation` (its reservation/hold JOIN included); (2) the commit cost the register rows add
    (`measure_commit_delta`; the run's own commits are also recorded, as `run_commit_ms`, for reference: they include
    the baseline commits and are not added); (3) the memory-hold transactions, commits included. Protocol: one
    unrecorded warm-up run, then `runs` runs; the figure is the median, so one scheduling pause cannot fail it."""
    from tests import test_result_leases as leases
    from codex_agents import project_release_ack
    samples, commits, call_counts = [], [], []
    with base.fixture() as test:
        for number in range(1, runs + 2):  # run 1 is the unrecorded warm-up
            timer = RegisterTimer()
            with instrumented(test.store) as connection, timer.installed():
                test.bound(number)
                test.publish(number)
                target = leases.lease_binding(number)
                with test.store._transaction():
                    lease = test.store._lease_row(target)
                    project_release_ack._release_obligation(test.store, lease, 'a' * 64)
            if number > 1:
                samples.append(timer.milliseconds)
                commits.append(connection.commit_ns / 1_000_000)
                call_counts.append(dict(timer.calls))
        released = test.store._connection.execute(
            "SELECT COUNT(*) FROM project_holds WHERE kind='reservation' AND state='released' "
            "AND release_kind='completed'").fetchone()[0]
    memory = base.memory_writer_samples()
    delta = measure_commit_delta()
    register_ms, run_commit_ms = statistics.median(samples), statistics.median(commits)
    return {'register_samples_ms': samples, 'register_ms': register_ms, 'run_commit_samples_ms': commits,
            'run_commit_ms': run_commit_ms, 'commit_delta': delta, 'commit_ms': delta['commit_delta_ms'],
            'register_calls': call_counts[0],
            'reservations_released_completed': released, 'warmup_runs': 1, 'runs': runs,
            'memory_writer_ms': memory['memory_writer_ms'],
            'memory_transactions_samples_ms': memory['memory_transactions_samples_ms'],
            'total_ms': register_ms + delta['commit_delta_ms'] + memory['memory_writer_ms'], 'limit_ms': B1_LIMIT_MS}


def blocked_start_pass(test, leases, assignment, blocked_count, arrivals, tombstone_count=0):
    """One eligible unarmed lease behind `blocked_count` live (blocked) leases; each batch brings `arrivals` more.

    Per batch the record holds: leases released, the largest selection the pass made (rows fetched at once from
    the lease or hold tables), the number of blocked leases given a back-off (hold `next_check_ms` moved), the
    wall-clock time and the SQLite VM steps. Returns the number of batches until the eligible lease is released."""
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
    batches, released, record = 0, 0, {'batch_ms': [], 'batch_vm_steps': [], 'batch_released': [],
                                       'batch_selected': [], 'batch_backed_off': []}
    while not released and batches < 40:
        add_live(arrivals)  # new blocked rows arrive before every batch, the first included
        batches += 1
        with instrumented(test.store) as connection:
            step, (ms, released) = base.work(connection, lambda: base.elapsed(
                lambda: test.store.release_earlier_unarmed_leases(frozenset(live), limit=BATCH)))
        record['batch_ms'].append(ms)
        record['batch_vm_steps'].append(step)
        record['batch_released'].append(released)
        record['batch_selected'].append(max(connection.selections, default=0))
        record.setdefault('batch_examined', []).append(sum(connection.selections))
        record['batch_backed_off'].append(db.execute(
            "SELECT COUNT(*) FROM project_holds WHERE kind='lease' AND state<>'released' AND next_check_ms>0").fetchone()[0])
    state = db.execute("SELECT state FROM result_leases WHERE run_handle=?",
                       (leases.lease_binding(target).run_handle,)).fetchone()[0]
    return {'batches': batches, 'target_state': state, 'blocked': blocked_count, 'arrivals_per_batch': arrivals,
            **record}


def measure_b6(tombstone_counts=(0,), repeats=REPEATS):
    """B6 on the product's start pass, `repeats` times on fresh stores. Deterministic fields (batches, released,
    selected rows, VM steps) are from the first repeat and must agree in all; each batch's wall-clock figure
    `batch_ms` is the median over the repeats at that batch position (`batch_ms_samples` keeps them all)."""
    from tests import test_result_leases as leases
    from tests import test_b6_unattended_close as b6
    from tests.learning_support import assignment
    rows = []
    for count in tombstone_counts:
        samples = []
        for _ in range(repeats):
            with base.fixture() as test:
                if count:
                    b6.valid_neighbours(test.store, 'orc_' + 'c' * 48, count)
                # Setup only (as B5 does): past the 4,096-lease-row cap every reserve is refused by design.
                with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
                    samples.append(blocked_start_pass(test, leases, assignment, 128, 50, count))
        row = dict(samples[0])
        for other in samples[1:]:
            for key in ('batches', 'batch_released', 'batch_selected', 'batch_backed_off'):
                if other[key] != row[key]:
                    raise RuntimeError(f'B6 repeats disagree on {key}')
        row['batch_ms_samples'] = [sample['batch_ms'] for sample in samples]
        row['batch_ms'] = [statistics.median(values) for values in zip(*row['batch_ms_samples'])]
        row['tombstones'], row['repeats'] = count, repeats
        rows.append(row)
    return rows


def due_rows(store):
    """Held unarmed lease rows due now: the lease pass stops only when this set is empty (RS-B section 12)."""
    return store._connection.execute(
        "SELECT COUNT(*) FROM project_holds WHERE kind='lease' AND state='held' AND lease_case='unarmed' "
        "AND next_check_ms<=?", (store._hold_now(),)).fetchone()[0]


def measure_due_holds(due=1000, repeats=REPEATS, limit=BATCH):
    """RS-10 at the start pass's own batch size: `due` unarmed leases, all due at once, none blocked, drained by
    consecutive `release_earlier_unarmed_leases(limit=64)` calls. Excludes scheduling (see the production figure).
    Repeated on fresh stores; per-batch wall-clock is the median over the repeats at that batch position."""
    from tests import test_result_leases as leases
    from tests.learning_support import assignment
    runs = []
    for _ in range(repeats):
        with base.fixture() as test:
            with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
                for number in range(1, due + 1):
                    test.store.reserve_result_lease(leases.lease_binding(number), assignment())
            batches, total, times, counts = 0, 0, [], []
            while total < due and batches < due:
                ms, released = base.elapsed(lambda: test.store.release_earlier_unarmed_leases(frozenset(), limit=limit))
                batches += 1
                total += released
                times.append(ms)
                counts.append(released)
                if not released and not due_rows(test.store):
                    break  # RS-B section 12 (decision 354): stop only on an empty due set
        runs.append((total, counts, times))
    if any(run[:2] != runs[0][:2] for run in runs):
        raise RuntimeError('due-hold repeats disagree on released counts')
    per_batch = [statistics.median(values) for values in zip(*(run[2] for run in runs))]
    return {'due': due, 'released': runs[0][0], 'batches': len(runs[0][1]), 'batch_released': runs[0][1],
            'batch_ms': per_batch, 'maximum_batch_ms': max(per_batch), 'repeats': repeats,
            'total_seconds': sum(per_batch) / 1000, 'limit_seconds': DUE_HOLDS_LIMIT_SECONDS, 'batch_limit_ms': 50}


def measure_due_holds_production(due=1000, tick=0.5, give_up_seconds=3600.0):
    """RS-10 on the production path: the service's own maintenance step, scheduled by the driver's own cadence.

    The driver's `_expire_results` (the real method) asks the real writer lane for one `expire` pass.
    Its lease pass selects at most 64 rows; full passes or passes that backed rows off continue on the next
    driver step, otherwise EXPIRY_SECONDS applies. The clock is virtual (the cadence is
    counted, not slept); each pass's real duration is added to it. The result is the time until `due` holds are
    released, and how many passes that took."""
    import time
    from types import SimpleNamespace
    from tests import test_result_leases as leases
    from tests import test_codex_worker as workers
    from tests.learning_support import assignment
    from codex_agents.project_driver import RunDriver
    from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceLane
    with base.fixture() as test:
        with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
            for number in range(1, due + 1):
                test.store.reserve_result_lease(leases.lease_binding(number), assignment())
        test.store.close()
        pool = workers.CodexWorkerTests(methodName='runTest')
        pool.setUp()
        capacity = test.fixture.capacity
        lane = ResultPersistenceLane(pool.owner, pool.pool, lambda: test.fixture.open(result_capacity=capacity),
                                     max_entries=4, retained_bytes=4 * MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=True)
        lane._project, lane._runtime = leases.lease_binding(1).project_id, leases.lease_binding(1).runtime_digest
        directory = test.fixture.directory / 'history.sqlite3'
        def remaining():
            with contextlib.closing(sqlite3.connect(f'file:{directory}?mode=ro', uri=True)) as reader:
                return reader.execute("SELECT COUNT(*) FROM result_leases WHERE state='reserved'").fetchone()[0]
        clock = [0.0]
        driver = SimpleNamespace(_expiry=None, _next_expiry=0.0, _clock=lambda: clock[0],
                                 _runtime=SimpleNamespace(persistence=lane))
        passes, pass_ms, progress, finished_at = 0, [], [], []
        try:
            while remaining() and clock[0] < give_up_seconds:
                issued = driver._expiry
                RunDriver._expire_results(driver)
                if driver._expiry is not None and driver._expiry is not issued:
                    start = time.perf_counter()
                    while driver._expiry.state == 'queued':
                        if time.perf_counter() - start > 30:
                            raise RuntimeError('the writer did not answer')
                        time.sleep(0.001)
                    took = time.perf_counter() - start
                    passes += 1
                    pass_ms.append(took * 1000)
                    clock[0] += took
                    progress.append(due - remaining())
                    finished_at.append(clock[0])
                clock[0] += tick
            left = remaining()
        finally:
            lane.close()
            lane._writer.join(6)
            pool.doCleanups()
    return {'due': due, 'released': due - left, 'passes': passes, 'pass_limit': PRODUCTION_LIMIT,
            'cadence_seconds': EXPIRY_SECONDS, 'seconds_until_drained': clock[0] if not left else None,
            'seconds_elapsed': clock[0], 'released_by_pass': progress[:12], 'pass_ms_median': statistics.median(pass_ms),
            'limit_seconds': DUE_HOLDS_LIMIT_SECONDS,
            'released_within_60_s': max([done for done, at in zip(progress, finished_at) if at <= DUE_HOLDS_LIMIT_SECONDS],
                                        default=0)}


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
                      'due_holds': measure_due_holds(), 'due_holds_production': measure_due_holds_production()},
                     indent=2, sort_keys=True))
