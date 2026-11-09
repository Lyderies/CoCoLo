"""Default-deny account metadata handler over one already opened account owner.

This module is a persistence-owner component, never a client dependency. Native
caller enrollment and endpoint identity are unimplemented gates, not a socket,
UID, role string or the authorization dataclass below. No production listener or
normal-client migration is enabled here.
"""

from __future__ import annotations

import os
import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable

from .account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, MAX_ACCOUNT_FRAME_BYTES,
    AccountError, AccountRequest, _DIGEST, _PROJECT, account_request_digest,
    decode_account_request, decode_account_response, integer, matches,
)
from .admission_inputs import (
    ADMISSION_CAPTURE_RECORD_TYPES, AdmissionCaptureError, AdmissionCaptureLimits,
    AdmissionInputPolicy, AdmissionInputSnapshot, admission_input_size,
)
from .learning_contracts import MAX_RECORD_BYTES, canonical_json
from .learning_store import (
    COLLECTOR_RUNTIME_DIGEST, OWNER_SIGNER_LINE, OWNER_SIGNERS_MAX_BYTES,
    LearningLedger, LedgerError, OwnerSignerNotConfigured, StateError,
    _owner_key_identity, _signer_fingerprint,
)
from .owner_key_contracts import owner_public_key_from_line
from .sql_read_budget import SQLReadError, SQLReadLimits
from .signature_inputs import (
    SignatureCaptureError, SignatureCaptureLimits, SignatureInputSnapshot, signature_input_size,
)
from .account_claim_contracts import ClaimError, LaunchClaimInput, decode_claim_input
from .capacity_status_contracts import CapacityStatusInput, decode_capacity_status_input
from .run_provenance import decode_run_provenance_input

# Fixed owner query: do not parse JSON or select variable record values during
# preliminary quota checks. Foreign-account/provider candidates are charged too.
_ADMISSION_INPUT_SCOPE = (
    "((r.project_id=? AND r.record_type IN ('provider_identity','billing_attestation','admission_decision')) "
    "OR (r.source='provider' AND r.record_type IN ('provider_billing','provider_headroom')) "
    "OR (r.source='lifecycle' AND r.record_type IN ('admission_decision','probe_admission')))"
)
_ADMISSION_INPUT_JOIN = (
    " FROM records r LEFT JOIN owner_attestations a "
    "ON a.project_id=r.project_id AND a.subject_record_id=r.record_id WHERE "
    + _ADMISSION_INPUT_SCOPE + " ORDER BY r.rowid LIMIT ?"
)


@dataclass(frozen=True)
class AccountAuthorization:
    """Trusted adapter output; never decoded from JSON or accepted as proof.

    Storage scope is derived independently from enrolled supervisor ownership;
    sealed evidence comes from its protected project writer. The data/HMAC do
    not establish native provenance, current permissions or caller identity.
    """

    project_id: str
    runtime_digest: str
    request_digest: str
    source: str
    allowed_operations: frozenset[str]
    caller_scope: str | None = None
    storage_binding: object = None
    storage_evidence: bytes | None = None
    terminal_evidence: bytes | None = None

    def __post_init__(self) -> None:
        if (
            not matches(self.project_id, _PROJECT) or not matches(self.runtime_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST) or type(self.source) is not str
            or self.source not in {"host", "owner", "hook", "app_server", "project"}
            or type(self.allowed_operations) is not frozenset or not self.allowed_operations
            or any(type(operation) is not str for operation in self.allowed_operations)
            or not self.allowed_operations <= ACCOUNT_OPERATIONS
            or (self.caller_scope is not None and not matches(self.caller_scope, _DIGEST))
        ):
            raise AccountError("account_not_authorized")
        from .claim_storage import capture_storage_evidence

        invalid = False
        try:
            capture_storage_evidence(self.storage_binding, self.storage_evidence)
            from .terminal_delivery import capture_terminal_evidence
            capture_terminal_evidence(self.terminal_evidence)
        except ClaimError:
            invalid = True
        if invalid:
            raise AccountError("account_not_authorized")


class AccountOwnerViews:
    """Views and closed claim metadata over ONE existing global owner.

    The launcher owns this ledger's lifetime. These methods never create another
    LearningState, key, writer or read-only connection and cannot register/migrate.
    Only the owning process/thread may access its SQL.
    """

    def __init__(self, ledger: LearningLedger) -> None:
        if not isinstance(ledger, LearningLedger):
            raise AccountError("account_incompatible")
        self._ledger = ledger
        self._origin = getattr(ledger, "_connection_origin", None)
        self._guard()
        self._admission_capture_origin = object()

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

    def claim_launch(self, project_id: str, *, runtime_digest: str, caller_scope: str,
                     request_id: str, launch: LaunchClaimInput, storage_required=False,
                     storage_binding=None, storage_evidence=None) -> dict:
        self._guard()
        return self._ledger.claim_launch(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                         request_id=request_id, launch=launch, storage_required=storage_required,
                                         storage_binding=storage_binding, storage_evidence=storage_evidence)

    def capacity_status(self, project_id: str, status: CapacityStatusInput) -> dict:
        self._guard()
        with self._ledger._lock:
            if self._ledger._connection.in_transaction:
                raise AccountError("account_unavailable")
            self._project_locked(project_id)
            return self._ledger._capacity_status(project_id, status)

    def run_provenance(self, project_id, *, runtime_digest, caller_scope, request,
                       storage_binding, storage_evidence, work_limits):
        self._guard()
        return self._ledger._attest_run_provenance(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                  request=request, storage_binding=storage_binding,
                                                  storage_evidence=storage_evidence, work_limits=work_limits)

    def register_execution(self, project_id, *, runtime_digest, caller_scope, registration, storage_binding, storage_evidence):
        self._guard()
        return self._ledger._register_project_execution(project_id, runtime_digest=runtime_digest,
                                                       caller_scope=caller_scope, registration=registration,
                                                       storage_binding=storage_binding, storage_evidence=storage_evidence)

    def submit_terminal(self, project_id, *, runtime_digest, caller_scope, submission,
                        storage_binding, storage_evidence, terminal_evidence):
        self._guard()
        return self._ledger._accept_project_terminal(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                     submission=submission, storage_binding=storage_binding,
                                                     storage_evidence=storage_evidence, terminal_evidence=terminal_evidence)

    def close_denied_attempt(self, project_id, *, runtime_digest, caller_scope, closure, storage_binding, storage_evidence):
        self._guard()
        return self._ledger._close_denied_attempt(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                  closure=closure, storage_binding=storage_binding, storage_evidence=storage_evidence)

    def _claim_preflight(self, project_id, *, runtime_digest, caller_scope, request_id, launch, work_limits=None,
                         storage_required=False, storage_binding=None, storage_evidence=None):
        self._guard()
        return self._ledger._claim_preflight(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                             request_id=request_id, launch=launch, work_limits=work_limits,
                                             storage_required=storage_required, storage_binding=storage_binding,
                                             storage_evidence=storage_evidence)

    def _claim_commit_denied(self, context, reason, work_limits=None):
        self._guard()
        if reason is None:
            raise ClaimError("claim_unavailable")
        return self._ledger._claim_commit(context, None, reason, work_limits=work_limits)

    def _claim_policy(self, provider):
        self._guard()
        settings = self._ledger._admission_settings(provider)
        if settings is None:
            raise ClaimError("claim_unavailable")
        return AdmissionInputPolicy(settings["client_versions"]["minimum"],
                                    settings["client_versions"]["maximum_exclusive"],
                                    settings.get("max_admissions_per_window"))

    def _claim_from_signature_job(self, context, pool, handle):
        self._guard()
        return self._ledger._claim_from_signature_job(context, pool, handle)

    def capture_admission_inputs(
        self, project_id: str, provider: str, policy: AdmissionInputPolicy,
        limits: AdmissionCaptureLimits, work_limits: SQLReadLimits,
    ) -> AdmissionInputSnapshot:
        """Capture raw evidence in one owner-local read snapshot, never admit.

        Policy is explicit trusted owner configuration, not an RPC parameter or
        proof of the installed policy. Public-signer and filesystem observations
        precede the SQL scope; no whole-operation OS latency bound is claimed.
        Every candidate history row is charged before provider/account filtering.
        Overflow refuses the capture rather than dropping restrictive evidence.
        Ownership/project scope failures retain AccountError; capture failures
        use AdmissionCaptureError. Neither error grants a fallback admission.
        """
        self._guard()
        if (not matches(project_id, _PROJECT) or type(provider) is not str or provider not in {"codex", "claude"}
                or type(policy) is not AdmissionInputPolicy or type(limits) is not AdmissionCaptureLimits
                or type(work_limits) is not SQLReadLimits):
            raise AdmissionCaptureError("admission_capture_invalid_request")
        policy.__post_init__()
        limits.__post_init__()
        policy = AdmissionInputPolicy(
            policy.minimum_version, policy.maximum_exclusive_version, policy.max_admissions_per_window,
        )
        limits = AdmissionCaptureLimits(limits.max_rows, limits.max_bytes)
        reason = "admission_capture_unavailable"
        try:
            work_limits.__post_init__()
            with self._ledger._lock:
                connection = self._ledger._connection
                connection._read_owner()
                unavailable = connection._sql_scope is not None or connection._sql_foreign_progress
                try:
                    unavailable = unavailable or connection.in_transaction
                except sqlite3.Error:
                    unavailable = True
                if unavailable:
                    raise AdmissionCaptureError("admission_capture_unavailable")
                filesystem_failed = False
                try:
                    self._ledger.state.harden_database_files()
                    signer = self._ledger._current_signer_bytes()
                except (StateError, OSError):
                    filesystem_failed = True
                if filesystem_failed:
                    raise AdmissionCaptureError("admission_capture_unavailable")
                return self._ledger._connection.read_call(
                    work_limits, lambda: self._capture_admission_transaction(project_id, provider, policy, limits, signer),
                )
        except (AccountError, AdmissionCaptureError):
            if getattr(self._ledger._connection, "_sql_unusable", False) is not True:
                raise
        except SQLReadError as error:
            if error.reason_code == "sql_read_invalid_limits":
                reason = "admission_capture_invalid_request"
            elif error.reason_code in {"sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit", "sql_read_busy"}:
                reason = "admission_capture_work_limit"
        raise AdmissionCaptureError(reason)

    def _capture_admission_transaction(self, project_id, provider, policy, limits, signer):
        connection = self._ledger._connection
        failure = traceback = result = None
        began = False
        try:
            connection.execute("BEGIN")
            began = True
            result = self._capture_admission_rows_locked(project_id, provider, policy, limits, signer)
        except BaseException as error:
            failure, traceback = error, error.__traceback__
        if began:
            try:
                # Fixed native rollback ends this read snapshot even if the SQL
                # scope's work/deadline latch refuses another wrapped statement.
                connection.rollback()
            except BaseException as error:
                connection._sql_unusable = True
                if failure is None or not isinstance(error, Exception):
                    failure, traceback = error, error.__traceback__
                try:
                    sqlite3.Connection.close(connection)
                except BaseException as closing:
                    if failure is None or not isinstance(closing, Exception):
                        failure, traceback = closing, closing.__traceback__
        if failure is not None:
            raise failure.with_traceback(traceback)
        return result

    def _capture_admission_rows_locked(self, project_id, provider, policy, limits, signer):
        connection = self._ledger._connection
        project = connection.execute(
            "SELECT CASE status WHEN 'active' THEN 1 ELSE 0 END,"
            "CASE WHEN typeof(created_at_ms)='integer' THEN created_at_ms END "
            "FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if project is None or project[0] != 1:
            raise AccountError("account_project_not_found")
        if type(project[1]) is not int or project[1] < 0:
            raise AdmissionCaptureError("admission_capture_incompatible")
        created_at_ms = project[1]
        timestamp = self._ledger._clock_ms()
        if type(timestamp) is not int or not 0 <= timestamp < 2 ** 63:
            raise AdmissionCaptureError("admission_capture_unavailable")
        owner_header = connection.execute(
            "SELECT CASE WHEN typeof(sequence)='integer' THEN sequence END,"
            "typeof(signer_line),length(signer_line),"
            "CASE reason WHEN 'enrolled' THEN 'enrolled' WHEN 'replaced' THEN 'replaced' END,"
            "CASE WHEN typeof(recorded_at_ms)='integer' THEN recorded_at_ms END "
            "FROM owner_key_history ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        size = admission_input_size(project_id, provider, COLLECTOR_RUNTIME_DIGEST, policy, None, signer, ())
        if owner_header is not None:
            sequence, line_type, line_bytes, reason, recorded = owner_header
            if (type(sequence) is not int or sequence < 1 or line_type != "blob"
                    or type(line_bytes) is not int or not 0 < line_bytes <= OWNER_SIGNERS_MAX_BYTES
                    or reason not in {"enrolled", "replaced"} or type(recorded) is not int or recorded < 0):
                raise AdmissionCaptureError("admission_capture_incompatible")
            size += 16 + line_bytes + len(reason)
        if size > limits.max_bytes:
            raise AdmissionCaptureError("admission_capture_limit")
        headers = connection.execute(
            "SELECT r.rowid,typeof(r.project_id),length(CAST(r.project_id AS BLOB)),"
            "typeof(r.record_id),length(CAST(r.record_id AS BLOB)),"
            "typeof(r.record_type),length(CAST(r.record_type AS BLOB)),"
            "typeof(r.content),length(r.content),"
            "typeof(r.content_digest),length(CAST(r.content_digest AS BLOB)),"
            "typeof(r.source),length(CAST(r.source AS BLOB)),"
            "CASE WHEN typeof(r.source_sequence)='integer' THEN r.source_sequence END,"
            "CASE WHEN typeof(r.observed_at_ms)='integer' THEN r.observed_at_ms END,"
            "CASE WHEN typeof(r.ingested_at_ms)='integer' THEN r.ingested_at_ms END,"
            "typeof(a.signature),length(a.signature)" + _ADMISSION_INPUT_JOIN,
            (project_id, limits.max_rows + 1),
        ).fetchall()
        if len(headers) > limits.max_rows:
            raise AdmissionCaptureError("admission_capture_limit")
        for header in headers:
            connection._before_statement()
            rowid, project_type, project_bytes, identifier_type, identifier_bytes, kind_type, kind_bytes, \
                content_type, content_bytes, digest_type, digest_bytes, source_type, source_bytes, \
                sequence, observed, ingested, signature_type, signature_bytes = header
            if (any(type(value) is not int or value < 0 for value in
                    (rowid, project_bytes, identifier_bytes, kind_bytes, content_bytes, digest_bytes,
                     source_bytes, sequence, observed, ingested))
                    or rowid < 1 or sequence < 1 or project_type != "text" or project_bytes != 36
                    or identifier_type != "text" or not 0 < identifier_bytes <= 640
                    or kind_type != "text" or not 0 < kind_bytes <= 24
                    or content_type != "blob" or not 0 < content_bytes <= MAX_RECORD_BYTES
                    or digest_type != "text" or digest_bytes != 64
                    or source_type != "text" or not 0 < source_bytes <= 32
                    or signature_type not in {"null", "blob"}
                    or (signature_type == "null" and signature_bytes is not None)
                    or (signature_type == "blob" and
                        (type(signature_bytes) is not int or not 0 <= signature_bytes <= 16_384))):
                raise AdmissionCaptureError("admission_capture_incompatible")
            size += (32 + project_bytes + identifier_bytes + kind_bytes + content_bytes + digest_bytes
                     + source_bytes + (signature_bytes or 0))
            if size > limits.max_bytes:
                raise AdmissionCaptureError("admission_capture_limit")
        # Complete every row/aggregate check before copying even the bounded
        # owner-history value. The current public signer was observed separately.
        incompatible = False
        try:
            owner = self._ledger._owner_generation_locked()
        except LedgerError:
            incompatible = True
        if incompatible:
            raise AdmissionCaptureError("admission_capture_incompatible")
        raw = connection.execute(
            "SELECT r.rowid,r.project_id,r.record_id,r.source_sequence,r.record_type,r.content,"
            "r.content_digest,r.source,r.observed_at_ms,r.ingested_at_ms,a.signature" + _ADMISSION_INPUT_JOIN,
            (project_id, limits.max_rows + 1),
        ).fetchall()
        if len(raw) != len(headers):
            raise AdmissionCaptureError("admission_capture_incompatible")
        rows = []
        for header, row in zip(headers, raw, strict=True):
            connection._before_statement()
            values = tuple(row)
            rowid, project, identifier, sequence, kind, content, digest, source, observed, ingested, signature = values
            if (any(type(value) is not int for value in (rowid, sequence, observed, ingested))
                    or rowid != header[0] or not matches(project, _PROJECT) or type(identifier) is not str
                    or type(kind) is not str or kind not in ADMISSION_CAPTURE_RECORD_TYPES
                    or type(content) is not bytes or not matches(digest, _DIGEST) or type(source) is not str
                    or (signature is not None and type(signature) is not bytes)
                    or (len(project.encode("utf-8")), len(identifier.encode("utf-8")), len(kind), len(content),
                        len(digest), len(source.encode("utf-8")), sequence, observed, ingested,
                        None if signature is None else len(signature)) !=
                       (header[2], header[4], header[6], header[8], header[10], header[12],
                        header[13], header[14], header[15], header[17])
                    or hashlib.sha256(content).hexdigest() != digest):
                raise AdmissionCaptureError("admission_capture_incompatible")
            rows.append(values)
        rows = tuple(rows)
        retained = admission_input_size(project_id, provider, COLLECTOR_RUNTIME_DIGEST, policy, owner, signer, rows)
        if retained != size:
            raise AdmissionCaptureError("admission_capture_incompatible")
        return AdmissionInputSnapshot(
            self._admission_capture_origin, project_id, provider, COLLECTOR_RUNTIME_DIGEST, timestamp,
            created_at_ms, policy, owner, signer, rows, retained,
        )

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
                # read_call guards unusable connections and outer transactions.
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
        except (StateError, OSError, UnicodeError):
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
    """Closed dispatcher; health never calls the persistence backend.

    The preopened owner is supplied by trusted startup, not constructed on a
    request. A native adapter must verify fresh caller/project/session/generation
    and operation scope independently. Default denial grants no usable endpoint.
    Closing this handler never closes the shared account owner.
    begin() defers claim signature work to explicitly configured owner jobs.
    New claims require armed protected-store evidence by default. The explicit
    unleased reference switch is trusted startup/test state, never wire input
    or an automatic fallback. Matching configured jobs enforce the same mode.
    Native authentication, bounded final admission work and normal-client
    migration remain required before endpoint activation.
    """

    def __init__(
        self, runtime_digest: str, views: AccountOwnerViews, *,
        authorizer: Callable[[AccountRequest, object], AccountAuthorization | None] | None = None,
        claim_jobs=None,
        allow_unleased_claim_reference=False,
        run_provenance_work_limits: SQLReadLimits | None = None,
    ) -> None:
        if (not matches(runtime_digest, _DIGEST) or not isinstance(views, AccountOwnerViews)
                or (authorizer is not None and not callable(authorizer)) or type(allow_unleased_claim_reference) is not bool):
            raise AccountError("account_invalid_request")
        if run_provenance_work_limits is not None and type(run_provenance_work_limits) is not SQLReadLimits:
            raise AccountError('account_invalid_request')
        self._provenance_limits = (None if run_provenance_work_limits is None else SQLReadLimits(
            run_provenance_work_limits.max_callbacks, run_provenance_work_limits.max_duration_ms,
            run_provenance_work_limits.max_value_bytes))
        if claim_jobs is not None:
            from .account_claim_jobs import AccountClaimJobs

            if (type(claim_jobs) is not AccountClaimJobs or claim_jobs._views is not views
                    or claim_jobs._allow_unleased_reference != allow_unleased_claim_reference):
                raise AccountError("account_invalid_request")
            claim_jobs._guard()
        self._runtime, self._views, self._authorizer = runtime_digest, views, authorizer
        self._claim_jobs = claim_jobs
        self._allow_unleased_reference = allow_unleased_claim_reference
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
        return self._handle(raw, transport_context, deferred=False)

    def begin(self, raw: bytes, transport_context: object = None, *, _ingress=None):
        """Return an immediate wire reply or an owner-local pending ticket.

        A transport retains its connection outside the database request loop.
        Tickets are never serialized to clients and authorize no execution.
        There is no synchronous signature fallback for this entry point.
        """
        return self._handle(raw, transport_context, deferred=True, ingress=_ingress)

    def drive_claims(self):
        if self._claim_jobs is None:
            raise ClaimError("claim_unavailable")
        self._claim_jobs.drive()

    def take_claim_reply(self, ticket, *, _ingress=None):
        if self._claim_jobs is None:
            raise ClaimError("claim_unavailable")
        reply = self._claim_jobs.take(ticket, _ingress=_ingress)
        if reply is None:
            return None
        context = reply.context
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.claim_launch", context.request_id,
                                 context.project_id, context.runtime_digest, context.launch.payload())
        labels = {"request_id": context.request_id, "project_id": context.project_id,
                  "runtime_digest": context.runtime_digest, "request_digest": account_request_digest(request)}
        raw = self._reply(labels, result=reply.result, reason=reply.reason)
        try:
            decode_account_response(raw, request)
        except AccountError as error:
            # The decoder also raises the validated public failure reason.
            # Preserve that wire reply; only a different validation error is
            # a malformed response, rather than a failed claim operation.
            if error.reason_code != reply.reason:
                raise
        return raw

    def _handle(self, raw: bytes, transport_context: object, *, deferred: bool, ingress=None):
        labels = {"request_id": None, "project_id": None, "runtime_digest": None, "request_digest": None}
        try:
            request = decode_account_request(raw)
            digest = account_request_digest(request)
            operation = request.operation
            project, runtime, identity = request.project_id, request.runtime_digest, request.request_id
            # Capture immutable scalar claim input before the trusted adapter
            # sees its mutable request object. The digest still binds its exact
            # original envelope; no JSON field supplies caller/role authority.
            launch = decode_claim_input(request.payload) if operation == "account.claim_launch" else None
            status = decode_capacity_status_input(request.payload) if operation == "account.capacity_status" else None
            from .execution_grant import decode_execution_registration
            registration = decode_execution_registration(request.payload) if operation == "account.register_execution" else None
            from .terminal_delivery import decode_terminal_submission
            submission = decode_terminal_submission(request.payload) if operation == 'account.submit_terminal' else None
            from .claim_denial import decode_close_denied
            closure = decode_close_denied(request.payload) if operation == 'account.close_denied_attempt' else None
            provenance = decode_run_provenance_input(request.payload) if operation == 'account.run_provenance' else None
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
                or (authorization.source == "project" and operation not in {
                    "account.health", "account.register_execution", "account.submit_terminal", "account.close_denied_attempt", "account.run_provenance"})
                or (operation in {"account.register_execution", "account.submit_terminal", "account.close_denied_attempt", "account.run_provenance"} and
                    (authorization.source != "project" or authorization.caller_scope is None))
                or (operation in {"account.claim_launch", "account.capacity_status"} and
                    (authorization.source != "host" or authorization.caller_scope is None))
            ):
                raise AccountError("account_not_authorized")
            if operation == "account.claim_launch":
                from .claim_storage import capture_storage_evidence

                storage_binding, storage_evidence = capture_storage_evidence(
                    authorization.storage_binding, authorization.storage_evidence)
                if deferred:
                    if self._claim_jobs is None:
                        raise ClaimError("claim_unavailable")
                    return self._claim_jobs.submit(project, runtime_digest=runtime, caller_scope=authorization.caller_scope,
                                                   request_id=identity, launch=launch, _ingress=ingress,
                                                   storage_binding=storage_binding, storage_evidence=storage_evidence)
                if self._claim_jobs is not None:
                    # Configured asynchronous owners never block or bypass their
                    # queue with the legacy synchronous reference operation.
                    raise ClaimError("claim_unavailable")
                result = self._views.claim_launch(project, runtime_digest=runtime, caller_scope=authorization.caller_scope,
                                                  request_id=identity, launch=launch,
                                                  storage_required=not self._allow_unleased_reference,
                                                  storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif operation == "account.register_execution":
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(
                    authorization.storage_binding, authorization.storage_evidence)
                result = self._views.register_execution(project, runtime_digest=runtime,
                                                        caller_scope=authorization.caller_scope, registration=registration,
                                                        storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif operation == 'account.run_provenance':
                from .claim_storage import capture_storage_evidence
                self._provenance_live()
                if self._provenance_limits is None:
                    raise ClaimError('claim_execution_unavailable')
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                caller = authorization.caller_scope
                result = self._views.run_provenance(project, runtime_digest=runtime, caller_scope=caller,
                                                    request=provenance, storage_binding=storage_binding,
                                                    storage_evidence=storage_evidence, work_limits=self._provenance_limits)
                self._provenance_live()
                # Fresh immutable request and current independent authority;
                # adapter closure/mutation/revocation cannot expose cached data.
                fresh = decode_account_request(raw)
                current = self._authorizer(fresh, transport_context)
                if type(current) is not AccountAuthorization:
                    raise AccountError('account_not_authorized')
                current.__post_init__()
                new_binding, new_evidence = capture_storage_evidence(current.storage_binding, current.storage_evidence)
                self._provenance_live()
                if (current.source != 'project' or current.project_id != project or current.runtime_digest != runtime
                        or current.caller_scope != caller or current.request_digest != digest
                        or account_request_digest(fresh) != digest or operation not in current.allowed_operations
                        or new_binding != storage_binding or new_evidence != storage_evidence):
                    raise AccountError('account_not_authorized')
                self._views._guard()
            elif operation == 'account.submit_terminal':
                from .claim_storage import capture_storage_evidence
                from .terminal_delivery import capture_terminal_evidence
                storage_binding, storage_evidence = capture_storage_evidence(
                    authorization.storage_binding, authorization.storage_evidence)
                result = self._views.submit_terminal(project, runtime_digest=runtime,
                                                     caller_scope=authorization.caller_scope, submission=submission,
                                                     storage_binding=storage_binding, storage_evidence=storage_evidence,
                                                     terminal_evidence=capture_terminal_evidence(authorization.terminal_evidence))
            elif operation == 'account.close_denied_attempt':
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                result = self._views.close_denied_attempt(project, runtime_digest=runtime,
                                                          caller_scope=authorization.caller_scope, closure=closure,
                                                          storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif request.operation == "account.health":
                result = {"transport_alive": True, "database_checked": False}
            elif request.operation == "account.project":
                result = self._views.project(request.project_id)
            elif operation == "account.capacity_status":
                result = self._views.capacity_status(project, status)
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
        except ClaimError as exc:
            reason = exc.reason_code
        except Exception:
            reason = "account_unavailable"
        return self._reply(labels, reason=reason)

    def _provenance_live(self):
        if self._pid != os.getpid():
            raise AccountError('account_wrong_owner')
        with self._front_lock:
            if self._closed:
                raise AccountError('account_not_authorized')

    def close(self) -> None:
        if self._pid != os.getpid():
            raise AccountError("account_wrong_owner")
        with self._front_lock:
            self._closed = True
