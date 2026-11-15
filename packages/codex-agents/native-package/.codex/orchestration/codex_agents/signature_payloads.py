"""Bounded owner-record payload data for a future fixed private helper.

Input correlation is not authentication. The returned data proves neither a
signature nor actual worker custody, freshness or account admission. No service,
process, persistence, private key or I/O capability is used here.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from .learning_contracts import parse_bounded_json
from .owner_record_payload import owner_record_payload
from .signature_input_wire import (
    SignatureWireInputs, SignatureWireLimits, decode_signature_inputs,
)

SIGNATURE_PAYLOAD_MAGIC = b"SIGPAY\x00\x01"
MAX_SIGNATURE_PAYLOAD_BYTES = 256
SIGNATURE_PAYLOAD_HEADER_BYTES = 64
_ENTRY_OVERHEAD = 6
_U32 = struct.Struct(">I")
_U16 = struct.Struct(">H")
_PAYLOAD = re.compile(
    rb'\{"project_id":"prj_[0-9a-f]{32}","purpose":"authenticated_owner_record",'
    rb'"record_digest":"[0-9a-f]{64}","record_type":"'
    rb'(feedback|grader_calibration|billing_attestation)"\}'
)
_REASONS = frozenset({"signature_payload_invalid_response", "signature_payload_limit"})


class SignaturePayloadError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in _REASONS:
            reason_code = "signature_payload_invalid_response"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _invalid() -> None:
    raise SignaturePayloadError("signature_payload_invalid_response")


def _limit() -> None:
    raise SignaturePayloadError("signature_payload_limit")


def _limits_copy(limits: SignatureWireLimits) -> SignatureWireLimits:
    # The input codec also validates its own independent immutable copy.
    if type(limits) is not SignatureWireLimits:
        from .signature_input_wire import SignatureWireError

        raise SignatureWireError("signature_wire_invalid_limits")
    return SignatureWireLimits(limits.max_rows, limits.max_field_bytes)


def signature_payload_response_limit(limits: SignatureWireLimits) -> int:
    """Format byte ceiling, not exact heap accounting or an installed default."""

    limits = _limits_copy(limits)
    return SIGNATURE_PAYLOAD_HEADER_BYTES + limits.max_rows * (_ENTRY_OVERHEAD + MAX_SIGNATURE_PAYLOAD_BYTES)


def _eligible(inputs: SignatureWireInputs, index: int) -> bool:
    return (inputs.signer is not None and (inputs.owner is None or inputs.owner[1] == inputs.signer)
            and inputs.rows[index][4] == "owner" and bool(inputs.rows[index][5]))


def _check_payload(payload: bytes, project: str) -> None:
    if type(payload) is not bytes or not payload:
        _invalid()
    if len(payload) > MAX_SIGNATURE_PAYLOAD_BYTES:
        _limit()
    # This checks only the closed purpose/project/shape. The digest is calculated
    # by the actual fixed helper; this check cannot authenticate that calculation.
    if (_PAYLOAD.fullmatch(payload) is None
            or not payload.startswith(b'{"project_id":"' + project.encode("ascii") + b'",')):
        _invalid()


@dataclass(frozen=True, repr=False)
class SignaturePayloadBatch:
    """Unverified signing bytes indexed into one exact original input frame."""

    nonce: bytes
    input_digest: bytes
    row_count: int
    payloads: tuple[tuple[int, bytes], ...]


def prepare_signature_payloads(input_wire: bytes, limits: SignatureWireLimits) -> bytes:
    """Calculate legacy payloads without verifying signatures or accessing state.

    Legacy malformed record/shape failures omit the row. Unexpected programming,
    resource and I/O faults propagate; no result or passing verdict is invented.
    """

    limits = _limits_copy(limits)
    inputs = decode_signature_inputs(input_wire, limits)
    entries = []
    for index, row in enumerate(inputs.rows):
        if not _eligible(inputs, index):
            continue
        try:
            payload = owner_record_payload(inputs.project_id, parse_bounded_json(row[2]))
        except (TypeError, ValueError):
            continue
        _check_payload(payload, inputs.project_id)
        entries.append(_U32.pack(index) + _U16.pack(len(payload)) + payload)
    return (SIGNATURE_PAYLOAD_MAGIC + inputs.nonce + hashlib.sha256(input_wire).digest()
            + _U32.pack(len(inputs.rows)) + _U32.pack(len(entries)) + b"".join(entries))


class _Reader:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.position = 0

    def take(self, count: int) -> bytes:
        end = self.position + count
        if end > len(self.raw):
            _invalid()
        value = self.raw[self.position:end]
        self.position = end
        return value

    def count(self) -> int:
        return _U32.unpack(self.take(4))[0]


def decode_signature_payloads(
    raw: bytes, input_wire: bytes, limits: SignatureWireLimits,
) -> SignaturePayloadBatch:
    """Check bounded response data against the parent's exact original input.

    This authenticates no sender or computation. Accept a response only through
    separately established fixed-worker/process/descriptor custody; verify each
    signature and perform fresh bounded account checks before using its record.
    """

    limits = _limits_copy(limits)
    if type(raw) is not bytes:
        _invalid()
    if len(raw) > signature_payload_response_limit(limits):
        _limit()
    inputs = decode_signature_inputs(input_wire, limits)
    reader = _Reader(raw)
    if reader.take(len(SIGNATURE_PAYLOAD_MAGIC)) != SIGNATURE_PAYLOAD_MAGIC:
        _invalid()
    nonce, digest = reader.take(16), reader.take(32)
    if nonce != inputs.nonce or digest != hashlib.sha256(input_wire).digest():
        _invalid()
    row_count, count = reader.count(), reader.count()
    if count > limits.max_rows:
        _limit()
    if row_count != len(inputs.rows) or count > row_count:
        _invalid()
    entries = []
    previous = -1
    for _ in range(count):
        index = reader.count()
        length = _U16.unpack(reader.take(2))[0]
        if index <= previous or index >= row_count or not _eligible(inputs, index) or length == 0:
            _invalid()
        if length > MAX_SIGNATURE_PAYLOAD_BYTES:
            _limit()
        payload = reader.take(length)
        _check_payload(payload, inputs.project_id)
        entries.append((index, payload))
        previous = index
    if reader.position != len(raw):
        _invalid()
    return SignaturePayloadBatch(nonce, digest, row_count, tuple(entries))
