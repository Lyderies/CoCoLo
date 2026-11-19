from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import tests.test_signature_inputs as capture_fixture
from tests.learning_support import attest_provider

from codex_agents.account_api import AccountError
from codex_agents import signature_execution as execution
from codex_agents.signature_job_intake import SignatureIntakeLimits, SignatureJobError, SignatureJobIntake
from codex_agents.signature_worker_pool import (
    SignatureWorkerError, SignatureWorkerLimits, SignatureWorkerPool,
)

SIGNATURE_WORKER = ((sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file())
                    or sys.platform.startswith("linux"))
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def config(*, workers=2, budget=4 * 1_048_576, timeout=30_000_000_000):
    return SignatureWorkerLimits(workers, budget, timeout, 10_000_000, 5_000_000_000)


class SignatureWorkerLimitTests(unittest.TestCase):
    def test_explicit_exact_frozen_limits_with_finite_ceilings(self):
        valid = config()
        with self.assertRaises(FrozenInstanceError):
            valid.max_workers = 1

        class CallbackTrap:
            def fail(self, *args):
                raise AssertionError("caller callback invoked")

            __len__ = __hash__ = __eq__ = __repr__ = fail

        for index, bad in enumerate((True, 0, -1, 1.0, "CANARY", CallbackTrap())):
            for position in range(5):
                values = [2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000]
                values[position] = bad
                with self.subTest(index=index, position=position), self.assertRaises(SignatureWorkerError) as caught:
                    SignatureWorkerLimits(*values)
                self.assertEqual("signature_pool_invalid_limits", caught.exception.reason_code)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        for position, value in enumerate((17, 512 * 1_048_576 + 1, 3_600_000_000_001, 100_000_001, 5_000_000_001)):
            values = [2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000]
            values[position] = value
            with self.assertRaises(SignatureWorkerError):
                SignatureWorkerLimits(*values)
        self.assertEqual("signature_pool_unavailable", str(SignatureWorkerError(CallbackTrap())))


@unittest.skipUnless(SIGNATURE_WORKER, "needs the macOS sandbox-exec policy or Linux")
class SignatureWorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        self.fixture.setUp()
        self.intake = SignatureJobIntake(self.fixture.views, SignatureIntakeLimits(2, 1, 8, 65536, 2))
        self.pool = None
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        if self.pool is not None:
            for handle in tuple(self.intake._jobs):
                self.pool.cancel(handle)
            self.pool.request_close()
            started = time.monotonic()
            while not self.pool.snapshot()["closed"]:
                for notice in self.pool.poll():
                    if not notice.released:
                        self.pool.discard(notice.handle)
                if time.monotonic() - started > 10:
                    self.fail("fixed worker fixture did not drain")
                time.sleep(0.002)
            self.assertFalse(any(slot.thread is not None and slot.thread.is_alive() for slot in self.pool._slots))
        self.assertTrue(self.fixture.doCleanups(), "owned fixture cleanup failed")

    def make_pool(self, limits=None):
        self.pool = SignatureWorkerPool(self.intake, config() if limits is None else limits)
        self.pool.bootstrap()
        return self.pool

    def capture(self, index=0):
        return self.intake.capture(self.fixture.projects[index], "codex", self.fixture.work_limits)

    def notices(self, count):
        received = {}
        started = time.monotonic()
        while len(received) < count:
            state = self.pool.snapshot()
            self.assertLessEqual(state["active_protocol_bytes"], state["static_protocol_capacity_bytes"])
            for notice in self.pool.poll():
                received[notice.handle] = notice
            if time.monotonic() - started > 10:
                self.fail("fixed worker completion timed out")
            time.sleep(0.002)
        return list(received.values())

    def started_on_a_worker(self, pool):
        handle = self.capture(0)
        started = time.monotonic()
        while pool.health.view()['busy'] < 1:  # the owner hands it to a worker
            pool.poll()
            self.assertLess(time.monotonic() - started, 10)
            time.sleep(0.002)
        return handle

    def test_H1a_an_internal_failure_is_noted_with_its_type_and_no_fault(self):
        pool = self.make_pool()
        with mock.patch.object(execution, '_execute_signature_job', side_effect=RuntimeError('a secret')):
            self.started_on_a_worker(pool)
            started = time.monotonic()
            while pool.health.view()['last_failure'] is None:
                self.assertLess(time.monotonic() - started, 10)
                time.sleep(0.002)
        view = pool.health.view()
        self.assertEqual(('signature_worker_failed', 'RuntimeError', False, 0),
                         (view['last_failure']['cause'], view['last_failure']['type'], view['faulted'], view['kept']))
        self.assertNotIn('secret', repr(view))

    def test_H1a_an_unconfirmed_job_is_kept_by_its_worker_while_the_owner_does_not_poll(self):
        pool = self.make_pool()
        with mock.patch.object(execution, '_execute_signature_job',
                               side_effect=execution.SignatureExecutionUnconfirmed(object())):
            self.started_on_a_worker(pool)
            started = time.monotonic()
            while pool.health.view()['kept'] < 1:  # no poll: the worker publishes it
                self.assertLess(time.monotonic() - started, 10)
                time.sleep(0.002)
        self.assertEqual('signature_execution_unconfirmed', pool.health.view()['last_failure']['cause'])
        self.assertFalse(pool.health.view()['faulted'])
        pool.poll()  # the owner acknowledges it: still counted once
        self.assertEqual(1, pool.health.view()['kept'])
        self.pool = None  # its worker keeps custody for good; nothing to drain here

    def test_two_actual_jobs_keep_returned_fields_until_consumed_with_no_owner_external_waits(self):
        attest_provider(self.fixture.ledger, self.fixture.projects[1], self.fixture.key, "codex", "b" * 64)
        pool = self.make_pool()
        handles = [self.capture(0), self.capture(1)]
        owner = threading.current_thread()
        original_popen = subprocess.Popen
        launches = []

        def owned_worker_popen(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread())
            launches.append(threading.current_thread().ident)
            return original_popen(*args, **kwargs)

        with ExitStack() as owner_traps:
            for module, name in ((execution, 'encode_signature_inputs'), (execution, '_public_file'),
                                 (execution.tempfile, 'mkdtemp'), (execution.shutil, 'rmtree'),
                                 (Path, 'unlink')):
                original = getattr(module, name)

                def worker_only(*args, _original=original, **kwargs):
                    self.assertIsNot(owner, threading.current_thread())
                    return _original(*args, **kwargs)

                owner_traps.enter_context(mock.patch.object(module, name, autospec=True, side_effect=worker_only))
            self.run_two_jobs_with_owner_traps(pool, handles, owner, owned_worker_popen)
        self.assertEqual(4, len(launches))
        self.assertEqual(2, len(set(launches)))
        self.assertEqual(0, self.intake.snapshot().jobs)

    def run_two_jobs_with_owner_traps(self, pool, handles, owner, owned_worker_popen):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("extra database")), \
                mock.patch.object(self.fixture.state, "digest_key", side_effect=AssertionError("private state key")), \
                mock.patch.object(threading.Thread, "start", side_effect=AssertionError("request thread start")), \
                mock.patch.object(threading.Thread, "join", side_effect=AssertionError("request join")), \
                mock.patch.object(subprocess, "Popen", side_effect=owned_worker_popen):
            self.assertEqual(set(handles), {pool.offer_next(), pool.offer_next()})
            self.assertIsNone(pool.offer_next())
            notices = self.notices(2)
            for notice in notices:
                self.assertFalse(notice.released)
                self.assertEqual(("signature_execution_completed", (0,), 2),
                                 (notice.result.reason, notice.result.indices, notice.result.roots_reaped))
            self.assertEqual(2, pool.snapshot()["returned"])
            self.assertEqual(2 * 65536, self.intake.snapshot().reserved_bytes)
            for handle in handles:
                self.assertEqual((0,), pool.result(handle).indices)
                self.assertFalse(hasattr(pool.result(handle), "origin"))
                self.assertFalse(hasattr(pool.result(handle), "admission"))
                pool.discard(handle)

    def test_cancel_after_actual_ready_keeps_share_until_root_reaped(self):
        pool = self.make_pool()
        ready, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = execution._Context.sample

        def gate(context, *args, **kwargs):
            if context.guarded and context.roots_reaped == 0 and not ready.is_set():
                ready.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test gate timeout")
            return original(context, *args, **kwargs)

        with mock.patch.object(execution._Context, "sample", gate):
            handle = self.capture()
            self.assertIs(handle, pool.offer_next())
            self.assertTrue(ready.wait(timeout=5))
            self.assertEqual("pending_confirmation", pool.cancel(handle))
            state = pool.snapshot()
            self.assertEqual((1, 1, 553844), (state["active"], state["cancel_requested"], state["active_protocol_bytes"]))
            self.assertEqual(65536, self.intake.snapshot().reserved_bytes)
            release.set()
            notice, = self.notices(1)
        self.assertFalse(notice.released)
        self.assertEqual(("signature_execution_cancelled", (), 1),
                         (notice.result.reason, notice.result.indices, notice.result.roots_reaped))
        self.assertEqual(65536, self.intake.snapshot().reserved_bytes)
        self.assertEqual('signature_execution_cancelled', pool.result(handle).reason)
        pool.discard(handle)
        self.assertEqual(0, self.intake.snapshot().jobs)
        with self.assertRaises(SignatureWorkerError):
            pool.result(handle)

    def test_expired_zero_process_result_keeps_its_metadata_under_intake_quota(self):
        pool = self.make_pool(config(timeout=1))
        handle = self.capture()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("expired job launched")):
            self.assertIs(handle, pool.offer_next())
            notice, = self.notices(1)
        self.assertEqual("signature_execution_expired", notice.result.reason)
        self.assertTrue(notice.result.no_process_started)
        self.assertEqual(0, notice.result.roots_reaped)
        self.assertFalse(notice.released)
        self.assertEqual(1, pool.snapshot()["returned"])
        self.assertEqual(65536, self.intake.snapshot().reserved_bytes)
        with self.assertRaises(SignatureJobError):
            self.capture()
        pool.discard(handle)
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_actual_positive_output_cannot_pass_after_owner_deadline(self):
        pool = self.make_pool()
        handle = self.capture()
        self.assertIs(handle, pool.offer_next())
        slot = next(slot for slot in pool._slots if slot.current is not None)
        deadline = slot.current.deadline_ns
        started = time.monotonic()
        while slot.outgoing.empty():
            self.assertLess(time.monotonic() - started, 10)
            time.sleep(0.002)
        with mock.patch.object(pool, "_now", return_value=deadline + 1):
            notice, = pool.poll()
        self.assertEqual(("signature_execution_expired", ()), (notice.result.reason, notice.result.indices))
        self.assertEqual(2, notice.result.roots_reaped)
        self.assertEqual(65536, self.intake.snapshot().reserved_bytes)
        pool.discard(handle)

    def test_mid_poll_clock_failure_keeps_every_completion_visible_until_discard(self):
        attest_provider(self.fixture.ledger, self.fixture.projects[1], self.fixture.key, 'codex', 'b' * 64)
        pool = self.make_pool()
        handles = [self.capture(0), self.capture(1)]
        self.assertEqual(set(handles), {pool.offer_next(), pool.offer_next()})
        started = time.monotonic()
        while not all(not slot.outgoing.empty() for slot in pool._slots):
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(0.002)
        first = time.monotonic_ns()
        failure = MemoryError('synthetic mid-poll clock failure')
        values = [first, first, failure]

        def clock():
            value = values.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        with mock.patch.object(time, 'monotonic_ns', side_effect=clock):
            with self.assertRaises(MemoryError) as caught:
                pool.poll()
        self.assertIs(failure, caught.exception)
        self.assertEqual(2 * 65536, self.intake.snapshot().reserved_bytes)
        self.assertEqual('signature_execution_clock_failure', pool.result(handles[0]).reason)
        notices = pool.poll()
        self.assertEqual(set(handles), {notice.handle for notice in notices})
        for notice in notices:
            self.assertFalse(notice.released)
            self.assertEqual(('signature_execution_clock_failure', (), 2),
                             (notice.result.reason, notice.result.indices, notice.result.roots_reaped))
        self.assertEqual(set(handles), {notice.handle for notice in pool.poll()})
        for handle in handles:
            pool.discard(handle)
        self.assertEqual((), pool.poll())
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_copy_budget_precedes_start_and_limits_are_revalidated_and_copied(self):
        with self.assertRaises(SignatureWorkerError) as caught:
            SignatureWorkerPool(self.intake, config(budget=1_107_687))
        self.assertEqual("signature_pool_invalid_limits", caught.exception.reason_code)
        self.assertIsNone(getattr(self.intake, "_signature_worker_pool", None))
        supplied = config()
        pool = self.make_pool(supplied)
        object.__setattr__(supplied, "timeout_ns", 1)
        object.__setattr__(supplied, "max_workers", True)
        self.assertEqual(1_107_688, pool.snapshot()["static_protocol_capacity_bytes"])
        handle = self.capture()
        self.assertIs(handle, pool.offer_next())
        notice, = self.notices(1)
        self.assertEqual("signature_execution_completed", notice.result.reason)
        pool.discard(handle)
        with self.assertRaises(SignatureWorkerError):
            SignatureWorkerPool(self.intake, supplied)

    def test_duplicate_owner_and_callback_handles_cannot_create_or_access_a_pool(self):
        pool = self.make_pool()
        with self.assertRaises(SignatureWorkerError):
            SignatureWorkerPool(self.intake, config())
        observed = queue.Queue(maxsize=1)

        def wrong_owner():
            try:
                pool.snapshot()
            except AccountError as error:
                observed.put_nowait(error.reason_code)

        thread = threading.Thread(target=wrong_owner)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual("account_wrong_owner", observed.get_nowait())

        class CallbackTrap:
            def fail(self, *args):
                raise AssertionError("caller callback invoked")
            __len__ = __hash__ = __eq__ = __repr__ = fail

        for method in (pool.cancel, pool.result, pool.discard):
            with self.assertRaises(SignatureWorkerError):
                method(CallbackTrap())


@unittest.skipUnless(SIGNATURE_WORKER, "needs the macOS sandbox-exec policy or Linux")
class SignatureWorkerBlockedStartTests(unittest.TestCase):
    def test_blocked_fixture_failure_waits_for_all_actual_roots_before_cleanup(self):
        completed = subprocess.run(
            [sys.executable, '-I', '-B', '-S', str(PACKAGE_ROOT / 'tests/fixtures/signature_pool_blocked.py'),
             str(PACKAGE_ROOT), 'fail-after-offer'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
            env={'PATH': os.defpath, 'LC_ALL': 'C'}, close_fds=True,
        )
        self.assertNotEqual(0, completed.returncode)
        error = completed.stderr.decode('utf-8', 'replace')
        self.assertIn('synthetic blocked-start failure', error)
        reports = [json.loads(line)['fixture_teardown'] for line in error.splitlines()
                   if line.startswith('{"fixture_teardown":')]
        self.assertEqual(1, len(reports), error)
        teardown, = reports
        self.assertGreaterEqual(teardown['actual_roots'], 1)
        for field in ('offered_executions_settled', 'actual_roots_reaped', 'scratch_removed'):
            self.assertTrue(teardown[field], error)

    def test_exceptional_cleanup_clock_metadata_and_publication_hold_capacity(self):
        for scenario in ('cleanup-exception', 'clock-exception', 'clock-regression',
                         'malformed-roots', 'malformed-early', 'malformed-future',
                         'offer-publication', 'result-publication', 'intake-publication',
                         'cancelled-intake-publication', 'spawn-exception'):
            with self.subTest(scenario=scenario):
                completed = subprocess.run(
                    [sys.executable, '-I', '-B', '-S', str(PACKAGE_ROOT / 'tests/fixtures/signature_pool_faults.py'),
                     str(PACKAGE_ROOT), scenario],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
                    env={'PATH': os.defpath, 'LC_ALL': 'C'}, close_fds=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode('utf-8', 'replace'))
                report = json.loads(completed.stdout)
                self.assertEqual(scenario, report['scenario'])
                for field in ('capacity_held', 'actual_roots_reaped_before_fixture_cleanup',
                              'scratch_removed_after_execution_settled', 'unknown_close_stays_false'):
                    self.assertTrue(report[field], field)
                self.assertEqual(not scenario.startswith('clock-'), report['unknown'])
                if scenario in {'cleanup-exception', 'clock-exception', 'clock-regression',
                                'malformed-roots', 'malformed-early', 'malformed-future',
                                'result-publication', 'intake-publication', 'cancelled-intake-publication'}:
                    self.assertEqual(2, report['actual_process_launches'])
                if scenario == 'spawn-exception':
                    self.assertEqual(0, report['actual_process_launches'])

    def test_other_project_progress_and_unknown_latch_with_actual_owned_children(self):
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", str(PACKAGE_ROOT / "tests/fixtures/signature_pool_blocked.py"),
             str(PACKAGE_ROOT)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
            env={"PATH": os.defpath, "LC_ALL": "C"}, close_fds=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        report = json.loads(completed.stdout)
        self.assertEqual((7, 2, 1), (report["actual_process_launches"],
                                   report["b_jobs_completed_while_a_spawn_blocked"],
                                   report["b_jobs_completed_after_a_unconfirmed"]))
        for field in ("owner_cancel_request_ns", "maximum_owner_snapshot_ns", "second_project_capture_ns"):
            self.assertLess(report[field], 2_000_000_000)
        for field in ("a_guard_never_observed", "a_actual_handle_inputs_wire_scratch_retained",
                      "a_intake_and_protocol_shares_still_held", "late_matching_token_completion_cannot_clear_unknown",
                      "close_did_not_claim_unknown_cleanup", "worker_context_retained_after_fixture_strong_reference_dropped"):
            self.assertTrue(report[field])
        self.assertEqual(7, report['teardown']['actual_roots'])
        for field in ('offered_executions_settled', 'actual_roots_reaped', 'scratch_removed'):
            self.assertTrue(report['teardown'][field])
