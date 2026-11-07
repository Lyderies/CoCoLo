"""Owner-local reservation and fair intake of unverified signature inputs.

This is in-memory preparation, not global durable capacity authority, a worker
launcher, native caller authentication or termination evidence. An actual fixed
adapter must establish worker custody and drop its input references before using
the owner-confirmation transitions. Do not activate across owner restart without
durable claims and conservative recovery. Handles and snapshots confer no proof.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .account_api import _DIGEST, _PROJECT, matches
from .account_service import AccountOwnerViews
from .learning_contracts import MAX_RECORD_BYTES
from .owner_key_contracts import OWNER_SIGNERS_MAX_BYTES
from .signature_inputs import (
    MAX_SIGNATURE_CAPTURE_BYTES, MAX_SIGNATURE_CAPTURE_ROWS, SignatureCaptureLimits,
    SignatureInputSnapshot, signature_input_size,
)
from .sql_read_budget import SQLReadError, SQLReadLimits

MAX_SIGNATURE_JOB_PROJECTS = 256
MAX_SIGNATURE_JOB_SLOTS = 4096
MAX_SIGNATURE_JOB_ROWS = 8192
MAX_SIGNATURE_JOB_BYTES = 64 * 1_048_576
SIGNATURE_JOB_REASONS = frozenset({
    "signature_job_invalid_limits", "signature_job_invalid_request",
    "signature_job_project_limit", "signature_job_project_full", "signature_job_unavailable",
    "signature_job_invalid_handle", "signature_job_invalid_transition", "signature_job_capture_incompatible",
})


class SignatureJobError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in SIGNATURE_JOB_REASONS:
            reason_code = "signature_job_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SignatureIntakeLimits:
    max_projects: int
    max_jobs_per_project: int
    rows_per_job: int
    bytes_per_job: int
    max_selected: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in (
            (self.max_projects, MAX_SIGNATURE_JOB_PROJECTS),
            (self.max_jobs_per_project, MAX_SIGNATURE_JOB_SLOTS),
            (self.rows_per_job, MAX_SIGNATURE_CAPTURE_ROWS),
            (self.bytes_per_job, MAX_SIGNATURE_CAPTURE_BYTES),
            (self.max_selected, MAX_SIGNATURE_JOB_PROJECTS),
        )):
            raise SignatureJobError("signature_job_invalid_limits")
        slots = self.max_projects * self.max_jobs_per_project
        if (slots > MAX_SIGNATURE_JOB_SLOTS or slots * self.rows_per_job > MAX_SIGNATURE_JOB_ROWS
                or slots * self.bytes_per_job > MAX_SIGNATURE_JOB_BYTES or self.max_selected > self.max_projects):
            raise SignatureJobError("signature_job_invalid_limits")


@dataclass(frozen=True)
class SignatureIntakeSnapshot:
    projects: int
    jobs: int
    reserved: int
    queued: int
    selected: int
    returned: int
    cancel_requested: int
    reserved_rows: int
    reserved_bytes: int


@dataclass(frozen=True, repr=False)
class SignatureWorkItem:
    """Unverified inputs for a future fixed adapter; selection is not start."""

    handle: object
    project_id: str
    provider: str
    inputs: SignatureInputSnapshot


@dataclass(repr=False)
class _Job:
    project_id: str
    provider: str
    state: str = "reserved"
    inputs: SignatureInputSnapshot | None = None
    cancel_requested: bool = False


class SignatureJobIntake:
    """One owner's bounded field reservations, FIFO and project rotation.

    Configuration reserves each project's full share before capture. Counts bound
    retained intake fields/rows and metadata entries, not exact process heap or
    references retained by arbitrary code. Only the actual owning process/thread
    may call this object. No method accepts a caller-supplied snapshot or callback.
    Native authorization and fixed-worker completion must be supplied elsewhere.
    """

    def __init__(self, views: AccountOwnerViews, limits: SignatureIntakeLimits) -> None:
        if type(views) is not AccountOwnerViews:
            raise SignatureJobError("signature_job_invalid_request")
        views._guard()
        if type(limits) is not SignatureIntakeLimits:
            raise SignatureJobError("signature_job_invalid_limits")
        limits.__post_init__()
        if getattr(views._ledger, "_signature_job_intake", None) is not None:
            raise SignatureJobError("signature_job_unavailable")
        self._views = views
        self._limits = SignatureIntakeLimits(
            limits.max_projects, limits.max_jobs_per_project, limits.rows_per_job,
            limits.bytes_per_job, limits.max_selected,
        )
        self._jobs: dict[object, _Job] = {}
        self._project_jobs: dict[str, int] = {}
        self._pending: dict[str, deque[object]] = {}
        self._rotation: deque[str] = deque()
        self._selected_projects: set[str] = set()
        views._ledger._signature_job_intake = self

    def _guard(self) -> None:
        self._views._guard()
        if getattr(self._views._ledger, "_signature_job_intake", None) is not self:
            raise SignatureJobError("signature_job_unavailable")

    def _job(self, handle: object) -> _Job:
        # Exact object handles have identity equality and no caller callbacks.
        if type(handle) is not object or handle not in self._jobs:
            raise SignatureJobError("signature_job_invalid_handle")
        return self._jobs[handle]

    def _reserve(self, project_id: str, provider: str) -> object:
        count = self._project_jobs.get(project_id, 0)
        if count >= self._limits.max_jobs_per_project:
            raise SignatureJobError("signature_job_project_full")
        if count == 0 and len(self._project_jobs) >= self._limits.max_projects:
            raise SignatureJobError("signature_job_project_limit")
        if len(self._jobs) >= self._limits.max_projects * self._limits.max_jobs_per_project:
            raise SignatureJobError("signature_job_unavailable")
        handle, job = object(), _Job(project_id, provider)
        try:
            self._jobs[handle] = job
            self._project_jobs[project_id] = count + 1
        except BaseException:
            self._jobs.pop(handle, None)
            if count:
                self._project_jobs[project_id] = count
            else:
                self._project_jobs.pop(project_id, None)
            raise
        return handle

    def _release(self, handle: object) -> None:
        job = self._job(handle)
        if job.state == "selected":
            raise SignatureJobError("signature_job_invalid_transition")
        # Check the pending index even after a partially completed publication.
        pending = self._pending.get(job.project_id)
        if pending is not None:
            try:
                pending.remove(handle)
            except ValueError:
                pass
            if not pending:
                del self._pending[job.project_id]
                try:
                    self._rotation.remove(job.project_id)
                except ValueError:
                    pass
        count = self._project_jobs[job.project_id]
        if count == 1:
            del self._project_jobs[job.project_id]
        else:
            self._project_jobs[job.project_id] = count - 1
        job.inputs = None
        del self._jobs[handle]

    def _validate_inputs(self, inputs: SignatureInputSnapshot, project_id: str, provider: str) -> None:
        bad = "signature_job_capture_incompatible"
        if (type(inputs) is not SignatureInputSnapshot
                or inputs.origin is not self._views._ledger._attestation_origin
                or type(inputs.project_id) is not str or inputs.project_id != project_id
                or type(inputs.provider) is not str or inputs.provider != provider
                or type(inputs.rows) is not tuple or len(inputs.rows) > self._limits.rows_per_job
                or type(inputs.retained_bytes) is not int or not 0 < inputs.retained_bytes <= self._limits.bytes_per_job):
            raise SignatureJobError(bad)
        owner, signer = inputs.owner, inputs.signer
        if owner is not None and (
            type(owner) is not tuple or len(owner) != 4 or type(owner[0]) is not int or owner[0] < 1
            or type(owner[1]) is not bytes or not 0 < len(owner[1]) <= OWNER_SIGNERS_MAX_BYTES
            or type(owner[2]) is not str or owner[2] not in {"enrolled", "replaced"}
            or type(owner[3]) is not int or owner[3] < 0
        ):
            raise SignatureJobError(bad)
        if signer is not None and (type(signer) is not bytes or not 0 < len(signer) <= OWNER_SIGNERS_MAX_BYTES):
            raise SignatureJobError(bad)
        for row in inputs.rows:
            if (type(row) is not tuple or len(row) != 6 or type(row[0]) is not str
                    or len(row[0]) > self._limits.bytes_per_job or type(row[1]) is not int or row[1] < 1
                    or type(row[2]) is not bytes or not 0 < len(row[2]) <= MAX_RECORD_BYTES
                    or not matches(row[3], _DIGEST) or type(row[4]) is not str
                    or len(row[4]) > self._limits.bytes_per_job
                    or (row[5] is not None and (type(row[5]) is not bytes or len(row[5]) > 16_384))):
                raise SignatureJobError(bad)
        incompatible = False
        try:
            size = signature_input_size(project_id, provider, owner, signer, inputs.rows)
        except UnicodeEncodeError:
            incompatible = True
        else:
            incompatible = size != inputs.retained_bytes or size > self._limits.bytes_per_job
        if incompatible:
            raise SignatureJobError(bad)

    def _publish(self, handle: object, inputs: SignatureInputSnapshot) -> None:
        job = self._job(handle)
        pending = self._pending.get(job.project_id)
        if pending is None:
            pending = deque()
            self._pending[job.project_id] = pending
            self._rotation.append(job.project_id)
        pending.append(handle)
        job.inputs, job.state = inputs, "queued"

    def capture(self, project_id: str, provider: str, work_limits: SQLReadLimits) -> object:
        self._guard()
        if not matches(project_id, _PROJECT) or type(provider) is not str or provider not in {"codex", "claude"}:
            raise SignatureJobError("signature_job_invalid_request")
        if type(work_limits) is not SQLReadLimits:
            raise SignatureJobError("signature_job_invalid_request")
        invalid_limits = False
        try:
            work_limits.__post_init__()
        except SQLReadError:
            invalid_limits = True
        if invalid_limits:
            raise SignatureJobError("signature_job_invalid_request")
        handle = self._reserve(project_id, provider)
        try:
            inputs = self._views.capture_signature_inputs(
                project_id, provider,
                SignatureCaptureLimits(self._limits.rows_per_job, self._limits.bytes_per_job), work_limits,
            )
            self._validate_inputs(inputs, project_id, provider)
            self._publish(handle, inputs)
            return handle
        except BaseException:
            self._release(handle)
            raise

    def select_next(self) -> SignatureWorkItem | None:
        self._guard()
        if getattr(self._views._ledger._connection, "_sql_unusable", False) is True:
            raise SignatureJobError("signature_job_unavailable")
        if len(self._selected_projects) >= self._limits.max_selected:
            return None
        for _ in range(len(self._rotation)):
            project_id = self._rotation.popleft()
            if project_id in self._selected_projects:
                self._rotation.append(project_id)
                continue
            pending = self._pending[project_id]
            handle = pending.popleft()
            if pending:
                self._rotation.append(project_id)
            else:
                del self._pending[project_id]
            job = self._job(handle)
            if job.state != "queued" or job.inputs is None:
                raise SignatureJobError("signature_job_unavailable")
            job.state = "selected"
            self._selected_projects.add(project_id)
            return SignatureWorkItem(handle, project_id, job.provider, job.inputs)
        return None

    def cancel(self, handle: object) -> str:
        """Return metadata only; a selected job still needs owner confirmation."""
        self._guard()
        job = self._job(handle)
        if job.state == "selected":
            job.cancel_requested = True
            return "pending_confirmation"
        self._release(handle)
        return "released"

    def confirm_no_worker_started(self, handle: object) -> None:
        """Trusted adapter assertion, not evidence produced by this object."""
        self._guard()
        job = self._job(handle)
        if job.state != "selected" or job.project_id not in self._selected_projects:
            raise SignatureJobError("signature_job_invalid_transition")
        self._selected_projects.remove(job.project_id)
        job.state = "returned"
        self._release(handle)

    def confirm_worker_stopped(self, handle: object, *, retain_cancelled: bool = False) -> bool:
        """Keep inputs until consumed, or release after an acknowledged cancel.

        A publishing adapter retains cancelled input through publication and
        explicit result consumption; otherwise a later publication fault could
        lose its reservation. The default preserves the original owner contract.
        The fixed adapter must establish the exact worker's end and drop
        its input references first. A handle/type/flag is not termination proof.
        """
        self._guard()
        if type(retain_cancelled) is not bool:
            raise SignatureJobError("signature_job_invalid_transition")
        job = self._job(handle)
        if job.state != "selected" or job.project_id not in self._selected_projects:
            raise SignatureJobError("signature_job_invalid_transition")
        self._selected_projects.remove(job.project_id)
        job.state = "returned"
        if job.cancel_requested and not retain_cancelled:
            self._release(handle)
            return True
        return False

    def returned_inputs(self, handle: object) -> SignatureInputSnapshot:
        """Unverified data for a future bounded final check; still reserved."""
        self._guard()
        job = self._job(handle)
        if job.state != "returned" or job.inputs is None:
            raise SignatureJobError("signature_job_invalid_transition")
        return job.inputs

    def discard_returned(self, handle: object) -> None:
        self._guard()
        if self._job(handle).state != "returned":
            raise SignatureJobError("signature_job_invalid_transition")
        self._release(handle)

    def snapshot(self) -> SignatureIntakeSnapshot:
        self._guard()
        states = [job.state for job in self._jobs.values()]
        jobs = len(states)
        return SignatureIntakeSnapshot(
            len(self._project_jobs), jobs, states.count("reserved"), states.count("queued"),
            states.count("selected"), states.count("returned"),
            sum(job.cancel_requested for job in self._jobs.values()),
            jobs * self._limits.rows_per_job, jobs * self._limits.bytes_per_job,
        )
