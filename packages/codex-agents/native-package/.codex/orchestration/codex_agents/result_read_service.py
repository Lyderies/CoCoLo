"""Connection-owner result reads; default denial, no enabled native endpoint.

The authorizer is a trusted adapter supplied by the future native front end. It
must prove the current owning primary and the complete operation after I/O too.
Neither its dataclass nor JSON labels are an implementation of that proof.
"""

from __future__ import annotations

import base64
import os
import threading
from dataclasses import dataclass
from typing import Callable

from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _PROJECT, matches
from .learning_contracts import canonical_json
from .project_history import ProjectHistory
from .result_read_api import (
    MAX_RESULT_READ_FRAME_BYTES, RESULT_CHUNK_BYTES, RESULT_READ_PROTOCOL_VERSION,
    ResultReadRequest, decode_result_read_request, decode_result_read_response, result_read_request_digest,
)


@dataclass(frozen=True)
class ResultReadAuthorization:
    """Trusted adapter output for this one read; grants no publication authority."""

    binding: AttemptBinding
    result_digest: str
    request_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not AttemptBinding or not matches(self.result_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST)
        ):
            raise HistoryError("history_not_authorized")
        self.binding.as_data()


class ResultReadService:
    def __init__(
        self, project_id: str, runtime_digest: str, open_history: Callable[[], ProjectHistory], *,
        authorizer: Callable[[ResultReadRequest, object], ResultReadAuthorization | None] | None = None,
        close_history: bool = True,
    ) -> None:
        if (
            not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or not callable(open_history) or (authorizer is not None and not callable(authorizer))
            or type(close_history) is not bool
        ):
            raise HistoryError("history_invalid_input")
        self._project, self._runtime, self._open = project_id, runtime_digest, open_history
        self._authorizer = authorizer
        # A composite project owner may lend its one connection. Lifetime policy
        # is supplied only by trusted construction, never by request JSON.
        self._close_history = close_history
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._store: ProjectHistory | None = None
        self._closed = False

    def _authorize(self, request: ResultReadRequest, context: object, digest: str) -> None:
        if (
            request.binding.project_id != self._project or request.binding.runtime_digest != self._runtime
            or self._authorizer is None
        ):
            raise HistoryError("history_not_authorized")
        authorization = self._authorizer(request, context)
        if type(authorization) is not ResultReadAuthorization:
            raise HistoryError("history_not_authorized")
        authorization.__post_init__()
        if (
            authorization.binding.as_data() != request.binding.as_data()
            or authorization.result_digest != request.result_digest
            or authorization.request_digest != digest or result_read_request_digest(request) != digest
        ):
            raise HistoryError("history_not_authorized")

    @staticmethod
    def _reply(request: ResultReadRequest | None, digest: str | None, *, result=None, reason=None) -> bytes:
        return canonical_json({
            "protocol_version": RESULT_READ_PROTOCOL_VERSION,
            "request_id": request.request_id if request is not None else None,
            "request_digest": digest, "binding": request.binding.as_data() if request is not None else None,
            "ready": reason is None, "result": result, "reason_code": reason,
        })

    def _history(self) -> ProjectHistory:
        if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        if self._store is None or not self._close_history:
            store = self._open()
            if (not isinstance(store, ProjectHistory) or store.project_id != self._project
                    or (self._store is not None and store is not self._store)):
                if isinstance(store, ProjectHistory) and self._close_history:
                    store.close()
                raise HistoryError("history_service_unavailable")
            store._require_owner()
            if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
                if self._close_history:
                    store.close()
                raise HistoryError("history_wrong_owner")
            self._store = store
        self._store._require_owner()
        return self._store

    def handle(self, raw: bytes, transport_context: object = None) -> bytes:
        request = None
        digest = None
        try:
            if self._closed or self._pid != os.getpid() or self._thread is not threading.current_thread():
                raise HistoryError("history_wrong_owner")
            request = decode_result_read_request(raw)
            digest = result_read_request_digest(request)
            self._authorize(request, transport_context, digest)
            body = self._history().read_result(request.binding)
            reservation = self._store.result_reservation(request.binding)
            if reservation.state != "committed":
                raise HistoryError("history_result_expired")
            if reservation.result_digest != request.result_digest:
                raise HistoryError("history_conflict")
            if request.offset >= len(body):
                raise HistoryError("history_invalid_request")
            result = {
                "result_digest": reservation.result_digest, "body_size": reservation.body_size,
                "terminal_record_id": reservation.terminal_record_id, "expires_at_ms": reservation.expires_at_ms,
                "offset": request.offset,
                "data_base64": base64.b64encode(body[request.offset:request.offset + RESULT_CHUNK_BYTES]).decode("ascii"),
            }
            reply = self._reply(request, digest, result=result)
            if len(reply) + 1 > MAX_RESULT_READ_FRAME_BYTES:
                raise HistoryError("history_service_unavailable")
            decode_result_read_response(reply, request)
            self._authorize(request, transport_context, digest)
            # Never release cached bytes after observed retirement, expiry or
            # changed metadata during body I/O, encoding or authorization work.
            if self._history().result_reservation(request.binding) != reservation:
                raise HistoryError("history_result_expired")
            return reply
        except HistoryError as exc:
            reason = exc.reason_code
        except Exception:
            reason = "history_service_unavailable"
        # An adapter may have corrupted its request object. Re-decode only the
        # bounded original wire labels for a fixed denial; never serialize it.
        try:
            request = decode_result_read_request(raw)
            digest = result_read_request_digest(request)
        except HistoryError:
            request = None
            digest = None
        return self._reply(request, digest, reason=reason)

    def close(self) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        self._closed = True
        if self._store is not None and self._close_history:
            self._store.close()
