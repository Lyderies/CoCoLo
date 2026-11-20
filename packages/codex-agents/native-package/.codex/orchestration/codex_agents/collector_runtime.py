"""Existing observational collector bootstrap and bounded client health counters.

No persistence-owner imports or database access. Native caller authentication and
new project/account service activation remain separate requirements.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX transport only.
    fcntl = None

from .collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION, CollectorClient
from .collector_contracts import CollectorAuthorizationError, CollectorError, CollectorVersionError, _version_tuple
from .collector_paths import CollectorPaths, StateError, _canonical_project_root, _check_owner_mode
from .contracts import ContractError
from .learning_contracts import canonical_json, keyed_digest
from .runtime_files import ORCHESTRATION_PREFIX, RUNTIME_FILES, runtime_digest

COLLECTOR_AUTHORIZATION_SCHEMA_VERSION = 1


HOOK_INGEST_REASON_CODES = {
    "binding_invalid", "provenance_mismatch", "capability_mismatch",
    "operation_rejected", "operation_invalid", "project_unregistered", "record_conflict",
    "request_timeout", "request_oversized", "request_invalid", "protocol_mismatch",
    "transport_unavailable", "response_invalid", "unknown_rejection",
}


def validate_collector_runtime(cli_path: Path) -> dict[str, str | int]:
    """Validate the complete installed package and return its collector identity."""

    from .learning_engine import _installed_managed_files, _read_managed_bytes

    try:
        if cli_path.is_symlink():
            raise ContractError("installed collector entry point is invalid")
        resolved_cli = cli_path.resolve(strict=True)
        bundle_root = resolved_cli.parents[2]
        expected_cli = bundle_root / ".codex" / "orchestration" / "cli.py"
        if resolved_cli != expected_cli:
            raise ContractError("installed collector entry point is invalid")
        paths, contents = _installed_managed_files(bundle_root)
        manifest_relative = ".codex/orchestration/install-manifest.json"
        manifest_path = bundle_root / manifest_relative
        if manifest_path.is_symlink():
            raise ContractError("installed package manifest is invalid")
        if manifest_path.exists():
            manifest = json.loads(
                _read_managed_bytes(bundle_root, manifest_relative)
            )
            version = manifest.get("version") if isinstance(manifest, dict) else None
        else:
            version_path = bundle_root / "VERSION"
            descriptor = os.open(
                version_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ContractError("package VERSION is invalid")
                version = os.read(descriptor, 33).decode("ascii").strip()
            finally:
                os.close(descriptor)
        if _version_tuple(version) is None:
            raise ContractError("package VERSION is invalid")
        # The runtime is this package's own file set, never whatever else the
        # manifest lists: an extra module there is not runtime, and the digest
        # covers every runtime file, configuration and schemas included.
        if any(ORCHESTRATION_PREFIX + relative not in contents for relative in RUNTIME_FILES):
            raise ContractError("installed collector runtime is incomplete")
        digest = runtime_digest(
            {relative: contents[ORCHESTRATION_PREFIX + relative] for relative in RUNTIME_FILES}
        )
        if version != PACKAGE_VERSION or digest != COLLECTOR_RUNTIME_DIGEST:
            raise ContractError("installed collector identity is inconsistent")
        return {
            "schema_version": COLLECTOR_AUTHORIZATION_SCHEMA_VERSION,
            "runtime_version": version,
            "runtime_digest": digest,
        }
    except (
        ContractError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        IndexError,
    ) as exc:
        raise CollectorAuthorizationError(
            "installed package integrity validation failed",
            code="runtime_integrity_invalid",
        ) from exc



def _collector_runtime_authorization(
    state: CollectorPaths, *, allow_quiesced: bool
) -> dict[str, str | int | bool]:
    """Read a valid active or upgrade-quiesced collector identity."""

    path = state.collector_authorization
    if not path.exists():
        raise CollectorAuthorizationError(
            "collector runtime is not authorized; run learning-register",
            code="runtime_authorization_missing",
        )
    if path.is_symlink():
        raise CollectorAuthorizationError(
            "collector runtime authorization is unsafe",
            code="runtime_authorization_invalid",
        )
    try:
        _check_owner_mode(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise StateError("collector runtime authorization is unsafe")
            raw = os.read(descriptor, 1025)
        finally:
            os.close(descriptor)
        if len(raw) > 1024:
            raise StateError("collector runtime authorization is invalid")
        value = json.loads(raw)
    except (OSError, StateError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) not in (
            {"schema_version", "runtime_version", "runtime_digest"},
            {
                "schema_version", "runtime_version", "runtime_digest",
                "quiesced_for_upgrade",
            },
        )
        or value.get("schema_version") != COLLECTOR_AUTHORIZATION_SCHEMA_VERSION
        or _version_tuple(value.get("runtime_version")) is None
        or not isinstance(value.get("runtime_digest"), str)
        or len(value["runtime_digest"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["runtime_digest"]
        )
    ):
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        )
    if value.get("quiesced_for_upgrade") is not True and "quiesced_for_upgrade" in value:
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        )
    if value.get("quiesced_for_upgrade") is True and not allow_quiesced:
        raise CollectorAuthorizationError(
            "collector runtime is quiesced for an owner-authorized upgrade",
            code="runtime_authorization_quiesced",
        )
    return value


def authorized_collector_runtime(state: CollectorPaths) -> dict[str, str | int]:
    """Read the exact active collector identity authorized in owner-only state."""

    return _collector_runtime_authorization(state, allow_quiesced=False)


def require_authorized_collector_runtime(
    state: CollectorPaths,
) -> dict[str, str | int]:
    """Fail closed unless this process is the exact owner-authorized runtime."""

    authorized = authorized_collector_runtime(state)
    if (
        authorized["runtime_version"] != PACKAGE_VERSION
        or authorized["runtime_digest"] != COLLECTOR_RUNTIME_DIGEST
    ):
        raise CollectorAuthorizationError(
            "installed collector differs from the owner-authorized runtime; "
            "run learning-register from the intended package",
            code="runtime_unauthorized",
        )
    return authorized


CLIENT_HEALTH_CODES = {
    "collector_start_failed",
    "collector_unavailable",
    "maintenance_active",
    "hook_normalization_rejected",
    "hook_binding_rejected",
    "hook_ingest_rejected",
}


# O0c P11: writes stay within CLIENT_HEALTH_WRITE_BYTES by dropping the projects whose newest counter is oldest;
# reads accept up to CLIENT_HEALTH_READ_BYTES, so a file an older build wrote past the bound is read and repaired by
# the next write instead of failing status, writes and clears for good.
CLIENT_HEALTH_WRITE_BYTES = 65_536
CLIENT_HEALTH_READ_BYTES = 1_048_576


def _client_health_payload(data: dict[str, Any], keep: str | None = None) -> bytes:
    """The file's bytes, within CLIENT_HEALTH_WRITE_BYTES; `keep` (the project just written) is dropped last."""
    projects = data["projects"]
    payload = canonical_json(data) + b"\n"
    while len(payload) > CLIENT_HEALTH_WRITE_BYTES and projects:
        excess = len(payload) - CLIENT_HEALTH_WRITE_BYTES
        for digest in sorted(projects, key=lambda d: (d == keep, max(
                (c["updated_at_ms"] for c in projects[d].values()), default=0))):
            if excess <= 0:
                break
            excess -= len(canonical_json({digest: projects.pop(digest)})) - 1  # its entry and comma
        payload = canonical_json(data) + b"\n"
    return payload


def _client_health_data(state: CollectorPaths) -> dict[str, Any]:
    if not state.client_health.exists():
        return {"schema_version": 1, "projects": {}}
    if state.client_health.is_symlink():
        raise StateError("client health path is unsafe")
    _check_owner_mode(state.client_health, directory=False)
    try:
        with state.client_health.open('rb') as stream:
            raw = stream.read(CLIENT_HEALTH_READ_BYTES + 1)
        if len(raw) > CLIENT_HEALTH_READ_BYTES:
            raise StateError("client health state is invalid")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("client health state is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("projects"), dict)
    ):
        raise StateError("client health state is invalid")
    allowed_codes = CLIENT_HEALTH_CODES
    for project_digest, counters in value["projects"].items():
        if (
            not isinstance(project_digest, str)
            or len(project_digest) != 64
            or any(character not in "0123456789abcdef" for character in project_digest)
            or not isinstance(counters, dict)
            or set(counters) - allowed_codes
        ):
            raise StateError("client health state is invalid")
        for counter in counters.values():
            if (
                not isinstance(counter, dict)
                or set(counter) not in ({"count", "updated_at_ms"},
                                        {"count", "updated_at_ms", "last_reason_code"})
                or ("last_reason_code" in counter
                    and (not isinstance(counter["last_reason_code"], str)
                         or counter["last_reason_code"] not in HOOK_INGEST_REASON_CODES))
                or not isinstance(counter["count"], int)
                or isinstance(counter["count"], bool)
                or counter["count"] < 1
                or not isinstance(counter["updated_at_ms"], int)
                or isinstance(counter["updated_at_ms"], bool)
                or counter["updated_at_ms"] < 0
            ):
                raise StateError("client health state is invalid")
    return value


def client_health_increment(
    state: CollectorPaths,
    project_root: Path,
    code: str,
    *,
    now_ms: int | None = None,
    reason_code: str | None = None,
) -> None:
    """Persist one content-free, project-keyed client failure counter."""

    if code not in CLIENT_HEALTH_CODES or fcntl is None:
        return
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    descriptor = os.open(
        state.client_health_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _client_health_data(state)
        projects = data["projects"]
        project = projects.setdefault(project_digest, {})
        current = project.get(code, {"count": 0, "updated_at_ms": 0})
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        project[code] = {
            "count": int(current.get("count", 0)) + 1,
            "updated_at_ms": timestamp,
        }
        if reason_code is not None and code == "hook_ingest_rejected":
            project[code]["last_reason_code"] = (
                reason_code if isinstance(reason_code, str) and reason_code in HOOK_INGEST_REASON_CODES
                else "unknown_rejection"
            )
        temporary = state.root / f".collector-client-health.{secrets.token_hex(8)}.tmp"
        payload = _client_health_payload(data, keep=project_digest)
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state.client_health)
            os.chmod(state.client_health, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def client_health_snapshot(state: CollectorPaths, project_root: Path) -> dict[str, Any]:
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    return dict(_client_health_data(state)["projects"].get(project_digest, {}))


def client_health_clear(state: CollectorPaths, project_root: Path) -> None:
    """Remove one deleted project's content-free client counters."""

    if fcntl is None or not state.client_health.exists():
        return
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    descriptor = os.open(
        state.client_health_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _client_health_data(state)
        if data["projects"].pop(project_digest, None) is None:
            return
        temporary = state.root / f".collector-client-health.{secrets.token_hex(8)}.tmp"
        descriptor_out = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor_out, "wb") as handle:
                handle.write(_client_health_payload(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state.client_health)
            os.chmod(state.client_health, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# How long a started collector is given to answer. It starts as two interpreters
# (the CLI entry, then the collector's own) and opens the account services, which
# takes over a second on a busy machine; the wait ends as soon as it answers.
COLLECTOR_START_SECONDS = 20.0
# A hook runs inside the client's own session and never holds it up that long: the
# collector it started keeps starting, and a later event finds it.
HOOK_START_SECONDS = 1.0


def ensure_collector(
    state: CollectorPaths,
    cli_path: Path,
    *,
    project_root: Path,
    timeout: float = COLLECTOR_START_SECONDS,
) -> dict[str, Any]:
    """Ensure one detached collector is healthy without storing its project path."""

    require_authorized_collector_runtime(state)
    client = CollectorClient(state.socket)
    try:
        return client.health()
    except CollectorVersionError:
        client_health_increment(state, project_root, "collector_start_failed")
        raise
    except CollectorError:
        pass
    if fcntl is None:
        client_health_increment(state, project_root, "collector_start_failed")
        raise CollectorError("collector auto-start is unavailable")
    descriptor = os.open(
        state.runtime_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            client_health_increment(state, project_root, "maintenance_active")
            raise CollectorError("collector maintenance is active") from exc
        return _start_collector_locked(
            state,
            cli_path,
            project_root=project_root,
            timeout=timeout,
        )
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _start_collector_locked(
    state: CollectorPaths,
    cli_path: Path,
    *,
    project_root: Path,
    timeout: float = COLLECTOR_START_SECONDS,
) -> dict[str, Any]:
    """Start the collector while the caller owns the runtime maintenance lock."""

    require_authorized_collector_runtime(state)
    client = CollectorClient(state.socket)
    try:
        return client.health()
    except CollectorVersionError:
        client_health_increment(state, project_root, "collector_start_failed")
        raise
    except CollectorError:
        pass
    from .host_paths import PYTHON_ISOLATION_FLAGS

    collector = subprocess.Popen(
        [
            sys.executable,
            # Isolated, no bytecode in managed paths (isolated mode ignores
            # PYTHONDONTWRITEBYTECODE), and no site-packages, so no .pth file or
            # sitecustomize a sandboxed session could reach runs in the collector.
            *PYTHON_ISOLATION_FLAGS,
            str(cli_path.resolve(strict=True)),
            "learning-serve",
            "--state-root",
            str(state.root),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        close_fds=True,
        # No inherited environment; macOS developer tools skip the xcrun lookup cache
        # under $TMPDIR, which a sandboxed session can write.
        env={"PATH": os.defpath, "xcrun_nocache": "1"},
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = client.health()
        except CollectorError:
            # A collector that has already ended never served: waiting longer cannot help,
            # so a start that cannot work is reported at once and only a slow one is waited for.
            if _ended(collector):
                break
            time.sleep(0.02)
            continue
        # A detached service that outlives this client: nothing waits for it, so its
        # handle is given up here instead of being reported as a leak when dropped.
        with contextlib.suppress(AttributeError):
            collector.returncode = 0
        return health
    client_health_increment(state, project_root, "collector_start_failed")
    raise CollectorError("collector did not become healthy")


def _ended(collector: Any) -> bool:
    """Whether the process just started has already ended."""

    poll = getattr(collector, "poll", None)
    return poll is None or type(poll()) is int
