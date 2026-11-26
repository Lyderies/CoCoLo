"""Fixed fair billing phases on the original account/claim owner.

The singleton signature pool has one driver. This companion consumes only its
own original handles; fixed observation/model workers receive no owner graph.
Native enrollment and closed-descendant containment remain adapter gates.
"""
from collections import deque
from dataclasses import dataclass
import itertools
import sqlite3
import time

from . import lane_health
from .account_billing_admission import AccountBillingAdmission, ProbeAdmission, ProbeAdmissionError
from .account_billing_execution import AccountBillingExecutions
from .account_observation import ObservationContext
from .account_observation_jobs import AccountObservationJobs
from .admission_inputs import AdmissionCaptureError, public_capture_reason
from .billing_probe_contracts import PROBE_STEP_REASONS
from .billing_probe_worker import BillingProbeWorkers, ProbeWorkerError
from .owned_execution import ExecutionCustodyError
from .history_contracts import _DIGEST, _ID, _PROJECT, matches
from .provider_observation_contracts import OBSERVATION_REASONS, ObservationInput
from .sql_read_budget import SQLReadError
from .signature_job_intake import SignatureJobError


# An identity observation refused for one of these is tried once more before the probe fails:
# the account's services, or the client, may not have been ready yet (just after a restart).
RETRIED_OBSERVATION_REASONS = frozenset({'observation_unavailable', 'account_unavailable'})
# Decision 47 (H1): a ready yes whose inputs changed before it was taken is judged again; if they keep changing,
# after this many new judgments it is refused by name (probe_replay_stale) and the client asks again.
MAX_REJUDGMENTS = 2
MAX_RECORD_FAILURES = 3


def _transient_record_failure(error):
    if isinstance(error, SQLReadError):
        return error.reason_code in {'sql_read_busy', 'sql_read_deadline', 'sql_read_unavailable'}
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if type(code) is int:
        return code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    # Constructed/forwarded OperationalErrors may lack SQLite's numeric code.
    return str(error) in {'database is locked', 'database table is locked', 'database schema is locked'}


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
    discard_faults: int = 0  # Transient errors while discarding, retried (0.3.3, C9d).
    cleanup_requested: bool = False  # Its preparation was asked to clean up.
    cancelled: bool = False
    observed_again: bool = False
    token: int = 0  # S1-H1a: counts this job once in the lane's health notes
    noted: bool = False  # S1-H1a: its failure reason has been noted
    closed: bool = False  # O0b RS-A round 2 G9: its reservation was closed by the sweep; ends with the stored answer
    timed_out: bool = False  # O0b RS-A round 3: cancelled by its own deadline, not by a caller or the close
    ended: bool = False  # O0c M10: unknown, and its admitted row settled (or none was admitted) once its child ended
    settle_after_ns: int = 0  # O0c review H1: a failed settlement is tried again from then
    record_failures: int = 0  # N3: non-transient failures; busy SQL does not spend this budget
    judgment: object = None  # decision 47 (H1, M1): what the owner's judgment (or the commit) answered, checked at delivery
    rejudged: int = 0  # decision 47 (H1): new judgments started because a ready yes's inputs had changed
    ident: str = ''  # the request id of this generation's identity observation
    own: bool = False  # it made the commit it answers (not a repeat of an earlier request): its re-judgments say known=False


@dataclass(frozen=True, repr=False)
class BillingJobReply:
    key: tuple
    result: dict | None
    reason: str | None
    before_publish: object
    context: ObservationContext
    history: dict | None = None  # a repeat's unchanged committed row (decision 44), checked at publication
    judgment: object = None  # decision 47 (M1): the committed row and reply digest of this answer's generation


# S1-H1a: the reasons that are failures (a refusal such as a cooldown is not one).
_FAILURE_REASONS = frozenset({'probe_preparation_unknown', 'probe_preparation_failed', 'probe_worker_failed',
                              'probe_step_failed', 'probe_unavailable'})


class AccountBillingJobs:
    def _publish_health(self):
        """S1-H1a: this owner's note from the job table (unknown jobs kept, the rest busy), and each
        job's failure reason once. Reporting only; never raises."""
        try:
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.reason in _FAILURE_REASONS and not job.noted:
                    job.noted = True
                    lane_health.safely(lambda: self.health.note_failure(self._note, job.reason, None))
            # O0c M9: an unknown job faults the lane (so the self-restart fires); a job whose signature
            # work is held unconfirmed is kept too, since it waits on the stuck lane and runs nothing.
            held = self._claims._pool.unknown_handles()
            kept = frozenset(job.token for job in jobs if job.state == 'unknown' or job.signature in held)
            lane_health.safely(lambda: self._note.publish(faulted=any(job.state == 'unknown' for job in jobs),
                                                          kept=kept, busy=sum(job.token not in kept for job in jobs)))
            # S1-H1a C6: a probe worker a kept job still holds is held in the probe lane (its own
            # slot there, written only by this owner).
            handles = {self._executions.worker_of(job.preparation) for job in jobs
                       if job.state == 'unknown' and job.preparation is not None} - {None}
            tokens = frozenset(self._workers._jobs[handle].token for handle in handles if handle in self._workers._jobs)
            for _ in tokens - self._parent_held:
                self._workers.health.enter_held()
            for _ in self._parent_held - tokens:
                self._workers.health.leave_held()
            self._parent_held = tokens
            # O0c M10: a probe worker an unknown job holds faults the probe lane too.
            lane_health.safely(lambda: self._parent_note.publish(kept=tokens, faulted=bool(tokens)))
        except Exception:
            pass

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
        self._generations = itertools.count()  # makes each identity observation's request id its own (decision 47)
        self.health = lane_health.Lane('billing')  # S1-H1a: published notes, reporting only
        self._note = self.health.slot('owner')
        self._parent_note = workers.health.slot('billing-parent')
        self._parent_held = frozenset()
        self._rotation = deque()
        self._queues = {}
        self._ingress_owner, self._closing, self._faulted = None, False, False
        claims._billing = observations._billing_owner = self
        self._owner._billing_jobs = self  # O0b RS-A round 2 G9: the owner leaves live jobs' originals to them

    def held_reservations(self):
        """O0b RS-A round 2 G9: the originals live jobs hold (the owner never forgets these behind a job)."""
        return frozenset(job.reservation for job in self._jobs.values() if job.reservation is not None)

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
            if job.state != 'unknown' or job.result is not None or not self._owner.committed(project, runtime, caller,
                                                                                         identity, context):
                return ticket
            # A fault after the commit left this job as local custody only. A repeat is a new judgment of the
            # committed row (decision 44): the custody job stays where it is, the request key moves to the new job.
            del self._requests[key]
        if (self._counts.get(project, 0) >= self._limits.jobs_per_project
                or project not in self._counts and len(self._counts) >= self._limits.max_projects):
            raise ProbeAdmissionError('probe_full')
        ticket, job = object(), _Job(key, context, snapshot, before_publish, token=lane_health.new_token())
        self._jobs[ticket], self._requests[key] = job, ticket
        self._publish_health()
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
                self._bind(job)
            else:
                job.own = original.history is None
                self._begin_judgment(ticket, job, original)
            return ticket
        except BaseException:
            if job.reservation is not None and job.observation is None:
                self._owner.abort_reserved(job.reservation)
            self._drop(ticket)
            raise

    def _begin_judgment(self, ticket, job, original):
        """Every judgment, a repeat's too (decision 47, M-identity), starts with an identity observation of its
        own: the local client's login and version, no model call, nothing spent. A repeat's request id carries its
        generation, so a stored observation of an earlier one is never replayed in its place."""
        job.reservation, job.state = original, 'identity'
        job.ident = 'billing-ident-'+original.key + (
            '' if original.history is None else f'-g{original.created}-{next(self._generations)}')
        job.observation = self._observations.submit(job.key[0], job.key[1], job.key[2],
            job.ident, ObservationInput('claude'), job.context, job.before_publish,
            _ingress=self._ingress_owner, _billing_parent=(self, ticket))

    def _bind(self, job):
        """M1: an answer no judgment made is bound to the committed row and reply it has now. Never raises."""
        if job.result is None or job.judgment is not None:
            return
        try:
            # A run this job completed brings the judgment it was admitted on (the owner's commit made it).
            job.judgment = (None if job.reservation is None else self._owner.judgment_of(job.reservation)
                            ) or self._owner.bind(*job.key, job.result)
        except Exception:
            job.judgment = None

    def _rejudge(self, ticket, job):
        """H1: a ready yes whose judged inputs are not as they were is judged again, as a repeat of its own committed
        row: a new reservation, identity observation and signature work; no provider call. Returns None while that
        runs, a reply when it ends in a refusal, False when the request now answers from its stored refusal."""
        if job.rejudged >= MAX_REJUDGMENTS:
            self._drop(ticket)
            return BillingJobReply(job.key, None, 'probe_replay_stale', job.before_publish, job.context)
        job.rejudged += 1
        original = None
        try:
            original = self._owner.reserve(*job.key, job.context, before_commit=lambda: self._authorize(ticket, job),
                                           known=not job.own)
            if type(original) is dict:
                job.result, job.judgment = original, None
                self._bind(job)
                return False
            job.result, job.reason, job.judgment = None, None, None
            self._begin_judgment(ticket, job, original)
            return None
        except Exception as error:
            if original is not None and type(original) is not dict and job.observation is None:
                self._owner.abort_reserved(original)
            reason = error.reason_code if isinstance(error, ProbeAdmissionError) else 'probe_unavailable'
            if not isinstance(error, ProbeAdmissionError):
                lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_rejudge_failed', error))
            self._drop(ticket)
            return BillingJobReply(job.key, None, reason, job.before_publish, job.context)

    def _drop(self, ticket):
        job = self._jobs.pop(ticket)
        self._publish_health()
        if self._requests.get(job.key) == ticket:
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
        self._bind(job)
        if self._closing:
            self._drop(ticket)

    def _name_failure(self, job, cause, error=None):
        """A failure that answers a bare probe_unavailable names its cause in this lane's last_failure,
        once per job (the recurring-issue rule: no unnamed probe_unavailable); a cause already named for this
        job stays the one shown. Never raises."""
        if job.noted:
            return
        job.noted = True
        lane_health.safely(lambda: self.health.note_failure(self._note, cause, error))

    def _stop_cause(self, job):
        """Why a job is stopped without a failure of its own: its deadline, the close, or a caller's cancel."""
        return ('probe_deadline_expired' if job.timed_out else 'probe_closing' if self._closing
                else 'probe_cancelled')

    def _discard(self, job, reason='probe_unavailable', *, cause=None, error=None):
        if cause is not None:
            self._name_failure(job, cause, error)
        job.reason, job.state = reason, 'discarding'
        self._request_cleanup(job)

    def _request_cleanup(self, job):
        """Ask the preparation to clean up; a failed request is asked again on the next step, and
        after three failures the job is kept and answered (0.3.3, C9d)."""
        if job.preparation is None or job.cleanup_requested:
            return
        try:
            self._executions.discard(job.preparation)
            job.cleanup_requested = True
        except Exception as error:
            job.noted = True  # S1-H1a C7: this failure is the one shown, not a later synthetic one
            lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_cleanup_failed', error))
            self._discard_fault(job)

    def _discard_fault(self, job):
        job.discard_faults += 1
        if job.discard_faults >= 3:
            job.state = 'unknown'

    def owns_signature(self, handle):
        return handle in self._signatures

    def owns_owner_signature(self, handle):
        return self._owner.owns_signature(handle)

    def finish_signature(self, handle):
        self._guard()
        ticket = self._signatures[handle]
        job = self._jobs[ticket]
        cause = 'probe_admit_failed'
        try:
            if job.cancelled or self._closing or not self._subscribed(ticket):
                cause = self._stop_cause(job)  # a stop is named as one, never as an admission failure
                raise ProbeAdmissionError()
            result = self._owner.admit(job.reservation)
            if type(result) is ProbeAdmission:
                job.permit, job.state = result, 'offered'
                job.supervisor = self._executions.launch(job.preparation, result)
            else:
                job.result, job.judgment = result, self._owner.judgment_of(job.reservation)
                self._discard(job, reason=None)
        except Exception as error:
            row = self._owner._row(job.key[0], job.reservation.key)
            if row is not None and row['state'] == 'admitted':
                # Admitted, then a step before the launch failed. With no execution ticket nothing
                # can have started: settle the row and free the machine's probe worker, rather than
                # keep both until a restart (0.3.3, C9). Once a ticket exists the run is kept.
                self._settle_failed_step(job, unsure='probe_unavailable')
            else:
                self._discard(job, getattr(error, 'reason_code', 'probe_unavailable'),
                              cause=cause, error=error)
        finally:
            if handle in self._claims._pool._returned:
                self._claims._pool.discard(handle)
                self._owner._signatures.pop(job.reservation, None)
            self._signatures.pop(handle)
            job.signature = None

    def _settle_ended(self, job):
        """O0c M10: an unknown job's admitted row is settled once no model it launched can still run, so
        it stops holding probe capacity and the self-restart. Review M5: a probe that ran and returned
        valid records has them committed, as its normal completion would; otherwise the row becomes a
        denial naming the failed step. Busy SQL is retried a second later without consuming the
        three-attempt record failure budget. Once that budget is spent, only denial is retried;
        the job is done only once its row is settled or no longer admitted."""
        if job.ended or job.settled or job.reservation is None or time.monotonic_ns() < job.settle_after_ns:
            return
        try:
            if not self._executions.child_ended(job.reservation):
                return
            row = self._owner._row(job.key[0], job.reservation.key)
            if row is None or row['state'] != 'admitted':
                job.ended = True  # nothing admitted to settle
                return
            if (job.record_failures < MAX_RECORD_FAILURES and job.preparation is not None
                    and self._executions.returned(job.preparation)):
                try:
                    self._executions.complete_ended(job.preparation)
                    job.ended = True
                    return
                except (ProbeAdmissionError, ExecutionCustodyError):
                    # Its packet does not validate, or its execution has no cleanup to retain (it never
                    # started a child): settled as a denial below. A busy ledger is retried instead.
                    pass
                except Exception as error:
                    if _transient_record_failure(error):
                        raise  # Transient reads never exhaust the record budget.
                    job.record_failures += 1
                    if job.record_failures < MAX_RECORD_FAILURES:
                        raise
                    lane_health.safely(lambda error=error: self.health.note_failure(
                        self._note, 'probe_records_unkept', error))
            reason = job.reason if job.reason in PROBE_STEP_REASONS else 'probe_worker_failed'
            self._owner.settle_ended(job.reservation, reason)
            job.ended = True
        except Exception as error:
            # Not settled yet: the row keeps holding, and this is tried again.
            job.settle_after_ns = time.monotonic_ns() + 1_000_000_000
            lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_settle_failed', error))

    def _step(self, ticket, job):
        if job.state == 'unknown':
            self._settle_ended(job)
            return
        if job.state == 'ready':
            return
        if job.reservation is not None and job.state != 'offered':
            explicit = self._closing or job.cancelled or not self._subscribed(ticket)
            if explicit or time.monotonic_ns() >= job.reservation.deadline_ns:
                # O0b RS-A round 3: a cancel by its own deadline is told apart, so a close the sweep commits
                # meanwhile still answers with its stored denial.
                job.timed_out = job.timed_out or not explicit
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
                    job.ident+'-again', ObservationInput('claude'), job.context,
                    job.before_publish, _ingress=self._ingress_owner, _billing_parent=(self, ticket))
            elif reply.reason is not None:
                # Say why: the observation's own fixed reason.
                recognised = reply.reason in OBSERVATION_REASONS  # only an unrecognised reason is a bare one
                self._discard(job, reply.reason if recognised else 'probe_unavailable',
                              cause=None if recognised else 'probe_observation_reason_unrecognised')
            else:
                job.identity = next(r for r in reply.result['records'] if r['record_type'] == 'provider_identity')
                job.state = 'prepare'
        if job.state == 'prepare' and job.reservation.history is not None:
            # A repeat (decision 44) never launches: no probe worker is prepared for it; once its identity is
            # observed it goes to the signature work its fresh judgment needs.
            job.identity = None
            if not self._start_signature(ticket, job):
                return
        if job.state == 'prepare':
            try:
                job.preparation = self._executions.prepare(job.reservation, job.identity)
            except ProbeWorkerError as error:
                if error.reason_code == 'probe_worker_full':
                    kept = frozenset(self._executions.worker_of(j.preparation) for j in self._jobs.values()
                                     if j.state == 'unknown' and j.preparation is not None) - {None}
                    if self._workers.stuck(kept):
                        # Nothing will free a worker before a restart: say so now (0.3.3, C9c).
                        self._discard(job, 'probe_worker_unavailable')
                    return
                raise
            job.identity, job.state = None, 'preparing'
        if job.state == 'preparing':
            state = self._executions.snapshot(job.preparation)['worker_state']
            if state == 'prepared' and self._claims._pool.stuck():
                # O0c M8: no signature worker can free before the account restarts itself: say so now,
                # rather than wait as work that would hold that restart.
                self._discard(job, 'probe_unavailable', cause='probe_signature_pool_stuck')
                return
            if state == 'prepared':
                self._start_signature(ticket, job)
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
                self._request_cleanup(job)
                if job.state != 'discarding' or not job.cleanup_requested:
                    return
                state = self._executions.snapshot(job.preparation)['worker_state']
                if state == 'unknown':
                    # Its cleanup could not be confirmed: answer, and keep custody (0.3.3, C9d).
                    job.state, job.reason = 'unknown', 'probe_preparation_unknown'
                    return
                if state not in {'cleaned', 'failed'}:
                    return
                self._executions.retire_discarded(job.preparation)
                job.preparation = None
            # Abort only a reservation still reserved; a committed one is only released from memory (C9d).
            if (job.result is None and not job.settled and job.reservation is not None
                    and self._owner._pending.get((job.reservation.project, job.reservation.key)) is job.reservation):
                row = self._owner._row(job.key[0], job.reservation.key)
                if row is not None and row['state'] == 'committed':
                    self._owner.release_committed(job.reservation)
                    if (job.closed or job.timed_out or not job.cancelled) and self._owner.closed_unlaunched(row):
                        # O0b RS-A round 2 G9: the sweep closed it; the job's answer is the stored denial,
                        # as a replay reads it, whichever step noticed first (round 3: also when its own
                        # deadline cancelled it first; only a caller's cancel or a close keeps its own answer).
                        job.result, job.reason = self._owner._reply(row, known=True), None
                else:
                    self._owner.abort_reserved(job.reservation)
            self._ready(ticket, job, result=job.result, reason=job.reason)

    def _start_signature(self, ticket, job):
        """Start the signature work of a job's judgment. False when the job was discarded or must wait."""
        try:
            job.signature = self._owner.prepare(job.reservation)
        except AdmissionCaptureError as error:
            # Nothing was admitted; the history admission needs did not fit or
            # could not be read, and the probe says which.
            self._discard(job, public_capture_reason(error))
            return False
        except SignatureJobError as error:
            if error.reason_code in {'signature_job_project_full', 'signature_job_project_limit'}:
                # Intake refused before returning any original handle.
                # Keep this unoffered preparation on the same deadline;
                # normal cancellation/expiry still retires it locally.
                return False
            raise
        self._signatures[job.signature], job.state = ticket, 'signature'
        return True

    def _end_closed(self, ticket, job):
        """O0b RS-A round 2 G9: the sweep closed this job's never-admitted reservation. Its observation,
        signature work and preparation stop as a cancel stops them (a released signature handle leaves both
        mappings now; a running one stays mapped until its return is discarded), and once discarded the job
        answers with the stored denial. Not a failure, so nothing is noted."""
        if job.state in {'ready', 'unknown', 'discarding', 'offered'} or job.result is not None:
            return
        job.closed, job.noted = True, True
        self.cancel(ticket, _ingress=self._ingress_owner)
        job.reason = None

    def _settle_failed_step(self, job, *, unsure='probe_step_failed'):
        """A step failed. A probe that has not started frees its worker and names the step; one that
        may have started keeps every original handle (0.3.3, C9 and C9b)."""
        if job.state == 'discarding':
            # Discarding is resumed on the next step; a persistent error keeps the job (0.3.3, C9d).
            self._discard_fault(job)
            if job.state == 'unknown':
                # A discarding job reaches this through a fault in a discarding step, which `drive` names
                # (`probe_step_failed`, setting noted) before calling here; the one caller that passes
                # unsure='probe_unavailable' (`finish_signature`) never has a discarding job with an admitted
                # row. So the bare reason left here is never an unnamed one.
                job.reason = job.reason or unsure
            return
        try:
            # The execution ticket, issued inside the launch, is what says a probe may have started.
            if job.state in {'ready', 'unknown'} or job.supervisor is not None or (
                    job.preparation is not None and self._executions.ticketed(job.reservation)):
                raise ProbeAdmissionError()
            if job.reservation is not None:
                row = self._owner._row(job.key[0], job.reservation.key)
                if row is not None and row['state'] == 'admitted':
                    self._owner.settle_unlaunched(job.reservation, 'probe_step_failed')
                    job.settled = True
            self._discard(job, 'probe_step_failed')
        except Exception as error:
            job.state, job.reason = 'unknown', unsure
            if unsure == 'probe_unavailable':
                self._name_failure(job, 'probe_settle_unconfirmed', error)

    def drive(self):
        self._guard()
        # O0b RS-A round 2 G3: each owner step, with or without a request, forgets probes a sweep has closed.
        # G9: a failure is noted in this lane's health, never faults the claims lane, and the notice stays set,
        # so the next step tries again; live jobs whose originals were closed end with the stored answer.
        try:
            self._owner.forget_expired()
        except Exception as error:
            lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_forget_failed', error))
        closed, self._owner._closed_live = self._owner._closed_live, set()
        if closed:
            for ticket, job in list(self._jobs.items()):
                if job.reservation in closed:
                    try:
                        self._end_closed(ticket, job)
                    except Exception as error:
                        # O0b RS-A round 3: ending it failed (its observation or signature work could not be
                        # stopped): noted in this lane, never escaping into the claims lane, and kept so the
                        # next step tries again.
                        self._owner._closed_live.add(job.reservation)
                        lane_health.safely(lambda error=error: self.health.note_failure(
                            self._note, 'probe_forget_failed', error))
        for _ in range(min(self._limits.owner_steps, len(self._rotation))):
            project = self._rotation.popleft()
            self._rotation.append(project)
            ticket = self._queues[project].popleft()
            self._queues[project].append(ticket)
            job = self._jobs[ticket]
            try:
                self._step(ticket, job)
            except Exception as error:
                job.noted = True  # S1-H1a: noted here, with the exception's type
                lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_step_failed', error))
                self._settle_failed_step(job)
        self._publish_health()
        if self._closing and not self._jobs:
            self._workers.close()

    def take(self, ticket, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
        job = self._jobs[ticket]
        if job.state not in {'ready', 'unknown'}:
            return None
        result, reason = job.result, job.reason
        if job.state == 'ready' and result is not None and self._owner._rejudges(result) and not self._current(job):
            # H1: a yes is never delivered from storage once the inputs it judged have moved: judge again.
            again = self._rejudge(ticket, job)
            if again is not False:
                return again
            result, reason = job.result, job.reason
        history = None if job.reservation is None else job.reservation.history
        if result is not None:
            try:
                # BR-8, M1: integrity of the exact row and reply of this job's judgment generation. Decision 50: the
                # yes itself is gated at publication (the one final gate); this check builds none.
                self._owner.verify_committed(*job.key, job.context, before_commit=job.before_publish,
                                             reply=result, history=history, judgment=job.judgment, gate=False)
            except ProbeAdmissionError as error:
                result, reason = None, error.reason_code
            except Exception as error:
                result, reason = None, 'probe_unavailable'
                lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'probe_reply_read_failed', error))
        reply = BillingJobReply(job.key, result, reason, job.before_publish, job.context, history, job.judgment)
        if job.state == 'ready':
            self._drop(ticket)
        return reply

    def _current(self, job):
        try:
            return job.judgment is not None and self._owner.gate_current(job.judgment)
        except Exception:
            return False

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
        self._discard(job, cause=self._stop_cause(job))
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
