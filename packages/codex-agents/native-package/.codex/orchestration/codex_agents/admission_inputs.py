"""Unverified owner-local admission input data, never launch authority.

Explicit limits bound returned rows and copied field bytes. They do not bound
OS latency, exact Python heap, queued work or arbitrary caller objects. No
native request accepts these values; fresh atomic owner checks remain required.
"""

from __future__ import annotations

from dataclasses import dataclass

from .learning_contracts import parse_semver
from .signature_inputs import OwnerGeneration

MAX_ADMISSION_CAPTURE_ROWS = 4096
MAX_ADMISSION_CAPTURE_BYTES = 64 * 1_048_576
# S14a measured maximum: 2.612 decoded-content bytes per admission charge byte.
# Three adds 14.9% margin; applied to the caller's limit, not the global ceiling.
ADMISSION_DECODED_CONTENT_MULTIPLE = 3
ADMISSION_CAPTURE_RECORD_TYPES = frozenset({
    "provider_identity", "billing_attestation", "provider_billing",
    "provider_headroom", "admission_decision", "probe_admission",
})
ADMISSION_CAPTURE_REASONS = frozenset({
    "admission_capture_invalid_request", "admission_capture_limit",
    "admission_capture_incompatible", "admission_capture_unavailable", "admission_capture_work_limit",
})
# What a client is told: the history admission must read did not fit in one bounded
# check. It is not an account or client problem, and nothing was admitted.
ADMISSION_CAPTURE_PUBLIC_REASONS = frozenset({"admission_capture_limit", "admission_capture_work_limit"})


def public_capture_reason(error: BaseException) -> str:
    """The reason a client may see for a failed admission capture."""
    reason = getattr(error, "reason_code", None)
    if reason in ADMISSION_CAPTURE_PUBLIC_REASONS:
        return reason
    if reason == "admission_capture_incompatible":
        return "account_incompatible"
    return "account_unavailable"


class AdmissionCaptureError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in ADMISSION_CAPTURE_REASONS:
            reason_code = "admission_capture_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class AdmissionCaptureLimits:
    """Trusted owner configuration, never client-selected or installed defaults."""

    max_rows: int
    max_bytes: int

    def __post_init__(self) -> None:
        if (type(self.max_rows) is not int or not 1 <= self.max_rows <= MAX_ADMISSION_CAPTURE_ROWS
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_ADMISSION_CAPTURE_BYTES):
            raise AdmissionCaptureError("admission_capture_invalid_request")


@dataclass(frozen=True)
class AdmissionInputPolicy:
    """A bounded copy of trusted admission settings, not proof of their origin."""

    minimum_version: str
    maximum_exclusive_version: str
    max_admissions_per_window: int | None

    def __post_init__(self) -> None:
        if (type(self.minimum_version) is not str or type(self.maximum_exclusive_version) is not str
                or not 1 <= len(self.minimum_version) <= 32
                or not 1 <= len(self.maximum_exclusive_version) <= 32):
            raise AdmissionCaptureError("admission_capture_invalid_request")
        minimum = parse_semver(self.minimum_version)
        maximum = parse_semver(self.maximum_exclusive_version)
        cap = self.max_admissions_per_window
        if minimum is None or maximum is None or minimum >= maximum or (
            cap is not None and (type(cap) is not int or not 1 <= cap < 2 ** 63)
        ):
            raise AdmissionCaptureError("admission_capture_invalid_request")


# rowid, project, record id, sequence, type, canonical content, digest, source,
# observation time, ingestion time, owner signature. Content is unparsed and may
# use admission_capture_content's lossless owner-local packing. The digest always
# names the original canonical record. This is never a persisted/wire format.
AdmissionInputRow = tuple[int, str, str, int, str, bytes, str, str, int, int, bytes | None]


def admission_input_size(
    project_id: str, provider: str, runtime_digest: str, policy: AdmissionInputPolicy,
    owner: OwnerGeneration | None, signer: bytes | None, rows: tuple[AdmissionInputRow, ...],
) -> int:
    """Retained field bytes, including shared metadata copies once, not heap size.

    Owner capture pools identical project/type/source strings within one snapshot;
    the detached decoder verifies that actual sharing before accepting the charge.
    """
    size = (len(project_id.encode("utf-8")) + len(provider) + len(runtime_digest) + 24
            + len(policy.minimum_version) + len(policy.maximum_exclusive_version))
    if owner is not None:
        size += 16 + len(owner[1]) + len(owner[2])
    if signer is not None:
        size += len(signer)
    shared = set()
    for _, project, identifier, _, kind, content, digest, source, _, _, signature in rows:
        size += 32 + len(identifier.encode("utf-8")) + len(content) + len(digest)
        for value in (project, kind, source):
            if value not in shared:
                size += len(value.encode("utf-8"))
                shared.add(value)
        if signature is not None:
            size += len(signature)
    return size


@dataclass(frozen=True, repr=False)
class AdmissionInputSnapshot:
    """Immutable raw capture; its brand and labels authenticate no caller.

    History includes every candidate account/provider that can still change a
    decision at or after `anchor_ms`, conservatively charged before filtering. Consumers must decode bounded closed records and retain
    legacy family-specific ordering and local-digest citation semantics.
    """

    origin: object
    project_id: str
    provider: str
    runtime_digest: str
    captured_at_ms: int
    project_created_at_ms: int
    policy: AdmissionInputPolicy
    owner: OwnerGeneration | None
    signer: bytes | None
    rows: tuple[AdmissionInputRow, ...]
    retained_bytes: int
    # The first capture's clock: what history the scope keeps. Recaptures reuse it.
    anchor_ms: int
