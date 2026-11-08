"""Prestarted fixed Codex workers and bounded, actual-returned result custody.

Trusted supervisor code supplies already authorized/exported inputs and an actual
execution ticket. Neither this adapter, input bytes nor returned packets establish
native caller/client/root authority. No listener, SQL fallback, admission, release,
automatic retry or production client migration is enabled here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import queue
import threading
import time
import weakref

from . import bridge
from .contracts import RESULT_VALIDATION_REASON_CODES, ContractError, validate_assignment
from .learning_contracts import SUPPORTED_EFFORTS, SUPPORTED_MODELS, canonical_json, parse_bounded_json
from .owned_execution import ExecutionCustodyError, ExecutionTicket, OwnedExecutionRegistry
from .result_contracts import MAX_RESULT_BODY_BYTES

MAX_INPUT_BYTES = 32 * 1_048_576
MAX_REPORT_BYTES = 16_384
MAX_PACKET_BYTES = MAX_RESULT_BODY_BYTES + MAX_REPORT_BYTES + 4096
_RUN_CODEX = bridge.run_codex
_PREPARE_RESULT = bridge.prepare_dispatch_result
_OUTCOMES = frozenset({
    "result", "launch_failed", "process_tracking_unavailable", "watchdog_unavailable",
    "unparseable_stream", "stream_too_large", "timed_out", "cancelled", "startup_silence",
    "work_silence", "monitor_unavailable", "codex_activity_invalid", "child_reported_error",
    "missing_result", "unparseable_result", "invalid_result", "result_too_large",
    *("codex_tool_not_allowed:" + label for label in (*bridge.CODEX_DENIED_ITEM_LABELS, "unknown")),
})


class CodexWorkerError(RuntimeError):
    def __init__(self, reason="worker_unavailable"):
        self.reason_code = reason if type(reason) is str and reason in {
            "worker_unavailable", "worker_busy", "worker_invalid_handle",
        } else "worker_unavailable"
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class CodexWorkerLimits:
    workers: int
    retained_bytes: int

    def __post_init__(self):
        if (type(self.workers) is not int or not 1 <= self.workers <= 16
                or type(self.retained_bytes) is not int
                or not self.workers * MAX_PACKET_BYTES <= self.retained_bytes <= 512 * 1_048_576):
            raise CodexWorkerError()


@dataclass(frozen=True, repr=False)
class CodexWorkerPacket:
    identity: tuple[str, int, str | None] | None
    body: bytes | None
    report: bytes | None
    fenced: bool
    reviewed: bool
    terminal_state: str | None
    validation_reason: str | None
    error_code: str | None


class CodexWorkerHandle:
    __slots__ = ("__weakref__",)


@dataclass(repr=False)
class _Job:
    ticket: ExecutionTicket | None
    payload: bytes | None
    slot: object
    charged: int
    supervisor: object = None
    state: str = "prepared"
    packet: CodexWorkerPacket | None = None


class _Slot:
    def __init__(self):
        self.incoming = queue.Queue(maxsize=1)
        self.ready = threading.Event()
        self.thread = None
        self.current = None
        self.fault = False


def _decode_input(payload):
    data = parse_bounded_json(payload, max_bytes=MAX_INPUT_BYTES)
    fields = {"task", "route", "assignment", "parent_grant", "schema", "export", "control",
              "executable", "environ", "role_prompt", "worker_result_path"}
    if type(data) is not dict or set(data) != fields:
        raise CodexWorkerError()
    assignment = validate_assignment(data["assignment"], task=data["task"], route=data["route"],
                                     parent_grant=data["parent_grant"])
    route, environment = data["route"], data["environ"]
    permissions = assignment["permissions"]
    if (route.get("provider") != "codex" or route.get("dispatch") != "bridge"
            or route.get("sandbox_mode") != "read-only" or permissions.get("network") is not False
            or any(not item.endswith(":read") for item in permissions.get("filesystem", []))
            or route.get("model") not in SUPPORTED_MODELS or not route["model"].startswith("gpt-")
            or type(environment) is not dict or len(environment) > 256
            or any(type(k) is not str or type(v) is not str or len(k) > 256 or len(v) > 16_384
                   for k, v in environment.items()) or bridge.api_keys_present(environment)):
        raise CodexWorkerError()
    bridge.effective_dispatch_bounds(route, assignment)
    for name in ("export", "control", "executable"):
        if type(data[name]) is not str or not 0 < len(data[name]) <= 8192 or not Path(data[name]).is_absolute():
            raise CodexWorkerError()
    if (type(data["schema"]) is not str or len(data["schema"].encode("utf-8")) > MAX_RESULT_BODY_BYTES
            or type(data["role_prompt"]) is not str or len(data["role_prompt"].encode("utf-8")) > MAX_INPUT_BYTES
            or (data["worker_result_path"] is not None and type(data["worker_result_path"]) is not str)):
        raise CodexWorkerError()
    return data, assignment


def _safe_report(report):
    if report.get("outcome") not in _OUTCOMES:
        raise CodexWorkerError()
    result = {key: report[key] for key in (
        "provider", "requested_model", "effort", "wall_seconds", "read_containment", "rerouted", "outcome",
    )}
    if (result["provider"] != "codex" or result["requested_model"] not in SUPPORTED_MODELS
            or result["effort"] not in SUPPORTED_EFFORTS
            or type(result["rerouted"]) is not bool or result["read_containment"] != "advisory"
            or type(result["wall_seconds"]) not in {int, float}
            or not math.isfinite(result["wall_seconds"]) or not 0 <= result["wall_seconds"] <= 1_000_000):
        raise CodexWorkerError()
    model = report.get("observed_model")
    result["observed_model"] = model if type(model) is str and model in SUPPORTED_MODELS else None
    usage = report.get("usage")
    names = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    result["usage"] = ({name: usage.get(name) for name in names} if type(usage) is dict and all(
        usage.get(name) is None or (type(usage.get(name)) is int and 0 <= usage[name] <= (1 << 63) - 1)
        for name in names) else None)
    code = report.get("result_validation_reason_code")
    if code is not None:
        if type(code) is not str or code not in RESULT_VALIDATION_REASON_CODES:
            raise CodexWorkerError()
        result["result_validation_reason_code"] = code
    body = canonical_json(result)
    if len(body) > MAX_REPORT_BYTES:
        raise CodexWorkerError()
    return body


def _execute(payload, ticket, project_id):
    try:
        data, assignment = _decode_input(payload)
        result, report = _RUN_CODEX(
            ledger=object(), project_id=project_id, route=data["route"], assignment=assignment,
            export=Path(data["export"]), control=Path(data["control"]),
            schema=data["schema"].encode("utf-8"), environ=data["environ"],
            role_prompt=data["role_prompt"], executable=data["executable"],
            worker_result_path=data["worker_result_path"],
            deadline_seconds=max(0, (ticket.deadline_ns() - time.monotonic_ns()) / 1_000_000_000),
            _execution=ticket,
        )
        result, report, fenced, reviewed, terminal, reason = _PREPARE_RESULT(assignment, result, report)
        body = canonical_json(result)
        if len(body) > MAX_RESULT_BODY_BYTES:
            raise CodexWorkerError()
        identity = (assignment["assignment_id"], assignment["attempt"], assignment.get("retry_of_assignment_id"))
        return CodexWorkerPacket(identity, body, _safe_report(report), fenced, reviewed, terminal, reason, None)
    except (ContractError, CodexWorkerError):
        code = "worker_input_invalid"
    except Exception:
        code = "worker_failed"
    except BaseException:
        code = "worker_interrupted"
    # No exception object, traceback, rejected result or original input retained.
    return CodexWorkerPacket(None, None, None, True, False, None, None, code)


class CodexWorkerPool:
    """Bootstrap before control intake; normal methods do no I/O, JSON or joins.

    Each slot reserves the complete supported result budget before submission.
    Input parsing, provider execution and result validation run on its fixed
    thread. Returned/unknown slots remain charged; only the original released
    execution may retire a returned slot. No automatic replacement or relaunch.
    """

    def __init__(self, registry, limits):
        if (type(registry) is not OwnedExecutionRegistry or registry._pid != os.getpid()
                or type(limits) is not CodexWorkerLimits):
            raise CodexWorkerError()
        limits.__post_init__()
        self._registry = registry
        self._limits = CodexWorkerLimits(limits.workers, limits.retained_bytes)
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._jobs = weakref.WeakKeyDictionary()
        self._slots = [_Slot() for _ in range(limits.workers)]
        self._charged = 0
        self._closed = False
        try:
            for slot in self._slots:
                slot.thread = threading.Thread(target=self._worker, args=(slot,), daemon=True)
                slot.thread.start()
                if not slot.ready.wait(5):
                    raise CodexWorkerError()
            registry._register_worker_pool(self)
        except BaseException:
            self._closed = True
            for slot in self._slots:
                with self._registry._lock:
                    try:
                        slot.incoming.put_nowait(None)
                    except queue.Full:
                        pass
            raise

    def _owner(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise CodexWorkerError("worker_invalid_handle")

    def _job(self, handle):
        self._owner()
        if type(handle) is not CodexWorkerHandle or handle not in self._jobs:
            raise CodexWorkerError("worker_invalid_handle")
        return self._jobs[handle]

    def _reserved(self, handle):
        job = self._job(handle)
        return job.state == "reserved" and job.slot.current is handle and job.slot.thread.is_alive()

    def prepare(self, payload):
        """Reserve local capacity before account admission; launch no work."""
        self._owner()
        if self._closed or type(payload) is not bytes or not 0 < len(payload) <= MAX_INPUT_BYTES:
            raise CodexWorkerError()
        with self._registry._lock:
            slot = next((slot for slot in self._slots if slot.current is None and not slot.fault), None)
            charged = len(payload) + MAX_PACKET_BYTES
            if slot is None or self._charged + charged > self._limits.retained_bytes:
                raise CodexWorkerError("worker_busy")
            handle = CodexWorkerHandle()
            job = _Job(None, payload, slot, charged)
            self._jobs[handle], slot.current = job, handle
            self._charged += charged
            return handle

    def bind(self, handle, ticket):
        """After an actual execution grant, transfer it to the prepared slot."""
        self._owner()
        with self._registry._lock:
            job = self._job(handle)
            if self._closed or job.state != "prepared":
                raise CodexWorkerError()
            self._registry._entry(ticket)
            job.ticket, job.state = ticket, "reserved"
            try:
                self._registry._handoff_worker(ticket, self, handle)
            except BaseException:
                job.state = "unknown"  # Never roll back an ambiguous transfer.
                raise

    def reserve(self, ticket, payload):
        """Internal synchronous reference; services prepare before claiming."""
        handle = self.prepare(payload)
        self.bind(handle, ticket)
        return handle

    def withdraw_prepared(self, handle):
        """Release unbound local input space only; never account capacity."""
        with self._registry._lock:
            job = self._job(handle)
            if job.state != "prepared" or job.ticket is not None:
                raise CodexWorkerError()
            self._jobs.pop(handle)
            job.slot.current = None
            self._charged -= job.charged

    def supervisor(self, handle):
        with self._registry._lock:
            return self._job(handle).supervisor

    def submit(self, handle):
        self._owner()
        with self._registry._lock:
            job = self._job(handle)
            if job.state in {"queued", "active", "returned"}:
                return  # Same actual handle never enqueues a second launch.
            if self._closed or job.state != "reserved" or job.supervisor is None:
                raise CodexWorkerError()
            entry = self._registry._supervisor_entry(job.supervisor)
            if entry.activity is None or entry.worker_pool is not self or entry.worker_handle is not handle:
                raise CodexWorkerError()
            job.state = "queued"
            try:
                job.slot.incoming.put_nowait(handle)
            except BaseException:
                job.state, job.slot.fault = "unknown", True
                raise

    def snapshot(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            return {"state": job.state, "returned": job.state == "returned",
                    "error_code": None if job.packet is None else job.packet.error_code}

    def packet(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.state != "returned" or job.packet is None:
                raise CodexWorkerError()
            return job.packet

    def retire(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            entry = self._registry._issued.get(job.ticket)
            if job.state != "returned" or entry is None or entry.state != "released":
                raise CodexWorkerError()
            self._jobs.pop(handle)
            job.slot.current = None
            self._charged -= job.charged

    def discard_candidate(self, handle):
        """Drop body bytes without freeing execution custody or its slot."""
        with self._registry._lock:
            job = self._job(handle)
            if job.state != "returned" or job.packet is None:
                raise CodexWorkerError()
            job.packet = replace(job.packet, body=None, error_code="worker_result_discarded")

    def close(self):
        self._owner()
        with self._registry._lock:
            if self._closed:
                return
            self._closed = True
            for slot in self._slots:
                if slot.current is not None:
                    job = self._jobs.get(slot.current)
                    if job is not None and job.supervisor is not None:
                        job.supervisor.request_stop()
                    if job is not None and job.state in {"queued", "active"}:
                        continue
                try:
                    slot.incoming.put_nowait(None)
                except queue.Full:
                    pass  # An already queued actual job retains its custody.

    def _worker(self, slot):
        slot.ready.set()
        job = None
        try:
            while True:
                handle = slot.incoming.get()
                if handle is None:
                    return
                with self._registry._lock:
                    job = self._jobs.get(handle)
                    if job is None or slot.current is not handle or job.state != "queued":
                        slot.fault = True
                        return
                    entry = self._registry._entry(job.ticket)
                    job.state = "active"
                    payload, job.payload = job.payload, None
                    ticket, project_id = job.ticket, entry.key.project_id
                packet = _execute(payload, ticket, project_id)
                payload = ticket = None
                with self._registry._lock:
                    job.packet, job.state = packet, "finishing"
                    self._registry._complete_worker(self, handle)
                    if not self._registry._entry(job.ticket).worker_returned:
                        raise ExecutionCustodyError()
                    job.state = "returned"
                    if self._closed:
                        return
                job = packet = handle = None
        except BaseException:
            # No input-bearing traceback is printed or retained as diagnostics.
            with self._registry._lock:
                slot.fault = True
                if job is not None:
                    job.state = "unknown"
                    entry = self._registry._issued.get(job.ticket)
                    if entry is not None and entry.worker_pool is self and entry.activity is not None:
                        entry.activity.fail()
                    if job.packet is not None:
                        job.packet = replace(job.packet, body=None, report=None, error_code="worker_unavailable")
