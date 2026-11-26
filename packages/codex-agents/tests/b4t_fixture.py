"""Build B4t's source with unmodified archived business methods.

Capture the real pending storage attestation immediately before the archived
bind, just as its driver does. This fixture never rewrites product rows.
"""
import json
from pathlib import Path
import sys
import shutil


def build(archive, destination):
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from codex_agents.project_history import ProjectHistory
    from codex_agents.learning_contracts import canonical_json
    # This driver file belongs to the current tests; all business methods and
    # supporting fixtures imported by build belong to the archived runtime.
    import rsf_s6_fixture
    attestations = {}
    original = ProjectHistory.bind_result_lease

    def bind(owner, scope, *args, **kwargs):
        attestations[scope.run_handle] = owner.attest_result_lease(scope).hex()
        return original(owner, scope, *args, **kwargs)

    ProjectHistory.bind_result_lease = bind
    try:
        rsf_s6_fixture.build(archive, destination)
    finally:
        ProjectHistory.bind_result_lease = original
    path = destination / 'manifest.json'
    data = json.loads(path.read_bytes())
    data['running_storage_evidence'] = attestations[data['running']['run_handle']]
    path.write_bytes(canonical_json(data))


def stop_result(archive, destination):
    """A consumed archived run, deliberately not published until service stop."""
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    from tests.test_record_context import RecordedContextFixture
    from tests.test_bridge import result
    from codex_agents.learning_contracts import canonical_json
    case = RecordedContextFixture(methodName='runTest')
    case.setUp()
    try:
        case.open_and_consume()
        data = dict(project=case.fixture.project,key=case.fixture.key.hex(),now=case.fixture.base.stamp,
                    finished=case.binding.as_data(),running=case.binding.as_data(),
                    assignment=case.assigned,body=canonical_json(result()).decode())
        case.store.close()
        shutil.copytree(case.directory,destination/'history')
        (destination/'manifest.json').write_bytes(canonical_json(data))
    finally:
        case.doCleanups()


def account(archive, destination):
    """A real old claim and terminal, with native enrollment supplied by its fixture."""
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from tests.test_account_terminal_delivery import AccountTerminalDeliveryTests
    from codex_agents.learning_contracts import canonical_json
    case = AccountTerminalDeliveryTests(methodName='runTest')
    case.setUp()
    try:
        data = dict(expected=case.expected.as_data(), binding=case.binding.as_data(),
            pending=case.fixture.evidence.hex(), bound=case.bound.hex(), terminal=case.proof.hex(),
            submission=case.submission.payload(), identity_digest=case.base.registration.identity_digest,
            now=case.fixture.base.stamp, clock_reference_ns=case.grant.clock_reference_ns,
            caller_scope=case.caller_scope)
        state = case.ledger.state.root
        case.ledger.close()
        shutil.copytree(state, destination / 'state')
        (destination / 'manifest.json').write_bytes(canonical_json(data))
    finally:
        case.doCleanups()


def migrations(archive, destination):
    """Use the archive's own S3 fixtures, with its original 17/22 targets."""
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from tests.test_rsf_s3_resumable import ResumableMigrationTests
    case = ResumableMigrationTests(methodName='runTest')
    case.setUp()
    try:
        for kind, make in (('account', case.account_fixture), ('project', case.project_fixture)):
            shutil.copytree(make(), destination / kind)
    finally:
        case.doCleanups()


def chain(archive, destination):
    """Two authentic 0.8.66 claims and immutable releasing project outboxes."""
    from dataclasses import replace
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from tests.test_account_claim_storage import AccountClaimStorageTests
    from codex_agents.account_api import account_request_digest
    from codex_agents.account_claim_contracts import LaunchClaimInput
    from codex_agents.history_contracts import AttemptBinding
    from codex_agents.result_contracts import LEASE_SCOPE_FIELDS
    from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
    from codex_agents.launch_journal import LaunchJournal
    from codex_agents.lost_run_contracts import SettleLostInput
    case = AccountClaimStorageTests(methodName='runTest')
    case.setUp()
    journal = LaunchJournal(case.directory / 'launches')
    try:
        runs = []
        for n in range(2):
            if n:
                source = case.base.fixture
                opened = case.ledger.open_host_run(case.project, assignment=case.assigned,
                    task=source.fixture.task(), route=source.route,
                    execution_configuration=source.route['execution_configuration'], client_version='bridge-v1',
                    orchestration_handle=case.orchestration['orchestration_handle'], now_ms=case.base.stamp)
                case.base.handle = opened['run_handle']
                run_id = case.ledger._connection.execute('SELECT run_id FROM host_run_bindings WHERE handle_digest=?',
                    (keyed_digest(case.key, case.base.handle),)).fetchone()[0]
                case.base.launch = LaunchClaimInput(case.base.handle, content_digest(case.base.observe()[0]))
                case.request = case.base.request('second-storage-claim')
                case.scope = replace(case.scope, run_handle=case.base.handle, run_id=run_id,
                    claim_request_id=case.request.request_id, claim_request_digest=account_request_digest(case.request))
                case.expected = case.scope
            case.arm()
            pending = case.evidence
            reply = case.call()
            binding = AttemptBinding(**{name: getattr(case.scope, name) for name in LEASE_SCOPE_FIELDS},
                                     claim_id=reply['bridge_run_id'], claim_digest=reply['receipt_digest'])
            case.store.bind_result_lease(case.scope, binding, case.assigned, hard_deadline_at_ms=reply['deadline_at_ms'])
            identity = case.base.launch.identity_digest
            journal.claimed(binding.claim_id, run_handle=binding.run_handle, attempt=binding.attempt,
                host_client='bridge-v1', lease=case.scope.as_data(), identity_digest=identity,
                claim_request_id=case.scope.claim_request_id, storage_evidence=pending, workspace=None)
            request_id = f'chain-lost-{n}'
            case.store.arbitrate_publication(binding.run_handle, binding.attempt, request_id)
            payload = SettleLostInput(binding.run_handle, identity, case.scope.claim_request_id,
                                     binding.claim_id, case.store.result_lease(case.scope).lease_digest).payload()
            original = case.store.open_account_settlement(binding.run_handle, binding.attempt, request_id, payload,
                                                         runtime_digest=binding.runtime_digest)
            runs.append(dict(scope=case.scope.as_data(), binding=binding.as_data(), pending=pending.hex(),
                             payload=payload, original=original, request_id=request_id))
        from codex_agents.collector_client import PACKAGE_VERSION, COLLECTOR_RUNTIME_DIGEST
        data = dict(project=case.project, key=case.key.hex(), now=case.base.stamp,
            clock_reference_ns=case.ledger._capacity_clock_reference(), runtime_digest=COLLECTOR_RUNTIME_DIGEST,
            version=PACKAGE_VERSION, runs=runs)
        case.store.close()
        case.ledger.close()
        journal.release()
        shutil.copytree(case.directory, destination / 'history')
        shutil.copytree(case.ledger.state.root, destination / 'state')
        (destination / 'manifest.json').write_bytes(canonical_json(data))
    finally:
        journal.release()
        case.doCleanups()


def memory(archive, destination):
    """Real pre-B4t memory custody, stopped abruptly with its kept inode."""
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from tests.test_memory_holds import MemoryFixture
    from codex_agents import memory_holds
    from codex_agents.collector_client import PACKAGE_VERSION, COLLECTOR_RUNTIME_DIGEST
    from codex_agents.learning_contracts import canonical_json
    case = MemoryFixture(methodName='runTest')
    case.setUp()
    case.register()
    case.prep()
    workspace = destination / 'prepare-b4t'
    workspace.mkdir()
    (workspace / 'pending').write_text('remove this fixture folder')
    memory_holds.note_cleanup(case.store, *case.key, case.instance, str(workspace))
    data = dict(project=case.store.project_id, key=case.store._result_key.hex(), now=case.now,
        instance=case.instance, run=list(case.key), workspace=str(workspace), version=PACKAGE_VERSION,
        runtime_digest=COLLECTOR_RUNTIME_DIGEST)
    case.store.close()
    case.journal.keep_lock = True
    shutil.move(str(case.directory), destination / 'history')
    shutil.move(str(case.journal.directory), destination / 'launches')
    (destination / 'manifest.json').write_bytes(canonical_json(data))
    print('memory-custody-ready', flush=True)
    sys.stdin.read()  # The test SIGKILLs this exact fixture process; no Python cleanup.


def rollover(archive, destination):
    """Stop an actual 0.8.67 U1 transaction before or after its atomic commit."""
    sys.path.insert(0, str(archive / 'packages/codex-agents'))
    import tests.support  # noqa: F401
    from codex_agents.collector_client import PACKAGE_VERSION, COLLECTOR_RUNTIME_DIGEST
    from codex_agents.learning_contracts import canonical_json
    from codex_agents.project_history import ProjectHistory
    from codex_agents.orchestration_store import OrchestrationOwner
    from codex_agents.orchestration_api import OrchestrationRequest, OrchestrationScope, OrchestrationLimits
    from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
    from tests.test_project_history import PROJECT_A
    key = b'j' * 32
    directory = destination / 'history'
    directory.mkdir(mode=0o700)
    owner = ProjectHistory(directory, PROJECT_A, result_key=key, result_clock_ms=lambda: 1000,
                           result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                           orchestration_limits=OrchestrationLimits(8, 12))
    scope = OrchestrationScope(PROJECT_A, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
    store = OrchestrationOwner(owner)
    first = store.apply(OrchestrationRequest(1, 'orchestration.open', 'first', scope, {}),
                        client_version='codex-host-v1', before_commit=lambda: None)['current']['snapshot']
    payload = dict(orchestration_handle=first['orchestration_handle'], expected_sequence=first['sequence'],
                   cause='session', session_id='session-1', next_session_id='session-2')
    data = dict(project=PROJECT_A, key=key.hex(), now=1000, version=PACKAGE_VERSION,
                runtime_digest=COLLECTOR_RUNTIME_DIGEST, scope=scope.as_data(), first=first, payload=payload)
    (destination / 'manifest.json').write_bytes(canonical_json(data))
    def checkpoint():
        print('rollover-custody-ready', flush=True)
        sys.stdin.read()
        raise RuntimeError('fixture must be killed')
    original = OrchestrationOwner.apply
    def before_open(store, request, **kwargs):
        if request.operation == 'orchestration.open':
            checkpoint()
        return original(store, request, **kwargs)
    if sys.argv[4] == 'inside':
        OrchestrationOwner.apply = before_open
    original(store, OrchestrationRequest(1, 'orchestration.rollover', 'handoff', scope, payload),
             client_version='codex-host-v1', before_commit=lambda: None)
    checkpoint()


if __name__ == '__main__':
    mode = sys.argv[3] if len(sys.argv) > 3 else 'build'
    {'stop-result': stop_result, 'account': account, 'migrations': migrations, 'chain': chain, 'memory': memory, 'rollover': rollover,
     'build': build}[mode](Path(sys.argv[1]), Path(sys.argv[2]))
