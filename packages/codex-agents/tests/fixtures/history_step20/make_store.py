"""Write the real format-20 project store used by RS-B-B6-7 (B6's 20 -> 21 records-upgrade test).

Run ONCE with the step-20 runtime (base 6a1ea9da = 0.8.57), never with a later one:
    git archive 6a1ea9da packages/codex-agents/native-package | tar -x -C <dir>
    python3 -I -B make_store.py <dir>/packages/codex-agents/native-package/.codex/orchestration store.sql
The output is a plain SQL dump (PRAGMAs, schema, rows) of a store written only through that runtime's own
operations: four orchestrations (active with a checkpoint, paused, closed by its primary, ended by an update with
its RS-D N1 tombstone). Fixture key and project only (tests/test_project_orchestration.py), no owner authority.

B6 review round 1 (HIGH adoption idle window): with a third argument `--leases` it writes `store_leases.sql`, the
same four plus three with lease and reservation histories after their last checkpoint: `withdrawn` (open 3500, a
lease taken at 3600 and withdrawn unsent at 3700, no reservation), `bound` (open 3800, a lease taken at 3900, armed at
3950 and bound at 4000: its reservation inherits the lease's 3900, its register row is taken at 4000) and `quiet`
(open 4100, nothing after). Same runtime, same rule: never regenerate either file with a later runtime.
"""
import hashlib
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope  # noqa: E402
from codex_agents.project_history import ProjectHistory  # noqa: E402
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity  # noqa: E402
from codex_agents.result_contracts import ResultLeaseBinding  # noqa: E402
from codex_agents.history_contracts import AttemptBinding  # noqa: E402

LEASES = sys.argv[3:] == ['--leases']
# tests/learning_support.assignment(), copied (this script imports only the step-20 runtime)
ASSIGNMENT = {
    "assignment_id": "assignment-1", "task_id": "task-1", "objective": "Read one fixture",
    "role": "codex_agents_researcher", "model": "gpt-6.1-sol", "effort": "high", "attempt": 1, "meaningful": True,
    "owned_paths": [], "context": ["source.txt"],
    "permissions": {"tools": ["shell"], "filesystem": ["source.txt:read"], "network": False, "mcps": [],
                    "external_effects": False},
    "expected_output": "One marker", "required_evidence": [{"id": "marker", "description": "marker exists"}],
    "verification_required": True,
    "bounds": {"max_attempts": 2, "max_steers_per_attempt": 1, "max_no_progress_events": 2, "wait_seconds": 60,
               "timeout_seconds": 600, "max_tool_calls": 30},
    "stopping_conditions": ["marker found"], "prohibited_actions": ["network"],
}

PROJECT_A = 'prj_' + 'a' * 32
KEY = b'j' * 32
CLIENT = 'codex-host-v1'
clock = {'now': 1000}

with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary).resolve() / 'history'
    directory.mkdir(mode=0o700)
    store = ProjectHistory(directory, PROJECT_A, result_key=KEY,
                           result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                           result_clock_ms=lambda: clock['now'], orchestration_limits=OrchestrationLimits(8, 12))
    owner = store._orchestration_owner()
    scope = OrchestrationScope(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1)

    def call(operation, request_id, payload=None):
        request = OrchestrationRequest(1, 'orchestration.' + operation, request_id, scope, payload or {})
        return owner.apply(request, client_version=CLIENT, before_commit=lambda: None)['current']['snapshot']

    def target(snapshot, **values):
        return {'orchestration_handle': snapshot['orchestration_handle'], 'expected_sequence': snapshot['sequence'],
                **values}

    updated = call('open', 'open-updated')
    clock['now'] = 1500
    assert owner.end_earlier(replace(scope, runtime_digest='b' * 64), 10) == 1  # RS-D N1 tombstone `closed`
    clock['now'] = 2000
    active = call('open', 'open-active')
    clock['now'] = 2500
    call('checkpoint', 'checkpoint-active', target(active, goal_tokens_used=5, goal_time_used_seconds=1))
    paused = call('open', 'open-paused')
    call('pause', 'pause-paused', target(paused))
    clock['now'] = 3000
    closed = call('open', 'open-closed')
    call('close', 'close-closed', target(closed, status='completed', goal_tokens_used=None,
                                         goal_time_used_seconds=None))
    if LEASES:
        def lease(snapshot, number):
            return ResultLeaseBinding(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1, snapshot['orchestration_handle'],
                                      'hrn_' + str(number) * 48, f'run-{number}', 1, f'claim-request-{number}',
                                      str(number) * 64)
        clock['now'] = 3500
        withdrawn = call('open', 'open-withdrawn')
        clock['now'] = 3600
        store.reserve_result_lease(lease(withdrawn, 1), ASSIGNMENT)
        clock['now'] = 3700
        store.release_unsent_result_lease(lease(withdrawn, 1))
        clock['now'] = 3800
        bound = call('open', 'open-bound')
        clock['now'] = 3900
        store.reserve_result_lease(lease(bound, 2), ASSIGNMENT)
        clock['now'] = 3950
        store.arm_result_lease(lease(bound, 2))
        clock['now'] = 4000
        attempt = AttemptBinding(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1, bound['orchestration_handle'],
                                 'hrn_' + '2' * 48, 'run-2', 1, 'claim-2', hashlib.sha256(b'claim-2').hexdigest())
        store.bind_result_lease(lease(bound, 2), attempt, ASSIGNMENT, hard_deadline_at_ms=34_000)
        clock['now'] = 4100
        call('open', 'open-quiet')
    connection = store._connection
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    application = connection.execute('PRAGMA application_id').fetchone()[0]
    assert version == 20, version
    lines = [f'PRAGMA application_id={application};', f'PRAGMA user_version={version};',
             *connection.iterdump()]
    store.close()
Path(sys.argv[2]).write_text('\n'.join(lines) + '\n')
