"""Prestarted fixed bridge workers and bounded, actual-returned result custody.

The pool runs one read-only bridged child per job: `codex exec` for a Codex
route, `claude -p` for a Claude route. A Claude job also needs the run's
rate-limit recorder, attached by the host after registration, so each event
reaches the account owner while the child runs.

Trusted supervisor code supplies already authorized/exported inputs and an actual
execution ticket. Neither this adapter, input bytes nor returned packets establish
native caller/client/root authority. No listener, SQL fallback, admission, release,
automatic retry or production client migration is enabled here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
import hashlib
import math
import os
from pathlib import Path
import queue
import threading
import time
import weakref

from . import bridge
from .contracts import RESULT_DIAGNOSTIC_REASON_CODES, ContractError, validate_assignment
from .learning_contracts import (
    SUPPORTED_EFFORTS, SUPPORTED_MODELS, canonical_json, parse_bounded_json, validate_client_diagnostics,
)
from .owned_execution import ExecutionCustodyError, ExecutionTicket, OwnedExecutionRegistry
from .result_contracts import MAX_RESULT_BODY_BYTES

MAX_INPUT_BYTES = 32 * 1_048_576
MAX_REPORT_BYTES = 16_384
MAX_PACKET_BYTES = MAX_RESULT_BODY_BYTES + MAX_REPORT_BYTES + 4096
_RUN_CODEX = bridge.run_codex
_RUN_CLAUDE = bridge.run_claude
_PREPARE_RESULT = bridge.prepare_dispatch_result
_OUTCOMES = frozenset({
    "result", "launch_failed", "process_tracking_unavailable", "watchdog_unavailable",
    "unparseable_stream", "stream_too_large", "timed_out", "cancelled", "startup_silence",
    "work_silence", "monitor_unavailable", "codex_activity_invalid", "child_reported_error",
    "missing_result", "unparseable_result", "invalid_result", "result_too_large",
    *("codex_tool_not_allowed:" + label for label in (*bridge.CODEX_DENIED_ITEM_LABELS, "unknown")),
})
CLAUDE_OUTCOMES = frozenset({
    "claude_activity_invalid", "claude_tool_not_allowed", "missing_init_frame", "paid_usage_observed",
    "provider_rejected", "rate_limit_event_invalid",
    *("init_" + name for name in ("api_key_source", "model_mismatch", "plugin_not_allowed", "mcp_server_loaded",
                                  "tool_not_allowed", "client_unsupported", "permission_mode_mismatch",
                                  "additional_directories_present", "plugin_not_disabled")),
})
_CLAUDE_USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_CODEX_USAGE = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


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
    recorder: object = None
    keeper: object = None
    client_sha256: str | None = None


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
    provider = route.get("provider")
    if (provider not in {"codex", "claude"} or route.get("dispatch") != "bridge"
            or route.get("sandbox_mode") != "read-only" or permissions.get("network") is not False
            or permissions.get("mcps") or any(not item.endswith(":read") for item in permissions.get("filesystem", []))
            or route.get("model") not in SUPPORTED_MODELS
            or not route["model"].startswith("gpt-" if provider == "codex" else "claude-")
            or type(environment) is not dict or len(environment) > 256
            or any(type(k) is not str or type(v) is not str or len(k) > 256 or len(v) > 16_384
                   for k, v in environment.items()) or bridge.api_keys_present(environment)):
        raise CodexWorkerError()
    bridge.effective_dispatch_bounds(route, assignment)
    for name in ("export", "control", "executable"):
        if type(data[name]) is not str or not 0 < len(data[name]) <= 8192 or not Path(data[name]).is_absolute():
            raise CodexWorkerError()
    if ((type(data["schema"]) is not str or len(data["schema"].encode("utf-8")) > MAX_RESULT_BODY_BYTES)  # O10 VF-1
            or type(data["role_prompt"]) is not str or len(data["role_prompt"].encode("utf-8")) > MAX_INPUT_BYTES
            or (data["worker_result_path"] is not None and type(data["worker_result_path"]) is not str)):
        raise CodexWorkerError()
    return data, assignment


def _safe_report(report, provider):
    claude = provider == "claude"
    if report.get("outcome") not in (_OUTCOMES | CLAUDE_OUTCOMES if claude else _OUTCOMES):
        raise CodexWorkerError()
    result = {key: report[key] for key in (
        "provider", "requested_model", "effort", "wall_seconds", "read_containment", "rerouted", "outcome",
    )}
    if (result["provider"] != provider or result["requested_model"] not in SUPPORTED_MODELS
            or result["effort"] not in SUPPORTED_EFFORTS
            or type(result["rerouted"]) is not bool
            or result["read_containment"] != ("enforced" if claude else "advisory")
            or type(result["wall_seconds"]) not in {int, float}
            or not math.isfinite(result["wall_seconds"]) or not 0 <= result["wall_seconds"] <= 1_000_000):
        raise CodexWorkerError()
    model = report.get("observed_model")
    result["observed_model"] = model if type(model) is str and model in SUPPORTED_MODELS else None
    usage = report.get("usage")
    names = _CLAUDE_USAGE if claude else _CODEX_USAGE
    result["usage"] = ({name: usage.get(name) for name in names} if type(usage) is dict and all(
        usage.get(name) is None or (type(usage.get(name)) is int and 0 <= usage[name] <= (1 << 63) - 1)
        for name in names) else None)
    code = report.get("result_validation_reason_code")
    if code is not None:
        if type(code) is not str or code not in RESULT_DIAGNOSTIC_REASON_CODES:
            raise CodexWorkerError()
        result["result_validation_reason_code"] = code
    if claude:
        counts = [report.get(name) for name in ("rate_limit_events", "rate_limit_records_kept")]
        if (any(type(n) is not int or not 0 <= n <= 1_000_000 for n in counts)
                or type(report.get("rate_limit_record_failed")) is not bool):
            raise CodexWorkerError()
        result.update(rate_limit_events=counts[0], rate_limit_records_kept=counts[1],
                      rate_limit_record_failed=report["rate_limit_record_failed"])
        # The client's own exit code, result subtype and error label: fixed safe values only.
        if report.get("client_diagnostics") is not None:
            result["client_diagnostics"] = validate_client_diagnostics(report["client_diagnostics"])
        denials = report.get("permission_denials")
        if denials is not None:
            # L6.1: the client's own refusals, as a count and fixed tool names only (never the inputs).
            if (type(denials) is not dict or set(denials) != {"count", "tools"} or type(denials["count"]) is not int
                    or not 0 <= denials["count"] <= 1_000_000 or type(denials["tools"]) is not list
                    or any(type(t) is not str for t in denials["tools"])):
                raise CodexWorkerError()
            # A name that is not a plain tool name is reported as "other", never passed on.
            names = sorted({t if re.fullmatch(r"[A-Za-z0-9_]{1,40}", t) else "other" for t in denials["tools"]})[:16]
            result["permission_denials"] = {"count": denials["count"], "tools": names}
    if "raw_answer" in report:
        result["raw_answer"] = _safe_raw_answer(report["raw_answer"])
    body = canonical_json(result)
    if len(body) > MAX_REPORT_BYTES:
        raise CodexWorkerError()
    return body


def _safe_raw_answer(kept):
    """What the host recorded of the child's kept answer (VF-INV-a): fixed fields only, or nothing kept."""
    if kept is None:
        return None
    if type(kept) is dict and kept == {"kept": False, "reason": "keep_failed"}:
        return dict(kept)
    if (type(kept) is dict and set(kept) == {"kept", "sha256", "bytes", "truncated", "scrubbed"}
            and kept["kept"] is True and type(kept["sha256"]) is str and len(kept["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in kept["sha256"])
            and type(kept["bytes"]) is int and 0 <= kept["bytes"] <= MAX_RESULT_BODY_BYTES
            and type(kept["truncated"]) is bool and type(kept["scrubbed"]) is bool):
        return {name: kept[name] for name in ("kept", "sha256", "bytes", "truncated", "scrubbed")}
    raise CodexWorkerError()


def _client_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execute(payload, ticket, project_id, run_handle=None, recorder=None, client_sha256=None, keeper=None):
    try:
        data, assignment = _decode_input(payload)
        # The prepared client, re-read just before launch, is still the one the account observed.
        if client_sha256 is not None and _client_digest(data["executable"]) != client_sha256:
            raise CodexWorkerError()
        provider = data["route"]["provider"]
        # VF-INV-c: a verifier's child is held to the schema the host would accept.
        child_schema = bridge.result_schema_for(data["schema"].encode("utf-8"), assignment)
        # VF-INV-a (decision 26, "original kept"): the runners offer the child's raw final answer here.
        raw_answer = {}
        deadline = max(0, (ticket.deadline_ns() - time.monotonic_ns()) / 1_000_000_000)
        common = dict(project_id=project_id, route=data["route"], assignment=assignment,
                      export=Path(data["export"]), control=Path(data["control"]), environ=data["environ"],
                      role_prompt=data["role_prompt"], executable=data["executable"],
                      worker_result_path=data["worker_result_path"], deadline_seconds=deadline, _execution=ticket,
                      raw_answer=raw_answer)
        if provider == "claude":
            # Without its recorder a Claude child could not report a restriction: never launch it.
            if recorder is None or type(run_handle) is not str or client_sha256 is None:
                raise CodexWorkerError()
            _, max_turns = bridge.effective_dispatch_limits(data["route"], assignment)
            result, report = _RUN_CLAUDE(ledger=recorder, run_handle=run_handle, max_turns=max_turns,
                                         schema=child_schema, **common)
        else:
            result, report = _RUN_CODEX(ledger=object(), schema=child_schema, **common)
        if keeper is not None:
            # Kept before validation can fence the answer, as on the bridge path; best effort.
            report["raw_answer"] = bridge._keep_raw_answer(keeper, project_id, raw_answer, data["environ"])
        result, report, fenced, reviewed, terminal, reason = _PREPARE_RESULT(assignment, result, report)
        body = canonical_json(result)
        if len(body) > MAX_RESULT_BODY_BYTES:
            raise CodexWorkerError()
        identity = (assignment["assignment_id"], assignment["attempt"], assignment.get("retry_of_assignment_id"))
        return CodexWorkerPacket(identity, body, _safe_report(report, provider), fenced, reviewed, terminal, reason, None)
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

    def prepare(self, payload, *, client_sha256=None):
        """Reserve local capacity before account admission; launch no work."""
        self._owner()
        if (self._closed or type(payload) is not bytes or not 0 < len(payload) <= MAX_INPUT_BYTES
                or (client_sha256 is not None and (type(client_sha256) is not str or len(client_sha256) != 64))):
            raise CodexWorkerError()
        with self._registry._lock:
            slot = next((slot for slot in self._slots if slot.current is None and not slot.fault), None)
            charged = len(payload) + MAX_PACKET_BYTES
            if slot is None or self._charged + charged > self._limits.retained_bytes:
                raise CodexWorkerError("worker_busy")
            handle = CodexWorkerHandle()
            job = _Job(None, payload, slot, charged, client_sha256=client_sha256)
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

    def attach_recorder(self, handle, recorder):
        """Before submission: the run's rate-limit recorder, used only by a Claude job."""
        with self._registry._lock:
            job = self._job(handle)
            if job.state not in {"prepared", "reserved"} or job.recorder is not None or recorder is None:
                raise CodexWorkerError()
            job.recorder = recorder

    def attach_answer_keeper(self, handle, keeper):
        """Before submission: where the child's raw final answer is kept (VF-INV-a).

        `keeper.keep_child_answer(project_id, content)` is the ledger's call of the same name; the
        project process holds no ledger of its own, so a host that has one attaches it here. Without
        a keeper nothing is kept and the report says nothing about it.
        """
        with self._registry._lock:
            job = self._job(handle)
            if (job.state not in {"prepared", "reserved"} or job.keeper is not None or keeper is None
                    or not callable(getattr(keeper, "keep_child_answer", None))):
                raise CodexWorkerError()
            job.keeper = keeper

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
            # C3: the service is closing, so each run's container is stopped as `service_close`.
            self._registry.closing()
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
                    run_handle, recorder, client = entry.run_handle, job.recorder, job.client_sha256
                    keeper = job.keeper
                packet = _execute(payload, ticket, project_id, run_handle, recorder, client, keeper)
                payload = ticket = recorder = keeper = None
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
