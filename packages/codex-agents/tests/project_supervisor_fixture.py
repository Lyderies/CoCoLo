"""Inert process fixture, not a native enrollment or production supervisor.

The parent supplies disposable test metadata through stdin and two inherited
socketpairs. No provider, account SQLite, listener or native credentials here.
"""
from copy import copy
from contextlib import nullcontext
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import sys
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
    monitor = ProjectMonitor(scope, LivenessLimits(30 * SECOND, 2 * SECOND, 5 * SECOND, 20 * SECOND),
                             max_tracked_attempts=2, clock_ns=time.monotonic_ns)
    control_scope = ControlScope(**asdict(scope))

    def authorize(request, context):
        if context is CONTEXT:
            return ControlAuthorization(control_scope, request.target, service_request_digest(request),
                                        frozenset({"project.status", "project.cancel"}))

    service = ControlService(monitor, authorizer=authorize, clock_ns=time.monotonic_ns)
    loop = ControlLoop(monitor, service, ControlLoopLimits(4, 4, 30 * SECOND, 1_000_000))
    owner = BridgeLivenessOwner(loop)
    registry = OwnedExecutionRegistry()
    pool = CodexWorkerPool(registry, CodexWorkerLimits(1, 16 * 1_048_576))
    root = Path(data["root"])
    key = bytes.fromhex(data["fixture_key"])

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
                OrchestrationOwner(store).apply(opened, client_version='codex-host-v1', before_commit=lambda: None)
        return store

    lane = None
    try:
        lane = ResultPersistenceLane(owner, pool, open_history, max_entries=1,
                                     retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=not data.get('with_provenance', False))

        def until(predicate):
            end = time.monotonic() + 12
            while not predicate():
                if time.monotonic() >= end:
                    raise RuntimeError("fixture_progress_timeout")
                owner.run_once()
                time.sleep(.002)

        payload = json.dumps({"task": data["task"], "route": data["route"], "assignment": data["assignment"],
                              "parent_grant": PARENT_GRANT, "schema": RESULT_SCHEMA.decode(),
                              "export": str(root / "export"), "control": str(root / "control"),
                              "executable": str(root / "inert-exec"), "environ": {"PATH": os.defpath},
                              "role_prompt": "Read the inert fixture only.", "worker_result_path": None}).encode()
        worker = pool.prepare(payload)
        if managed_open:
            # Actual local resources and original inputs exist before account
            # admission. This fixture message is not native enrollment proof.
            emit(stage='prepared', tickets=len(registry._issued), launched=(root/'launches').exists())
            lease_scope = ResultLeaseBinding(**receive()['scope'])
            if any(getattr(lease_scope, name) != value for name, value in asdict(scope).items()):
                raise RuntimeError('fixture_foreign_original_open')
        handle = lane.prepare_lease(lease_scope, worker)
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
        ticket = lane.issue_execution(handle)
        refuses_issue(handle)
        pool.bind(worker, ticket)
        supervisor = pool.supervisor(worker)
        owner.bind(binding, supervisor)
        lane.bind(handle)
        for descriptor in sys.argv[1:]:
            loop.attach(socket.socket(fileno=int(descriptor)), CONTEXT)
        pool.submit(worker)
        until(lambda: (root / "launches").exists())
        emit(stage="started", pid=os.getpid(), registry_pid=registry._pid, deadline_ns=grant.deadline_ns)
        until(lambda: pool.snapshot(worker)["returned"])
        selected = owner.result(binding, pool, worker)
        lane.submit(handle)
        until(lambda: lane.snapshot(handle)["state"] in {"committed", "failed", "unknown"})
        if lane.snapshot(handle)["state"] != "committed":
            raise RuntimeError("fixture_persistence_failed")
        emit(stage='terminal', evidence=lane.terminal_evidence(handle).hex(), delivery=lane.delivery(handle).as_data())
        from codex_agents.learning_contracts import canonical_json
        acknowledgement = canonical_json(receive())
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
        emit(stage="done", cleanup_confirmed=supervisor.snapshot()["cleanup_confirmed"],
             fenced=selected.packet.fenced, terminal_state=selected.packet.terminal_state,
             stop_reason=selected.stop_reason, ticket_count=len(registry._issued),
             launches=(root / "launches").read_text().splitlines(),
             acknowledged=lane.snapshot(handle)['state'] == 'acknowledged',
             persisted_claim=lane.delivery(handle).binding.claim_id)
    finally:
        pool.close()
        if lane is not None:
            lane.close()
            lane._writer.join(6)
            if lane._writer.is_alive():
                raise RuntimeError("fixture_writer_cleanup")
        for slot in pool._slots:
            slot.thread.join(6)
            if slot.thread.is_alive():
                raise RuntimeError("fixture_worker_cleanup")
        loop.close()
        service.close()


if __name__ == "__main__":
    main()
