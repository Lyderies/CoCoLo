"""Queue isolation/custody with a fake journal boundary, never native proof.

Real original-account MAC and journal writes run in test_project_execution_processes.
Here a fixed fake writer result isolates queue stalls, lost replies and custody.
"""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_lease_lane as fixtures

from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory


class HostOpenLaneTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ResultLeaseLaneTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.worker, self.lane = self.base.worker, self.base.lane
        self.entry = SimpleNamespace(record_id='host_open_provenance:'+'1'*64, content_digest='2'*64)

    def prepare_until(self, opening):
        def before(target, worker_handle):
            self.base.before(target, worker_handle, opening=opening)
        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.worker.prepare(before_bind=before, after_bind=self.base.after)
        self.assertEqual(0, len(self.worker.registry._issued))
        self.assertTrue(all(not (root/'launches').exists() for root in self.worker.workspaces))

    def test_blocked_journal_writer_keeps_control_and_cancellation_responsive(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('fixture journal gate expired')
            kwargs['before_commit']()
            return self.entry
        def opening(target, worker_handle, handle):
            started = time.monotonic()
            self.lane.record_host_open(handle, b'{}')
            self.assertLess(time.monotonic()-started, 1)
            self.assertTrue(entered.wait(3))
            started = time.monotonic()
            self.assertEqual('stop_requested', self.worker.request(target).result['cancellation_state'])
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual('recording_host_open', self.lane.snapshot(handle)['state'])
            with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
                self.lane.host_open_record(handle)
            resume.set()
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual(self.entry.record_id, self.lane.host_open_record(handle)['record_id'])
            raise HistoryError('history_result_not_ready')  # Cancelled frontend never claims/launches.
        with mock.patch.object(ProjectHistory, 'record_host_open', new=blocked):
            self.prepare_until(opening)

    def test_lost_journal_reply_retains_exact_original_proof_and_retries_no_execution(self):
        observed = []
        def lose_first(store, binding, request, proof, **kwargs):
            kwargs['before_commit']()
            observed.append((binding, request, proof))
            if len(observed) == 1:
                raise OSError('PRIVATE-LOST-JOURNAL-REPLY')
            return self.entry
        def opening(target, worker_handle, handle):
            self.lane.record_host_open(handle, b'{}')
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual('failed', self.lane.snapshot(handle)['state'])
            with self.assertRaisesRegex(HistoryError, 'history_conflict'):
                self.lane.record_host_open(handle, b'{} ')
            self.lane.record_host_open(handle)
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual('claim_pending', self.lane.snapshot(handle)['state'])
            self.assertEqual({'record_id': self.entry.record_id, 'content_digest': self.entry.content_digest},
                self.lane.host_open_record(handle))
            self.assertEqual(observed[0], observed[1])
            raise HistoryError('history_result_not_ready')
        with mock.patch.object(ProjectHistory, 'record_host_open', new=lose_first):
            self.prepare_until(opening)

    def test_withdrawn_prepared_worker_cannot_commit_journal_after_the_custody_callback(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('fixture custody gate expired')
            kwargs['before_commit']()
            self.fail('withdrawn worker reached fake commit')
        def opening(target, worker_handle, handle):
            self.lane.record_host_open(handle, b'{}')
            self.assertTrue(entered.wait(3))
            self.worker.pool.withdraw_prepared(worker_handle)
            resume.set()
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual('history_not_authorized', self.lane.snapshot(handle)['reason_code'])
            with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
                self.lane.host_open_record(handle)
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                self.lane.record_host_open(handle)
            raise HistoryError('history_result_not_ready')
        with mock.patch.object(ProjectHistory, 'record_host_open', new=blocked):
            self.prepare_until(opening)

    def test_withdrawal_after_precommit_cannot_publish_a_successful_writer_acknowledgement(self):
        committed, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        records = []
        def commit_then_wait(store, *args, **kwargs):
            kwargs['before_commit']()
            records.append(self.entry)  # Fixed boundary has committed; reply is still withheld.
            committed.set()
            if not resume.wait(5):
                raise RuntimeError('fixture postcommit gate expired')
            return self.entry
        def opening(target, worker_handle, handle):
            self.lane.record_host_open(handle, b'{}')
            self.assertTrue(committed.wait(3))
            self.worker.pool.withdraw_prepared(worker_handle)
            resume.set()
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual('failed', self.lane.snapshot(handle)['state'])
            self.assertEqual('history_not_authorized', self.lane.snapshot(handle)['reason_code'])
            self.assertEqual([self.entry], records)  # Do not erase/rebuild a committed journal.
            with self.assertRaises(HistoryError):
                self.lane.host_open_record(handle)
            raise HistoryError('history_result_not_ready')
        with mock.patch.object(ProjectHistory, 'record_host_open', new=commit_then_wait):
            self.prepare_until(opening)

    def test_previously_confirmed_acknowledgement_rechecks_prepared_worker_custody(self):
        def commit(store, *args, **kwargs):
            kwargs['before_commit']()
            return self.entry
        def opening(target, worker_handle, handle):
            self.lane.record_host_open(handle, b'{}')
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual(self.entry.record_id, self.lane.host_open_record(handle)['record_id'])
            self.worker.pool.withdraw_prepared(worker_handle)
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                self.lane.host_open_record(handle)
            raise HistoryError('history_result_not_ready')
        with mock.patch.object(ProjectHistory, 'record_host_open', new=commit):
            self.prepare_until(opening)

    def test_pool_close_invalidates_a_previously_confirmed_acknowledgement(self):
        def commit(store, *args, **kwargs):
            kwargs['before_commit']()
            return self.entry
        def opening(target, worker_handle, handle):
            self.lane.record_host_open(handle, b'{}')
            self.worker.until(lambda: self.lane.snapshot(handle)['state'] != 'recording_host_open')
            self.assertEqual(self.entry.record_id, self.lane.host_open_record(handle)['record_id'])
            self.worker.pool.close()
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                self.lane.host_open_record(handle)
            raise HistoryError('history_result_not_ready')
        with mock.patch.object(ProjectHistory, 'record_host_open', new=commit):
            self.prepare_until(opening)
