"""Bounded owner-local fixed signature jobs, without durable/admission authority.

Only trusted owner code uses this adapter. No normal client or service operation
is enabled here. Restart recovery, durable claims, native caller authentication
and fresh atomic account checks are mandatory before production activation.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import sys
import threading
import time

from .signature_input_wire import SignatureWireLimits
from .signature_job_intake import SignatureJobIntake
from .signature_payloads import SignaturePayloadError
from . import signature_execution as execution

_MAX_NS = (1 << 63) - 1
_REASONS = frozenset({
    'signature_execution_completed', 'signature_execution_cancelled', 'signature_execution_expired',
    'signature_execution_clock_failure', 'signature_execution_birth_unavailable',
    'signature_execution_invalid_output', 'signature_execution_output_limit',
    'signature_execution_worker_failed', 'signature_execution_internal_failure',
})


class SignatureWorkerError(RuntimeError):
    def __init__(self, reason='signature_pool_unavailable'):
        if type(reason) is not str or reason not in {
            'signature_pool_unavailable', 'signature_pool_invalid_limits', 'signature_pool_invalid_handle',
        }:
            reason = 'signature_pool_unavailable'
        self.reason_code = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SignatureWorkerLimits:
    max_workers: int
    protocol_bytes: int
    timeout_ns: int
    poll_ns: int
    cleanup_ns: int

    def __post_init__(self):
        if any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in (
            (self.max_workers, 16), (self.protocol_bytes, 512 * 1048576),
            (self.timeout_ns, 3600 * 1000000000), (self.poll_ns, 100000000),
            (self.cleanup_ns, 5000000000),
        )):
            raise SignatureWorkerError('signature_pool_invalid_limits')


def _protocol_share(rows, fields):
    return 4 * (fields + 27) + 4 * (82 + 262 * rows) + 4 * 65536 + 20736


@dataclass(frozen=True, repr=False)
class SignatureWorkerResult:
    reason: str
    indices: tuple[int, ...]
    finished_ns: int | None
    roots_reaped: int
    no_process_started: bool


@dataclass(frozen=True, repr=False)
class SignatureWorkerNotice:
    handle: object
    result: SignatureWorkerResult
    released: bool


@dataclass(repr=False)
class _Active:
    handle: object
    token: object
    cancelled: threading.Event
    deadline_ns: int
    row_count: int
    unknown: bool = False


@dataclass(frozen=True, repr=False)
class _Envelope:
    token: object
    inputs: object
    cancelled: threading.Event
    deadline_ns: int


@dataclass(frozen=True, repr=False)
class _Complete:
    token: object
    result: SignatureWorkerResult


@dataclass(frozen=True, repr=False)
class _Unknown:
    token: object


class _Slot:
    def __init__(self):
        self.incoming = queue.Queue(maxsize=1)
        self.outgoing = queue.Queue(maxsize=1)
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.fault = threading.Event()
        self.thread = None
        self.current = None


def _worker_loop(slot, wire_limits, poll_ns, cleanup_ns):
    slot.ready.set()
    while True:
        try:
            envelope = slot.incoming.get(timeout=poll_ns / 1000000000)
        except queue.Empty:
            if not slot.stop.is_set():
                continue
            try:
                envelope = slot.incoming.get_nowait()
            except queue.Empty:
                return
        token = envelope.token
        inputs, cancelled, deadline = envelope.inputs, envelope.cancelled, envelope.deadline_ns
        retained = None
        try:
            actual = execution._execute_signature_job(inputs, wire_limits, deadline, cancelled, poll_ns, cleanup_ns)
            result = SignatureWorkerResult(actual.reason, actual.indices, actual.finished_ns,
                                actual.roots_reaped, actual.no_process_started)
        except execution.SignatureExecutionUnconfirmed as error:
            retained = error.context
            result = None
        except SignaturePayloadError:
            result = SignatureWorkerResult('signature_execution_invalid_output', (), None, 0, False)
        except BaseException:
            result = SignatureWorkerResult('signature_execution_internal_failure', (), None, 0, False)
        # No complete job retains a full input/wire/payload in its mailbox or
        # this persistent worker's locals. Exception handlers have also ended.
        inputs = None
        envelope = None
        cancelled = None
        if retained is not None:
            slot.outgoing.put_nowait(_Unknown(token))
            # There is no owner flag, timeout or automatic worker replacement
            # that may free an unconfirmed job. Keep actual custody and slot.
            threading.Event().wait()
            return
        slot.outgoing.put_nowait(_Complete(token, result))
        actual = None
        result = None


def _worker(slot, wire_limits, poll_ns, cleanup_ns):
    try:
        _worker_loop(slot, wire_limits, poll_ns, cleanup_ns)
    except BaseException:
        # Retain the exceptional frame/custody without printing input-bearing
        # tracebacks. The owner sees a fault and never reuses this slot.
        slot.fault.set()
        threading.Event().wait()


class SignatureWorkerPool:
    """Owner-local fixed adapter, with no durable or admission authority.

    bootstrap may block and belongs strictly before the account request loop is
    enabled, outside independent cheap health/control. Normal methods launch no
    thread/process, encode no wire, touch no filesystem and join no live worker.
    """

    def __init__(self, intake, limits):
        if sys.platform != 'darwin' or type(intake) is not SignatureJobIntake:
            raise SignatureWorkerError()
        intake._guard()
        if type(limits) is not SignatureWorkerLimits:
            raise SignatureWorkerError('signature_pool_invalid_limits')
        limits = SignatureWorkerLimits(limits.max_workers, limits.protocol_bytes, limits.timeout_ns,
                            limits.poll_ns, limits.cleanup_ns)
        if getattr(intake, '_signature_worker_pool', None) is not None:
            raise SignatureWorkerError()
        snapshot = intake.snapshot()
        if snapshot.selected or snapshot.returned or snapshot.reserved:
            raise SignatureWorkerError()
        rows, fields = intake._limits.rows_per_job, intake._limits.bytes_per_job
        share = _protocol_share(rows, fields)
        if limits.max_workers > intake._limits.max_selected or limits.max_workers * share > limits.protocol_bytes:
            raise SignatureWorkerError('signature_pool_invalid_limits')
        self._intake, self._limits = intake, limits
        self._wire_limits = SignatureWireLimits(rows, fields)
        self._share = share
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._slots = [_Slot() for _ in range(limits.max_workers)]
        self._active = {}
        self._returned = {}
        self._bootstrapped = self._starting = self._faulted = self._closing = self._closed = False
        self._clock_failed = False
        self._last_ns = None
        intake._signature_worker_pool = self

    def _owner(self):
        self._intake._guard()
        if (self._pid != os.getpid() or self._thread is not threading.current_thread()
                or getattr(self._intake, '_signature_worker_pool', None) is not self):
            raise SignatureWorkerError()

    def _now(self):
        try:
            value = time.monotonic_ns()
        except BaseException:
            self._faulted = True
            self._clock_failed = True
            for active in self._active.values():
                active.cancelled.set()
            raise
        if type(value) is not int or not 0 <= value <= _MAX_NS or (self._last_ns is not None and value < self._last_ns):
            self._faulted = True
            self._clock_failed = True
            for active in self._active.values():
                active.cancelled.set()
            raise SignatureWorkerError()
        self._last_ns = value
        return value

    def bootstrap(self):
        self._owner()
        if self._starting or self._bootstrapped or self._closing or self._faulted:
            raise SignatureWorkerError()
        self._starting = True
        try:
            for slot in self._slots:
                slot.thread = threading.Thread(
                    target=_worker, args=(slot, self._wire_limits, self._limits.poll_ns, self._limits.cleanup_ns),
                    name='signature-fixed-worker', daemon=True,
                )
                slot.thread.start()
                if not slot.ready.wait(timeout=self._limits.cleanup_ns / 1000000000):
                    raise SignatureWorkerError()
            if not all(slot.ready.is_set() and slot.thread.is_alive() for slot in self._slots):
                raise SignatureWorkerError()
            self._bootstrapped = True
        except BaseException:
            self._faulted = True
            for slot in self._slots:
                slot.stop.set()
            raise
        finally:
            self._starting = False

    def offer_next(self):
        self._owner()
        if not self._bootstrapped or self._faulted or self._closing:
            raise SignatureWorkerError()
        slot = next((slot for slot in self._slots if slot.current is None and not slot.fault.is_set()
                     and slot.thread.is_alive()), None)
        if slot is None:
            return None
        now = self._now()
        if now > _MAX_NS - self._limits.timeout_ns:
            raise SignatureWorkerError()
        cancelled, token = threading.Event(), object()
        # Static k*share capacity was validated before startup; selecting this
        # slot consumes its entire share before encoding/offer/spawn can begin.
        try:
            item = self._intake.select_next()
            if item is None:
                return None
            active = _Active(item.handle, token, cancelled, now + self._limits.timeout_ns, len(item.inputs.rows))
            slot.current = active
            self._active[item.handle] = active
            slot.incoming.put_nowait(_Envelope(token, item.inputs, cancelled, active.deadline_ns))
            return item.handle
        except BaseException:
            # No inference of no-start after an ambiguous queue/publication
            # fault. Retain intake/static shares and refuse further offers.
            self._faulted = True
            raise

    def cancel(self, handle):
        self._owner()
        if type(handle) is not object:
            raise SignatureWorkerError('signature_pool_invalid_handle')
        result = self._intake.cancel(handle)
        active = self._active.get(handle)
        if active is not None:
            active.cancelled.set()
        if result == 'released':
            self._returned.pop(handle, None)
        return result

    def _valid_result(self, result, active):
        if (type(result) is not SignatureWorkerResult or type(result.reason) is not str or result.reason not in _REASONS
                or type(result.indices) is not tuple or len(result.indices) > active.row_count
                or type(result.roots_reaped) is not int or not 0 <= result.roots_reaped <= active.row_count + 1
                or type(result.no_process_started) is not bool
                or (result.no_process_started and result.roots_reaped != 0)
                or (result.finished_ns is not None and (type(result.finished_ns) is not int or not 0 <= result.finished_ns <= _MAX_NS))):
            return False
        previous = -1
        for index in result.indices:
            if type(index) is not int or not previous < index < active.row_count:
                return False
            previous = index
        if result.reason != 'signature_execution_completed':
            return not result.indices
        return result.finished_ns is not None and result.roots_reaped >= 1 and not result.no_process_started

    def poll(self):
        self._owner()
        now = self._now()
        notices = []
        for slot in self._slots:
            active = slot.current
            if active is None or active.unknown:
                continue
            try:
                message = slot.outgoing.get_nowait()
            except queue.Empty:
                if slot.fault.is_set() or not slot.thread.is_alive():
                    active.unknown = True
                continue
            if type(message) is _Unknown and message.token is active.token:
                active.unknown = True
                continue
            if (type(message) is not _Complete or message.token is not active.token
                    or not self._valid_result(message.result, active)):
                active.unknown = True
                slot.fault.set()
                continue
            result = message.result
            now = self._now()
            if self._clock_failed:
                result = SignatureWorkerResult('signature_execution_clock_failure', (), result.finished_ns,
                                    result.roots_reaped, result.no_process_started)
            elif active.cancelled.is_set():
                result = SignatureWorkerResult('signature_execution_cancelled', (), result.finished_ns,
                                    result.roots_reaped, result.no_process_started)
            elif now >= active.deadline_ns or (result.finished_ns is not None and result.finished_ns >= active.deadline_ns):
                result = SignatureWorkerResult('signature_execution_expired', (), result.finished_ns,
                                    result.roots_reaped, result.no_process_started)
            # The fixed execution has actually ended and dropped its full data
            # before publishing. Keep noncancelled return fields under intake's
            # quota even for a known zero-process job; cancelled jobs release.
            released = self._intake.confirm_worker_stopped(active.handle)
            del self._active[active.handle]
            slot.current = None
            if not released:
                self._returned[active.handle] = result
            notices.append(SignatureWorkerNotice(active.handle, result, released))
        if self._closing:
            for slot in self._slots:
                slot.stop.set()
            self._closed = (not self._active and not self._returned and self._intake.snapshot().jobs == 0
                            and all(slot.thread is None or not slot.thread.is_alive() for slot in self._slots))
        return tuple(notices)

    def result(self, handle):
        self._owner()
        if type(handle) is not object or handle not in self._returned:
            raise SignatureWorkerError('signature_pool_invalid_handle')
        return self._returned[handle]

    def discard(self, handle):
        self._owner()
        if type(handle) is not object or handle not in self._returned:
            raise SignatureWorkerError('signature_pool_invalid_handle')
        self._intake.discard_returned(handle)
        del self._returned[handle]

    def request_close(self):
        self._owner()
        self._closing = True
        for handle, active in self._active.items():
            self._intake.cancel(handle)
            active.cancelled.set()
        for handle in tuple(self._returned):
            self._intake.cancel(handle)
            del self._returned[handle]
        for slot in self._slots:
            slot.stop.set()

    def snapshot(self):
        self._owner()
        intake = self._intake.snapshot()
        unknown = sum(active.unknown for active in self._active.values())
        unknown += max(0, intake.selected - len(self._active))
        return {
            'active': len(self._active), 'unconfirmed': unknown, 'returned': len(self._returned),
            'cancel_requested': sum(active.cancelled.is_set() for active in self._active.values()),
            'active_protocol_bytes': len(self._active) * self._share,
            'static_protocol_capacity_bytes': len(self._slots) * self._share,
            'intake_jobs': intake.jobs, 'closing': self._closing, 'closed': self._closed,
            'faulted': self._faulted,
        }
