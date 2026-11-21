"""Inert process fixture, not a native enrollment or production supervisor.

The parent supplies disposable test metadata through stdin and two inherited
socketpairs. No provider, account SQLite, listener or native credentials here.
"""
from copy import copy
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
import json
import os
import select
from pathlib import Path
import socket
import sys
import threading
import time
from unittest import mock

import tests.support  # noqa: F401
from tests.test_bridge import PARENT_GRANT, RESULT_SCHEMA

from codex_agents.bridge_liveness import BridgeLivenessOwner
from codex_agents.codex_worker import CodexWorkerLimits, CodexWorkerPool
from codex_agents.control_api import ControlScope
from codex_agents.control_loop import ControlLoop, ControlLoopLimits
from codex_agents.control_service import ControlAuthorization, ControlService
from codex_agents.execution_grant import checked_execution_grant
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.liveness_clock import LivenessLimits
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.project_history import ProjectHistory
from codex_agents.project_monitor import ProjectMonitor, SupervisorScope
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceLane
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.service_protocol import service_request_digest

SECOND = 1_000_000_000
CONTEXT = object()  # Synthetic native frontend; never authentication evidence.


def receive():
    raw = sys.stdin.buffer.readline(131_073)
    if not raw.endswith(b"\n") or len(raw) > 131_072:
        raise RuntimeError("fixture_input")
    return json.loads(raw)


def emit(**value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main():
    data = receive()
    managed_open = data.get('managed_original_open', False)
    lease_scope = None if managed_open else ResultLeaseBinding(**data["scope"])
    scope = SupervisorScope(**{name: data['scope'][name] for name in (
        "project_id", "primary_session_id", "runtime_digest", "supervisor_instance_id", "supervisor_generation")})
    control_scope = ControlScope(**asdict(scope))

    def authorize(request, context):
        if context is CONTEXT:
            return ControlAuthorization(control_scope, request.target, service_request_digest(request),
                                        frozenset({"project.status", "project.cancel"}))

    runtime = monitor = service = loop = owner = registry = pool = None
    if not data.get('product_runtime', False):
        monitor = ProjectMonitor(scope, LivenessLimits(30 * SECOND, 2 * SECOND, 5 * SECOND, 20 * SECOND),
                                 max_tracked_attempts=2, clock_ns=time.monotonic_ns)
        service = ControlService(monitor, authorizer=authorize, clock_ns=time.monotonic_ns)
        loop = ControlLoop(monitor, service, ControlLoopLimits(4, 4, 30 * SECOND, 1_000_000))
        owner = BridgeLivenessOwner(loop)
        registry = OwnedExecutionRegistry()
        pool = CodexWorkerPool(registry, CodexWorkerLimits(1, 16 * 1_048_576))
    root = Path(data["root"])
    key = bytes.fromhex(data["fixture_key"])
    preparation_stack = ExitStack()
    if data.get('actual_preparation', False):
        from codex_agents import bridge
        from tests.learning_support import trusted_tools
        preparation_stack.enter_context(trusted_tools(Path(data['tools'])))
        preparation_stack.enter_context(mock.patch.object(bridge, 'WORKSPACE_PARENT', str(root/'prepare-base')))
        preparation_stack.enter_context(mock.patch.object(bridge, 'temp_roots', new=lambda: set()))
        preparation_stack.enter_context(mock.patch.object(bridge, 'CODEX_MANAGED_PATHS',
            {'darwin': (str(root/'absent-managed.toml'),), 'linux': (str(root/'absent-managed.toml'),)}))
        preparation_stack.enter_context(mock.patch.object(bridge, 'CODEX_SYSTEM_DIRECTORY', str(root/'absent-system')))

    def open_history():
        managed = data.get('with_provenance', False)
        store = ProjectHistory(Path(data["history"]), scope.project_id, result_key=key,
                               allow_legacy_orchestration_reference=not managed,
                               orchestration_limits=OrchestrationLimits(1, 8) if managed else None,
                               result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))
        if managed and not managed_open:
            # Explicit deterministic fake enrollment, never native authority.
            opened = OrchestrationRequest(1, 'orchestration.open', 'fixture-open', OrchestrationScope(**asdict(scope)), {})
            with mock.patch('codex_agents.orchestration_store.secrets.token_hex', return_value=lease_scope.orchestration_handle[4:]):
                OrchestrationOwner(store).apply(opened, client_version=data.get('primary_client', 'codex-host-v1'),
                                               before_commit=lambda: None)
        return store

    lane = None
    dispatcher = ingress = None
    listeners = []
    publication = threading.Event()
    publication_entered = threading.Event()
    publication_patch = None
    admission_patch = None
    admitted_replies = []
    if data.get('lose_result_reply', False):
        original_admit = ProjectHistory.admit_host_result
        def admit_then_lose_reply(*args, **kwargs):
            reply = original_admit(*args, **kwargs)
            if admitted_replies and reply != admitted_replies[0]:
                raise RuntimeError('fixture_changed_original_host_outcome')
            admitted_replies.append(reply)
            if len(admitted_replies) == 1:
                raise OSError('PRIVATE-LOST-HOST-OUTCOME-REPLY')
            return reply
        admission_patch = mock.patch.object(ProjectHistory, 'admit_host_result', new=admit_then_lose_reply)
        admission_patch.start()
    if data.get('hold_result_publish', False) or data.get('hold_result_read', False):
        method = 'read_result' if data.get('hold_result_read', False) else 'publish_result'
        original = getattr(ProjectHistory, method)
        def held_writer(*args, **kwargs):
            if not publication_entered.is_set():
                publication_entered.set()
                emit(stage='writer_stalled', operation=method)
                if not publication.wait(10):
                    raise RuntimeError('fixture_writer_not_released')
            return original(*args, **kwargs)
        publication_patch = mock.patch.object(ProjectHistory, method, new=held_writer)
        publication_patch.start()
    try:
        api_options = {}
        if data.get('client_dispatch', False):
            from codex_agents.project_request_queue import ProjectRequestLimits
            from codex_agents.result_read_api import result_read_request_digest
            from codex_agents.result_read_service import ResultReadAuthorization
            def authorize_result(request, context):
                if context is CONTEXT and request.binding == binding:
                    return ResultReadAuthorization(binding, request.result_digest, result_read_request_digest(request))
            api_options = {'request_limits': ProjectRequestLimits(4, 10*SECOND), 'result_authorizer': authorize_result}
            if data.get('client_open', False) or data.get('client_result', False):
                from codex_agents.history_api import history_request_digest
                from codex_agents.history_service import HistoryAuthorization
                from codex_agents.host_result_contracts import HostResultContext
                from codex_agents.open_scope_contracts import OpenScopeContext
                prospective = (OpenScopeContext.from_snapshot(data['prospective'])
                               if data.get('client_open', False) else None)
                def authorize_history(request, context):
                    if context is CONTEXT:
                        operations = {'history.open_scope'} if prospective is not None else set()
                        original_result = None
                        if data.get('client_result', False):
                            operations |= {'history.admit_host_result', 'history.read_record',
                                           'history.result_reference'}
                            if request.operation == 'history.result_reference':
                                # D1: as the product host does, the stored context of the named attempt authorizes it;
                                # an attempt that was never opened is refused as not found (O0d R1).
                                payload = request.payload
                                if not (root/'launches').exists():
                                    # The fixture leases at open, where the product driver leases when it dispatches:
                                    # before any launch there is no dispatch to find.
                                    raise HistoryError('history_not_found')
                                original_result = lane.stored_attempt_context(
                                    payload['run_handle'], payload['attempt'], data.get('primary_client', 'codex-host-v1'),
                                    payload['inputs_digest'])
                            if request.operation != 'history.open_scope':
                                original_result = HostResultContext(binding, data['assignment'], data['task'],
                                                                    data['route'], data['client_capability'])
                        return HistoryAuthorization(request.project_id, request.runtime_digest,
                            history_request_digest(request), 'host', frozenset(operations), original_result,
                            open_scope_context=prospective)
                api_options['history_authorizer'] = authorize_history
            if data.get('client_result', False):
                from codex_agents.orchestration_api import orchestration_request_digest
                from codex_agents.orchestration_service import OrchestrationAuthorization
                def authorize_orchestration(request, context):
                    if context is CONTEXT:
                        return OrchestrationAuthorization(OrchestrationScope(**asdict(scope)),
                            orchestration_request_digest(request), data.get('primary_client', 'codex-host-v1'),
                            frozenset({'orchestration.status'}))
                api_options['orchestration_authorizer'] = authorize_orchestration
        if data.get('product_runtime', False):
            from codex_agents.project_runtime import ProjectRuntime, ProjectRuntimeLimits
            from codex_agents.project_ingress import ProjectIngressLimits
            limits = ProjectRuntimeLimits(2, 1, 2*MAX_PERSISTENCE_ENTRY_BYTES,
                LivenessLimits(30*SECOND, 2*SECOND, 5*SECOND, 20*SECOND),
                ControlLoopLimits(4, 4, 30*SECOND, 1_000_000), CodexWorkerLimits(1, 16*1_048_576),
                api_options['request_limits'], ProjectIngressLimits(4, 4, 10*SECOND, 1_000_000))
            runtime = ProjectRuntime(OrchestrationScope(**asdict(scope)), open_history, limits,
                control_authorizer=authorize, ingress_authorizer=lambda raw, context, phase: context is CONTEXT,
                history_authorizer=api_options.get('history_authorizer'), result_authorizer=api_options['result_authorizer'],
                orchestration_authorizer=api_options.get('orchestration_authorizer'))
            monitor, service, loop, owner, pool, lane, ingress = (runtime.monitor, runtime.control, runtime.loop,
                runtime.owner, runtime.pool, runtime.persistence, runtime.ingress)
            registry = pool._registry
        else:
            lane = ResultPersistenceLane(owner, pool, open_history, max_entries=1,
                retained_bytes=2*MAX_PERSISTENCE_ENTRY_BYTES if api_options else MAX_PERSISTENCE_ENTRY_BYTES,
                allow_local_execution_reference=not data.get('with_provenance', False), **api_options)

        def until(predicate):
            end = time.monotonic() + 12
            while not predicate():
                if time.monotonic() >= end:
                    raise RuntimeError("fixture_progress_timeout")
                for listener, kind in listeners:
                    try:
                        connection, _ = listener.accept()
                    except BlockingIOError:
                        continue
                    if kind == 'dispatch':
                        if runtime is None:
                            loop.attach(connection, CONTEXT)
                        else:
                            runtime.attach_control(connection, CONTEXT)
                    else:
                        if runtime is None:
                            ingress.attach(connection, CONTEXT)
                        else:
                            runtime.attach_project(connection, CONTEXT)
                if runtime is None:
                    owner.run_once()
                    if dispatcher is not None:
                        dispatcher.drive()
                else:
                    runtime.run_once()
                if publication_entered.is_set() and not publication.is_set() and select.select([sys.stdin], [], [], 0)[0]:
                    if receive() != {'release_writer': True}:
                        raise RuntimeError('fixture_invalid_writer_release')
                    publication.set()
                time.sleep(.002)

        def next_message():
            if data.get('client_dispatch', False):
                until(lambda: bool(select.select([sys.stdin], [], [], 0)[0]))
            return receive()

        def start_ingress():
            nonlocal ingress
            if listeners:
                return
            from codex_agents.project_ingress import ProjectIngress, ProjectIngressLimits
            if ingress is None:
                ingress = ProjectIngress(lane, ProjectIngressLimits(4, 4, 10*SECOND, 1_000_000),
                                         authorizer=lambda raw, context, phase: context is CONTEXT)
                ingress.bootstrap()
            for name, kind in (('d.sock', 'dispatch'), ('r.sock', 'result')):
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(root/name))
                (root/name).chmod(0o600)
                listener.listen(4)
                listener.setblocking(False)
                listeners.append((listener, kind))

        preparation_handle = prepared_inputs = None
        if data.get('actual_preparation', False):
            from codex_agents.codex_preparation import (MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES,
                PreparationContext, PreparationLimits, preparation_payload)
            from codex_agents.open_scope_contracts import OpenScopeContext
            original_preparation = PreparationContext(preparation_payload(OpenScopeContext.from_snapshot(data['prospective']),
                Path(data['project_root']), Path(data['bundle_root']), {'PATH': os.defpath},
                expected_export_commit=data['expected_export_commit']))
            preparation = runtime.bind_preparation(PreparationLimits(1, MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES),
                authorizer=lambda context: context is original_preparation)
            preparation_handle = preparation.submit(original_preparation)
            until(lambda: preparation.snapshot(preparation_handle)['state'] in {'ready', 'failed', 'unknown', 'cleaned'})
            prepared_inputs = preparation.prepared(preparation_handle)
            worker = prepared_inputs.worker
        else:
            payload = json.dumps({"task": data["task"], "route": data["route"], "assignment": data["assignment"],
                                  "parent_grant": PARENT_GRANT, "schema": RESULT_SCHEMA.decode(),
                                  "export": str(root / "export"), "control": str(root / "control"),
                                  "executable": str(root / "inert-exec"), "environ": {"PATH": os.defpath},
                                  "role_prompt": "Read the inert fixture only.", "worker_result_path": None}).encode()
            worker = pool.prepare(payload)
        if managed_open:
            # Actual local resources and original inputs exist before account
            # admission. This fixture message is not native enrollment proof.
            endpoints = {}
            if data.get('client_open', False):
                start_ingress()
                endpoints = {'dispatch_endpoint': str(root/'d.sock'), 'result_endpoint': str(root/'r.sock')}
            actual = ({} if prepared_inputs is None else {'export_info': prepared_inputs.export_info,
                'client_identity': prepared_inputs.client_identity})
            emit(stage='prepared', tickets=len(registry._issued), launched=(root/'launches').exists(), **endpoints, **actual)
            lease_scope = ResultLeaseBinding(**next_message()['scope'])
            if any(getattr(lease_scope, name) != value for name, value in asdict(scope).items()):
                raise RuntimeError('fixture_foreign_original_open')
        # D1: as the project driver does, the lease stores the run's record context (its host class and capability).
        stored_context = ({'host_class': data.get('primary_client', 'codex-host-v1'),
                           'client_capability': data['client_capability']} if data.get('client_result', False) else None)
        handle = lane.prepare_lease(lease_scope, worker, record_context=stored_context)
        until(lambda: lane.snapshot(handle)["state"] != "preparing")
        lane.arm(handle)
        until(lambda: lane.snapshot(handle)["state"] != "arming")
        emit(stage="armed", evidence=lane.storage_evidence(handle).hex())
        if data.get('with_provenance', False):
            from codex_agents.learning_contracts import canonical_json
            original_open = canonical_json(receive()['provenance'])
            original_record_open = ProjectHistory.record_host_open
            opened_records = []
            def record_then_lose_reply(store, *args, **kwargs):
                if opened_records:
                    with mock.patch.object(store, '_result_clock', side_effect=AssertionError('journal clock refreshed')):
                        result = original_record_open(store, *args, **kwargs)
                else:
                    result = original_record_open(store, *args, **kwargs)
                opened_records.append(result)
                if len(opened_records) == 1:
                    raise OSError('PRIVATE-LOST-OPEN-JOURNAL-REPLY')
                return result
            patcher = (mock.patch.object(ProjectHistory, 'record_host_open', new=record_then_lose_reply)
                if data.get('lose_open_reply', False) else nullcontext())
            with patcher:
                lane.record_host_open(handle, original_open)
                until(lambda: lane.snapshot(handle)['state'] != 'recording_host_open')
                if data.get('lose_open_reply', False):
                    if lane.snapshot(handle)['state'] != 'failed':
                        raise RuntimeError('fixture_lost_open_reply_not_failed')
                    try:
                        lane.record_host_open(handle, original_open+b' ')
                    except HistoryError as error:
                        if error.reason_code != 'history_conflict':
                            raise
                    else:
                        raise RuntimeError('fixture_lost_open_changed_proof')
                    lane.record_host_open(handle)
                    until(lambda: lane.snapshot(handle)['state'] != 'recording_host_open')
                    if len(opened_records) != 2 or opened_records[0] != opened_records[1]:
                        raise RuntimeError('fixture_lost_open_changed_original')
            if lane.snapshot(handle)['state'] != 'claim_pending':
                raise RuntimeError('fixture_original_open_not_recorded')
            emit(stage='open_recorded', **lane.host_open_record(handle))
        receipt = receive()
        binding = AttemptBinding(**{name: getattr(lease_scope, name) for name in LEASE_SCOPE_FIELDS},
                                 claim_id=receipt["bridge_run_id"], claim_digest=receipt["receipt_digest"])
        lane.bind_claim(handle, binding, hard_deadline_at_ms=receipt["deadline_at_ms"])
        until(lambda: lane.snapshot(handle)["state"] != "binding_claim")
        emit(stage="bound", evidence=lane.bound_storage_evidence(handle).hex())
        registered = receive()
        grant = checked_execution_grant(registered["grant"])
        monitor.register(binding, hard_deadline_ns=grant.deadline_ns)

        def refuses_issue(value):
            try:
                lane.issue_execution(value)
            except HistoryError:
                return
            raise RuntimeError("fixture_duplicate_execution")

        refuses_issue(handle)  # Bound storage alone cannot issue local custody.
        options = {}
        if data.get('with_provenance', False):
            from codex_agents.learning_contracts import canonical_json
            options['provenance'] = canonical_json(registered['provenance'])
            try:
                lane.consume_execution(handle, grant.encode())
            except HistoryError as error:
                if error.reason_code != 'history_execution_not_ready':
                    raise
            else:
                raise RuntimeError('fixture_missing_provenance_admitted')
            if lane.snapshot(handle)['state'] != 'reserved':
                raise RuntimeError('fixture_missing_provenance_changed_state')
            damaged = dict(registered['provenance'], seal='0' * 64)
            for invalid in (b'{', canonical_json(damaged)):
                lane.consume_execution(handle, grant.encode(), provenance=invalid)
                until(lambda: lane.snapshot(handle)['state'] != 'consuming_execution')
                if lane.snapshot(handle)['state'] != 'failed':
                    raise RuntimeError('fixture_invalid_provenance_admitted')
                refuses_issue(handle)
                if (root / 'launches').exists():
                    raise RuntimeError('fixture_invalid_provenance_launched')
                try:
                    lane.consume_execution(handle, grant.encode() + b' ', **options)
                except HistoryError as error:
                    if error.reason_code != 'history_conflict':
                        raise
                else:
                    raise RuntimeError('fixture_rejected_provenance_changed_grant')
        if data.get('lose_consumption_reply', False):
            original_consume = ProjectHistory.consume_execution_grant

            def commit_then_lose_reply(store, *args, **kwargs):
                original_consume(store, *args, **kwargs)
                raise OSError('PRIVATE-LOST-CONSUMPTION-REPLY')

            with mock.patch.object(ProjectHistory, 'consume_execution_grant', new=commit_then_lose_reply):
                lane.consume_execution(handle, grant.encode(), **options)
                until(lambda: lane.snapshot(handle)['state'] != 'consuming_execution')
            if lane.snapshot(handle)['state'] != 'failed':
                raise RuntimeError('fixture_lost_consumption_not_failed')
            refuses_issue(handle)
            try:
                lane.consume_execution(handle, grant.encode(), provenance=canonical_json(damaged))
            except HistoryError as error:
                if error.reason_code != 'history_conflict':
                    raise
            else:
                raise RuntimeError('fixture_uncertain_consumption_changed_proof')
            lane.consume_execution(handle, grant.encode(), **options)
            until(lambda: lane.snapshot(handle)['state'] != 'consuming_execution')
            if lane.snapshot(handle)['reason_code'] != 'history_execution_consumed':
                raise RuntimeError('fixture_consumption_reissued')
            refuses_issue(handle)
            emit(stage='consumption_fenced', tickets=len(registry._issued), launched=(root / 'launches').exists())
            return
        lane.consume_execution(handle, grant.encode(), **options)
        until(lambda: lane.snapshot(handle)["state"] != "consuming_execution")
        refuses_issue(copy(handle))
        if data.get('client_dispatch', False):
            from codex_agents.prepared_dispatch import DispatchAuthorization, PreparedDispatchContext, PreparedDispatchService
            from codex_agents.dispatch_api import dispatch_request_digest
            export_info = (0, '0'*40, 'a'*64) if prepared_inputs is None else prepared_inputs.export_info
            prepared = PreparedDispatchContext(binding, handle, worker, lane.prepared_inputs_digest(handle), export_info)
            def authorize_dispatch(request, context):
                if context is CONTEXT:
                    return DispatchAuthorization(control_scope, dispatch_request_digest(request), prepared,
                                                 frozenset({'dispatch.submit', 'dispatch.status'}))
            if runtime is None:
                dispatcher = PreparedDispatchService(owner, pool, lane, max_requests=1, steps_per_cycle=1,
                                                     authorizer=authorize_dispatch)
                service.bind_dispatch(dispatcher)
            else:
                dispatcher = runtime.bind_dispatch(max_requests=1, steps_per_cycle=1, authorizer=authorize_dispatch)
            start_ingress()
            emit(stage='dispatch_ready', inputs_digest=lane.prepared_inputs_digest(handle),
                 dispatch_endpoint=str(root/'d.sock'), result_endpoint=str(root/'r.sock'),
                 tickets=len(registry._issued), launched=(root/'launches').exists())
        else:
            ticket = lane.issue_execution(handle)
            refuses_issue(handle)
            pool.bind(worker, ticket)
            owner.bind(binding, pool.supervisor(worker))
            lane.bind(handle)
        for descriptor in sys.argv[1:]:
            loop.attach(socket.socket(fileno=int(descriptor)), CONTEXT)
        if dispatcher is None:
            pool.submit(worker)
        until(lambda: (root / "launches").exists())
        supervisor = pool.supervisor(worker)
        if dispatcher is not None:
            ticket = lane._job(handle).execution_ticket
        retirement_refusals = []
        def refuses_dispatch_retirement(stage):
            if dispatcher is None:
                return
            from codex_agents.dispatch_api import DispatchError
            job = dispatcher._jobs[(binding.run_handle, binding.attempt)]
            retained = job.pending
            try:
                dispatcher.retire_released(prepared)
            except DispatchError as error:
                if error.reason_code != 'dispatch_conflict':
                    raise
            else:
                raise RuntimeError('fixture_dispatch_retired_early')
            if job.pending is not retained:
                raise RuntimeError('fixture_dispatch_dropped_original_on_refusal')
            if prepared_inputs is not None:
                from codex_agents.codex_preparation import PreparationError
                try:
                    preparation.discard(preparation_handle)
                except PreparationError as error:
                    if error.reason_code != 'preparation_busy':
                        raise
                else:
                    raise RuntimeError('fixture_original_export_dropped_before_worker_retirement')
                if not prepared_inputs.workspace.exists():
                    raise RuntimeError('fixture_original_export_missing_before_worker_retirement')
            retirement_refusals.append(stage)
        refuses_dispatch_retirement('running')
        preparation_closed_while_bound = prepared_inputs is not None and data.get('await_release', False)
        if preparation_closed_while_bound:
            preparation.request_close()
        emit(stage="started", pid=os.getpid(), registry_pid=registry._pid, deadline_ns=grant.deadline_ns)
        until(lambda: pool.snapshot(worker)["returned"])
        selected = owner.result(binding, pool, worker)
        if dispatcher is None:
            lane.submit(handle)
        until(lambda: lane.snapshot(handle)["state"] in {"committed", "failed", "unknown"})
        if lane.snapshot(handle)["state"] != "committed":
            raise RuntimeError("fixture_persistence_failed")
        if dispatcher is not None:
            dispatcher.drive()
        refuses_dispatch_retirement('published')
        if data.get('retain_cleanup', False):
            try:
                lane.retain_cleanup(handle)
            except HistoryError as error:
                if error.reason_code != 'history_result_not_ready':
                    raise
            else:
                raise RuntimeError('fixture_cleanup_before_terminal_ack')
        emit(stage='terminal', evidence=lane.terminal_evidence(handle).hex(), delivery=lane.delivery(handle).as_data())
        from codex_agents.learning_contracts import canonical_json
        acknowledgement = canonical_json(next_message())
        original_ack = ProjectHistory.acknowledge_account_terminal
        acknowledgements = []

        def lose_first_reply(store, binding, raw):
            original_ack(store, binding, raw)
            acknowledgements.append(raw)
            if len(acknowledgements) == 1:
                raise OSError('PRIVATE-LOST-WRITER-ACK')

        with mock.patch.object(ProjectHistory, 'acknowledge_account_terminal', new=lose_first_reply):
            lane.acknowledge_terminal(handle, acknowledgement)
            until(lambda: lane.snapshot(handle)['state'] != 'acknowledging')
            if lane.snapshot(handle)['state'] != 'failed' or not lane.snapshot(handle)['committed']:
                raise RuntimeError('fixture_lost_ack_not_preserved')
            lane.acknowledge_terminal(handle)  # Original retained reply; no account/model retry.
            until(lambda: lane.snapshot(handle)['state'] != 'acknowledging')
        if acknowledgements != [acknowledgement, acknowledgement]:
            raise RuntimeError('fixture_replaced_acknowledgement')
        if lane.snapshot(handle)['state'] != 'acknowledged':
            raise RuntimeError('fixture_acknowledgement_failed')
        refuses_dispatch_retirement('acknowledged')
        cleanup = {}
        if data.get('retain_cleanup', False):
            proof = lane.retain_cleanup(handle)
            if lane.retain_cleanup(handle) is not proof:
                raise RuntimeError('fixture_replaced_cleanup_custody')
            snapshot = registry.cleanup_snapshot(proof)
            if (snapshot['key'] != registry.key(ticket, cleaned=True)
                    or snapshot['deadline_ns'] != grant.deadline_ns):
                raise RuntimeError('fixture_cleanup_original_mismatch')
            cleanup['cleanup_custody_retained'] = True
            original_cleanup = ProjectHistory.issue_cleanup
            cleanup_attempts = []
            custody_race_checked = []

            def lose_cleanup_reply(store, *args, **options):
                if not custody_race_checked:
                    original_reference = registry._cleanups[proof]
                    def revoke_after_insert(sql):
                        if sql.lstrip().startswith('INSERT INTO project_cleanup_receipts'):
                            if data.get('crash_cleanup', False):
                                os._exit(88)  # Disposable writer-process crash, not a cleanup acknowledgement.
                            with registry._lock:
                                registry._cleanups.pop(proof)
                    store._connection.set_trace_callback(revoke_after_insert)
                    try:
                        try:
                            original_cleanup(store, *args, **options)
                        except HistoryError as error:
                            if error.reason_code != 'history_not_authorized':
                                raise
                        else:
                            raise RuntimeError('fixture_cleanup_revocation_accepted')
                    finally:
                        with registry._lock:
                            registry._cleanups[proof] = original_reference
                        store._connection.set_trace_callback(None)
                    if (store._connection.execute('SELECT COUNT(*) FROM project_cleanup_receipts').fetchone()[0] != 0
                            or store._connection.execute('SELECT cleanup_digest FROM project_cleanup_reservations').fetchone()[0] is not None):
                        raise RuntimeError('fixture_cleanup_revocation_not_rolled_back')
                    custody_race_checked.append(True)
                raw = original_cleanup(store, *args, **options)
                cleanup_attempts.append(raw)
                # Recovery reads original bytes without a fresh clock/window.
                with mock.patch.object(store, '_result_clock', side_effect=AssertionError('cleanup read clock')):
                    if store.cleanup_evidence(args[0]) != raw:
                        raise RuntimeError('fixture_changed_cleanup_journal')
                if len(cleanup_attempts) == 1:
                    raise OSError('fixture_lost_cleanup_commit_reply')
                return raw

            with mock.patch.object(ProjectHistory, 'issue_cleanup', new=lose_cleanup_reply):
                lane.prepare_cleanup(handle)
                until(lambda: lane.snapshot(handle)['state'] != 'preparing_cleanup')
                if lane.snapshot(handle)['state'] != 'failed':
                    raise RuntimeError('fixture_missing_cleanup_reply_loss')
                lane.prepare_cleanup(handle)
                until(lambda: lane.snapshot(handle)['state'] != 'preparing_cleanup')
            if lane.snapshot(handle)['state'] != 'cleanup_ready' or len(cleanup_attempts) != 2:
                raise RuntimeError('fixture_cleanup_not_ready')
            raw = lane.cleanup_evidence(handle)
            if cleanup_attempts != [raw, raw] or lane.cleanup_evidence(handle) is not raw:
                raise RuntimeError('fixture_replaced_cleanup_evidence')
            cleanup['cleanup_evidence'] = raw.hex()
            cleanup['cleanup_lost_ack_recovered'] = True
            cleanup['cleanup_custody_race_rolled_back'] = custody_race_checked == [True]
        refuses_dispatch_retirement('cleanup_ready')
        emit(stage="done", cleanup_confirmed=supervisor.snapshot()["cleanup_confirmed"],
             fenced=selected.packet.fenced, terminal_state=selected.packet.terminal_state,
             stop_reason=selected.stop_reason, ticket_count=len(registry._issued),
             launches=(root / "launches").read_text().splitlines(),
             acknowledged=lane.snapshot(handle)['state'] in {'acknowledged', 'cleanup_ready'},
             persisted_claim=lane.delivery(handle).binding.claim_id, **cleanup)
        if data.get('await_release', False):
            from codex_agents.cleanup_contracts import verify_cleanup_evidence
            from codex_agents.release_contracts import verify_release_receipt
            original = lane.cleanup_evidence(handle)
            value = verify_cleanup_evidence(key, lease_scope, original)
            from codex_agents.learning_contracts import keyed_digest
            account_reply = canonical_json(next_message())
            verify_release_receipt(key, value, keyed_digest(key, original), account_reply)
            refuses_dispatch_retirement('account_reply_only')
            try:
                lane.finish_release(handle)
            except HistoryError:
                pass
            else:
                raise RuntimeError('fixture_retired_before_durable_release_ack')
            try:
                lane.retire(handle)
            except HistoryError:
                pass
            else:
                raise RuntimeError('fixture_lane_retired_before_release_ack')
            record_release = ProjectHistory.record_account_release
            release_attempts = []
            release_race_checked = []
            def lose_release_reply(store, *args, **options):
                if not release_race_checked:
                    reference = registry._cleanups[proof]
                    def withdraw(sql):
                        if sql.lstrip().startswith('INSERT INTO project_cleanup_acknowledgements'):
                            with registry._lock:
                                registry._cleanups.pop(proof)
                    store._connection.set_trace_callback(withdraw)
                    try:
                        try:
                            record_release(store, *args, **options)
                        except HistoryError as error:
                            if error.reason_code != 'history_not_authorized':
                                raise
                        else:
                            raise RuntimeError('fixture_release_custody_loss_accepted')
                    finally:
                        with registry._lock:
                            registry._cleanups[proof] = reference
                        store._connection.set_trace_callback(None)
                    if (store._connection.execute('SELECT COUNT(*) FROM project_cleanup_acknowledgements').fetchone()[0] != 0
                            or store._connection.execute('SELECT account_release_digest FROM project_cleanup_reservations').fetchone()[0] is not None):
                        raise RuntimeError('fixture_release_custody_loss_not_rolled_back')
                    release_race_checked.append(True)
                raw = record_release(store, *args, **options)
                release_attempts.append(raw)
                with mock.patch.object(store, '_result_clock', side_effect=AssertionError('release ACK clock')):
                    if store.account_release_receipt(args[0]) != raw:
                        raise RuntimeError('fixture_changed_original_release_ack')
                if len(release_attempts) == 1:
                    raise OSError('fixture_lost_project_release_ack')
                return raw
            with mock.patch.object(ProjectHistory, 'record_account_release', new=lose_release_reply):
                lane.acknowledge_release(handle, account_reply)
                until(lambda: lane.snapshot(handle)['state'] != 'acknowledging_release')
                if lane.snapshot(handle)['state'] != 'failed' or registry._issued[ticket].state != 'cleaned':
                    raise RuntimeError('fixture_early_original_release')
                refuses_dispatch_retirement('writer_reply_unknown')
                try:
                    lane.finish_release(handle)
                except HistoryError:
                    pass
                else:
                    raise RuntimeError('fixture_released_after_unknown_writer_reply')
                lane.acknowledge_release(handle)
                until(lambda: lane.snapshot(handle)['state'] != 'acknowledging_release')
            if release_attempts != [account_reply, account_reply] or lane.release_receipt(handle) != account_reply:
                raise RuntimeError('fixture_replaced_project_release_ack')
            refuses_dispatch_retirement('release_acknowledged')
            lane.finish_release(handle)
            lane.finish_release(handle)
            refuses_dispatch_retirement('release_finished')
            lane.retire(handle)
            refuses_dispatch_retirement('persistence_retired')
            owner.retire(binding, pool, worker)
            preparation_cleanup = {}
            if prepared_inputs is not None:
                preparation.discard(preparation_handle)
                until(lambda: preparation.snapshot(preparation_handle)['state'] in {'cleaned', 'unknown'})
                if preparation.snapshot(preparation_handle)['state'] != 'cleaned':
                    raise RuntimeError('fixture_original_export_cleanup_unconfirmed')
                preparation.retire(preparation_handle)
                until(preparation.threads_stopped)
                preparation_cleanup = {'original_export_removed': not prepared_inputs.workspace.exists(),
                    'preparation_closed_while_bound': preparation_closed_while_bound,
                    'preparation_cleanup_thread_stopped': preparation.threads_stopped()}
            dispatch_retirement = {}
            if dispatcher is not None:
                from codex_agents.dispatch_api import DispatchError
                try:
                    dispatcher.retire_released(copy(prepared))
                except DispatchError as error:
                    if error.reason_code != 'dispatch_not_authorized':
                        raise
                else:
                    raise RuntimeError('fixture_copied_dispatch_context_retired_original')
                dispatcher.retire_released(prepared)
                dispatcher.retire_released(prepared)
                job = dispatcher._jobs[(binding.run_handle, binding.attempt)]
                if (job.state != 'retired' or job.pending is not None or job.report is not None
                        or job.supervisor is not None or job.delivery is not None
                        or job.transport is not None or job.raw != b'' or dispatcher._rotation):
                    raise RuntimeError('fixture_dispatch_payload_retained')
                dispatch_retirement = {'dispatch_state': job.state, 'dispatch_payload_dropped': True,
                    'dispatch_lifetime_slots': len(dispatcher._jobs), 'dispatch_retirement_refusals': retirement_refusals}
            emit(stage='release_received', local_execution_released=registry._issued[ticket].state == 'released',
                 local_persistence_retired=handle not in lane._jobs,
                 local_worker_retired=worker not in pool._jobs, release_ack=account_reply.hex(),
                 release_custody_loss_rolled_back=release_race_checked == [True], **dispatch_retirement, **preparation_cleanup)
        if data.get('client_dispatch', False) and next_message() != {'client_done': True}:
            raise RuntimeError('fixture_client_not_finished')
        if data.get('lose_result_reply', False):
            emit(stage='result_reconciled', attempts=len(admitted_replies),
                 identical=bool(admitted_replies) and all(v == admitted_replies[0] for v in admitted_replies))
    finally:
        publication.set()
        if publication_patch is not None:
            publication_patch.stop()
        if admission_patch is not None:
            admission_patch.stop()
        for listener, _ in listeners:
            listener.close()
        if runtime is not None:
            runtime.request_close()
            end = time.monotonic()+6
            while not runtime.threads_stopped():
                if time.monotonic() >= end:
                    raise RuntimeError('fixture_product_runtime_shutdown')
                time.sleep(.002)
        else:
            if ingress is not None:
                ingress.close()
                ingress._thread.join(6)
                if ingress._thread.is_alive():
                    raise RuntimeError('fixture_ingress_cleanup')
            if pool is not None:
                pool.close()
            if lane is not None:
                lane.close()
                lane._writer.join(6)
                if lane._writer.is_alive():
                    raise RuntimeError('fixture_writer_cleanup')
            if pool is not None:
                for slot in pool._slots:
                    slot.thread.join(6)
                    if slot.thread.is_alive():
                        raise RuntimeError('fixture_worker_cleanup')
            if loop is not None:
                loop.close()
            if service is not None:
                service.close()
        preparation_stack.close()



if __name__ == "__main__":
    main()
