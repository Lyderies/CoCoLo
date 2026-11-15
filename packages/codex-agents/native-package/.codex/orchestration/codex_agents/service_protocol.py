"""Closed wire contracts for the first project-service control operations.

This module performs no I/O and grants no authority. All caller/session/runtime
fields are claims to be matched against independently authenticated server state.
There is deliberately no dispatcher, enrollment or persistence fallback here.
Account operations and result-body transfer require their own closed contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_CONTROL_REQUEST_BYTES = 65_536
MAX_GENERATION = (1 << 63) - 1
MAX_ATTEMPT = (1 << 31) - 1
OPERATIONS = frozenset({"project.status", "project.cancel"})
REASON_CODES = frozenset({
    "request_too_large", "invalid_encoding", "invalid_json", "duplicate_fields",
    "invalid_envelope", "unsupported_version", "unsupported_operation", "invalid_target",
})
_FIELDS = frozenset({
    "protocol_version", "operation", "request_id", "project_id", "primary_session_id",
    "supervisor_instance_id", "supervisor_generation", "runtime_digest", "target",
})
_TARGET_FIELDS = frozenset({"orchestration_handle", "run_handle", "attempt"})
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_ORCHESTRATION = re.compile(r"orc_[0-9a-f]{48}")
_RUN = re.compile(r"hrn_[0-9a-f]{48}")


class ServiceProtocolError(ValueError):
    """A fixed public code, never rejected fields, values or parser messages."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in REASON_CODES:
            reason_code = "invalid_envelope"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class ServiceTarget:
    orchestration_handle: str
    run_handle: str | None
    attempt: int | None

    def __post_init__(self) -> None:
        _validate_target({name: getattr(self, name) for name in _TARGET_FIELDS})


@dataclass(frozen=True)
class ServiceRequest:
    protocol_version: int
    operation: str
    request_id: str
    project_id: str
    primary_session_id: str
    supervisor_instance_id: str
    supervisor_generation: int
    runtime_digest: str
    target: ServiceTarget

    def __post_init__(self) -> None:
        _validate_fields(_request_data(self))


def _matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _positive_integer(value: Any, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _validate_target(target: Any) -> None:
    if not isinstance(target, dict) or target.keys() != _TARGET_FIELDS:
        raise ServiceProtocolError("invalid_target")
    if not _matches(target["orchestration_handle"], _ORCHESTRATION):
        raise ServiceProtocolError("invalid_target")
    if target["run_handle"] is None:
        if target["attempt"] is not None:
            raise ServiceProtocolError("invalid_target")
    elif not _matches(target["run_handle"], _RUN) or not _positive_integer(target["attempt"], MAX_ATTEMPT):
        raise ServiceProtocolError("invalid_target")


def _validate_fields(data: Any) -> None:
    if not isinstance(data, dict) or data.keys() != _FIELDS:
        raise ServiceProtocolError("invalid_envelope")
    if type(data["protocol_version"]) is not int or data["protocol_version"] != PROTOCOL_VERSION:
        raise ServiceProtocolError("unsupported_version")
    operation = data["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise ServiceProtocolError("unsupported_operation")
    for name in ("request_id", "primary_session_id", "supervisor_instance_id"):
        if not _matches(data[name], _OPAQUE_ID):
            raise ServiceProtocolError("invalid_envelope")
    if not _matches(data["project_id"], _PROJECT):
        raise ServiceProtocolError("invalid_envelope")
    if not _matches(data["runtime_digest"], _DIGEST):
        raise ServiceProtocolError("invalid_envelope")
    if not _positive_integer(data["supervisor_generation"], MAX_GENERATION):
        raise ServiceProtocolError("invalid_envelope")
    target = data["target"]
    _validate_target(target)
    if target["run_handle"] is None and operation != "project.status":
        raise ServiceProtocolError("invalid_target")


def _request_data(request: ServiceRequest) -> dict[str, Any]:
    if type(request) is not ServiceRequest:
        raise ServiceProtocolError("invalid_envelope")
    incomplete = False
    try:
        if type(request.target) is not ServiceTarget:
            raise ServiceProtocolError("invalid_envelope")
        data = {
            **{name: getattr(request, name) for name in _FIELDS if name != "target"},
            "target": {name: getattr(request.target, name) for name in _TARGET_FIELDS},
        }
    except AttributeError:
        incomplete = True
    if incomplete:
        raise ServiceProtocolError("invalid_envelope")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ServiceProtocolError("duplicate_fields")
        value[key] = item
    return value


def _reject_constant(_value: str) -> Any:
    raise ServiceProtocolError("invalid_json")


def decode_service_request(payload: bytes) -> ServiceRequest:
    """Decode one complete bounded JSON message, without trusting its claims.

    Framing, slow-sender deadlines and authenticated caller context belong to the
    transport/server. A valid request is never proof of admission or ownership.
    """

    if type(payload) is not bytes:
        raise ServiceProtocolError("invalid_envelope")
    if len(payload) > MAX_CONTROL_REQUEST_BYTES:
        raise ServiceProtocolError("request_too_large")
    reason = None
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        reason = "invalid_encoding"
    if reason is not None:
        # Raise after leaving the handler: no UnicodeError.object or parser
        # exception containing rejected input is chained onto the public error.
        raise ServiceProtocolError(reason)
    try:
        data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ServiceProtocolError as exc:
        reason = exc.reason_code
    except (ValueError, RecursionError):
        reason = "invalid_json"
    if reason is not None:
        raise ServiceProtocolError(reason)
    _validate_fields(data)
    return ServiceRequest(**{**data, "target": ServiceTarget(**data["target"])})


def canonical_request_bytes(request: ServiceRequest) -> bytes:
    """Encode only the closed request, including its complete binding scope.

    Validate directly constructed dataclasses too. The result can be used for
    request-conflict detection; it is neither a signature nor a capability.
    """

    data = _request_data(request)
    _validate_fields(data)
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(encoded) > MAX_CONTROL_REQUEST_BYTES:
        raise ServiceProtocolError("request_too_large")
    return encoded


def service_request_digest(request: ServiceRequest) -> str:
    """Hash canonical parameters; services must separately authenticate and scope IDs."""

    return hashlib.sha256(canonical_request_bytes(request)).hexdigest()
