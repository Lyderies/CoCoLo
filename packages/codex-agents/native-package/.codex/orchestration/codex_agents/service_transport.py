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
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(remaining())
            client.connect(str(socket_path))
            client.settimeout(remaining())
            client.sendall(message + b"\n")
            raw = bytearray()
            while b"\n" not in raw:
                client.settimeout(remaining())
                chunk = client.recv(min(8192, response_limit + 1 - len(raw)))
                remaining()
                if not chunk:
                    break
                raw.extend(chunk)
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
