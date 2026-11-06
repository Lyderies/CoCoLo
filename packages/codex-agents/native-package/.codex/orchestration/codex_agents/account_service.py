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
from .learning_contracts import MAX_RECORD_BYTES, canonical_json
from .learning_store import (
    OWNER_SIGNER_LINE, OWNER_SIGNERS_MAX_BYTES, LearningLedger, LedgerError, OwnerSignerNotConfigured, StateError,
    _owner_key_identity, _signer_fingerprint,
)
from .owner_key_contracts import owner_public_key_from_line
from .sql_read_budget import SQLReadError, SQLReadLimits
from .signature_inputs import (
    SignatureCaptureError, SignatureCaptureLimits, SignatureInputSnapshot, signature_input_size,
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
        origin = self._origin
        current = getattr(self._ledger, "_connection_origin", None)
        if (
            type(origin) is not tuple or len(origin) != 2
            or type(current) is not tuple or len(current) != 2
            or type(origin[0]) is not int or origin[0] != os.getpid()
            or origin[1] is not threading.current_thread()
            or type(current[0]) is not int or current[0] != origin[0]
            or current[1] is not origin[1]
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
        return self._owner_key_view(project_id, include_public_key=False)

    def owner_public_key(self, project_id: str) -> dict:
        return self._owner_key_view(project_id, include_public_key=True)

    def capture_signature_inputs(
        self, project_id: str, provider: str, limits: SignatureCaptureLimits,
        work_limits: SQLReadLimits,
    ) -> SignatureInputSnapshot:
        """Capture unverified inputs within explicit field and SQL work limits.

        All project billing rows are charged before the legacy provider filter.
        The cooperative callback budget does not bound OS I/O or global job
        capacity. No RPC, worker or installed defaults are activated here.
        """
        self._guard()
        if (type(provider) is not str or provider not in {"codex", "claude"}
                or type(limits) is not SignatureCaptureLimits or type(work_limits) is not SQLReadLimits):
            raise SignatureCaptureError("signature_capture_invalid_request")
        limits.__post_init__()
        reason = "signature_capture_unavailable"
        try:
            work_limits.__post_init__()
            with self._ledger._lock:
                if self._ledger._connection.in_transaction:
                    raise SignatureCaptureError("signature_capture_unavailable")
                return self._ledger._connection.read_call(
                    work_limits, lambda: self._capture_signature_inputs_locked(project_id, provider, limits),
                )
        except (AccountError, SignatureCaptureError):
            if getattr(self._ledger._connection, "_sql_unusable", False) is not True:
                raise
        except SQLReadError as error:
            if error.reason_code == "sql_read_invalid_limits":
                reason = "signature_capture_invalid_request"
            elif error.reason_code in {"sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit", "sql_read_busy"}:
                reason = "signature_capture_work_limit"
        except Exception:
            pass
        # Outside every handler: no raw SQL/path exception cause or context.
        raise SignatureCaptureError(reason)

    def _capture_signature_inputs_locked(
        self, project_id: str, provider: str, limits: SignatureCaptureLimits,
    ) -> SignatureInputSnapshot:
        self._ledger.state.harden_database_files()
        self._project_locked(project_id)
        # Validate stored owner metadata before the legacy helper can
        # fetch its signer BLOB. Non-STRICT integer columns must never
        # return variable TEXT/BLOB values as preliminary metadata.
        owner_header = self._ledger._connection.execute(
            "SELECT CASE WHEN typeof(sequence)='integer' THEN sequence END,"
            "typeof(signer_line),length(signer_line),"
            "CASE reason WHEN 'enrolled' THEN 'enrolled' WHEN 'replaced' THEN 'replaced' END,"
            "CASE WHEN typeof(recorded_at_ms)='integer' THEN recorded_at_ms END "
            "FROM owner_key_history ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        minimum = signature_input_size(project_id, provider, None, None, ())
        if owner_header is not None:
            sequence, line_type, line_bytes, reason, timestamp = owner_header
            if (
                type(sequence) is not int or sequence < 1 or line_type != "blob"
                or type(line_bytes) is not int or not 0 < line_bytes <= OWNER_SIGNERS_MAX_BYTES
                or reason not in {"enrolled", "replaced"}
                or type(timestamp) is not int or timestamp < 0
            ):
                raise SignatureCaptureError("signature_capture_incompatible")
            minimum += 16 + line_bytes + len(reason)
        if minimum > limits.max_bytes:
            raise SignatureCaptureError("signature_capture_limit")
        try:
            owner = self._ledger._owner_generation_locked()
        except LedgerError:
            incompatible = True
        else:
            incompatible = False
        if incompatible:
            raise SignatureCaptureError("signature_capture_incompatible")
        signer = self._ledger._current_signer_bytes()
        size = signature_input_size(project_id, provider, owner, signer, ())
        if size > limits.max_bytes:
            raise SignatureCaptureError("signature_capture_limit")
        # Only type/length/integer metadata is returned. In particular,
        # no JSON expression or content/signature value is selected.
        # LIMIT bounds returned metadata, not the underlying scan work.
        headers = self._ledger._connection.execute(
            "SELECT typeof(r.record_id),length(CAST(r.record_id AS BLOB)),"
            "typeof(r.content),length(r.content),"
            "typeof(r.content_digest),length(CAST(r.content_digest AS BLOB)),"
            "typeof(r.source),length(CAST(r.source AS BLOB)),"
            "CASE WHEN typeof(r.source_sequence)='integer' THEN r.source_sequence END,"
            "typeof(a.signature),length(a.signature) "
            "FROM records r LEFT JOIN owner_attestations a "
            "ON a.project_id=r.project_id AND a.subject_record_id=r.record_id "
            "WHERE r.project_id=? AND r.record_type='billing_attestation' "
            "ORDER BY r.source_sequence LIMIT ?",
            (project_id, limits.max_rows + 1),
        ).fetchall()
        if len(headers) > limits.max_rows:
            raise SignatureCaptureError("signature_capture_limit")
        for header in headers:
            identifier_type, identifier_bytes, content_type, content_bytes, digest_type, digest_bytes, \
                source_type, source_bytes, sequence, signature_type, signature_bytes = header
            if (
                identifier_type != "text" or content_type != "blob" or digest_type != "text" or source_type != "text"
                or any(type(value) is not int or value < 0
                       for value in (identifier_bytes, content_bytes, digest_bytes, source_bytes))
                or not 0 < content_bytes <= MAX_RECORD_BYTES or digest_bytes != 64
                or type(sequence) is not int or sequence < 1
                or (signature_type == "null" and signature_bytes is not None)
                or (signature_type == "blob" and
                    (type(signature_bytes) is not int or not 0 <= signature_bytes <= 16_384))
                or signature_type not in {"null", "blob"}
            ):
                raise SignatureCaptureError("signature_capture_incompatible")
            size += 8 + identifier_bytes + content_bytes + digest_bytes + source_bytes + (signature_bytes or 0)
            if size > limits.max_bytes:
                raise SignatureCaptureError("signature_capture_limit")
        try:
            rows = self._ledger._attestation_rows_locked(project_id, provider)
        except LedgerError:
            incompatible = True
        else:
            incompatible = False
        if incompatible:
            raise SignatureCaptureError("signature_capture_incompatible")
        retained = signature_input_size(project_id, provider, owner, signer, rows)
        if len(rows) > limits.max_rows or retained > size or retained > limits.max_bytes:
            raise SignatureCaptureError("signature_capture_incompatible")
        return SignatureInputSnapshot(
            self._ledger._attestation_origin, project_id, provider, owner, signer, rows, retained,
        )

    def _owner_key_view(self, project_id: str, *, include_public_key: bool) -> dict:
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
            try:
                signer = self._ledger.state._owner_signer_bytes()
            except (FileNotFoundError, OwnerSignerNotConfigured):
                signer, unconfigured = None, True
            except (StateError, OSError):
                signer, unconfigured = None, False
            else:
                unconfigured = False
            if owner is None:
                if count:
                    raise AccountError("account_incompatible")
                result = {"state": "not_configured" if unconfigured else "mismatch", "fingerprint": None,
                          "sequence": 0, "replacements": 0, "last_replaced_at_ms": None}
                if include_public_key:
                    result["public_key"] = None
                return result
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
            result = {"state": "ok" if signer == owner[1] else "mismatch", "fingerprint": fingerprint,
                      "sequence": owner[0], "replacements": count, "last_replaced_at_ms": last}
            if include_public_key:
                result["public_key"] = None
                if result["state"] == "ok":
                    try:
                        value, public_fingerprint = owner_public_key_from_line(owner[1])
                    except ValueError:
                        raise AccountError("account_incompatible") from None
                    if public_fingerprint != fingerprint:
                        raise AccountError("account_incompatible")
                    result["public_key"] = value
            return result


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
                or (request.operation == "account.owner_public_key" and authorization.source != "owner")
            ):
                raise AccountError("account_not_authorized")
            if request.operation == "account.health":
                result = {"transport_alive": True, "database_checked": False}
            elif request.operation == "account.project":
                result = self._views.project(request.project_id)
            elif request.operation == "account.owner_public_key":
                result = self._views.owner_public_key(request.project_id)
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
