"""Finite fair non-spending probe work outside the original account owner.

Only fixed startup threads receive captured probe payloads. A cancelled/expired
active slot remains charged until its actual worker return. A missing return
fails closed and cannot restart or be inferred as confirmed process cleanup.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import queue
import threading
import time

from . import lane_health
from .account_observation import AccountObservationOwner, ObservationContext, ProbeCompletion, run_observation_probe
from .history_contracts import _DIGEST, _ID, _PROJECT, matches
from .provider_observation_contracts import ObservationError, ObservationInput

OBSERVATION_JOB_BYTES = 128 * 1024


@dataclass(frozen=True)
class ObservationJobLimits:
    max_projects: int
    max_jobs_per_project: int
    workers: int
    owner_steps_per_cycle: int

    def __post_init__(self):
        if (any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in (
                (self.max_projects, 64), (self.max_jobs_per_project, 64), (self.workers, 16),
                (self.owner_steps_per_cycle, 16)))
                or self.max_projects * self.max_jobs_per_project > 64
                or self.workers > self.max_projects or self.owner_steps_per_cycle > self.workers):
            raise ObservationError('observation_invalid_request')

    @property
    def retained_bytes(self):
        return self.max_projects * self.max_jobs_per_project * OBSERVATION_JOB_BYTES


class _Slot:
    def __init__(self):
        self.incoming = queue.Queue(maxsize=1)
        self.outgoing = queue.Queue(maxsize=1)
        self.ready, self.stop, self.fault = threading.Event(), threading.Event(), threading.Event()
        self.thread = threading.Thread(target=_worker, args=(self,), name='account-observation', daemon=True)


def _worker(slot):
    slot.ready.set()
    while True:
        try:
            ticket, capture = slot.incoming.get(timeout=.05)
        except queue.Empty:
            if slot.stop.is_set():
                return
            continue
        try:
            if slot.stop.is_set():
                reply = ProbeCompletion(capture, None, 'observation_unavailable')
            else:
                reply = run_observation_probe(capture)
            slot.outgoing.put_nowait((ticket, reply))
        except BaseException as error:
            # No completion is manufactured after unknown worker/cleanup state.
            slot.note.publish(faulted=True, kept=frozenset({slot.health_token}))  # S1-H1a, by this worker
            slot.lane.note_failure(slot.note, 'observation_worker_failed', error)
            slot.fault.set()
            return
        if slot.stop.is_set():
            return


@dataclass(repr=False)
class _Job:
    key: tuple
    request: ObservationInput
    context: ObservationContext
    snapshot: tuple
    before_publish: object
    deadline_ns: int
    reservation: object = None
    state: str = 'reserved'
    result: dict | None = None
    reason: str | None = None
    cancelled: bool = False
    billing_parent: tuple | None = None


@dataclass(frozen=True, repr=False)
class ObservationJobReply:
    key: tuple
    request: ObservationInput
    result: dict | None
    reason: str | None
    before_publish: object


class AccountObservationJobs:
    def __init__(self, owner, limits):
        if type(owner) is not AccountObservationOwner or type(limits) is not ObservationJobLimits:
            raise ObservationError('observation_invalid_request')
        owner._guard()
        if getattr(owner, '_jobs_owner', None) is not None or owner._pending:
            raise ObservationError('observation_unavailable')
        self._owner = owner
        self._limits = ObservationJobLimits(limits.max_projects, limits.max_jobs_per_project,
                                            limits.workers, limits.owner_steps_per_cycle)
        self._jobs, self._requests, self._counts = {}, {}, {}
        self._queues, self._active, self._active_projects = {}, {}, set()
        self._rotation = deque()
        self._slots = [_Slot() for _ in range(limits.workers)]
        # S1-H1a: published notes (reporting only); a slot kept after a fault or an invalid reply
        # holds its one job for good, so one token per slot counts it once.
        self.health = lane_health.Lane('observations')
        self._note = self.health.slot('owner')
        self._mismatched = set()
        for index, slot in enumerate(self._slots):
            slot.lane, slot.note = self.health, self.health.slot(f'worker-{index}')
            slot.health_token = lane_health.new_token()
        self._slot_rotation = deque(self._slots)
        self._started = self._closing = self._faulted = self._busy = False
        self._ingress_owner = None
        self._billing_owner = None
        owner._jobs_owner = self

    @contextmanager
    def _operation(self):
        self._owner._guard()
        if self._owner._jobs_owner is not self or self._busy:
            raise ObservationError('observation_unavailable')
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    def _delivery(self, ingress):
        if self._ingress_owner is not None and self._ingress_owner is not ingress:
            raise ObservationError('observation_unavailable')

    def bootstrap(self):
        with self._operation():
            if self._started or self._closing or self._faulted:
                raise ObservationError('observation_unavailable')
            try:
                for slot in self._slots:
                    slot.thread.start()
                for slot in self._slots:
                    if not slot.ready.wait(5):
                        raise ObservationError('observation_unavailable')
                self._started = True
            except BaseException:
                self._faulted = self._closing = True
                for slot in self._slots:
                    slot.stop.set()
                raise

    def _job(self, ticket):
        if type(ticket) is not object or ticket not in self._jobs:
            raise ObservationError('observation_not_authorized')
        return self._jobs[ticket]

    def _unqueue(self, ticket, job):
        project = job.key[0]
        if project in self._queues:
            try:
                self._queues[project].remove(ticket)
            except ValueError:
                pass
            if not self._queues[project]:
                del self._queues[project]
                self._rotation.remove(project)

    def _drop(self, ticket, job):
        self._unqueue(ticket, job)
        del self._requests[job.key]
        del self._jobs[ticket]
        project = job.key[0]
        self._counts[project] -= 1
        if not self._counts[project]:
            del self._counts[project]

    def submit(self, project, runtime, caller, identity, request, context, before_publish, *, _ingress=None,
               _billing_parent=None):
        with self._operation():
            self._delivery(_ingress)
            if not self._started or self._closing or self._faulted:
                raise ObservationError('observation_unavailable')
            if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                    or not matches(identity, _ID) or type(request) is not ObservationInput
                    or type(context) is not ObservationContext or not callable(before_publish)):
                raise ObservationError('observation_invalid_request')
            request = ObservationInput(**request.payload())
            snapshot = context.snapshot()
            key = (project, runtime, caller, identity)
            if _billing_parent is not None:
                from .account_billing_jobs import AccountBillingJobs
                if (type(_billing_parent) is not tuple or len(_billing_parent) != 2
                        or type(_billing_parent[0]) is not AccountBillingJobs
                        or _billing_parent[0] is not self._billing_owner
                        or _billing_parent[0]._observations is not self
                        or _billing_parent[1] not in _billing_parent[0]._jobs
                        or _billing_parent[0]._jobs[_billing_parent[1]].key[:3] != key[:3]):
                    raise ObservationError('observation_not_authorized')
            if key in self._requests:
                ticket = self._requests[key]
                job = self._job(ticket)
                if (job.request != request or job.context is not context or job.snapshot != snapshot
                        or job.cancelled or job.state == 'reserved' or job.billing_parent != _billing_parent):
                    raise ObservationError('observation_conflict')
                self._owner._authorize(context, snapshot, before_publish)
                return ticket
            if (self._counts.get(project, 0) >= self._limits.max_jobs_per_project
                    or (project not in self._counts and len(self._counts) >= self._limits.max_projects)):
                raise ObservationError('observation_full')
            ticket = object()
            job = _Job(key, request, context, snapshot, before_publish,
                       time.monotonic_ns() + self._owner._timeout * 1_000_000)
            job.billing_parent = _billing_parent
            self._jobs[ticket], self._requests[key] = job, ticket
            self._counts[project] = self._counts.get(project, 0) + 1
            try:
                reserved = self._owner.reserve(project, runtime, caller, identity, request, context, before_publish)
                if type(reserved) is dict:
                    job.state, job.result = 'ready', reserved
                else:
                    job.reservation, job.state = reserved, 'queued'
                    if project not in self._queues:
                        self._queues[project] = deque()
                        self._rotation.append(project)
                    self._queues[project].append(ticket)
                return ticket
            except BaseException:
                self._drop(ticket, job)
                raise

    def _finish(self, ticket, job, result=None, reason=None):
        job.reservation = None
        if self._closing:
            self._drop(ticket, job)
        else:
            job.state, job.result, job.reason = 'ready', result, reason

    def _reply_mismatch(self, slot):
        """S1-H1a: an invalid reply keeps its slot; noted once, with no exception."""
        if slot not in self._mismatched:
            self._mismatched.add(slot)
            self.health.note_failure(self._note, 'reply_mismatch', None)

    def _publish_health(self):
        """S1-H1a: this owner's note: active slots busy, faulted or mismatched ones kept, queued jobs.
        Reporting only; never raises."""
        try:
            kept = {slot.health_token for slot in self._active
                    if slot.fault.is_set() or slot in self._mismatched}
            self._note.publish(kept=frozenset(kept), busy=len(self._active) - len(kept),
                               queued=sum(len(queue_) for queue_ in self._queues.values()))
        except Exception:
            pass

    def _drive(self):
        try:
            self._drive_steps()
        finally:
            self._publish_health()

    def _drive_steps(self):
        now = time.monotonic_ns()
        for ticket, job in tuple(self._jobs.items()):
            if job.state == 'queued' and (self._closing or job.cancelled or now >= job.deadline_ns):
                self._owner.abort(job.reservation)
                self._unqueue(ticket, job)
                self._finish(ticket, job, reason='observation_unavailable')
        completed = 0
        for _ in range(len(self._slot_rotation)):
            slot = self._slot_rotation.popleft()
            self._slot_rotation.append(slot)
            ticket = self._active.get(slot)
            if ticket is None:
                continue
            if slot.fault.is_set():
                self._faulted = True
                continue  # Retain the original unknown slot, never infer cleanup.
            if completed >= self._limits.owner_steps_per_cycle:
                continue
            try:
                actual, reply = slot.outgoing.get_nowait()
            except queue.Empty:
                continue
            if actual is not ticket:
                self._faulted = True
                self._reply_mismatch(slot)
                continue
            job = self._job(ticket)
            if type(reply) is not ProbeCompletion or reply.capture is not job.reservation.capture:
                self._faulted = True
                self._reply_mismatch(slot)
                continue
            completed += 1
            del self._active[slot]
            self._active_projects.remove(job.key[0])
            if (self._closing or job.cancelled or time.monotonic_ns() >= job.deadline_ns
                    or not self._subscribed(ticket, job)):
                self._owner.abort(job.reservation)
                self._finish(ticket, job, reason='observation_unavailable')
                continue
            try:
                result = self._owner.complete(job.reservation, reply)
            except ObservationError as error:
                self._finish(ticket, job, reason=error.reason_code)
            except Exception:
                self._finish(ticket, job, reason='observation_unavailable')
            else:
                self._finish(ticket, job, result=result)
        if self._closing or self._faulted:
            return
        offered = 0
        for slot in self._slots:
            if slot in self._active or offered >= self._limits.owner_steps_per_cycle:
                continue
            for _ in range(len(self._rotation)):
                if not self._rotation:
                    break
                project = self._rotation.popleft()
                self._rotation.append(project)
                if project in self._active_projects:
                    continue
                ticket = self._queues[project][0]
                job = self._job(ticket)
                try:
                    self._owner._authorize(job.context, job.snapshot, job.before_publish)
                except Exception:
                    self._owner.abort(job.reservation)
                    self._unqueue(ticket, job)
                    self._finish(ticket, job, reason='observation_not_authorized')
                    continue
                self._unqueue(ticket, job)
                slot.incoming.put_nowait((ticket, job.reservation.capture))
                self._active[slot], job.state = ticket, 'active'
                self._active_projects.add(project)
                offered += 1
                break

    def _subscribed(self, ticket, job):
        if job.billing_parent is not None:
            owner, parent = job.billing_parent
            if (owner is not self._billing_owner or owner._observations is not self
                    or parent not in owner._jobs or owner._jobs[parent].observation is not ticket
                    or owner._jobs[parent].cancelled):
                return False
            ticket = parent
        return self._ingress_owner is None or self._ingress_owner._claim_has_subscribers(ticket)

    def drive(self):
        with self._operation():
            self._drive()

    def take(self, ticket, *, _ingress=None):
        with self._operation():
            self._delivery(_ingress)
            job = self._job(ticket)
            if job.state != 'ready':
                return None
            expired = time.monotonic_ns() >= job.deadline_ns
            reason = 'observation_unavailable' if expired else job.reason
            if reason is None:
                try:
                    self._owner.recheck_reply(job.key, job.request, job.context, job.snapshot,
                                              job.before_publish, job.result)
                except ObservationError as error:
                    reason = error.reason_code
                except Exception:
                    reason = 'observation_unavailable'
            reply = ObservationJobReply(job.key, job.request, job.result if reason is None else None,
                                        reason, job.before_publish)
            self._drop(ticket, job)
            return reply

    def _cancel(self, ticket, job):
        job.cancelled = True
        if job.state == 'queued':
            self._owner.abort(job.reservation)
            self._unqueue(ticket, job)
            self._finish(ticket, job, reason='observation_unavailable')
        elif job.state == 'ready':
            if self._closing:
                self._drop(ticket, job)
            else:
                job.result, job.reason = None, 'observation_unavailable'

    def cancel(self, ticket, *, _ingress=None):
        with self._operation():
            self._delivery(_ingress)
            self._cancel(ticket, self._job(ticket))

    def request_close(self):
        with self._operation():
            self._closing = True
            for slot in self._slots:
                slot.stop.set()
            for ticket, job in tuple(self._jobs.items()):
                self._cancel(ticket, job)

    def drain(self):
        with self._operation():
            if not self._closing:
                raise ObservationError('observation_unavailable')
            self._drive()
            return (not self._active and not self._jobs
                    and all(slot.thread.ident is None or not slot.thread.is_alive() for slot in self._slots))

    def snapshot(self):
        with self._operation():
            return {'jobs': len(self._jobs), 'projects': len(self._counts), 'active': len(self._active),
                    'queued': sum(len(items) for items in self._queues.values()), 'workers': len(self._slots),
                    'retained_bytes_limit': self._limits.retained_bytes,
                    'started': self._started, 'closing': self._closing, 'faulted': self._faulted}
