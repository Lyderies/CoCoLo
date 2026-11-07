"""Isolated synthetic pool fault cases; never a service or recovery path."""

from dataclasses import replace
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from unittest import mock


def main():
    package = Path(sys.argv[1]).resolve()
    scenario = sys.argv[2]
    sys.path[:0] = [str(package), str(package / 'native-package/.codex/orchestration')]
    from codex_agents import signature_execution as execution
    from codex_agents import signature_worker_pool as pools
    from codex_agents.signature_job_intake import SignatureIntakeLimits, SignatureJobIntake
    import tests.test_signature_inputs as capture_fixture

    fixture = capture_fixture.SignatureInputOwnerTests('test_capture_matches_legacy_inputs_without_crypto_connections_or_writes')
    fixture.setUp()
    pool = None
    offered = False
    report = {}
    unknown = False
    settled = roots_reaped = False
    finished, release, cleanup_ready = threading.Event(), threading.Event(), threading.Event()
    launches, scratch = [], []
    original_execute = execution._execute_signature_job
    original_popen, original_mkdtemp = subprocess.Popen, execution.tempfile.mkdtemp
    original_rmtree = shutil.rmtree

    def execute(*args, **kwargs):
        try:
            return original_execute(*args, **kwargs)
        finally:
            finished.set()

    def popen(*args, **kwargs):
        if scenario == 'spawn-exception':
            raise OSError('synthetic ambiguous Popen failure')
        process = original_popen(*args, **kwargs)
        launches.append((process, execution.process_record(process.pid)))
        return process

    def mkdtemp(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        scratch.append(Path(path))
        return path

    def rmtree(path, *args, **kwargs):
        if scenario == 'cleanup-exception' and Path(path) in scratch:
            cleanup_ready.set()
            assert release.wait(timeout=5), 'cleanup gate timeout'
            raise MemoryError('synthetic final cleanup failure')
        return original_rmtree(path, *args, **kwargs)

    def wait_for(condition):
        start = time.monotonic()
        while not condition():
            assert time.monotonic() - start < 5, 'fixture wait timed out'
            time.sleep(0.002)

    try:
        intake = SignatureJobIntake(fixture.views, SignatureIntakeLimits(2, 1, 8, 65536, 2))
        pool = pools.SignatureWorkerPool(intake, pools.SignatureWorkerLimits(2, 4 * 1048576, 30_000_000_000, 10_000_000, 5_000_000_000))
        pool.bootstrap()
        handle = intake.capture(fixture.projects[0], 'codex', fixture.work_limits)
        with mock.patch.object(execution, '_execute_signature_job', execute), \
                mock.patch.object(subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(execution.tempfile, 'mkdtemp', side_effect=mkdtemp), \
                mock.patch.object(shutil, 'rmtree', side_effect=rmtree):
            # Keep launch instrumentation active through failure teardown.
            try:
                slot = pool._slots[0]
                if scenario == 'offer-publication':
                    original_put = slot.incoming.put_nowait

                    def ambiguous_put(value):
                        original_put(value)
                        raise MemoryError('synthetic post-enqueue fault')

                    offered = True
                    with mock.patch.object(slot.incoming, 'put_nowait', side_effect=ambiguous_put):
                        try:
                            pool.offer_next()
                        except MemoryError:
                            pass
                        else:
                            raise AssertionError('offer fault was swallowed')
                    assert slot.current.unknown and pool.snapshot()['faulted']
                else:
                    offered = True
                    assert pool.offer_next() is handle
                if scenario == 'cleanup-exception':
                    assert cleanup_ready.wait(timeout=5)
                    assert pool.cancel(handle) == 'pending_confirmation'
                    release.set()
                wait_for(lambda: finished.is_set() and not slot.outgoing.empty())
                if scenario in {'clock-exception', 'clock-regression'}:
                    real_clock = time.monotonic_ns
                    first = real_clock()
                    failure = MemoryError('synthetic owner clock failure')
                    values = [first, failure if scenario == 'clock-exception' else first - 1]

                    def clock():
                        value = values.pop(0)
                        if isinstance(value, BaseException):
                            raise value
                        return value

                    with mock.patch.object(pools.time, 'monotonic_ns', side_effect=clock):
                        try:
                            pool.poll()
                        except (MemoryError, pools.SignatureWorkerError) as error:
                            if scenario == 'clock-exception':
                                assert error is failure
                        else:
                            raise AssertionError('clock failure was swallowed')
                    assert slot.pending is not None and slot.outgoing.empty()
                    assert intake.snapshot().reserved_bytes == 65536
                    notice, = pool.poll()
                    assert notice.result.reason == 'signature_execution_clock_failure'
                    assert notice.result.indices == () and not notice.released
                    pool.discard(handle)
                    unknown = False
                else:
                    if scenario in {'malformed-roots', 'malformed-early', 'malformed-future'}:
                        actual = slot.outgoing.get_nowait()
                        assert actual.result.reason == 'signature_execution_completed'
                        assert actual.result.roots_reaped == 2 and actual.result.indices == (0,)
                        changes = {'malformed-roots': {'roots_reaped': 1},
                                   'malformed-early': {'finished_ns': 0},
                                   'malformed-future': {'finished_ns': (1 << 63) - 1}}
                        slot.outgoing.put_nowait(pools._Complete(actual.token, replace(actual.result, **changes[scenario])))
                    if scenario == 'result-publication':
                        class RejectPublication(dict):
                            def __setitem__(self, key, value):
                                raise MemoryError('synthetic result publication failure')
                        pool._returned = RejectPublication()
                    calls = []
                    original_confirm = intake.confirm_worker_stopped

                    def confirm(value, **kwargs):
                        calls.append(value)
                        original_confirm(value, **kwargs)
                        raise MemoryError('synthetic post-confirmation failure')

                    if scenario in {'intake-publication', 'cancelled-intake-publication'}:
                        if scenario == 'cancelled-intake-publication':
                            assert pool.cancel(handle) == 'pending_confirmation'
                        with mock.patch.object(intake, 'confirm_worker_stopped', side_effect=confirm):
                            try:
                                pool.poll()
                            except MemoryError:
                                pass
                            else:
                                raise AssertionError('confirmation fault was swallowed')
                            assert pool.poll() == () and calls == [handle]
                    elif scenario == 'result-publication':
                        try:
                            pool.poll()
                        except MemoryError:
                            pass
                        else:
                            raise AssertionError('publication fault was swallowed')
                    else:
                        assert pool.poll() == ()
                    assert pool.snapshot()['unconfirmed'] == 1
                    assert pool.snapshot()['active_protocol_bytes'] == 553844
                    assert intake.snapshot().reserved_bytes == 65536
                    assert pool.cancel(handle) == 'pending_confirmation'
                    assert intake.snapshot().reserved_bytes == 65536
                    assert pool.poll() == ()
                    try:
                        pool.result(handle)
                    except pools.SignatureWorkerError:
                        pass
                    else:
                        raise AssertionError('unknown result became available')
                    unknown = True
                report = {'scenario': scenario, 'unknown': unknown,
                          'capacity_held': intake.snapshot().reserved_bytes == (65536 if unknown else 0),
                          'actual_process_launches': len(launches)}
            finally:
                body_error = sys.exc_info()[1]
                cleanup_errors = []
                release.set()
                try:
                    pool.request_close()
                    if offered:
                        wait_for(finished.is_set)
                    settled = True
                except BaseException as error:
                    cleanup_errors.append(error)
                # All offered executions have ended, including unknown ones:
                # no later helper launch can race this test-only cleanup.
                if settled:
                    for process, birth in launches:
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
                    roots_reaped = all(p.returncode is not None for p, _ in launches)
                if settled and roots_reaped:
                    for path in scratch:
                        try:
                            if path.exists():
                                original_rmtree(path)
                        except BaseException as error:
                            cleanup_errors.append(error)
                report['actual_roots_reaped_before_fixture_cleanup'] = settled and roots_reaped
                report['scratch_removed_after_execution_settled'] = settled and roots_reaped and all(not p.exists() for p in scratch)
                try:
                    # Never confirm or free unknown intake during teardown.
                    pool.poll()
                    report['unknown_close_stays_false'] = not pool.snapshot()['closed'] if unknown else True
                except BaseException as error:
                    cleanup_errors.append(error)
                if body_error is not None or cleanup_errors:
                    print(json.dumps({'fixture_teardown': report}), file=sys.stderr)
                if cleanup_errors:
                    raise BaseExceptionGroup('fixture body/cleanup failures',
                                             ([body_error] if body_error is not None else []) + cleanup_errors)
    finally:
        release.set()
        if not offered or (settled and roots_reaped):
            assert fixture.doCleanups(), 'owned fixture cleanup failed'
    print(json.dumps(report))


if __name__ == '__main__':
    main()
