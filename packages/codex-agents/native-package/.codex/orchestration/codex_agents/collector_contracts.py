"""Collector wire metadata and errors, with no persistence or I/O."""

from __future__ import annotations

from typing import Any

from .learning_contracts import MAX_RECORD_BYTES

LEDGER_SCHEMA_VERSION = 16  # PC-4: nullable probe provenance
COLLECTOR_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = MAX_RECORD_BYTES + 16_384

# Owner decision 129.3: pre-1.0 callers must use the serving runtime's identity.
COLLECTOR_MUTATING_OPERATIONS = frozenset({
    "bind", "ingest", "issue_result_repair", "register", "commit_owner_record",
    "learning_moved_if_empty", "shutdown", "shutdown_if_drained",
})
RUNTIME_UPDATE_COMMAND = "./setup.sh --update"
RUNTIME_UPGRADE_MESSAGE = (
    "This client and the collector must use the same runtime. "
    f"Update this project with: {RUNTIME_UPDATE_COMMAND}"
)


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


class CollectorError(RuntimeError):
    """Raised for bounded collector protocol failures."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class CollectorTransportError(CollectorError):
    """The local exchange failed; a server-supplied reason cannot create this type."""


class CollectorVersionError(CollectorError):
    """Raised when a healthy collector runs a different package build."""


class CollectorUpgradeRequired(CollectorVersionError):
    """Raised when the active collector is older than this installed package."""


class CollectorNewerVersionError(CollectorVersionError):
    """Raised without replacement when a newer collector owns shared state."""


class CollectorBuildConflictError(CollectorVersionError):
    """Raised without replacement for divergent builds with the same version."""


class CollectorAuthorizationError(CollectorVersionError):
    """Raised when this package is not the owner-authorized collector runtime."""


class CollectorRestartError(CollectorError):
    """Raised after a ledger operation committed but collector recovery failed."""
