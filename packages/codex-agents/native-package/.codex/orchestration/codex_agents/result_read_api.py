"""Closed, bounded reads from an already trusted result-owner endpoint.

This client imports no persistence owner, obtains no digest key and cannot publish
or acknowledge a result. Binding labels and a keyed digest do not authenticate
the endpoint or caller. Native enrollment is a separate, still closed gate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError
from .history_contracts import (
    HISTORY_REASON_CODES, AttemptBinding, HistoryError, _DIGEST, _ID, matches, valid_integer,
)
from .learning_contracts import canonical_json, parse_bounded_json
from .result_contracts import MAX_RESULT_BODY_BYTES
from .service_transport import MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

RESULT_READ_PROTOCOL_VERSION = 1
RESULT_CHUNK_BYTES = 8192
MAX_RESULT_READ_FRAME_BYTES = 16_384
RESULT_READ_OPERATION = "result.read_chunk"
_REQUEST_FIELDS = {"protocol_version", "operation", "request_id", "binding", "result_digest", "offset"}
_RESPONSE_FIELDS = {
    "protocol_version", "request_id", "request_digest", "binding", "ready", "result", "reason_code",
}
_CHUNK_FIELDS = {"result_digest", "body_size", "terminal_record_id", "expires_at_ms", "offset", "data_base64"}


def _closed(value: object, fields: set[str]) -> bool:
    return type(value) is dict and value.keys() == fields


def _identity(result_digest: str, offset: int) -> str:
    # Read identities are deterministic within the complete binding scope. A
    # different offset never reuses the same identity with changed parameters.
    if (not matches(result_digest, _DIGEST)
            or not valid_integer(offset, maximum=MAX_RESULT_BODY_BYTES - 1)
            or offset % RESULT_CHUNK_BYTES != 0):
        raise HistoryError("history_invalid_request")
    return f"result-read:{result_digest}:{offset}"


@dataclass(frozen=True)
class ResultReadRequest:
    protocol_version: int
    operation: str
    request_id: str
    binding: AttemptBinding
    result_digest: str
    offset: int

    def __post_init__(self) -> None:
        encode_result_read_request(self)


def encode_result_read_request(request: ResultReadRequest) -> bytes:
    invalid = False
    try:
        if (
            type(request) is not ResultReadRequest or type(request.protocol_version) is not int
            or request.protocol_version != RESULT_READ_PROTOCOL_VERSION
            or type(request.operation) is not str or request.operation != RESULT_READ_OPERATION
            or type(request.binding) is not AttemptBinding or not matches(request.result_digest, _DIGEST)
            or not valid_integer(request.offset, maximum=MAX_RESULT_BODY_BYTES - 1)
            or request.offset % RESULT_CHUNK_BYTES != 0
            or type(request.request_id) is not str or request.request_id != _identity(request.result_digest, request.offset)
        ):
            raise HistoryError("history_invalid_request")
        encoded = canonical_json({
            **{name: getattr(request, name) for name in _REQUEST_FIELDS if name != "binding"},
            "binding": request.binding.as_data(),
        })
        invalid = len(encoded) + 1 > MAX_RESULT_READ_FRAME_BYTES
    except (HistoryError, ContractError, AttributeError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_request")
    return encoded


def decode_result_read_request(raw: bytes) -> ResultReadRequest:
    invalid = False
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_RESULT_READ_FRAME_BYTES:
            raise HistoryError("history_invalid_request")
        value = parse_bounded_json(raw, max_bytes=MAX_RESULT_READ_FRAME_BYTES - 1)
        if not _closed(value, _REQUEST_FIELDS) or type(value["binding"]) is not dict:
            raise HistoryError("history_invalid_request")
        request = ResultReadRequest(**{**value, "binding": AttemptBinding(**value["binding"])})
    except (HistoryError, ContractError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_request")
    return request


def result_read_request(binding: AttemptBinding, result_digest: str, offset: int = 0) -> ResultReadRequest:
    return ResultReadRequest(RESULT_READ_PROTOCOL_VERSION, RESULT_READ_OPERATION,
                             _identity(result_digest, offset), binding, result_digest, offset)


def result_read_request_digest(request: ResultReadRequest) -> str:
    # This hashes only bounded read metadata, never private result bytes.
    return hashlib.sha256(encode_result_read_request(request)).hexdigest()


@dataclass(frozen=True)
class ResultChunk:
    result_digest: str
    body_size: int
    terminal_record_id: str
    expires_at_ms: int
    offset: int
    data: bytes


def decode_result_read_response(raw: bytes, request: ResultReadRequest) -> ResultChunk:
    invalid = False
    try:
        encode_result_read_request(request)
        if type(raw) is not bytes or len(raw) + 1 > MAX_RESULT_READ_FRAME_BYTES:
            raise HistoryError("history_invalid_response")
        value = parse_bounded_json(raw, max_bytes=MAX_RESULT_READ_FRAME_BYTES - 1)
        if (
            not _closed(value, _RESPONSE_FIELDS) or type(value["protocol_version"]) is not int
            or value["protocol_version"] != RESULT_READ_PROTOCOL_VERSION
            or value["request_id"] != request.request_id
            or value["request_digest"] != result_read_request_digest(request)
            or type(value["binding"]) is not dict
            or AttemptBinding(**value["binding"]).as_data() != request.binding.as_data()
            or type(value["ready"]) is not bool
        ):
            raise HistoryError("history_invalid_response")
        if not value["ready"]:
            if (value["result"] is not None or type(value["reason_code"]) is not str
                    or value["reason_code"] not in HISTORY_REASON_CODES):
                raise HistoryError("history_invalid_response")
        else:
            chunk = value["result"]
            if (
                value["reason_code"] is not None or not _closed(chunk, _CHUNK_FIELDS)
                or chunk["result_digest"] != request.result_digest
                or not valid_integer(chunk["body_size"], minimum=request.offset + 1, maximum=MAX_RESULT_BODY_BYTES)
                or not matches(chunk["terminal_record_id"], _ID) or not valid_integer(chunk["expires_at_ms"], minimum=1)
                or type(chunk["offset"]) is not int or chunk["offset"] != request.offset
                or type(chunk["data_base64"]) is not str
                or len(chunk["data_base64"]) > 4 * ((RESULT_CHUNK_BYTES + 2) // 3)
            ):
                raise HistoryError("history_invalid_response")
            data = base64.b64decode(chunk["data_base64"], validate=True)
            if (
                len(data) != min(RESULT_CHUNK_BYTES, chunk["body_size"] - request.offset)
                or base64.b64encode(data).decode("ascii") != chunk["data_base64"]
            ):
                raise HistoryError("history_invalid_response")
            result = ResultChunk(**{name: chunk[name] for name in _CHUNK_FIELDS if name != "data_base64"}, data=data)
    except (HistoryError, ContractError, AttributeError, KeyError, TypeError, ValueError,
            binascii.Error, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_response")
    if not value["ready"]:
        raise HistoryError(value["reason_code"])
    return result


class ResultReadClient:
    """Read only: at most 128 chunks and one total caller-supplied time budget.

    Whole reads return nothing on a missing, changed, expired or lost chunk. No
    partial body is persisted, no transfer receipt is durable, and no automatic
    resend, owner start, key access or database fallback occurs. The service must
    authenticate its endpoint and check the private keyed body digest; this client
    cannot independently verify that digest without the deliberately withheld key.
    """

    def __init__(self, endpoint: Path, *, timeout: float) -> None:
        if (
            not isinstance(endpoint, Path) or not endpoint.is_absolute()
            or type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise HistoryError("history_invalid_input")
        self._endpoint, self._timeout = endpoint, timeout

    def _read(self, request: ResultReadRequest, timeout: float) -> ResultChunk:
        encoded = encode_result_read_request(request)
        request = decode_result_read_request(encoded)
        failed = False
        try:
            raw = exchange_frame(self._endpoint, encoded, timeout=timeout,
                                 request_limit=MAX_RESULT_READ_FRAME_BYTES, response_limit=MAX_RESULT_READ_FRAME_BYTES)
        except ServiceTransportError:
            failed = True
        if failed:
            raise HistoryError("history_service_unavailable")
        return decode_result_read_response(raw, request)

    def read_chunk(self, binding: AttemptBinding, result_digest: str, *, offset: int = 0) -> ResultChunk:
        return self._read(result_read_request(binding, result_digest, offset), self._timeout)

    def read_result(self, binding: AttemptBinding, result_digest: str) -> bytes:
        # Capture and validate the complete scope before I/O. Canonical decoding
        # copies it so an external reference cannot mutate a later chunk's target.
        request = decode_result_read_request(encode_result_read_request(result_read_request(binding, result_digest)))
        deadline = time.monotonic() + self._timeout
        body = bytearray()
        expected = None
        for offset in range(0, MAX_RESULT_BODY_BYTES, RESULT_CHUNK_BYTES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HistoryError("history_service_unavailable")
            chunk = self._read(result_read_request(request.binding, request.result_digest, offset), remaining)
            current = (chunk.result_digest, chunk.body_size, chunk.terminal_record_id, chunk.expires_at_ms)
            if (expected is not None and current != expected) or time.monotonic() >= deadline:
                raise HistoryError("history_invalid_response" if current != expected and expected is not None
                                   else "history_service_unavailable")
            expected = current
            body.extend(chunk.data)
            if len(body) == chunk.body_size:
                return bytes(body)
        raise HistoryError("history_invalid_response")
