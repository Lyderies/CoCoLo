"""Owner-local deferred claims over the fixed bounded signature worker pool.

This coordinator starts no model work and authenticates no native caller. It
retains pending and completed replies within the same per-project ticket quota.
Only the persistence owner's original thread may capture or finish a claim;
signature parsing and verification run in the prestarted fixed worker pool.
"""
from __future__ import annotations

from dataclasses import dataclass

from .account_api import _DIGEST, _ID, _PROJECT, matches
from .account_claim_contracts import ClaimError, LaunchClaimInput
from .account_service import AccountOwnerViews
from .signature_job_intake import SignatureIntakeLimits, SignatureJobIntake
from .signature_worker_pool import SignatureWorkerLimits, SignatureWorkerPool
from .sql_read_budget import SQLReadLimits


@dataclass(frozen=True, repr=False)
class ClaimJobReply:
    context: object
    result: dict | None
    reason: str | None


@dataclass(repr=False)
class _Job:
    key: tuple[str, str, str, str]
    launch: LaunchClaimInput
    context: object = None
    signature_handle: object = None
    state: str = "reserved"
    result: dict | None = None
    reason: str | None = None
    cancelled: bool = False


class AccountClaimJobs:
    """One owner, fixed startup configuration, explicit submit/drive/take.

    Construction and bootstrap precede the request loop. Request methods create
    no thread/process and wait for no worker. Capture and final commit still use
    legacy JSON/filesystem paths; this is not a complete owner-loop latency or
    native enrollment guarantee. Tickets, notices and replies grant no spawn.
    """

    def __init__(self, views, intake_limits, worker_limits, work_limits):
        if (type(views) is not AccountOwnerViews or type(intake_limits) is not SignatureIntakeLimits
                or type(worker_limits) is not SignatureWorkerLimits or type(work_limits) is not SQLReadLimits):
            raise ClaimError("claim_invalid_request")
        views._guard()
        work_limits.__post_init__()
        self._views = views
        self._work_limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms,
                                         work_limits.max_value_bytes)
        self._intake = SignatureJobIntake(views, intake_limits)
        self._pool = SignatureWorkerPool(self._intake, worker_limits)
        self._jobs: dict[object, _Job] = {}
        self._requests: dict[tuple[str, str, str, str], object] = {}
        self._counts: dict[str, int] = {}
        self._signatures: dict[object, object] = {}
        self._started = self._closing = self._faulted = False

    def _guard(self):
        self._views._guard()
        if self._faulted:
            raise ClaimError("claim_unavailable")

    def bootstrap(self):
        self._guard()
        if self._started or self._closing:
            raise ClaimError("claim_unavailable")
        try:
            self._pool.bootstrap()
            self._started = True
        except BaseException:
            self._faulted = True
            raise

    def _job(self, ticket):
        if type(ticket) is not object or ticket not in self._jobs:
            raise ClaimError("claim_invalid_request")
        return self._jobs[ticket]

    def _drop(self, ticket):
        job = self._job(ticket)
        del self._requests[job.key]
        count = self._counts[job.key[0]]
        if count == 1:
            del self._counts[job.key[0]]
        else:
            self._counts[job.key[0]] = count - 1
        del self._jobs[ticket]

    def submit(self, project_id, *, runtime_digest, caller_scope, request_id, launch):
        self._guard()
        if not self._started or self._closing:
            raise ClaimError("claim_unavailable")
        if (not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
                or not matches(caller_scope, _DIGEST) or not matches(request_id, _ID)
                or type(launch) is not LaunchClaimInput):
            raise ClaimError("claim_invalid_request")
        launch = LaunchClaimInput(launch.run_handle, launch.identity_digest)
        key = (project_id, runtime_digest, caller_scope, request_id)
        existing = self._requests.get(key)
        if existing is not None:
            if self._job(existing).launch != launch:
                raise ClaimError("claim_conflict")
            return existing
        limits = self._intake._limits
        count = self._counts.get(project_id, 0)
        if (count >= limits.max_jobs_per_project
                or (not count and len(self._counts) >= limits.max_projects)
                or len(self._jobs) >= limits.max_projects * limits.max_jobs_per_project):
            raise ClaimError("claim_busy")
        ticket, job = object(), _Job(key, launch)
        try:
            # Reserve metadata/reply capacity before any capture or write.
            self._jobs[ticket] = job
            self._requests[key] = ticket
            self._counts[project_id] = count + 1
            context, known, reason, provider = self._views._claim_preflight(
                project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
                request_id=request_id, launch=launch,
            )
            job.context = context
            if known is not None:
                job.result, job.state = known, "ready"
            elif reason is not None:
                job.result = self._views._claim_commit_denied(context, reason)
                job.state = "ready"
            else:
                job.signature_handle = self._intake.capture(project_id, provider, self._work_limits)
                self._signatures[job.signature_handle] = ticket
                job.state = "pending"
            return ticket
        except BaseException:
            try:
                if job.signature_handle is not None:
                    if self._pool.cancel(job.signature_handle) != "released":
                        raise ClaimError("claim_unavailable")
                    self._signatures.pop(job.signature_handle, None)
                self._requests.pop(key, None)
                self._jobs.pop(ticket, None)
                if count:
                    self._counts[project_id] = count
                else:
                    self._counts.pop(project_id, None)
            except BaseException:
                self._faulted = True
            raise

    def drive(self):
        self._guard()
        if not self._started:
            raise ClaimError("claim_unavailable")
        try:
            for notice in self._pool.poll():
                ticket = self._signatures[notice.handle]
                job = self._job(ticket)
                if job.state != "pending":
                    raise ClaimError("claim_unavailable")
                if job.cancelled:
                    job.reason = "claim_cancelled"
                else:
                    try:
                        job.result = self._views._claim_from_signature_job(job.context, self._pool, notice.handle)
                    except ClaimError as error:
                        job.reason = error.reason_code
                    except Exception:
                        job.reason = "claim_unavailable"
                # Publish the bounded reply before releasing returned inputs.
                # Any interruption/fault fences the coordinator without replay.
                job.state = "ready"
                self._pool.discard(notice.handle)
                del self._signatures[notice.handle]
                job.signature_handle = None
            if not self._closing:
                for _ in range(self._pool._limits.max_workers):
                    if self._pool.offer_next() is None:
                        break
        except BaseException:
            self._faulted = True
            raise

    def take(self, ticket):
        self._guard()
        job = self._job(ticket)
        if job.state != "ready":
            return None
        reply = ClaimJobReply(job.context, None if job.result is None else dict(job.result), job.reason)
        try:
            self._drop(ticket)
        except BaseException:
            self._faulted = True
            raise
        return reply

    def cancel(self, ticket):
        self._guard()
        job = self._job(ticket)
        if job.state == "ready":
            return "complete"
        if job.state != "pending":
            raise ClaimError("claim_unavailable")
        try:
            job.cancelled = True
            state = self._pool.cancel(job.signature_handle)
            if state == "released":
                del self._signatures[job.signature_handle]
                job.signature_handle = None
                job.reason, job.state = "claim_cancelled", "ready"
            return state
        except BaseException:
            self._faulted = True
            raise

    def request_close(self):
        self._guard()
        self._closing = True
        for ticket in tuple(self._jobs):
            if self._jobs[ticket].state == "pending":
                self.cancel(ticket)
        self._pool.request_close()

    def snapshot(self):
        self._guard()
        return {"tickets": len(self._jobs), "projects": len(self._counts),
                "pending": sum(job.state == "pending" for job in self._jobs.values()),
                "ready": sum(job.state == "ready" for job in self._jobs.values()),
                "closing": self._closing,
                "workers_closed": self._pool.snapshot()["closed"]}
