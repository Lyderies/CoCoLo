"""The wizard's effects (S5): the only code that reads or writes files or runs anything. Each ports
setup.sh exactly; `steps` decides what they mean."""

import importlib.util
import os
import stat
import subprocess
import tempfile
from pathlib import Path

CLONE = Path(__file__).resolve().parents[2]

PROFILE_READ_LIMIT = 65536  # One byte past it is read, so a larger file shows as larger.


class EffectRefused(RuntimeError):
    """An effect refused before changing anything; its message is fixed text, safe to show."""


def read_owned_file(path) -> bytes | None:
    """The bytes of a regular file the owner owns, without ever following a symlink or opening a FIFO
    or device; None when it is absent or refused. Only root-owned system links on the way (such as
    /var on macOS) are resolved. Ported from setup.sh's read_profile."""
    try:
        uid = os.geteuid()
        full = os.path.abspath(str(path))
        for _ in range(40):
            parts = [part for part in full.split("/") if part]
            current = "/"
            for index, part in enumerate(parts):
                current = os.path.join(current, part)
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    if index == len(parts) - 1 or os.lstat(current).st_uid != 0:
                        return None
                    full = os.path.join(os.path.realpath(current), *parts[index + 1:])
                    break
            else:
                break
        else:
            return None
        parts = [part for part in full.split("/") if part]
        if not parts:
            return None
        # Walk by directory descriptors, each opened relative to its checked parent without following
        # a link, so a swapped parent cannot redirect the read.
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = following
                if os.fstat(directory).st_uid not in (0, uid):
                    return None
            leaf = parts[-1]
            info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                return None
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                    return None
                return os.read(descriptor, PROFILE_READ_LIMIT + 1)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    except OSError:
        return None


def read_routing(path) -> tuple[str, bytes | None]:
    """W4.5: ("absent", None) when there is no routing file, ("present", bytes) for a regular file read
    safely, and ("refused", None) for a symlink or any other kind of file, which is never opened."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "refused", None
    if not stat.S_ISREG(info.st_mode):
        return "refused", None
    raw = read_owned_file(path)
    return ("refused", None) if raw is None else ("present", raw)


def write_project_routing(project, content: str) -> None:
    """W4.4: write `.codex/orchestration/provider-routing.json` atomically (a new file renamed into
    place, mode 0644), never through a symlink. Refuses, changing nothing, when a folder on the way is
    a symlink or not a folder, when it resolves outside the project, or when the destination is a
    symlink or not a regular file. Ported from setup.sh's write_project_routing."""
    project = Path(project)
    current = project
    for part in (".codex", "orchestration"):
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise EffectRefused(f"refused: {part} is not a real directory inside the project")
    if os.path.realpath(current) != os.path.join(os.path.realpath(project), ".codex", "orchestration"):
        raise EffectRefused("refused: the configuration directory resolves outside the project")
    destination = current / "provider-routing.json"
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise EffectRefused("refused: provider-routing.json is a symlink or not a regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-routing.", dir=current)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


_INSTALLER = None


def installer():
    """W1.8: this clone's scripts/install.py, loaded by its path (as setup.sh does), never found on
    sys.path, so nothing from the project or the current folder can stand in for it."""
    global _INSTALLER
    if _INSTALLER is None:
        spec = importlib.util.spec_from_file_location("agent_orchestration_install", CLONE / "scripts" / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _INSTALLER = module
    return _INSTALLER


def git_helpers(project) -> list[str] | None:
    """W1.4: the project's own Git settings that name a program Git could start (none: []), read with
    plumbing; None when the settings cannot be read."""
    install = installer()
    try:
        return install.repository_helpers(Path(project))
    except (install.InstallRefused, OSError, subprocess.SubprocessError):
        return None


def git_changes(project, paths: list[str]) -> list[str] | None:
    """W5.1, W5.2: the paths under `paths` with uncommitted changes (a rename's both names), literal
    paths only; None when Git could not answer or must not run (a helper named, W1.4)."""
    if git_helpers(project) != []:
        return None
    install = installer()
    try:
        return install.project_changes(Path(project), list(paths))
    except (install.InstallRefused, OSError, subprocess.SubprocessError, ValueError):
        return None


def git_commit(project, message: str, detail: str, paths: list[str]) -> tuple[str, str]:
    """W5.3-W5.5: commit exactly `paths` with the installer's plumbing (no hook, filter or signing
    program runs): ("committed", ""), ("nothing", "") when no path changed (W5.4: not a failure), or
    ("failed", the installer's fixed refusal, or "")."""
    install = installer()
    try:
        result = install.commit_paths(Path(project), message, detail, list(paths))
    except install.InstallRefused as exc:
        return "failed", str(exc)
    except (OSError, subprocess.SubprocessError, ValueError):
        return "failed", ""
    return (result, "") if result in ("committed", "nothing") else ("failed", "")
