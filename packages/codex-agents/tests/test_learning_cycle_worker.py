from __future__ import annotations

from dataclasses import FrozenInstanceError
from collections import deque
import queue
import threading
import time
import unittest
from unittest import mock

from tests.test_learning_cycle_execution import BINDING, MACOS, request
from tests.learning_support import verification_outcome
from codex_agents import learning_cycle_execution as execution
from codex_agents import learning_cycle_worker as workers


def limits(*, count=1, jobs=4, input_bytes=1_048_576):
    return workers.LearningWorkerLimits(count, jobs, input_bytes,
        count * workers.WIRE_BYTES_PER_WORKER, 10_000_000, 1_000_000_000)


class LearningWorkerTests(unittest.TestCase):
    def setUp(self):
        self.pools = []
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for pool in self.pools:
            pool.request_close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for pool in self.pools:
                pool.poll()
                for handle in tuple(pool._jobs):
                    if pool.state(handle) == 'returned':
                        pool.take(handle)
            if all(slot.thread is None or not slot.thread.is_alive() for p in self.pools for slot in p._slots):
                break
            time.sleep(0.005)
        self.assertTrue(all(slot.thread is None or not slot.thread.is_alive() for p in self.pools for slot in p._slots))

    def make_pool(self, configuration=None):
        with mock.patch.object(workers.sys, 'platform', 'darwin'):
            pool = workers.LearningWorkerPool(limits() if configuration is None else configuration)
        self.pools.append(pool)
        pool.bootstrap()
        return pool

    def submit(self, pool, raw=None, *, seconds=10):
        return pool.submit(request() if raw is None else raw, operation='select_mining',
                           binding_digest=BINDING, deadline_ns=time.monotonic_ns() + int(seconds * 1e9))

    def await_state(self, pool, handle, expected):
        deadline = time.monotonic() + 5
        while pool.state(handle) != expected:
            self.assertLess(time.monotonic(), deadline)
            pool.poll()
            time.sleep(0.002)

    @staticmethod
    def complete(raw, **options):
        return execution.LearningExecutionResult('learning_execution_completed',
            {'mining_references': []}, time.monotonic_ns(), 1, False)

    def test_limits_quotas_and_callback_values_refuse_before_startup(self):
        valid = limits()
        with self.assertRaises(FrozenInstanceError):
            valid.max_workers = 3
        class Trap:
            def fail(self, *args):
                raise AssertionError('callback invoked')
            __eq__ = __hash__ = __repr__ = __len__ = fail
        values = [1, 4, 1_048_576, workers.WIRE_BYTES_PER_WORKER, 10_000_000, 1_000_000_000]
        for index in range(len(values)):
            for value in (True, 0, -1, 1.0, 'CANARY', Trap()):
                bad = list(values)
                bad[index] = value
                with self.subTest(index=index), self.assertRaises(workers.LearningWorkerError):
                    workers.LearningWorkerLimits(*bad)
        for index, value in enumerate((5, 33, 256 * 1_048_576 + 1, 8_192 * 1_048_576 + 1,
                                       100_000_001, 5_000_000_001)):
            bad = list(values)
            bad[index] = value
            with self.assertRaises(workers.LearningWorkerError):
                workers.LearningWorkerLimits(*bad)
        with mock.patch.object(workers.threading, 'Thread') as start:
            with self.assertRaises(workers.LearningWorkerError):
                workers.LearningWorkerPool(object())
            with mock.patch.object(workers.sys, 'platform', 'darwin'), self.assertRaises(workers.LearningWorkerError):
                workers.LearningWorkerPool(workers.LearningWorkerLimits(2, 4, 1_048_576,
                    workers.WIRE_BYTES_PER_WORKER, 10_000_000, 1_000_000_000))
            start.assert_not_called()

    def test_mutated_limits_invalid_inputs_and_foreign_mailbox_do_not_release(self):
        corrupted = limits()
        object.__setattr__(corrupted, 'max_workers', 999)
        with mock.patch.object(workers.sys, 'platform', 'darwin'), self.assertRaises(workers.LearningWorkerError):
            workers.LearningWorkerPool(corrupted)
        pool = self.make_pool()
        raw = request()
        options = dict(operation='select_mining', binding_digest=BINDING, deadline_ns=time.monotonic_ns() + 10**10)
        for changed in ({'raw': bytearray(raw)}, {'raw': b''}, {'operation': True},
                        {'binding_digest': 'D' * 64}, {'deadline_ns': True}, {'deadline_ns': 0}):
            with self.assertRaises(workers.LearningWorkerError):
                pool.submit(**{'raw': raw, **options, **changed})
        self.assertEqual(0, pool.snapshot()['jobs'])
        started, gate = threading.Event(), threading.Event()
        self.gates.append(gate)
        def stalled(*args, **kwargs):
            started.set()
            self.assertTrue(gate.wait(5))
            return self.complete(*args, **kwargs)
        with mock.patch.object(execution, '_execute_learning_job', side_effect=stalled):
            handle = self.submit(pool)
            pool.poll()
            self.assertTrue(started.wait(5))
            pool._slots[0].outgoing.put_nowait(workers._Complete(object(), self.complete(raw)))
            pool.poll()
            self.assertEqual('unknown', pool.state(handle))
            self.assertIsNone(pool.take(handle))
            self.assertEqual((1, len(raw)), (pool.snapshot()['jobs'], pool.snapshot()['input_bytes']))
            gate.set()

    def test_fixed_threads_fifo_and_returned_data_hold_capacity_until_taken(self):
        calls = []
        def run(raw, **options):
            calls.append(raw)
            return self.complete(raw, **options)
        with mock.patch.object(execution, '_execute_learning_job', side_effect=run):
            pool = self.make_pool(limits(jobs=2))
            first, second = self.submit(pool), self.submit(pool, request(outcomes=[verification_outcome()]))
            with self.assertRaisesRegex(workers.LearningWorkerError, 'learning_pool_overloaded'):
                self.submit(pool)
            self.await_state(pool, first, 'returned')
            self.assertEqual('queued', pool.state(second))
            self.assertEqual(1, len(calls))
            before = pool.snapshot()
            self.assertEqual(2, before['jobs'])
            self.assertEqual(len(request()) + len(request(outcomes=[verification_outcome()])), before['input_bytes'])
            self.assertEqual('learning_execution_completed', pool.take(first).reason)
            with self.assertRaises(workers.LearningWorkerError):
                pool.take(first)
            self.await_state(pool, second, 'returned')
            self.assertEqual(2, len(calls))
            self.assertEqual('learning_execution_completed', pool.take(second).reason)
            self.assertEqual((0, 0), (pool.snapshot()['jobs'], pool.snapshot()['input_bytes']))

    def test_stalled_calculation_does_not_block_owner_or_another_pool(self):
        started, gate = threading.Event(), threading.Event()
        self.gates.append(gate)
        def run(raw, **options):
            if len(raw) == len(request()):
                started.set()
                self.assertTrue(gate.wait(5))
            return self.complete(raw, **options)
        with mock.patch.object(execution, '_execute_learning_job', side_effect=run):
            stalled, other = self.make_pool(), self.make_pool()
            first = self.submit(stalled)
            stalled.poll()
            self.assertTrue(started.wait(5))
            begin = time.monotonic_ns()
            with mock.patch.object(workers.threading, 'Thread', side_effect=AssertionError('owner started thread')):
                second = self.submit(other, request(outcomes=[verification_outcome()]))
                self.assertEqual('pending_confirmation', stalled.cancel(first))
                for _ in range(20):
                    stalled.poll()
                    stalled.snapshot()
                self.await_state(other, second, 'returned')
                self.assertEqual('learning_execution_completed', other.take(second).reason)
            self.assertLess(time.monotonic_ns() - begin, 2_000_000_000)
            self.assertEqual(('running', 1), (stalled.state(first), stalled.snapshot()['jobs']))
            gate.set()
            self.await_state(stalled, first, 'returned')
            self.assertEqual('learning_execution_cancelled', stalled.take(first).reason)

    def test_queued_cancel_expiry_and_input_budget_do_not_launch(self):
        raw = request()
        with mock.patch.object(execution, '_execute_learning_job') as run:
            pool = self.make_pool(limits(input_bytes=2 * len(raw)))
            first, second = self.submit(pool, seconds=-1), self.submit(pool)
            with self.assertRaisesRegex(workers.LearningWorkerError, 'learning_pool_overloaded'):
                self.submit(pool)
            self.assertEqual('cancelled', pool.cancel(second))
            pool.poll()
            for handle, suffix in ((first, 'expired'), (second, 'cancelled')):
                result = pool.take(handle)
                self.assertEqual('learning_execution_' + suffix, result.reason)
                self.assertEqual((None, 0, True), (result.result, result.roots_reaped, result.no_process_started))
            run.assert_not_called()

    def test_unknown_context_and_unexpected_fault_keep_capacity_without_replacement(self):
        for error in (execution.LearningExecutionUnconfirmed(object()), RuntimeError('SYNTHETIC-SECRET')):
            with self.subTest(error_type=type(error).__name__), \
                    mock.patch.object(execution, '_execute_learning_job', side_effect=error):
                pool = self.make_pool(limits(jobs=1))
                handle = self.submit(pool)
                self.await_state(pool, handle, 'unknown')
                self.assertIsNone(pool.take(handle))
                self.assertEqual('pending_confirmation', pool.cancel(handle))
                with self.assertRaisesRegex(workers.LearningWorkerError, 'learning_pool_overloaded'):
                    self.submit(pool)
                pool.request_close()
                pool.poll()
                self.assertEqual((1, 1, False),
                    (pool.snapshot()['jobs'], pool.snapshot()['unknown'], pool.snapshot()['closed']))
                self.assertIsNotNone(pool._slots[0].retained)
                self.assertNotIn('SYNTHETIC', repr(pool.snapshot()))

    def test_late_cancel_deadline_clock_and_bad_completion_never_publish_success(self):
        for mode in ('cancel', 'expiry', 'clock', 'bad'):
            with self.subTest(mode=mode), mock.patch.object(execution, '_execute_learning_job',
                    side_effect=self.complete if mode != 'bad' else lambda *a, **k: object()):
                pool = self.make_pool()
                handle = self.submit(pool)
                self.await_state(pool, handle, 'unknown' if mode == 'bad' else 'returned')
                if mode == 'bad':
                    self.assertIsNone(pool.take(handle))
                    continue
                if mode == 'cancel':
                    pool.cancel(handle)
                    result = pool.take(handle)
                else:
                    if mode == 'clock':
                        with mock.patch.object(workers.time, 'monotonic_ns', side_effect=OSError('CANARY')):
                            with self.assertRaises(workers.LearningWorkerError):
                                pool.take(handle)
                        self.assertEqual(1, pool.snapshot()['jobs'])
                        result = pool.take(handle)
                    else:
                        with mock.patch.object(workers.time, 'monotonic_ns', return_value=pool._jobs[handle].deadline_ns):
                            result = pool.take(handle)
                expected = {'cancel': 'cancelled', 'expiry': 'expired', 'clock': 'clock_failure'}[mode]
                self.assertEqual('learning_execution_' + expected, result.reason)
                self.assertIsNone(result.result)

    def test_foreign_thread_pid_and_forged_handles_cannot_touch_retained_jobs(self):
        pool = self.make_pool()
        handle = self.submit(pool)
        with self.assertRaises(workers.LearningWorkerError):
            pool.cancel(object())
        failures = queue.Queue()
        def foreign():
            try:
                pool.cancel(handle)
            except workers.LearningWorkerError:
                failures.put(True)
        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures.get_nowait())
        with mock.patch.object(workers.os, 'getpid', return_value=pool._pid + 1), \
                self.assertRaises(workers.LearningWorkerError):
            pool.snapshot()
        self.assertEqual(1, pool.snapshot()['jobs'])

    def test_clock_fault_during_mailbox_poll_keeps_exact_completion_until_drain(self):
        with mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete):
            pool = self.make_pool()
            handle = self.submit(pool)
            pool.poll()
            slot = pool._slots[0]
            deadline = time.monotonic() + 5
            while slot.outgoing.empty():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.002)
            with mock.patch.object(workers.time, 'monotonic_ns', side_effect=OSError('SYNTHETIC-PRIVATE')):
                with self.assertRaises(workers.LearningWorkerError):
                    pool.poll()
            self.assertIsNotNone(slot.pending)
            self.assertEqual(('running', 1), (pool.state(handle), pool.snapshot()['jobs']))
            pool.poll()
            result = pool.take(handle)
            self.assertEqual(('learning_execution_clock_failure', None, 1),
                             (result.reason, result.result, result.roots_reaped))

    def test_worker_mailbox_fault_is_retained_without_thread_traceback_or_new_offer(self):
        pool = self.make_pool()
        slot = pool._slots[0]
        escaped = threading.Event()
        with mock.patch.object(slot.incoming, 'get', side_effect=RuntimeError('SYNTHETIC-PRIVATE')), \
                mock.patch.object(threading, 'excepthook', side_effect=lambda *args: escaped.set()):
            deadline = time.monotonic() + 3
            while slot.thread.is_alive():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.002)
        self.assertTrue(slot.fault.is_set())
        self.assertIsNotNone(slot.retained)
        self.assertFalse(escaped.is_set())
        with self.assertRaises(workers.LearningWorkerError):
            self.submit(pool)
        self.assertEqual(0, pool.snapshot()['jobs'])

    def test_failed_bootstrap_can_close_without_join_or_partial_worker_replacement(self):
        with mock.patch.object(workers.sys, 'platform', 'darwin'):
            pool = workers.LearningWorkerPool(limits(count=2))
        self.pools.append(pool)
        with mock.patch.object(workers.threading.Thread, 'start', side_effect=RuntimeError('SYNTHETIC-PRIVATE')):
            with self.assertRaisesRegex(workers.LearningWorkerError, '^learning_pool_unavailable$'):
                pool.bootstrap()
        with mock.patch.object(workers.threading.Thread, 'join', side_effect=AssertionError('owner joined worker')):
            pool.request_close()
            pool.poll()
        self.assertTrue(pool.snapshot()['closed'])
        self.assertTrue(pool.snapshot()['faulted'])
        with self.assertRaises(workers.LearningWorkerError):
            pool.bootstrap()

    def test_interrupted_intake_and_consumption_hold_capacity_and_never_restart(self):
        for stage in ('intake', 'take'):
            with self.subTest(stage=stage), mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete) as run:
                pool = self.make_pool()
                if stage == 'intake':
                    class InterruptedQueue(deque):
                        def append(self, value):
                            super().append(value)
                            raise RuntimeError('SYNTHETIC-PRIVATE')
                    pool._queued = InterruptedQueue()
                    with self.assertRaisesRegex(workers.LearningWorkerError, '^learning_pool_unavailable$'):
                        self.submit(pool)
                    pool.poll()
                    run.assert_not_called()
                else:
                    handle = self.submit(pool)
                    self.await_state(pool, handle, 'returned')
                    class InterruptedDelete(dict):
                        def __delitem__(self, key):
                            super().__delitem__(key)
                            raise RuntimeError('SYNTHETIC-PRIVATE')
                    pool._jobs = InterruptedDelete(pool._jobs)
                    with self.assertRaisesRegex(workers.LearningWorkerError, '^learning_pool_unavailable$'):
                        pool.take(handle)
                snapshot = pool.snapshot()
                self.assertEqual((True, 1, 1, pool._limits.input_bytes),
                    (snapshot['faulted'], snapshot['jobs'], snapshot['unknown'], snapshot['input_bytes']))
                with self.assertRaises(workers.LearningWorkerError):
                    self.submit(pool)
                pool.request_close()
                pool.poll()
                self.assertFalse(pool.snapshot()['closed'])

    def test_interrupted_worker_offer_never_infers_no_start_or_releases_charge(self):
        with mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete):
            pool = self.make_pool(limits(jobs=1))
            handle = self.submit(pool)
            slot = pool._slots[0]
            original = slot.incoming.put_nowait
            def interrupted(item):
                original(item)
                raise RuntimeError('SYNTHETIC-PRIVATE')
            with mock.patch.object(slot.incoming, 'put_nowait', side_effect=interrupted):
                with self.assertRaisesRegex(workers.LearningWorkerError, '^learning_pool_unavailable$'):
                    pool.poll()
            self.assertEqual('unknown', pool.state(handle))
            self.assertIsNone(pool.take(handle))
            self.assertEqual('pending_confirmation', pool.cancel(handle))
            self.assertEqual(1, pool.snapshot()['jobs'])

    def test_multiple_interrupted_consumptions_preserve_every_actual_orphan(self):
        with mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete):
            pool = self.make_pool(limits(count=2))
            handles = [self.submit(pool), self.submit(pool)]
            for handle in handles:
                self.await_state(pool, handle, 'returned')
            class InterruptedDelete(dict):
                def __delitem__(self, key):
                    super().__delitem__(key)
                    raise RuntimeError('SYNTHETIC-PRIVATE')
            pool._jobs = InterruptedDelete(pool._jobs)
            for handle in handles:
                with self.assertRaises(workers.LearningWorkerError):
                    pool.take(handle)
            self.assertEqual((2, 2), (pool.snapshot()['jobs'], pool.snapshot()['unknown']))

    def test_typed_capture_owner_traps_worker_wire_and_charge_until_take(self):
        from contextlib import ExitStack
        import builtins
        import hashlib
        import json
        from codex_agents import learning_cycle_input as inputs
        from tests.test_learning_cycle_input import typed, wire
        value = typed([verification_outcome()])
        expected = wire(value)
        _, charge = inputs.capture_learning_input(value, operation='select_mining')
        pool = self.make_pool(limits(input_bytes=2 * charge))
        owner = threading.current_thread()
        calls = []
        def worker_only(original):
            def guard(*args, **kwargs):
                if threading.current_thread() is owner:
                    raise AssertionError('owner codec/hash/IO/thread/process')
                return original(*args, **kwargs)
            return guard
        def run(raw, **options):
            self.assertIsNot(owner, threading.current_thread())
            self.assertEqual(expected, raw)
            self.assertEqual(job.deadline_ns, options['deadline_ns'])
            self.assertIs(job.cancelled, options['cancelled'])
            calls.append(raw)
            return self.complete(raw, **options)
        with mock.patch.object(execution, '_execute_learning_job', side_effect=run):
            with ExitStack() as traps:
                for module, name in ((inputs, 'canonical_json'), (inputs, 'parse_bounded_json'),
                        (inputs, 'validate_outcome'), (inputs, 'validate_evaluation_case'),
                        (hashlib, 'sha256'), (json, 'dumps'), (json, 'loads'),
                        (builtins, 'open'), (execution.subprocess, 'Popen'),
                        (threading.Thread, 'start'), (threading.Thread, 'join'),
                        (threading, 'Thread')):
                    traps.enter_context(mock.patch.object(module, name, worker_only(getattr(module, name))))
                handle = self.submit(pool, value)
                job = pool._jobs[handle]
                self.assertIsNot(value, job.raw)
                object.__setattr__(value, 'outcomes', ())
                self.await_state(pool, handle, 'returned')
                self.assertEqual(1, len(calls))
                self.assertEqual(charge, pool.snapshot()['input_bytes'])
                self.assertEqual(workers.WIRE_BYTES_PER_WORKER, pool.snapshot()['active_protocol_bytes'])
                frame = __import__('sys')._current_frames()[pool._slots[0].thread.ident]
                while frame.f_code.co_name != '_worker_loop':
                    frame = frame.f_back
                self.assertIsNone(frame.f_locals['envelope'])
                self.assertIsNone(frame.f_locals['result'])
                self.assertNotIn('raw', frame.f_locals)
                self.assertEqual('learning_execution_completed', pool.take(handle).reason)
                self.assertEqual(0, pool.snapshot()['input_bytes'])

    def test_typed_intake_refusal_mutation_and_known_worker_failure_never_launch(self):
        from codex_agents import learning_cycle_input as inputs
        from tests.test_learning_cycle_input import entry, typed
        pool = self.make_pool(limits(input_bytes=8 * 1_048_576))
        class Subclass(inputs.LearningCycleInput):
            def __getattribute__(self, name):
                raise AssertionError('subclass callback')
        with mock.patch.object(execution, '_execute_learning_job') as run:
            for value in (Subclass((), ()), inputs.LearningCycleInput([], ()),
                          typed(as_of_ms=0), inputs.LearningCycleInput((('a', 'd' * 64, b''),), ())):
                with self.assertRaises(workers.LearningWorkerError):
                    self.submit(pool, value)
            self.assertEqual(0, pool.snapshot()['jobs'])
            record = entry(verification_outcome())
            for changed in (('different-id', record[1], record[2]),
                            (record[0], '0' * 64, record[2]),
                            (record[0], record[1], record[2] + b' ')):
                handle = self.submit(pool, inputs.LearningCycleInput((changed,), ()))
                charge = pool.snapshot()['input_bytes']
                self.await_state(pool, handle, 'returned')
                self.assertEqual(charge, pool.snapshot()['input_bytes'])
                result = pool.take(handle)
                self.assertEqual(('learning_execution_invalid_output', None, 0, True),
                    (result.reason, result.result, result.roots_reaped, result.no_process_started))
            handle = self.submit(pool, typed([verification_outcome()]))
            object.__setattr__(pool._jobs[handle].raw, 'outcomes', [])
            self.await_state(pool, handle, 'returned')
            self.assertEqual('learning_execution_invalid_output', pool.take(handle).reason)
            run.assert_not_called()

    def test_invalid_unicode_invalidation_is_refused_before_helper_launch(self):
        from codex_agents.learning_cycle_core import select_mining_outcomes
        from tests.test_learning_cycle_input import typed
        value = typed(invalidated=('\ud800',))
        with self.assertRaises(UnicodeEncodeError):
            select_mining_outcomes([], existing_cases=[], owner_invalidated_run_ids=value.owner_invalidated_run_ids)
        pool = self.make_pool(limits(input_bytes=8 * 1_048_576))
        with mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete) as run:
            handle = self.submit(pool, value)
            self.await_state(pool, handle, 'returned')
            result = pool.take(handle)
            self.assertEqual(('learning_execution_invalid_output', None, 0, True),
                             (result.reason, result.result, result.roots_reaped, result.no_process_started))
            run.assert_not_called()

    def test_typed_original_deadline_cancel_and_clock_during_encoding_never_launch(self):
        from types import SimpleNamespace
        from codex_agents import learning_cycle_input as inputs
        from tests.test_learning_cycle_input import typed
        for mode in ('cancelled', 'expired', 'clock_failure'):
            pool = self.make_pool(limits(input_bytes=8 * 1_048_576))
            handle = self.submit(pool, typed([verification_outcome()]), seconds=0.2 if mode == 'expired' else 10)
            job = pool._jobs[handle]
            validate = inputs.validate_outcome
            def interrupt(record):
                checked = validate(record)
                if mode == 'cancelled':
                    job.cancelled.set()
                elif mode == 'expired':
                    time.sleep(0.25)
                return checked
            original_encode = workers.encode_learning_input
            def encode(value, **options):
                self.assertEqual(job.deadline_ns, options['deadline_ns'])
                self.assertIs(job.cancelled, options['cancelled'])
                if mode == 'clock_failure':
                    now = time.monotonic_ns()
                    ticks = iter((now, now + 1, now))
                    with mock.patch.object(inputs, 'time', SimpleNamespace(monotonic_ns=lambda: next(ticks))):
                        return original_encode(value, **options)
                return original_encode(value, **options)
            with mock.patch.object(inputs, 'validate_outcome', side_effect=interrupt), \
                    mock.patch.object(workers, 'encode_learning_input', side_effect=encode), \
                    mock.patch.object(execution, '_execute_learning_job') as run:
                self.await_state(pool, handle, 'returned')
                result = pool.take(handle)
                self.assertEqual(('learning_execution_' + mode, None, 0, True),
                    (result.reason, result.result, result.roots_reaped, result.no_process_started))
                if mode == 'clock_failure':
                    self.assertIsNone(result.finished_ns)
                run.assert_not_called()

    def test_typed_overload_queued_stops_and_unknown_keep_exact_charge(self):
        from codex_agents import learning_cycle_input as inputs
        from tests.test_learning_cycle_input import typed
        value = typed([verification_outcome()])
        _, charge = inputs.capture_learning_input(value, operation='select_mining')
        with mock.patch.object(execution, '_execute_learning_job') as run:
            pool = self.make_pool(limits(input_bytes=charge))
            handle = self.submit(pool, value)
            with self.assertRaisesRegex(workers.LearningWorkerError, 'learning_pool_overloaded'):
                self.submit(pool, value)
            pool.cancel(handle)
            pool.poll()
            self.assertEqual(charge, pool.snapshot()['input_bytes'])
            self.assertEqual('learning_execution_cancelled', pool.take(handle).reason)
            run.assert_not_called()
        for mode in ('encode', 'publication'):
            pool = self.make_pool(limits(input_bytes=charge))
            slot = pool._slots[0]
            with mock.patch.object(execution, '_execute_learning_job', side_effect=self.complete) as run:
                target, name = (workers, 'encode_learning_input') if mode == 'encode' else (slot.outgoing, 'put_nowait')
                with mock.patch.object(target, name, side_effect=RuntimeError('SYNTHETIC-PRIVATE')):
                    handle = self.submit(pool, value)
                    self.await_state(pool, handle, 'unknown')
                if mode == 'encode':
                    run.assert_not_called()
            self.assertIsNone(pool.take(handle))
            self.assertEqual((1, charge, 1), (pool.snapshot()['jobs'],
                pool.snapshot()['input_bytes'], pool.snapshot()['unknown']))
            pool.request_close()
            pool.poll()
            self.assertFalse(pool.snapshot()['closed'])
            self.assertNotIn('SYNTHETIC', repr(pool.snapshot()))

    @unittest.skipUnless(MACOS, 'disposable Darwin typed helper; no native service acceptance')
    def test_actual_typed_nonempty_selection_and_plan_through_fixed_pool(self):
        from codex_agents import learning_cycle_input as inputs
        from codex_agents.learning_contracts import content_digest
        from codex_agents.learning_cycle_core import plan_learning_cycle
        from tests.learning_support import heldout_cases
        from tests.test_learning_cycle_input import typed
        outcome = verification_outcome('outcome-00')
        case = heldout_cases(1)[0]
        pool = self.make_pool(limits(input_bytes=8 * 1_048_576))
        for operation, clocks in (('select_mining', {}), ('plan', dict(as_of_ms=2, max_age_ms=10))):
            cases = [] if operation == 'select_mining' else [case]
            value = typed([outcome], cases, **clocks)
            _, charge = inputs.capture_learning_input(value, operation=operation)
            handle = pool.submit(value, operation=operation, binding_digest=BINDING,
                deadline_ns=time.monotonic_ns() + 10**10)
            self.await_state(pool, handle, 'returned')
            self.assertEqual(charge, pool.snapshot()['input_bytes'])
            result = pool.take(handle)
            self.assertEqual(('learning_execution_completed', 1, False),
                (result.reason, result.roots_reaped, result.no_process_started))
            expected = ({'mining_references': [[outcome['record_id'], content_digest(outcome)]]}
                if operation == 'select_mining' else plan_learning_cycle([outcome], all_cases=[case], **clocks))
            self.assertEqual(expected, result.result)
            self.assertEqual(0, pool.snapshot()['input_bytes'])

    @unittest.skipUnless(MACOS, 'disposable Darwin helper policy; no native service acceptance')
    def test_actual_nonempty_helper_result_and_nonblocking_close(self):
        pool = self.make_pool()
        handle = self.submit(pool, request(outcomes=[verification_outcome()]))
        self.await_state(pool, handle, 'returned')
        result = pool.take(handle)
        self.assertEqual('learning_execution_completed', result.reason)
        self.assertEqual(1, len(result.result['mining_references']))
        begin = time.monotonic_ns()
        pool.request_close()
        self.assertLess(time.monotonic_ns() - begin, 2_000_000_000)

    @unittest.skipUnless(MACOS, 'disposable Darwin helpers; no actual admitted-model or native service acceptance')
    def test_actual_stalled_helper_keeps_other_pool_and_owner_control_responsive(self):
        from codex_agents import learning_cycle_process as helper
        raw = request(outcomes=[verification_outcome()])
        first, other = self.make_pool(), self.make_pool()
        ready = threading.Event()
        observed = []
        original_sample = execution._Context.sample
        def sample(context):
            if context.guarded and context.offset == len(context.raw) and context.process.stdin.closed:
                if not observed:
                    observed.append(context)
                ready.set()
            return original_sample(context)
        probe = '''from codex_agents import learning_cycle_process as p
import time
def stall(*args, **kwargs):
    time.sleep(5)
    return b""
p.calculate_learning_request=stall
'''
        bootstrap = execution._BOOTSTRAP.replace('from codex_agents.learning_cycle_process import main',
            probe + 'from codex_agents.learning_cycle_process import main')
        with mock.patch.object(execution, '_BOOTSTRAP', bootstrap), \
                mock.patch.object(execution._Context, 'sample', sample):
            handle = self.submit(first)
            deadline = time.monotonic() + 4
            while not ready.is_set():
                self.assertLess(time.monotonic(), deadline)
                first.poll()
                time.sleep(0.002)
        owner = threading.current_thread()
        def worker_only(original):
            def guard(*args, **kwargs):
                if threading.current_thread() is owner:
                    raise AssertionError('owner attempted blocking worker/codec operation')
                return original(*args, **kwargs)
            return guard
        with mock.patch.object(execution.subprocess, 'Popen', worker_only(execution.subprocess.Popen)), \
                mock.patch.object(execution, 'decode_calculation_reply', worker_only(execution.decode_calculation_reply)), \
                mock.patch.object(helper, 'canonical_json', worker_only(helper.canonical_json)), \
                mock.patch.object(workers.threading.Thread, 'start', worker_only(workers.threading.Thread.start)), \
                mock.patch.object(workers.threading.Thread, 'join', worker_only(workers.threading.Thread.join)):
            begin = time.monotonic_ns()
            second = self.submit(other, raw)
            self.await_state(other, second, 'returned')
            self.assertEqual('learning_execution_completed', other.take(second).reason)
            self.assertEqual('running', first.state(handle))
            self.assertFalse(observed[0].record()[3])
            self.assertEqual('pending_confirmation', first.cancel(handle))
            self.assertLess(time.monotonic_ns() - begin, 2_000_000_000)
            self.await_state(first, handle, 'returned')
            result = first.take(handle)
        self.assertEqual(('learning_execution_cancelled', None, 1),
                         (result.reason, result.result, result.roots_reaped))
