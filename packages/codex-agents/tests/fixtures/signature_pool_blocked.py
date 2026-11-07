"""Synthetic blocked-spawn fixture, executed only by the pool regression test."""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import threading
from threading import local as thread_local
import time
from unittest import mock

def main():
    PACKAGE_ROOT = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(PACKAGE_ROOT))
    sys.path.insert(0, str(PACKAGE_ROOT / 'native-package/.codex/orchestration'))
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
        launches = []

        def observe(context, *args, **kwargs):
            nonlocal context_a
            local.project = context.inputs.project_id
            if local.project == fixture.projects[0]:
                context_a = context
            return original_sample(context, *args, **kwargs)

        def blocked_popen(*args, **kwargs):
            assert threading.current_thread() is not threading.main_thread()
            if getattr(local, 'project', None) == fixture.projects[0] and not blocked.is_set():
                blocked.set()
                if not release.wait(timeout=5):
                    raise AssertionError('test spawn gate timeout')
            launches.append(local.project)
            return real_popen(*args, **kwargs)

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

        with mock.patch.object(execution._Context, 'sample', observe), \
                mock.patch.object(subprocess, 'Popen', side_effect=blocked_popen), \
                mock.patch.object(threading.Thread, 'start', side_effect=AssertionError('request thread start')), \
                mock.patch.object(threading.Thread, 'join', side_effect=AssertionError('request join')):
            assert pool.offer_next() is handle_a and pool.offer_next() is handle_b
            assert blocked.wait(timeout=5)
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
            assert pool.offer_next() is handle_b2
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
            slot_a = next(slot for slot in pool._slots if slot.current is not None)
            active = slot_a.current
            forged = pools.SignatureWorkerResult('signature_execution_completed', (0,), time.monotonic_ns(), 1, False)
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
            assert pool.offer_next() is handle_b3
            wait_b(handle_b3)
            assert pool.snapshot()['active'] == pool.snapshot()['unconfirmed'] == 1
            assert slot_a.outgoing.qsize() == 1  # late completion was never consumed as authority
        assert len(launches) == 7
        pool.request_close()
        for _ in range(10):
            pool.poll()
            time.sleep(0.002)
        state = pool.snapshot()
        assert not state['closed'] and state['unconfirmed'] == 1 and state['intake_jobs'] == 1
        assert state['active_protocol_bytes'] == 553844
        report = {'actual_process_launches': len(launches), 'b_jobs_completed': 3, 'b_jobs_completed_while_a_spawn_blocked': 2, 'b_jobs_completed_after_a_unconfirmed': 1, 'owner_cancel_request_ns': cancellation_request_ns, 'maximum_owner_snapshot_ns': max(latencies), 'second_project_capture_ns': second_project_capture_ns, 'a_root_actual_returncode': context_a.process.returncode, 'a_guard_never_observed': not context_a.guarded, 'a_actual_handle_inputs_wire_scratch_retained': context_a.inputs is not None and context_a.wire is not None and context_a.scratch.exists(), 'a_intake_and_protocol_shares_still_held': state['active_protocol_bytes'] == 553844 and intake.snapshot().reserved_bytes == 65536, 'late_matching_token_completion_cannot_clear_unknown': slot_a.outgoing.qsize() == 1, 'close_did_not_claim_unknown_cleanup': not state['closed'], 'scope': 'Isolated actual-intake/fixed-helper test with test-only Popen gate. Demonstrates owner/other-project progress and conservative unknown latch; not actual provider admission, normal service/native authority, durable recovery, installed or release proof.'}
    finally:
        release.set()
        if pool is not None:
            pool.request_close()
        if context_a is not None and context_a.process is not None:
            assert context_a.process.returncode is not None
            if context_a.scratch is not None and context_a.scratch.exists():
                # Isolated fixture teardown only after its actual owned root reaped;
                # no intake confirmation or normal recovery/capacity release occurs.
                shutil.rmtree(context_a.scratch)
        if not fixture.doCleanups():
            raise AssertionError('owned fixture cleanup failed')
    print(json.dumps(report))


if __name__ == "__main__":
    main()
