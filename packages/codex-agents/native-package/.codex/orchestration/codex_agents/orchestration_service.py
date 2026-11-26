"""Default-deny project lifecycle adapter on the existing persistence owner.

No native listener is enabled. The authorizer must independently prove the actual
primary, current permissions/epochs and entire requested operation; neither its
output nor request scope/role labels are native proof. Slow enrollment belongs
outside this owner, with independently revocable bounded context.
"""
from dataclasses import dataclass
import os
import threading

from .history_contracts import HistoryError, _DIGEST, _PROJECT, matches
from .learning_contracts import canonical_json
from .orchestration_api import (
    MAX_ORCHESTRATION_FRAME_BYTES, OrchestrationScope, ORCHESTRATION_OPERATIONS, HOST_CLIENT_VERSIONS,
    decode_orchestration_request, decode_orchestration_response, encode_orchestration_request,
    orchestration_request_digest,
)
from .orchestration_store import OrchestrationOwner
from .project_history import ProjectHistory


@dataclass(frozen=True)
class OrchestrationAuthorization:
    scope: OrchestrationScope
    request_digest: str
    client_version: str
    allowed_operations: frozenset

    def __post_init__(self):
        # Shape only. A well-formed value is adapter output, never native proof.
        if (type(self.scope) is not OrchestrationScope or not matches(self.request_digest, _DIGEST)
                or type(self.client_version) is not str or self.client_version not in HOST_CLIENT_VERSIONS
                or type(self.allowed_operations) is not frozenset
                or any(type(name) is not str for name in self.allowed_operations)
                or not self.allowed_operations <= ORCHESTRATION_OPERATIONS):
            raise HistoryError('history_not_authorized')
        self.scope.as_data()


class OrchestrationService:
    def __init__(self, project_id, runtime_digest, open_history, *, authorizer=None, close_history=True):
        if (not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST) or not callable(open_history)
                or (authorizer is not None and not callable(authorizer)) or type(close_history) is not bool):
            raise HistoryError('history_invalid_input')
        self._project, self._runtime = project_id, runtime_digest
        self._open, self._authorizer, self._close_history = open_history, authorizer, close_history
        self._store = None
        self._pid, self._thread, self._closed = os.getpid(), threading.current_thread(), False

    def _owner(self):
        return self._pid == os.getpid() and self._thread is threading.current_thread()

    def _authorize(self, encoded, context, digest):
        # Default deny. Every adapter call gets a fresh copy of the immutable
        # canonical request, so its mutation cannot reach persistence.
        request = decode_orchestration_request(encoded)
        if (self._closed or not self._owner()
                or request.scope.project_id != self._project
                or self._authorizer is None):
            raise HistoryError('history_not_authorized')
        denied = False
        try:
            auth = self._authorizer(request, context)
            if type(auth) is not OrchestrationAuthorization:
                raise HistoryError('history_not_authorized')
            auth.__post_init__()
            expected = decode_orchestration_request(encoded)
            # The adapter must independently expect the full scope and digest.
            if (self._closed or not self._owner()
                    or canonical_json(auth.scope.as_data()) != canonical_json(expected.scope.as_data())
                    or auth.request_digest != digest or orchestration_request_digest(expected) != digest
                    or orchestration_request_digest(request) != digest
                    or expected.operation not in auth.allowed_operations):
                raise HistoryError('history_not_authorized')
            client = auth.client_version
            if request.scope.runtime_digest != self._runtime:
                from .project_takeover import adopted_orchestration_scope
                if (request.operation == 'orchestration.open' or not adopted_orchestration_scope(
                        self._history(), request.scope, request.payload.get('orchestration_handle'), client)):
                    raise HistoryError('history_not_authorized')
        except Exception:
            denied = True
        if denied:
            raise HistoryError('history_not_authorized')
        return client

    def _history(self):
        # An owned store opens once, only after authorization. A borrowed
        # factory is consulted on every call so its owner's closure applies.
        if self._closed or not self._owner():
            raise HistoryError('history_wrong_owner')
        if self._store is None or not self._close_history:
            store = self._open()
            if type(store) is not ProjectHistory:
                raise HistoryError('history_service_unavailable')
            if self._closed or not self._owner():
                if self._close_history:
                    store.close()
                raise HistoryError('history_wrong_owner')
            if store.project_id != self._project or (self._store is not None and store is not self._store):
                if self._close_history:
                    store.close()
                raise HistoryError('history_service_unavailable')
            self._store = store
        self._store._require_owner()
        return self._store

    @staticmethod
    def _reply(request, digest, *, result=None, reason=None):
        return canonical_json({'protocol_version': 1, 'request_id': None if request is None else request.request_id,
                               'scope': None if request is None else request.scope.as_data(), 'request_digest': digest,
                               'ready': reason is None, 'result': result, 'reason_code': reason})

    def handle(self, raw, transport_context=None):
        request, digest = None, None
        try:
            # Copy, validate and digest the bounded input before owner work.
            decoded = decode_orchestration_request(raw)
            encoded = encode_orchestration_request(decoded)
            request, digest = decoded, orchestration_request_digest(decoded)
            if self._closed or not self._owner():
                raise HistoryError('history_wrong_owner')
            client = self._authorize(encoded, transport_context, digest)
            store = self._history()

            def reauthorize():
                # Current scoped revocation check before each commit or reply.
                if (self._closed or not self._owner()
                        or self._authorize(encoded, transport_context, digest) != client):
                    raise HistoryError('history_not_authorized')

            result = OrchestrationOwner(store).apply(decode_orchestration_request(encoded), client_version=client,
                                                     before_commit=reauthorize)
            if self._closed or not self._owner():
                raise HistoryError('history_wrong_owner')
            reply = self._reply(request, digest, result=result)
            if len(reply) + 1 > MAX_ORCHESTRATION_FRAME_BYTES:
                raise HistoryError('history_service_unavailable')
            try:
                decode_orchestration_response(reply, request)
            except HistoryError:
                raise HistoryError('history_service_unavailable') from None
            return reply
        except HistoryError as exc:
            reason = exc.reason_code
        except Exception:
            # A fault after commit never proves the operation did not commit.
            reason = 'history_service_unavailable'
        return self._reply(request, digest, reason=reason)

    def close(self):
        if not self._owner():
            raise HistoryError('history_wrong_owner')
        if self._closed:
            return
        self._closed = True
        store, self._store = self._store, None
        if store is not None and self._close_history:
            store.close()
