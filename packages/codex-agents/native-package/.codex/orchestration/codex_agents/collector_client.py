"""Bounded collector client and local runtime identity; no SQLite access.

The caller supplies the endpoint. This preserves the existing collector protocol;
it does not authenticate a caller or enable the new project/account service APIs.
"""

from __future__ import annotations

import base64

import json
import re
from pathlib import Path
from typing import Any

from .collector_contracts import (
    COLLECTOR_PROTOCOL_VERSION,
    LEDGER_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    CollectorBuildConflictError,
    CollectorError,
    CollectorTransportError,
    CollectorNewerVersionError,
    CollectorUpgradeRequired,
    _version_tuple,
)
from .learning_contracts import canonical_json
from .learning_adapters import HOOK_EVENTS
from .runtime_files import RUNTIME_FILES, runtime_digest
from .service_transport import ServiceTransportError, exchange_frame


def _collector_runtime_digest() -> str:
    """The digest of every runtime file this process runs from (`RUNTIME_FILES`)."""

    orchestration_root = Path(__file__).resolve().parent.parent
    return runtime_digest(
        {relative: (orchestration_root / relative).read_bytes() for relative in RUNTIME_FILES}
    )


def _package_version() -> str:
    bundle_root = Path(__file__).resolve().parent.parent.parent.parent
    manifest_path = bundle_root / ".codex" / "orchestration" / "install-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("installed package manifest is invalid") from exc
        value = manifest.get("version") if isinstance(manifest, dict) else None
    else:
        value = (bundle_root / "VERSION").read_text(encoding="ascii").strip()
    if not isinstance(value, str):
        raise RuntimeError("package version is unavailable")
    if len(value.split(".")) != 3 or not all(
        part.isdigit() for part in value.split(".")
    ):
        raise RuntimeError("package VERSION must be a three-part numeric version")
    return value


COLLECTOR_RUNTIME_DIGEST = _collector_runtime_digest()
PACKAGE_VERSION = _package_version()


HOOK_EVENT_CODES = tuple(value[0] for value in HOOK_EVENTS.values())


def empty_ingestion_snapshot(*, registered: bool) -> dict[str, Any]:
    """Return the closed aggregate shape without exposing project or record identity."""

    return {
        "registered_project_state": "active" if registered else "unregistered",
        "ingestion_state": "active" if registered else "inactive",
        "accepted_hook_event_count": 0,
        "last_accepted_hook_event_at_ms": None,
        "rejected_hook_event_count": 0,
        "last_rejected_hook_event_at_ms": None,
        "freshness": {
            "state": (
                "awaiting_hook_review_or_first_event" if registered else "unregistered"
            ),
            "age_ms": None,
            "message": (
                "No events since the hooks were installed; open /hooks on the main checkout."
                if registered else "Project is not registered."
            ),
        },
        "hook_event_counts": {code: 0 for code in HOOK_EVENT_CODES},
        "coverage": {
            "root_lifecycle": True,
            "child_lifecycle": True,
            "tool_lifecycle": True,
            "root_usage": False,
            "child_usage": False,
        },
        "managed_lifecycle": {
            "orchestrations_active": 0,
            "orchestrations_paused": 0,
            "orchestrations_closed": 0,
            "host_runs_opened": 0,
            "host_runs_terminal": 0,
            "host_runs_pending": 0,
            "host_runs_abandoned": 0,
            "native_agent_bindings": 0,
            "operational_qa_passed": 0,
            "operational_qa_failed": 0,
            "host_usage_complete": 0,
            "host_usage_unknown": 0,
        },
    }



class CollectorClient:
    """Bounded, fail-fast client used by hook and App Server adapters."""

    def __init__(self, socket_path: Path, *, timeout: float = 0.25) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        reason = None
        try:
            raw = exchange_frame(
                self.socket_path, canonical_json(request), timeout=self.timeout,
                request_limit=MAX_REQUEST_BYTES, response_limit=16_384,
            )
        except ServiceTransportError as exc:
            reason = exc.reason_code
        if reason == "request_too_large":
            raise CollectorError("collector request is oversized")
        if reason == "response_too_large":
            raise CollectorError("collector response is oversized")
        if reason is not None:
            raise CollectorTransportError("collector is unavailable", code="transport_unavailable")
        invalid_response = False
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            invalid_response = True
        if invalid_response:
            raise CollectorError("collector response is invalid", code="response_invalid")
        if not isinstance(response, dict) or response.get("ok") is not True:
            code = response.get("code", "unknown") if isinstance(response, dict) else "unknown"
            raise CollectorError(f"collector rejected request: {code}", code=code)
        return response

    def bind(
        self, project_root: Path, session_id: str, producer: str, client_version: str
    ) -> str:
        response = self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "bind",
                "project_root": str(project_root.resolve()),
                "session_id": session_id,
                "producer": producer,
                "client_version": client_version,
            }
        )
        return str(response["binding_token"])

    def ingest(self, binding_token: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "ingest",
                "binding_token": binding_token,
                "record": record,
            }
        )

    def health(self, project_root: Path | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "protocol": COLLECTOR_PROTOCOL_VERSION,
            "op": "health",
        }
        if project_root is not None:
            request["project_root"] = str(project_root.resolve())
        response = self._request(request)
        server_schema = response.get("schema_version")
        server_version = _version_tuple(response.get("runtime_version"))
        client_version = _version_tuple(PACKAGE_VERSION)
        if client_version is None:  # guarded when PACKAGE_VERSION is loaded
            raise CollectorError("installed package version is invalid")
        if (
            server_version is not None
            and server_version > client_version
        ) or (
            isinstance(server_schema, int)
            and not isinstance(server_schema, bool)
            and server_schema > LEDGER_SCHEMA_VERSION
        ):
            raise CollectorNewerVersionError(
                "a newer collector owns the shared learning state; update this project",
                code="runtime_newer",
            )
        if (
            not isinstance(server_schema, int)
            or isinstance(server_schema, bool)
            or server_schema < LEDGER_SCHEMA_VERSION
            or server_version is None
            or server_version < client_version
        ):
            raise CollectorUpgradeRequired(
                "collector runtime is older than the installed package",
                code="runtime_upgrade_required",
            )
        if response.get("runtime_digest") != COLLECTOR_RUNTIME_DIGEST:
            raise CollectorBuildConflictError(
                "collector build conflicts with this package version",
                code="runtime_conflict",
            )
        return response

    def project(self, project_root: Path) -> str:
        """The registered project's identifier; registration stays an owner step."""
        response = self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "project",
                "project_root": str(project_root.resolve()),
            }
        )
        project_id = response.get("project_id")
        if type(project_id) is not str or re.fullmatch(r"prj_[0-9a-f]{32}", project_id) is None:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return project_id

    def register(self, project_root: Path) -> str:
        """Register this root on the running collector (the owner's step) and return its identifier."""
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "register",
                                  "project_root": str(project_root.resolve())})
        project_id = response.get("project_id")
        if type(project_id) is not str or re.fullmatch(r"prj_[0-9a-f]{32}", project_id) is None:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return project_id

    def owner_record_payload(self, project_root: Path, record: dict) -> bytes:
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "owner_record_payload",
                                  "project_root": str(project_root.resolve()), "record": record})
        try:
            return base64.b64decode(response["signing_payload_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise CollectorError("collector response is invalid", code="response_invalid") from None

    def commit_owner_record(self, project_root: Path, record: dict, signature: bytes) -> dict:
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "commit_owner_record",
                                  "project_root": str(project_root.resolve()), "record": record,
                                  "signature_b64": base64.b64encode(signature).decode("ascii")})
        committed = response.get("committed")
        if type(committed) is not dict:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return committed

    def learning_migration(self, project_root: Path) -> dict[str, bool]:
        """Whether this project's learning history still has to move, and whether a move could start now."""
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "learning_migration",
                                  "project_root": str(project_root.resolve())})
        names = ("moved", "has_learning", "drained")
        if any(type(response.get(name)) is not bool for name in names):
            raise CollectorError("collector response is invalid", code="response_invalid")
        return {name: response[name] for name in names}

    def shutdown(self, *, only_if_drained: bool = False) -> None:
        """Stop the collector; with `only_if_drained` it refuses (`collector_busy`) while any run is active."""
        self._request(
            {"protocol": COLLECTOR_PROTOCOL_VERSION,
             "op": "shutdown_if_drained" if only_if_drained else "shutdown"}
        )

    def issue_result_repair(
        self,
        project_root: Path,
        assignment_digest: str,
        reason_code: str,
    ) -> str:
        response = self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "issue_result_repair",
                "project_root": str(project_root.resolve()),
                "assignment_digest": assignment_digest,
                "reason_code": reason_code,
            }
        )
        return str(response["receipt_id"])
