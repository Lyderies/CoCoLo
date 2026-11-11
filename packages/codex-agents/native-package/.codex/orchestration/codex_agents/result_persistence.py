"""Bounded owner-to-project persistence handoff; no native endpoint or grant.

Only the registered bridge owner selects the actual returned worker bytes. One
prestarted project thread owns its history connection and all assignment/result
parsing and file/SQL work. Native enrollment, account completion/release delivery
and normal-client migration remain mandatory before production activation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
import queue
import threading
import time
import weakref

from .bridge_liveness import BridgeLivenessOwner, CodexOwnerResult
from .claim_storage import MAX_STORAGE_EVIDENCE_BYTES
from .execution_grant import MAX_EXECUTION_GRANT_BYTES
from .host_open_contracts import MAX_OPEN_PROVENANCE_BYTES, OpenProvenanceInput
from .run_provenance import MAX_RUN_PROVENANCE_BYTES
from .terminal_delivery import MAX_TERMINAL_EVIDENCE_BYTES
from .claim_denial import MAX_DENIAL_RECEIPT_BYTES
from .cleanup_contracts import MAX_CLEANUP_EVIDENCE_BYTES
from .owned_execution import ExecutionCustodyError, OwnedExecutionKey
from .codex_worker import CodexWorkerError, CodexWorkerHandle, CodexWorkerPacket, CodexWorkerPool, MAX_INPUT_BYTES, MAX_PACKET_BYTES, MAX_REPORT_BYTES, _decode_input
from .contracts import ContractError
from .history_contracts import AttemptBinding, HistoryError, TerminalDelivery, valid_integer
from .learning_contracts import parse_bounded_json
from .project_history import ProjectHistory, _ExecutionProvenanceRejected
from .history_service import HistoryService
from .project_request_queue import ProjectRequestLimits, ProjectRequestQueue
from .project_monitor import _capture_binding
from .result_contracts import (
    MAX_ASSIGNMENT_CANONICAL_BYTES, CodexUsageObservation, ResultLeaseBinding, ResultObservation, assignment_bytes,
)

_TIME_NS = time.time_ns
MAX_PERSISTENCE_ENTRY_BYTES = (MAX_INPUT_BYTES + MAX_ASSIGNMENT_CANONICAL_BYTES + MAX_PACKET_BYTES
                               + MAX_STORAGE_EVIDENCE_BYTES + 2 * MAX_EXECUTION_GRANT_BYTES
                               + 2 * MAX_RUN_PROVENANCE_BYTES
                               + 2 * MAX_OPEN_PROVENANCE_BYTES + 1024
                               + 2 * MAX_TERMINAL_EVIDENCE_BYTES + MAX_DENIAL_RECEIPT_BYTES
                               + 4 * MAX_CLEANUP_EVIDENCE_BYTES)


class ResultPersistenceHandle:
    __slots__ = ("__weakref__",)


@dataclass(repr=False)
class _Pending:
    binding: AttemptBinding | ResultLeaseBinding
    worker_handle: object
    original_job: object
    payload: bytes | None
    deadline_ms: int | None
    state: str = "preparing"
    assignment: bytes | None = None
    identity: tuple | None = None
    inputs_digest: str | None = None
    supervisor: object = None
    selected: object = None
    observed_at_ms: int | None = None
    delivery: TerminalDelivery | None = None
    reason: str | None = None
    lease_binding: ResultLeaseBinding | None = None
    lease: object = None
    claim_binding: AttemptBinding | None = None
    storage_evidence: bytes | None = None
    bound_evidence: bytes | None = None
    grant_raw: bytes | None = None
    provenance_raw: bytes | None = None
    provenance_rejected: bool = False
    open_provenance_raw: bytes | None = None
    open_record: tuple[str, str] | None = None
    grant: object = None
    execution_issued: bool = False
    execution_ticket: object = None
    terminal_evidence: bytes | None = None
    acknowledgement: bytes | None = None
    denial_receipt: bytes | None = None
    cleanup_custody: object = None
    cleanup_raw: bytes | None = None
    release_acknowledgement: bytes | None = None
    release_confirmed: bytes | None = None


class ResultPersistenceLane:
    """One fixed project writer, with finite retained jobs and nonblocking intake.

    prepare_lease()/arm() durably reserve before the trusted frontend consumes
    account launch authority; record_host_open() queues the original account
    receipt before that claim, and bind_claim() associates its actual receipt.
    prepare() retains the earlier known-claim internal reference only. This lane
    does not admit or obtain account registration. consume_execution() commits
    that original registration on the writer before issue_execution() creates
    one project-local ticket; bind() associates its actual registered execution.
    submit() accepts no caller result, flags,
    assignment, record, key, timestamp or cleanup claim. Explicit persistence
    retries retain the first selected bytes and observation time, never execution.
    """

    def __init__(self, owner, pool, open_history, *, max_entries, retained_bytes, allow_local_execution_reference=False,
                 request_limits=None, history_authorizer=None, result_authorizer=None, orchestration_authorizer=None,
                 bootstrap_history=False):
        if (type(owner) is not BridgeLivenessOwner or type(pool) is not CodexWorkerPool
                or not callable(open_history) or type(max_entries) is not int or not 1 <= max_entries <= 64
                or type(retained_bytes) is not int
                or not max_entries * MAX_PERSISTENCE_ENTRY_BYTES <= retained_bytes <= 512 * 1_048_576
                or type(allow_local_execution_reference) is not bool
                or type(bootstrap_history) is not bool
                or (request_limits is not None and type(request_limits) is not ProjectRequestLimits)
                or any(value is not None and not callable(value)
                       for value in (history_authorizer, result_authorizer, orchestration_authorizer))
                or (request_limits is None and any(value is not None
                    for value in (history_authorizer, result_authorizer, orchestration_authorizer)))):
            raise HistoryError("history_invalid_input")
        if request_limits is not None:
            request_limits.__post_init__()
            request_limits = ProjectRequestLimits(request_limits.requests, request_limits.max_wait_ns)
            if max_entries * MAX_PERSISTENCE_ENTRY_BYTES + request_limits.retained_bytes > retained_bytes:
                raise HistoryError("history_invalid_input")
        owner._owner()
        pool._owner()
        self._owner, self._pool, self._open = owner, pool, open_history
        self._project = owner._monitor.scope().project_id
        self._runtime = owner._monitor.scope().runtime_digest
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._maximum = max_entries
        self._allow_local_reference = allow_local_execution_reference
        self._bootstrap_history = bootstrap_history
        self._lock = threading.Lock()
        self._jobs = weakref.WeakKeyDictionary()
        self._by_worker = weakref.WeakKeyDictionary()
        self._requests = None if request_limits is None else ProjectRequestQueue(request_limits)
        self._api_ingress_owner = None
        self._service_authorizers = (history_authorizer, result_authorizer, orchestration_authorizer)
        # API requests cannot consume one slot per result entry or the close marker.
        self._incoming = queue.Queue(maxsize=max_entries + 1 + (0 if request_limits is None else request_limits.requests))
        self._ready = threading.Event()
        self._closed = self._fault = False
        self._store = None
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()
        if not self._ready.wait(5):
            self.close()
            raise HistoryError("history_service_unavailable")
        if self._fault:
            self.close()
            raise HistoryError("history_service_unavailable")

    def queue_request(self, raw, transport_context=None, *, deadline_ns):
        """Trusted frontend intake: immutable frame, bounded token, no SQL/wait."""
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        with self._lock:
            if self._closed or self._fault or self._requests is None:
                raise HistoryError("history_service_unavailable")
            handle = self._requests.prepare(raw, transport_context, deadline_ns=deadline_ns)
            try:
                self._incoming.put_nowait(("api", handle))
            except queue.Full:
                self._requests.reject_unqueued(handle)
                self._fault = True
                raise HistoryError("history_service_unavailable") from None
            return handle

    def take_request(self, handle):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        if self._requests is None:
            raise HistoryError("history_service_unavailable")
        return self._requests.take(handle)

    def abandon_request(self, handle):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        if self._requests is None:
            raise HistoryError("history_service_unavailable")
        self._requests.abandon(handle)

    def request_snapshot(self):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        if self._requests is None:
            raise HistoryError("history_service_unavailable")
        return self._requests.snapshot()

    def _check_owner(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        self._owner._owner()

    def _job(self, handle):
        self._check_owner()
        if type(handle) is not ResultPersistenceHandle or handle not in self._jobs:
            raise HistoryError("history_not_authorized")
        return self._jobs[handle]

    def prepare(self, binding, worker_handle, *, hard_deadline_at_ms):
        """Known-claim reference only; services use prepare_lease before claiming."""
        self._check_owner()
        binding = _capture_binding(binding)
        self._owner._monitor._entry(binding)
        if not valid_integer(hard_deadline_at_ms, minimum=1):
            raise HistoryError("history_invalid_input")
        return self._prepare_entry(binding, worker_handle, hard_deadline_at_ms)

    def prepare_lease(self, binding, worker_handle):
        """Reserve before the account creates its claim ID; launch nothing."""
        self._check_owner()
        if type(binding) is not ResultLeaseBinding:
            raise HistoryError("history_invalid_input")
        binding = ResultLeaseBinding(**binding.as_data())
        scope = self._owner._monitor.scope()
        if any(getattr(scope, name) != getattr(binding, name) for name in vars(scope)):
            raise HistoryError("history_not_authorized")
        return self._prepare_entry(binding, worker_handle, None)

    def _prepare_entry(self, binding, worker_handle, deadline):
        if type(worker_handle) is not CodexWorkerHandle:
            raise HistoryError("history_invalid_input")
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            previous = self._by_worker.get(worker_handle)
            if previous is not None:
                pending = self._jobs[previous]
                initial = pending.lease_binding or pending.binding
                if initial != binding or (pending.lease_binding is None and pending.deadline_ms != deadline):
                    raise HistoryError("history_conflict")
                return previous
            if len(self._jobs) >= self._maximum:
                raise HistoryError("history_result_capacity")
        with self._pool._registry._lock:
            original = self._pool._job(worker_handle)
            if original.state != "prepared" or type(original.payload) is not bytes:
                raise HistoryError("history_not_authorized")
            payload = original.payload
        handle = ResultPersistenceHandle()
        with self._lock:
            pending = _Pending(binding, worker_handle, original, payload, deadline)
            if type(binding) is ResultLeaseBinding:
                pending.lease_binding = binding
            self._jobs[handle] = pending
            self._by_worker[worker_handle] = handle
            self._incoming.put_nowait(("prepare", handle))
        return handle

    def lease(self, handle):
        pending = self._job(handle)
        with self._lock:
            value = pending.lease
            if value is None:
                raise HistoryError("history_result_not_ready")
            return replace(value, binding=ResultLeaseBinding(**value.binding.as_data()))

    def prepared_inputs_digest(self, handle):
        """Bounded metadata computed by the original writer, never a grant."""
        pending = self._job(handle)
        with self._lock:
            if pending.inputs_digest is None:
                raise HistoryError('history_result_not_ready')
            return pending.inputs_digest

    def arm(self, handle):
        """Finish this durable marker before the trusted adapter sends a claim."""
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.lease_binding is None:
                raise HistoryError("history_invalid_input")
            if pending.state in {"arming", "claim_pending"} or pending.claim_binding is not None:
                return
            if pending.state not in {"reserved", "failed"} or pending.lease is None:
                raise HistoryError("history_result_not_ready")
            pending.state, pending.reason = "arming", None
            self._incoming.put_nowait(("arm", handle))

    def storage_evidence(self, handle):
        """Immutable server-to-server data, not a client or native grant."""
        pending = self._job(handle)
        with self._lock:
            if pending.state != "claim_pending" or pending.storage_evidence is None:
                raise HistoryError("history_result_not_ready")
            return pending.storage_evidence

    def record_host_open(self, handle, raw=None):
        """Queue the actual original receipt, using retained prepared inputs.

        This is an internal writer handoff, never native caller authentication.
        The trusted frontend checks the original account peer/current permission
        before supplying it. A retry retains exactly the same bytes, including
        after an unknown committed reply, and cannot create an account claim.
        """
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError('history_service_unavailable')
            if raw is None:
                raw = pending.open_provenance_raw
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_OPEN_PROVENANCE_BYTES:
                raise HistoryError('history_invalid_input')
            if pending.open_provenance_raw is not None and pending.open_provenance_raw != raw:
                raise HistoryError('history_conflict')
            if pending.state == 'recording_host_open':
                return
            if (pending.state not in {'claim_pending', 'failed'} or pending.lease_binding is None
                    or pending.lease is None or pending.lease.state != 'claim_pending'
                    or pending.claim_binding is not None or pending.denial_receipt is not None
                    or pending.grant_raw is not None or pending.supervisor is not None):
                raise HistoryError('history_result_not_ready')
        with self._pool._registry._lock:
            self._prepared_open_worker_locked(pending)
        with self._lock:
            pending.open_provenance_raw, pending.open_record = raw, None
            pending.state, pending.reason = 'recording_host_open', None
            self._incoming.put_nowait(('record_host_open', handle))

    def host_open_record(self, handle):
        """Return only the writer's confirmed original record identity."""
        pending = self._job(handle)
        with self._pool._registry._lock:
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                if pending.state != 'claim_pending' or pending.open_record is None:
                    raise HistoryError('history_result_not_ready')
                self._prepared_open_worker_locked(pending)
                identity, digest = pending.open_record
                return {'record_id': identity, 'content_digest': digest}

    def _prepared_open_worker_locked(self, pending):
        """Caller holds the pool registry lock; no SQL, callbacks or file I/O."""
        original = self._pool._jobs.get(pending.worker_handle)
        if (self._pool._closed or original is not pending.original_job or original.state != 'prepared'
                or original.payload is not pending.payload or original.slot.current is not pending.worker_handle
                or original.slot.fault or not original.slot.thread.is_alive()):
            raise HistoryError('history_not_authorized')

    def bound_storage_evidence(self, handle):
        pending = self._job(handle)
        with self._lock:
            if pending.bound_evidence is None or pending.lease is None or pending.lease.state != "bound":
                raise HistoryError("history_result_not_ready")
            return pending.bound_evidence

    def pending_storage_evidence(self, handle):
        """Original writer's historical pending metadata; not fresh admission."""
        pending = self._job(handle)
        with self._lock:
            if pending.state not in {'claim_pending', 'lease_claim_pending'} or pending.storage_evidence is None:
                raise HistoryError('history_result_not_ready')
            return pending.storage_evidence

    def settle_denial(self, handle, raw=None):
        """Queue one original native-account closure; no request/model retry."""
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError('history_service_unavailable')
            if raw is None:
                raw = pending.denial_receipt
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_DENIAL_RECEIPT_BYTES:
                raise HistoryError('history_invalid_input')
            if pending.denial_receipt is not None and pending.denial_receipt != raw:
                raise HistoryError('history_conflict')
            if pending.state in {'denial_settling', 'denied'}:
                return
            if (pending.state not in {'claim_pending', 'lease_claim_pending', 'failed'} or pending.lease_binding is None
                    or pending.claim_binding is not None or pending.grant is not None or pending.supervisor is not None):
                raise HistoryError('history_result_not_ready')
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            if original is not pending.original_job or original.state != 'prepared' or original.payload is not pending.payload:
                raise HistoryError('history_not_authorized')
        with self._lock:
            pending.denial_receipt, pending.state, pending.reason = raw, 'denial_settling', None
            self._incoming.put_nowait(('settle_denial', handle))

    def retire_denied(self, handle):
        pending = self._job(handle)
        with self._lock:
            if pending.state != 'denied' or pending.lease.state != 'expired' or pending.execution_issued:
                raise HistoryError('history_result_not_ready')
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            if original is not pending.original_job or original.state != 'prepared' or original.payload is not pending.payload:
                raise HistoryError('history_not_authorized')
        self._pool.withdraw_prepared(pending.worker_handle)
        with self._lock:
            self._jobs.pop(handle)
            self._by_worker.pop(pending.worker_handle)

    def bind_claim(self, handle, binding, *, hard_deadline_at_ms):
        """After trusted account checks, queue the exact actual claim bind.

        This metadata is not account authentication. Native enrollment and the
        actual account receipt checks still precede production activation.
        """
        pending = self._job(handle)
        binding = _capture_binding(binding)
        # This is persistence binding, before an execution registration exposes
        # the original monotonic deadline. No placeholder monitor claim is needed.
        if not valid_integer(hard_deadline_at_ms, minimum=1):
            raise HistoryError("history_invalid_input")
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.lease_binding is None or not pending.lease_binding.matches_attempt(binding):
                raise HistoryError("history_conflict")
            if pending.claim_binding is not None:
                if pending.claim_binding != binding or pending.deadline_ms != hard_deadline_at_ms:
                    raise HistoryError("history_conflict")
                if pending.state != "failed":
                    return
            elif pending.state != "claim_pending":
                raise HistoryError("history_result_not_ready")
            pending.claim_binding, pending.deadline_ms = binding, hard_deadline_at_ms
            pending.state, pending.reason = "binding_claim", None
            self._incoming.put_nowait(("bind_claim", handle))

    def bind(self, handle):
        pending = self._job(handle)
        with self._lock:
            if (self._closed or self._fault or pending.state not in {"reserved", "execution_issued"}
                    or type(pending.binding) is not AttemptBinding):
                raise HistoryError("history_result_not_ready")
        binding = self._owner._worker(pending.binding, self._pool, pending.worker_handle)
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            if (original is not pending.original_job or original.state != "reserved"
                    or (pending.supervisor is None and original.payload is not pending.payload)):
                raise HistoryError("history_not_authorized")
            if (pending.lease_binding is not None and not self._allow_local_reference
                    and (pending.execution_ticket is None or original.ticket is not pending.execution_ticket)):
                raise HistoryError("history_not_authorized")
            supervisor = original.supervisor
        with self._lock:
            # No caller may replace the registered execution on a retry.
            if pending.binding != binding or (pending.supervisor is not None and pending.supervisor is not supervisor):
                raise HistoryError("history_conflict")
            pending.supervisor = supervisor
            pending.payload = None
            pending.state = "reserved"

    def consume_execution(self, handle, raw, *, provenance=None):
        """Queue a server-authenticated account registration for durable use.

        No ticket is accepted or returned here. Native peer identity and the
        enrolled supervisor epoch must be established before this internal call.
        """
        pending = self._job(handle)
        self._owner._monitor._entry(pending.binding)
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_EXECUTION_GRANT_BYTES:
            raise HistoryError("history_invalid_input")
        if provenance is not None and (type(provenance) is not bytes or not 0 < len(provenance) <= MAX_RUN_PROVENANCE_BYTES):
            raise HistoryError("history_invalid_input")
        if provenance is None and not self._allow_local_reference:
            # Refuse before capturing the immutable retry identity or queuing
            # work, so an absent receipt cannot poison the original grant.
            raise HistoryError('history_execution_not_ready')
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.grant_raw is not None and pending.grant_raw != raw:
                raise HistoryError("history_conflict")
            if (pending.grant_raw is not None and pending.provenance_raw != provenance
                    and not (pending.state == 'failed' and pending.provenance_rejected)):
                raise HistoryError("history_conflict")
            if pending.state in {"consuming_execution", "execution_consumed", "execution_issued"}:
                return
            if (pending.state not in {"reserved", "failed"} or pending.supervisor is not None
                    or pending.lease is None or pending.lease.state != "bound"):
                raise HistoryError("history_execution_not_ready")
            pending.grant_raw, pending.state, pending.reason = raw, "consuming_execution", None
            pending.provenance_raw = provenance
            pending.provenance_rejected = False
            self._incoming.put_nowait(("consume_execution", handle))

    def issue_execution(self, handle):
        """Only the actual original writer's first committed return issues custody.

        The ticket/registry belongs to this project process and owner. Metadata
        reads, copied handles, retries and lost writer replies cannot issue one.
        """
        pending = self._job(handle)
        status = self._owner._monitor.status(pending.binding)
        with self._lock:
            if self._closed or self._fault or pending.state != "execution_consumed" or pending.execution_issued:
                raise HistoryError("history_execution_not_ready")
            grant = pending.grant
            entry = self._owner._monitor._entry(pending.binding)
            if grant is None or entry.deadline != grant.deadline_ns or status.stop_requested:
                raise HistoryError("history_execution_not_ready")
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            if original is not pending.original_job or original.state != "prepared" or original.payload is not pending.payload:
                raise HistoryError("history_not_authorized")
            with self._lock:
                pending.execution_issued = True
                pending.state = "execution_unknown"  # An interruption cannot restore launch eligibility.
            ticket = self._pool._registry.issue(OwnedExecutionKey(
                pending.binding.project_id, grant.handle_digest, pending.binding.run_id, pending.binding.claim_id),
                deadline_ns=grant.deadline_ns, run_handle=pending.binding.run_handle)
            with self._lock:
                pending.execution_ticket, pending.state = ticket, "execution_issued"
                pending.bound_evidence = None
            return ticket

    def submit(self, handle):
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.state in {"queued", "committing", "committed", "acknowledging", "acknowledged"}:
                return
            if pending.state != "reserved" or pending.supervisor is None:
                raise HistoryError("history_result_not_ready")
        # Only this original owner's actual cached decision is taken. Large body
        # bytes remain immutable references; persistence never calls the owner.
        selected = self._owner.result(pending.binding, self._pool, pending.worker_handle)
        if (selected.packet.error_code is not None or selected.packet.body is None
                or selected.packet.identity != pending.identity):
            raise HistoryError("history_result_unavailable")
        with self._lock:
            packet = selected.packet
            pending.selected = CodexOwnerResult(CodexWorkerPacket(
                packet.identity, packet.body, packet.report, packet.fenced, packet.reviewed,
                packet.terminal_state, packet.validation_reason, packet.error_code), selected.stop_reason)
            pending.observed_at_ms = _TIME_NS() // 1_000_000
            pending.state = "queued"
            self._incoming.put_nowait(("publish", handle))

    def retry(self, handle):
        """Explicit exact persistence reconciliation; never enqueue model work."""
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.state in {"queued", "committing", "committed", "acknowledging", "acknowledged"}:
                return
            if pending.state != "failed" or pending.selected is None:
                raise HistoryError("history_result_not_ready")
            pending.state, pending.reason = "queued", None
            self._incoming.put_nowait(("publish", handle))

    def snapshot(self, handle):
        pending = self._job(handle)
        with self._lock:
            return {"state": pending.state, "reason_code": pending.reason,
                    "bound": pending.supervisor is not None, "committed": pending.delivery is not None}

    def terminal_evidence(self, handle):
        pending = self._job(handle)
        with self._lock:
            if pending.terminal_evidence is None or pending.delivery is None:
                raise HistoryError('history_result_not_ready')
            return pending.terminal_evidence

    def acknowledge_terminal(self, handle, raw=None):
        """Queue original authenticated account metadata; no cleanup claim.

        Parsing/MAC/storage stay on the writer. An explicit retry without raw
        retains the first immutable reply after a lost local commit response.
        """
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError('history_service_unavailable')
            if raw is None:
                raw = pending.acknowledgement
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_TERMINAL_EVIDENCE_BYTES:
                raise HistoryError('history_invalid_input')
            if pending.acknowledgement is not None and pending.acknowledgement != raw:
                raise HistoryError('history_conflict')
            if pending.state in {'acknowledging', 'acknowledged'}:
                return
            if (pending.state not in {'committed', 'failed'} or pending.delivery is None
                    or pending.terminal_evidence is None or pending.grant is None):
                raise HistoryError('history_result_not_ready')
            pending.acknowledgement, pending.state, pending.reason = raw, 'acknowledging', None
            self._incoming.put_nowait(('acknowledge_terminal', handle))

    def retain_cleanup(self, handle):
        """Capture original native custody for this writer after terminal ACK.

        The opaque reference carries no account release or native endpoint
        authority. Durable cleanup issuance and authenticated delivery remain
        separate steps; uncertainty keeps both the execution and slot held.
        """
        self._check_owner()
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault or self._allow_local_reference:
                raise HistoryError('history_not_authorized')
            if (pending.state != 'acknowledged' or pending.execution_ticket is None
                    or not pending.execution_issued or pending.grant is None or pending.delivery is None
                    or pending.acknowledgement is None or pending.terminal_evidence is None):
                raise HistoryError('history_result_not_ready')
            grant, binding, ticket = pending.grant, pending.binding, pending.execution_ticket
        registry = self._pool._registry
        with registry._lock:
            original = self._pool._jobs.get(pending.worker_handle)
            if original is not pending.original_job or original.ticket is not ticket:
                raise HistoryError('history_not_authorized')
            try:
                proof = registry.retain_cleanup(ticket, self._pool, pending.worker_handle, consumer=self._writer)
                snapshot = registry.cleanup_snapshot(proof)
            except ExecutionCustodyError:
                raise HistoryError('history_not_authorized') from None
            if (snapshot['key'] != OwnedExecutionKey(binding.project_id, grant.handle_digest, binding.run_id, binding.claim_id)
                    or snapshot['deadline_ns'] != grant.deadline_ns):
                raise HistoryError('history_not_authorized')
            with self._lock:
                if pending.cleanup_custody is not None and pending.cleanup_custody is not proof:
                    raise HistoryError('history_conflict')
                pending.cleanup_custody = proof
            return proof

    def prepare_cleanup(self, handle):
        """Queue original returned-worker custody; no SQL on the control path."""
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault or self._allow_local_reference:
                raise HistoryError('history_not_authorized')
            if pending.state in {'preparing_cleanup', 'cleanup_ready'}:
                return
            if pending.state not in {'acknowledged', 'failed'} or pending.lease_binding is None:
                raise HistoryError('history_result_not_ready')
            retry = pending.cleanup_custody is not None
            if retry and (pending.acknowledgement is None or pending.delivery is None or pending.grant is None):
                raise HistoryError('history_result_not_ready')
        if not retry:
            self.retain_cleanup(handle)
        self._cleanup_snapshot(pending)
        with self._lock:
            pending.state, pending.reason, pending.cleanup_raw = 'preparing_cleanup', None, None
            self._incoming.put_nowait(('prepare_cleanup', handle))

    def _cleanup_snapshot(self, pending):
        registry = self._pool._registry
        try:
            with registry._lock:
                original = self._pool._jobs.get(pending.worker_handle)
                if original is not pending.original_job or original.ticket is not pending.execution_ticket:
                    raise ExecutionCustodyError()
                snapshot = registry.cleanup_snapshot(pending.cleanup_custody)
                grant = pending.grant
                if (grant is None or snapshot['key'] != OwnedExecutionKey(pending.binding.project_id,
                        grant.handle_digest, pending.binding.run_id, pending.binding.claim_id)
                        or snapshot['deadline_ns'] != grant.deadline_ns):
                    raise ExecutionCustodyError()
        except ExecutionCustodyError:
            raise HistoryError('history_not_authorized') from None

    def cleanup_evidence(self, handle):
        pending = self._job(handle)
        with self._pool._registry._lock:
            self._cleanup_snapshot(pending)
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                if pending.state != 'cleanup_ready' or pending.cleanup_raw is None:
                    raise HistoryError('history_result_not_ready')
                return pending.cleanup_raw

    def delivery(self, handle):
        pending = self._job(handle)
        with self._lock:
            if pending.state not in {"committed", "acknowledging", "acknowledged", "preparing_cleanup", "cleanup_ready",
                                     "acknowledging_release", "release_acknowledged", "released"} or pending.delivery is None:
                raise HistoryError("history_result_not_ready")
            value = pending.delivery
            # Return a metadata copy, never the registry's retained authority.
            return TerminalDelivery(value.delivery_sequence, _capture_binding(value.binding),
                                    value.record_id, value.record_content_digest, value.result_digest)

    def acknowledge_release(self, handle, raw=None):
        """Queue original account settlement; do not yet free native custody."""
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault or self._allow_local_reference:
                raise HistoryError('history_not_authorized')
            if raw is None:
                raw = pending.release_acknowledgement
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_CLEANUP_EVIDENCE_BYTES:
                raise HistoryError('history_invalid_input')
            if pending.release_acknowledgement is not None and pending.release_acknowledgement != raw:
                raise HistoryError('history_conflict')
            if pending.state in {'acknowledging_release', 'release_acknowledged', 'released'}:
                return
            if (pending.state not in {'cleanup_ready', 'failed'} or pending.cleanup_raw is None
                    or pending.cleanup_custody is None or pending.lease_binding is None):
                raise HistoryError('history_result_not_ready')
        self._cleanup_snapshot(pending)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError('history_service_unavailable')
            pending.release_acknowledgement = raw
            pending.state, pending.reason = 'acknowledging_release', None
            self._incoming.put_nowait(('acknowledge_release', handle))

    def release_receipt(self, handle):
        pending = self._job(handle)
        with self._pool._registry._lock:
            self._cleanup_snapshot(pending)
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                if pending.state != 'release_acknowledged' or pending.release_confirmed is None:
                    raise HistoryError('history_result_not_ready')
                return pending.release_confirmed

    def finish_release(self, handle):
        """No SQL or wait: finish only the writer-confirmed original execution."""
        pending = self._job(handle)
        registry = self._pool._registry
        with registry._lock:
            original = self._pool._jobs.get(pending.worker_handle)
            if original is not pending.original_job or original.ticket is not pending.execution_ticket:
                raise HistoryError('history_not_authorized')
            with self._lock:
                if self._closed or self._fault or pending.release_confirmed is None:
                    raise HistoryError('history_result_not_ready')
                if pending.state == 'released':
                    entry = registry._issued.get(pending.execution_ticket)
                    if entry is None or entry.state != 'released':
                        raise HistoryError('history_not_authorized')
                    return
                if pending.state != 'release_acknowledged':
                    raise HistoryError('history_result_not_ready')
                self._cleanup_snapshot(pending)
                try:
                    registry.acknowledge_release(pending.execution_ticket)
                except ExecutionCustodyError:
                    raise HistoryError('history_not_authorized') from None
                pending.state = 'released'

    def retire(self, handle):
        """Free local memory only after commit AND original account release."""
        pending = self._job(handle)
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            entry = self._pool._registry._issued.get(original.ticket)
            if original is not pending.original_job or entry is None or entry.state != "released":
                raise HistoryError("history_not_authorized")
        with self._lock:
            if pending.state not in {"committed", "acknowledged", "cleanup_ready", "released"}:
                raise HistoryError("history_result_not_ready")
            self._jobs.pop(handle)
            self._by_worker.pop(pending.worker_handle)

    def close(self):
        self._check_owner()
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._requests is not None:
                    self._requests.close()
                self._incoming.put_nowait(None)

    def _history(self):
        if self._store is None:
            store = self._open()
            if type(store) is not ProjectHistory or store.project_id != self._project:
                if type(store) is ProjectHistory:
                    store.close()
                raise HistoryError("history_service_unavailable")
            self._store = store
        return self._store

    def _prepare(self, pending):
        invalid = False
        try:
            original, assignment = _decode_input(pending.payload)
        except (CodexWorkerError, ContractError):
            invalid = True
        if invalid:
            raise HistoryError("history_invalid_input")
        encoded = assignment_bytes(assignment, pending.binding)
        from .open_scope_contracts import open_inputs_digest
        inputs_digest = open_inputs_digest(assignment, original['task'], original['route'], original['parent_grant'])
        if pending.lease_binding is not None:
            lease = self._history().reserve_result_lease(pending.lease_binding, assignment)
        else:
            self._history().reserve_result(pending.binding, assignment, hard_deadline_at_ms=pending.deadline_ms)
            lease = None
        historical = (self._history().attest_pending_result_lease(pending.lease_binding)
                      if lease is not None and lease.state == 'claim_pending' else None)
        with self._lock:
            pending.assignment = encoded
            pending.inputs_digest = inputs_digest
            pending.identity = (assignment["assignment_id"], assignment["attempt"],
                                assignment.get("retry_of_assignment_id"))
            pending.state = ("reserved" if lease is None or lease.state == "reserved" else "lease_" + lease.state)
            pending.lease = lease
            pending.storage_evidence = historical

    def _arm(self, pending):
        lease = self._history().arm_result_lease(pending.lease_binding)
        evidence = self._history().attest_result_lease(pending.lease_binding)
        with self._lock:
            pending.storage_evidence = evidence
            pending.lease, pending.state, pending.reason = lease, "claim_pending", None

    def _bind_claim(self, pending):
        assignment = parse_bounded_json(pending.assignment, max_bytes=MAX_ASSIGNMENT_CANONICAL_BYTES)
        self._history().bind_result_lease(pending.lease_binding, pending.claim_binding, assignment,
                                         hard_deadline_at_ms=pending.deadline_ms)
        lease = self._history().result_lease(pending.lease_binding)
        evidence = self._history().attest_bound_result_lease(pending.lease_binding)
        with self._lock:
            pending.binding = pending.claim_binding
            pending.storage_evidence = None
            pending.bound_evidence = evidence
            pending.lease, pending.state, pending.reason = lease, "reserved", None

    def _record_host_open(self, pending):
        data, assignment = _decode_input(pending.payload)
        try:
            proof = parse_bounded_json(pending.open_provenance_raw, max_bytes=MAX_OPEN_PROVENANCE_BYTES)
        except ContractError:
            raise HistoryError('history_invalid_input') from None
        request = OpenProvenanceInput(pending.lease_binding.run_handle, assignment['assignment_id'],
            data['task'].get('verification_for_outcome'))

        def still_prepared():
            # Brief custody check only: never retain this lock through SQL or
            # private-file work, so control/cancellation remain independent.
            with self._pool._registry._lock:
                self._prepared_open_worker_locked(pending)

        entry = self._history().record_host_open(pending.lease_binding, request, proof,
            assignment=assignment, task=data['task'], route=data['route'], before_commit=still_prepared)
        # A committed journal remains immutable if custody ended during I/O.
        # Admit the acknowledgement only under the same brief lock order used
        # for execution issue; neither lock is retained across the store call.
        with self._pool._registry._lock:
            self._prepared_open_worker_locked(pending)
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                pending.open_record = (entry.record_id, entry.content_digest)
                pending.state, pending.reason = 'claim_pending', None

    def _consume_execution(self, pending):
        data, _ = _decode_input(pending.payload)
        options = {'task': data['task'], 'route': data['route']}
        if pending.provenance_raw is not None:
            try:
                options['provenance'] = parse_bounded_json(pending.provenance_raw, max_bytes=MAX_RUN_PROVENANCE_BYTES)
            except ContractError:
                with self._lock:
                    pending.provenance_rejected = True
                raise HistoryError('history_execution_not_ready') from None
        try:
            grant = self._history().consume_execution_grant(pending.binding, pending.grant_raw, **options)
        except _ExecutionProvenanceRejected:
            with self._lock:
                pending.provenance_rejected = True
            raise HistoryError('history_execution_not_ready') from None
        with self._lock:
            pending.grant, pending.state, pending.reason = grant, "execution_consumed", None

    def _publish(self, pending):
        selected = pending.selected.packet
        observation = ResultObservation(pending.binding.claim_id, pending.observed_at_ms,
                                        selected.fenced, selected.reviewed, selected.terminal_state,
                                        selected.validation_reason)
        assignment = parse_bounded_json(pending.assignment, max_bytes=MAX_ASSIGNMENT_CANONICAL_BYTES)
        report = (None if selected.report is None else parse_bounded_json(selected.report, max_bytes=MAX_REPORT_BYTES))
        if report is not None and (type(report) is not dict or report.get("provider") != "codex"
                                   or report.get("requested_model") != assignment["model"]):
            raise HistoryError("history_invalid_input")
        usage = CodexUsageObservation(assignment["model"], None if report is None else report.get("usage"))
        delivery = self._history().publish_result(pending.binding, assignment, selected.body, observation, client_usage=usage)
        if type(delivery) is not TerminalDelivery or delivery.binding != pending.binding:
            raise HistoryError("history_service_unavailable")
        delivery.__post_init__()
        evidence = (None if pending.grant is None else self._history().attest_terminal_delivery(pending.binding))
        with self._lock:
            pending.delivery, pending.state, pending.reason = delivery, "committed", None
            pending.terminal_evidence = evidence
            pending.selected = None

    def _acknowledge_terminal(self, pending):
        self._history().acknowledge_account_terminal(pending.binding, pending.acknowledgement)
        with self._lock:
            pending.state, pending.reason = 'acknowledged', None

    def _prepare_cleanup(self, pending):
        self._cleanup_snapshot(pending)
        raw = self._history().issue_cleanup(pending.lease_binding, registry=self._pool._registry,
                                             custody=pending.cleanup_custody)
        with self._pool._registry._lock:
            self._cleanup_snapshot(pending)
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                pending.cleanup_raw, pending.state, pending.reason = raw, 'cleanup_ready', None

    def _acknowledge_release(self, pending):
        self._cleanup_snapshot(pending)
        raw = self._history().record_account_release(pending.lease_binding, pending.release_acknowledgement,
            registry=self._pool._registry, custody=pending.cleanup_custody)
        with self._pool._registry._lock:
            self._cleanup_snapshot(pending)
            with self._lock:
                if self._closed or self._fault:
                    raise HistoryError('history_service_unavailable')
                pending.release_confirmed = raw
                pending.state, pending.reason = 'release_acknowledged', None

    def _settle_denial(self, pending):
        value = self._history().settle_denied_result_lease(pending.lease_binding, pending.denial_receipt)
        with self._lock:
            pending.lease, pending.state, pending.reason = value, 'denied', None
            pending.storage_evidence = None

    def _run(self):
        operations = {"prepare": self._prepare, "publish": self._publish,
                      "arm": self._arm, "bind_claim": self._bind_claim,
                      "record_host_open": self._record_host_open,
                      "consume_execution": self._consume_execution,
                      "settle_denial": self._settle_denial,
                      "acknowledge_terminal": self._acknowledge_terminal,
                      "prepare_cleanup": self._prepare_cleanup,
                      "acknowledge_release": self._acknowledge_release}
        service = None
        pending = None
        try:
            if self._bootstrap_history:
                # Trusted runtime startup only, never selected by a request.
                # Validate schema and acquire the existing exclusive writer lock
                # before the native frontend publishes any endpoint.
                self._history()
            if self._requests is not None:
                history_auth, result_auth, orchestration_auth = self._service_authorizers
                service = HistoryService(self._project, self._runtime, self._history,
                    authorizer=history_auth, result_authorizer=result_auth,
                    orchestration_authorizer=orchestration_auth, close_history=False)
            self._ready.set()
            while True:
                command = self._incoming.get()
                if command is None:
                    return
                operation, handle = command
                if operation == "api":
                    self._requests.execute(handle, service)
                    handle = command = None
                    continue
                with self._lock:
                    pending = self._jobs[handle]
                    if operation == "publish":
                        pending.state = "committing"
                try:
                    operations[operation](pending)
                except Exception as error:
                    reason = (HistoryError(error.reason_code).reason_code if type(error) is HistoryError
                              else "history_service_unavailable")
                    with self._lock:
                        pending.state, pending.reason = "failed", reason
                        if operation == "prepare":
                            pending.payload = None
                pending = handle = command = None
        except BaseException:
            with self._lock:
                self._fault = True
                for retained in self._jobs.values():
                    if retained.state not in {"committed", "failed"}:
                        retained.state, retained.reason = "unknown", "history_service_unavailable"
        finally:
            self._ready.set()
            if service is not None:
                try:
                    service.close()
                    self._requests.finish_writer(service)
                except BaseException:
                    with self._lock:
                        self._fault = True
            if self._store is not None:
                try:
                    self._store.close()
                except BaseException:
                    with self._lock:
                        self._fault = True
