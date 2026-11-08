"""Bounded owner-to-project persistence handoff; no native endpoint or grant.

Only the registered bridge owner selects the actual returned worker bytes. One
prestarted project thread owns its history connection and all assignment/result
parsing and file/SQL work. Native enrollment, account completion/release delivery
and normal-client migration remain mandatory before production activation.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import threading
import time
import weakref

from .bridge_liveness import BridgeLivenessOwner, CodexOwnerResult
from .codex_worker import CodexWorkerError, CodexWorkerHandle, CodexWorkerPacket, CodexWorkerPool, MAX_INPUT_BYTES, MAX_PACKET_BYTES, _decode_input
from .contracts import ContractError
from .history_contracts import AttemptBinding, HistoryError, TerminalDelivery, valid_integer
from .learning_contracts import parse_bounded_json
from .project_history import ProjectHistory
from .project_monitor import _capture_binding
from .result_contracts import MAX_ASSIGNMENT_CANONICAL_BYTES, ResultObservation, assignment_bytes

_TIME_NS = time.time_ns
MAX_PERSISTENCE_ENTRY_BYTES = MAX_INPUT_BYTES + MAX_ASSIGNMENT_CANONICAL_BYTES + MAX_PACKET_BYTES


class ResultPersistenceHandle:
    __slots__ = ("__weakref__",)


@dataclass(repr=False)
class _Pending:
    binding: AttemptBinding
    worker_handle: object
    original_job: object
    payload: bytes | None
    deadline_ms: int
    state: str = "preparing"
    assignment: bytes | None = None
    identity: tuple | None = None
    supervisor: object = None
    selected: object = None
    observed_at_ms: int | None = None
    delivery: TerminalDelivery | None = None
    reason: str | None = None


class ResultPersistenceLane:
    """One fixed project writer, with finite retained jobs and nonblocking intake.

    prepare() must precede binding/submitting the provider worker. The trusted
    frontend must additionally reserve before consuming account launch authority;
    this lane does not admit, register or launch anything. bind() then associates
    the actual registered execution. submit() accepts no caller result, flags,
    assignment, record, key, timestamp or cleanup claim. Explicit persistence
    retries retain the first selected bytes and observation time, never execution.
    """

    def __init__(self, owner, pool, open_history, *, max_entries, retained_bytes):
        if (type(owner) is not BridgeLivenessOwner or type(pool) is not CodexWorkerPool
                or not callable(open_history) or type(max_entries) is not int or not 1 <= max_entries <= 64
                or type(retained_bytes) is not int
                or not max_entries * MAX_PERSISTENCE_ENTRY_BYTES <= retained_bytes <= 512 * 1_048_576):
            raise HistoryError("history_invalid_input")
        owner._owner()
        pool._owner()
        self._owner, self._pool, self._open = owner, pool, open_history
        self._project = owner._monitor.scope().project_id
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._maximum = max_entries
        self._lock = threading.Lock()
        self._jobs = weakref.WeakKeyDictionary()
        self._by_worker = weakref.WeakKeyDictionary()
        self._incoming = queue.Queue(maxsize=max_entries + 1)  # Reserved close marker.
        self._ready = threading.Event()
        self._closed = self._fault = False
        self._store = None
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()
        if not self._ready.wait(5):
            self.close()
            raise HistoryError("history_service_unavailable")

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
        self._check_owner()
        binding = _capture_binding(binding)
        self._owner._monitor._entry(binding)
        if type(worker_handle) is not CodexWorkerHandle or not valid_integer(hard_deadline_at_ms, minimum=1):
            raise HistoryError("history_invalid_input")
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            previous = self._by_worker.get(worker_handle)
            if previous is not None:
                pending = self._jobs[previous]
                if pending.binding != binding or pending.deadline_ms != hard_deadline_at_ms:
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
            self._jobs[handle] = _Pending(binding, worker_handle, original, payload, hard_deadline_at_ms)
            self._by_worker[worker_handle] = handle
            self._incoming.put_nowait(("prepare", handle))
        return handle

    def bind(self, handle):
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault or pending.state != "reserved":
                raise HistoryError("history_result_not_ready")
        binding = self._owner._worker(pending.binding, self._pool, pending.worker_handle)
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            if (original is not pending.original_job or original.state != "reserved"
                    or (pending.supervisor is None and original.payload is not pending.payload)):
                raise HistoryError("history_not_authorized")
            supervisor = original.supervisor
        with self._lock:
            # No caller may replace the registered execution on a retry.
            if pending.binding != binding or (pending.supervisor is not None and pending.supervisor is not supervisor):
                raise HistoryError("history_conflict")
            pending.supervisor = supervisor
            pending.payload = None

    def submit(self, handle):
        pending = self._job(handle)
        with self._lock:
            if self._closed or self._fault:
                raise HistoryError("history_service_unavailable")
            if pending.state in {"queued", "committing", "committed"}:
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
            if pending.state in {"queued", "committing", "committed"}:
                return
            if pending.state != "failed" or pending.selected is None:
                raise HistoryError("history_result_not_ready")
            pending.state, pending.reason = "queued", None
            self._incoming.put_nowait(("publish", handle))

    def snapshot(self, handle):
        pending = self._job(handle)
        with self._lock:
            return {"state": pending.state, "reason_code": pending.reason,
                    "bound": pending.supervisor is not None, "committed": pending.state == "committed"}

    def delivery(self, handle):
        pending = self._job(handle)
        with self._lock:
            if pending.state != "committed" or pending.delivery is None:
                raise HistoryError("history_result_not_ready")
            value = pending.delivery
            # Return a metadata copy, never the registry's retained authority.
            return TerminalDelivery(value.delivery_sequence, _capture_binding(value.binding),
                                    value.record_id, value.record_content_digest, value.result_digest)

    def retire(self, handle):
        """Free local memory only after commit AND original account release."""
        pending = self._job(handle)
        with self._pool._registry._lock:
            original = self._pool._job(pending.worker_handle)
            entry = self._pool._registry._issued.get(original.ticket)
            if original is not pending.original_job or entry is None or entry.state != "released":
                raise HistoryError("history_not_authorized")
        with self._lock:
            if pending.state != "committed":
                raise HistoryError("history_result_not_ready")
            self._jobs.pop(handle)
            self._by_worker.pop(pending.worker_handle)

    def close(self):
        self._check_owner()
        with self._lock:
            if not self._closed:
                self._closed = True
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
            _, assignment = _decode_input(pending.payload)
        except (CodexWorkerError, ContractError):
            invalid = True
        if invalid:
            raise HistoryError("history_invalid_input")
        encoded = assignment_bytes(assignment, pending.binding)
        self._history().reserve_result(pending.binding, assignment, hard_deadline_at_ms=pending.deadline_ms)
        with self._lock:
            pending.assignment = encoded
            pending.identity = (assignment["assignment_id"], assignment["attempt"],
                                assignment.get("retry_of_assignment_id"))
            pending.state = "reserved"

    def _publish(self, pending):
        selected = pending.selected.packet
        observation = ResultObservation(pending.binding.claim_id, pending.observed_at_ms,
                                        selected.fenced, selected.reviewed, selected.terminal_state,
                                        selected.validation_reason)
        assignment = parse_bounded_json(pending.assignment, max_bytes=MAX_ASSIGNMENT_CANONICAL_BYTES)
        delivery = self._history().publish_result(pending.binding, assignment, selected.body, observation)
        if type(delivery) is not TerminalDelivery or delivery.binding != pending.binding:
            raise HistoryError("history_service_unavailable")
        delivery.__post_init__()
        with self._lock:
            pending.delivery, pending.state, pending.reason = delivery, "committed", None
            pending.selected = None

    def _run(self):
        self._ready.set()
        pending = None
        try:
            while True:
                command = self._incoming.get()
                if command is None:
                    return
                operation, handle = command
                with self._lock:
                    pending = self._jobs[handle]
                    if operation == "publish":
                        pending.state = "committing"
                try:
                    (self._prepare if operation == "prepare" else self._publish)(pending)
                except Exception as error:
                    reason = (HistoryError(error.reason_code).reason_code if type(error) is HistoryError
                              else "history_service_unavailable")
                    with self._lock:
                        pending.state, pending.reason = "failed", reason
                        pending.payload = None
                pending = handle = command = None
        except BaseException:
            with self._lock:
                self._fault = True
                for retained in self._jobs.values():
                    if retained.state not in {"committed", "failed"}:
                        retained.state, retained.reason = "unknown", "history_service_unavailable"
        finally:
            if self._store is not None:
                try:
                    self._store.close()
                except BaseException:
                    with self._lock:
                        self._fault = True
