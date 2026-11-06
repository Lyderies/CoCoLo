"""Closed account read contracts/client; no persistence or native authority.

Read IDs correlate fresh views, not durable snapshots. Endpoint identity and
caller enrollment remain separate mandatory gates. No startup, retries or SQLite
fallback are performed here.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError
from .learning_contracts import canonical_json, parse_bounded_json
from .owner_key_contracts import checked_public_key
from .service_transport import MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

ACCOUNT_PROTOCOL_VERSION = 1
MAX_ACCOUNT_FRAME_BYTES = 16_384
MAX_ACCOUNT_INTEGER = (1 << 63) - 1
ACCOUNT_OPERATIONS = frozenset({"account.health", "account.project", "account.owner_key", "account.owner_public_key"})
ACCOUNT_REASON_CODES = frozenset({
    "account_invalid_request", "account_request_too_large", "account_invalid_response",
    "account_not_authorized", "account_wrong_owner", "account_unavailable",
    "account_incompatible", "account_project_not_found",
})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_REQUEST_FIELDS = {"protocol_version", "operation", "request_id", "project_id", "runtime_digest", "payload"}
_RESPONSE_FIELDS = {"protocol_version", "request_id", "project_id", "runtime_digest",
                    "request_digest", "ready", "result", "reason_code"}


class AccountError(RuntimeError):
    """Only a fixed public reason, never OS/SQL/request content."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in ACCOUNT_REASON_CODES:
            reason_code = "account_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


def matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_ACCOUNT_INTEGER


@dataclass(frozen=True)
class AccountRequest:
    protocol_version: int
    operation: str
    request_id: str
    project_id: str
    runtime_digest: str
    payload: dict

    def __post_init__(self) -> None:
        encode_account_request(self)


def _request_data(request: AccountRequest) -> dict:
    if type(request) is not AccountRequest:
        raise AccountError("account_invalid_request")
    return {name: getattr(request, name) for name in _REQUEST_FIELDS}


def _checked_request(data: object) -> None:
    if (
        type(data) is not dict or set(data) != _REQUEST_FIELDS
        or type(data["protocol_version"]) is not int or data["protocol_version"] != ACCOUNT_PROTOCOL_VERSION
        or type(data["operation"]) is not str or data["operation"] not in ACCOUNT_OPERATIONS
        or not matches(data["request_id"], _ID) or not matches(data["project_id"], _PROJECT)
        or not matches(data["runtime_digest"], _DIGEST) or type(data["payload"]) is not dict or data["payload"]
    ):
        raise AccountError("account_invalid_request")


def encode_account_request(request: AccountRequest) -> bytes:
    data = _request_data(request)
    _checked_request(data)
    raw = canonical_json(data)
    if len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
        raise AccountError("account_request_too_large")
    return raw


def decode_account_request(raw: bytes) -> AccountRequest:
    if type(raw) is not bytes:
        raise AccountError("account_invalid_request")
    if len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
        raise AccountError("account_request_too_large")
    try:
        data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_FRAME_BYTES - 1)
        _checked_request(data)
        return AccountRequest(**data)
    except AccountError as exc:
        reason = exc.reason_code
    except (ContractError, TypeError, ValueError, RecursionError):
        reason = "account_invalid_request"
    raise AccountError(reason)


def account_request_digest(request: AccountRequest) -> str:
    return hashlib.sha256(encode_account_request(request)).hexdigest()


def _checked_result(result: object, request: AccountRequest) -> dict:
    if type(result) is not dict:
        raise AccountError("account_invalid_response")
    if request.operation == "account.health":
        if set(result) != {"transport_alive", "database_checked"} or result["transport_alive"] is not True or result["database_checked"] is not False:
            raise AccountError("account_invalid_response")
    elif request.operation == "account.project":
        if (
            set(result) != {"project_id", "status", "created_at_ms"}
            or result["project_id"] != request.project_id or result["status"] != "active"
            or not integer(result["created_at_ms"])
        ):
            raise AccountError("account_invalid_response")
    else:
        owner_fields = {"state", "fingerprint", "sequence", "replacements", "last_replaced_at_ms"}
        if request.operation == "account.owner_public_key":
            owner_fields.add("public_key")
        if (
            set(result) != owner_fields
            or type(result["state"]) is not str or result["state"] not in {"ok", "mismatch", "not_configured"}
            or not integer(result["sequence"]) or not integer(result["replacements"])
            or result["replacements"] > result["sequence"]
            or (result["fingerprint"] is not None and not matches(result["fingerprint"], _FINGERPRINT))
            or (result["last_replaced_at_ms"] is not None and not integer(result["last_replaced_at_ms"]))
            or (result["replacements"] == 0) != (result["last_replaced_at_ms"] is None)
            or (result["sequence"] == 0) != (result["fingerprint"] is None)
            or (result["state"] == "ok" and result["sequence"] == 0)
            or (result["state"] == "not_configured" and result["sequence"] != 0)
        ):
            raise AccountError("account_invalid_response")
        if request.operation == "account.owner_public_key":
            if result["state"] == "ok":
                invalid = False
                try:
                    _, fingerprint = checked_public_key(result["public_key"])
                    invalid = fingerprint != result["fingerprint"]
                except ValueError:
                    invalid = True
                if invalid:
                    raise AccountError("account_invalid_response")
            elif result["public_key"] is not None:
                raise AccountError("account_invalid_response")
    return result


def decode_account_response(raw: bytes, request: AccountRequest) -> dict:
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
            raise AccountError("account_invalid_response")
        data = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_FRAME_BYTES - 1)
        if (
            set(data) != _RESPONSE_FIELDS or type(data["protocol_version"]) is not int
            or data["protocol_version"] != ACCOUNT_PROTOCOL_VERSION
            or data["request_id"] != request.request_id or data["project_id"] != request.project_id
            or data["runtime_digest"] != request.runtime_digest
            or data["request_digest"] != account_request_digest(request) or type(data["ready"]) is not bool
        ):
            raise AccountError("account_invalid_response")
        if data["ready"]:
            if data["reason_code"] is not None:
                raise AccountError("account_invalid_response")
            return _checked_result(data["result"], request)
        if data["result"] is not None or type(data["reason_code"]) is not str or data["reason_code"] not in ACCOUNT_REASON_CODES:
            raise AccountError("account_invalid_response")
        reason = data["reason_code"]
    except (AccountError, ContractError, TypeError, ValueError, RecursionError):
        reason = "account_invalid_response"
    raise AccountError(reason)


class AccountClient:
    """One bounded exchange per view; no database import, fallback or startup."""

    def __init__(self, socket_path: Path, project_id: str, runtime_digest: str, *, timeout: float = 1.0) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or type(timeout) not in {int, float} or not 0 < timeout <= MAX_TIMEOUT_SECONDS
            or not math.isfinite(timeout)
        ):
            raise AccountError("account_invalid_request")
        self._path, self._project, self._runtime, self._timeout = socket_path, project_id, runtime_digest, timeout

    def _read(self, operation: str, request_id: str | None) -> dict:
        identity = "read-" + secrets.token_hex(16) if request_id is None else request_id
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, operation, identity, self._project, self._runtime, {})
        raw = encode_account_request(request)
        sent = decode_account_request(raw)
        try:
            reply = exchange_frame(self._path, raw, timeout=self._timeout, response_limit=MAX_ACCOUNT_FRAME_BYTES)
        except ServiceTransportError:
            failed = True
        else:
            failed = False
        if failed:
            raise AccountError("account_unavailable")
        return decode_account_response(reply, sent)

    def health(self, *, request_id: str | None = None) -> dict:
        return self._read("account.health", request_id)

    def project(self, *, request_id: str | None = None) -> dict:
        return self._read("account.project", request_id)

    def owner_key(self, *, request_id: str | None = None) -> dict:
        return self._read("account.owner_key", request_id)

    def owner_public_key(self, *, request_id: str | None = None) -> dict:
        return self._read("account.owner_public_key", request_id)
