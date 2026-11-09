"""Closed history/result-read owner with no enabled native endpoint.

This runs on the persistence owner's thread, outside local control/timer processes.
The default authorization adapter denies without opening a store. A real adapter
must independently authenticate and authorize the entire operation, including
session/generation/claim and receipt semantics; a role string or this dataclass is
not proof. Disposable process fixtures may supply explicitly fake server context.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .history_api import (
    HISTORY_OPERATIONS, HISTORY_PROTOCOL_VERSION, MAX_HISTORY_REPLY_BYTES,
    HistoryRequest, decode_history_request, decode_history_response, history_request_digest,
)
from .history_contracts import AttemptBinding, HistoryEntry, HistoryError, _DIGEST, _PROJECT, matches
from .host_result_contracts import HostResultContext, OperationalQAContext, WorkerResultContext, decode_host_result_reference
from .host_open_contracts import HostOpenContext, decode_host_open_submission
from .learning_contracts import canonical_json, parse_bounded_json
from .project_history import ProjectHistory
from .result_contracts import ResultLeaseBinding
from .orchestration_api import ORCHESTRATION_OPERATIONS
from .orchestration_service import OrchestrationService
from .result_read_api import RESULT_READ_OPERATION, ResultReadRequest
from .result_read_service import ResultReadAuthorization, ResultReadService
from .service_transport import MAX_FRAME_BYTES


@dataclass(frozen=True)
class HistoryAuthorization:
    """Server adapter output, never decoded from a request or accepted as proof."""

    project_id: str
    runtime_digest: str
    request_digest: str
    source: str
    allowed_operations: frozenset[str]
    host_result_context: HostResultContext | None = None
    host_open_context: HostOpenContext | None = None
    worker_result_context: WorkerResultContext | None = None
    operational_qa_context: OperationalQAContext | None = None

    def __post_init__(self) -> None:
        if (
            not matches(self.project_id, _PROJECT) or not matches(self.runtime_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST) or type(self.source) is not str
            or self.source not in {"host", "hook", "app_server"}
            or type(self.allowed_operations) is not frozenset or not self.allowed_operations
            or any(type(operation) is not str for operation in self.allowed_operations)
            or not self.allowed_operations <= HISTORY_OPERATIONS
            or (self.host_result_context is not None and type(self.host_result_context) is not HostResultContext)
            or (self.host_open_context is not None and type(self.host_open_context) is not HostOpenContext)
            or (self.worker_result_context is not None and type(self.worker_result_context) is not WorkerResultContext)
            or (self.operational_qa_context is not None and type(self.operational_qa_context) is not OperationalQAContext)
        ):
            raise HistoryError("history_not_authorized")


def _metadata(entry: HistoryEntry) -> dict:
    return {name: getattr(entry, name) for name in (
        "record_id", "source", "source_sequence", "observed_at_ms", "ingested_at_ms", "content_digest", "late",
    )}


class HistoryService:
    """One connection-owning handler; intake, native authority and timers are separate.

    Neither the trusted factory nor authorizer can be selected in JSON. Default
    denial is deliberately not a usable native-enrollment implementation. A real
    front end must finish its bounded auth/business checks before writer work.
    Metadata and result reads share exactly one connection, with independent
    authorization adapters; live control/timers never belong on this owner loop.
    """

    def __init__(
        self, project_id: str, runtime_digest: str, open_history: Callable[[], ProjectHistory], *,
        authorizer: Callable[[HistoryRequest, object], HistoryAuthorization | None] | None = None,
        result_authorizer: Callable[[ResultReadRequest, object], ResultReadAuthorization | None] | None = None,
        orchestration_authorizer: Callable | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or not callable(open_history) or (authorizer is not None and not callable(authorizer))
            or (result_authorizer is not None and not callable(result_authorizer))
            or (orchestration_authorizer is not None and not callable(orchestration_authorizer))
            or (clock_ms is not None and not callable(clock_ms))
        ):
            raise HistoryError("history_invalid_input")
        self._project, self._runtime, self._open = project_id, runtime_digest, open_history
        self._authorizer, self._clock = authorizer, clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._store: ProjectHistory | None = None
        self._closed = False
        self._reader = ResultReadService(project_id, runtime_digest, self._history,
                                         authorizer=result_authorizer, close_history=False)
        self._orchestrations = OrchestrationService(project_id, runtime_digest, self._history,
                                                   authorizer=orchestration_authorizer, close_history=False)

    def _history(self) -> ProjectHistory:
        """Lend the same connection after each handler's own authorization.

        No normal client receives this callable or the connection. Both handlers
        run on the persistence owner's thread; live control remains elsewhere.
        """
        if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        if self._store is None:
            store = self._open()
            if not isinstance(store, ProjectHistory):
                raise HistoryError("history_service_unavailable")
            store._require_owner()
            if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
                store.close()
                raise HistoryError('history_wrong_owner')
            if store.project_id != self._project:
                store.close()
                raise HistoryError("history_service_unavailable")
            self._store = store
        return self._store

    @staticmethod
    def _wire_operation(raw: bytes) -> str | None:
        # This bounded peek selects a codec only. Each handler still validates
        # its entire closed frame and independently authorizes the operation.
        if type(raw) is not bytes or len(raw) + 1 > MAX_FRAME_BYTES:
            return None
        try:
            value = parse_bounded_json(raw, max_bytes=MAX_FRAME_BYTES - 1)
            operation = value.get("operation") if type(value) is dict else None
            return operation if type(operation) is str else None
        except Exception:
            return None

    def _reply(self, identity: str | None, *, request_digest: str | None = None,
               project_id: str | None = None, runtime_digest: str | None = None,
               result: dict | None = None, reason: str | None = None) -> bytes:
        return canonical_json({
            # Denials echo only validated caller labels; malformed requests get
            # null labels. No configured project/runtime is disclosed pre-auth.
            "protocol_version": HISTORY_PROTOCOL_VERSION, "project_id": project_id,
            "runtime_digest": runtime_digest, "request_id": identity,
            "request_digest": request_digest,
            "ready": reason is None, "result": result, "reason_code": reason,
        })

    def handle(self, raw: bytes, transport_context: object = None) -> bytes:
        identity = None
        digest = None
        project = runtime = None
        try:
            if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
                raise HistoryError("history_wrong_owner")
            operation = self._wire_operation(raw)
            if operation == RESULT_READ_OPERATION:
                return self._reader.handle(raw, transport_context)
            if operation in ORCHESTRATION_OPERATIONS:
                return self._orchestrations.handle(raw, transport_context)
            request = decode_history_request(raw)
            identity = request.request_id
            digest = history_request_digest(request)
            project, runtime = request.project_id, request.runtime_digest
            opened = (decode_host_open_submission(request.payload, project, runtime)
                      if request.operation == 'history.record_host_open' else None)
            if (
                request.project_id != self._project or request.runtime_digest != self._runtime
                or self._authorizer is None
            ):
                raise HistoryError("history_not_authorized")
            authorization = self._authorizer(request, transport_context)
            if type(authorization) is not HistoryAuthorization:
                raise HistoryError("history_not_authorized")
            authorization.__post_init__()
            if (
                authorization.project_id != self._project or authorization.runtime_digest != self._runtime
                or authorization.request_digest != digest or history_request_digest(request) != digest
                or request.operation not in authorization.allowed_operations
                or (authorization.source != "host" and request.operation != "history.record_event")
            ):
                raise HistoryError("history_not_authorized")
            if request.operation == 'history.qa_scope':
                if authorization.operational_qa_context is None:
                    raise HistoryError('history_not_authorized')
                original_context, captured = authorization.operational_qa_context.snapshot()
                if any(captured[name]['binding'] != request.payload[name + '_binding'] for name in ('worker', 'verifier')):
                    raise HistoryError('history_not_authorized')
                expected_qa_context = OperationalQAContext.from_snapshot(captured)
                worker_digest = request.payload['host_outcome_digest']
                verifier_digest = request.payload['verifier_host_outcome_digest']
                self._history()

                def authorize_qa():
                    self._history()
                    fresh = decode_history_request(raw)
                    current = self._authorizer(fresh, transport_context)
                    if type(current) is not HistoryAuthorization:
                        raise HistoryError('history_not_authorized')
                    current.__post_init__()
                    if (current.project_id != self._project or current.runtime_digest != self._runtime
                            or current.source != 'host' or current.request_digest != digest
                            or history_request_digest(fresh) != digest or 'history.qa_scope' not in current.allowed_operations
                            or current.operational_qa_context is None
                            or current.operational_qa_context.snapshot()[0] != original_context):
                        raise HistoryError('history_not_authorized')
                    self._history()._require_owner()

                result = self._store.operational_qa_scope(expected_qa_context, worker_digest, verifier_digest,
                                                        before_reply=authorize_qa)
            elif request.operation == 'history.worker_reference':
                if authorization.worker_result_context is None:
                    raise HistoryError('history_not_authorized')
                original_context, captured = authorization.worker_result_context.snapshot()
                if captured['binding'] != request.payload['binding']:
                    raise HistoryError('history_not_authorized')
                expected_binding = AttemptBinding(**captured['binding'])
                expected_context = WorkerResultContext(expected_binding, captured['task'])
                outcome_digest = request.payload['host_outcome_digest']
                self._history()

                def authorize_worker():
                    self._history()
                    fresh = decode_history_request(raw)
                    current = self._authorizer(fresh, transport_context)
                    if type(current) is not HistoryAuthorization:
                        raise HistoryError('history_not_authorized')
                    current.__post_init__()
                    if (current.project_id != self._project or current.runtime_digest != self._runtime
                            or current.source != 'host' or current.request_digest != digest
                            or history_request_digest(fresh) != digest
                            or 'history.worker_reference' not in current.allowed_operations
                            or current.worker_result_context is None
                            or current.worker_result_context.snapshot()[0] != original_context):
                        raise HistoryError('history_not_authorized')
                    self._history()._require_owner()

                result = self._store.verification_worker_reference(expected_context, outcome_digest,
                    before_reply=authorize_worker)
            elif request.operation == 'history.record_host_open':
                if authorization.host_open_context is None:
                    raise HistoryError('history_not_authorized')
                original_context, captured = authorization.host_open_context.snapshot()
                open_request, proof = opened
                expected_data = captured.pop('binding')
                if expected_data != proof['provenance']['binding']:
                    raise HistoryError('history_not_authorized')
                expected = ResultLeaseBinding(**expected_data)
                self._history()

                def authorize_open():
                    self._history()
                    fresh = decode_history_request(raw)
                    current = self._authorizer(fresh, transport_context)
                    if type(current) is not HistoryAuthorization:
                        raise HistoryError('history_not_authorized')
                    current.__post_init__()
                    if (current.project_id != self._project or current.runtime_digest != self._runtime
                            or current.source != 'host' or current.request_digest != digest
                            or history_request_digest(fresh) != digest
                            or 'history.record_host_open' not in current.allowed_operations
                            or current.host_open_context is None
                            or current.host_open_context.snapshot()[0] != original_context):
                        raise HistoryError('history_not_authorized')
                    self._history()._require_owner()

                entry = self._store.record_host_open(expected, open_request, proof, **captured, before_commit=authorize_open)
                result = {'record': parse_bounded_json(entry.content), 'metadata': _metadata(entry)}
            elif request.operation == "history.admit_host_result":
                if authorization.host_result_context is None:
                    raise HistoryError("history_not_authorized")
                original_context, captured = authorization.host_result_context.snapshot()
                reference = decode_host_result_reference(request.payload)
                if captured.pop("binding") != reference.binding.as_data():
                    raise HistoryError("history_not_authorized")
                self._history()

                def reauthorize():
                    # Recheck the independent current authority after body I/O.
                    # Immutable copies of the original inputs enter the writer;
                    # mutable adapter data cannot change what gets admitted.
                    current = self._authorizer(request, transport_context)
                    if type(current) is not HistoryAuthorization:
                        raise HistoryError("history_not_authorized")
                    current.__post_init__()
                    if (current.project_id != self._project or current.runtime_digest != self._runtime
                            or current.request_digest != digest or history_request_digest(request) != digest
                            or current.source != "host" or request.operation not in current.allowed_operations
                            or current.host_result_context is None
                            or current.host_result_context.snapshot()[0] != original_context):
                        raise HistoryError("history_not_authorized")

                result = self._store.admit_host_result(reference, **captured, before_commit=reauthorize)
            else:
                self._history()
                result = self._execute(request, authorization)
            reply = self._reply(identity, request_digest=digest, project_id=project, runtime_digest=runtime, result=result)
            if request.operation == "history.pending_terminals":
                # The owner caps the bare list; the wire envelope must fit too.
                while len(reply) + 1 > MAX_HISTORY_REPLY_BYTES and result["deliveries"]:
                    result["deliveries"].pop()
                    result["next_after_sequence"] = (
                        result["deliveries"][-1]["delivery_sequence"] if result["deliveries"] else None
                    )
                    reply = self._reply(identity, request_digest=digest, project_id=project, runtime_digest=runtime, result=result)
            limit = MAX_FRAME_BYTES if request.operation == "history.read_record" else MAX_HISTORY_REPLY_BYTES
            if len(reply) + 1 > limit:
                raise HistoryError("history_service_unavailable")
            decode_history_response(reply, request)
            if request.operation == 'history.record_host_open':
                # Same owner thread as close(); admit no success after an
                # adapter/factory/encoder has closed the borrowed writer.
                self._history()._require_owner()
            if request.operation == 'history.worker_reference':
                authorize_worker()
                from .project_worker_result import current_worker_reference
                current, _ = current_worker_reference(self._store, expected_context, outcome_digest)
                if current != result:
                    raise HistoryError('history_conflict')
            if request.operation == 'history.qa_scope':
                authorize_qa()
                from .project_operational_qa import current_qa_scope
                if current_qa_scope(self._store, expected_qa_context, worker_digest, verifier_digest) != result:
                    raise HistoryError('history_conflict')
            return reply
        except HistoryError as exc:
            reason = exc.reason_code
        except Exception:
            # A fault after commit still leaves its durable identity. An error
            # reply never proves the operation did not commit, and leaks no input.
            reason = "history_service_unavailable"
        return self._reply(identity, request_digest=digest, project_id=project, runtime_digest=runtime, reason=reason)

    def _execute(self, request: HistoryRequest, authorization: HistoryAuthorization) -> dict:
        operation, payload, store = request.operation, request.payload, self._store
        if operation in {"history.record_event", "history.record_intent", "history.record_outcome", "history.bind_verifier"}:
            return _metadata(store.append_record(canonical_json(payload["record"]), source=authorization.source,
                                                 ingested_at_ms=self._clock()))
        if operation == "history.commit_terminal":
            delivery = store.commit_terminal(canonical_json(payload["record"]), AttemptBinding(**payload["binding"]),
                                             ingested_at_ms=self._clock())
            return self._delivery_result(delivery)
        if operation == "history.acknowledge_terminal":
            store.acknowledge_terminal(payload["claim_id"], payload["record_content_digest"], payload["receipt_digest"],
                                       runtime_digest=request.runtime_digest)
            return {"recorded": True}
        if operation == "history.pending_terminals":
            deliveries = store.pending_terminals(after_sequence=payload["after_sequence"], limit=payload["limit"])
            return {"deliveries": [self._delivery_result(delivery) for delivery in deliveries],
                    "next_after_sequence": deliveries[-1].delivery_sequence if deliveries else None}
        entry = store.record(payload["record_id"])
        return {"record": parse_bounded_json(entry.content), "metadata": _metadata(entry)}

    def _delivery_result(self, delivery) -> dict:
        # Dispatch-result records contain only closed trusted metadata, including
        # validator reasons and fences; never a rejected model body. Return the
        # persisted bytes' value rather than an untrusted supplied summary.
        data = delivery.as_data()
        data["dispatch_result"] = parse_bounded_json(self._store.record(delivery.record_id).content)
        return data

    def close(self) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        if self._closed:
            return
        self._closed = True
        try:
            self._reader.close()
            self._orchestrations.close()
        finally:
            if self._store is not None:
                self._store.close()
