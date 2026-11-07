"""Synthetic blocked-spawn fixture, executed only by the pool regression tests."""

from contextlib import ExitStack
from pathlib import Path
import gc
import json
import os
import shutil
import subprocess
import sys
import threading
from threading import local as thread_local
import time
import weakref
from unittest import mock


def main():
    package_root = Path(sys.argv[1]).resolve()
    inject_failure = len(sys.argv) == 3 and sys.argv[2] == 'fail-after-offer'
    sys.path[:0] = [str(package_root), str(package_root / 'native-package/.codex/orchestration')]
    from codex_agents import signature_worker_pool as pools
    from codex_agents import signature_execution as execution
    import tests.test_signature_inputs as capture_fixture
    from tests.learning_support import attest_provider
    from codex_agents.signature_job_intake import SignatureIntakeLimits, SignatureJobIntake

    fixture = capture_fixture.SignatureInputOwnerTests('test_capture_matches_legacy_inputs_without_crypto_connections_or_writes')
    fixture.setUp()
    pool = None
    context_a = None
    release = threading.Event()
    patches = ExitStack()
    offered, execution_events, processes, scratch = [], {}, [], set()
    report = None
    try:
        attest_provider(fixture.ledger, fixture.projects[1], fixture.key, 'codex', 'b' * 64)
        intake = SignatureJobIntake(fixture.views, SignatureIntakeLimits(2, 1, 8, 65536, 2))
        pool = pools.SignatureWorkerPool(intake, pools.SignatureWorkerLimits(2, 4 * 1048576, 30_000_000_000, 10_000_000, 5_000_000_000))
        pool.bootstrap()
        handle_a = intake.capture(fixture.projects[0], 'codex', fixture.work_limits)
        handle_b = intake.capture(fixture.projects[1], 'codex', fixture.work_limits)
        local = thread_local()
        blocked = threading.Event()
        real_popen = subprocess.Popen
        original_sample = execution._Context.sample
        original_execute = execution._execute_signature_job
        launches = []

        def observe(context, *args, **kwargs):
            nonlocal context_a
            local.project = context.inputs.project_id
            if context.scratch is not None:
                scratch.add(context.scratch)
            if local.project == fixture.projects[0]:
                context_a = context
            return original_sample(context, *args, **kwargs)

        def execute(inputs, *args, **kwargs):
            ended = execution_events[id(inputs)]
            try:
                return original_execute(inputs, *args, **kwargs)
            finally:
                ended.set()

        def offer(handle):
            ended = threading.Event()
            execution_events[id(intake._jobs[handle].inputs)] = ended
            offered.append(ended)
            assert pool.offer_next() is handle

        def blocked_popen(*args, **kwargs):
            assert threading.current_thread() is not threading.main_thread()
            scratch.add(Path(kwargs['cwd']))
            if getattr(local, 'project', None) == fixture.projects[0] and not blocked.is_set():
                blocked.set()
                assert release.wait(timeout=5), 'test spawn gate timeout'
            process = real_popen(*args, **kwargs)
            processes.append((process, execution.process_record(process.pid)))
            launches.append(local.project)
            return process

        latencies = []

        def wait_b(handle):
            since = time.monotonic()
            while True:
                before = time.monotonic_ns()
                state = pool.snapshot()
                latencies.append(time.monotonic_ns() - before)
                assert state['active_protocol_bytes'] <= state['static_protocol_capacity_bytes']
                for notice in pool.poll():
                    assert notice.handle is handle
                    assert notice.result.reason == 'signature_execution_completed' and notice.result.indices == (0,)
                    assert notice.result.roots_reaped == 2 and not notice.released
                    pool.discard(handle)
                    return
                assert time.monotonic() - since < 5
                time.sleep(0.002)

        patches.enter_context(mock.patch.object(execution._Context, 'sample', observe))
        patches.enter_context(mock.patch.object(execution, '_execute_signature_job', execute))
        patches.enter_context(mock.patch.object(subprocess, 'Popen', side_effect=blocked_popen))
        patches.enter_context(mock.patch.object(threading.Thread, 'start', side_effect=AssertionError('request thread start')))
        patches.enter_context(mock.patch.object(threading.Thread, 'join', side_effect=AssertionError('request join')))
        offer(handle_a)
        offer(handle_b)
        assert blocked.wait(timeout=5)
        if inject_failure:
            raise AssertionError('synthetic blocked-start failure')
        before_cancel = time.monotonic_ns()
        assert pool.cancel(handle_a) == 'pending_confirmation'
        cancellation_request_ns = time.monotonic_ns() - before_cancel
        pending = pool.snapshot()
        assert pending['active'] == 2 and pending['cancel_requested'] == 1
        assert pending['active_protocol_bytes'] == 1107688
        wait_b(handle_b)
        assert not release.is_set() and pool.snapshot()['active'] == 1
        before_capture = time.monotonic_ns()
        handle_b2 = intake.capture(fixture.projects[1], 'codex', fixture.work_limits)
        second_project_capture_ns = time.monotonic_ns() - before_capture
        offer(handle_b2)
        wait_b(handle_b2)
        assert not release.is_set() and pool.snapshot()['active_protocol_bytes'] == 553844
        assert intake.snapshot().selected == intake.snapshot().cancel_requested == 1
        release.set()
        since = time.monotonic()
        while pool.snapshot()['unconfirmed'] != 1:
            assert pool.poll() == ()
            assert time.monotonic() - since < 5
            time.sleep(0.002)
        assert context_a is not None and context_a.process is not None and context_a.reaped
        assert context_a.process.returncode is not None and not context_a.guarded
        # Drop the fixture's sole strong context reference. A weak reference
        # remaining live now independently checks the parked worker's custody.
        retained = weakref.ref(context_a)
        context_a = None
        gc.collect()
        assert retained() is not None
        slot_a = next(slot for slot in pool._slots if slot.current is not None)
        active = slot_a.current
        # Remove the already-observed marker so it cannot mask the unknown
        # latch. The next candidate is otherwise valid completion metadata.
        slot_a.pending = None
        forged = pools.SignatureWorkerResult('signature_execution_completed', (0,), time.monotonic_ns(), 2, False)
        assert pool._valid_result(forged, active)
        assert active.started_ns <= forged.finished_ns < active.deadline_ns
        slot_a.outgoing.put_nowait(pools._Complete(active.token, forged))
        assert pool.poll() == () and pool.snapshot()['unconfirmed'] == 1
        assert intake.snapshot().selected == 1 and intake.snapshot().reserved_bytes == 65536
        try:
            pool.result(handle_a)
        except pools.SignatureWorkerError:
            pass
        else:
            raise AssertionError('unknown result became available')
        handle_b3 = intake.capture(fixture.projects[1], 'codex', fixture.work_limits)
        offer(handle_b3)
        wait_b(handle_b3)
        assert pool.snapshot()['active'] == pool.snapshot()['unconfirmed'] == 1
        assert slot_a.outgoing.qsize() == 1
        assert len(launches) == 7
        pool.request_close()
        for _ in range(10):
            pool.poll()
            time.sleep(0.002)
        state = pool.snapshot()
        assert not state['closed'] and state['unconfirmed'] == 1 and state['intake_jobs'] == 1
        assert state['active_protocol_bytes'] == 553844
        report = {
            'actual_process_launches': len(launches), 'b_jobs_completed': 3,
            'b_jobs_completed_while_a_spawn_blocked': 2, 'b_jobs_completed_after_a_unconfirmed': 1,
            'owner_cancel_request_ns': cancellation_request_ns, 'maximum_owner_snapshot_ns': max(latencies),
            'second_project_capture_ns': second_project_capture_ns,
            'a_root_actual_returncode': retained().process.returncode,
            'a_guard_never_observed': not retained().guarded,
            'a_actual_handle_inputs_wire_scratch_retained': retained().inputs is not None and retained().wire is not None and retained().scratch.exists(),
            'worker_context_retained_after_fixture_strong_reference_dropped': retained() is not None,
            'a_intake_and_protocol_shares_still_held': state['active_protocol_bytes'] == 553844 and intake.snapshot().reserved_bytes == 65536,
            'late_matching_token_completion_cannot_clear_unknown': slot_a.outgoing.qsize() == 1,
            'close_did_not_claim_unknown_cleanup': not state['closed'],
            'scope': 'Isolated actual-intake/fixed-helper test with test-only spawn gate. No provider admission, normal service/native authority, durable recovery, installed or release proof.',
        }
    finally:
        body_error = sys.exc_info()[1]
        cleanup_errors = []
        release.set()
        settled = False
        roots_reaped = False
        try:
            if pool is not None:
                pool.request_close()
            since = time.monotonic()
            while not all(event.is_set() for event in offered):
                if pool is not None:
                    for notice in pool.poll():
                        if not notice.released:
                            pool.discard(notice.handle)
                assert time.monotonic() - since < 5, 'offered executions did not settle'
                time.sleep(0.002)
            while pool is not None and any(slot.current is not None and not slot.current.unknown
                                           for slot in pool._slots):
                for notice in pool.poll():
                    if not notice.released:
                        pool.discard(notice.handle)
                assert time.monotonic() - since < 5, 'completion publication did not settle'
                time.sleep(0.002)
            settled = True
        except BaseException as error:
            cleanup_errors.append(error)
        if settled:
            for process, birth in processes:
                try:
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        current = execution.process_record(process.pid)
                        assert birth is not None and current is not None
                        assert current[0] == os.getpid() and current[2] == birth[2]
                        process.kill()
                        process.wait(timeout=1)
                    assert process.returncode is not None
                except BaseException as error:
                    cleanup_errors.append(error)
            roots_reaped = all(process.returncode is not None for process, _ in processes)
        if settled and roots_reaped:
            for path in scratch:
                try:
                    if path.exists():
                        shutil.rmtree(path)
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                assert fixture.doCleanups(), 'owned fixture cleanup failed'
            except BaseException as error:
                cleanup_errors.append(error)
        # Keep instrumentation until every offered execution has ended.
        patches.close()
        teardown = {'offered_executions_settled': settled,
                    'actual_roots': len(processes), 'actual_roots_reaped': roots_reaped,
                    'scratch_removed': settled and roots_reaped and all(not path.exists() for path in scratch)}
        if report is not None:
            report['teardown'] = teardown
        if body_error is not None or cleanup_errors:
            print(json.dumps({'fixture_teardown': teardown}), file=sys.stderr)
        if cleanup_errors:
            raise BaseExceptionGroup('fixture body/cleanup failures',
                                     ([body_error] if body_error is not None else []) + cleanup_errors)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
