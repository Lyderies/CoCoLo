"""Actual fixed inert runner custody; no native peer or account release proof."""
from copy import copy
import queue
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_codex_worker as fixtures

from codex_agents import codex_worker, owned_execution
from codex_agents.owned_execution import ExecutionCleanup, ExecutionCustodyError, OwnedExecutionRegistry


class CleanupCustodyTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.CodexWorkerTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.registry, self.pool = self.base.registry, self.base.pool

    def reader(self):
        incoming, returned, ready = queue.Queue(), queue.Queue(), threading.Event()
        def run():
            ready.set()
            while True:
                action = incoming.get()
                if action is None:
                    return
                try:
                    returned.put((True, action()))
                except BaseException as error:
                    returned.put((False, error))
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(5))
        def stop():
            if thread.is_alive():
                incoming.put(None)
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        def call(action):
            incoming.put(action)
            ok, value = returned.get(timeout=5)
            if not ok:
                raise value
            return value
        return thread, call, stop

    def completed(self, number=1):
        prepared = self.base.prepare(number=number)
        self.base.returned(prepared)
        self.assertTrue(prepared[4].snapshot()['cleanup_confirmed'])
        return prepared

    def retain(self, prepared, consumer):
        return self.registry.retain_cleanup(prepared[2], self.pool, prepared[3], consumer=consumer)

    def test_original_cleaned_return_has_one_reference_for_selected_reader_only(self):
        prepared = self.completed()
        reader, read, _ = self.reader()
        other, wrong_read, _ = self.reader()
        proof = self.retain(prepared, reader)
        self.assertIs(proof, self.retain(prepared, reader))
        original = self.registry.key(prepared[2], cleaned=True)
        expected = {'key': original, 'deadline_ns': prepared[4].target()['deadline_ns']}
        self.assertEqual(expected, self.registry.cleanup_snapshot(proof))
        self.assertEqual(expected, read(lambda: self.registry.cleanup_snapshot(proof)))
        with self.assertRaises(ExecutionCustodyError):
            wrong_read(lambda: self.registry.cleanup_snapshot(proof))
        with self.assertRaises(ExecutionCustodyError):
            self.retain(prepared, other)
        # Returned data cannot mutate the actual registry key or release it.
        snapshot = read(lambda: self.registry.cleanup_snapshot(proof))
        object.__setattr__(snapshot['key'], 'run_id', 'run:'+'f'*64)
        self.assertEqual(original, self.registry.cleanup_snapshot(proof)['key'])
        with self.assertRaises(ExecutionCustodyError):
            self.registry.acknowledge_release(snapshot['key'])
        self.assertIsNotNone(self.registry._pending.get(prepared[2]))

    def test_copied_fabricated_foreign_and_forked_references_have_no_membership(self):
        prepared = self.completed()
        reader, read, _ = self.reader()
        proof = self.retain(prepared, reader)
        for candidate in (copy(proof), ExecutionCleanup(self.registry), ExecutionCleanup(OwnedExecutionRegistry())):
            with self.subTest(candidate=type(candidate)), self.assertRaises(ExecutionCustodyError):
                read(lambda: self.registry.cleanup_snapshot(candidate))
        with self.assertRaises(ExecutionCustodyError):
            OwnedExecutionRegistry().cleanup_snapshot(proof)
        with mock.patch.object(self.registry, '_pid', -1), self.assertRaises(ExecutionCustodyError):
            self.registry.cleanup_snapshot(proof)

    def test_prepared_unlaunched_worker_is_not_confirmed_cleanup(self):
        prepared = self.base.prepare(submit=False)
        reader, _, _ = self.reader()
        with self.assertRaises(ExecutionCustodyError):
            self.retain(prepared, reader)
        self.assertEqual(0, len(self.registry._cleanups))
        self.pool.submit(prepared[3])
        self.base.returned(prepared)
        self.assertIsInstance(self.retain(prepared, reader), ExecutionCleanup)

    def test_reaped_process_before_actual_worker_return_is_still_unavailable(self):
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = codex_worker._PREPARE_RESULT
        def blocked(*args):
            ready.set()
            if not resume.wait(5):
                raise RuntimeError('fixture_validation_gate')
            return original(*args)
        reader, _, _ = self.reader()
        with mock.patch.object(codex_worker, '_PREPARE_RESULT', side_effect=blocked):
            prepared = self.base.prepare()
            try:
                self.assertTrue(ready.wait(5))
                self.assertTrue(prepared[4].snapshot()['cleanup_confirmed'])
                self.assertFalse(prepared[4].snapshot()['worker_returned'])
                with self.assertRaises(ExecutionCustodyError):
                    self.retain(prepared, reader)
            finally:
                resume.set()
            self.base.returned(prepared)
        self.assertIsInstance(self.retain(prepared, reader), ExecutionCleanup)

    def test_selected_reader_cannot_capture_or_substitute_another_original_worker(self):
        first, second = self.completed(), self.completed(2)
        reader, read, _ = self.reader()
        with self.assertRaises(ExecutionCustodyError):
            read(lambda: self.retain(first, reader))
        with self.assertRaises(ExecutionCustodyError):
            self.registry.retain_cleanup(first[2], self.pool, second[3], consumer=reader)
        self.assertEqual(0, len(self.registry._cleanups))

    def test_original_identity_deadline_and_owner_health_are_rechecked(self):
        prepared = self.completed()
        reader, read, _ = self.reader()
        proof = self.retain(prepared, reader)
        entry = self.registry._issued[prepared[2]]
        deadline, run = entry.deadline_ns, entry.key.run_id
        try:
            entry.deadline_ns += 1
            with self.assertRaises(ExecutionCustodyError):
                read(lambda: self.registry.cleanup_snapshot(proof))
            entry.deadline_ns = deadline
            object.__setattr__(entry.key, 'run_id', 'run:'+'f'*64)
            with self.assertRaises(ExecutionCustodyError):
                self.registry.cleanup_snapshot(proof)
        finally:
            entry.deadline_ns = deadline
            object.__setattr__(entry.key, 'run_id', run)
        self.assertEqual(run, read(lambda: self.registry.cleanup_snapshot(proof))['key'].run_id)
        self.pool.close()
        with self.assertRaises(ExecutionCustodyError):
            read(lambda: self.registry.cleanup_snapshot(proof))
        self.assertIsNotNone(self.registry._pending.get(prepared[2]))

    def test_dead_reader_cannot_be_replaced_or_turn_uncertainty_into_release(self):
        prepared = self.completed()
        reader, _, stop = self.reader()
        proof = self.retain(prepared, reader)
        stop()
        with self.assertRaises(ExecutionCustodyError):
            self.registry.cleanup_snapshot(proof)
        other, _, _ = self.reader()
        with self.assertRaises(ExecutionCustodyError):
            self.retain(prepared, other)
        self.assertIsNotNone(self.registry._pending.get(prepared[2]))

    def test_reference_count_is_bounded_and_native_retirement_invalidates_it(self):
        first, second = self.completed(), self.completed(2)
        reader, read, _ = self.reader()
        proof = self.retain(first, reader)
        with mock.patch.object(owned_execution, 'MAX_OWNED_EXECUTIONS', 1), self.assertRaises(ExecutionCustodyError):
            self.retain(second, reader)
        self.assertEqual(1, len(self.registry._cleanups))
        # Explicit test boundary: this local acknowledgement proves no account
        # commit. Production must verify its original account receipt first.
        self.registry.acknowledge_release(first[2])
        with self.assertRaises(ExecutionCustodyError):
            read(lambda: self.registry.cleanup_snapshot(proof))
        self.assertEqual(0, len(self.registry._cleanups))
        self.assertIsInstance(self.retain(second, reader), ExecutionCleanup)
