"""Build S6's mixed store with the archived schema-18 runtime, never SQL edits.

Synthetic enrollment and claim identities use the historical test fixtures.
All source rows, context files, opens and results use historical business methods.
Executed explicitly by test_rsf_s6_classify; no persistence at import time.
"""
import json
from pathlib import Path
import shutil
import sys
from dataclasses import replace


def build(archive, destination):
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from tests import test_record_context as rc
    from codex_agents.history_contracts import AttemptBinding
    from codex_agents.learning_contracts import canonical_json, external_id
    from codex_agents.host_open_contracts import OpenProvenanceInput
    from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
    from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS

    fixture = rc.RecordedContextFixture(methodName='runTest')
    fixture.setUp()
    try:
        fixture.open_and_consume()
        fixture.publish()
        store, source = fixture.store, fixture.fixture
        store._result_capacity = ResultCapacity(256, 256 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * rc.DAY_MS)
        store._allow_legacy_orchestration_reference = True
        original = source.scope
        task, route = fixture.task, fixture.route
        def original_open(label, context):
            opened = source.ledger.open_host_run(source.project, assignment=fixture.assigned, task=task, route=route,
                execution_configuration=route['execution_configuration'], client_version='bridge-v1',
                orchestration_handle=original.orchestration_handle, now_ms=source.base.stamp)
            handle = opened['run_handle']
            scope = replace(original, run_handle=handle, run_id=external_id(source.key, 'run', handle),
                            claim_request_id='s6-' + label, claim_request_digest='3' * 64)
            store.reserve_result_lease(scope, fixture.assigned, **({'record_context': context} if context else {}))
            store.arm_result_lease(scope)
            request = OpenProvenanceInput(handle, fixture.assigned['assignment_id'])
            proof = source.ledger._attest_open_provenance(source.project, runtime_digest=scope.runtime_digest,
                caller_scope='a' * 64, request=request, storage_binding=scope,
                storage_evidence=store.attest_result_lease(scope), work_limits=fixture.base.limits)
            store.record_host_open(scope, request, proof, assignment=fixture.assigned, task=task, route=route,
                                   before_commit=lambda: None)
            return scope
        # Real original host open, synthetic claim identity, no process or planted closure.
        running_scope = original_open('running', fixture.record_context)
        running = AttemptBinding(**{name: getattr(running_scope, name) for name in SCOPE_FIELDS +
            ('orchestration_handle', 'run_handle', 'run_id', 'attempt')},
            claim_id='brg_' + '2' * 32, claim_digest='2' * 64)
        store.bind_result_lease(running_scope, running, fixture.assigned,
                               hard_deadline_at_ms=store.result_reservation(fixture.binding).hard_deadline_at_ms)
        # Pre-B3 open whose withdrawal inputs lived only in predecessor memory.
        open_scope = original_open('pending', None)
        # Preparations retain only their reserved lease; their inputs existed in memory.
        preparations = []
        for n in range(70):
            handle = 'hrn_' + f'{100 + n:048x}'
            scope = replace(original, run_handle=handle, run_id=external_id(source.key, 'run', handle),
                            claim_request_id=f's6-prep-{n}', claim_request_digest=f'{100 + n:064x}')
            store.reserve_result_lease(scope, fixture.assigned)
            preparations.append(scope.as_data())
        # Real paused and closed sealed orchestrations share the same project.
        store._orchestration_limits = OrchestrationLimits(8, 8)
        owner = store._orchestration_owner()
        scope = OrchestrationScope(**{name: getattr(original, name) for name in SCOPE_FIELDS})
        paused = owner.apply(OrchestrationRequest(1, 'orchestration.open', 's6-pause-open', scope, {}),
                             client_version=rc.CODEX, before_commit=lambda: None)
        closed = owner.apply(OrchestrationRequest(1, 'orchestration.open', 's6-close-open', scope, {}),
                             client_version=rc.CODEX, before_commit=lambda: None)
        for reply, operation, extra in ((paused, 'pause', {}), (closed, 'close',
                {'status': 'completed', 'goal_tokens_used': None, 'goal_time_used_seconds': None})):
            owner.apply(OrchestrationRequest(1, 'orchestration.' + operation, 's6-' + operation, scope,
                {'orchestration_handle': reply['current']['snapshot']['orchestration_handle'], 'expected_sequence': reply['current']['snapshot']['sequence'],
                 **extra}), client_version=rc.CODEX, before_commit=lambda: None)
        manifest = dict(project=source.project, key=source.key.hex(), now=source.base.stamp,
            finished=fixture.binding.as_data(), running=running.as_data(), running_scope=running_scope.as_data(),
            pending=open_scope.as_data(), preparations=preparations, assignment=fixture.assigned,
            body=fixture.body.decode(), paused=paused['current']['snapshot']['orchestration_handle'], closed=closed['current']['snapshot']['orchestration_handle'])
        store.close()
        shutil.copytree(source.directory, destination / 'history')
        (destination / 'manifest.json').write_bytes(canonical_json(manifest))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(sys.argv[2]))
