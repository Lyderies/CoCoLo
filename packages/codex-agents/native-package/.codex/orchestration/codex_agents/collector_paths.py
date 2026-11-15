"""Read-only paths for the observational collector client.

Neither a socket path nor possession of the digest key grants native authority.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class StateError(RuntimeError):
    """Raised when local state would violate an accepted boundary."""


UNMOVED_STATE = (
    "the learning state is still in its old macOS folder "
    "(~/Library/Application Support/codex-agents); ask the owner to run ./setup.sh "
    "(collector step), which moves it to ~/.local/state/codex-agents"
)


def default_state_root() -> Path:
    """Return the original platform-specific default without creating it."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise StateError("LOCALAPPDATA is required on Windows")
        return Path(base) / "codex-agents"
    from .host_paths import HostPathError
    from .host_paths import default_state_root as host_default_state_root

    try:
        return host_default_state_root()
    except HostPathError as exc:
        raise StateError(str(exc)) from None


def _unmoved_legacy_state() -> bool:
    """Whether only the pre-0.8.10 macOS state folder exists."""
    if os.name == "nt":
        return False
    from .host_paths import legacy_state_root

    legacy = legacy_state_root()
    return (
        legacy is not None
        and os.path.lexists(legacy)
        and not os.path.lexists(default_state_root())
    )


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise StateError("state path cannot be inspected") from None
        # lstat also catches dangling links, which exists() would hide.
        if stat.S_ISLNK(mode):
            raise StateError("state path contains a symlink component")


def _check_owner_mode(path: Path, *, directory: bool) -> None:
    # Shared persistence callers classify missing files and permission errors.
    # Preserve their OSError contract; CollectorPaths maps it at its boundary.
    info = path.lstat()
    # Refuse links and special files explicitly, including dangling links.
    valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_kind:
        raise StateError("state path has an unsafe file kind")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StateError("state path is not owned by the current user")
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(info.st_mode) != expected:
        raise StateError("state path has unsafe permissions")
    if not directory and info.st_nlink != 1:
        raise StateError("state file has another hard link")


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _canonical_project_root(path: Path) -> Path:
    if not path.is_absolute():
        raise StateError("project root must be absolute")
    _assert_no_symlink_components(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise StateError("project root must be a directory")
    _assert_no_symlink_components(resolved)
    return resolved


class CollectorPaths:
    """Existing client paths only; construction does not read optional files."""

    def __init__(self, root: Path | None = None) -> None:
        try:
            if root is None:
                if _unmoved_legacy_state():
                    raise StateError(UNMOVED_STATE)
                requested = default_state_root()
            else:
                requested = root.expanduser()
            if not requested.is_absolute():
                raise StateError("state root must be absolute")
            _assert_no_symlink_components(requested)
            if _inside_git_tree(requested):
                raise StateError("state root must be outside a Git working tree")
            if not requested.is_dir():
                raise StateError("state root must be a directory")
            resolved = requested.resolve(strict=True)
            _assert_no_symlink_components(resolved)
            if _inside_git_tree(resolved):
                raise StateError("state root must be outside a Git working tree")
            _check_owner_mode(resolved, directory=True)
        except StateError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise StateError("state root cannot be inspected") from None
        self.root = resolved
        self.socket = resolved / "collector.sock"
        self.runtime_lock = resolved / "collector-runtime.lock"
        self.collector_authorization = resolved / "collector-authorization.json"
        self.client_health_lock = resolved / "collector-client-health.lock"
        self.client_health = resolved / "collector-client-health.json"
        self.key_path = resolved / "digest.key"

    def digest_key(self) -> bytes:
        """Read at most 33 bytes from a checked, single-link owner secret."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if not nofollow or not nonblock:
            raise StateError("digest key safe open is unavailable")
        descriptor = None
        try:
            _assert_no_symlink_components(self.key_path)
            _check_owner_mode(self.root, directory=True)
            _check_owner_mode(self.key_path, directory=False)
            expected = self.key_path.lstat()
            if not stat.S_ISREG(expected.st_mode):
                raise StateError("digest key path is unsafe")
            descriptor = os.open(self.key_path, os.O_RDONLY | nofollow | nonblock)
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or stat.S_IMODE(actual.st_mode) != 0o600
                or (hasattr(os, "getuid") and actual.st_uid != os.getuid())
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise StateError("digest key path is unsafe")
            _assert_no_symlink_components(self.key_path)
            key = os.read(descriptor, 33)
            after = os.fstat(descriptor)
            current = self.key_path.lstat()
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
                or (hasattr(os, "getuid") and after.st_uid != os.getuid())
                or (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino)
                or not stat.S_ISREG(current.st_mode)
            ):
                raise StateError("digest key path is unsafe")
            if len(key) != 32 or actual.st_size != 32 or after.st_size != 32:
                raise StateError("digest key has an invalid length")
            return key
        except StateError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise StateError("digest key path is unsafe") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    raise StateError("digest key path is unsafe") from None
