"""Bounded private-helper input data, with no owner or verification authority.

The parent's actual job, process and descriptor custody remains separate. The
correlation nonce is data; snapshot origins are never serialized. No public
project/account API accepts this frame and no worker is launched by this module.
"""

from __future__ import annotations

import codecs
import re
import struct
from dataclasses import dataclass

from .learning_contracts import MAX_RECORD_BYTES
from .owner_key_contracts import OWNER_SIGNERS_MAX_BYTES
from .signature_inputs import (
    MAX_SIGNATURE_CAPTURE_BYTES, MAX_SIGNATURE_CAPTURE_ROWS, AttestationRow,
    OwnerGeneration, SignatureInputSnapshot,
)

SIGNATURE_INPUT_MAGIC = b"SIGJOB\x00\x01"
SIGNATURE_INPUT_NONCE_BYTES = 16
# Header overhead is at most 22 bytes, plus four for a current signer. Owner
# metadata and each row reduce wire size relative to retained-field accounting.
SIGNATURE_INPUT_WIRE_OVERHEAD = 26
SIGNATURE_WIRE_REASONS = frozenset({
    "signature_wire_invalid_limits", "signature_wire_invalid_input", "signature_wire_limit",
})
_MAX_INTEGER = (1 << 63) - 1
_MAX_SIGNATURE_BYTES = 16_384
_TEXT_CHUNK_CHARS = 16_384
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROVIDERS = ("codex", "claude")
_OWNER_REASONS = ("enrolled", "replaced")
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")


class SignatureWireError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in SIGNATURE_WIRE_REASONS:
            reason_code = "signature_wire_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _invalid() -> None:
    raise SignatureWireError("signature_wire_invalid_input")


def _limit() -> None:
    raise SignatureWireError("signature_wire_limit")


def _integer(value: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= _MAX_INTEGER:
        _invalid()


def _encode_text_chunk(text: str) -> bytes:
    invalid = False
    try:
        encoded = codecs.encode(text, "utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
    if invalid:
        _invalid()
    return encoded


def _decode_text(data: bytes) -> str:
    invalid = False
    try:
        text = codecs.decode(data, "utf-8", "strict")
    except UnicodeDecodeError:
        invalid = True
    if invalid:
        _invalid()
    return text


@dataclass(frozen=True)
class SignatureWireLimits:
    max_rows: int
    max_field_bytes: int

    def __post_init__(self) -> None:
        if (type(self.max_rows) is not int or not 1 <= self.max_rows <= MAX_SIGNATURE_CAPTURE_ROWS
                or type(self.max_field_bytes) is not int or not 1 <= self.max_field_bytes <= MAX_SIGNATURE_CAPTURE_BYTES):
            raise SignatureWireError("signature_wire_invalid_limits")

    @property
    def max_wire_bytes(self) -> int:
        return self.max_field_bytes + SIGNATURE_INPUT_WIRE_OVERHEAD


def _limits_copy(limits: SignatureWireLimits) -> SignatureWireLimits:
    if type(limits) is not SignatureWireLimits:
        raise SignatureWireError("signature_wire_invalid_limits")
    return SignatureWireLimits(limits.max_rows, limits.max_field_bytes)


@dataclass(frozen=True, repr=False)
class SignatureWireInputs:
    """Unverified decoded fields; no owner-origin or admission projection."""

    nonce: bytes
    project_id: str
    provider: str
    owner: OwnerGeneration | None
    signer: bytes | None
    rows: tuple[AttestationRow, ...]
    retained_bytes: int


class _Budget:
    def __init__(self, limits: SignatureWireLimits) -> None:
        self.limits = limits
        self.used = 0

    def charge(self, count: int) -> None:
        if count > self.limits.max_field_bytes - self.used:
            _limit()
        self.used += count


class _Writer(_Budget):
    def __init__(self, limits: SignatureWireLimits) -> None:
        super().__init__(limits)
        self.parts: list[bytes] = []
        self.wire_bytes = 0

    def add(self, data: bytes) -> None:
        if len(data) > self.limits.max_wire_bytes - self.wire_bytes:
            _limit()
        self.wire_bytes += len(data)
        self.parts.append(data)

    def byte(self, value: int) -> None:
        self.add(bytes((value,)))

    def integer(self, value: int, minimum: int = 0) -> None:
        _integer(value, minimum)
        self.add(_U64.pack(value))

    def blob(self, value: bytes, *, minimum: int, maximum: int) -> None:
        if type(value) is not bytes or len(value) < minimum:
            _invalid()
        if len(value) > maximum:
            _limit()
        self.charge(len(value))
        self.add(_U32.pack(len(value)))
        self.add(value)

    def text(self, value: str) -> None:
        if type(value) is not str:
            _invalid()
        if len(value) > self.limits.max_field_bytes - self.used:
            _limit()
        length_index = len(self.parts)
        self.add(b"\x00" * 4)
        length = 0
        for start in range(0, len(value), _TEXT_CHUNK_CHARS):
            chunk = _encode_text_chunk(value[start:start + _TEXT_CHUNK_CHARS])
            self.charge(len(chunk))
            self.add(chunk)
            length += len(chunk)
        self.parts[length_index] = _U32.pack(length)


class _Reader(_Budget):
    def __init__(self, raw: bytes, limits: SignatureWireLimits) -> None:
        super().__init__(limits)
        self.raw = raw
        self.position = 0

    def take(self, count: int) -> bytes:
        end = self.position + count
        if end > len(self.raw):
            _invalid()
        value = self.raw[self.position:end]
        self.position = end
        return value

    def byte(self) -> int:
        return self.take(1)[0]

    def flag(self) -> bool:
        value = self.byte()
        if value not in (0, 1):
            _invalid()
        return value == 1

    def count(self) -> int:
        return _U32.unpack(self.take(4))[0]

    def integer(self, minimum: int = 0) -> int:
        value = _U64.unpack(self.take(8))[0]
        _integer(value, minimum)
        return value

    def field(self, *, minimum: int, maximum: int) -> bytes:
        length = self.count()
        if length < minimum:
            _invalid()
        if length > maximum:
            _limit()
        self.charge(length)
        return self.take(length)

    def text(self, maximum: int | None = None) -> str:
        bound = self.limits.max_field_bytes if maximum is None else maximum
        return _decode_text(self.field(minimum=0, maximum=bound))


def encode_signature_inputs(
    inputs: SignatureInputSnapshot, nonce: bytes, limits: SignatureWireLimits,
) -> bytes:
    """Encode unverified fields; an origin object confers no wire authority."""

    limits = _limits_copy(limits)
    if type(inputs) is not SignatureInputSnapshot or type(nonce) is not bytes or len(nonce) != SIGNATURE_INPUT_NONCE_BYTES:
        _invalid()
    project, provider = inputs.project_id, inputs.provider
    owner, signer, rows, retained = inputs.owner, inputs.signer, inputs.rows, inputs.retained_bytes
    if (type(project) is not str or _PROJECT.fullmatch(project) is None
            or type(provider) is not str or provider not in _PROVIDERS
            or type(rows) is not tuple):
        _invalid()
    if len(rows) > limits.max_rows:
        _limit()
    if type(retained) is not int or retained < 1:
        _invalid()
    writer = _Writer(limits)
    writer.add(SIGNATURE_INPUT_MAGIC + nonce)
    writer.charge(8 + len(provider))
    writer.text(project)
    writer.byte(_PROVIDERS.index(provider))
    writer.byte(int(owner is not None))
    if owner is not None:
        if (type(owner) is not tuple or len(owner) != 4 or type(owner[2]) is not str
                or owner[2] not in _OWNER_REASONS):
            _invalid()
        writer.charge(16 + len(owner[2]))
        writer.integer(owner[0], 1)
        writer.blob(owner[1], minimum=1, maximum=OWNER_SIGNERS_MAX_BYTES)
        writer.byte(_OWNER_REASONS.index(owner[2]))
        writer.integer(owner[3])
    writer.byte(int(signer is not None))
    if signer is not None:
        writer.blob(signer, minimum=1, maximum=OWNER_SIGNERS_MAX_BYTES)
    writer.add(_U32.pack(len(rows)))
    for row in rows:
        if type(row) is not tuple or len(row) != 6 or type(row[3]) is not str or _DIGEST.fullmatch(row[3]) is None:
            _invalid()
        writer.charge(8 + 64)
        writer.text(row[0])
        writer.integer(row[1], 1)
        writer.blob(row[2], minimum=1, maximum=MAX_RECORD_BYTES)
        writer.add(bytes.fromhex(row[3]))
        writer.text(row[4])
        writer.byte(int(row[5] is not None))
        if row[5] is not None:
            writer.blob(row[5], minimum=0, maximum=_MAX_SIGNATURE_BYTES)
    if writer.used != retained:
        _invalid()
    return b"".join(writer.parts)


def decode_signature_inputs(raw: bytes, limits: SignatureWireLimits) -> SignatureWireInputs:
    """Check byte/count quotas before materializing the next field or row."""

    limits = _limits_copy(limits)
    if type(raw) is not bytes:
        _invalid()
    if len(raw) > limits.max_wire_bytes:
        _limit()
    reader = _Reader(raw, limits)
    if reader.take(len(SIGNATURE_INPUT_MAGIC)) != SIGNATURE_INPUT_MAGIC:
        _invalid()
    nonce = reader.take(SIGNATURE_INPUT_NONCE_BYTES)
    project = reader.text(36)
    if _PROJECT.fullmatch(project) is None:
        _invalid()
    provider_index = reader.byte()
    if provider_index >= len(_PROVIDERS):
        _invalid()
    provider = _PROVIDERS[provider_index]
    reader.charge(8 + len(provider))
    owner = None
    if reader.flag():
        sequence = reader.integer(1)
        line = reader.field(minimum=1, maximum=OWNER_SIGNERS_MAX_BYTES)
        reason_index = reader.byte()
        if reason_index >= len(_OWNER_REASONS):
            _invalid()
        reason = _OWNER_REASONS[reason_index]
        reader.charge(16 + len(reason))
        owner = (sequence, line, reason, reader.integer())
    signer = reader.field(minimum=1, maximum=OWNER_SIGNERS_MAX_BYTES) if reader.flag() else None
    count = reader.count()
    if count > limits.max_rows:
        _limit()
    rows = []
    for _ in range(count):
        reader.charge(8 + 64)
        identifier = reader.text()
        sequence = reader.integer(1)
        content = reader.field(minimum=1, maximum=MAX_RECORD_BYTES)
        digest = reader.take(32).hex()
        source = reader.text()
        signature = reader.field(minimum=0, maximum=_MAX_SIGNATURE_BYTES) if reader.flag() else None
        rows.append((identifier, sequence, content, digest, source, signature))
    if reader.position != len(raw):
        _invalid()
    return SignatureWireInputs(nonce, project, provider, owner, signer, tuple(rows), reader.used)
