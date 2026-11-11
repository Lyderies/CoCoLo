"""Product runtime in a disposable process; explicitly synthetic native frontend."""
from dataclasses import asdict
import json
from pathlib import Path
import select
import socket
import sys
import threading
import time

import tests.support  # noqa: F401
from tests.test_project_runtime import limits
from codex_agents.orchestration_api import ORCHESTRATION_OPERATIONS, OrchestrationLimits, OrchestrationScope, orchestration_request_digest
from codex_agents.orchestration_service import OrchestrationAuthorization
from codex_agents.project_history import ProjectHistory
from codex_agents.project_runtime import ProjectRuntime
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

CONTEXT = object()  # Test boundary only; no native proof from this marker.


def emit(**data):
    print(json.dumps(data, separators=(',', ':')), flush=True)


def main():
    data = json.loads(sys.stdin.buffer.readline(65_537))
    scope = OrchestrationScope(**data['scope'])
    root, key = Path(data['root']), bytes.fromhex(data['fixture_key'])
    calls, dropped = [], []
    def open_history():
        calls.append(threading.current_thread().name)
        return ProjectHistory(root/'history', scope.project_id, result_key=key,
            orchestration_limits=OrchestrationLimits(2, 32),
            result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))
    def authorize(request, context):
        if context is CONTEXT:
            return OrchestrationAuthorization(scope, orchestration_request_digest(request),
                data['client_version'], ORCHESTRATION_OPERATIONS)
    def ingress_authorize(raw, context, phase):
        if context is not CONTEXT:
            return False
        command = json.loads(raw)
        if (phase == 'publication' and not dropped and
                (command['request_id'] == data.get('drop_identity') or
                 (data.get('drop_first_open', False) and command['operation'] == 'orchestration.open'))):
            dropped.append(True)
            return False  # Actual owner commit precedes this publication revocation.
        return True
    allowed = data.get('authorized', True)
    runtime = ProjectRuntime(scope, open_history, limits(),
        orchestration_authorizer=authorize if allowed else None,
        ingress_authorizer=ingress_authorize if allowed else None)
    listeners = []
    try:
        for name, kind in (('d.sock', 'control'), ('p.sock', 'project')):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(root/name))
            (root/name).chmod(0o600)
            listener.listen(4)
            listener.setblocking(False)
            listeners.append((listener, kind))
        emit(stage='ready', scope=asdict(scope), dispatch_endpoint=str(root/'d.sock'), result_endpoint=str(root/'p.sock'),
            startup_store_opens=len(calls), tickets=len(runtime.pool._registry._issued))
        while True:
            for listener, kind in listeners:
                try:
                    connection, _ = listener.accept()
                except BlockingIOError:
                    continue
                if kind == 'control':
                    runtime.attach_control(connection, CONTEXT)
                else:
                    runtime.attach_project(connection, CONTEXT)
            runtime.run_once()
            if select.select([sys.stdin], [], [], 0)[0]:
                if json.loads(sys.stdin.buffer.readline(1025)) != {'stop': True}:
                    raise RuntimeError('fixture_invalid_stop')
                break
            time.sleep(.002)
    finally:
        for listener, _ in listeners:
            listener.close()
        runtime.request_close()
        end = time.monotonic()+6
        while not runtime.threads_stopped():
            if time.monotonic() >= end:
                raise RuntimeError('fixture_runtime_threads_not_stopped')
            time.sleep(.002)
    emit(stage='stopped', store_opens=len(calls), tickets=len(runtime.pool._registry._issued),
         threads_stopped=runtime.threads_stopped(), reply_revoked=bool(dropped))


if __name__ == '__main__':
    main()
