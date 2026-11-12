"""One bounded local metadata exchange, with no retry or persistence fallback.

The caller supplies a trusted endpoint and operation-specific limits. This module
does not discover endpoints, authenticate peers, parse authority or launch work.
New privileged services still need independently reviewed enrollment and schemas.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

MAX_FRAME_BYTES = 81_920
MAX_TIMEOUT_SECONDS = 60
REASON_CODES = frozenset({
    "invalid_request", "request_too_large", "response_too_large",
    "deadline_exceeded", "transport_unavailable",
})


class ServiceTransportError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in REASON_CODES:
            reason_code = "invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


_ENROLLMENTS: dict = {}


def install_enrollment(provider, *, owner: str = "client") -> None:
    """Set one owner's enrollment: ``provider(socket_path, message)`` returns the
    one-line preamble for an endpoint it enrolled, or None for any other.

    The checked CLI entry installs the client's; a service host that calls
    another service installs its own under a different owner. None removes it.
    The preamble is sent once, ahead of the request it names; this module
    still authenticates nothing by itself.
    """

    if (provider is not None and not callable(provider)) or type(owner) is not str:
        raise ServiceTransportError("invalid_request")
    if provider is None:
        _ENROLLMENTS.pop(owner, None)
    else:
        _ENROLLMENTS[owner] = provider


def _preamble(socket_path, message):
    """The first enrolled owner's (preamble, withdraw) for this endpoint, or (None, None).

    A provider may return the preamble alone, or with a callable that withdraws
    its single-use proof if the request is never delivered.
    """
    for provider in tuple(_ENROLLMENTS.values()):
        value = provider(socket_path, message)
        if value is None:
            continue
        if type(value) is tuple:
            line, withdraw = value
            if withdraw is not None and not callable(withdraw):
                raise ValueError()
            return line, withdraw
        return value, None
    return None, None


def exchange_frame(
    socket_path: Path,
    message: bytes,
    *,
    timeout: float,
    request_limit: int = MAX_FRAME_BYTES,
    response_limit: int = 16_384,
) -> bytes:
    """Send once and return the first newline frame, or bytes before EOF.

    Limits include the newline delimiter. EOF compatibility is retained for the
    existing collector; the operation codec must reject an incomplete message.
    A timeout or lost reply never proves that the server did not commit.
    """

    if (
        type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        or type(request_limit) is not int or not 1 <= request_limit <= MAX_FRAME_BYTES
        or type(response_limit) is not int or not 1 <= response_limit <= MAX_FRAME_BYTES
        or type(message) is not bytes or not message or b"\n" in message
    ):
        raise ServiceTransportError("invalid_request")
    if len(message) + 1 > request_limit:
        raise ServiceTransportError("request_too_large")
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ServiceTransportError("deadline_exceeded")
        return value

    reason = None
    response = b""
    withdraw = None
    try:
        preamble, withdraw = _preamble(socket_path, message)
        if preamble is not None and (type(preamble) is not bytes or not preamble or b"\n" in preamble):
            raise ValueError()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(remaining())
            try:
                client.connect(str(socket_path))
                client.settimeout(remaining())
                client.sendall((b"" if preamble is None else preamble + b"\n") + message + b"\n")
            except BaseException:
                # Not delivered: leave no proof behind for anyone else to spend.
                if withdraw is not None:
                    withdraw()
                raise
            raw = bytearray()
            while b"\n" not in raw:
                client.settimeout(remaining())
                chunk = client.recv(min(8192, response_limit + 1 - len(raw)))
                remaining()
                if not chunk:
                    break
                raw.extend(chunk)
                newline = raw.find(b"\n")
                if newline != -1:
                    if newline + 1 > response_limit:
                        raise ServiceTransportError("response_too_large")
                    del raw[newline + 1:]
                    break
                if len(raw) > response_limit:
                    raise ServiceTransportError("response_too_large")
            response = bytes(raw).split(b"\n", 1)[0]
    except ServiceTransportError as exc:
        reason = exc.reason_code
    except TimeoutError:
        reason = "deadline_exceeded"
    except (OSError, ValueError):
        reason = "transport_unavailable"
    if reason is not None:
        # The OS exception may name a private endpoint. Do not chain it into a
        # public diagnostic, and never resend an ambiguously delivered request.
        raise ServiceTransportError(reason)
    return response
