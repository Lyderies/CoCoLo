"""Fixed owner-local calculation workers; no persistence or admission authority.

Only trusted runtime startup may bootstrap this pool, before publishing a service.
Normal methods neither start nor join threads/processes, encode/decode bulk data,
call SQL, or wait for calculation/cleanup. Inputs are captured immutable bytes.
Queued/returned/unknown jobs retain their count and input charge until an explicit
take of a confirmed result. Each active or returned slot keeps its static wire
share. Unknown custody is never replaced or released by elapsed time or close.

Wire quotas are logical buffer envelopes, not physical RSS containment. The fixed
executor remains experimental and unselected by services. Native memory/read
policy, image/enrollment, scheduling, revision/authority, migration and bounded
writer finalization remain requirements before production activation.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
import queue
import sys
import threading
import time

from . import learning_cycle_execution as execution
from .learning_cycle_process import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_STRUCTURE_WEIGHT

# Charge input/output wire views plus both predecode syntax envelopes before a
# slot can start. This deliberately over-reserves logical capacity; Python/native
# allocator behavior still needs the separate physical-memory policy/proof.
WIRE_BYTES_PER_WORKER = 4 * MAX_INPUT_BYTES + 8 * MAX_OUTPUT_BYTES + 2 * MAX_STRUCTURE_WEIGHT + 65_536
_MAX_NS = (1 << 63) - 1
_REASONS = frozenset('learning_execution_' + suffix for suffix in (
    'completed', 'cancelled', 'expired', 'clock_failure', 'birth_unavailable',
    'invalid_output', 'output_limit', 'worker_failed', 'internal_failure',
))


class LearningWorkerError(RuntimeError):
    def __init__(self, reason='learning_pool_unavailable'):
        if type(reason) is not str or reason not in {
            'learning_pool_unavailable', 'learning_pool_invalid_input', 'learning_pool_invalid_limits',
            'learning_pool_invalid_handle', 'learning_pool_overloaded',
        }:
            reason = 'learning_pool_unavailable'
        self.reason_code = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LearningWorkerLimits:
    max_workers: int
    max_jobs: int
    input_bytes: int
    protocol_bytes: int
    poll_ns: int
    cleanup_ns: int

    def __post_init__(self):
        if any(type(value) is not int or not minimum <= value <= maximum for value, minimum, maximum in (
            (self.max_workers, 1, 4), (self.max_jobs, 1, 32), (self.input_bytes, 1, 256 * 1_048_576),
            (self.protocol_bytes, 1, 8_192 * 1_048_576), (self.poll_ns, 1_000_000, 100_000_000),
            (self.cleanup_ns, 1_000_000_000, 5_000_000_000),
        )):
            raise LearningWorkerError('learning_pool_invalid_limits')


@dataclass(repr=False)
class _Job:
    raw: bytes
    operation: str
    binding_digest: str
    deadline_ns: int
    started_ns: int
    cancelled: threading.Event
    state: str = 'queued'
    result: object = None
    slot: object = None


@dataclass(frozen=True, repr=False)
class _Envelope:
    token: object
    raw: bytes
    operation: str
    binding_digest: str
    deadline_ns: int
    cancelled: threading.Event


@dataclass(frozen=True, repr=False)
class _Complete:
    token: object
    result: object


@dataclass(frozen=True, repr=False)
class _Unknown:
    token: object


class _Slot:
    def __init__(self):
        self.incoming, self.outgoing = queue.Queue(1), queue.Queue(1)
        self.ready, self.stop, self.fault = threading.Event(), threading.Event(), threading.Event()
        self.thread = None
        self.current = self.pending = self.retained = None


def _worker_loop(slot, poll_ns, cleanup_ns):
    slot.ready.set()
    while True:
        try:
            envelope = slot.incoming.get(timeout=poll_ns / 1_000_000_000)
        except queue.Empty:
            if slot.stop.is_set():
                return
            continue
        try:
            result = execution._execute_learning_job(
                envelope.raw, operation=envelope.operation, binding_digest=envelope.binding_digest,
                deadline_ns=envelope.deadline_ns, cancelled=envelope.cancelled,
                poll_ns=poll_ns, cleanup_ns=cleanup_ns,
            )
            message = _Complete(envelope.token, result)
            result = None
        except execution.LearningExecutionUnconfirmed as error:
            slot.retained = error.context
            message = _Unknown(envelope.token)
        except BaseException as error:
            # Retain exceptional frames/custody, with no payload-bearing traceback
            # or caller-selected callback. A thread ending proves no cleanup.
            slot.retained = error
            message = _Unknown(envelope.token)
        envelope = None
        try:
            slot.outgoing.put_nowait(message)
        except BaseException as error:
            slot.retained = (slot.retained, message, error)
            slot.fault.set()
            return
        unknown = type(message) is _Unknown
        message = None
        if unknown:
            return


def _worker(slot, poll_ns, cleanup_ns):
    try:
        _worker_loop(slot, poll_ns, cleanup_ns)
    except BaseException as error:
        # Include mailbox/startup faults outside the execution call. Retain the
        # original exceptional frame; never let the thread hook print its data.
        slot.retained = (slot.retained, error)
        slot.fault.set()


class LearningWorkerPool:
    def __init__(self, limits):
        if sys.platform != 'darwin' or type(limits) is not LearningWorkerLimits:
            raise LearningWorkerError()
        limits = LearningWorkerLimits(limits.max_workers, limits.max_jobs, limits.input_bytes,
            limits.protocol_bytes, limits.poll_ns, limits.cleanup_ns)
        if limits.max_workers * WIRE_BYTES_PER_WORKER > limits.protocol_bytes:
            raise LearningWorkerError('learning_pool_invalid_limits')
        self._limits = limits
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._slots = [_Slot() for _ in range(limits.max_workers)]
        self._jobs, self._queued = {}, deque()
        self._input_bytes = 0
        self._last_ns = None
        self._mutation_unknown = None
        self._clock_failed = self._faulted = self._bootstrapped = self._closing = self._closed = False

    def _owner(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise LearningWorkerError()

    def _now(self):
        try:
            now = time.monotonic_ns()
            if (type(now) is not int or not 0 <= now <= _MAX_NS
                    or self._last_ns is not None and now < self._last_ns):
                raise ValueError()
        except BaseException:
            self._clock_failed = True
            for job in self._jobs.values():
                job.cancelled.set()
            raise LearningWorkerError() from None
        self._last_ns = now
        return now

    def _job(self, handle):
        self._owner()
        if type(handle) is not object or handle not in self._jobs:
            raise LearningWorkerError('learning_pool_invalid_handle')
        return self._jobs[handle]

    def _retain_mutation(self, handle, job, error):
        # A partial local publication must never undercharge a future offer or
        # make a lost completion look consumed. Keep the actual job/frame even
        # if its registry deletion/insertion already happened, and latch closed
        # all further offers. Logical input/protocol capacity stays fully held.
        self._faulted = True
        self._mutation_unknown = (handle, job, error, self._mutation_unknown)
        job.state = 'unknown'
        job.cancelled.set()
        self._input_bytes = self._limits.input_bytes

    def bootstrap(self):
        """Blocking startup only, before the trusted frontend accepts requests."""
        self._owner()
        if self._bootstrapped or self._faulted or self._closing:
            raise LearningWorkerError()
        try:
            for slot in self._slots:
                slot.thread = threading.Thread(target=_worker,
                    args=(slot, self._limits.poll_ns, self._limits.cleanup_ns), daemon=True,
                    name='learning-fixed-worker')
                slot.thread.start()
                if not slot.ready.wait(self._limits.cleanup_ns / 1_000_000_000) or not slot.thread.is_alive():
                    raise LearningWorkerError()
            self._bootstrapped = True
        except BaseException:
            self._faulted = True
            for slot in self._slots:
                slot.stop.set()
            raise LearningWorkerError() from None

    def submit(self, raw, *, operation, binding_digest, deadline_ns):
        self._owner()
        if not self._bootstrapped or self._closing or self._faulted or self._clock_failed:
            raise LearningWorkerError()
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_INPUT_BYTES
                or type(operation) is not str or operation not in {'select_mining', 'plan'}
                or type(binding_digest) is not str or len(binding_digest) != 64
                or any(char not in '0123456789abcdef' for char in binding_digest)
                or type(deadline_ns) is not int or not 1 <= deadline_ns <= _MAX_NS):
            raise LearningWorkerError('learning_pool_invalid_input')
        if len(self._jobs) >= self._limits.max_jobs or self._input_bytes + len(raw) > self._limits.input_bytes:
            raise LearningWorkerError('learning_pool_overloaded')
        if all(slot.fault.is_set() for slot in self._slots):
            raise LearningWorkerError()
        now = self._now()
        handle = object()
        job = _Job(raw, operation, binding_digest, deadline_ns, now, threading.Event())
        try:
            self._jobs[handle] = job
            self._input_bytes += len(raw)
            self._queued.append(handle)
        except BaseException as error:
            self._retain_mutation(handle, job, error)
            raise LearningWorkerError() from None
        return handle

    def state(self, handle):
        return self._job(handle).state

    def cancel(self, handle):
        job = self._job(handle)
        job.cancelled.set()
        return 'pending_confirmation' if job.state in {'running', 'unknown'} else 'cancelled'

    def _masked(self, result, job, now):
        suffix = ('clock_failure' if self._clock_failed else 'cancelled' if job.cancelled.is_set()
                  else 'expired' if now >= job.deadline_ns or result.finished_ns is not None
                  and result.finished_ns >= job.deadline_ns else None)
        if suffix is None:
            return result
        return execution.LearningExecutionResult('learning_execution_' + suffix, None,
            result.finished_ns, result.roots_reaped, result.no_process_started)

    def _valid(self, result, job, now):
        if (type(result) is not execution.LearningExecutionResult
                or type(result.reason) is not str or result.reason not in _REASONS
                or type(result.roots_reaped) is not int or result.roots_reaped not in {0, 1}
                or type(result.no_process_started) is not bool
                or result.no_process_started != (result.roots_reaped == 0)):
            return False
        finished = result.finished_ns
        if finished is None:
            if result.reason != 'learning_execution_clock_failure' or not result.no_process_started:
                return False
        elif type(finished) is not int or not job.started_ns <= finished <= now:
            return False
        if result.reason == 'learning_execution_completed':
            return type(result.result) is dict and result.roots_reaped == 1 and finished is not None
        return result.result is None

    def poll(self):
        self._owner()
        if not self._bootstrapped and not self._closing:
            raise LearningWorkerError()
        notices = []
        for slot in self._slots:
            if slot.current is None:
                continue
            if slot.current not in self._jobs:
                slot.fault.set()
                continue
            job = self._jobs[slot.current]
            if job.state != 'running':
                continue
            if slot.pending is None:
                try:
                    slot.pending = slot.outgoing.get_nowait()
                except queue.Empty:
                    if slot.fault.is_set() or not slot.thread.is_alive():
                        job.state = 'unknown'
                        notices.append(slot.current)
                    continue
            message = slot.pending
            now = self._now()  # Preserve the sole pending message if this fails.
            if (type(message) is not _Complete or message.token is not slot.current
                    or not self._valid(message.result, job, now)):
                job.state = 'unknown'
                slot.retained = (slot.retained, message)
                slot.fault.set()
            else:
                job.result = self._masked(message.result, job, now)
                job.state = 'returned'
            notices.append(slot.current)
            slot.pending = None
        # One bounded pass removes cancelled/expired jobs without launching and
        # preserves FIFO among the remaining captured jobs.
        waiting = deque()
        while self._queued:
            handle = self._queued.popleft()
            job = self._jobs[handle]
            if job.state != 'queued':
                continue
            try:
                now = self._now()
            except LearningWorkerError:
                waiting.append(handle)
                waiting.extend(self._queued)
                self._queued = waiting
                raise
            if self._clock_failed or job.cancelled.is_set() or now >= job.deadline_ns:
                result = execution.LearningExecutionResult('learning_execution_cancelled', None, now, 0, True)
                job.result, job.state = self._masked(result, job, now), 'returned'
                notices.append(handle)
            else:
                waiting.append(handle)
        self._queued = waiting
        if not self._closing and not self._faulted and not self._clock_failed:
            for slot in self._slots:
                if not self._queued:
                    break
                if slot.current is not None or slot.fault.is_set():
                    continue
                if slot.thread is None or not slot.thread.is_alive():
                    slot.fault.set()
                    continue
                handle = self._queued.popleft()
                job = self._jobs[handle]
                slot.current, job.slot, job.state = handle, slot, 'running'
                try:
                    slot.incoming.put_nowait(_Envelope(handle, job.raw, job.operation,
                        job.binding_digest, job.deadline_ns, job.cancelled))
                except BaseException:
                    job.state = 'unknown'
                    slot.fault.set()
                    job.cancelled.set()
                    raise LearningWorkerError() from None
        if (self._closing and not self._jobs and self._mutation_unknown is None
                and all(slot.thread is None or not slot.thread.is_alive() for slot in self._slots)):
            self._closed = True
        return tuple(notices)

    def take(self, handle):
        job = self._job(handle)
        if job.state != 'returned':
            return None
        now = self._now()
        result = self._masked(job.result, job, now)
        try:
            if job.slot is not None:
                job.slot.current = None
            if self._mutation_unknown is None:
                self._input_bytes -= len(job.raw)
            del self._jobs[handle]
        except BaseException as error:
            self._retain_mutation(handle, job, error)
            raise LearningWorkerError() from None
        return result

    def request_close(self):
        self._owner()
        self._closing = True
        for job in self._jobs.values():
            job.cancelled.set()
        retained = self._mutation_unknown
        while retained is not None:
            retained[1].cancelled.set()
            retained = retained[3]
        for slot in self._slots:
            slot.stop.set()

    def snapshot(self):
        self._owner()
        orphaned = 0
        retained = self._mutation_unknown
        while retained is not None:
            orphaned += retained[0] not in self._jobs
            retained = retained[3]
        return {
            'jobs': len(self._jobs) + orphaned, 'input_bytes': self._input_bytes,
            'queued': sum(j.state == 'queued' for j in self._jobs.values()),
            'running': sum(j.state == 'running' for j in self._jobs.values()),
            'returned': sum(j.state == 'returned' for j in self._jobs.values()),
            'unknown': sum(j.state == 'unknown' for j in self._jobs.values()) + orphaned,
            'active_protocol_bytes': (len(self._slots) if self._mutation_unknown is not None else
                                     sum(s.current is not None for s in self._slots)) * WIRE_BYTES_PER_WORKER,
            'static_protocol_capacity_bytes': len(self._slots) * WIRE_BYTES_PER_WORKER,
            'closing': self._closing, 'closed': self._closed,
            'clock_failed': self._clock_failed, 'faulted': self._faulted,
        }
