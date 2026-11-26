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

from .compatibility_message import compatibility_message
from .collector_contracts import (
    COLLECTOR_PROTOCOL_VERSION,
    COLLECTOR_MUTATING_OPERATIONS,
    LEDGER_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    CollectorBuildConflictError,
    CollectorError,
    CollectorTransportError,
    CollectorNewerVersionError,
    CollectorUpgradeRequired,
    RUNTIME_UPGRADE_MESSAGE,
    _version_tuple,
)
from .learning_contracts import canonical_json
from .learning_adapters import HOOK_EVENTS
from .runtime_files import RUNTIME_FILES, runtime_digest
from .service_transport import ServiceTransportError, exchange_frame


def _collector_runtime_digest(root: Path | None = None) -> str:
    """The digest of every runtime file this process runs from (`RUNTIME_FILES`), or of those under `root`."""

    orchestration_root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    return runtime_digest(
        {relative: (orchestration_root / relative).read_bytes() for relative in RUNTIME_FILES}
    )


def _package_version() -> str:
    bundle_root = Path(__file__).resolve().parent.parent.parent.parent
    manifest_path = bundle_root / ".codex" / "orchestration" / "install-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, json.JSONDecodeError, RecursionError) as exc:
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


def collector_code_stale(*, root: Path | None = None, loaded: str | None = None) -> bool:
    """True when the runtime files now on disk differ from the digest this process loaded.

    Python never reloads a module, so a collector started before its files changed keeps running the old
    code; this says so. A file that cannot be read counts as changed. Never raises.
    """
    try:
        return _collector_runtime_digest(root) != (COLLECTOR_RUNTIME_DIGEST if loaded is None else loaded)
    except Exception:  # noqa: BLE001 - unreadable or missing files mean the loaded code cannot be confirmed
        return True
PACKAGE_VERSION = _package_version()


def _with_loaded_digest(error: CollectorError, response: dict[str, Any], *, identity_valid: bool) -> CollectorError:
    """Keep what the collector reported about the code it loaded on the refusal health() raises.

    A client that starts after the runtime files changed hashes the new files, so an old collector's
    digest differs from its own: health() then raises instead of returning `runtime_stale`. The error says
    which code the collector loaded (when well formed), whether the identity fields it judged by were well
    formed at all, and what the collector said about its own files, so callers can tell a collector that
    runs old code from one that sent a malformed identity or runs another build.
    """
    digest = response.get("runtime_digest")
    error.loaded_digest = digest if type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest) else None
    error.identity_valid = identity_valid
    reported = response.get("runtime_stale")
    error.reported_stale = reported if type(reported) is bool else None
    return error


def collector_code_diagnosis(error: BaseException) -> str | None:
    """What a health() refusal says about the collector's code, one of:

    `stale`: it runs code other than this install's files and restarting it would load them (an older package
    version or schema, or the same version with a digest this install does not have and a collector that
    does not say its own files are unchanged); `conflict`: the same version, a well-formed other digest, and
    the collector says its own files are unchanged: another build, which a restart would not change;
    `invalid`: the identity fields it judged by (schema, version, digest) were malformed; None: any other
    error (a newer collector included).
    """
    if not isinstance(error, (CollectorUpgradeRequired, CollectorBuildConflictError)):
        return None
    if getattr(error, "identity_valid", False) is not True:
        return "invalid"
    if isinstance(error, CollectorBuildConflictError) and getattr(error, "reported_stale", None) is False:
        return "conflict"
    return "stale"


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

    def _request(self, request: dict[str, Any], *, earlier_collector: bool = False,
                 echo_digest: str | None = None) -> dict[str, Any]:
        # `earlier_collector`: the hand-over stop to a collector of an older release (UPG-1), whose
        # handler accepts only the bare shape. Never used for a same-build collector (decision 129.3).
        if request.get("op") in COLLECTOR_MUTATING_OPERATIONS and not earlier_collector:
            request = {**request, "runtime_digest": COLLECTOR_RUNTIME_DIGEST if echo_digest is None else echo_digest}
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
            if code == "runtime_upgrade_required":
                raise CollectorUpgradeRequired(RUNTIME_UPGRADE_MESSAGE, code=code)
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
                compatibility_message(response.get("runtime_version"), project_root=project_root),
                code="runtime_newer",
            )
        if (
            not isinstance(server_schema, int)
            or isinstance(server_schema, bool)
            or server_schema < LEDGER_SCHEMA_VERSION
            or server_version is None
            or server_version < client_version
        ):
            raise _with_loaded_digest(CollectorUpgradeRequired(
                compatibility_message(response.get("runtime_version"), project_root=project_root),
                code="runtime_upgrade_required",
            ), response, identity_valid=(
                isinstance(server_schema, int) and not isinstance(server_schema, bool)
                and server_version is not None))
        if response.get("runtime_digest") != COLLECTOR_RUNTIME_DIGEST:
            raise _with_loaded_digest(CollectorBuildConflictError(
                "collector build conflicts with this package version",
                code="runtime_conflict",
            ), response, identity_valid=(type(response.get("runtime_digest")) is str and re.fullmatch(
                r"[0-9a-f]{64}", response["runtime_digest"]) is not None))
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

    def _owner_signed(self, request: dict[str, Any]) -> dict[str, Any]:
        """RS-E E2a-3/11 (review round 1): the request with the owner's proof for exactly its fields (subject: its
        project root). Needs the owner's state folder: a caller who cannot write it cannot sign."""
        from .collector_paths import CollectorPaths, StateError
        from .service_enrollment import EnrollmentError, collector_owner_preamble
        message = {key: value for key, value in request.items() if key != "protocol"}
        try:
            line = collector_owner_preamble(CollectorPaths(Path(self.socket_path).parent), canonical_json(message),
                                            request["project_root"])
        except (EnrollmentError, StateError, OSError):
            raise CollectorError("the owner's collector credential is unavailable", code="operation_rejected") from None
        return {**request, "owner": line.decode("ascii")}

    def launch_recorded(self, project_root: Path) -> bool:
        """RS-E E2a-11: whether setup's launch record exists for this registered root (the owner's read)."""
        response = self._request(self._owner_signed({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "launch_status",
                                                     "project_root": str(project_root.resolve())}))
        if type(response.get("recorded")) is not bool:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return response["recorded"]

    def start_status(self, project_root: Path) -> str | None:
        """RS-E E2c-9: the collector starter's status line for this registered root (the owner's read), or None."""
        response = self._request(self._owner_signed({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "start_status",
                                                     "project_root": str(project_root.resolve())}))
        line = response.get("recovery")
        if line is not None and (type(line) is not str or len(line) > 4096):
            raise CollectorError("collector response is invalid", code="response_invalid")
        return line

    def start_unpark(self, project_root: Path) -> None:
        """RS-E E2c-4 (owner decision 143): the owner's start command ends this root's crash-loop park."""
        response = self._request(self._owner_signed({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "start_unpark",
                                                     "project_root": str(project_root.resolve())}))
        if response.get("ok") is not True:
            raise CollectorError("collector response is invalid", code="response_invalid")

    def record_launch(self, project_root: Path, launch_copy: str, launch_digest: str) -> None:
        """RS-E E2a-3: record setup's validated launch target for this registered root (the owner's step; the
        collector verifies the copy itself, so this may take longer than the default timeout)."""
        response = self._request(self._owner_signed({
            "protocol": COLLECTOR_PROTOCOL_VERSION, "op": "record_launch",
            "project_root": str(project_root.resolve()), "launch_copy": str(launch_copy),
            "launch_digest": launch_digest}))
        if response.get("ok") is not True:
            raise CollectorError("collector response is invalid", code="response_invalid")

    def owner_record_payload(self, project_root: Path, record: dict) -> bytes:
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "owner_record_payload",
                                  "project_root": str(project_root.resolve()), "record": record})
        try:
            return base64.b64decode(response["signing_payload_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise CollectorError("collector response is invalid", code="response_invalid") from None

    def verify_owner_record(self, project_root: Path, record: dict, signature: bytes) -> bool:
        """Whether this is the owner's signature over that feedback record for this project."""
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "verify_owner_record",
                                  "project_root": str(project_root.resolve()), "record": record,
                                  "signature_b64": base64.b64encode(signature).decode("ascii")})
        if type(response.get("valid")) is not bool:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return response["valid"]

    def commit_owner_record(self, project_root: Path, record: dict, signature: bytes) -> dict:
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "commit_owner_record",
                                  "project_root": str(project_root.resolve()), "record": record,
                                  "signature_b64": base64.b64encode(signature).decode("ascii")})
        committed = response.get("committed")
        if type(committed) is not dict:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return committed

    def confirm_legacy_slot(self, project_root: Path, record: dict, signature: bytes) -> dict:
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "confirm_legacy_slot",
                                  "project_root": str(project_root.resolve()), "record": record,
                                  "signature_b64": base64.b64encode(signature).decode("ascii")})
        committed = response.get("committed")
        if type(committed) is not dict:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return committed

    def learning_migration(self, project_root: Path) -> dict:
        """Whether this project's learning history still has to move, and whether a move could start now.

        `earlier_version_runs` says how many runs an earlier version left still hold
        a slot (and so hold the move back), and when the last of them stops counting;
        None from a collector of an earlier release, which does not report it.
        """
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "learning_migration",
                                  "project_root": str(project_root.resolve())})
        names = ("moved", "has_learning", "drained", "activation_pending")
        if any(type(response.get(name)) is not bool for name in names):
            raise CollectorError("collector response is invalid", code="response_invalid")
        earlier = response.get("earlier_version_runs")
        if earlier is None and "earlier_version_runs" not in response:
            # A collector of an earlier release does not report it: unknown.
            return {**{name: response[name] for name in names}, "earlier_version_runs": None}
        if (type(earlier) is not dict or set(earlier) != {"holding", "finished_by_ms"}
                or type(earlier["holding"]) is not int or earlier["holding"] < 0
                or (earlier["finished_by_ms"] is not None if earlier["holding"] == 0
                    else earlier["finished_by_ms"] is not None and
                    (type(earlier["finished_by_ms"]) is not int or earlier["finished_by_ms"] < 0))):
            raise CollectorError("collector response is invalid", code="response_invalid")
        return {**{name: response[name] for name in names}, "earlier_version_runs": dict(earlier)}

    def learning_moved_if_empty(self, project_root: Path) -> bool:
        """Mark a project with no ledger learning history as living in its own store; whether it is moved."""
        response = self._request({"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "learning_moved_if_empty",
                                  "project_root": str(project_root.resolve())})
        if type(response.get("moved")) is not bool:
            raise CollectorError("collector response is invalid", code="response_invalid")
        return response["moved"]

    def shutdown(self, *, only_if_drained: bool = False, earlier_collector: bool = False,
                 echo_digest: str | None = None) -> None:
        """Stop the collector; with `only_if_drained` it refuses (`collector_busy`) while any run is active.

        `earlier_collector` sends the bare shape an older release accepts (UPG-1); only the maintenance
        hand-over uses it, after the collector's own health reply showed it is older than this build."""
        self._request(
            {"protocol": COLLECTOR_PROTOCOL_VERSION,
             "op": "shutdown_if_drained" if only_if_drained else "shutdown"},
            earlier_collector=earlier_collector, echo_digest=echo_digest,
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
