"""RS-B section 12: real writer requests driven by the owner's maintenance step, without sockets."""
import contextlib
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_bridge_liveness as owners
from tests import test_result_leases as leases
from tests.test_s13_budgets import measurements
from codex_agents.codex_worker import CodexWorkerLimits, CodexWorkerPool
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.history_contracts import HistoryError
from codex_agents.project_driver import RunDriver
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceHandle, ResultPersistenceLane, _Pending

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

WHY = 'Real persistence lane and disposable SQLite store; virtual driver steps count cadence without sockets.'


class DueDrainTests(unittest.TestCase):
    def start(self, count, blocked=0):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.fixture.capacity = ResultCapacity(2048, 2048 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen(result_capacity=self.base.fixture.capacity)
        for number in range(1, count + 1):
            self.base.reserve(number)
        self.base.store.close()
        owner = owners.BridgeLivenessTests(methodName='runTest')
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        pool = CodexWorkerPool(OwnedExecutionRegistry(), CodexWorkerLimits(2, 16 * 1_048_576))
        self.addCleanup(self.stop_pool, pool)
        self.lane = ResultPersistenceLane(owner.owner, pool,
            lambda: self.base.fixture.open(result_capacity=self.base.fixture.capacity),
            max_entries=4, retained_bytes=4 * MAX_PERSISTENCE_ENTRY_BYTES,
            allow_local_execution_reference=True)
        self.addCleanup(self.stop)
        self.lane._project = leases.lease_binding(1).project_id
        self.lane._runtime = leases.lease_binding(1).runtime_digest
        self.handles = [ResultPersistenceHandle() for _ in range(blocked)]
        for number, handle in enumerate(self.handles, 1):
            self.lane._jobs[handle] = _Pending(leases.lease_binding(number), None, None, None, None,
                                               lease_binding=leases.lease_binding(number))
        self.now = 0.0
        self.driver = SimpleNamespace(_expiry=None, _next_expiry=0.0, _clock=lambda: self.now,
                                       _runtime=SimpleNamespace(persistence=self.lane))
        self.passes = []
        original = ProjectHistory.release_earlier_unarmed_leases
        module = measurements()
        def observed(store, live, *, limit=10):
            with module.instrumented(store) as connection:
                try:
                    return original(store, live, limit=limit)
                finally:
                    self.passes.append((limit, sum(connection.selections)))
        patch = mock.patch.object(ProjectHistory, 'release_earlier_unarmed_leases', observed)
        patch.start()
        self.addCleanup(patch.stop)

    def stop_pool(self, pool):
        pool.close()
        for slot in pool._slots:
            slot.thread.join(6)
            self.assertFalse(slot.thread.is_alive())

    def stop(self):
        self.lane._jobs.clear()  # fixture-only live jobs never owned workers
        self.lane.close()
        self.lane._writer.join(6)
        self.assertFalse(self.lane._writer.is_alive())

    def step(self):
        previous = self.driver._expiry
        RunDriver._expire_results(self.driver)
        request = self.driver._expiry
        if request is not None and request is not previous:
            until = time.monotonic() + 10
            while request.state == 'queued' and time.monotonic() < until:
                time.sleep(0.001)
            self.assertEqual('done', request.state, request.reason)
        return request

    def held(self):
        path = self.base.fixture.directory / 'history.sqlite3'
        with contextlib.closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as reader:
            return reader.execute("SELECT next_check_ms FROM project_holds WHERE kind='lease' AND state='held' "
                                  "ORDER BY next_check_ms").fetchall()

    @requirements('S13B-NEXT', 'S13B-BOUND', why_not_scenario=WHY)
    def test_full_pass_queues_one_request_on_next_step_and_selects_at_most_64_per_call(self):
        self.start(130)
        self.step()
        self.assertEqual(66, len(self.held()))
        self.step()  # same clock: no ten-second wait
        self.assertEqual(2, len(self.held()))
        self.step()
        self.assertEqual([], self.held())
        self.assertEqual([(64, 64), (64, 64), (64, 2)], self.passes)
        self.step()
        self.assertEqual(3, len(self.passes))

    @requirements('S13B-IDLE', why_not_scenario=WHY)
    def test_non_full_pass_without_backoff_keeps_ten_second_cadence(self):
        self.start(3)
        self.step()
        self.assertEqual([], self.held())
        for at in (0, 0.5, 9.999):
            self.now = at
            self.step()
        self.assertEqual(1, len(self.passes))
        self.now = 10
        self.step()
        self.assertEqual(2, len(self.passes))

    @requirements('S13B-BACKOFF', why_not_scenario=WHY)
    def test_non_full_backed_off_pass_continues_then_empty_pass_stops_busy_loop(self):
        self.start(3, blocked=3)
        self.step()
        self.assertEqual([(2000,)] * 3, self.held())
        self.step()
        self.assertEqual([(64, 3), (64, 0)], self.passes)
        for at in (0, 0.5, 9.999):
            self.now = at
            self.step()
        self.assertEqual(2, len(self.passes))
        self.assertEqual([(2000,)] * 3, self.held())
        self.base.fixture.now = 2000
        self.now = 10
        self.step()
        self.assertEqual([(4000,)] * 3, self.held())

    @requirements('S13B-BACKOFF', 'S13B-BOUND', why_not_scenario=WHY)
    def test_full_blocked_pass_reaches_eligible_rows_then_waits_for_backoff(self):
        self.start(65, blocked=64)
        self.step()
        self.step()
        self.assertEqual([(64, 64), (64, 1)], self.passes)
        self.assertEqual([(2000,)] * 64, self.held())
        for _ in range(20):
            self.step()
        self.assertEqual(2, len(self.passes))

    @requirements('S13B-IDLE', why_not_scenario=WHY)
    def test_queued_or_failed_request_never_triggers_immediate_retry(self):
        self.start(0)
        request = SimpleNamespace(state='queued', lease_pass_pending=True)
        self.driver._expiry, self.driver._next_expiry = request, 10
        self.assertIs(request, self.step())
        self.assertEqual([], self.passes)
        request.state = 'failed'
        self.step()
        self.assertEqual([], self.passes)
        self.now = 10
        self.step()
        self.assertEqual(1, len(self.passes))

    @requirements('S13B-IDLE', why_not_scenario=WHY)
    def test_full_selection_that_cannot_back_rows_off_waits_instead_of_spinning(self):
        self.start(64, blocked=64)
        with mock.patch.object(ProjectHistory, '_defer_unarmed_lease',
                               side_effect=HistoryError('history_incompatible')):
            self.step()
            self.assertEqual(64, len(self.held()))
            self.step()
            self.assertEqual(1, len(self.passes))
