#!/usr/bin/env python3
"""B7b measurements of actual store operations. No persistence at import time.

Run through tests.test_b7b_budgets in the named test runner. Unimplemented
scheduler/service measurements remain unmeasured, even when a store proxy is fast.
"""
import contextlib
import json
import statistics
import time
from unittest import mock


def elapsed(call):
    start = time.perf_counter_ns()
    value = call()
    return (time.perf_counter_ns() - start) / 1_000_000, value


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


def measure():
    from tests import test_result_leases as leases
    from tests.learning_support import assignment
    from tests.test_project_history import PROJECT_A, binding
    from codex_agents import project_holds_schema as hs, project_result_contexts as contexts
    from codex_agents.project_holds_report import _holds
    from codex_agents.history_contracts import HistoryError
    result = {}
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
            'scope': 'Actual reserve/arm/bind/commit register calls; excludes release tail and transaction commit cost. Full normal-run writer delta is not established.'}
    with fixture() as test:
        baseline = [elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))[0] for _ in range(3)]
        # Setup only: all 1,064 ordinary lease writes still run; avoid quadratic
        # repeated capacity verification while constructing this below-cap fixture.
        # Run the unmodified verifier once, and reopen/validate before measuring.
        with mock.patch.object(test.store, '_check_result_capacity', return_value=None):
            for number in range(1, 1065):
                test.store.reserve_result_lease(leases.lease_binding(number), assignment())
        test.store._check_result_capacity()
        populated = [elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))[0] for _ in range(3)]
        overhead = statistics.median(populated) - statistics.median(baseline)
        result['B2'] = {'status': 'unmeasured', 'numbers': {'empty_open_ms': baseline, 'held_1064_open_ms': populated,
                         'store_overhead_ms': overhead, 'limit_ms': 100},
            'scope': 'Real store reopen with 1,064 held leases, including validators; whole service startup needs sockets and is unmeasured.'}
        blocked = frozenset((leases.lease_binding(n).run_handle, 1) for n in range(1, 65))
        batches, total, times = 0, 0, []
        while total < 1000 and batches < 19:
            ms, released = elapsed(lambda: test.store.release_earlier_unarmed_leases(blocked, limit=64))
            batches += 1
            total += released
            times.append(ms)
            if not released:
                break
        result['B3'] = {'status': 'met' if max(times) < 50 else 'failed',
            'numbers': {'batch_ms': times, 'maximum_ms': max(times), 'limit_ms': 50},
            'scope': 'Real release_earlier_unarmed_leases(limit=64); individual durable source/register transactions included.'}
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
            'scope': 'Real lease pass releases target behind 128 live rows plus 50 arrivals. Generic re-due/backoff register scheduler and memory holds do not exist; required recurring-arrival scenario is not established.'}
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
            lookup_ms, _ = elapsed(lookup)
            test.now = 121000
            retirement_steps, (retirement_ms, retired) = work(
                db, lambda: elapsed(lambda: test.store.expire_results(limit=10)))
            plans = [tuple(row) for row in db.execute('EXPLAIN QUERY PLAN SELECT * FROM project_result_contexts '
                "INDEXED BY project_result_contexts_live WHERE state NOT IN ('ended','expired')")]
            comparisons.append({'tombstones': count, 'holds_vm_steps': hold_steps, 'context_sweep_vm_steps': sweep_steps,
                'reconcile_vm_steps': reconcile_steps, 'lookup_vm_steps': lookup_steps, 'lookup_ms': lookup_ms,
                'retirement_ms': retirement_ms, 'retirement_vm_steps': retirement_steps,
                'retired': retired, 'live_index_plan': plans})
    equal = all(comparisons[0][key] == comparisons[1][key] for key in
                ('holds_vm_steps', 'context_sweep_vm_steps', 'reconcile_vm_steps', 'lookup_vm_steps', 'retirement_vm_steps'))
    result['B4'] = {'status': 'met' if equal else 'failed', 'numbers': {'comparisons': comparisons},
        'scope': 'Actual hold report, context candidate sweep, reconciliation and absent outcome lookup at 0/100,000 valid custody tombstones. VM instructions are recorded; source lease/reservation tombstone growth is not covered.'}
    with fixture() as test:
        empty = elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))[0]
        from tests import test_record_context as rc
        for n in range(1, 65):
            test.store.reserve_result_lease(leases.lease_binding(n), assignment(), record_context=rc.simple_context(n))
        live_open = elapsed(lambda: test.base.reopen(result_capacity=test.fixture.capacity))[0]
    result['S3'] = {'status': 'unmeasured', 'numbers': {'comparisons': comparisons,
                     'empty_store_open_ms': empty, 'live_64_store_open_ms': live_open,
                     'store_start_overhead_ms': live_open - empty, 'retirement_limit_ms': 50, 'lookup_limit_ms': 5,
                     'service_start_overhead_limit_ms': 200},
        'scope': 'Real retirement of ten committed bodies/contexts (VM counting enabled, so time includes tracing overhead), absent outcome lookup and context reconciliation at 0/100,000 tombstones; store startup with 64 context files. Successful authenticated lookup is measured separately by measure_lookup; full service startup remains unmeasured.'}
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
            times = [elapsed(call)[0] for _ in range(5)]
            values.append({'tombstones': count, 'lookup_ms': max(times), 'lookup_samples_ms': times,
                           'lookup_vm_steps': steps, 'limit_ms': 5})
        finally:
            test.doCleanups()
    return values


if __name__ == '__main__':
    print(json.dumps({'budgets': measure(), 'successful_lookup': measure_lookup()}, indent=2, sort_keys=True))
