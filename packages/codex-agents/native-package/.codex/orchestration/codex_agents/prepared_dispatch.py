"""Dispatch consumed original project executions without client persistence.

Trusted native preparation must finish original account/project admission before
offering a context. A context/receipt/callback is not native enrollment. This
consumer creates no grant, substitutes no original, and activates no listener.
"""
from dataclasses import dataclass, field
import os
import threading

from .bridge_liveness import BridgeLivenessOwner
from .codex_worker import CodexWorkerHandle, CodexWorkerPool
from .control_api import ControlScope, scope_data
from .dispatch_api import (
    DispatchError, decode_dispatch_request, decode_dispatch_response, dispatch_reply, dispatch_request_digest,
)
from .history_contracts import AttemptBinding, _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, parse_bounded_json
from .result_persistence import ResultPersistenceHandle, ResultPersistenceLane


@dataclass(frozen=True, repr=False)
class PreparedDispatchContext:
    binding: AttemptBinding
    persistence: ResultPersistenceHandle
    worker: CodexWorkerHandle
    inputs_digest: str
    export_info: tuple
    _pid: int = field(default_factory=os.getpid, init=False)

    def snapshot(self):
        if (self._pid != os.getpid() or type(self.binding) is not AttemptBinding
                or type(self.persistence) is not ResultPersistenceHandle or type(self.worker) is not CodexWorkerHandle
                or not matches(self.inputs_digest, _DIGEST)
                or type(self.export_info) is not tuple or len(self.export_info) != 3
                or not valid_integer(self.export_info[0], maximum=2000)
                or type(self.export_info[1]) is not str or len(self.export_info[1]) not in {40, 64}
                or any(c not in '0123456789abcdef' for c in self.export_info[1])
                or not matches(self.export_info[2], _DIGEST)):
            raise DispatchError('dispatch_not_authorized')
        return (tuple(self.binding.as_data().items()), self.persistence, self.worker, self.inputs_digest, self.export_info)


@dataclass(frozen=True, repr=False)
class DispatchAuthorization:
    scope: ControlScope
    request_digest: str
    context: PreparedDispatchContext
    allowed_operations: frozenset


@dataclass(repr=False)
class _Dispatch:
    raw: bytes
    transport: object
    context: PreparedDispatchContext
    snapshot: tuple
    inputs: str
    expected_commit: str | None
    pending: object = None
    state: str = 'queued'
    supervisor: object = None
    delivery: dict | None = None
    report: bytes | None = None


class PreparedDispatchService:
    def __init__(self, owner, pool, lane, *, max_requests, steps_per_cycle, authorizer=None):
        if (type(owner) is not BridgeLivenessOwner or type(pool) is not CodexWorkerPool
                or type(lane) is not ResultPersistenceLane or lane._owner is not owner or lane._pool is not pool
                or lane._allow_local_reference or not valid_integer(max_requests, minimum=1, maximum=64)
                or not valid_integer(steps_per_cycle, minimum=1, maximum=max_requests)
                or (authorizer is not None and not callable(authorizer))):
            raise DispatchError('dispatch_unavailable')
        owner._owner()
        self._owner, self._pool, self._lane = owner, pool, lane
        self._monitor = owner._monitor
        self._scope = ControlScope(**vars(self._monitor.scope()))
        self._authorizer, self._maximum, self._steps = authorizer, max_requests, steps_per_cycle
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._jobs, self._rotation = {}, []
        self._control_facade = None
        self._closed = self._busy = False

    def _guard(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise DispatchError('dispatch_unavailable')
        self._monitor._owner()
        if (self._closed or self._control_facade is None or self._control_facade._closed
                or self._control_facade._dispatch is not self or self._lane._owner is not self._owner
                or self._lane._pool is not self._pool or self._owner._monitor is not self._monitor):
            raise DispatchError('dispatch_unavailable')

    def _authorize(self, request, transport):
        self._guard()
        digest = dispatch_request_digest(request)
        if scope_data(request.scope) != scope_data(self._scope) or self._authorizer is None:
            raise DispatchError('dispatch_not_authorized')
        authorization = self._authorizer(request, transport)
        self._guard()
        if (type(authorization) is not DispatchAuthorization or type(authorization.scope) is not ControlScope
                or scope_data(authorization.scope) != scope_data(self._scope)
                or authorization.request_digest != digest or dispatch_request_digest(request) != digest
                or type(authorization.allowed_operations) is not frozenset
                or request.operation not in authorization.allowed_operations
                or not authorization.allowed_operations <= {'dispatch.submit', 'dispatch.status'}
                or type(authorization.context) is not PreparedDispatchContext):
            raise DispatchError('dispatch_not_authorized')
        context = authorization.context
        context.snapshot()
        binding = context.binding
        if (any(getattr(binding, n) != v for n, v in scope_data(self._scope).items())
                or binding.run_handle != request.run_handle or binding.attempt != request.attempt):
            raise DispatchError('dispatch_not_authorized')
        return context

    def _current(self, job):
        request = decode_dispatch_request(job.raw)
        if self._authorize(request, job.transport) is not job.context or job.context.snapshot() != job.snapshot:
            raise DispatchError('dispatch_not_authorized')
        return request

    def _view(self, job, *, known):
        return {'state': job.state, 'known': known, 'binding': job.context.binding.as_data(),
            'inputs_digest': job.inputs, 'delivery': job.delivery,
            'report': None if job.report is None else parse_bounded_json(job.report, max_bytes=12_288)}

    def handle(self, raw, transport_context=None):
        request, reason = None, None
        entered = False
        try:
            self._guard()
            if self._busy:
                raise DispatchError('dispatch_unavailable')
            self._busy = True
            entered = True
            request = decode_dispatch_request(raw)
            context = self._authorize(request, transport_context)
            if request.inputs_digest != context.inputs_digest:
                raise DispatchError('dispatch_conflict')
            if request.expected_export_commit is not None and request.expected_export_commit != context.export_info[1]:
                raise DispatchError('dispatch_conflict')
            key = (request.run_handle, request.attempt)
            job = self._jobs.get(key)
            known = job is not None
            if job is None:
                if request.operation != 'dispatch.submit':
                    raise DispatchError('dispatch_not_found')
                if len(self._jobs) >= self._maximum:
                    raise DispatchError('dispatch_full')
                job = _Dispatch(raw, transport_context, context, context.snapshot(),
                                request.inputs_digest, request.expected_export_commit)
                self._jobs[key] = job
                self._rotation.append(key)
            if (job.context is not context or job.snapshot != context.snapshot() or job.inputs != request.inputs_digest
                    or (request.operation == 'dispatch.submit' and job.expected_commit != request.expected_export_commit)):
                raise DispatchError('dispatch_conflict')
            reply = dispatch_reply(request, result=self._view(job, known=known))
            decode_dispatch_response(reply, request)
            if self._authorize(decode_dispatch_request(raw), transport_context) is not context:
                raise DispatchError('dispatch_not_authorized')
            return reply
        except DispatchError as error:
            reason = error.reason_code
        except Exception:
            reason = 'dispatch_unavailable'
        finally:
            if entered:
                self._busy = False
        return dispatch_reply(request, reason=reason)

    def _original(self, job):
        context = job.context
        pending = self._lane._job(context.persistence)
        if (pending.binding != context.binding or pending.worker_handle is not context.worker
                or pending.original_job is not self._pool._job(context.worker)
                or (job.pending is not None and pending is not job.pending)
                or self._lane.prepared_inputs_digest(context.persistence) != job.inputs
                or pending.open_record is None or pending.grant is None
                or (job.expected_commit is not None and context.export_info[1] != job.expected_commit)):
            raise DispatchError('dispatch_conflict')
        job.pending = pending
        return pending

    def _advance(self, job):
        self._current(job)
        pending = self._original(job)
        if job.state == 'queued':
            if pending.state != 'execution_consumed':
                raise DispatchError('dispatch_conflict')
            self._current(job)
            ticket = self._lane.issue_execution(job.context.persistence)
            self._current(job)
            self._pool.bind(job.context.worker, ticket)
            job.supervisor = self._pool.supervisor(job.context.worker)
            self._owner.bind(job.context.binding, job.supervisor)
            self._lane.bind(job.context.persistence)
            self._current(job)
            self._pool.submit(job.context.worker)
            job.state = 'running'
        elif job.state == 'running' and self._pool.snapshot(job.context.worker)['returned']:
            self._owner.result(job.context.binding, self._pool, job.context.worker)
            self._lane.submit(job.context.persistence)
            job.state = 'publishing'
        elif job.state == 'publishing':
            state = self._lane.snapshot(job.context.persistence)['state']
            if state in {'failed', 'unknown'}:
                raise DispatchError('dispatch_unavailable')
            if state == 'committed':
                selected = self._owner.result(job.context.binding, self._pool, job.context.worker)
                report = parse_bounded_json(selected.packet.report, max_bytes=12_288)
                report.update(bridge_run_id=job.context.binding.claim_id, reviewed=selected.packet.reviewed,
                    terminal_state=selected.packet.terminal_state, capacity_state='retained',
                    export_file_count=job.context.export_info[0], export_commit=job.context.export_info[1],
                    export_manifest_digest=job.context.export_info[2])
                job.report = canonical_json(report)
                job.delivery = self._lane.delivery(job.context.persistence).as_data()
                self._current(job)
                job.state = 'published'

    def drive(self):
        self._guard()
        self._owner._owner()
        if self._busy:
            raise DispatchError('dispatch_unavailable')
        self._busy = True
        try:
            for _ in range(min(self._steps, len(self._rotation))):
                key = self._rotation.pop(0)
                self._rotation.append(key)
                job = self._jobs[key]
                if job.state in {'published', 'unknown'}:
                    continue
                try:
                    self._advance(job)
                except Exception:
                    if job.supervisor is not None:
                        job.supervisor.request_stop('monitor_unavailable')
                    job.state = 'unknown'
                    job.delivery = job.report = None
        finally:
            self._busy = False

    def retire_released(self, context):
        """Trusted owner housekeeping after original release and retirement.

        This is not a wire operation. Keep the lifetime metadata slot and exact
        original context so a repeated submit cannot launch again. Drop private
        packet/persistence references only after both original owners retired.
        """
        self._guard()
        self._owner._owner()
        if self._busy or type(context) is not PreparedDispatchContext:
            raise DispatchError('dispatch_unavailable')
        key = (context.binding.run_handle, context.binding.attempt)
        job = self._jobs.get(key)
        if job is None or job.context is not context or job.snapshot != context.snapshot():
            raise DispatchError('dispatch_not_authorized')
        if job.state == 'retired':
            return
        pending = job.pending
        if (job.state != 'published' or pending is None or pending.binding != context.binding
                or pending.worker_handle is not context.worker or pending.state != 'released'
                or pending.release_confirmed is None or pending.execution_ticket is None
                or pending.original_job.ticket is not pending.execution_ticket
                or pending.supervisor is not job.supervisor
                or context.persistence in self._lane._jobs or context.worker in self._pool._jobs):
            raise DispatchError('dispatch_conflict')
        registry = self._pool._registry
        with registry._lock:
            entry = registry._issued.get(pending.execution_ticket)
            if entry is None or entry.state != 'released':
                raise DispatchError('dispatch_conflict')
        self._rotation.remove(key)
        job.state = 'retired'
        job.pending = job.supervisor = job.delivery = job.report = job.transport = None
        job.raw = b''

    def close(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise DispatchError('dispatch_unavailable')
        self._closed = True
        for job in self._jobs.values():
            if job.supervisor is not None and job.state not in {'published', 'unknown'}:
                job.supervisor.request_stop('monitor_unavailable')
