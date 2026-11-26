#!/usr/bin/env python3
"""B7b measurements of actual store operations. No persistence at import time.

Run through tests.test_b7b_budgets in the named test runner. Unimplemented
scheduler measurements remain unmeasured. Service startup uses real listeners
when Unix socket binding is permitted; store proxies never establish that budget.
"""
import contextlib
import json
import statistics
import socket
import sqlite3
import tempfile
from pathlib import Path
import time
from unittest import mock


def elapsed(call):
    start = time.perf_counter_ns()
    value = call()
    return (time.perf_counter_ns() - start) / 1_000_000, value


def median_elapsed(call):
    """One unrecorded warm-up, then three actual operation timings."""
    call()
    samples = [elapsed(call)[0] for _ in range(3)]
    return statistics.median(samples), samples


def socket_unavailable_reason():
    from tests.support import SHORT_TMP
    with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(Path(directory) / 'b.sock'))
        except OSError as error:
            return f'Unix socket bind unavailable: {type(error).__name__} (errno={error.errno}).'
    return None


@contextlib.contextmanager
def service_startup_timer(test):
    """Real service readiness: writer bootstrap, driver, listeners, published hint.

    The same product host configuration is used for empty and populated stores.
    The real RunDriver is constructed; no run or provider call is submitted.
    """
    from tests.support import SHORT_TMP, BUNDLE_ROOT
    from tests.test_project_results import KEY
    from tests.test_project_runtime import limits
    from codex_agents.collector_paths import CollectorPaths
    from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
    from codex_agents.orchestration_api import OrchestrationScope
    from codex_agents.service_host import ProjectServiceHost, ServiceHostLimits
    with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory:
        root = Path(directory)
        state, project = root / 's', root / 'p'
        state.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        (state / 'digest.key').write_bytes(KEY)
        (state / 'digest.key').chmod(0o600)
        (state / 'projects').mkdir(mode=0o700)
        (state / 'projects' / test.store.project_id).mkdir(mode=0o700)
        paths = CollectorPaths(state)
        scope = OrchestrationScope(test.store.project_id, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
        def sample():
            # The host's writer opens/closes its own connection on its own thread.
            # Close the setup writer before timing; reopen it only after shutdown.
            test.store.close()
            host = None
            try:
                ms, host = elapsed(lambda: ProjectServiceHost(paths, project, scope,
                    test.fixture.open, limits(), ServiceHostLimits(4, 500_000_000, 16,
                        30_000_000_000, 30_000, 64), bundle_root=BUNDLE_ROOT))
                return ms
            finally:
                if host is not None:
                    host.close()
                    deadline = time.monotonic() + 6
                    while not host.threads_stopped():
                        if time.monotonic() >= deadline:
                            raise RuntimeError('measurement service threads did not stop')
                        time.sleep(.002)
                    host.settle_lost_runs_later()  # Release this idle driver's journal lock.
                test.fixture.store = test.fixture.open()
        yield sample


def startup_samples(sample):
    sample()  # Warm up a complete real startup, excluding shutdown from timing.
    return [sample() for _ in range(3)]


def record_startup(result, empty, populated, limit, unavailable):
    result['service_startup_status'] = 'unmeasured'
    if unavailable:
        result['service_startup_unmeasured_reason'] = unavailable
        return
    overhead = statistics.median(populated) - statistics.median(empty)
    result['numbers'].update(empty_service_startup_ms=empty,
        populated_service_startup_ms=populated, service_start_overhead_ms=overhead,
        service_start_overhead_limit_ms=limit)
    result['service_startup_status'] = 'met' if overhead <= limit else 'failed'


def work(db, call):
    count = [0]
    def tick():
        count[0] += 1
        return 0
    db.set_progress_handler(tick, 1)
    try:
        value = call()
    finally:
        db.set_progress_handler(None, 0)
    return count[0], value


@contextlib.contextmanager
def fixture():
    from tests import test_deadlines_retention as dr
    from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
    test = dr.LeaseBackedFixture(methodName='runTest')
    test.setUp()
    test.fixture.capacity = ResultCapacity(2048, 2048 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
    test.base.reopen(result_capacity=test.fixture.capacity)
    try:
        yield test
    finally:
        test.doCleanups()


def tombstones(db, project, count):
    # Valid released custody rows and expired context source rows, one atomic setup.
    db.execute('BEGIN')
    try:
        db.executemany("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,release_kind) "
                       "VALUES(?,'result_custody',?,'released',0,1,'retention')",
                       ((project, f'hrn_{n+10000:048x}:1') for n in range(count)))
        db.executemany("INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,"
                       "lease_deadline_ms) VALUES(?,?,1,'expired',0,0)",
                       ((project, f'hrn_{n+10000:048x}') for n in range(count)))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def recovery_work(count):
    """Real recovery SELECTs; valid sealed source tombstones share an open owner.

    The empty owner forces both EXISTS branches to finish. A second owner has a
    live closing-denied lease, so the pending join must actually probe its source.
    """
    from tests import test_b6_unattended_close as b6
    from codex_agents.recovery_report import recovery_pending
    test = b6.LeaseTests(methodName='runTest')
    test.setUp()
    try:
        empty = test.open('empty')['orchestration_handle']
        live = test.open('live')['orchestration_handle']
        test.lease(live)
        test.store.arm_result_lease(test.lease_binding)
        db = test.store._connection
        db.execute("UPDATE project_holds SET lease_case='closing_denied' WHERE kind='lease'")
        b6.valid_neighbours(test.store, empty, count)
        from codex_agents.recovery_report import RecoveryNotes, RECOVERY_REASONS
        test.store._recovery_notes = RecoveryNotes()
        test.store._recovery_notes.publish(dict.fromkeys(RECOVERY_REASONS, 0))
        statements = []
        db.set_trace_callback(statements.append)
        try:
            steps, value = work(db, lambda: recovery_pending(test.store))
        finally:
            db.set_trace_callback(None)
        has_holds_sql = next(sql for sql in statements if sql.startswith('SELECT COUNT(*) FROM project_orchestrations'))
        has_steps, has = work(db, lambda: db.execute(has_holds_sql).fetchone()[0])
        return {'recovery_pending_vm_steps': steps, 'has_holds_vm_steps': has_steps, 'has_holds': has,
                'released_leases': db.execute("SELECT COUNT(*) FROM result_leases WHERE state='expired'").fetchone()[0],
                'released_reservations': db.execute("SELECT COUNT(*) FROM result_reservations WHERE state='expired'").fetchone()[0],
                'pending_settlement': value['reasons']['needs_settlement']}
    finally:
        test.doCleanups()



def memory_writer_samples():
    """B2's actual memory transactions including commit, not a full normal-run proxy."""
    from tests.test_memory_holds import MemoryHoldTests
    from codex_agents import memory_holds
    samples, recovery = [], []
    for trial in range(4):
        test = MemoryHoldTests(methodName='runTest')
        test.setUp()
        try:
            calls = (test.register, test.prep, lambda: test.release('preparation'),
                     lambda: test.release('driver_entry'))
            times = [elapsed(call)[0] for call in calls]
            if trial:
                samples.append(times)
            test.key = ('hrn_' + 'd'*48,1)
            test.register()
            test.prep()
            test.journal.keep_lock = True
            test.journal.release()
            ms, _ = elapsed(lambda: memory_holds.recover(test.store,test.journal.directory,'current'))
            released = test.store._connection.execute("SELECT COUNT(*) FROM project_holds WHERE kind IN "
                "('driver_entry','preparation') AND release_kind='service_stopped'").fetchone()[0]
            if released != 2:
                raise RuntimeError('memory recovery did not release its two proven-stopped rows')
            if trial:
                recovery.append(ms)
        finally:
            test.doCleanups()
    return {'memory_transactions_samples_ms':samples,'memory_writer_ms':statistics.median(map(sum,samples)),
            'memory_recovery_samples_ms':recovery,'memory_recovery_ms':statistics.median(recovery),
            'memory_rows_released':2,'memory_limit_ms':5,'memory_warmup_runs':1}


def measure():
    from tests import test_result_leases as leases
    from tests.learning_support import assignment
    from tests.test_project_history import PROJECT_A, binding
    from codex_agents import project_holds_schema as hs, project_result_contexts as contexts
    from codex_agents.project_holds_report import _holds
    from codex_agents.history_contracts import HistoryError
    result = {}
    unavailable = socket_unavailable_reason()
    with fixture() as test:
        costs = []
        # Measure the register calls actually made by reserve/arm/bind/commit. This
        # excludes the normal acknowledgement/cleanup/release tail and says so.
        def timed(original):
            def invoke(*args, **kwargs):
                ms, value = elapsed(lambda: original(*args, **kwargs))
                costs.append(ms)
                return value
            return invoke
        with mock.patch.object(hs, 'take_hold', timed(hs.take_hold)), \
             mock.patch.object(hs, 'arm_hold', timed(hs.arm_hold)), \
             mock.patch.object(hs, 'release_hold', timed(hs.release_hold)):
            test.bound()
            test.publish()
        result['B1'] = {'status': 'unmeasured', 'numbers': {'partial_register_ms': sum(costs), 'calls': len(costs)},
            'unmeasured_reason': 'Full normal-run writer delta including the account release tail is not measured; memory transactions are measured separately.',
            'scope': 'Actual reserve/arm/bind/commit register calls; excludes release tail and transaction commit cost. Full normal-run writer delta is not established.'}
    with fixture() as test:
        _, baseline = median_elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))
        empty_service = populated_service = None
        if unavailable is None:
            with service_startup_timer(test) as sample:
                empty_service = startup_samples(sample)
        # Setup only: all 1,064 ordinary lease writes still run; avoid quadratic
        # repeated capacity verification while constructing this below-cap fixture.
        # Run the unmodified verifier once, and reopen/validate before measuring.
        with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
            for number in range(1, 1065):
                test.store.reserve_result_lease(leases.lease_binding(number), assignment())
        test.store._check_result_capacity()
        _, populated = median_elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))
        if unavailable is None:
            with service_startup_timer(test) as sample:
                populated_service = startup_samples(sample)
        overhead = statistics.median(populated) - statistics.median(baseline)
        result['B2'] = {'status': 'unmeasured', 'numbers': {'empty_open_ms': baseline, 'held_1064_open_ms': populated,
                         'store_overhead_ms': overhead, 'limit_ms': 100},
            'scope': 'Real store reopen proxy; when binding works, complete ProjectServiceHost startup with writer bootstrap, driver, listeners and published hint at empty/1,064 held leases.'}
        record_startup(result['B2'], empty_service, populated_service, 100, unavailable)
        result['B2']['status'] = result['B2']['service_startup_status']
        if unavailable:
            result['B2']['unmeasured_reason'] = unavailable
        blocked = frozenset((leases.lease_binding(n).run_handle, 1) for n in range(1, 65))
        # Lease-only drain has no files: restore an identical SQLite snapshot
        # outside timing so every trial includes real durable release commits.
        db = test.store._connection
        with contextlib.closing(sqlite3.connect(':memory:')) as snapshot:
            db.backup(snapshot)
            trials = []
            for trial in range(4):
                snapshot.backup(db)
                batches, total, times = 0, 0, []
                while total < 1000 and batches < 19:
                    ms, released = elapsed(lambda: test.store.release_earlier_unarmed_leases(blocked, limit=64))
                    batches += 1
                    total += released
                    times.append(ms)
                    if not released and not db.execute(
                            "SELECT 1 FROM project_holds WHERE kind='lease' AND state='held' AND lease_case='unarmed' "
                            "AND next_check_ms<=? LIMIT 1", (test.store._hold_now(),)).fetchone():
                        break  # RS-B section 12 (decision 354): continue past backed-off rows; stop on an empty due set
                trials.append((total, batches, times))
        if any((row[0], row[1]) != (total, batches) for row in trials):
            raise RuntimeError('B3/B5 repeated drain changed released counts')
        samples = list(map(list, zip(*(row[2] for row in trials[1:]))))
        times = [statistics.median(values) for values in samples]
        result['B3'] = {'status': 'met' if max(times) < 50 else 'failed',
            'numbers': {'batch_ms': times, 'batch_samples_ms': samples, 'warmup_runs': 1,
                       'maximum_ms': max(times), 'limit_ms': 50},
            'scope': 'Real release_earlier_unarmed_leases(limit=64), durable transactions included; maximum per-batch median of three identical drain passes after one warm-up pass.'}
        result['B5'] = {'status': 'met' if total == 1000 and batches <= 18 else 'failed',
            'numbers': {'eligible': 1000, 'blocked': 64, 'released': total, 'batches': batches, 'limit_batches': 18},
            'scope': 'Real unarmed-lease start pass; 1,000 eligible leases behind 64 live leases. Does not establish memory-kind drain.'}
        # B6: live rows stand in for cheap blocked rows, but the missing generic
        # scheduler/backoff cannot be established by this cheaper operation.
        live = set(blocked)
        for n in range(1065, 1129):
            test.store.reserve_result_lease(leases.lease_binding(n), assignment())
            live.add((leases.lease_binding(n).run_handle, 1))
        target = 1129
        test.store.reserve_result_lease(leases.lease_binding(target), assignment())
        for n in range(1130, 1180):
            test.store.reserve_result_lease(leases.lease_binding(n), assignment())
            live.add((leases.lease_binding(n).run_handle, 1))
        ms, released = elapsed(lambda: test.store.release_earlier_unarmed_leases(frozenset(live), limit=64))
        result['B6'] = {'status': 'unmeasured', 'numbers': {'blocked': 128, 'arrivals': 50, 'released': released,
                          'store_pass_ms': ms, 'store_batches': 1, 'limit_batches': 4},
            'unmeasured_reason': 'The generic recurring-arrival/re-due scheduler is not measured; memory recovery is measured separately.',
            'scope': 'Real lease pass releases target behind 128 live rows plus 50 arrivals. The generic re-due/backoff recurring-arrival scenario is not established by this lease pass.'}
    memory = memory_writer_samples()
    result['B1']['numbers'].update(memory)
    result['B6']['numbers'].update(memory_rows_released=memory['memory_rows_released'],
        memory_recovery_ms=memory['memory_recovery_ms'],memory_recovery_samples_ms=memory['memory_recovery_samples_ms'])
    comparisons = []
    for count in (0, 100000):
        with fixture() as test:
            for n in range(1, 11):
                test.bound(n)
                test.publish(n)
            tombstones(test.store._connection, PROJECT_A, count)
            db = test.store._connection
            # Reset SQLite VM instruction counters per actual product operation.
            hold_steps, holds = work(db, lambda: _holds(test.store))
            sweep_steps, rows = work(db, lambda: contexts._sweep_candidates(test.store, None, 10))
            reconcile_steps, _ = work(db, lambda: contexts.reconcile(test.store))
            def lookup():
                try:
                    contexts.stored_worker_context(test.store, 'f' * 64, host_class='codex')
                except HistoryError:
                    pass
            lookup_steps, _ = work(db, lookup)
            lookup_ms, lookup_samples = median_elapsed(lookup)
            retirement_samples, retirement_counts, retirement_work = [], [], []
            for trial in range(4):
                if trial:
                    test.now = 1000 + 120001 * trial
                    from tests import test_record_context as rc
                    for n in range(10 * trial + 1, 10 * trial + 11):
                        test.store.reserve_result_lease(leases.lease_binding(n), assignment(),
                            record_context=rc.simple_context(n))
                        test.store.arm_result_lease(leases.lease_binding(n))
                        test.base.bind(n, hard_deadline_at_ms=test.now + 1000)
                        test.publish(n)
                test.now = 121000 + 120001 * trial
                steps, (ms, retired) = work(db, lambda: elapsed(lambda: test.store.expire_results(limit=10)))
                retirement_counts.append(retired)
                retirement_work.append(steps)
                if trial:
                    retirement_samples.append(ms)
            if retirement_counts != [10] * 4:
                raise RuntimeError('retirement trial did not retire ten real results')
            retirement_steps = retirement_work[1]
            retirement_ms = statistics.median(retirement_samples)
            plans = [tuple(row) for row in db.execute('EXPLAIN QUERY PLAN SELECT * FROM project_result_contexts '
                "INDEXED BY project_result_contexts_live WHERE state NOT IN ('ended','expired')")]
            comparisons.append({'tombstones': count, 'holds_vm_steps': hold_steps, 'context_sweep_vm_steps': sweep_steps,
                'reconcile_vm_steps': reconcile_steps, 'lookup_vm_steps': lookup_steps, 'lookup_ms': lookup_ms,
                'retirement_ms': retirement_ms, 'retirement_samples_ms': retirement_samples,
                'lookup_samples_ms': lookup_samples, 'warmup_runs': 1,
                'retirement_vm_steps': retirement_steps, 'retirement_vm_steps_samples': retirement_work[1:],
                'retired': retired, 'live_index_plan': plans})
        comparisons[-1].update(recovery_work(count))
    equal = all(abs(comparisons[0][key] - comparisons[1][key]) <= 16 for key in
                ('holds_vm_steps', 'context_sweep_vm_steps', 'reconcile_vm_steps', 'lookup_vm_steps',
                 'retirement_vm_steps', 'recovery_pending_vm_steps', 'has_holds_vm_steps'))
    result['B4'] = {'status': 'met' if equal else 'failed', 'numbers': {'comparisons': comparisons},
        'scope': 'Actual hold report, context candidate sweep, reconciliation, absent outcome lookup and recovery_pending/has_holds at 0/100,000 custody/context and valid sealed released lease/reservation tombstones. VM work must differ by at most 16 instructions.'}
    with fixture() as test:
        empty, _ = median_elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))
        empty_service = populated_service = None
        if unavailable is None:
            with service_startup_timer(test) as sample:
                empty_service = startup_samples(sample)
        from tests import test_record_context as rc
        for n in range(1, 65):
            test.store.reserve_result_lease(leases.lease_binding(n), assignment(), record_context=rc.simple_context(n))
        live_open, _ = median_elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))
        if unavailable is None:
            with service_startup_timer(test) as sample:
                populated_service = startup_samples(sample)
    result['S3'] = {'status': 'met' if all(row['retirement_ms'] < 50 and row['lookup_ms'] < 5 for row in comparisons) else 'failed',
                   'service_startup_status': 'unmeasured', 'numbers': {'comparisons': comparisons,
                     'empty_store_open_ms': empty, 'live_64_store_open_ms': live_open,
                     'store_start_overhead_ms': live_open - empty, 'retirement_limit_ms': 50, 'lookup_limit_ms': 5,
                     'service_start_overhead_limit_ms': 200},
        'scope': 'Real retirement of ten committed bodies/contexts (VM counting enabled, so time includes tracing overhead), absent outcome lookup and context reconciliation at 0/100,000 tombstones; store startup with 64 context files. Median of three after one warm-up. Successful authenticated lookup is measured separately; full ProjectServiceHost startup is measured when sockets bind.'}
    record_startup(result['S3'], empty_service, populated_service, 200, unavailable)
    return result


def measure_lookup():
    """Successful authenticated outcome lookup, including context verification/file read."""
    from tests import test_record_context as rc
    from codex_agents.hosts import CODEX_HOST_CLIENT
    values = []
    for count in (0, 100000):
        test = rc.RecordedContextFixture(methodName='runTest')
        test.setUp()
        try:
            test.recorded()
            db = test.store._connection
            tombstones(db, test.store.project_id, count)
            call = lambda: test.store.stored_worker_context(test.digest, host_class=CODEX_HOST_CLIENT)
            steps, context = work(db, call)
            median, times = median_elapsed(call)
            values.append({'tombstones': count, 'lookup_ms': median, 'lookup_samples_ms': times, 'warmup_runs': 1,
                           'lookup_vm_steps': steps, 'limit_ms': 5})
        finally:
            test.doCleanups()
    return values


if __name__ == '__main__':
    print(json.dumps({'budgets': measure(), 'successful_lookup': measure_lookup()}, indent=2, sort_keys=True))
