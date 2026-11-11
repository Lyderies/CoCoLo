"""Original runtime startup and shutdown; no native enrollment or provider calls."""
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

import tests.support  # noqa: F401
from tests.test_project_results import KEY
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.control_api import ControlError, ControlScope, control_request, decode_control_response, encode_control_request
from codex_agents.control_loop import ControlLoopLimits
from codex_agents.codex_worker import CodexWorkerLimits
from codex_agents.history_contracts import HistoryError
from codex_agents.liveness_clock import LivenessLimits
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationScope
from codex_agents.project_history import ProjectHistory
from codex_agents.project_ingress import ProjectIngressLimits
from codex_agents.project_request_queue import ProjectRequestLimits
from codex_agents.project_runtime import ProjectRuntime, ProjectRuntimeLimits
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES
from codex_agents.service_protocol import ServiceTarget

SECOND = 1_000_000_000


def limits():
    requests = ProjectRequestLimits(4, 10*SECOND)
    return ProjectRuntimeLimits(4, 2, 2*MAX_PERSISTENCE_ENTRY_BYTES+requests.retained_bytes,
        LivenessLimits(30*SECOND, 2*SECOND, 5*SECOND, 20*SECOND),
        ControlLoopLimits(4, 4, 10*SECOND, 1_000_000), CodexWorkerLimits(1, 16*1_048_576),
        requests, ProjectIngressLimits(4, 4, 10*SECOND, 1_000_000))


class ProjectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.scope = OrchestrationScope('prj_'+'a'*32, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
        self.calls = []
        self.runtimes = []
        self.addCleanup(self.stop_all)

    def factory(self):
        self.calls.append(threading.current_thread())
        return ProjectHistory(self.directory, self.scope.project_id, result_key=KEY,
            orchestration_limits=OrchestrationLimits(2, 32),
            result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))

    def start(self, **changes):
        options = {'scope': self.scope, 'open_history': self.factory, 'limits': limits()}
        options.update(changes)
        runtime = ProjectRuntime(**options)
        self.runtimes.append(runtime)
        return runtime

    def stop_all(self):
        for runtime in self.runtimes:
            runtime.request_close()
        end = time.monotonic()+6
        while not all(runtime.threads_stopped() for runtime in self.runtimes):
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)

    def test_startup_owns_one_writer_before_serving_and_all_components_share_originals(self):
        runtime = self.start()
        self.assertEqual([runtime.persistence._writer], self.calls)
        self.assertIsNot(self.calls[0], threading.current_thread())
        self.assertIs(runtime.owner._loop, runtime.loop)
        self.assertIs(runtime.persistence._owner, runtime.owner)
        self.assertIs(runtime.persistence._pool, runtime.pool)
        self.assertIs(runtime.ingress._lane, runtime.persistence)
        self.assertEqual({}, runtime.pool._registry._issued)
        runtime.run_once()
        runtime.request_close()
        runtime.request_close()
        self.stop_all()
        with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
            runtime.run_once()

    def test_second_original_writer_is_refused_during_startup(self):
        first = self.start()
        with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
            self.start()
        self.assertEqual(2, len(self.calls))
        self.assertTrue(first.persistence._writer.is_alive())
        self.assertEqual({}, first.pool._registry._issued)
        first.run_once()

    def test_changed_runtime_or_unbounded_budget_refuses_before_factory(self):
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            self.start(scope=replace(self.scope, runtime_digest='f'*64))
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            replace(limits(), persistence_bytes=1)
        self.assertEqual([], self.calls)

    def test_default_control_denial_is_preserved_after_owner_startup(self):
        runtime = self.start()
        server, client = socket.socketpair()
        self.addCleanup(client.close)
        runtime.attach_control(server, {'authorized': True})
        request = control_request(ControlScope(**self.scope.as_data()), 'project.status',
                                  ServiceTarget('orc_'+'a'*48, None, None))
        client.sendall(encode_control_request(request)+b'\n')
        client.setblocking(False)
        raw = bytearray()
        end = time.monotonic()+2
        while b'\n' not in raw:
            runtime.run_once()
            try:
                raw.extend(client.recv(16_384))
            except BlockingIOError:
                pass
            self.assertLess(time.monotonic(), end)
        with self.assertRaisesRegex(ControlError, 'control_not_authorized'):
            decode_control_response(bytes(raw[:-1]), request)
        self.assertEqual([runtime.persistence._writer], self.calls)
        self.assertEqual({}, runtime.pool._registry._issued)
