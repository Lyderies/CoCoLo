"""Fixed fair billing phases on the original account/claim owner.

The singleton signature pool has one driver. This companion consumes only its
own original handles; fixed observation/model workers receive no owner graph.
Native enrollment and closed-descendant containment remain adapter gates.
"""
from collections import deque
from dataclasses import dataclass
import time

from .account_billing_admission import AccountBillingAdmission, ProbeAdmission, ProbeAdmissionError
from .account_billing_execution import AccountBillingExecutions
from .account_observation import ObservationContext
from .account_observation_jobs import AccountObservationJobs
from .admission_inputs import AdmissionCaptureError, public_capture_reason
from .billing_probe_worker import BillingProbeWorkers, ProbeWorkerError
from .history_contracts import _DIGEST, _ID, _PROJECT, matches
from .provider_observation_contracts import OBSERVATION_REASONS, ObservationInput
from .signature_job_intake import SignatureJobError


# An identity observation refused for one of these is tried once more before the probe fails:
# the account's services, or the client, may not have been ready yet (just after a restart).
RETRIED_OBSERVATION_REASONS = frozenset({'observation_unavailable', 'account_unavailable'})

@dataclass(frozen=True)
class BillingJobLimits:
    max_projects: int
    jobs_per_project: int
    owner_steps: int

    def __post_init__(self):
        if (any(type(v) is not int or not 1 <= v <= cap for v, cap in
                ((self.max_projects, 64), (self.jobs_per_project, 64), (self.owner_steps, 16)))
                or self.max_projects*self.jobs_per_project > 64):
            raise ProbeAdmissionError('probe_invalid_request')


@dataclass(repr=False)
class _Job:
    key: tuple
    context: ObservationContext
    snapshot: tuple
    before_publish: object
    state: str = 'reserving'
    reservation: object = None
    observation: object = None
    identity: object = None
    preparation: object = None
    signature: object = None
    permit: object = None
    supervisor: object = None
    result: dict | None = None
    reason: str | None = None
    settled: bool = False  # Its admitted row was settled with no launch (0.3.3, C9).
    cancelled: bool = False
    observed_again: bool = False


@dataclass(frozen=True, repr=False)
class BillingJobReply:
    key: tuple
    result: dict | None
    reason: str | None
    before_publish: object
    context: ObservationContext


class AccountBillingJobs:
    def __init__(self, claims, observations, workers, limits):
        from .account_claim_jobs import AccountClaimJobs
        if (type(claims) is not AccountClaimJobs or type(observations) is not AccountObservationJobs
                or type(workers) is not BillingProbeWorkers or type(limits) is not BillingJobLimits):
            raise ProbeAdmissionError('probe_invalid_request')
        claims._guard()
        observations._owner._guard()
        workers._owner()
        limits.__post_init__()
        if (claims._started or claims._closing or claims._jobs or claims._billing is not None
                or observations._started or observations._jobs or observations._billing_owner is not None
                or observations._owner._views is not claims._views):
            raise ProbeAdmissionError()
        self._claims, self._observations, self._workers = claims, observations, workers
        self._owner = AccountBillingAdmission(claims._views, claims._pool, claims._work_limits)
        self._executions = AccountBillingExecutions(self._owner, workers, workers._registry)
        self._limits = BillingJobLimits(limits.max_projects, limits.jobs_per_project, limits.owner_steps)
        self._jobs, self._requests, self._counts, self._signatures = {}, {}, {}, {}
        self._rotation = deque()
        self._queues = {}
        self._ingress_owner, self._closing, self._faulted = None, False, False
        claims._billing = observations._billing_owner = self

    def _guard(self):
        self._owner._guard()
        if self._claims._billing is not self or self._observations._billing_owner is not self:
            raise ProbeAdmissionError()

    def _delivery(self, ingress):
        if self._ingress_owner is not None and ingress is not self._ingress_owner:
            raise ProbeAdmissionError()

    def _subscribed(self, ticket):
        return self._ingress_owner is None or self._ingress_owner._claim_has_subscribers(ticket)

    def _authorize(self, ticket, job):
        self._owner._authorize(job.context, job.snapshot, job.before_publish)
        return (job.state in {'reserving', 'offered', 'unknown', 'ready'}
                or not job.cancelled and not self._closing and self._subscribed(ticket))

    def submit(self, project, runtime, caller, identity, context, before_publish, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
        if not self._claims._started or not self._observations._started or self._closing or self._faulted:
            raise ProbeAdmissionError()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(context) is not ObservationContext or not callable(before_publish)):
            raise ProbeAdmissionError('probe_invalid_request')
        key, snapshot = (project, runtime, caller, identity), context.snapshot()
        if key in self._requests:
            ticket = self._requests[key]
            job = self._jobs[ticket]
            if job.context is not context or job.snapshot != snapshot or job.cancelled:
                raise ProbeAdmissionError('probe_conflict')
            self._owner._authorize(context, snapshot, before_publish)
            return ticket
        if (self._counts.get(project, 0) >= self._limits.jobs_per_project
                or project not in self._counts and len(self._counts) >= self._limits.max_projects):
            raise ProbeAdmissionError('probe_full')
        ticket, job = object(), _Job(key, context, snapshot, before_publish)
        self._jobs[ticket], self._requests[key] = job, ticket
        self._counts[project] = self._counts.get(project, 0)+1
        if project not in self._queues:
            self._queues[project] = deque()
            self._rotation.append(project)
        self._queues[project].append(ticket)
        try:
            original = self._owner.reserve(project, runtime, caller, identity, context,
                                           before_commit=lambda: self._authorize(ticket, job))
            if type(original) is dict:
                job.result, job.state = original, 'ready'
            else:
                job.reservation, job.state = original, 'identity'
                job.observation = self._observations.submit(project, runtime, caller,
                    'billing-ident-'+original.key, ObservationInput('claude'), context, before_publish,
                    _ingress=self._ingress_owner, _billing_parent=(self, ticket))
            return ticket
        except BaseException:
            if job.reservation is not None and job.observation is None:
                self._owner.abort_reserved(job.reservation)
            self._drop(ticket)
            raise

    def _drop(self, ticket):
        job = self._jobs.pop(ticket)
        self._requests.pop(job.key)
        project = job.key[0]
        self._queues[project].remove(ticket)
        if not self._queues[project]:
            self._queues.pop(project)
            self._rotation.remove(project)
        self._counts[project] -= 1
        if not self._counts[project]:
            self._counts.pop(project)

    def _ready(self, ticket, job, *, result=None, reason=None):
        job.result, job.reason, job.state = result, reason, 'ready'
        if self._closing:
            self._drop(ticket)

    def _discard(self, job, reason='probe_unavailable'):
        job.reason, job.state = reason, 'discarding'
        if job.preparation is not None:
            self._executions.discard(job.preparation)

    def owns_signature(self, handle):
        return handle in self._signatures

    def finish_signature(self, handle):
        self._guard()
        ticket = self._signatures[handle]
        job = self._jobs[ticket]
        try:
            if job.cancelled or self._closing or not self._subscribed(ticket):
                raise ProbeAdmissionError()
            result = self._owner.admit(job.reservation)
            if type(result) is ProbeAdmission:
                job.permit, job.state = result, 'offered'
                job.supervisor = self._executions.launch(job.preparation, result)
            else:
                job.result = result
                self._discard(job, reason=None)
        except Exception as error:
            row = self._owner._row(job.key[0], job.reservation.key)
            if row is not None and row['state'] == 'admitted':
                # Admitted, then a step before the launch failed. With no execution ticket nothing
                # can have started: settle the row and free the machine's probe worker, rather than
                # keep both until a restart (0.3.3, C9). Once a ticket exists the run is kept.
                try:
                    self._executions.discard(job.preparation)
                    self._owner.settle_unlaunched(job.reservation, 'probe_step_failed')
                    job.settled = True
                    self._discard(job, 'probe_step_failed')
                except Exception:
                    job.state, job.reason = 'unknown', 'probe_unavailable'
            else:
                self._discard(job, getattr(error, 'reason_code', 'probe_unavailable'))
        finally:
            if handle in self._claims._pool._returned:
                self._claims._pool.discard(handle)
                self._owner._signatures.pop(job.reservation, None)
            self._signatures.pop(handle)
            job.signature = None

    def _step(self, ticket, job):
        if job.state in {'ready', 'unknown'}:
            return
        if (job.reservation is not None and job.state != 'offered'
                and (self._closing or job.cancelled or time.monotonic_ns() >= job.reservation.deadline_ns
                     or not self._subscribed(ticket))):
            self.cancel(ticket, _ingress=self._ingress_owner)
        if job.state == 'identity':
            reply = self._observations.take(job.observation, _ingress=self._ingress_owner)
            if reply is None:
                return
            job.observation = None
            if reply.reason in RETRIED_OBSERVATION_REASONS and not job.observed_again:
                # Right after the services restart a first observation can be refused.
                # Observing spends nothing and nothing is launched yet: try once more.
                job.observed_again = True
                project, runtime, caller = job.key[0], job.key[1], job.key[2]
                job.observation = self._observations.submit(project, runtime, caller,
                    'billing-ident-'+job.reservation.key+'-again', ObservationInput('claude'), job.context,
                    job.before_publish, _ingress=self._ingress_owner, _billing_parent=(self, ticket))
            elif reply.reason is not None:
                # Say why: the observation's own fixed reason.
                self._discard(job, reply.reason if reply.reason in OBSERVATION_REASONS else 'probe_unavailable')
            else:
                job.identity = next(r for r in reply.result['records'] if r['record_type'] == 'provider_identity')
                job.state = 'prepare'
        if job.state == 'prepare':
            try:
                job.preparation = self._executions.prepare(job.reservation, job.identity)
            except ProbeWorkerError as error:
                if error.reason_code == 'probe_worker_full':
                    return
                raise
            job.identity, job.state = None, 'preparing'
        if job.state == 'preparing':
            state = self._executions.snapshot(job.preparation)['worker_state']
            if state == 'prepared':
                try:
                    job.signature = self._owner.prepare(job.reservation)
                except AdmissionCaptureError as error:
                    # Nothing was admitted; the history admission needs did not fit or
                    # could not be read, and the probe says which.
                    self._discard(job, public_capture_reason(error))
                    return
                except SignatureJobError as error:
                    if error.reason_code in {'signature_job_project_full', 'signature_job_project_limit'}:
                        # Intake refused before returning any original handle.
                        # Keep this unoffered preparation on the same deadline;
                        # normal cancellation/expiry still retires it locally.
                        return
                    raise
                self._signatures[job.signature], job.state = ticket, 'signature'
            elif state == 'failed':
                self._discard(job, 'probe_preparation_failed')
            elif state == 'unknown':
                job.state, job.reason = 'unknown', 'probe_preparation_unknown'
        if job.state == 'offered':
            state = self._executions.snapshot(job.preparation)['worker_state']
            if state == 'returned':
                result = self._executions.complete(job.preparation)
                job.preparation = None
                self._ready(ticket, job, result=result)
            elif state == 'unknown':
                job.state, job.reason = 'unknown', 'probe_worker_failed'
        if job.state == 'discarding':
            if job.observation is not None:
                if job.observation in self._observations._jobs:
                    if self._observations.take(job.observation, _ingress=self._ingress_owner) is None:
                        return
                job.observation = None
            if job.signature is not None:
                return
            if job.preparation is not None:
                state = self._executions.snapshot(job.preparation)['worker_state']
                if state not in {'cleaned', 'failed'}:
                    return
                self._executions.retire_discarded(job.preparation)
                job.preparation = None
            if job.result is None and not job.settled:
                self._owner.abort_reserved(job.reservation)
            self._ready(ticket, job, result=job.result, reason=job.reason)

    def drive(self):
        self._guard()
        for _ in range(min(self._limits.owner_steps, len(self._rotation))):
            project = self._rotation.popleft()
            self._rotation.append(project)
            ticket = self._queues[project].popleft()
            self._queues[project].append(ticket)
            job = self._jobs[ticket]
            try:
                self._step(ticket, job)
            except Exception:
                # Keep every original handle/reservation after ambiguity.
                job.state, job.reason = 'unknown', 'probe_step_failed'
        if self._closing and not self._jobs:
            self._workers.close()

    def take(self, ticket, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
        job = self._jobs[ticket]
        if job.state not in {'ready', 'unknown'}:
            return None
        result, reason = job.result, job.reason
        if result is not None:
            try:
                actual = self._owner.reserve(*job.key, job.context, before_commit=job.before_publish)
                if actual != {**result, 'known': True}:
                    raise ProbeAdmissionError('probe_conflict')
            except ProbeAdmissionError as error:
                result, reason = None, error.reason_code
            except Exception:
                result, reason = None, 'probe_unavailable'
        reply = BillingJobReply(job.key, result, reason, job.before_publish, job.context)
        if job.state == 'ready':
            self._drop(ticket)
        return reply

    def cancel(self, ticket, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
        job = self._jobs[ticket]
        job.cancelled = True
        if job.state == 'offered':
            job.supervisor.request_stop()
            return 'cleanup_pending'
        if job.state in {'ready', 'unknown', 'discarding'}:
            return 'complete' if job.state == 'ready' else 'cleanup_pending'
        if job.observation is not None:
            self._observations.cancel(job.observation, _ingress=self._ingress_owner)
        if job.signature is not None:
            if self._claims._pool.cancel(job.signature) == 'released':
                self._signatures.pop(job.signature)
                self._owner._signatures.pop(job.reservation)
                job.signature = None
        self._discard(job)
        return 'cleanup_pending'

    def request_close(self):
        self._guard()
        self._closing = True
        for ticket in tuple(self._jobs):
            self.cancel(ticket, _ingress=self._ingress_owner)
            if ticket in self._jobs and self._jobs[ticket].state == 'ready':
                self._drop(ticket)

    def drained(self):
        self.drive()
        return (self._closing and not self._jobs
                and all(not s.thread.is_alive() for s in self._workers._slots))
