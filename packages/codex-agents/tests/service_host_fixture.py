"""The product service host in a disposable process.

Listeners, enrollment, routing hints and the owner loop are the real host. Only
the history parameters, limits and scope arrive as test input on stdin.
"""
import json
from pathlib import Path
import select
import sys
import time

import tests.support  # noqa: F401
from tests.test_project_runtime import limits
from codex_agents.collector_paths import CollectorPaths
from codex_agents.history_contracts import HistoryError
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationScope
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.service_host import ProjectServiceHost, ServiceHostLimits

SECOND = 1_000_000_000


def emit(**data):
    print(json.dumps(data, separators=(',', ':')), flush=True)


def main():
    data = json.loads(sys.stdin.buffer.readline(65_537))
    scope = OrchestrationScope(**data['scope'])
    state, key = CollectorPaths(Path(data['state'])), bytes.fromhex(data['fixture_key'])

    def open_history():
        return ProjectHistory(state.root/data.get('history', 'history'), scope.project_id, result_key=key,
            orchestration_limits=OrchestrationLimits(2, 32),
            result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))

    try:
        host = ProjectServiceHost(state, Path(data['project']), scope, open_history, limits(),
            ServiceHostLimits(data.get('pending', 4), data.get('enrollment_timeout_ms', 500)*1_000_000, 16, 30*SECOND,
                              data.get('window_ms', 30_000), 64))
    except HistoryError as error:
        emit(stage='refused', reason_code=error.reason_code)
        return
    try:
        emit(stage='ready', **host.endpoints())
        while True:
            host.serve_once()
            if select.select([sys.stdin], [], [], 0)[0]:
                if json.loads(sys.stdin.buffer.readline(1025)) != {'stop': True}:
                    raise RuntimeError('fixture_invalid_stop')
                break
            time.sleep(.002)
    finally:
        host.close()
        end = time.monotonic()+6
        while not host.threads_stopped():
            if time.monotonic() >= end:
                raise RuntimeError('fixture_host_threads_not_stopped')
            time.sleep(.002)
    emit(stage='stopped', threads_stopped=host.threads_stopped())


if __name__ == '__main__':
    main()
