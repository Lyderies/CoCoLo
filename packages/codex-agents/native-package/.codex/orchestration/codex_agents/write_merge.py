"""The merge of a verified change into the owner's working directory, its exact undo, and crash recovery.

Requirements (DESIGN-0.4.0-M3.md draft 2 §3, draft 3 slice 2; the owner's decision: COCOLO merges
verified work into the working directory itself, a file the owner changed since the child's base
is never overwritten, and every merge keeps an exact undo):
- M3-21 The merge (`merge_candidate`). Preconditions first, each a fixed reason and a plain sentence
  with nothing changed: git usable and the project a normal work tree; no merge, rebase,
  cherry-pick, revert or bisect in progress (`write_merge_repo_busy`); the owner's HEAD still the
  candidate's base (`write_merge_base_changed`; a moved HEAD needs a candidate rebuilt and re-tested
  on it); only regular files added, modified (same mode) or deleted
  (`write_merge_unsupported_change`); no sparse checkout, skip-worktree path, submodule, nested
  repository, symlink or other filesystem in the way (`write_merge_path_unsafe`); no path git
  converts, by attributes of the base or the candidate tree or `info/attributes`, or
  `core.autocrlf` (`write_merge_transformed_path`); a filesystem with an atomic exchange and a
  no-replace rename (`write_merge_fs_unsupported`, probed); each base file at most 4 MiB and the
  undo at most 16 MiB (`write_merge_too_large`); each modified or deleted file exactly the base
  (bytes, and the executable bit) (`write_merge_owner_changed`); nothing at an added path
  (`write_merge_path_taken`). Then: the undo set (base bytes) stored and a journal written in
  COCOLO's state folder, both fsynced; git fenced; parent folders created one at a time (under a
  temporary name, recorded, then renamed into place with no-replace); each file in sorted order,
  progress recorded after each: modify = write the new content to a temporary name beside the file
  (with the file's own permission bits), exchange it with the file in one kernel step, then check
  the swapped-out file is still exactly the base and the target still the new file, exchanging back
  on any difference; add = a rename the kernel refuses if anything is there (a letter-case variant
  too); delete = rename away (no-replace), check, rename back on a mismatch. The swapped-out old
  copies are kept until a final check of every target and every old copy, then each is checked once
  more and removed. Any mismatch or error rolls back every step in reverse, each step checked first;
  the folder is then as it was. The merge never stages, commits or runs a git command that touches
  the working tree.
- M3-21 §3.4 The git fence: COCOLO creates `.git/index.lock` exclusively, with its token as content,
  from just before the first change to just after the last; git refuses index-writing commands
  meanwhile. When the owner's git holds it, the merge waits up to 10 s, then stops
  (`write_merge_git_busy`). The lock is removed only when it holds a token COCOLO recorded.
- M3-22 Exact undo (`undo_merge`): the same machinery in reverse, under the same lock and fence, with
  its own journal: bytes and modes restored, deleted files re-created, added files removed, folders
  the merge created removed when empty and still the same folders; stopped with nothing changed
  when the owner changed a merged file since (`write_undo_owner_changed`) or when a later merge
  touched the same files and must be undone first (`write_undo_order`, naming it). The undo needs
  no git object: the base bytes are COCOLO's own files. Not restored: modification times, extended
  attributes and Finder tags of changed files.
- M3-27 Crash recovery (`recover`, also run first by every merge and undo): an unfinished journal is
  rolled back from what is on disk (each temporary's inode is recorded before it is used), unless
  every file had already been written, in which case the final check runs and the merge completes.
  A path recovery cannot put back without overwriting something is left alone and reported
  (`needs_owner`); the fence is removed either way (when it holds COCOLO's token).
- M3-29 Nothing of COCOLO's is left in the owner's repository: no object, ref, index entry or
  configuration is written; the fence and the `.cocolo-merge-*` names exist only while a merge
  runs. The object-freshening rule of slice 1 holds: every git call here only reads.

Residual (design R13): a program that held an old file open from before the merge and writes into
it after its last check (microseconds before it is removed) writes into a removed file.
Evidence: S11 (`atomic_rename`). No callers yet (slice 12 wires it); RS-D custody of the undo set
is slice 9. Standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import atomic_rename
from . import hardened_git as hg
from .git_plumbing import GitOutputLimitError, git_executable
from .host_paths import within

MAX_FILE_BYTES = 4 * 1_048_576
MAX_UNDO_BYTES = 16 * 1_048_576
MAX_NEW_FILE_BYTES = hg.MAX_BASE_BLOB_BYTES
MAX_PATHS = 2000
MAX_JOURNAL_BYTES = 16 * 1_048_576
GIT_BUSY_WAIT_SECONDS = 10.0
LOCK_WAIT_SECONDS = 60.0
TEMP_PREFIX = ".cocolo-merge-"
JOURNAL_FORMAT = 1
BUSY_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
CONVERTING_ATTRIBUTES = ("filter", "text", "eol", "working-tree-encoding", "ident", "crlf")
REGULAR_MODES = {"100644": 0o644, "100755": 0o755}
TRUE_VALUES = ("true", "yes", "on", "1", "")
NOT_RESTORED = (
    "Not restored: file modification times, and extended attributes and Finder tags of the "
    "files that were changed."
)

_ID = re.compile(r"[0-9a-f]{32}")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

FaultHook = Callable[[str, "str | None"], None]


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Stopped:
    reason: str
    message: str
    path: str | None = None

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class Merged:
    merge_id: str
    task: str
    changes: tuple[tuple[str, str], ...]
    created_folders: tuple[str, ...]
    kept_copies: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class Undone:
    undo_id: str
    merge_id: str
    task: str
    changes: tuple[tuple[str, str], ...]
    folders_kept: tuple[str, ...]
    kept_copies: tuple[str, ...] = ()
    not_restored: str = NOT_RESTORED

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class Recovered:
    journal_id: str
    kind: str
    task: str
    outcome: str  # completed | rolled_back | needs_owner
    paths: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class MergeRecord:
    merge_id: str
    task: str
    seq: int
    state: str  # merged | undone | stopped | interrupted | needs_owner | unfinished
    paths: tuple[str, ...]


class _Stop(Exception):
    """Internal: stop the merge with this reason (the caller rolls back)."""

    def __init__(self, reason: str, path: str | None, sentence: str) -> None:
        super().__init__(reason)
        self.reason, self.path, self.sentence = reason, path, sentence


# --------------------------------------------------------------------------------------------
# Files: no-follow reads and writes under directory descriptors, durable
# --------------------------------------------------------------------------------------------

def _fsync(descriptor: int) -> None:
    if sys.platform == "darwin" and hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    """Write a temporary file's content (the merge's only writes of file content)."""

    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _write_plain(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _write_temp(directory: int, name: str, data: bytes, mode: int) -> int:
    """Create `name` exclusively, write, give it the mode, fsync; its inode. Removed on failure."""

    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    inode = os.fstat(descriptor).st_ino
    try:
        _write_all(descriptor, data)
        os.fchmod(descriptor, mode)
        _fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        if _inode(directory, name) == inode:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=directory)
        raise
    os.close(descriptor)
    return inode


def _inode(directory: int, name: str) -> int | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False).st_ino
    except OSError:
        return None


@dataclass(frozen=True)
class _Seen:
    kind: str  # absent | file | symlink | folder | other
    inode: int = 0
    mode: int = 0
    size: int = 0
    sha: str | None = None


def _look(directory: int, name: str, *, limit: int = MAX_NEW_FILE_BYTES) -> _Seen:
    """What is at `name` (not following a symlink); a regular file is read in full and hashed."""

    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return _Seen("absent")
    kinds = {stat.S_IFREG: "file", stat.S_IFLNK: "symlink", stat.S_IFDIR: "folder"}
    kind = kinds.get(stat.S_IFMT(info.st_mode), "other")
    if kind != "file":
        return _Seen(kind, info.st_ino, stat.S_IMODE(info.st_mode), info.st_size)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return _Seen("absent")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return _Seen("other", opened.st_ino)
        digest, size = hashlib.sha256(), 0
        while size <= limit:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        sha = digest.hexdigest() if size <= limit else None
        return _Seen("file", opened.st_ino, stat.S_IMODE(opened.st_mode), size, sha)
    finally:
        os.close(descriptor)


def _matches(seen: _Seen, sha: str | None, size: int | None, mode: int | None) -> bool:
    return seen.kind == "file" and seen.sha == sha and seen.size == size and seen.mode == mode


@contextlib.contextmanager
def _parent(root: int, root_device: int, path: str):
    """The folder holding `path`, opened component by component with no symlink followed."""

    parts = path.split("/")
    descriptor = os.dup(root)
    try:
        for part in parts[:-1]:
            following = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            if os.fstat(descriptor).st_dev != root_device:
                raise OSError(errno.EXDEV, "another filesystem", path)
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_id(data: bytes, object_format: str) -> str:
    digest = hashlib.new("sha1" if object_format == "sha1" else "sha256")
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------
# COCOLO's state folder: the integration lock, journals, undo sets
# --------------------------------------------------------------------------------------------

def _open_state(state_dir: Path | str, forbidden: tuple[Path, ...], *, create: bool) -> int | None:
    """The state folder's descriptor; None when it is unsafe (or absent and not created)."""

    state_dir = Path(state_dir)
    if not state_dir.is_absolute() or state_dir.name in ("", ".", ".."):
        return None
    parent = Path(os.path.realpath(state_dir.parent))
    location = parent / state_dir.name
    if any(within(location, root) for root in forbidden):
        return None
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(location, 0o700)
        descriptor = os.open(location, _DIRECTORY)
    except OSError:
        return None
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(descriptor)
        return None
    return descriptor


@contextlib.contextmanager
def _integration_lock(state: int, wait: float = LOCK_WAIT_SECONDS):
    descriptor = os.open("merge.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _record(descriptor: int, record: dict) -> None:
    _write_plain(descriptor, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
    _fsync(descriptor)


def _read_journal(state: int, name: str) -> tuple[dict, list[dict]] | None:
    """(intent, events); None when the intent itself was never completely written."""

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=state)
    except OSError:
        return None
    try:
        data = os.read(descriptor, MAX_JOURNAL_BYTES + 1)
    finally:
        os.close(descriptor)
    lines = data.split(b"\n")
    if len(lines) < 2:
        return None
    try:
        intent = json.loads(lines[0])
    except ValueError:
        return None
    events = []
    for line in lines[1:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line torn by a crash
        if isinstance(event, dict):
            events.append(event)
    if not isinstance(intent, dict) or intent.get("format") != JOURNAL_FORMAT:
        return None
    return intent, events


def _open_journal_for_append(state: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=state)
    size = os.fstat(descriptor).st_size
    if size and os.pread(descriptor, 1, size - 1) != b"\n":
        _write_plain(descriptor, b"\n")  # end a line torn by a crash; readers skip it
    return descriptor


def _journals(state: int) -> list[tuple[str, dict, list[dict]]]:
    found = []
    for name in sorted(os.listdir(state)):
        if not name.endswith(".journal"):
            continue
        read = _read_journal(state, name)
        if read is not None:
            found.append((name, *read))
    found.sort(key=lambda item: item[1].get("seq", 0))
    return found


def _final_state(events: list[dict]) -> str | None:
    states = [event["state"] for event in events if event.get("e") == "state"]
    return states[-1] if states else None


def _next_seq(state: int) -> int:
    return max((intent.get("seq", 0) for _, intent, _ in _journals(state)), default=0) + 1


def _remove_tree(state: int, name: str) -> None:
    try:
        descriptor = os.open(name, _DIRECTORY, dir_fd=state)
    except OSError:
        return
    try:
        for entry in os.listdir(descriptor):
            with contextlib.suppress(OSError):
                os.unlink(entry, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=state)


# --------------------------------------------------------------------------------------------
# The git fence (§3.4)
# --------------------------------------------------------------------------------------------

def _fence_text(token: str) -> bytes:
    return f"cocolo-merge {token}\n".encode()


def _take_fence(git_dir: str, token: str, wait: float) -> bool:
    deadline = time.monotonic() + max(0.0, wait)
    git = os.open(git_dir, _DIRECTORY)
    try:
        while True:
            try:
                descriptor = os.open(
                    "index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=git,
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
                continue
            try:
                _write_plain(descriptor, _fence_text(token))
                _fsync(descriptor)
            finally:
                os.close(descriptor)
            return True
    finally:
        os.close(git)


def _release_fence(git_dir: str, token: str) -> bool:
    """Remove index.lock only when it holds this token; True when it is gone or was never ours."""

    try:
        git = os.open(git_dir, _DIRECTORY)
    except OSError:
        return False
    try:
        try:
            descriptor = os.open("index.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=git)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            content = os.read(descriptor, 4096)
        finally:
            os.close(descriptor)
        if content != _fence_text(token):
            return False
        os.unlink("index.lock", dir_fd=git)
        _fsync(git)
        return True
    finally:
        os.close(git)


def _busy_problem(git_dir: str) -> str | None:
    for marker in BUSY_MARKERS:
        if os.path.lexists(os.path.join(git_dir, marker)):
            return marker
    return None


# --------------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------------

@dataclass
class _Op:
    path: str
    change: str  # replace | create | remove
    label: str
    temp: str
    before_sha: str | None = None
    before_size: int | None = None
    before_mode: int | None = None
    after_sha: str | None = None
    after_size: int | None = None
    after_mode: int | None = None
    base_blob: str | None = None
    data: bytes | None = field(default=None, repr=False)

    def journal(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if key != "data"}

    @classmethod
    def from_journal(cls, record: dict) -> _Op:
        return cls(**{key: record.get(key) for key in (
            "path", "change", "label", "temp", "before_sha", "before_size", "before_mode",
            "after_sha", "after_size", "after_mode", "base_blob",
        )})


@dataclass
class _Run:
    """One merge or undo: its plan, its journal and what has been done on disk."""

    kind: str
    prefix: str
    ident: str
    task: str
    root: int
    root_device: int
    git_dir: str
    token: str
    ops: list[_Op]
    folders: list[str]
    remove_folders: list[dict]
    state: int
    journal: int | None = None
    temps: dict[int, int] = field(default_factory=dict)
    removing: dict[int, int] = field(default_factory=dict)
    made: dict[int, int] = field(default_factory=dict)
    hook: FaultHook = lambda point, path: None
    current: str | None = None

    def event(self, **record) -> None:
        _record(self.journal, record)

    def folder_temp(self, index: int) -> str:
        return f"{TEMP_PREFIX}{self.ident}-d{index}"

    def stopped(self, reason: str, sentence: str, path: str | None) -> Stopped:
        what = "merge" if self.kind == "merge" else "undo of the merge"
        return Stopped(
            f"{self.prefix}_{reason}",
            f"COCOLO did not finish the {what} of task {self.task}: {sentence} Nothing was changed.",
            path,
        )


def _path_problem(path: str) -> str | None:
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or "\0" in part:
            return "it is not a plain path inside the project"
        if part.lower().rstrip(". ") == ".git" or part.lower().startswith(".git~"):
            return "it is inside git's own folder"
        if part.startswith(TEMP_PREFIX):
            return "it uses a name COCOLO reserves"
    return None


def _inspect_parents(root: int, root_device: int, path: str) -> tuple[int | None, str | None]:
    """(index of the first missing parent component or None, a problem or None)."""

    parts = path.split("/")[:-1]
    descriptor = os.dup(root)
    try:
        for index, part in enumerate(parts):
            try:
                following = os.open(part, _DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                return index, None
            except OSError:
                return None, f"`{'/'.join(parts[:index + 1])}` is a symbolic link or a file, not a folder"
            os.close(descriptor)
            descriptor = following
            if os.fstat(descriptor).st_dev != root_device:
                return None, f"`{'/'.join(parts[:index + 1])}` is on another filesystem"
            if _inode(descriptor, ".git") is not None:
                return None, f"`{'/'.join(parts[:index + 1])}` is inside another repository"
        return None, None
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------------------------
# Applying, checking, rolling back (shared by merge, undo and recovery)
# --------------------------------------------------------------------------------------------

def _apply(run: _Run) -> None:
    """Create the folders, then apply every file in order; raises _Stop or OSError."""

    for index, folder in enumerate(run.folders):
        run.current = folder
        with _parent(run.root, run.root_device, folder) as (directory, name):
            temporary = run.folder_temp(index)
            os.mkdir(temporary, 0o755, dir_fd=directory)
            run.made[index] = os.stat(temporary, dir_fd=directory, follow_symlinks=False).st_ino
            run.event(e="folder", i=index, ino=run.made[index])
            try:
                atomic_rename.rename_noreplace(directory, temporary, directory, name)
            except FileExistsError:
                raise _Stop("path_taken", folder, f"a folder `{folder}` appeared while COCOLO was working.") from None
            _fsync(directory)
        run.hook("directory_created", folder)
    for index, op in enumerate(run.ops):
        run.current = op.path
        with _parent(run.root, run.root_device, op.path) as (directory, name):
            _apply_one(run, index, op, directory, name)
            _fsync(directory)
        run.event(e="done", i=index)
        run.hook("file_done", op.path)


def _apply_one(run: _Run, index: int, op: _Op, directory: int, name: str) -> None:
    changed = f"`{op.path}` changed while COCOLO was working; the change was put back."
    if op.change == "remove":
        seen = _look(directory, name)
        if not _matches(seen, op.before_sha, op.before_size, op.before_mode):
            raise _Stop("owner_changed", op.path, f"`{op.path}` is not what COCOLO expected.")
        run.removing[index] = seen.inode
        run.event(e="removing", i=index, ino=seen.inode)
        atomic_rename.rename_noreplace(directory, name, directory, op.temp)
        run.hook("exchanged", op.path)
        kept = _look(directory, op.temp)
        if kept.inode != seen.inode or not _matches(kept, op.before_sha, op.before_size, op.before_mode):
            raise _Stop("owner_changed", op.path, changed)
        return
    inode = _write_temp(directory, op.temp, op.data, op.after_mode)
    run.temps[index] = inode
    run.event(e="temp", i=index, ino=inode)
    run.hook("temp_written", op.path)
    if op.change == "create":
        try:
            atomic_rename.rename_noreplace(directory, op.temp, directory, name)
        except FileExistsError:
            raise _Stop(
                "path_taken", op.path,
                f"something appeared at `{op.path}` (or at a name differing only in letter case).",
            ) from None
        run.hook("exchanged", op.path)
        if _inode(directory, name) != inode:
            raise _Stop("owner_changed", op.path, changed)
        return
    try:
        atomic_rename.exchange(directory, op.temp, directory, name)
    except FileNotFoundError:
        raise _Stop("owner_changed", op.path, f"`{op.path}` was deleted while COCOLO was working.") from None
    run.hook("exchanged", op.path)
    old = _look(directory, op.temp)
    if not _matches(old, op.before_sha, op.before_size, op.before_mode) or _inode(directory, name) != inode:
        raise _Stop("owner_changed", op.path, changed)


def _on_disk_applied(run: _Run) -> bool:
    """Every folder and file is in its new place (by the recorded inodes)."""

    try:
        for index, folder in enumerate(run.folders):
            with _parent(run.root, run.root_device, folder) as (directory, name):
                if index not in run.made or _inode(directory, name) != run.made[index]:
                    return False
        for index, op in enumerate(run.ops):
            with _parent(run.root, run.root_device, op.path) as (directory, name):
                if op.change == "remove":
                    if index not in run.removing or _inode(directory, op.temp) != run.removing[index]:
                        return False
                elif index not in run.temps or _inode(directory, name) != run.temps[index]:
                    return False
    except OSError:
        return False
    return True


def _final_problem(run: _Run) -> str | None:
    """The first path whose target or kept old copy is not as expected; None when all hold."""

    for index, folder in enumerate(run.folders):
        try:
            with _parent(run.root, run.root_device, folder) as (directory, name):
                if _inode(directory, name) != run.made.get(index):
                    return folder
        except OSError:
            return folder
    for index, op in enumerate(run.ops):
        try:
            with _parent(run.root, run.root_device, op.path) as (directory, name):
                target = _look(directory, name)
                if op.change == "remove":
                    kept = _look(directory, op.temp)
                    if target.kind != "absent" or kept.inode != run.removing.get(index) or not _matches(
                        kept, op.before_sha, op.before_size, op.before_mode,
                    ):
                        return op.path
                    continue
                if target.inode != run.temps.get(index) or not _matches(
                    target, op.after_sha, op.after_size, op.after_mode,
                ):
                    return op.path
                if op.change == "replace" and not _matches(
                    _look(directory, op.temp), op.before_sha, op.before_size, op.before_mode,
                ):
                    return op.path
        except OSError:
            return op.path
    return None


def _rollback(run: _Run) -> list[str]:
    """Put every step back, newest first, checking before each; the paths left for the owner."""

    unresolved: list[str] = []
    for index in reversed(range(len(run.ops))):
        op = run.ops[index]
        try:
            with _parent(run.root, run.root_device, op.path) as (directory, name):
                if not _rollback_one(run, index, op, directory, name):
                    unresolved.append(op.path)
                _fsync(directory)
        except FileNotFoundError:
            if index in run.temps or index in run.removing:
                unresolved.append(op.path)
        except OSError:
            unresolved.append(op.path)
    for index in reversed(range(len(run.folders))):
        folder = run.folders[index]
        try:
            with _parent(run.root, run.root_device, folder) as (directory, name):
                temporary = run.folder_temp(index)
                inode = run.made.get(index)
                if inode is not None and _inode(directory, name) == inode:
                    os.rmdir(name, dir_fd=directory)
                elif _look(directory, temporary).kind == "folder" and (
                    inode is None or _inode(directory, temporary) == inode
                ):
                    os.rmdir(temporary, dir_fd=directory)
                _fsync(directory)
        except FileNotFoundError:
            pass
        except OSError:
            unresolved.append(folder)
    return unresolved


def _rollback_one(run: _Run, index: int, op: _Op, directory: int, name: str) -> bool:
    if op.change == "remove":
        inode = run.removing.get(index)
        if inode is None or _inode(directory, op.temp) != inode:
            return True
        try:
            atomic_rename.rename_noreplace(directory, op.temp, directory, name)
        except FileExistsError:
            return False  # the owner made a new file there: both are kept
        return True
    inode = run.temps.get(index)
    if inode is None:
        # Never recorded: a temporary is removed only when it is exactly the new content.
        if _matches(_look(directory, op.temp), op.after_sha, op.after_size, op.after_mode):
            os.unlink(op.temp, dir_fd=directory)
        return True
    if op.change == "create":
        if _inode(directory, name) == inode:
            if not _matches(_look(directory, name), op.after_sha, op.after_size, op.after_mode):
                return False
            os.unlink(name, dir_fd=directory)
        elif _inode(directory, op.temp) == inode:
            os.unlink(op.temp, dir_fd=directory)
        return True
    if _inode(directory, name) == inode:
        if _look(directory, op.temp).kind == "absent":
            return False
        atomic_rename.exchange(directory, op.temp, directory, name)
    if _inode(directory, op.temp) == inode:
        if not _matches(_look(directory, op.temp), op.after_sha, op.after_size, op.after_mode):
            return False
        os.unlink(op.temp, dir_fd=directory)
        return True
    # Our new file is gone (the owner saved over it by rename); what is left under the temporary
    # name is the owner's previous version, removed only when it is exactly the base.
    left = _look(directory, op.temp)
    if left.kind == "absent":
        return True
    if _matches(left, op.before_sha, op.before_size, op.before_mode):
        os.unlink(op.temp, dir_fd=directory)
        return True
    return False


def _finish(run: _Run) -> tuple[list[str], list[str]]:
    """After the final check: remove the kept old copies (each checked once more) and, for an undo,
    the folders the merge created. (kept copies, kept folders)."""

    kept: list[str] = []
    for index, op in enumerate(run.ops):
        if op.change == "create":
            continue
        try:
            with _parent(run.root, run.root_device, op.path) as (directory, _name):
                old = _look(directory, op.temp)
                if old.kind == "absent":
                    continue
                expected = run.removing.get(index) if op.change == "remove" else None
                if (expected is None or old.inode == expected) and _matches(
                    old, op.before_sha, op.before_size, op.before_mode,
                ):
                    os.unlink(op.temp, dir_fd=directory)
                    _fsync(directory)
                else:
                    kept.append(op.path)
        except OSError:
            kept.append(op.path)
    folders_kept: list[str] = []
    for folder in sorted(run.remove_folders, key=lambda item: item["path"], reverse=True):
        try:
            with _parent(run.root, run.root_device, folder["path"]) as (directory, name):
                if _inode(directory, name) != folder["ino"]:
                    folders_kept.append(folder["path"])
                    continue
                os.rmdir(name, dir_fd=directory)
                _fsync(directory)
        except FileNotFoundError:
            continue
        except OSError:
            folders_kept.append(folder["path"])
    return kept, folders_kept


def _execute(run: _Run, *, fence_wait: float, recheck: Callable[[], _Stop | None]) -> Stopped | tuple:
    """Fence, apply, check, finish. On any stop: roll back, release, record; nothing changed."""

    if not _take_fence(run.git_dir, run.token, fence_wait):
        run.event(e="state", state="stopped", reason="git_busy")
        return run.stopped(
            "git_busy", "git is busy in this project (another git command holds its lock). "
            "Try again when it has finished.", None,
        )
    try:
        problem = recheck()
        if problem is not None:
            raise problem
        run.hook("fenced", None)
        _apply(run)
        run.hook("all_done", None)
        path = _final_problem(run)
        if path is not None:
            raise _Stop("owner_changed", path, f"`{path}` changed while COCOLO was working; the change was put back.")
    except _Stop as stop:
        return _stop(run, stop.reason, stop.sentence, stop.path)
    except Exception as error:  # noqa: BLE001 -- any failure puts everything back
        code = getattr(error, "errno", None) or errno.EIO
        unsupported = isinstance(error, OSError) and atomic_rename.is_unsupported(error)
        reason = "fs_unsupported" if unsupported else "failed"
        path = run.current
        sentence = (
            "this folder's filesystem cannot replace files atomically." if unsupported
            else f"a file could not be written ({os.strerror(code)})"
            + (f" while working on `{path}`." if path else ".")
        )
        return _stop(run, reason, sentence, path)
    run.event(e="applied")
    run.hook("applied", None)
    kept, folders_kept = _finish(run)
    run.hook("finished", None)
    _release_fence(run.git_dir, run.token)
    run.event(e="state", state="merged" if run.kind == "merge" else "undone", kept=kept, folders_kept=folders_kept)
    return kept, folders_kept


def _stop(run: _Run, reason: str, sentence: str, path: str | None) -> Stopped:
    unresolved = _rollback(run)
    _release_fence(run.git_dir, run.token)
    if unresolved:
        run.event(e="state", state="needs_owner", reason=reason, path=path, unresolved=unresolved)
        listed = ", ".join(f"`{item}`" for item in unresolved)
        return Stopped(
            f"{run.prefix}_needs_owner",
            f"COCOLO stopped task {run.task} ({sentence}) and could not put back {listed} without "
            "overwriting something; those files were left as they are for you to check.",
            path,
        )
    run.event(e="state", state="stopped", reason=reason, path=path)
    return run.stopped(reason, sentence, path)


# --------------------------------------------------------------------------------------------
# Git reads for the preconditions (every call only reads; slice 1's freshening rule holds)
# --------------------------------------------------------------------------------------------

def _git(executable: str, cwd: Path, *arguments: str, private: hg.PrivateRepository | None = None,
         limit: int = hg.MAX_OUTPUT_BYTES, text: bool = True) -> subprocess.CompletedProcess:
    return hg.run_hardened_git(executable, cwd, *arguments, private=private, text=text, max_output_bytes=limit)


def _config(executable: str, root: Path, key: str) -> str | None:
    completed = _git(executable, root, "config", "--get", key, limit=65_536)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() in TRUE_VALUES


def _head(executable: str, root: Path) -> tuple[str, str] | None:
    found = []
    for name in ("HEAD^{commit}", "HEAD^{tree}"):
        completed = _git(executable, root, "rev-parse", "--verify", "--quiet", "--end-of-options", name, limit=4096)
        if completed.returncode != 0 or len(completed.stdout.split()) != 1:
            return None
        found.append(completed.stdout.strip())
    return found[0], found[1]


@dataclass(frozen=True)
class _Entry:
    path: str
    status: str
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str


def _changed_entries(executable: str, reader: hg.PrivateRepository, base_tree: str, tree: str) -> list[_Entry] | None:
    completed = _git(executable, reader.git_dir, "diff-tree", *hg.DIFF_TREE_OPTIONS, base_tree, tree, private=reader)
    if completed.returncode != 0:
        return None
    fields = completed.stdout.split("\0")
    entries, index = [], 0
    while index < len(fields) and fields[index]:
        header = fields[index]
        if not header.startswith(":") or index + 1 >= len(fields) or not fields[index + 1]:
            return None
        parts = header[1:].split(" ")
        if len(parts) != 5:
            return None
        entries.append(_Entry(fields[index + 1], parts[4], parts[0], parts[1], parts[2], parts[3]))
        index += 2
    return sorted(entries, key=lambda entry: entry.path)


def _converted(executable: str, cwd: Path, tree: str, paths: list[str],
               private: hg.PrivateRepository | None) -> dict[str, str] | None:
    """Path -> the first attribute that makes git convert it, read from `tree`; None on failure."""

    completed = _git(
        executable, cwd, "check-attr", "-z", f"--source={tree}", *CONVERTING_ATTRIBUTES, "--", *paths,
        private=private,
    )
    if completed.returncode != 0:
        return None
    fields = completed.stdout.split("\0")
    found: dict[str, str] = {}
    for index in range(0, len(fields) - 2, 3):
        path, attribute, value = fields[index:index + 3]
        if value not in ("unspecified", "unset"):
            found.setdefault(path, attribute if value == "set" else f"{attribute}={value}")
    return found


def _skip_worktree(executable: str, root: Path, paths: list[str]) -> str | None:
    completed = _git(executable, root, "ls-files", *hg.LS_FILES_OPTIONS, *paths)
    if completed.returncode != 0:
        return paths[0]
    for record in filter(None, completed.stdout.split("\0")):
        tag, _, path = record.partition(" ")
        if tag in ("S", "s"):
            return path
    return None


def _read_blob(executable: str, reader: hg.PrivateRepository, oid: str, limit: int,
               object_format: str) -> bytes | None:
    """The blob's bytes, checked against its id; None when it is over `limit` (GitOutputLimitError)."""

    try:
        completed = _git(executable, reader.git_dir, "cat-file", "blob", oid, private=reader, limit=limit, text=False)
    except GitOutputLimitError:
        return None
    if completed.returncode != 0 or _blob_id(completed.stdout, object_format) != oid:
        raise OSError(errno.EIO, "a file of the change could not be read from git")
    return completed.stdout


def _owner(executable: str | None, project_root: Path, prefix: str) -> hg._Owner | Stopped:
    problem = hg.git_version_problem(executable)
    if problem is not None:
        return Stopped(problem.reason, f"{problem.message} Nothing was changed.")
    owner = hg._owner_repository(executable, project_root, None)
    if isinstance(owner, hg.Refusal):
        return Stopped(owner.reason, owner.message)
    return owner


def _plain(reason: str, sentence: str, path: str | None = None) -> Stopped:
    return Stopped(reason, f"{sentence} Nothing was changed.", path)


def _locked_call(prefix: str, executable: str | None, project_root, state_dir, work) -> Stopped | object:
    """Git and repository checks, the state folder, the integration lock, recovery first, then `work`."""

    project_root = Path(project_root)
    if executable is None:
        executable = git_executable([project_root])
    try:
        owner = _owner(executable, project_root, prefix)
        if isinstance(owner, Stopped):
            return owner
        state = _open_state(state_dir, (owner.root, owner.git_dir, owner.common_dir), create=True)
        if state is None:
            return _plain(
                f"{prefix}_state_unusable",
                "COCOLO's merge state folder cannot be used: it must be a private folder outside the "
                "project's repository.",
            )
        try:
            with _integration_lock(state) as locked:
                if not locked:
                    return _plain(
                        f"{prefix}_busy",
                        "Another merge or undo in this project is still running. Try again when it has finished.",
                    )
                _recover_locked(state, owner.root)
                return work(executable, owner, state)
        finally:
            os.close(state)
    except subprocess.TimeoutExpired:
        return _plain(f"{prefix}_failed", "Git did not answer in time.")
    except (OSError, UnicodeError, subprocess.SubprocessError, hg.GitCallRefused):
        return _plain(f"{prefix}_failed", "COCOLO could not read what it needed from the project.")


# --------------------------------------------------------------------------------------------
# The merge (M3-21)
# --------------------------------------------------------------------------------------------

def merge_candidate(
    project_root: Path | str,
    state_dir: Path | str,
    candidate: hg.CandidateTree,
    *,
    task: str,
    executable: str | None = None,
    git_busy_wait: float = GIT_BUSY_WAIT_SECONDS,
    fault_hook: FaultHook | None = None,
) -> Merged | Stopped:
    """Merge the candidate tree's change into the project's working directory, all or nothing.

    `state_dir` is COCOLO's private folder for this project's merges (outside the repository):
    journals, undo sets and the integration lock. `fault_hook(point, path)` is for tests only.
    """

    if type(task) is not str or not task or len(task) > 200 or any(ord(c) < 0x20 for c in task):
        return _plain("write_merge_invalid", "The task name is not usable.")
    if not isinstance(candidate, hg.CandidateTree):
        return _plain("write_merge_invalid", "The change to merge is not a checked candidate.")
    hook = fault_hook or (lambda point, path: None)

    def work(executable: str, owner: hg._Owner, state: int) -> Merged | Stopped:
        return _merge_locked(executable, owner, state, candidate, task, git_busy_wait, hook)

    return _locked_call("write_merge", executable, project_root, state_dir, work)


def _merge_locked(executable: str, owner: hg._Owner, state: int, candidate: hg.CandidateTree, task: str,
                  wait: float, hook: FaultHook) -> Merged | Stopped:
    ident = secrets.token_hex(16)

    def stop(reason: str, sentence: str, path: str | None = None) -> Stopped:
        return _plain(f"write_merge_{reason}", f"COCOLO did not merge task {task}: {sentence}", path)

    store_path = Path(candidate.store)
    if candidate.object_format != owner.object_format or not store_path.is_dir():
        return stop("candidate_missing", "the checked change is no longer in COCOLO's private store.")
    store = hg._open_store(store_path, owner)
    if isinstance(store, hg.Refusal):
        return Stopped(store.reason, store.message)
    marker = _busy_problem(str(owner.git_dir))
    if marker is not None:
        return stop("repo_busy", _busy_sentence(marker))
    head = _head(executable, owner.root)
    if head != (candidate.base_commit, candidate.base_tree):
        return stop("base_changed", _moved_sentence(candidate.base_commit, head))
    reader = hg.PrivateRepository(store, None, (owner.objects,))
    entries = _changed_entries(executable, reader, candidate.base_tree, candidate.tree)
    if entries is None:
        return stop("failed", "git could not compare the change with its base.")
    if not entries:
        return stop("nothing_to_merge", "the change makes no difference to the files.")
    if len(entries) > MAX_PATHS:
        return stop("too_large", f"it changes more than {MAX_PATHS} files.")
    for entry in entries:
        problem = _entry_problem(entry)
        if problem is not None:
            return stop(problem[0], problem[1], entry.path)
    paths = [entry.path for entry in entries]
    if _truthy(_config(executable, owner.root, "core.sparseCheckout")):
        return stop("path_unsafe", "the project uses a sparse checkout; merge this change by hand.")
    skipped = _skip_worktree(executable, owner.root, paths)
    if skipped is not None:
        return stop("path_unsafe", f"`{skipped}` is marked skip-worktree in git's index.", skipped)
    for tree, cwd, private in (
        (candidate.base_tree, owner.root, None),
        (candidate.tree, owner.git_dir, hg.PrivateRepository(owner.git_dir, None, (store / "objects",))),
    ):
        converted = _converted(executable, cwd, tree, paths, private)
        if converted is None:
            return stop("failed", "git could not read the change's attributes.")
        for path in paths:
            if path in converted:
                return stop(
                    "transformed_path",
                    f"git converts `{path}` when it writes it to the folder ({converted[path]}), so COCOLO "
                    "cannot compare it byte for byte; merge this change by hand.", path,
                )
    if _truthy(_config(executable, owner.root, "core.autocrlf")):
        return stop(
            "transformed_path",
            "the project sets core.autocrlf, so git converts line endings in the folder; merge this "
            "change by hand.",
        )
    exec_bits = _config(executable, owner.root, "core.fileMode") not in ("false", "no", "off", "0")
    root = os.open(owner.root, _DIRECTORY)
    try:
        return _merge_prepared(
            executable, owner, state, candidate, task, wait, hook, ident, reader, entries, root, exec_bits, stop,
        )
    finally:
        os.close(root)


def _busy_sentence(marker: str) -> str:
    what = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert",
            "BISECT_LOG": "bisect", "rebase-merge": "rebase", "rebase-apply": "rebase"}[marker]
    return f"a git {what} is in progress in this project; finish or abort it first."


def _moved_sentence(base: str, head: tuple[str, str] | None) -> str:
    now = head[0][:12] if head else "an unreadable commit"
    return (
        f"the project's commit moved from {base[:12]} (where the change was built and tested) to "
        f"{now}; the change has to be rebuilt and tested on the new commit first."
    )


def _entry_problem(entry: _Entry) -> tuple[str, str] | None:
    problem = _path_problem(entry.path)
    if problem is not None:
        return "path_unsafe", f"`{entry.path}`: {problem}."
    modes = {entry.old_mode, entry.new_mode} - {"000000"}
    if "160000" in modes:
        return "path_unsafe", f"`{entry.path}` is a submodule; merge this change by hand."
    if "120000" in modes:
        return "unsupported_change", f"`{entry.path}` is a symbolic link, which COCOLO does not merge."
    if not modes <= set(REGULAR_MODES) or entry.status not in ("A", "M", "D"):
        return "unsupported_change", f"`{entry.path}` changes type, which COCOLO does not merge."
    if entry.status == "M" and entry.old_mode != entry.new_mode:
        return "unsupported_change", f"the change alters the permissions of `{entry.path}`, which COCOLO does not merge."
    return None


def _merge_prepared(executable, owner, state, candidate, task, wait, hook, ident, reader, entries, root,
                    exec_bits, stop) -> Merged | Stopped:
    root_device = os.fstat(root).st_dev
    folders: set[str] = set()
    for entry in entries:
        missing, problem = _inspect_parents(root, root_device, entry.path)
        if problem is not None:
            return stop("path_unsafe", f"{problem}, on the way to `{entry.path}`.", entry.path)
        if missing is not None:
            if entry.status != "A":
                return stop("owner_changed", f"`{entry.path}` is missing from the folder.", entry.path)
            parts = entry.path.split("/")[:-1]
            folders.update("/".join(parts[:end]) for end in range(missing + 1, len(parts) + 1))
    probe = atomic_rename.support_problem(root, f"{TEMP_PREFIX}{ident}-probe-")
    if probe is not None:
        return stop("fs_unsupported", f"this folder's filesystem cannot replace files atomically ({probe}).")
    ops: list[_Op] = []
    undo_set: list[tuple[int, bytes]] = []
    undo_bytes = 0
    for index, entry in enumerate(entries):
        temp = f"{TEMP_PREFIX}{ident}-{index}"
        base = after = None
        if entry.status in ("M", "D"):
            base = _read_blob(executable, reader, entry.old_oid, MAX_FILE_BYTES, owner.object_format)
            if base is None:
                return stop("too_large", f"`{entry.path}` is larger than {MAX_FILE_BYTES} bytes.", entry.path)
            undo_bytes += len(base)
            undo_set.append((index, base))
            if undo_bytes > MAX_UNDO_BYTES:
                return stop("too_large", f"the files it changes total more than {MAX_UNDO_BYTES} bytes.")
        if entry.status in ("A", "M"):
            after = _read_blob(executable, reader, entry.new_oid, MAX_NEW_FILE_BYTES, owner.object_format)
            if after is None:
                return stop("too_large", f"the new `{entry.path}` is larger than {MAX_NEW_FILE_BYTES} bytes.", entry.path)
        seen = _Seen("absent")
        if not (entry.status == "A" and any(entry.path.startswith(f"{folder}/") for folder in folders)):
            with _parent(root, root_device, entry.path) as (directory, name):
                seen = _look(directory, name)
        problem = _target_problem(entry, seen, base, exec_bits, candidate.base_commit)
        if problem is not None:
            return stop(problem[0], problem[1], entry.path)
        if entry.status == "A":
            mode = REGULAR_MODES[entry.new_mode]
            ops.append(_Op(entry.path, "create", "added", temp, after_sha=_sha(after), after_size=len(after),
                           after_mode=mode, data=after))
        elif entry.status == "M":
            ops.append(_Op(entry.path, "replace", "modified", temp, _sha(base), len(base), seen.mode,
                           _sha(after), len(after), seen.mode, entry.old_oid, data=after))
        else:
            ops.append(_Op(entry.path, "remove", "deleted", temp, _sha(base), len(base), seen.mode,
                           base_blob=entry.old_oid))
    undo_name = f"merge-{ident}.undo"
    _store_undo_set(state, undo_name, undo_set)
    ordered = sorted(folders)
    intent = {
        "format": JOURNAL_FORMAT, "kind": "merge", "id": ident, "seq": _next_seq(state), "task": task,
        "project": str(owner.root), "root_ino": os.fstat(root).st_ino, "git_dir": str(owner.git_dir),
        "token": secrets.token_hex(16), "base_commit": candidate.base_commit, "tree": candidate.tree,
        "ops": [op.journal() for op in ops], "folders": ordered, "remove_folders": [],
    }
    run = _Run("merge", "write_merge", ident, task, root, root_device, str(owner.git_dir), intent["token"],
               ops, ordered, [], state, hook=hook)
    journal_name = f"merge-{ident}.journal"
    run.journal = _create_journal(state, journal_name, intent)
    try:
        hook("prepared", None)

        def recheck() -> _Stop | None:
            marker = _busy_problem(str(owner.git_dir))
            if marker is not None:
                return _Stop("repo_busy", None, _busy_sentence(marker))
            head = _head(executable, owner.root)
            if head != (candidate.base_commit, candidate.base_tree):
                return _Stop("base_changed", None, _moved_sentence(candidate.base_commit, head))
            return None

        result = _execute(run, fence_wait=wait, recheck=recheck)
    finally:
        os.close(run.journal)
    if isinstance(result, Stopped):
        if not result.reason.endswith("_needs_owner"):
            _remove_tree(state, undo_name)
        return result
    kept, _folders_kept = result
    return Merged(ident, task, tuple((op.path, op.label) for op in ops), tuple(ordered), tuple(kept))


def _target_problem(entry: _Entry, seen: _Seen, base: bytes | None, exec_bits: bool,
                    base_commit: str) -> tuple[str, str] | None:
    if entry.status == "A":
        if seen.kind == "absent":
            return None
        if seen.kind == "symlink":
            return "path_unsafe", f"`{entry.path}` is a symbolic link in the folder."
        return "path_taken", (
            f"there is already something at `{entry.path}` (or at a name differing from it only in "
            "letter case), and COCOLO never replaces it."
        )
    if seen.kind == "absent":
        return "owner_changed", f"you deleted `{entry.path}` since the child started (from commit {base_commit[:12]})."
    if seen.kind == "symlink":
        return "path_unsafe", f"`{entry.path}` is a symbolic link in the folder."
    if seen.kind != "file" or seen.sha != _sha(base) or seen.size != len(base):
        return "owner_changed", f"you changed `{entry.path}` since the child started (from commit {base_commit[:12]})."
    if exec_bits and bool(seen.mode & 0o100) != (entry.old_mode == "100755"):
        return "owner_changed", f"you changed the permissions of `{entry.path}` since the child started."
    return None


def _store_undo_set(state: int, name: str, items: list[tuple[int, bytes]]) -> None:
    os.mkdir(name, 0o700, dir_fd=state)
    directory = os.open(name, _DIRECTORY, dir_fd=state)
    try:
        for index, data in items:
            descriptor = os.open(
                str(index), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory,
            )
            try:
                _write_plain(descriptor, data)
                _fsync(descriptor)
            finally:
                os.close(descriptor)
        _fsync(directory)
    finally:
        os.close(directory)
    _fsync(state)


def _read_undo_item(state: int, name: str, index: int, sha: str) -> bytes | None:
    try:
        directory = os.open(name, _DIRECTORY, dir_fd=state)
    except OSError:
        return None
    try:
        descriptor = os.open(str(index), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            data = os.read(descriptor, MAX_UNDO_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    finally:
        os.close(directory)
    return data if _sha(data) == sha else None


def _create_journal(state: int, name: str, intent: dict) -> int:
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=state)
    try:
        _record(descriptor, intent)
        _fsync(state)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


# --------------------------------------------------------------------------------------------
# The undo (M3-22)
# --------------------------------------------------------------------------------------------

def undo_merge(
    project_root: Path | str,
    state_dir: Path | str,
    merge_id: str,
    *,
    executable: str | None = None,
    git_busy_wait: float = GIT_BUSY_WAIT_SECONDS,
    fault_hook: FaultHook | None = None,
) -> Undone | Stopped:
    """Put back exactly what a kept merge changed; refused when anything it wrote changed since."""

    hook = fault_hook or (lambda point, path: None)

    def work(executable: str, owner: hg._Owner, state: int) -> Undone | Stopped:
        return _undo_locked(owner, state, merge_id, git_busy_wait, hook)

    return _locked_call("write_undo", executable, project_root, state_dir, work)


def _effective_merges(state: int) -> list[tuple[str, dict, list[dict], str]]:
    journals = _journals(state)
    undone = {
        intent.get("merge_id") for _, intent, events in journals
        if intent.get("kind") == "undo" and _final_state(events) == "undone"
    }
    found = []
    for name, intent, events in journals:
        if intent.get("kind") != "merge":
            continue
        current = _final_state(events) or "unfinished"
        if current == "merged" and intent.get("id") in undone:
            current = "undone"
        found.append((name, intent, events, current))
    return found


def _undo_locked(owner: hg._Owner, state: int, merge_id: str, wait: float, hook: FaultHook) -> Undone | Stopped:
    def stop(reason: str, sentence: str, path: str | None = None) -> Stopped:
        return _plain(f"write_undo_{reason}", f"COCOLO did not undo the merge: {sentence}", path)

    if type(merge_id) is not str or _ID.fullmatch(merge_id) is None:
        return stop("not_kept", "there is no kept merge with that id.")
    merges = _effective_merges(state)
    target = next((item for item in merges if item[1].get("id") == merge_id), None)
    if target is None or target[3] != "merged" or target[1].get("project") != str(owner.root):
        return stop("not_kept", "there is no kept merge with that id (it may have been undone already).")
    _, intent, events, _ = target
    task = intent["task"]
    merged_ops = [_Op.from_journal(record) for record in intent["ops"]]
    paths = {op.path for op in merged_ops}
    later = [
        item for item in merges
        if item[3] == "merged" and item[1].get("seq", 0) > intent.get("seq", 0)
        and paths & {record["path"] for record in item[1]["ops"]}
    ]
    if later:
        newest = max(later, key=lambda item: item[1].get("seq", 0))
        shared = sorted(paths & {record["path"] for record in newest[1]["ops"]})
        return stop(
            "order",
            f"task {newest[1]['task']} was merged after task {task} and changed the same files "
            f"(`{shared[0]}`); undo task {newest[1]['task']} first.", shared[0],
        )
    marker = _busy_problem(str(owner.git_dir))
    if marker is not None:
        return stop("repo_busy", _busy_sentence(marker))
    made = {event["i"]: event["ino"] for event in events if event.get("e") == "folder"}
    remove_folders = [
        {"path": folder, "ino": made[index]} for index, folder in enumerate(intent.get("folders", [])) if index in made
    ]
    undo_name = f"merge-{merge_id}.undo"
    ident = secrets.token_hex(16)
    root = os.open(owner.root, _DIRECTORY)
    try:
        if os.fstat(root).st_ino != intent.get("root_ino"):
            return stop("not_kept", "the project folder is not the one the merge changed.")
        root_device = os.fstat(root).st_dev
        ops: list[_Op] = []
        folders: set[str] = set()
        for index, merged in enumerate(merged_ops):
            temp = f"{TEMP_PREFIX}{ident}-{index}"
            missing, problem = _inspect_parents(root, root_device, merged.path)
            if problem is not None:
                return stop("path_unsafe", f"{problem}, on the way to `{merged.path}`.", merged.path)
            changed = f"you changed `{merged.path}` since task {task} merged it."
            if missing is not None:
                if merged.change != "remove":
                    return stop("owner_changed", changed, merged.path)
                parts = merged.path.split("/")[:-1]
                folders.update("/".join(parts[:end]) for end in range(missing + 1, len(parts) + 1))
                seen = _Seen("absent")
            else:
                with _parent(root, root_device, merged.path) as (directory, name):
                    seen = _look(directory, name)
            if merged.change == "remove":
                if seen.kind != "absent":
                    return stop("owner_changed", changed, merged.path)
                data = _read_undo_item(state, undo_name, index, merged.before_sha)
                if data is None:
                    return stop("failed", "COCOLO's copy of the original files is damaged or missing.")
                ops.append(_Op(merged.path, "create", "re-created", temp, after_sha=merged.before_sha,
                               after_size=merged.before_size, after_mode=merged.before_mode, data=data))
                continue
            if not _matches(seen, merged.after_sha, merged.after_size, merged.after_mode):
                return stop("owner_changed", changed, merged.path)
            if merged.change == "create":
                ops.append(_Op(merged.path, "remove", "removed", temp, merged.after_sha, merged.after_size,
                               merged.after_mode))
                continue
            data = _read_undo_item(state, undo_name, index, merged.before_sha)
            if data is None:
                return stop("failed", "COCOLO's copy of the original files is damaged or missing.")
            ops.append(_Op(merged.path, "replace", "restored", temp, merged.after_sha, merged.after_size,
                           merged.after_mode, merged.before_sha, merged.before_size, merged.before_mode,
                           merged.base_blob, data=data))
        probe = atomic_rename.support_problem(root, f"{TEMP_PREFIX}{ident}-probe-")
        if probe is not None:
            return stop("fs_unsupported", f"this folder's filesystem cannot replace files atomically ({probe}).")
        ordered = sorted(folders)
        undo_intent = {
            "format": JOURNAL_FORMAT, "kind": "undo", "id": ident, "seq": _next_seq(state), "task": task,
            "merge_id": merge_id, "project": str(owner.root), "root_ino": os.fstat(root).st_ino,
            "git_dir": str(owner.git_dir), "token": secrets.token_hex(16),
            "ops": [op.journal() for op in ops], "folders": ordered, "remove_folders": remove_folders,
        }
        run = _Run("undo", "write_undo", ident, task, root, root_device, str(owner.git_dir), undo_intent["token"],
                   ops, ordered, remove_folders, state, hook=hook)
        run.journal = _create_journal(state, f"undo-{ident}.journal", undo_intent)
        try:
            hook("prepared", None)

            def recheck() -> _Stop | None:
                marker = _busy_problem(str(owner.git_dir))
                return None if marker is None else _Stop("repo_busy", None, _busy_sentence(marker))

            result = _execute(run, fence_wait=wait, recheck=recheck)
        finally:
            os.close(run.journal)
    finally:
        os.close(root)
    if isinstance(result, Stopped):
        return result
    kept, folders_kept = result
    _remove_tree(state, undo_name)
    return Undone(ident, merge_id, task, tuple((op.path, op.label) for op in ops), tuple(folders_kept), tuple(kept))


# --------------------------------------------------------------------------------------------
# Crash recovery (M3-27) and the record of merges
# --------------------------------------------------------------------------------------------

def recover(project_root: Path | str, state_dir: Path | str) -> tuple[Recovered, ...]:
    """Finish every interrupted merge or undo of this project: roll back, or complete when every
    file was already written. Needs no git. Run before anything else when the service starts."""

    root = Path(os.path.realpath(project_root))
    state = _open_state(state_dir, (root,), create=False)
    if state is None:
        return ()
    try:
        with _integration_lock(state) as locked:
            if not locked:
                return ()
            return _recover_locked(state, root)
    finally:
        os.close(state)


def _recover_locked(state: int, root: Path) -> tuple[Recovered, ...]:
    outcomes = []
    for name in sorted(os.listdir(state)):
        if name.endswith(".journal") and _read_journal(state, name) is None:
            # The intent was never completely written, so nothing in the folder changed.
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=state)
    for name, intent, events in _journals(state):
        if _final_state(events) is None and intent.get("project") == str(root):
            outcomes.append(_recover_one(state, name, intent, events))
    journals = {intent.get("id") for _, intent, _ in _journals(state) if intent.get("kind") == "merge"}
    for name in os.listdir(state):
        if name.startswith("merge-") and name.endswith(".undo") and name[6:-5] not in journals:
            _remove_tree(state, name)
    return tuple(outcomes)


def _recover_one(state: int, name: str, intent: dict, events: list[dict]) -> Recovered:
    kind, task = intent.get("kind", "merge"), intent.get("task", "")
    what = "merge" if kind == "merge" else "undo of the merge"
    try:
        root = os.open(intent["project"], _DIRECTORY)
    except OSError:
        return Recovered(intent.get("id", ""), kind, task, "needs_owner", (),
                         f"The interrupted {what} of task {task} could not be checked: its project folder is missing.")
    journal = _open_journal_for_append(state, name)
    try:
        if os.fstat(root).st_ino != intent.get("root_ino"):
            return Recovered(intent["id"], kind, task, "needs_owner", (),
                             f"The interrupted {what} of task {task} was for a project folder that has been replaced.")
        run = _Run(
            kind, "write_merge" if kind == "merge" else "write_undo", intent["id"], task, root,
            os.fstat(root).st_dev, intent["git_dir"], intent["token"],
            [_Op.from_journal(record) for record in intent["ops"]], list(intent.get("folders", [])),
            list(intent.get("remove_folders", [])), state, journal,
        )
        for event in events:
            if event.get("e") == "temp":
                run.temps[event["i"]] = event["ino"]
            elif event.get("e") == "removing":
                run.removing[event["i"]] = event["ino"]
            elif event.get("e") == "folder":
                run.made[event["i"]] = event["ino"]
        applied = any(event.get("e") == "applied" for event in events)
        if not applied and run.ops and _on_disk_applied(run) and _final_problem(run) is None:
            run.event(e="applied")
            applied = True
        if applied:
            kept, folders_kept = _finish(run)
            _release_fence(run.git_dir, run.token)
            run.event(e="state", state="merged" if kind == "merge" else "undone", kept=kept,
                      folders_kept=folders_kept, recovered=True)
            if kind == "undo":
                _remove_tree(state, f"merge-{intent.get('merge_id')}.undo")
            return Recovered(intent["id"], kind, task, "completed", tuple(kept),
                             f"The {what} of task {task} was interrupted after every file was written; it was completed.")
        unresolved = _rollback(run)
        _release_fence(run.git_dir, run.token)
        if unresolved:
            run.event(e="state", state="needs_owner", unresolved=unresolved, recovered=True)
            listed = ", ".join(f"`{path}`" for path in unresolved)
            return Recovered(intent["id"], kind, task, "needs_owner", tuple(unresolved),
                             f"The {what} of task {task} was interrupted and put back, except {listed}, which "
                             "COCOLO could not put back without overwriting something; check those files.")
        run.event(e="state", state="interrupted", recovered=True)
        if kind == "merge":
            _remove_tree(state, f"merge-{intent['id']}.undo")
        return Recovered(intent["id"], kind, task, "rolled_back", (),
                         f"The {what} of task {task} was interrupted and undone; nothing changed. Run it again.")
    finally:
        os.close(journal)
        os.close(root)


def kept_merges(state_dir: Path | str, *, include_finished: bool = False) -> tuple[MergeRecord, ...]:
    """The merges recorded in this state folder, oldest first: by default only those kept for undo."""

    state = _open_state(state_dir, (), create=False)
    if state is None:
        return ()
    try:
        records = []
        for _name, intent, _events, current in _effective_merges(state):
            if include_finished or current == "merged":
                records.append(MergeRecord(
                    intent.get("id", ""), intent.get("task", ""), intent.get("seq", 0), current,
                    tuple(record.get("path", "") for record in intent.get("ops", [])),
                ))
        return tuple(records)
    finally:
        os.close(state)
