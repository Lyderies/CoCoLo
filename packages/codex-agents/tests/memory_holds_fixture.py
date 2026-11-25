"""Executable memory-hold checkpoints; nothing starts at import."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock


def main(stage):
    if stage in {'handoff','detach'}:
        return handoff(stage)
    from tests.test_codex_preparation import CodexPreparationTests
    from tests.test_memory_holds import DeferredLane
    from tests.test_b6_unattended_close import KEY, CLIENT
    from codex_agents import memory_holds as mh, project_driver, project_holds_schema as schema
    from codex_agents.launch_journal import LaunchJournal
    from codex_agents.project_history import ProjectHistory
    from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest
    from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
    fixture = CodexPreparationTests(methodName='runTest')
    fixture.setUp()
    directory = fixture.fixture.state.root / 'memory-store'
    directory.mkdir(mode=0o700)
    store = ProjectHistory(directory, fixture.scope.project_id, result_key=KEY,
        result_clock_ms=lambda: 1000, orchestration_limits=OrchestrationLimits(8, 12),
        result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000))
    opened = store._orchestration_owner().apply(OrchestrationRequest(1, 'orchestration.open', 'open',
        fixture.scope, {}), client_version=CLIENT, before_commit=lambda: None)['current']['snapshot']
    handle = opened['orchestration_handle']
    key = ('hrn_' + 'b' * 48, 1)
    journal = LaunchJournal(directory.parent / 'launches')
    journal.keep_lock = True
    def checkpoint():
        print(json.dumps({'directory': str(directory), 'project': store.project_id, 'instance': journal.instance,
                          'key': key, 'stage': stage}), flush=True)
        sys.stdin.read()
        raise RuntimeError('checkpoint must be killed')
    if stage == 'hung':
        mh.register(store, handle, *key, 'run', journal.instance, launches=journal.directory)
        store.close()
        checkpoint()
    if stage == 'registration_transaction':
        original = schema.take_hold
        def hold(*args, **kwargs):
            original(*args, **kwargs)
            if args[2] == 'driver_entry':
                checkpoint()
        with mock.patch.object(schema, 'take_hold', hold):
            mh.register(store, handle, *key, 'run', journal.instance, launches=journal.directory)
    mh.register(store, handle, *key, 'run', journal.instance, launches=journal.directory)
    if stage == 'registration_commit':
        checkpoint()
    mh.take_preparation(store, *key, journal.instance)
    if stage == 'before_submit':
        checkpoint()
    prepared, inputs = fixture.prepare()
    driver = object.__new__(project_driver.RunDriver)
    driver._runtime = SimpleNamespace(persistence=DeferredLane(store))
    driver._journal, driver._preparation = journal, fixture.lane
    driver._giving_back, driver._cleanup_retry, driver._retired = [], {}, {}
    driver._clock = lambda: 1000
    run = project_driver._Run(key, fixture.scope, CLIENT, SimpleNamespace(orchestration_handle=handle), 'run', 'd'*64)
    run.state, run.prepared, run.prepared_inputs = 'failed', prepared, inputs
    run.reporting = SimpleNamespace(state='done')
    driver._runs = {key: run}
    driver._memory = mh.DriverMemory(driver)
    driver._memory.preparations[prepared] = (key, 'retired')
    if stage == 'discard':
        import threading
        from codex_agents import codex_preparation
        entered, resume = threading.Event(), threading.Event()
        original_remove = codex_preparation._remove
        def blocked_remove(workspace):
            entered.set()
            resume.wait(20)
            return original_remove(workspace)
        codex_preparation._remove = blocked_remove
    driver._give_back(run)
    if stage == 'discard':
        fixture.until(entered.is_set)
        assert fixture.lane.snapshot(prepared)['state'] == 'cleanup'
        checkpoint()  # The actual cleanup worker cannot yet mark this slot cleaned.
    fixture.until(lambda: fixture.lane.snapshot(prepared)['state'] == 'cleaned')
    fixture.lane.retire(prepared)
    if stage == 'entry_delete':
        driver._memory.retired_preparation(prepared)
        driver._runtime.persistence.flush()
        driver._memory.step()
        run.prepared = None
        driver._memory.drop(run)
    checkpoint()


def handoff(stage):
    from tests.test_memory_holds_lane import MemoryLaneTests
    from codex_agents import memory_holds, project_driver
    import time
    fixture = MemoryLaneTests(methodName='runTest')
    fixture.setUp()
    run, lane = fixture.armed()
    run.state, run.claim_sent = 'failed', True
    def checkpoint():
        print(json.dumps({'directory':str(fixture.directory),'project':fixture.scope.project_id,
            'instance':fixture.driver._journal.instance,'key':fixture.key,'stage':stage,
            'now_ms':time.time_ns()//1_000_000,'state_root':str(fixture.fixture.state.root),
            'project_root':str(fixture.fixture.project),'bundle_root':str(fixture.fixture.bundle)}),flush=True)
        sys.stdin.read()
        raise RuntimeError('checkpoint must be killed')
    if stage == 'handoff':
        original = memory_holds.hand_over
        def operation(*args):
            result = original(*args)
            checkpoint()  # committed, before the writer publishes its answer
            return result
        memory_holds.hand_over = operation
    else:
        original = lane.detach
        def detach(handle):
            original(handle)
            checkpoint()  # the actual pool worker and lane entry are now detached
        lane.detach = detach
    for _ in range(project_driver.MAX_FENCE_ATTEMPTS):
        fixture.driver._fence_later(run)
    fixture.until(lambda: False)


if __name__ == '__main__':
    main(sys.argv[1])
