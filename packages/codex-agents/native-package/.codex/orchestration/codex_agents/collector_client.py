"""Bounded collector client and local runtime identity; no SQLite access.

The caller supplies the endpoint. This preserves the existing collector protocol;
it does not authenticate a caller or enable the new project/account service APIs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .collector_contracts import (
    COLLECTOR_PROTOCOL_VERSION,
    LEDGER_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    CollectorBuildConflictError,
    CollectorError,
    CollectorNewerVersionError,
    CollectorUpgradeRequired,
    _version_tuple,
)
from .learning_contracts import canonical_json
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
            raise CollectorError("collector is unavailable", code="transport_unavailable")
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

    def shutdown(self) -> None:
        self._request(
            {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "shutdown"}
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
