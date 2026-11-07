"""Detached admission-input parsing, never admission or signature authority.

This pure step validates a complete owner-local raw capture before decoding any
JSON. It preserves row order and every candidate, including foreign restrictive
evidence. Returned dictionaries are detached mutable data, not authenticated or
immutable decisions. No normal client or public operation uses this module.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .admission_inputs import (
    ADMISSION_CAPTURE_RECORD_TYPES, AdmissionCaptureLimits, AdmissionInputPolicy,
    AdmissionInputRow, AdmissionInputSnapshot, admission_input_size,
)
from .contracts import ContractError
from .learning_contracts import (
    MAX_RECORD_BYTES, canonical_json, parse_bounded_json, validate_learning_record,
)
from .owner_key_contracts import OWNER_SIGNER_LINE, OWNER_SIGNERS_MAX_BYTES

_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_INTEGER = (1 << 63) - 1
_REASONS = frozenset({"admission_decode_invalid_input", "admission_decode_invalid_limits", "admission_decode_limit"})


class AdmissionDecodeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in _REASONS:
            reason_code = "admission_decode_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, repr=False)
class ParsedAdmissionRow:
    """Unverified data: raw signature/source labels prove no authority."""

    raw: AdmissionInputRow
    record: dict


def _invalid() -> None:
    raise AdmissionDecodeError("admission_decode_invalid_input")


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _text_size(value: object, maximum: int) -> int:
    if type(value) is not str or not 0 < len(value) <= maximum:
        _invalid()
    failed = False
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        failed, size = True, 0
    if failed or size > maximum or "\x00" in value:
        _invalid()
    return size


def _signer(value: object) -> None:
    if value is not None and (type(value) is not bytes or not 0 < len(value) <= OWNER_SIGNERS_MAX_BYTES
                              or OWNER_SIGNER_LINE.fullmatch(value) is None):
        _invalid()


def _preflight(snapshot: AdmissionInputSnapshot, limits: AdmissionCaptureLimits) -> tuple[AdmissionInputRow, ...]:
    if type(limits) is not AdmissionCaptureLimits:
        raise AdmissionDecodeError("admission_decode_invalid_limits")
    failed = False
    try:
        limits = AdmissionCaptureLimits(limits.max_rows, limits.max_bytes)
    except (TypeError, ValueError, RuntimeError):
        failed = True
    if failed:
        raise AdmissionDecodeError("admission_decode_invalid_limits")
    if type(snapshot) is not AdmissionInputSnapshot:
        _invalid()
    # Capture exact immutable field values once. Origin is intentionally unused.
    project, provider, runtime = snapshot.project_id, snapshot.provider, snapshot.runtime_digest
    captured, created = snapshot.captured_at_ms, snapshot.project_created_at_ms
    policy, owner, signer, rows, retained = snapshot.policy, snapshot.owner, snapshot.signer, snapshot.rows, snapshot.retained_bytes
    if (not _matches(project, _PROJECT) or type(provider) is not str or provider not in {"codex", "claude"}
            or not _matches(runtime, _DIGEST) or not _integer(captured) or not _integer(created)
            or type(policy) is not AdmissionInputPolicy or type(rows) is not tuple or not _integer(retained)):
        _invalid()
    if len(rows) > limits.max_rows or retained > limits.max_bytes:
        raise AdmissionDecodeError("admission_decode_limit")
    failed = False
    try:
        policy = AdmissionInputPolicy(policy.minimum_version, policy.maximum_exclusive_version, policy.max_admissions_per_window)
    except (TypeError, ValueError, RuntimeError):
        failed = True
    if failed:
        _invalid()
    _signer(signer)
    if owner is not None:
        if (type(owner) is not tuple or len(owner) != 4 or not _integer(owner[0], 1)
                or type(owner[2]) is not str or owner[2] not in {"enrolled", "replaced"}
                or not _integer(owner[3])):
            _invalid()
        _signer(owner[1])
        if owner[1] is None:
            _invalid()
    # Charge the whole capture before hashing, parsing or copying decoded values.
    size = admission_input_size(project, provider, runtime, policy, owner, signer, ())
    previous = 0
    identifiers = set()
    for row in rows:
        if type(row) is not tuple or len(row) != 11:
            _invalid()
        rowid, scope, identifier, sequence, kind, content, digest, source, observed, ingested, signature = row
        if (not _integer(rowid, 1) or rowid <= previous or not _matches(scope, _PROJECT)
                or not _integer(sequence, 1) or type(kind) is not str or kind not in ADMISSION_CAPTURE_RECORD_TYPES
                or type(content) is not bytes or not 0 < len(content) <= MAX_RECORD_BYTES
                or not _matches(digest, _DIGEST) or not _integer(observed) or not _integer(ingested)
                or (signature is not None and (type(signature) is not bytes or len(signature) > 16_384))):
            _invalid()
        identifier_size, source_size = _text_size(identifier, 640), _text_size(source, 32)
        if (scope, identifier) in identifiers:
            _invalid()
        if kind in {"provider_identity", "billing_attestation"}:
            if scope != project:
                _invalid()
        elif source != ("provider" if kind in {"provider_billing", "provider_headroom"} else "lifecycle"):
            _invalid()
        size += 32 + len(scope) + identifier_size + len(kind) + len(content) + len(digest) + source_size
        size += 0 if signature is None else len(signature)
        if size > limits.max_bytes:
            raise AdmissionDecodeError("admission_decode_limit")
        previous = rowid
        identifiers.add((scope, identifier))
    if size != retained:
        _invalid()
    return rows


def _depth_bounded(content: bytes) -> bool:
    """A lexical nesting cap before the standard strict JSON parser allocates."""
    depth = 0
    quoted = escaped = False
    for value in content:
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value in (91, 123):
            depth += 1
            if depth > 16:
                return False
        elif value in (93, 125):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def decode_admission_inputs(
    snapshot: AdmissionInputSnapshot, limits: AdmissionCaptureLimits,
) -> tuple[ParsedAdmissionRow, ...]:
    """Parse all six closed record families without filtering or granting trust.

    Limits bound rows and retained input-field bytes, not exact Python heap or
    elapsed time. Owner signatures, current evidence/policy, native caller and
    atomic account-cap/launch checks must be established elsewhere at use.
    """
    rows = _preflight(snapshot, limits)
    result = []
    for row in rows:
        _, _, identifier, _, kind, content, digest, _, observed, _, _ = row
        if not _depth_bounded(content) or hashlib.sha256(content).hexdigest() != digest:
            _invalid()
        failed = False
        try:
            record = validate_learning_record(parse_bounded_json(content))
            if (record["record_id"] != identifier or record["record_type"] != kind
                    or record["observed_at_ms"] != observed or canonical_json(record) != content):
                failed = True
        except (ContractError, TypeError, ValueError, KeyError, RecursionError, OverflowError):
            failed = True
        if failed:
            _invalid()
        result.append(ParsedAdmissionRow(row, record))
    return tuple(result)
