"""Actual fixed project writer settlement; no process ticket or provider launch."""
from copy import copy
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_claim_denial_recovery as denial_fixtures
from tests.test_bridge import PARENT_GRANT, RESULT_SCHEMA

from codex_agents import claim_denial
from codex_agents.bridge_liveness import BridgeLivenessOwner
from codex_agents.codex_worker import CodexWorkerError, CodexWorkerLimits, CodexWorkerPool
from codex_agents.control_loop import ControlLoop, ControlLoopLimits
from codex_agents.control_service import ControlService
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.liveness_clock import LivenessLimits
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.project_history import ProjectHistory
from codex_agents.project_monitor import ProjectMonitor, SupervisorScope
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceLane


class ResultDenialLaneTests(unittest.TestCase):
    def setUp(self):
        self.account = denial_fixtures.ClaimDenialRecoveryTests(methodName='runTest')
        self.account.setUp()
        self.addCleanup(self.account.doCleanups)
        fixture = self.account.fixture
        fixture.store.close()
        scope = fixture.scope
        native = {n: getattr(scope, n) for n in ('project_id', 'primary_session_id', 'runtime_digest',
                                               'supervisor_instance_id', 'supervisor_generation')}
        monitor = ProjectMonitor(SupervisorScope(**native), LivenessLimits(10_000_000_000, 2_000_000_000,
                                  4_000_000_000, 8_000_000_000), max_tracked_attempts=2, clock_ns=time.monotonic_ns)
        self.monitor = monitor
        self.service = ControlService(monitor, clock_ns=time.monotonic_ns)
        self.loop = ControlLoop(monitor, self.service, ControlLoopLimits(2, 2, 10_000_000_000, 1_000_000))
        self.owner = BridgeLivenessOwner(self.loop)
        self.registry = OwnedExecutionRegistry()
        self.pool = CodexWorkerPool(self.registry, CodexWorkerLimits(1, 16 * 1_048_576))
        self.lane = ResultPersistenceLane(self.owner, self.pool, fixture.open_history, max_entries=1,
                                         retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES)
        self.addCleanup(self.stop)
        bridge = fixture.base.fixture.fixture
        root = Path(bridge.temporary.name) / 'unlaunched-worker'
        root.mkdir(mode=0o700)
        for name in ('export', 'control'):
            (root / name).mkdir(mode=0o700)
        self.marker = root / 'unexpected-launch'
        executable = root / 'inert-exec'
        executable.write_text('#!/bin/sh\ntouch "' + str(self.marker) + '"\nexit 99\n')
        executable.chmod(0o700)
        payload = json.dumps({'task': bridge.task(), 'route': fixture.base.fixture.route, 'assignment': fixture.assigned,
                              'parent_grant': PARENT_GRANT, 'schema': RESULT_SCHEMA.decode(),
                              'export': str(root / 'export'), 'control': str(root / 'control'),
                              'executable': str(executable), 'environ': {'PATH': os.defpath},
                              'role_prompt': 'Inert denied request.', 'worker_result_path': None}).encode()
        self.worker = self.pool.prepare(payload)
        self.handle = self.lane.prepare_lease(scope, self.worker)
        self.until(lambda: self.lane.snapshot(self.handle)['state'] != 'preparing')
        self.assertEqual('lease_claim_pending', self.lane.snapshot(self.handle)['state'])
        self.account.evidence = self.lane.pending_storage_evidence(self.handle)
        self.raw = canonical_json(self.account.call())

    def until(self, predicate):
        end = time.monotonic() + 6
        while not predicate():
            self.assertLess(time.monotonic(), end, 'writer did not return')
            self.owner.run_once()
            time.sleep(.002)

    def stop(self):
        self.lane.close()
        self.lane._writer.join(6)
        self.assertFalse(self.lane._writer.is_alive())
        self.pool.close()
        for slot in self.pool._slots:
            slot.thread.join(6)
            self.assertFalse(slot.thread.is_alive())
        self.loop.close()
        self.service.close()

    def test_actual_writer_settles_then_original_owner_retires_only_unbound_input(self):
        with self.assertRaisesRegex(HistoryError, 'history_result_not_ready'):
            self.lane.retire_denied(self.handle)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.lane.settle_denial(copy(self.handle), self.raw)
        original = claim_denial.verify_denial_closure
        owner = threading.current_thread()

        def off_owner(*args):
            self.assertIsNot(owner, threading.current_thread())
            return original(*args)

        with mock.patch.object(claim_denial, 'verify_denial_closure', side_effect=off_owner):
            self.lane.settle_denial(self.handle, self.raw)
            self.until(lambda: self.lane.snapshot(self.handle)['state'] != 'denial_settling')
        self.assertEqual('denied', self.lane.snapshot(self.handle)['state'])
        self.assertEqual('expired', self.lane.lease(self.handle).state)
        self.assertEqual({}, self.monitor._attempts)
        self.assertEqual(0, len(self.registry._issued))
        self.assertFalse(self.marker.exists())
        self.lane.retire_denied(self.handle)
        self.assertEqual(0, self.pool._charged)
        with self.assertRaises(CodexWorkerError):
            self.pool.submit(self.worker)

    def test_lost_writer_reply_retains_original_closure_without_rearming_execution(self):
        original = ProjectHistory.settle_denied_result_lease
        values = []

        def lose_reply(store, binding, raw):
            value = original(store, binding, raw)
            values.append((raw, value))
            if len(values) == 1:
                raise OSError('PRIVATE-LOST-WRITER-SETTLEMENT')
            return value

        with mock.patch.object(ProjectHistory, 'settle_denied_result_lease', new=lose_reply):
            self.lane.settle_denial(self.handle, self.raw)
            self.until(lambda: self.lane.snapshot(self.handle)['state'] == 'failed')
            self.assertEqual(0, len(self.registry._issued))
            self.lane.settle_denial(self.handle)
            self.until(lambda: self.lane.snapshot(self.handle)['state'] == 'denied')
        self.assertEqual(values[0], values[1])
        self.assertFalse(self.marker.exists())
        self.lane.retire_denied(self.handle)


if __name__ == '__main__':
    unittest.main()
