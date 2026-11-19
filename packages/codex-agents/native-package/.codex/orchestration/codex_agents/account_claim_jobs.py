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
from .account_open_contracts import AccountOpenInput, AccountOpenContext, OpenError, OpenOwnerLimits
from .account_service import AccountOwnerViews
from .signature_job_intake import SignatureIntakeLimits, SignatureJobIntake
from .signature_worker_pool import SignatureWorkerLimits, SignatureWorkerPool
from .sql_read_budget import SQLReadLimits


def claim_anchor_ms(context):
    """A claim's capture anchor: its binding's open time (`created_at_ms`), read at preflight."""
    return context.run_context[0][5]


@dataclass(frozen=True, repr=False)
class ClaimJobReply:
    context: object
    result: dict | None
    reason: str | None
    operation: str = 'account.claim_launch'
    before_publish: object = None


@dataclass(repr=False)
class _Job:
    key: tuple[str, str, str, str]
    launch: LaunchClaimInput | AccountOpenInput
    context: object = None
    signature_handle: object = None
    state: str = "reserved"
    result: dict | None = None
    reason: str | None = None
    cancelled: bool = False
    kind: str = 'claim'
    before_commit: object = None
    provider: str | None = None
    stale_preparations: int = 0


# How many times a claim whose prepared admission went stale is captured and prepared again before it
# is refused (0.4.0 FR1): sibling claims dispatched at once add decisions that change each other's rows.
MAX_STALE_PREPARATIONS = 3


class AccountClaimJobs:
    """One owner, fixed startup configuration, explicit submit/drive/take.

    Construction and bootstrap precede the request loop. Request methods create
    no thread/process and wait for no worker. Capture and final commit still use
    legacy JSON/filesystem paths; this is not a complete owner-loop latency or
    native enrollment guarantee. Tickets, notices and replies grant no spawn.
    Bounded immutable protected-store evidence remains with the original request;
    only the account commit verifies it and consumes admission, never a worker.
    Unleased reference mode must be explicitly configured before intake.
    """

    def __init__(self, views, intake_limits, worker_limits, work_limits, *, allow_unleased_claim_reference=False,
                 open_limits=None):
        if (type(views) is not AccountOwnerViews or type(intake_limits) is not SignatureIntakeLimits
                or type(worker_limits) is not SignatureWorkerLimits or type(work_limits) is not SQLReadLimits
                or type(allow_unleased_claim_reference) is not bool
                or (open_limits is not None and type(open_limits) is not OpenOwnerLimits)):
            raise ClaimError("claim_invalid_request")
        views._guard()
        work_limits.__post_init__()
        self._views = views
        self._allow_unleased_reference = allow_unleased_claim_reference
        self._open_limits = None if open_limits is None else OpenOwnerLimits(
            open_limits.max_pending, open_limits.max_retained, open_limits.max_context_bytes)
        self._work_limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms,
                                         work_limits.max_value_bytes)
        self._intake = SignatureJobIntake(views, intake_limits)
        self._pool = SignatureWorkerPool(self._intake, worker_limits)
        self._jobs: dict[object, _Job] = {}
        self._requests: dict[tuple[str, str, str, str], object] = {}
        self._open_requests: dict[tuple[str, str, str, str], object] = {}
        self._counts: dict[str, int] = {}
        self._signatures: dict[object, object] = {}
        self._started = self._closing = self._faulted = False
        self._ingress_owner = None
        self._billing = None

    def _delivery(self, ingress):
        # A bound transport owns all subscribers to a deduplicated ticket.
        # This is internal delivery custody, never native caller authority.
        if self._ingress_owner is not None and ingress is not self._ingress_owner:
            raise ClaimError("claim_unavailable")

    def _guard(self, *, allow_faulted=False):
        self._views._guard()
        if self._faulted and not allow_faulted:
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
        del (self._open_requests if job.kind == 'open' else self._requests)[job.key]
        count = self._counts[job.key[0]]
        if count == 1:
            del self._counts[job.key[0]]
        else:
            self._counts[job.key[0]] = count - 1
        del self._jobs[ticket]

    def submit(self, project_id, *, runtime_digest, caller_scope, request_id, launch, _ingress=None,
               storage_binding=None, storage_evidence=None):
        self._guard()
        self._delivery(_ingress)
        if not self._started or self._closing:
            raise ClaimError("claim_unavailable")
        if (not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
                or not matches(caller_scope, _DIGEST) or not matches(request_id, _ID)
                or type(launch) is not LaunchClaimInput):
            raise ClaimError("claim_invalid_request")
        launch = LaunchClaimInput(launch.run_handle, launch.identity_digest)
        from .claim_storage import capture_storage_evidence

        storage_binding, storage_evidence = capture_storage_evidence(storage_binding, storage_evidence)
        key = (project_id, runtime_digest, caller_scope, request_id)
        existing = self._requests.get(key)
        if existing is not None:
            job = self._job(existing)
            if job.launch != launch:
                raise ClaimError("claim_conflict")
            if ((storage_binding is not None and (job.context is None or storage_binding != job.context.storage_binding))
                    or (storage_evidence is not None and (job.context is None or storage_evidence != job.context.storage_evidence))):
                raise ClaimError("claim_conflict")
            if job.cancelled:
                # Cancellation cannot be undone by a late duplicate. Keep its
                # quota/custody and require an explicit retry after cleanup.
                raise ClaimError("claim_busy")
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
                request_id=request_id, launch=launch, work_limits=self._work_limits,
                storage_required=not self._allow_unleased_reference, storage_binding=storage_binding,
                storage_evidence=storage_evidence,
            )
            job.context = context
            if known is not None:
                job.result, job.state = known, "ready"
            elif reason is not None:
                job.result = self._views._claim_commit_denied(context, reason, self._work_limits)
                job.state = "ready"
            else:
                # Anchored at the run's open, so the capture keeps the run's own
                # admission and the identity it cited however late the launch is.
                job.provider = provider
                job.signature_handle = self._intake.capture_admission(
                    project_id, provider, self._work_limits, claim_anchor_ms(context))
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

    def submit_open(self, project_id, *, runtime_digest, caller_scope, request_id, request, context,
                    before_commit, _ingress=None):
        """Share the original fixed worker/ticket quota with launch claims."""
        from .account_original_open import preflight_open
        self._guard()
        self._delivery(_ingress)
        if not self._started or self._closing or self._open_limits is None:
            raise OpenError('open_unavailable')
        if (not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
                or not matches(caller_scope, _DIGEST) or not matches(request_id, _ID)
                or type(request) is not AccountOpenInput or type(context) is not AccountOpenContext
                or not callable(before_commit)):
            raise OpenError('open_invalid_request')
        raw, _ = context.snapshot()
        if len(raw) > self._open_limits.max_context_bytes:
            raise OpenError('open_busy')
        key = (project_id, runtime_digest, caller_scope, request_id)
        existing = self._open_requests.get(key)
        if existing is not None:
            job = self._job(existing)
            if job.launch != request or job.context.context_bytes != raw:
                raise OpenError('open_conflict')
            if job.cancelled:
                raise OpenError('open_busy')
            before_commit()
            return existing
        limits, count = self._intake._limits, self._counts.get(project_id, 0)
        if (count >= limits.max_jobs_per_project
                or (not count and len(self._counts) >= limits.max_projects)
                or len(self._jobs) >= limits.max_projects*limits.max_jobs_per_project):
            raise OpenError('open_busy')
        ticket, job = object(), _Job(key, request, kind='open', before_commit=before_commit)
        try:
            # Reserve bounded delivery and worker metadata before any SQL write.
            self._jobs[ticket] = job
            self._open_requests[key] = ticket
            self._counts[project_id] = count+1
            captured, known = preflight_open(self._views._ledger, project_id, runtime_digest=runtime_digest,
                caller_scope=caller_scope, request_id=request_id, request=request, context=context,
                limits=self._open_limits, work_limits=self._work_limits, before_commit=before_commit)
            job.context = captured
            if known is not None:
                job.result, job.state = known, 'ready'
            else:
                from .account_original_open import _provider
                job.signature_handle = self._intake.capture_admission(project_id, _provider(captured), self._work_limits)
                self._signatures[job.signature_handle] = ticket
                job.state = 'pending'
            return ticket
        except BaseException:
            try:
                if job.signature_handle is not None:
                    if self._pool.cancel(job.signature_handle) != 'released':
                        raise OpenError('open_unavailable')
                    self._signatures.pop(job.signature_handle, None)
                self._open_requests.pop(key, None)
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
        if self._closing:
            self.drain()
            return
        try:
            for notice in self._pool.poll():
                if self._billing is not None and self._billing.owns_signature(notice.handle):
                    self._billing.finish_signature(notice.handle)
                    continue
                ticket = self._signatures[notice.handle]
                job = self._job(ticket)
                if job.state != "pending":
                    raise ClaimError("claim_unavailable")
                if self._ingress_owner is not None and not self._ingress_owner._claim_has_subscribers(ticket):
                    # The actual returned worker is stopped. Published last-
                    # subscriber loss must win before any final SQL claim; it
                    # need not wait behind the owner's ordinary request batch.
                    job.cancelled = True
                again = False
                if job.cancelled:
                    job.reason = 'open_cancelled' if job.kind == 'open' else "claim_cancelled"
                else:
                    try:
                        if job.kind == 'open':
                            from .account_original_open import finish_open_from_signature_job
                            job.result = finish_open_from_signature_job(self._views._ledger, job.context,
                                self._pool, notice.handle, before_commit=job.before_commit)
                        else:
                            job.result = self._views._claim_from_signature_job(job.context, self._pool, notice.handle)
                    except OpenError as error:
                        job.reason = error.reason_code
                    except ClaimError as error:
                        again = (error.reason_code == "claim_preparation_stale" and job.kind == 'claim'
                                 and not getattr(error, "run_changed", False)
                                 and job.provider is not None and job.stale_preparations < MAX_STALE_PREPARATIONS)
                        job.reason = None if again else error.reason_code
                    except Exception:
                        job.reason = 'open_unavailable' if job.kind == 'open' else "claim_unavailable"
                if again:
                    # Captured and prepared again from the run's open, verified in full as the first was
                    # (0.4.0 FR1); the job keeps its place, quota and identity.
                    self._pool.discard(notice.handle)
                    del self._signatures[notice.handle]
                    job.signature_handle = None
                    job.stale_preparations += 1
                    try:
                        job.signature_handle = self._intake.capture_admission(
                            job.context.project_id, job.provider, self._work_limits, claim_anchor_ms(job.context))
                    except ClaimError as error:
                        job.reason, job.state = error.reason_code, "ready"  # Its own reason, as a first capture.
                        continue
                    except Exception:
                        job.reason, job.state = "claim_unavailable", "ready"
                        continue
                    self._signatures[job.signature_handle] = ticket
                    continue
                # Publish the bounded reply before releasing returned inputs.
                # Any interruption/fault fences the coordinator without replay.
                job.state = "ready"
                self._pool.discard(notice.handle)
                del self._signatures[notice.handle]
                job.signature_handle = None
            if self._billing is not None:
                self._billing.drive()
            if not self._closing:
                for _ in range(self._pool._limits.max_workers):
                    if self._pool.offer_next() is None:
                        break
        except BaseException:
            self._faulted = True
            raise

    def take(self, ticket, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
        job = self._job(ticket)
        if job.state != "ready":
            return None
        reply = ClaimJobReply(job.context, None if job.result is None else dict(job.result), job.reason,
                              'account.open_host_run' if job.kind == 'open' else 'account.claim_launch',
                              job.before_commit if job.kind == 'open' else None)
        try:
            self._drop(ticket)
        except BaseException:
            self._faulted = True
            raise
        return reply

    def cancel(self, ticket, *, _ingress=None):
        self._guard()
        self._delivery(_ingress)
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
                job.reason, job.state = ('open_cancelled' if job.kind == 'open' else "claim_cancelled"), "ready"
            return state
        except BaseException:
            self._faulted = True
            raise

    def request_close(self):
        """Stop existing work even after a claim/publication fault.

        This fence is permanent. Shutdown does not offer, commit or replay work;
        the fixed pool keeps unknown custody and cancels its actual queued jobs.
        """
        self._guard(allow_faulted=True)
        self._closing = True
        try:
            if self._billing is not None:
                self._billing.request_close()
            self._pool.request_close()
        except BaseException:
            self._faulted = True
            raise

    def drain(self):
        """Poll cleanup only; return true after the fixed pool confirms closure.

        No metadata flag can release an unknown worker's reservation. Pending
        and ready ticket metadata remain until all owned workers actually close;
        durable receipts stay in the database for later explicit caller retries.
        """
        self._guard(allow_faulted=True)
        if not self._closing:
            raise ClaimError("claim_unavailable")
        try:
            for notice in self._pool.poll():
                if self._billing is not None and self._billing.owns_signature(notice.handle):
                    self._billing.finish_signature(notice.handle)
                else:
                    self._pool.discard(notice.handle)
            billing_drained = self._billing is None or self._billing.drained()
            if not self._pool.snapshot()["closed"] or not billing_drained:
                return False
            self._jobs.clear()
            self._requests.clear()
            self._open_requests.clear()
            self._counts.clear()
            self._signatures.clear()
            return True
        except BaseException:
            self._faulted = True
            raise

    def snapshot(self):
        self._guard(allow_faulted=True)
        return {"tickets": len(self._jobs), "projects": len(self._counts),
                "pending": sum(job.state == "pending" for job in self._jobs.values()),
                "ready": sum(job.state == "ready" for job in self._jobs.values()),
                "closing": self._closing, "faulted": self._faulted,
                "workers_closed": self._pool.snapshot()["closed"]}
