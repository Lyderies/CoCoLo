"""The wizard's effects (S5): the only code that reads or writes files or runs anything. Each ports
setup.sh exactly; `steps` decides what they mean."""

import os
import stat
import tempfile
from pathlib import Path

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
