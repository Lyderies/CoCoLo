"""Pure public owner-key encoding checks; no persistence or signing authority.

Legacy identity/fingerprint helpers retain their existing behavior. New public
read values require one bounded canonical SSH encoding and prove no possession,
native owner role, endpoint identity or signing permission.
"""

from __future__ import annotations

import base64
import hashlib
import re

OWNER_SIGNERS_MAX_BYTES = 4096
OWNER_SIGNER_LINE = re.compile(
    rb"codex-agents-owner (?:ssh-ed25519|sk-ssh-ed25519@openssh\.com|ssh-rsa|"
    rb"ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+\n"
)

def _ssh_key_parts(blob: bytes) -> list[bytes]:
    """The length-prefixed fields of a public key blob; nothing may be left over."""

    parts: list[bytes] = []
    position = 0
    while position < len(blob):
        length = int.from_bytes(blob[position:position + 4], "big")
        end = position + 4 + length
        if position + 4 > len(blob) or end > len(blob):
            raise ValueError("truncated public key")
        parts.append(blob[position + 4:end])
        position = end
    return parts


def _require_canonical_key(key_type: str, parts: list[bytes]) -> None:
    """Refuse a blob that is not its key type's one encoding of a usable key.

    One key must have one spelling: `ssh-keygen` treats a padded RSA integer as the same
    key, so a second spelling could bring back a key the history has retired.
    """

    if not parts or parts[0] != key_type.encode("ascii"):
        raise ValueError("wrong public key type")
    if key_type == "ssh-ed25519":
        valid = len(parts) == 2 and len(parts[1]) == 32
    elif key_type == "sk-ssh-ed25519@openssh.com":
        valid = len(parts) == 3 and len(parts[1]) == 32 and bool(parts[2])
    elif key_type == "ssh-rsa":
        valid = (
            len(parts) == 3
            and all(_is_minimal_positive_integer(part) for part in parts[1:])
            and len(parts[2].lstrip(b"\0")) >= 128
        )
    elif key_type in {"ecdsa-sha2-nistp256", "sk-ecdsa-sha2-nistp256@openssh.com"}:
        valid = (
            len(parts) == (3 if key_type.startswith("ecdsa") else 4)
            and parts[1] == b"nistp256"
            and len(parts[2]) == 65
            and parts[2][0] == 4
            and all(parts[3:])
        )
    else:
        valid = False
    if not valid:
        raise ValueError("malformed public key")


def _is_minimal_positive_integer(value: bytes) -> bool:
    if not value or value[0] & 0x80 or not any(value):
        return False
    return value[0] != 0 or bool(value[1] & 0x80)


def _owner_key_identity(signer_line: bytes) -> bytes:
    """What makes two signer lines the same key, whichever way an older one was encoded."""

    fields = signer_line.split()
    try:
        parts = _ssh_key_parts(base64.b64decode(fields[2]))
    except (ValueError, IndexError):
        return signer_line
    if fields[1] == b"ssh-rsa":
        parts = [parts[0], *(part.lstrip(b"\0") for part in parts[1:])]
    return b"\0".join(len(part).to_bytes(4, "big") + part for part in parts)


def _signer_fingerprint(signer_line: bytes) -> str:
    """The SHA256 fingerprint `ssh-keygen -l` prints for a canonical signer line."""

    blob = base64.b64decode(signer_line.split()[2])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")



def checked_public_key(value: object) -> tuple[str, str]:
    """Return the exact public value and its fingerprint after bounded checks."""
    if type(value) is not str or len(value) > OWNER_SIGNERS_MAX_BYTES:
        raise ValueError("invalid owner public key")
    try:
        line = b"codex-agents-owner " + value.encode("ascii") + b"\n"
        if len(line) > OWNER_SIGNERS_MAX_BYTES or OWNER_SIGNER_LINE.fullmatch(line) is None:
            raise ValueError("invalid owner public key")
        key_type, encoded = value.split(" ")
        blob = base64.b64decode(encoded, validate=True)
        if base64.b64encode(blob).decode("ascii") != encoded:
            raise ValueError("invalid owner public key")
        _require_canonical_key(key_type, _ssh_key_parts(blob))
    except (ValueError, IndexError, OverflowError):
        raise ValueError("invalid owner public key") from None
    return value, _signer_fingerprint(line)


def owner_public_key_from_line(signer_line: object) -> tuple[str, str]:
    """Decode one canonical stored line without rewriting historical bytes."""
    if (type(signer_line) is not bytes or not 0 < len(signer_line) <= OWNER_SIGNERS_MAX_BYTES
            or OWNER_SIGNER_LINE.fullmatch(signer_line) is None):
        raise ValueError("invalid owner public key")
    return checked_public_key(signer_line[len(b"codex-agents-owner "):-1].decode("ascii"))
