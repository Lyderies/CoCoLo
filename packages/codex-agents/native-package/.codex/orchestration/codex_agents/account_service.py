"""Default-deny account metadata handler over one already opened account owner.

This module is a persistence-owner component, never a client dependency. Native
caller enrollment and endpoint identity are unimplemented gates, not a socket,
UID, role string or the authorization dataclass below. This component starts no
production listener; normal API clients remain denied without native enrollment.
"""

from __future__ import annotations

import os
import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable

from .account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_READ_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, MAX_ACCOUNT_FRAME_BYTES,
    AccountError, AccountRequest, _DIGEST, _PROJECT, account_request_digest,
    decode_account_request, decode_account_response, encode_account_request, integer, matches,
)
from .admission_inputs import (
    ADMISSION_CAPTURE_PUBLIC_REASONS, ADMISSION_CAPTURE_RECORD_TYPES, AdmissionCaptureError, AdmissionCaptureLimits,
    AdmissionInputPolicy, AdmissionInputSnapshot, admission_input_size, public_capture_reason,
)
from .contracts import ContractError
from .collector_contracts import RUNTIME_UPDATE_COMMAND, RUNTIME_UPGRADE_MESSAGE
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, parse_bounded_json
from .learning_store import (
    COLLECTOR_RUNTIME_DIGEST, OWNER_SIGNER_LINE, OWNER_SIGNERS_MAX_BYTES,
    LearningLedger, LedgerError, OwnerSignerNotConfigured, StateError,
    _owner_key_identity, _signer_fingerprint,
)
from .owner_key_contracts import owner_public_key_from_line
from . import ledger_waits, lane_health
from .sql_read_budget import SQLReadError, SQLReadLimits
from .signature_inputs import (
    SignatureCaptureError, SignatureCaptureLimits, SignatureInputSnapshot, signature_input_size,
)
from .account_claim_contracts import ClaimError, LaunchClaimInput, decode_claim_input
from .capacity_status_contracts import CapacityStatusInput, decode_capacity_status_input
from .run_provenance import decode_run_provenance_input
from .host_open_contracts import decode_open_provenance_input
from .account_open_contracts import AccountOpenContext, OpenError, decode_account_open_input
from .account_observation import ObservationContext
from .provider_observation_contracts import OBSERVATION_REASONS, ObservationError, decode_observation_input
from .observation_failure_contracts import decode_observation_failure
from .account_observation_failure import record_observation_failure, recheck_observation_failure
from .billing_probe_contracts import PROBE_STEP_REASONS, ProbeAdmissionError
from .open_withdrawal import WithdrawError

# B3: the ledger's withdrawal refusals, as public reasons. An exact replay presented by another caller scope, and a
# request whose run id, inputs digest or caller scope is not the open's, are not authorized, whatever they name.
WITHDRAW_LEDGER_REASONS = {
    'withdrawal is unavailable': 'withdraw_unavailable',
    'withdrawal is stale': 'withdraw_stale',
    'generation_required': 'withdraw_generation_required',
    'withdrawal is not authorized': 'account_not_authorized',
}

# Fixed owner query over the history that can still change an admission decision.
# `:a` is the capture's anchor: the first capture's own clock, kept in its snapshot
# and reused by every recapture, which refuses a clock earlier than it. Every
# decision is evaluated at a recapture's clock, so `now >= :a`, and each rule
# keeps every row a consumer could read at any such `now` (admission_metadata
# and its account, claim and probe callers):
#   identities: observed in the last 30 minutes (or later);
#   attestations: all, the latest verified one wins at any age;
#   decisions and probes: the last 5 hours (run cap, probe cooldown, a run's own
#     decision); and the first passing Claude decision per account, any age (the
#     one bootstrap run per account);
#   billing and headroom: the last 24 hours (spend control, paid usage, live
#     windows without a reset time); headroom whose window resets after :a, any
#     age; and for Claude, the latest older billing and headroom per account,
#     keeping ties (a probe may reuse old evidence; a bootstrap needs none).
# Rows from the future are kept by every time rule. Foreign-account/provider
# candidates are still charged. Content is never parsed here: the account, a
# headroom record's latest reset and a passing Claude decision come from the
# admission_evidence index, which a trigger fills as each record is written.
_ADMISSION_EVIDENCE = "SELECT 1 FROM admission_evidence e WHERE e.project_id=r.project_id AND e.record_id=r.record_id"
_ADMISSION_INPUT_SCOPE = (
    "((r.project_id=:p AND r.record_type='provider_identity' AND r.observed_at_ms>=:a-1800000)"
    " OR (r.project_id=:p AND r.record_type='billing_attestation')"
    " OR (r.project_id=:p AND r.record_type='admission_decision' AND r.source<>'lifecycle')"
    " OR (r.source='lifecycle' AND r.record_type IN ('admission_decision','probe_admission')"
    " AND (r.observed_at_ms>=:a-18000000 OR EXISTS(" + _ADMISSION_EVIDENCE + " AND e.claude_pass=1"
    # The first passing Claude decision of its account, in a fixed order.
    " AND NOT EXISTS(SELECT 1 FROM admission_evidence f WHERE f.claude_pass=1 AND f.source='lifecycle'"
    " AND f.provider='claude' AND f.account_fingerprint=e.account_fingerprint"
    " AND (f.observed_at_ms,f.project_id,f.record_id)<(e.observed_at_ms,e.project_id,e.record_id)))))"
    " OR (r.source='provider' AND r.record_type IN ('provider_billing','provider_headroom')"
    " AND (r.observed_at_ms>=:a-86400000"
    " OR (r.record_type='provider_headroom' AND EXISTS(" + _ADMISSION_EVIDENCE + " AND e.live_until_ms>:a))"
    # Claude's latest older billing or headroom per account, every tie kept.
    " OR EXISTS(" + _ADMISSION_EVIDENCE + " AND e.provider='claude' AND e.account_fingerprint IS NOT NULL"
    " AND NOT EXISTS(SELECT 1 FROM admission_evidence f WHERE f.provider='claude' AND f.source='provider'"
    " AND f.record_type=e.record_type AND f.account_fingerprint=e.account_fingerprint"
    " AND f.observed_at_ms>e.observed_at_ms AND f.observed_at_ms<:a-86400000)))))"
)
_ADMISSION_INPUT_JOIN = (
    " FROM records r LEFT JOIN owner_attestations a "
    "ON a.project_id=r.project_id AND a.subject_record_id=r.record_id WHERE "
    + _ADMISSION_INPUT_SCOPE + " ORDER BY r.rowid LIMIT :limit"
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
    open_context: AccountOpenContext | None = None
    cleanup_evidence: bytes | None = None
    observation_context: ObservationContext | None = None

    def __post_init__(self) -> None:
        if (
            not matches(self.project_id, _PROJECT) or not matches(self.runtime_digest, _DIGEST)
            or not matches(self.request_digest, _DIGEST) or type(self.source) is not str
            or self.source not in {"host", "owner", "hook", "app_server", "project"}
            or type(self.allowed_operations) is not frozenset or not self.allowed_operations
            or any(type(operation) is not str for operation in self.allowed_operations)
            or not self.allowed_operations <= ACCOUNT_OPERATIONS
            or (self.caller_scope is not None and not matches(self.caller_scope, _DIGEST))
            or (self.open_context is not None and type(self.open_context) is not AccountOpenContext)
            or (self.observation_context is not None and type(self.observation_context) is not ObservationContext)
        ):
            raise AccountError("account_not_authorized")
        from .claim_storage import capture_storage_evidence

        invalid = False
        try:
            capture_storage_evidence(self.storage_binding, self.storage_evidence)
            from .terminal_delivery import capture_terminal_evidence
            capture_terminal_evidence(self.terminal_evidence)
            from .release_contracts import capture_cleanup_evidence
            capture_cleanup_evidence(self.cleanup_evidence)
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
        with self._ledger._held():
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
        with self._ledger._held():
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

    def open_provenance(self, project_id, *, runtime_digest, caller_scope, request,
                        storage_binding, storage_evidence, work_limits):
        self._guard()
        return self._ledger._attest_open_provenance(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
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

    def record_rate_limit_event(self, project_id, *, runtime_digest, event, storage_binding, storage_evidence):
        self._guard()
        from .rate_limit_contracts import rate_limit_reply
        return rate_limit_reply(self._ledger._record_project_rate_limit_event(
            project_id, runtime_digest=runtime_digest, event=event,
            storage_binding=storage_binding, storage_evidence=storage_evidence))

    def close_denied_attempt(self, project_id, *, runtime_digest, caller_scope, closure, storage_binding, storage_evidence):
        self._guard()
        return self._ledger._close_denied_attempt(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                                  closure=closure, storage_binding=storage_binding, storage_evidence=storage_evidence)

    def fence_claim(self, project_id, *, runtime_digest, caller_scope, fence, storage_binding, storage_evidence):
        self._guard()
        return self._ledger._fence_claim(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                                         fence=fence, storage_binding=storage_binding, storage_evidence=storage_evidence)

    def settle_lost_run(self, project_id, *, runtime_digest, caller_scope, settle, storage_binding, storage_evidence):
        self._guard()
        from .account_lost_run import settle_lost_run
        return settle_lost_run(self._ledger, project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                               settle=settle, storage_binding=storage_binding, storage_evidence=storage_evidence)

    def withdraw_open(self, project_id, *, caller_scope, withdrawal, request_digest):
        """B3: the ledger's withdrawal for an authenticated caller, its refusals as public reasons."""
        self._guard()
        try:
            return self._ledger.withdraw_open(project_id, run_handle=withdrawal.run_handle,
                                              request_digest=request_digest, generation=withdrawal.generation,
                                              owner={'caller_scope': caller_scope, 'run_id': withdrawal.run_id,
                                                     'inputs_digest': withdrawal.inputs_digest,
                                                     'attempt': withdrawal.attempt, 'reason': withdrawal.reason})
        except LedgerError as error:
            reason = WITHDRAW_LEDGER_REASONS.get(str(error))
            if reason is None:
                raise
        if reason == 'account_not_authorized':
            raise AccountError(reason)
        raise WithdrawError(reason)

    def release_capacity(self, project_id, **options):
        self._guard()
        from .account_cleanup import release_capacity
        return release_capacity(self._ledger, project_id, **options)

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
        limits: AdmissionCaptureLimits, work_limits: SQLReadLimits, anchor_ms: int | None = None,
    ) -> AdmissionInputSnapshot:
        """Capture raw evidence in one owner-local read snapshot, never admit.

        `anchor_ms` is the earliest time any later evaluation of this capture may
        use: the ledger clock by default, a claim's open time for a claim.

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
            with self._ledger._held():
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
                    work_limits, lambda: self._capture_admission_transaction(
                        project_id, provider, policy, limits, signer, anchor_ms),
                )
        except (AccountError, AdmissionCaptureError):
            if getattr(self._ledger._connection, "_sql_unusable", False) is not True:
                raise
        except SQLReadError as error:
            if error.reason_code == "sql_read_busy":
                raise  # M4b E1a-2: busy, not admission_capture_unavailable
            if error.reason_code == "sql_read_invalid_limits":
                reason = "admission_capture_invalid_request"
            elif error.reason_code in {"sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit"}:
                reason = "admission_capture_work_limit"
        raise AdmissionCaptureError(reason)

    def _capture_admission_transaction(self, project_id, provider, policy, limits, signer, anchor_ms=None):
        connection = self._ledger._connection
        failure = traceback = result = None
        began = False
        try:
            connection.execute("BEGIN")
            began = True
            result = self._capture_admission_rows_locked(project_id, provider, policy, limits, signer, anchor_ms)
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

    def _capture_admission_rows_locked(self, project_id, provider, policy, limits, signer, anchor_ms=None):
        """Capture at the ledger clock; a recapture passes its snapshot's `anchor_ms`."""
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
        if anchor_ms is None:
            anchor_ms = timestamp
        elif type(anchor_ms) is not int or not 0 <= anchor_ms <= timestamp:
            # A clock earlier than the anchor could need history the scope left out.
            raise AdmissionCaptureError("admission_capture_unavailable")
        scope = {"p": project_id, "a": anchor_ms, "limit": limits.max_rows + 1}
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
            scope,
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
            scope,
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
            created_at_ms, policy, owner, signer, rows, retained, anchor_ms,
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
            with self._ledger._held():
                # read_call guards unusable connections and outer transactions.
                return self._ledger._connection.read_call(
                    work_limits, lambda: self._capture_signature_inputs_locked(project_id, provider, limits),
                )
        except (AccountError, SignatureCaptureError):
            if getattr(self._ledger._connection, "_sql_unusable", False) is not True:
                raise
        except SQLReadError as error:
            if error.reason_code == "sql_read_busy":
                raise  # M4b E1a-2: busy, not a work limit
            if error.reason_code == "sql_read_invalid_limits":
                reason = "signature_capture_invalid_request"
            elif error.reason_code in {"sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit"}:
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
        with self._ledger._held():
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
        open_provenance_work_limits: SQLReadLimits | None = None,
        observation_jobs=None,
    ) -> None:
        if (not matches(runtime_digest, _DIGEST) or not isinstance(views, AccountOwnerViews)
                or (authorizer is not None and not callable(authorizer)) or type(allow_unleased_claim_reference) is not bool):
            raise AccountError("account_invalid_request")
        if run_provenance_work_limits is not None and type(run_provenance_work_limits) is not SQLReadLimits:
            raise AccountError('account_invalid_request')
        self._provenance_limits = (None if run_provenance_work_limits is None else SQLReadLimits(
            run_provenance_work_limits.max_callbacks, run_provenance_work_limits.max_duration_ms,
            run_provenance_work_limits.max_value_bytes))
        if open_provenance_work_limits is not None and type(open_provenance_work_limits) is not SQLReadLimits:
            raise AccountError('account_invalid_request')
        self._open_provenance_limits = (None if open_provenance_work_limits is None else SQLReadLimits(
            open_provenance_work_limits.max_callbacks, open_provenance_work_limits.max_duration_ms,
            open_provenance_work_limits.max_value_bytes))
        if claim_jobs is not None:
            from .account_claim_jobs import AccountClaimJobs

            if (type(claim_jobs) is not AccountClaimJobs or claim_jobs._views is not views
                    or claim_jobs._allow_unleased_reference != allow_unleased_claim_reference):
                raise AccountError("account_invalid_request")
            claim_jobs._guard()
        self._runtime, self._views, self._authorizer = runtime_digest, views, authorizer
        self._claim_jobs = claim_jobs
        if observation_jobs is not None:
            from .account_observation_jobs import AccountObservationJobs
            if type(observation_jobs) is not AccountObservationJobs or observation_jobs._owner._views is not views:
                raise AccountError('account_invalid_request')
            observation_jobs._owner._guard()
        self._observation_jobs = observation_jobs
        self._billing_jobs = None if claim_jobs is None else claim_jobs._billing
        if self._billing_jobs is not None and self._billing_jobs._observations is not observation_jobs:
            raise AccountError('account_invalid_request')
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
            **({"message": RUNTIME_UPGRADE_MESSAGE, "update_command": RUNTIME_UPDATE_COMMAND}
               if reason == "runtime_upgrade_required" else {}),
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
        if self._claim_jobs is None and self._observation_jobs is None:
            raise ClaimError("claim_unavailable")
        if self._claim_jobs is not None:
            self._claim_jobs.drive()
        if self._observation_jobs is not None:
            self._observation_jobs.drive()

    def _billing_reply(self, key, result, context, before_publish, history=None, judgment=None):
        project, runtime, caller, identity = key
        request = AccountRequest(1, 'account.billing_probe', identity, project, runtime, {})
        labels = {'request_id': identity, 'project_id': project, 'runtime_digest': runtime,
                  'request_digest': account_request_digest(request)}
        try:
            records = result['records']
            # Decision 50 (BR-16): the caller's callback runs first and is untrusted; the waiver display and the
            # provenance are read next; the one final gate (verify_committed, which asks the caller once more
            # before it) is the last thing, and nothing external runs after it.
            if before_publish() is not True:
                raise ProbeAdmissionError('probe_not_authorized')
            waivers = self._observation_jobs._owner.billing_waivers(project, records) if records else {}
            raw = self._reply(labels, result={**result, 'billing_waivers': waivers})
            try:
                decode_account_response(raw, request)
            except AccountError:
                # R3: the reply failed its own contract: say so, and record it in the billing lane.
                lane_health.safely(lambda: self._billing_jobs.health.note_failure(
                    self._billing_jobs._note, 'probe_reply_invalid', None))
                raise ProbeAdmissionError('probe_reply_invalid') from None
            self._provenance_live()
            # BR-8 (decision 44, 47, 50): the exact committed row and reply of the judgment; a yes is gated here.
            self._billing_jobs._owner.verify_committed(*key, context, before_commit=before_publish,
                                                       reply=result, history=history, judgment=judgment)
            with self._front_lock:
                if self._closed:
                    raise ProbeAdmissionError('probe_not_authorized')
                return raw
        except ProbeAdmissionError as error:
            return self._reply(labels, reason=error.reason_code)
        except SQLReadError as error:  # M4b E1a-2
            if error.reason_code != 'sql_read_busy':
                self._name_probe_failure('probe_sql_read_failed', error)
            return self._reply(labels, reason='account_busy' if error.reason_code == 'sql_read_busy'
                               else 'probe_unavailable')
        except Exception as error:
            self._name_probe_failure('probe_reply_failed', error)
            return self._reply(labels, reason='probe_unavailable')

    def _name_probe_failure(self, cause, error=None):
        """Record why a probe was answered probe_unavailable in the billing lane's last_failure."""
        if self._billing_jobs is not None:
            lane_health.safely(lambda: self._billing_jobs.health.note_failure(
                self._billing_jobs._note, cause, error))

    def take_claim_reply(self, ticket, *, _ingress=None):
        """Deferred publication has its own request deadline (M4b E1a revision 3)."""
        with ledger_waits.request():
            return self._take_claim_reply(ticket, _ingress=_ingress)

    def _take_claim_reply(self, ticket, *, _ingress=None):
        if self._billing_jobs is not None and type(ticket) is object and ticket in self._billing_jobs._jobs:
            reply = self._billing_jobs.take(ticket, _ingress=_ingress)
            if reply is None:
                return None
            project, runtime, caller, identity = reply.key
            request = AccountRequest(1, 'account.billing_probe', identity, project, runtime, {})
            labels = {'request_id': identity, 'project_id': project, 'runtime_digest': runtime,
                      'request_digest': account_request_digest(request)}
            if reply.reason is not None:
                allowed = {
                    'probe_invalid_request', 'probe_not_authorized', 'probe_unavailable', 'probe_conflict',
                    'probe_full', 'probe_busy', 'probe_not_ready', 'probe_reply_invalid', 'account_busy',
                    'probe_replay_stale', 'provider_api_key_present', *ADMISSION_CAPTURE_PUBLIC_REASONS,
                    *PROBE_STEP_REASONS, *OBSERVATION_REASONS}
                if reply.reason not in allowed:
                    self._name_probe_failure('probe_reason_unrecognised')
                return self._reply(labels, reason=reply.reason if reply.reason in allowed else 'probe_unavailable')
            return self._billing_reply(reply.key, reply.result, reply.context, reply.before_publish, reply.history,
                                       reply.judgment)
        if (self._observation_jobs is not None and type(ticket) is object
                and ticket in self._observation_jobs._jobs):
            reply = self._observation_jobs.take(ticket, _ingress=_ingress)
            if reply is None:
                return None
            project, runtime, caller, identity = reply.key
            request = AccountRequest(1, 'account.observe_provider', identity, project, runtime, reply.request.payload())
            labels = {'request_id': identity, 'project_id': project, 'runtime_digest': runtime,
                      'request_digest': account_request_digest(request)}
            if reply.reason is not None:
                return self._reply(labels, reason=reply.reason)
            try:
                waivers = self._observation_jobs._owner.billing_waivers(project, reply.result['records'])
                raw = self._reply(labels, result={**reply.result, 'billing_waivers': waivers})
                decode_account_response(raw, request)
                if reply.before_publish() is not True:
                    raise ObservationError('observation_not_authorized')
                if waivers != self._observation_jobs._owner.billing_waivers(project, reply.result['records']):
                    raise ObservationError('observation_not_authorized')
                self._provenance_live()
                with self._front_lock:
                    if self._closed:
                        raise ObservationError('observation_not_authorized')
                    return raw
            except ObservationError as error:
                return self._reply(labels, reason=error.reason_code)
            except SQLReadError as error:  # M4b E1a-2
                return self._reply(labels, reason='account_busy' if error.reason_code == 'sql_read_busy'
                                   else 'observation_unavailable')
            except Exception:
                return self._reply(labels, reason='observation_unavailable')
        if self._claim_jobs is None:
            raise ClaimError("claim_unavailable")
        reply = self._claim_jobs.take(ticket, _ingress=_ingress)
        if reply is None:
            return None
        context = reply.context
        operation = reply.operation
        payload = context.wire_input.payload() if operation == 'account.open_host_run' else context.launch.payload()
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, operation, context.request_id,
                                 context.project_id, context.runtime_digest, payload)
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
        if operation == 'account.open_host_run' and reply.result is not None:
            try:
                if not callable(reply.before_publish):
                    raise OpenError('open_not_authorized')
                reply.before_publish()
                from .account_original_open import current_open_request
                self._views._guard()
                ledger = self._views._ledger
                # The read takes the shared lock itself, with a bounded wait (C12a, C12f).
                current = ledger._connection.read_call(self._claim_jobs._work_limits,
                    lambda: current_open_request(ledger, context))[1]
                if current is None or {**current, 'known': reply.result['known']} != reply.result:
                    raise OpenError('open_conflict')
                self._provenance_live()
                with self._front_lock:
                    if self._closed:
                        raise OpenError('open_account_closing')  # S1-U10a (verifier of review 040-s1-u10)
                    return raw
            except OpenError as error:
                return self._reply(labels, reason=error.reason_code)
            except SQLReadError as error:  # S1-U10b
                return self._reply(labels, reason='open_account_busy' if error.reason_code == 'sql_read_busy'
                                   else 'open_failed')
            except AccountError as error:  # an authorization or service refusal keeps its own reason
                return self._reply(labels, reason=error.reason_code)
            except Exception:  # S1-U10f
                return self._reply(labels, reason='open_failed')
        return raw

    def _handle(self, raw: bytes, transport_context: object, *, deferred: bool, ingress=None):
        """M4b E1a (revision 3): every ledger wait in one request shares one deadline, set here, where
        both handle() and begin() arrive, and restored on return."""
        with ledger_waits.request():
            return self._handle_request(raw, transport_context, deferred=deferred, ingress=ingress)

    def runtime_upgrade_reply(self, raw: bytes) -> bytes | None:
        """RSF-S1: bounded, side-effect-free gate, also safe on the socket front thread."""
        if type(raw) is not bytes or len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
            return None  # retain the normal malformed/oversized refusal
        try:
            envelope = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_FRAME_BYTES - 1)
        except (ContractError, TypeError, ValueError, RecursionError):
            return None
        if (type(envelope.get("operation")) is not str
                or envelope["operation"] not in ACCOUNT_OPERATIONS - ACCOUNT_READ_OPERATIONS
                or envelope.get("runtime_digest") == self._runtime):
            return None
        labels = {"request_id": None, "project_id": None, "runtime_digest": None, "request_digest": None}
        try:
            request = decode_account_request(raw)
            labels = {"request_id": request.request_id, "project_id": request.project_id,
                      "runtime_digest": request.runtime_digest, "request_digest": account_request_digest(request)}
        except AccountError:
            pass  # missing identity or invalid payload: never echo unchecked fields
        return self._reply(labels, reason="runtime_upgrade_required")

    def _handle_request(self, raw: bytes, transport_context: object, *, deferred: bool, ingress=None):
        labels = {"request_id": None, "project_id": None, "runtime_digest": None, "request_digest": None}
        operation = None  # known once decoded; chooses the busy answer
        try:
            upgrade = self.runtime_upgrade_reply(raw)
            if upgrade is not None:
                return upgrade
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
            submission = decode_terminal_submission(request.payload) if operation in {'account.submit_terminal', 'account.release_capacity'} else None
            from .claim_denial import decode_close_denied, decode_fence_claim
            closure = decode_close_denied(request.payload) if operation == 'account.close_denied_attempt' else None
            fence = decode_fence_claim(request.payload) if operation == 'account.fence_claim' else None
            from .lost_run_contracts import decode_settle_lost
            lost = decode_settle_lost(request.payload) if operation == 'account.settle_lost_run' else None
            from .open_withdrawal import decode_withdraw_open
            withdrawal = decode_withdraw_open(request.payload) if operation == 'account.withdraw_open' else None
            provenance = decode_run_provenance_input(request.payload) if operation == 'account.run_provenance' else None
            open_provenance = decode_open_provenance_input(request.payload) if operation == 'account.open_provenance' else None
            opening = decode_account_open_input(request.payload, project, runtime) if operation == 'account.open_host_run' else None
            observation = decode_observation_input(request.payload) if operation == 'account.observe_provider' else None
            if operation == 'account.record_observation_unavailable':
                observation = decode_observation_failure(request.payload)
            from .rate_limit_contracts import decode_rate_limit_event
            rate_event = decode_rate_limit_event(request.payload) if operation == 'account.record_rate_limit_event' else None
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
                    "account.health", "account.register_execution", "account.submit_terminal", "account.release_capacity", "account.close_denied_attempt", "account.fence_claim", "account.settle_lost_run", "account.run_provenance", "account.open_provenance", "account.observe_provider", "account.record_observation_unavailable",
                    "account.record_rate_limit_event", "account.withdraw_open"})
                # B3: a withdrawal comes from the run's project service or the host that opened it, each with the
                # caller scope of the run's host class (the ledger matches it to the open's receipt).
                or (operation == "account.withdraw_open" and (authorization.source not in {"project", "host"}
                                                              or authorization.caller_scope is None))
                or (operation in {"account.register_execution", "account.submit_terminal", "account.release_capacity", "account.close_denied_attempt", "account.fence_claim", "account.settle_lost_run", "account.run_provenance", "account.open_provenance",
                                  "account.record_rate_limit_event"} and
                    (authorization.source != "project" or authorization.caller_scope is None))
                or (operation in {"account.claim_launch", "account.capacity_status", 'account.open_host_run'} and
                    (authorization.source != "host" or authorization.caller_scope is None))
                or (operation in {'account.observe_provider', 'account.record_observation_unavailable'} and
                    (authorization.source not in {'host', 'project'} or authorization.caller_scope is None
                     or authorization.observation_context is None
                     or (authorization.source == 'project' and observation.run_handle is None)))
                or (operation == 'account.billing_probe' and (authorization.source != 'host'
                    or authorization.caller_scope is None or authorization.observation_context is None))
            ):
                raise AccountError("account_not_authorized")
            if operation == 'account.billing_probe':
                if self._billing_jobs is None or not deferred:
                    raise ProbeAdmissionError()
                context, caller = authorization.observation_context, authorization.caller_scope
                expected = context.snapshot()

                def authorize_billing():
                    self._provenance_live()
                    fresh = decode_account_request(raw)
                    current = self._authorizer(fresh, transport_context)
                    if type(current) is not AccountAuthorization:
                        raise ProbeAdmissionError('probe_not_authorized')
                    current.__post_init__()
                    if (current.source != 'host' or current.project_id != project or current.runtime_digest != runtime
                            or current.caller_scope != caller or current.request_digest != digest
                            or account_request_digest(fresh) != digest or operation not in current.allowed_operations
                            or current.observation_context is not context or context.snapshot() != expected):
                        raise ProbeAdmissionError('probe_not_authorized')
                    self._provenance_live()
                    return True

                key = (project, runtime, caller, identity)
                known = self._billing_jobs._owner.known_reply(*key, context, before_commit=authorize_billing)
                if known is not None:
                    return self._billing_reply(key, known, context, authorize_billing)
                return self._billing_jobs.submit(project, runtime, caller, identity, context, authorize_billing,
                                                  _ingress=ingress)
            if operation in {'account.observe_provider', 'account.record_observation_unavailable'}:
                if self._observation_jobs is None or (operation == 'account.observe_provider' and not deferred):
                    raise ObservationError('observation_unavailable')
                caller, source = authorization.caller_scope, authorization.source
                context = authorization.observation_context
                expected = context.snapshot()

                def authorize_observation():
                    try:
                        self._provenance_live()
                        fresh = decode_account_request(raw)
                        current = self._authorizer(fresh, transport_context)
                        if type(current) is not AccountAuthorization:
                            raise ValueError()
                        current.__post_init__()
                        if (current.source != source or current.project_id != project or current.runtime_digest != runtime
                                or current.caller_scope != caller or current.request_digest != digest
                                or account_request_digest(fresh) != digest or operation not in current.allowed_operations
                                or current.observation_context is not context or context.snapshot() != expected):
                            raise ValueError()
                        self._provenance_live()
                        self._views._guard()
                        return True
                    except SQLReadError as error:
                        if error.reason_code == 'sql_read_busy':
                            raise  # M4b E1a-2: busy, answered at the reply, never "not authorized"
                        raise ObservationError('observation_not_authorized') from None
                    except Exception:
                        raise ObservationError('observation_not_authorized') from None

                if operation == 'account.record_observation_unavailable':
                    owner = self._observation_jobs._owner
                    result = record_observation_failure(owner, project, runtime, caller, identity,
                                                         observation, context, authorize_observation)
                    encoded = self._reply(labels, result=result)
                    decode_account_response(encoded, request)
                    recheck_observation_failure(owner, project, runtime, caller, identity,
                                               observation, context, authorize_observation, result)
                    with self._front_lock:
                        if self._closed:
                            raise ObservationError('observation_not_authorized')
                        return encoded
                return self._observation_jobs.submit(project, runtime, caller, identity, observation,
                    context, authorize_observation, _ingress=ingress)
            elif operation == 'account.open_host_run':
                if not deferred:  # An open is always deferred; a synchronous call is a misuse (S1-U10f).
                    raise OpenError('open_failed')
                self._provenance_live()
                if authorization.open_context is None:  # Authorization before anything about readiness.
                    raise OpenError('open_not_authorized')
                if self._claim_jobs is None or self._claim_jobs._open_limits is None:
                    raise OpenError('open_account_starting')  # S1-U10a
                original_context, captured = authorization.open_context.snapshot()
                expected_context = AccountOpenContext.from_snapshot(captured)
                caller = authorization.caller_scope

                def authorize_open():
                    try:
                        self._provenance_live()
                        fresh = decode_account_request(raw)
                        current = self._authorizer(fresh, transport_context)
                        if type(current) is not AccountAuthorization:
                            raise ValueError()
                        current.__post_init__()
                        if (current.source != 'host' or current.project_id != project or current.runtime_digest != runtime
                                or current.caller_scope != caller or current.request_digest != digest
                                or account_request_digest(fresh) != digest or operation not in current.allowed_operations
                                or current.open_context is None or current.open_context.snapshot()[0] != original_context):
                            raise ValueError()
                        self._provenance_live()
                        self._views._guard()
                    except SQLReadError as error:
                        if error.reason_code == 'sql_read_busy':
                            raise  # M4b E1a-2
                        raise OpenError('open_not_authorized') from None
                    except Exception:
                        raise OpenError('open_not_authorized') from None

                try:
                    return self._claim_jobs.submit_open(project, runtime_digest=runtime, caller_scope=caller,
                        request_id=identity, request=opening, context=expected_context, before_commit=authorize_open,
                        _ingress=ingress)
                except SQLReadError as error:  # S1-U10b: the database, as the open's own reason
                    raise OpenError('open_account_busy' if error.reason_code == 'sql_read_busy' else 'open_failed') from None
                except Exception as error:
                    if type(getattr(error, 'reason_code', None)) is str:
                        raise  # a refusal with its own public reason (admission capture, claims) stays as it is
                    raise OpenError('open_failed') from None  # S1-U10f: nothing unexplained is account_unavailable
            elif operation == "account.claim_launch":
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
            elif operation in {'account.run_provenance', 'account.open_provenance'}:
                from .claim_storage import capture_storage_evidence
                self._provenance_live()
                limits = self._provenance_limits if operation == 'account.run_provenance' else self._open_provenance_limits
                if limits is None:
                    raise ClaimError('claim_execution_unavailable')
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                caller = authorization.caller_scope
                producer = self._views.run_provenance if operation == 'account.run_provenance' else self._views.open_provenance
                result = producer(project, runtime_digest=runtime, caller_scope=caller,
                                  request=provenance if operation == 'account.run_provenance' else open_provenance,
                                  storage_binding=storage_binding, storage_evidence=storage_evidence, work_limits=limits)
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
            elif operation == 'account.release_capacity':
                from .claim_storage import capture_storage_evidence
                from .release_contracts import capture_cleanup_evidence
                from .terminal_delivery import capture_terminal_evidence
                storage_binding, storage_evidence = capture_storage_evidence(
                    authorization.storage_binding, authorization.storage_evidence)
                cleanup_raw = capture_cleanup_evidence(authorization.cleanup_evidence)
                terminal_raw = capture_terminal_evidence(authorization.terminal_evidence)
                caller = authorization.caller_scope
                original_raw = encode_account_request(request)

                def release_current():
                    self._provenance_live()
                    fresh_request = decode_account_request(original_raw)
                    current = self._authorizer(fresh_request, transport_context)
                    if type(current) is not AccountAuthorization:
                        raise AccountError('account_not_authorized')
                    current.__post_init__()
                    current_binding, current_storage = capture_storage_evidence(current.storage_binding, current.storage_evidence)
                    self._provenance_live()
                    if (current.source != 'project' or current.project_id != project or current.runtime_digest != runtime
                            or current.request_digest != digest or account_request_digest(fresh_request) != digest
                            or current.caller_scope != caller or operation not in current.allowed_operations
                            or current_binding != storage_binding or current_storage != storage_evidence
                            or capture_cleanup_evidence(current.cleanup_evidence) != cleanup_raw
                            or capture_terminal_evidence(current.terminal_evidence) != terminal_raw):
                        raise AccountError('account_not_authorized')
                    self._views._guard()

                release_current()
                result = self._views.release_capacity(project, runtime_digest=runtime, caller_scope=caller,
                    submission=submission, storage_binding=storage_binding, storage_evidence=storage_evidence,
                    terminal_evidence=terminal_raw, cleanup_evidence=cleanup_raw, before_commit=release_current)
                release_current()
            elif operation == 'account.submit_terminal':
                from .claim_storage import capture_storage_evidence
                from .terminal_delivery import capture_terminal_evidence
                storage_binding, storage_evidence = capture_storage_evidence(
                    authorization.storage_binding, authorization.storage_evidence)
                result = self._views.submit_terminal(project, runtime_digest=runtime,
                                                     caller_scope=authorization.caller_scope, submission=submission,
                                                     storage_binding=storage_binding, storage_evidence=storage_evidence,
                                                     terminal_evidence=capture_terminal_evidence(authorization.terminal_evidence))
            elif operation == 'account.record_rate_limit_event':
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                result = self._views.record_rate_limit_event(project, runtime_digest=runtime, event=rate_event,
                                                             storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif operation == 'account.close_denied_attempt':
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                result = self._views.close_denied_attempt(project, runtime_digest=runtime,
                                                          caller_scope=authorization.caller_scope, closure=closure,
                                                          storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif operation == 'account.fence_claim':
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                result = self._views.fence_claim(project, runtime_digest=runtime, caller_scope=authorization.caller_scope,
                                                 fence=fence, storage_binding=storage_binding, storage_evidence=storage_evidence)
            elif operation == 'account.settle_lost_run':
                from .claim_storage import capture_storage_evidence
                storage_binding, storage_evidence = capture_storage_evidence(authorization.storage_binding, authorization.storage_evidence)
                result = self._views.settle_lost_run(project, runtime_digest=runtime, caller_scope=authorization.caller_scope,
                                                     settle=lost, storage_binding=storage_binding,
                                                     storage_evidence=storage_evidence)
            elif operation == 'account.withdraw_open':
                result = self._views.withdraw_open(project, caller_scope=authorization.caller_scope,
                                                   withdrawal=withdrawal, request_digest=digest)
            elif request.operation == "account.health":
                result = {"transport_alive": True, "database_checked": False}
            elif request.operation == "account.provider_state_snapshot":
                self._views._guard()
                result = self._views._ledger.provider_state_snapshot(project).audit()
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
            if operation == 'account.release_capacity':
                release_current()
                with self._front_lock:
                    if self._closed:
                        raise AccountError('account_not_authorized')
                    return reply
            if operation in {'account.run_provenance', 'account.open_provenance'}:
                if self._pid != os.getpid():
                    raise AccountError('account_wrong_owner')
                # Admit the already encoded/validated immutable reply under
                # the same lock as close(). No adapter, SQL or I/O holds it.
                # A close before this point denies; an admitted reply is
                # ordered before a subsequent close.
                with self._front_lock:
                    if self._closed:
                        raise AccountError('account_not_authorized')
                    return reply
            return reply
        except AccountError as exc:
            reason = exc.reason_code
        except ClaimError as exc:
            reason = exc.reason_code
        except OpenError as exc:
            reason = exc.reason_code
        except ObservationError as exc:
            reason = exc.reason_code
        except ProbeAdmissionError as exc:
            reason = exc.reason_code
        except WithdrawError as exc:
            reason = exc.reason_code
        except AdmissionCaptureError as exc:
            reason = public_capture_reason(exc)
        except SQLReadError as exc:  # M4b E1a-2: a busy ledger is answered as busy
            reason = busy_reason(operation) if exc.reason_code == "sql_read_busy" else "account_unavailable"
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


def busy_reason(operation) -> str:
    """M4b E1a-2: the public answer to a busy ledger; an open's own busy reason on open paths
    (review 040-m4b-e1a-code)."""
    return "open_account_busy" if operation == "account.open_host_run" else "account_busy"
