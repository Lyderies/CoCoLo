"""Owner-local, unverified signature input data; no credentials or execution.

Limits bound copied field bytes per capture, not SQLite scan work, Python heap,
queued work or installed defaults. No value here proves signature validity,
native caller identity or a passing admission.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SIGNATURE_CAPTURE_ROWS = 4096
MAX_SIGNATURE_CAPTURE_BYTES = 64 * 1_048_576
SIGNATURE_CAPTURE_REASONS = frozenset({
    "signature_capture_invalid_request", "signature_capture_limit",
    "signature_capture_incompatible", "signature_capture_unavailable",
})


class SignatureCaptureError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in SIGNATURE_CAPTURE_REASONS:
            reason_code = "signature_capture_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SignatureCaptureLimits:
    """Explicit trusted configuration; never decoded from a client request."""

    max_rows: int
    max_bytes: int

    def __post_init__(self) -> None:
        if (type(self.max_rows) is not int or not 1 <= self.max_rows <= MAX_SIGNATURE_CAPTURE_ROWS
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_SIGNATURE_CAPTURE_BYTES):
            raise SignatureCaptureError("signature_capture_invalid_request")


OwnerGeneration = tuple[int, bytes, str, int]
AttestationRow = tuple[str, int, bytes, str, str, bytes | None]


def signature_input_size(
    project_id: str, provider: str, owner: OwnerGeneration | None,
    signer: bytes | None, rows: tuple[AttestationRow, ...],
) -> int:
    """Encoded field bytes plus fixed-width integers, over owner-checked data.

    Count both recorded and current signer bytes conservatively even when they
    share a value. This is not a wire format or an exact Python allocation size.
    """
    size = len(project_id.encode("utf-8")) + len(provider.encode("utf-8")) + 8
    if owner is not None:
        size += 16 + len(owner[1]) + len(owner[2].encode("utf-8"))
    if signer is not None:
        size += len(signer)
    for identifier, _, content, digest, source, signature in rows:
        size += (8 + len(identifier.encode("utf-8")) + len(content)
                 + len(digest.encode("utf-8")) + len(source.encode("utf-8")))
        if signature is not None:
            size += len(signature)
    return size


@dataclass(frozen=True, repr=False)
class SignatureInputSnapshot:
    """Immutable owner-local inputs, never an accepted verification result.

    The owner instance brands its own capture. No service accepts this value
    from JSON; its type or origin label alone grants no native authority.
    """

    origin: object
    project_id: str
    provider: str
    owner: OwnerGeneration | None
    signer: bytes | None
    rows: tuple[AttestationRow, ...]
    retained_bytes: int
