"""Verifier preparation reads one original actual project result through APIs."""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest

from tests import test_project_open_scope as fixtures
from tests.test_bridge import CLEAN

from codex_agents import codex_worker
from codex_agents.codex_preparation import (MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES,
    CodexPreparationLane, PreparationContext, PreparationLimits, preparation_payload)
from codex_agents.codex_worker import CodexWorkerLimits, CodexWorkerPool
from codex_agents.history_api import history_request_digest
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_result_contracts import WorkerResultContext
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.result_read_api import result_read_request_digest
from codex_agents.result_read_service import ResultReadAuthorization, ResultReadService


class CodexVerifierPreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectOpenScopeTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.prospective = self.fixture.store, self.fixture.context
        self.scope, self.worker = self.prospective.scope, self.prospective.worker
        self.token, self.allowed = object(), True
        self.operations, self.peers = [], []
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.endpoint = Path(self.temp.name)/'v.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.endpoint))
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.addCleanup(self.listener.close)
        self.pool = CodexWorkerPool(OwnedExecutionRegistry(), CodexWorkerLimits(1, 16*1_048_576))
        self.context = PreparationContext(preparation_payload(self.prospective, self.fixture.bridge.project,
            self.fixture.bridge.bundle, CLEAN, history_endpoint=self.endpoint))
        self.lane = CodexPreparationLane(self.scope, self.pool,
            PreparationLimits(1, MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES),
            authorizer=lambda context: context is self.context)
        self.addCleanup(self.stop)
        def authorize_history(request, context):
            if self.allowed and context is self.token:
                return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request),
                    'host', frozenset({'history.worker_reference', 'history.read_record'}),
                    worker_result_context=WorkerResultContext(self.worker.binding, self.prospective.task))
        def authorize_result(request, context):
            if self.allowed and context is self.token and request.binding == self.worker.binding:
                return ResultReadAuthorization(request.binding, request.result_digest, result_read_request_digest(request))
        self.history = HistoryService(self.scope.project_id, self.scope.runtime_digest, lambda: self.store,
                                      authorizer=authorize_history, close_history=False)
        self.result = ResultReadService(self.scope.project_id, self.scope.runtime_digest, lambda: self.store,
                                        authorizer=authorize_result, close_history=False)
        self.addCleanup(self.history.close)
        self.addCleanup(self.result.close)

    def stop(self):
        self.lane.request_close()
        self.pool.close()
        end = time.monotonic()+8
        while not self.lane.threads_stopped() or any(s.thread.is_alive() for s in self.pool._slots):
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        for connection, _ in self.peers:
            connection.close()

    def pump(self):
        try:
            connection, _ = self.listener.accept()
            connection.setblocking(False)
            self.peers.append((connection, bytearray()))
        except BlockingIOError:
            pass
        for connection, incoming in list(self.peers):
            try:
                incoming.extend(connection.recv(16_384))
            except BlockingIOError:
                continue
            if b'\n' not in incoming:
                continue
            raw = bytes(incoming).split(b'\n')[0]
            operation = json.loads(raw)['operation']
            self.operations.append(operation)
            service = self.history if operation.startswith('history.') else self.result
            reply = service.handle(raw, self.token)
            connection.settimeout(1)
            connection.sendall(reply+b'\n')
            connection.close()
            self.peers.remove((connection, incoming))

    def until(self, predicate):
        end = time.monotonic()+10
        while not predicate():
            self.pump()
            self.lane.drive()
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)

    def test_original_bound_worker_body_is_placed_without_new_host_run_or_rewriting_original_result(self):
        expected = json.loads(self.store.read_result(self.worker.binding))
        reservation = self.store.result_reservation(self.worker.binding)
        counts = {name: self.store._connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                  for name in ('records', 'result_reservations', 'terminal_outbox', 'result_leases')}
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        self.addCleanup(self.store._connection.set_trace_callback, None)
        handle = self.lane.submit(self.context)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'ready', 'failed', 'unknown'})
        self.assertEqual('ready', self.lane.snapshot(handle)['state'])
        prepared = self.lane.prepared(handle)
        payload, assignment = codex_worker._decode_input(self.pool._job(prepared.worker).payload)
        self.assertEqual(self.prospective.assignment, assignment)
        actual = prepared.workspace/'export'/payload['worker_result_path']
        self.assertEqual(expected, json.loads(actual.read_bytes()))
        self.assertEqual(['history.worker_reference', 'history.read_record', 'result.read_chunk'], self.operations)
        mutations = [sql for sql in statements if sql.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE '))]
        self.assertTrue(mutations)
        self.assertTrue(all(sql.startswith('UPDATE result_identity SET last_observed_at_ms=') for sql in mutations))
        self.assertEqual(reservation, self.store.result_reservation(self.worker.binding))
        self.assertEqual(counts, {name: self.store._connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                                 for name in counts})
        self.assertEqual({}, self.pool._registry._issued)
        self.lane.discard(handle)
        self.until(lambda: self.lane.snapshot(handle)['state'] == 'cleaned')
        self.lane.retire(handle)
        self.assertFalse(prepared.workspace.exists())

    def test_revoked_original_result_read_cannot_create_export_or_worker(self):
        self.allowed = False
        handle = self.lane.submit(self.context)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'ready', 'failed', 'unknown'})
        self.assertEqual('failed', self.lane.snapshot(handle)['state'])
        self.assertEqual(['history.worker_reference'], self.operations)
        self.assertEqual({}, self.pool._jobs)
        self.assertEqual({}, self.pool._registry._issued)
        self.lane.retire(handle)
