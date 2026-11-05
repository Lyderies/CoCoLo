"""Closed history-owner handler with no enabled native endpoint.

This runs on the persistence owner's thread, outside local control/timer processes.
The default authorization adapter denies without opening a store. A real adapter
must independently authenticate and authorize the entire operation, including
session/generation/claim and receipt semantics; a role string or this dataclass is
not proof. Disposable process fixtures may supply explicitly fake server context.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .history_api import (
    HISTORY_OPERATIONS, HISTORY_PROTOCOL_VERSION, MAX_HISTORY_REPLY_BYTES,
    HistoryRequest, decode_history_request, decode_history_response, history_request_digest,
)
from .history_contracts import AttemptBinding, HistoryEntry, HistoryError, _DIGEST, _PROJECT, matches
from .learning_contracts import canonical_json, parse_bounded_json
from .project_history import ProjectHistory
from .service_transport import MAX_FRAME_BYTES


@dataclass(frozen=True)
class HistoryAuthorization:
    """Server adapter output, never decoded from a request or accepted as proof."""

    project_id: str
    runtime_digest: str
    request_digest: str
    source: str
    allowed_operations: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not matches(self.project_id, _PROJECT) or not matches(self.runtime_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST) or type(self.source) is not str
            or self.source not in {"host", "hook", "app_server"}
            or type(self.allowed_operations) is not frozenset or not self.allowed_operations
            or any(type(operation) is not str for operation in self.allowed_operations)
            or not self.allowed_operations <= HISTORY_OPERATIONS
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
    """

    def __init__(
        self, project_id: str, runtime_digest: str, open_history: Callable[[], ProjectHistory], *,
        authorizer: Callable[[HistoryRequest, object], HistoryAuthorization | None] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or not callable(open_history) or (authorizer is not None and not callable(authorizer))
            or (clock_ms is not None and not callable(clock_ms))
        ):
            raise HistoryError("history_invalid_input")
        self._project, self._runtime, self._open = project_id, runtime_digest, open_history
        self._authorizer, self._clock = authorizer, clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._thread = threading.get_ident()
        self._store: ProjectHistory | None = None
        self._closed = False

    def _reply(self, identity: str | None, *, result: dict | None = None, reason: str | None = None) -> bytes:
        return canonical_json({
            "protocol_version": HISTORY_PROTOCOL_VERSION, "project_id": self._project,
            "runtime_digest": self._runtime, "request_id": identity,
            "ready": reason is None, "result": result, "reason_code": reason,
        })

    def handle(self, raw: bytes, transport_context: object = None) -> bytes:
        identity = None
        try:
            if self._closed or self._thread != threading.get_ident():
                raise HistoryError("history_wrong_owner")
            request = decode_history_request(raw)
            identity = request.request_id
            if (
                request.project_id != self._project or request.runtime_digest != self._runtime
                or self._authorizer is None
            ):
                raise HistoryError("history_not_authorized")
            digest = history_request_digest(request)
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
            if self._store is None:
                store = self._open()
                if not isinstance(store, ProjectHistory) or store.project_id != self._project:
                    if isinstance(store, ProjectHistory):
                        store.close()
                    raise HistoryError("history_service_unavailable")
                self._store = store
            result = self._execute(request, authorization)
            reply = self._reply(identity, result=result)
            if request.operation == "history.pending_terminals":
                # The owner caps the bare list; the wire envelope must fit too.
                while len(reply) + 1 > MAX_HISTORY_REPLY_BYTES and result["deliveries"]:
                    result["deliveries"].pop()
                    result["next_after_sequence"] = (
                        result["deliveries"][-1]["delivery_sequence"] if result["deliveries"] else None
                    )
                    reply = self._reply(identity, result=result)
            limit = MAX_FRAME_BYTES if request.operation == "history.read_record" else MAX_HISTORY_REPLY_BYTES
            if len(reply) + 1 > limit:
                raise HistoryError("history_service_unavailable")
            decode_history_response(reply, request)
            return reply
        except HistoryError as exc:
            reason = exc.reason_code
        except Exception:
            # A fault after commit still leaves its durable identity. An error
            # reply never proves the operation did not commit, and leaks no input.
            reason = "history_service_unavailable"
        return self._reply(identity, reason=reason)

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
            store.acknowledge_terminal(payload["claim_id"], payload["record_content_digest"], payload["receipt_digest"])
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
        if self._thread != threading.get_ident():
            raise HistoryError("history_wrong_owner")
        self._closed = True
        if self._store is not None:
            self._store.close()
