"""Fix-round component evidence: real threads, event handoffs, no polling."""
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from codex_agents.ledger_owner_queue import LedgerOwnerQueue, LedgerQueueBusy, LedgerQueueLimits

A = 'prj_' + 'a' * 32
ERROR = {'ok': False, 'reason_code': 'operation_rejected'}


class LedgerQueueLifecycleTests(unittest.TestCase):
    __why_not_scenario__ = ('Canonical standalone queue thread lifecycle proof; no ledger or '
                            'process boundary is involved in this slice.')

    def setUp(self):
        self.now = 1_000_000_000
        self.queue = LedgerOwnerQueue(clock_ns=lambda: self.now)

    def offer(self, operation=lambda: 'ok', **options):
        return self.queue.offer(A, operation, caller=options.get('caller', 'account'),
                                busy_reason='account_busy', deadline_ns=self.now + 800_000_000)

    def busy(self, operation):
        with self.assertRaises(LedgerQueueBusy) as caught:
            operation()
        self.assertEqual(caught.exception.reason_code, 'account_busy')

    def error_result(self, handle):
        try:
            return self.queue.wait(handle)
        except BaseException as error:
            self.fail(f'caller received {type(error).__name__} instead of a plain result')

    def start(self, target):
        errors = []
        def run():
            try:
                target()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(lambda: thread.join(2))
        return thread, errors

    def finish(self, thread, errors):
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    @requirements('E1b-6', 'E1b-7')
    def test_close_idle_refuses_and_acknowledges_queued_work_idempotently(self):
        operation = mock.Mock()
        handles = [self.offer(operation), self.offer(operation)]
        self.queue.close()
        self.queue.close()
        self.assertTrue(self.queue.drain(0))
        for handle in handles:
            self.assertTrue(handle.handled.is_set())
            self.busy(lambda: self.queue.wait(handle))
        operation.assert_not_called()
        self.assertEqual(self.queue.snapshot()['retained'], 0)
        self.assertEqual(self.queue.snapshot()['projects'], 0)
        self.assertFalse(self.queue.execute_next())
        self.busy(self.offer)
        self.assertEqual(self.queue.snapshot()['refusals'], {'account': {'closed': 3}})

    @requirements('E1b-6', 'E1b-7')
    def test_close_is_bounded_while_running_and_drains_after_whole_completion(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        effects = []
        def operation():
            effects.append('begin')
            entered.set()
            self.assertTrue(release.wait(2))
            effects.append('commit')
            return 'committed'
        running = self.offer(operation)
        queued_op = mock.Mock()
        queued = self.offer(queued_op)
        thread, errors = self.start(self.queue.execute_next)
        try:
            self.assertTrue(entered.wait(2))
            self.queue.close()
            self.queue.close()
            self.busy(self.offer)
            self.assertFalse(self.queue.drain(0.001))
            self.assertEqual(effects, ['begin'])
            self.assertFalse(running.handled.is_set())
            self.assertFalse(queued.handled.is_set())
            self.assertEqual(self.queue.snapshot()['retained'], 2)
        finally:
            release.set()
        self.finish(thread, errors)
        self.assertTrue(self.queue.drain(0))
        self.assertEqual(effects, ['begin', 'commit'])
        self.assertEqual(self.queue.wait(running), 'committed')
        self.assertTrue(running.handled.is_set())
        self.assertTrue(queued.handled.is_set())
        self.busy(lambda: self.queue.wait(queued))
        queued_op.assert_not_called()
        self.assertEqual(self.queue.snapshot()['retained'], 0)
        self.assertEqual(self.queue.snapshot()['projects'], 0)
        self.assertEqual(self.queue.snapshot()['refusals'], {'account': {'closed': 2}})

    @requirements('E1b-5', 'E1b-7')
    def test_operation_error_is_plain_result_and_owner_serves_next_request(self):
        error = KeyError('private operation detail')
        def fail():
            raise error
        failed = self.offer(fail)
        next_handle = self.offer()
        def owner():
            self.assertTrue(self.queue.execute_next())
            self.assertTrue(self.queue.execute_next())
        thread, errors = self.start(owner)
        self.finish(thread, errors)
        self.assertTrue(failed.handled.is_set())
        self.assertEqual(self.queue.wait(failed), ERROR)
        self.assertIsNone(getattr(failed, '_error', None))
        result = self.queue.wait(failed)
        result['ok'] = 'mutated'
        self.assertEqual(self.queue.wait(failed), ERROR)
        self.assertEqual(self.queue.wait(next_handle), 'ok')
        self.assertEqual(self.queue.snapshot()['retained'], 0)

    @requirements('E1b-5', 'E1b-7')
    def test_base_exceptions_stay_on_owner_and_are_acknowledged(self):
        for kind in (KeyboardInterrupt, SystemExit):
            with self.subTest(kind=kind):
                self.queue = LedgerOwnerQueue(clock_ns=lambda: self.now)
                error = kind('owner only')
                def fail():
                    raise error
                handle = self.offer(fail)
                thread, errors = self.start(self.queue.execute_next)
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [error])
                self.assertTrue(handle.handled.is_set())
                self.assertEqual(self.error_result(handle), ERROR)
                self.assertIsNone(getattr(handle, '_error', None))
                self.assertEqual(self.queue.snapshot()['retained'], 0)

    def waiting_owner(self, action):
        waiting = threading.Event()
        original = threading.Condition.wait
        notified = []
        def observe(condition, timeout=None):
            # Called with the queue lock held, before Condition atomically releases it.
            if condition is self.queue._work:
                waiting.set()
                result = original(condition, timeout)
                notified.append(result)
                return result
            return original(condition, timeout)
        answers = []
        with mock.patch.object(threading.Condition, 'wait', observe):
            thread, errors = self.start(lambda: answers.append(self.queue.wait_for_work(2)))
            try:
                self.assertTrue(waiting.wait(2))
                action()
            finally:
                # Release a waiter even if the action/assertion fails.
                self.queue.close()
            self.finish(thread, errors)
        self.assertEqual(notified, [True], 'owner must wake by notification, not timeout')
        return answers

    @requirements('E1b-3')
    def test_offer_wakes_waiting_owner_without_polling(self):
        waiting = threading.Event()
        original = threading.Condition.wait
        notified = []
        def observe(condition, timeout=None):
            if condition is self.queue._work:
                waiting.set()
                result = original(condition, timeout)
                notified.append(result)
                return result
            return original(condition, timeout)
        answers = []
        def owner():
            answers.append(self.queue.wait_for_work(2))
            self.assertTrue(self.queue.execute_next())
        with mock.patch.object(threading.Condition, 'wait', observe):
            thread, errors = self.start(owner)
            try:
                self.assertTrue(waiting.wait(2))
                handle = self.offer()
                self.finish(thread, errors)
            finally:
                self.queue.close()
        self.assertEqual(answers, [True])
        self.assertEqual(notified, [True], 'owner must wake by notification, not timeout')
        self.assertTrue(handle.handled.is_set())
        self.assertEqual(self.queue.wait(handle), 'ok')

    @requirements('E1b-3', 'E1b-6')
    def test_close_wakes_waiting_owner_and_timeout_is_bounded(self):
        self.assertFalse(self.queue.wait_for_work(0))
        self.assertEqual(self.waiting_owner(self.queue.close), [False])
        self.assertFalse(self.queue.wait_for_work(0))
        self.assertTrue(self.queue.drain(0))
        for timeout in (-1, float('inf'), float('nan'), None):
            for api in (self.queue.wait_for_work, self.queue.drain):
                with self.subTest(timeout=timeout, api=api), self.assertRaises(ValueError):
                    api(timeout)

    @requirements('E1b-3')
    def test_refusal_caller_buckets_are_bounded_and_preserve_totals(self):
        self.queue.close()
        for index in range(100):
            self.busy(lambda: self.offer(caller=f'caller-{index}'))
        counters = self.queue.snapshot()['refusals']
        self.assertEqual(len(counters), 16)
        self.assertEqual(counters['caller-0'], {'closed': 1})
        self.assertEqual(counters['other'], {'closed': 85})
        self.assertEqual(sum(row['closed'] for row in counters.values()), 100)

    @requirements('E1b-3', 'E1b-4')
    def test_documented_owner_binding_and_deadline_precedence(self):
        self.assertFalse(self.queue.execute_next())
        thread, errors = self.start(self.queue.execute_next)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        handle = self.offer()
        self.queue.abandon(handle)
        self.now += 800_000_000
        self.assertTrue(self.queue.execute_next())
        self.busy(lambda: self.queue.wait(handle))
        self.assertEqual(self.queue.snapshot()['refusals'], {'account': {'deadline': 1}})

    @requirements('E1b-3')
    def test_overflow_refusal_counters_are_bounded_without_close(self):
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(16, 8, 2, 800_000_000), clock_ns=lambda: self.now)
        self.offer()
        self.offer()
        for index in range(100):
            self.busy(lambda: self.offer(caller=f'caller-{index}'))
        counters = self.queue.snapshot()['refusals']
        self.assertEqual(len(counters), 16)
        self.assertEqual(counters['caller-0'], {'project_full': 1})
        self.assertEqual(counters['other'], {'project_full': 85})
        self.assertEqual(sum(row['project_full'] for row in counters.values()), 100)

    @requirements('E1b-6', 'E1b-7')
    def test_drain_waiter_wakes_after_final_acknowledgement(self):
        entered, release, draining = (threading.Event() for _ in range(3))
        self.addCleanup(release.set)
        def operation():
            entered.set()
            self.assertTrue(release.wait(2))
        handle = self.offer(operation)
        owner, errors = self.start(self.queue.execute_next)
        try:
            self.assertTrue(entered.wait(2))
            original = self.queue._drained.wait
            def observe(timeout):
                draining.set()
                return original(timeout)
            answers = []
            with mock.patch.object(self.queue._drained, 'wait', side_effect=observe):
                waiter, waiter_errors = self.start(lambda: answers.append(self.queue.drain(2)))
                self.assertTrue(draining.wait(2))
                self.assertFalse(handle.handled.is_set())
                release.set()
                self.finish(waiter, waiter_errors)
            self.finish(owner, errors)
            self.assertEqual(answers, [True])
            self.assertTrue(handle.handled.is_set())
            self.assertEqual(self.queue.snapshot()['retained'], 0)
        finally:
            release.set()
