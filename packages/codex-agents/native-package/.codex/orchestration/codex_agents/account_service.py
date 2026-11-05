"""Default-deny account read handler over one already opened account owner.

This module is a persistence-owner component, never a client dependency. Native
caller enrollment and endpoint identity are unimplemented gates, not a socket,
UID, role string or the authorization dataclass below. No production listener or
normal-client migration is enabled here.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable

from .account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, MAX_ACCOUNT_FRAME_BYTES,
    AccountError, AccountRequest, _DIGEST, _PROJECT, account_request_digest,
    decode_account_request, decode_account_response, integer, matches,
)
from .learning_contracts import canonical_json
from .learning_store import (
    OWNER_SIGNER_LINE, LearningLedger, LedgerError, _owner_key_identity, _signer_fingerprint,
)


@dataclass(frozen=True)
class AccountAuthorization:
    """Trusted adapter output; never decoded from JSON or accepted as proof."""

    project_id: str
    runtime_digest: str
    request_digest: str
    source: str
    allowed_operations: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not matches(self.project_id, _PROJECT) or not matches(self.runtime_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST) or type(self.source) is not str
            or self.source not in {"host", "owner", "hook", "app_server"}
            or type(self.allowed_operations) is not frozenset or not self.allowed_operations
            or any(type(operation) is not str for operation in self.allowed_operations)
            or not self.allowed_operations <= ACCOUNT_OPERATIONS
        ):
            raise AccountError("account_not_authorized")


class AccountOwnerViews:
    """Read views over ONE existing global owner; no constructors or migrations.

    The launcher owns this ledger's lifetime. These methods never create another
    LearningState, key, writer or read-only connection and cannot mutate/register.
    Only the owning process/thread may access its SQL.
    """

    def __init__(self, ledger: LearningLedger) -> None:
        if not isinstance(ledger, LearningLedger):
            raise AccountError("account_incompatible")
        self._ledger = ledger
        self._origin = getattr(ledger, "_connection_origin", None)
        self._guard()

    def _guard(self) -> None:
        if (
            self._origin != (os.getpid(), threading.current_thread())
            or self._origin != self._ledger._connection_origin
        ):
            raise AccountError("account_wrong_owner")

    def _project_locked(self, project_id: str) -> dict:
        if not matches(project_id, _PROJECT):
            raise AccountError("account_invalid_request")
        row = self._ledger._connection.execute(
            "SELECT status,created_at_ms FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if row is None or row["status"] != "active":
            raise AccountError("account_project_not_found")
        if not integer(row["created_at_ms"]):
            raise AccountError("account_incompatible")
        return {"project_id": project_id, "status": "active", "created_at_ms": row["created_at_ms"]}

    def project(self, project_id: str) -> dict:
        self._guard()
        with self._ledger._lock:
            self._ledger.state.harden_database_files()
            return self._project_locked(project_id)

    def owner_key(self, project_id: str) -> dict:
        self._guard()
        with self._ledger._lock:
            self._ledger.state.harden_database_files()
            self._project_locked(project_id)
            try:
                owner = self._ledger._owner_generation_locked()
            except LedgerError:
                incompatible = True
            else:
                incompatible = False
            if incompatible:
                raise AccountError("account_incompatible")
            row = self._ledger._connection.execute(
                "SELECT COUNT(*),MAX(recorded_at_ms) FROM owner_key_history WHERE reason='replaced'"
            ).fetchone()
            count, last = row[0], row[1]
            if not integer(count) or (last is not None and not integer(last)) or (count == 0) != (last is None):
                raise AccountError("account_incompatible")
            signer = self._ledger._current_signer_bytes()
            if owner is None:
                if count:
                    raise AccountError("account_incompatible")
                return {"state": "not_configured" if signer is None else "mismatch", "fingerprint": None,
                        "sequence": 0, "replacements": 0, "last_replaced_at_ms": None}
            try:
                if OWNER_SIGNER_LINE.fullmatch(owner[1]) is None:
                    raise ValueError("invalid canonical key")
                _owner_key_identity(owner[1])
                fingerprint = _signer_fingerprint(owner[1])
            except Exception:
                invalid = True
            else:
                invalid = False
            if invalid or count > owner[0]:
                raise AccountError("account_incompatible")
            return {"state": "ok" if signer == owner[1] else "mismatch", "fingerprint": fingerprint,
                    "sequence": owner[0], "replacements": count, "last_replaced_at_ms": last}


class AccountService:
    """Closed read dispatcher; health never calls the persistence backend.

    The preopened owner is supplied by trusted startup, not constructed on a
    request. A native adapter must verify fresh caller/project/session/generation
    and operation scope independently. Default denial grants no usable endpoint.
    Closing this handler never closes the shared account owner.
    """

    def __init__(
        self, runtime_digest: str, views: AccountOwnerViews, *,
        authorizer: Callable[[AccountRequest, object], AccountAuthorization | None] | None = None,
    ) -> None:
        if not matches(runtime_digest, _DIGEST) or not isinstance(views, AccountOwnerViews) or (authorizer is not None and not callable(authorizer)):
            raise AccountError("account_invalid_request")
        self._runtime, self._views, self._authorizer = runtime_digest, views, authorizer
        self._pid = os.getpid()
        self._closed = False
        self._front_lock = threading.Lock()

    @staticmethod
    def _reply(labels: dict, *, result: dict | None = None, reason: str | None = None) -> bytes:
        return canonical_json({
            "protocol_version": ACCOUNT_PROTOCOL_VERSION,
            **labels,
            "ready": reason is None, "result": result, "reason_code": reason,
        })

    def handle(self, raw: bytes, transport_context: object = None) -> bytes:
        labels = {"request_id": None, "project_id": None, "runtime_digest": None, "request_digest": None}
        try:
            request = decode_account_request(raw)
            digest = account_request_digest(request)
            labels = {"request_id": request.request_id, "project_id": request.project_id,
                      "runtime_digest": request.runtime_digest, "request_digest": digest}
            if self._pid != os.getpid():
                raise AccountError("account_wrong_owner")
            with self._front_lock:
                closed = self._closed
            if closed:
                raise AccountError("account_unavailable")
            if request.runtime_digest != self._runtime or self._authorizer is None:
                raise AccountError("account_not_authorized")
            authorization = self._authorizer(request, transport_context)
            if type(authorization) is not AccountAuthorization:
                raise AccountError("account_not_authorized")
            authorization.__post_init__()
            try:
                current_digest = account_request_digest(request)
            except AccountError:
                current_digest = None
            if (
                authorization.project_id != request.project_id or authorization.runtime_digest != self._runtime
                or authorization.request_digest != digest or current_digest != digest
                or request.operation not in authorization.allowed_operations
                or (authorization.source in {"hook", "app_server"} and request.operation != "account.health")
            ):
                raise AccountError("account_not_authorized")
            if request.operation == "account.health":
                result = {"transport_alive": True, "database_checked": False}
            elif request.operation == "account.project":
                result = self._views.project(request.project_id)
            else:
                result = self._views.owner_key(request.project_id)
            reply = self._reply(labels, result=result)
            if len(reply) + 1 > MAX_ACCOUNT_FRAME_BYTES:
                raise AccountError("account_incompatible")
            decode_account_response(reply, request)
            return reply
        except AccountError as exc:
            reason = exc.reason_code
        except Exception:
            reason = "account_unavailable"
        return self._reply(labels, reason=reason)

    def close(self) -> None:
        if self._pid != os.getpid():
            raise AccountError("account_wrong_owner")
        with self._front_lock:
            self._closed = True
