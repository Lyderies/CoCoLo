"""Hardened git for COCOLO's own git work, and the candidate tree built in a private object store.

Requirements (DESIGN-0.4.0-M3.md draft 3, slice 1):
- M3-16 Every call is hardened. System and global configuration are ignored, the caller's
  environment is never inherited (a fixed environment replaces it), and command-line settings,
  which beat the repository's own configuration, switch off hooks, fsmonitor, the pager, ssh,
  credential helpers, signing and the global attributes file; no transport is allowed. Only an
  allow-list of plumbing runs (`run_hardened_git` refuses anything else before starting git):
  reads (`rev-parse`, `cat-file` without filters or textconv, `ls-tree`, `config --get*`,
  `version`, and for the merge of slice 2 `diff-tree` as a raw tree comparison, `check-attr -z
  --source=<tree id>` and `ls-files -z -v`) anywhere, and the writing plumbing (`read-tree` without `-u`, `apply --cached`,
  `hash-object -w --no-filters`, `write-tree --missing-ok`) only inside COCOLO's private store.
  A call that can write an object never has the owner's objects reachable: git "freshens" (sets
  the modification time of) an existing object it is asked to write, in an alternate object
  directory too, so with the owner's objects as an alternate such a call would touch files in the
  owner's `.git` (found by this slice's before/after map). Git must be 2.50.1 or later (CVE-2025-48384
  and the 2025 fixes); below that, or when its version cannot be read, the answer is a plain
  refusal.
- M3-17 The candidate tree (the base commit plus the patch) is built in a private repository that
  COCOLO owns: its own git directory, configuration and object directory, reading the owner's
  objects through `GIT_ALTERNATE_OBJECT_DIRECTORIES` for reading only. In two phases: with the
  owner's objects reachable, nothing but reads and the private index are written (`read-tree` of
  the base, `apply --cached --check`, the list of paths the patch touches and their base blobs);
  then, with only the private store reachable, those base blobs are copied in (`hash-object -w
  --no-filters`, each checked against its id), the patch is applied to the private index
  (`apply --cached`), and the tree is written (`write-tree --missing-ok`: blobs the patch did not
  touch stay in the owner's objects, read through alternates later). Git never opens the owner's repository as the repository for
  that work, so the owner's configuration (filters, drivers, hooks) is not even read there, and
  nothing is written to the owner's working tree, index, refs, objects or configuration.
- M3-28 (git part) Git missing, too old or of unknown version, and a repository COCOLO cannot
  use (not the top of a normal work tree, bare, a partial clone, an unknown object format) are
  refused with a fixed reason and one plain sentence.
- M3-29 Nothing of COCOLO's is left in the owner's repository: the store may not lie inside it,
  and every temporary file of a build lives in the store and is removed when the build ends.

Evidence: RESEARCH-0.4.0-M3-integration.md §1-2 (the hardened settings, plumbing only, the
tripwire test); DESIGN-0.4.0-M3.md draft 2 §2 F8' (the private store with alternates).
No callers yet (slice 12 wires it). Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .git_plumbing import (
    GIT_ENVIRONMENT,
    GIT_OPTIONS,
    GitOutputLimitError,
    _bounded_git_output,
    git_executable,
    repository_layout_problem,
)
from .host_paths import within

GIT_VERSION_FLOOR = (2, 50, 1)
FLOOR_TEXT = ".".join(str(part) for part in GIT_VERSION_FLOOR)
MAX_PATCH_BYTES = 4 * 1_048_576
MAX_OUTPUT_BYTES = 1_048_576
CALL_TIMEOUT_SECONDS = 60
STORE_MARKER = "cocolo-private-store.json"
STORE_FORMAT = 1
OID_LENGTHS = {"sha1": 40, "sha256": 64}

HARDENED_OPTIONS = (
    *GIT_OPTIONS,
    "-c", "core.sshCommand=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "log.showSignature=false",
    "-c", "core.protectHFS=true",
    "-c", "core.protectNTFS=true",
    "-c", "submodule.recurse=false",
)
HARDENED_ENVIRONMENT = {**GIT_ENVIRONMENT, "GIT_ATTR_NOSYSTEM": "1"}

READ_SUBCOMMANDS = frozenset({
    "version", "rev-parse", "cat-file", "ls-tree", "config", "diff-tree", "check-attr", "ls-files",
})
# The merge's reads (slice 2), in fixed shapes only: a raw tree comparison (no diff program, no
# textconv, no rename detection), attributes read from a tree (not the working tree), and the
# index's skip-worktree flags.
DIFF_TREE_OPTIONS = ("-r", "-z", "--raw", "--no-renames", "--full-index")
LS_FILES_OPTIONS = ("-z", "-v", "--")
WRITE_SUBCOMMANDS = frozenset({"read-tree", "apply", "hash-object", "write-tree"})
CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp"})
APPLY_OPTIONS = frozenset({"--cached", "--check", "--numstat", "-R", "-z", "--whitespace=nowarn"})
# apply options that only report: with one of them, apply writes no object and no index.
APPLY_REPORTS = frozenset({"--check", "--numstat"})
MAX_BASE_BLOB_BYTES = 32 * 1_048_576
# cat-file options that run the repository's filters or textconv programs.
CAT_FILE_HELPERS = ("--textconv", "--filters", "--path")

_VERSION = re.compile(r"git version (\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?(?:[.\- (][^\n]*)?\n?")
_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class GitCallRefused(ValueError):
    """A git call outside the allow-list; git was not started."""


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class CandidateTree:
    tree: str
    base_commit: str
    base_tree: str
    store: Path
    object_format: str

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class PrivateRepository:
    """COCOLO's private repository: its git directory, an optional private index, read-only alternates."""

    git_dir: Path
    index_file: Path | None
    alternates: tuple[Path, ...]

    def environment(self) -> dict[str, str]:
        environment = {**HARDENED_ENVIRONMENT, "GIT_DIR": str(self.git_dir)}
        if self.index_file is not None:
            environment["GIT_INDEX_FILE"] = str(self.index_file)
        if self.alternates:
            environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = ":".join(
                _quoted_alternate(path) for path in self.alternates
            )
        return environment


def _quoted_alternate(path: Path) -> str:
    """One entry of GIT_ALTERNATE_OBJECT_DIRECTORIES, C-quoted so ':' and '"' in folder names are safe."""

    text = str(path)
    if not os.path.isabs(text) or any(ord(character) < 0x20 or character == "\x7f" for character in text):
        raise GitCallRefused("an alternate object directory must be an absolute path without control characters")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _call_problem(arguments: tuple[str, ...], private: PrivateRepository | None) -> str | None:
    if not arguments or any(type(item) is not str or "\0" in item for item in arguments):
        return "a git call needs a subcommand and text arguments"
    subcommand, rest = arguments[0], arguments[1:]
    if subcommand not in READ_SUBCOMMANDS | WRITE_SUBCOMMANDS:
        return f"git {subcommand} is not allowed"
    if subcommand in WRITE_SUBCOMMANDS and (private is None or private.index_file is None):
        return f"git {subcommand} runs only in COCOLO's private store, with its private index"
    if subcommand == "version" and rest:
        return "git version takes no arguments here"
    if subcommand == "write-tree" and rest != ("--missing-ok",):
        return "git write-tree runs only as write-tree --missing-ok"
    if subcommand == "hash-object" and (
        len(rest) != 4 or rest[:3] != ("-w", "--no-filters", "--") or not os.path.isabs(rest[3])
    ):
        return "git hash-object runs only as hash-object -w --no-filters -- <absolute file>"
    if subcommand == "config" and (
        not rest or rest[0] not in CONFIG_READS
        or any(item.startswith("-") and item not in ("-z", "--null") for item in rest[1:])
    ):
        return "only reading git config is allowed"
    if subcommand == "cat-file" and any(item.startswith(CAT_FILE_HELPERS) for item in rest):
        return "git cat-file may not run filters or textconv"
    if subcommand == "diff-tree" and (
        len(rest) != len(DIFF_TREE_OPTIONS) + 2 or rest[:-2] != DIFF_TREE_OPTIONS
        or any(_HEX.fullmatch(item) is None for item in rest[-2:])
    ):
        return "git diff-tree runs only as a raw comparison of two tree ids"
    if subcommand == "check-attr" and not _attribute_read(rest):
        return "git check-attr runs only as check-attr -z --source=<tree id> <attributes> -- <paths>"
    if subcommand == "ls-files" and (len(rest) < 4 or rest[:3] != LS_FILES_OPTIONS):
        return "git ls-files runs only as ls-files -z -v -- <paths>"
    if subcommand == "read-tree" and (not rest or any(item.startswith("-") for item in rest)):
        return "git read-tree takes tree-ish arguments only"
    if subcommand == "apply":
        options = [item for item in rest if item.startswith("-")]
        paths = [item for item in rest if not item.startswith("-")]
        if "--cached" not in options or not set(options) <= APPLY_OPTIONS:
            return "git apply runs only with --cached"
        if "-R" in options and "--numstat" not in options:
            return "git apply -R only lists paths here"
        if len(paths) != 1 or not os.path.isabs(paths[0]):
            return "git apply takes one absolute patch file"
    writes_objects = subcommand in ("hash-object", "write-tree") or (
        subcommand == "apply" and not APPLY_REPORTS & set(rest)
    )
    if writes_objects and private is not None and private.alternates:
        return f"git {subcommand} may not run with the owner's objects reachable"
    return None


def _attribute_read(rest: tuple[str, ...]) -> bool:
    if len(rest) < 5 or rest[0] != "-z" or not rest[1].startswith("--source="):
        return False
    if _HEX.fullmatch(rest[1][len("--source="):]) is None or "--" not in rest[2:]:
        return False
    separator = rest.index("--", 2)
    names, paths = rest[2:separator], rest[separator + 1:]
    return bool(names) and bool(paths) and all(
        re.fullmatch(r"[a-z][a-z0-9-]*", name) is not None for name in names
    )


def run_hardened_git(
    executable: str,
    cwd: Path,
    *arguments: str,
    private: PrivateRepository | None = None,
    text: bool = False,
    timeout: float = CALL_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    deadline: float | None = None,
) -> subprocess.CompletedProcess:
    """Run one allow-listed git plumbing call with the hardened settings; output is bounded.

    With `private`, git works on COCOLO's private repository (its directory is also the working
    directory); without it, on the repository at `cwd`, for reading only. Raises GitCallRefused,
    before starting anything, for a call outside the allow-list.
    """

    problem = _call_problem(arguments, private)
    if problem is not None:
        raise GitCallRefused(problem)
    if private is not None:
        cwd, environment = private.git_dir, private.environment()
    else:
        environment = dict(HARDENED_ENVIRONMENT)
    return _bounded_git_output(
        [executable, *HARDENED_OPTIONS, "-C", str(cwd), *arguments],
        text=text, timeout=timeout, limit=max_output_bytes, deadline=deadline, env=environment,
    )


def parse_git_version(output: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) from `git version` output, or None when it is not that shape."""

    match = _VERSION.fullmatch(output) if type(output) is str else None
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def git_version_problem(executable: str | None, *, deadline: float | None = None) -> Refusal | None:
    """None when `executable` is git 2.50.1 or later; otherwise a plain refusal."""

    if executable is None:
        return _git_missing()
    try:
        completed = run_hardened_git(
            executable, Path("/"), "version", text=True, timeout=10, max_output_bytes=4096,
            deadline=deadline,
        )
    except OSError:
        return _git_missing()
    except (UnicodeError, subprocess.SubprocessError):
        return _git_version_unknown()
    version = parse_git_version(completed.stdout) if completed.returncode == 0 else None
    if version is None:
        return _git_version_unknown()
    if version < GIT_VERSION_FLOOR:
        return Refusal(
            "git_too_old",
            f"COCOLO needs git {FLOOR_TEXT} or later to make changes in a project, because older "
            f"versions have security problems a change could trigger; this computer has git "
            f"{'.'.join(str(part) for part in version)}. Update git, then try again.",
        )
    return None


def _git_missing() -> Refusal:
    return Refusal(
        "git_missing",
        f"COCOLO needs git {FLOOR_TEXT} or later to make changes in a project, and no usable git "
        "was found. Install git (on a Mac: the Xcode Command Line Tools or Homebrew), then try again.",
    )


def _git_version_unknown() -> Refusal:
    return Refusal(
        "git_version_unknown",
        "COCOLO could not tell which version of git is installed, so it will not use it to make "
        "changes. Check that `git --version` works, then try again.",
    )


def _nothing_changed(reason: str, sentence: str) -> Refusal:
    return Refusal(reason, f"{sentence} Nothing was changed.")


def _call_timeout(deadline: float | None) -> float:
    if deadline is None:
        return CALL_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("hardened git", 0)
    return min(CALL_TIMEOUT_SECONDS, remaining)


@dataclass(frozen=True)
class _Owner:
    root: Path
    git_dir: Path
    common_dir: Path
    objects: Path
    object_format: str


def _owner_repository(executable: str, project_root: Path, deadline: float | None) -> _Owner | Refusal:
    def unusable(problem: str) -> Refusal:
        return _nothing_changed(
            "repository_unusable", f"COCOLO cannot build the change in this project's repository: {problem}.",
        )

    problem = repository_layout_problem(executable, project_root, deadline=deadline)
    if problem is not None:
        return unusable(problem)
    completed = run_hardened_git(
        executable, project_root, "rev-parse", "--path-format=absolute", "--git-dir",
        "--git-common-dir", "--show-object-format", text=True, timeout=_call_timeout(deadline),
        max_output_bytes=65_536, deadline=deadline,
    )
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 3:
        return unusable("the project repository is unavailable")
    git_dir, common_dir = (Path(os.path.realpath(line)) for line in lines[:2])
    object_format = lines[2]
    if object_format not in OID_LENGTHS:
        return unusable("it uses an object format COCOLO does not know")
    objects = common_dir / "objects"
    if not objects.is_dir() or any(ord(character) < 0x20 for character in str(objects)):
        return unusable("its object directory cannot be read")
    return _Owner(Path(os.path.realpath(project_root)), git_dir, common_dir, objects, object_format)


def _store_config(object_format: str) -> str:
    if object_format == "sha1":
        return "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
    return (
        "[core]\n\trepositoryformatversion = 1\n\tbare = true\n"
        f"[extensions]\n\tobjectformat = {object_format}\n"
    )


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _open_store(store: Path | str, owner: _Owner) -> Path | Refusal:
    """The private store's directory, created on first use; refused when unsafe or another project's."""

    def unusable(problem: str) -> Refusal:
        return _nothing_changed("store_unusable", f"COCOLO's private store cannot be used: {problem}.")

    store = Path(store)
    if not store.is_absolute() or store.name in ("", ".", ".."):
        return unusable("its location is not an absolute folder path")
    parent = Path(os.path.realpath(store.parent))
    if not parent.is_dir():
        return unusable("its parent folder does not exist")
    location = parent / store.name
    if any(within(location, root) for root in (owner.root, owner.git_dir, owner.common_dir)):
        return _nothing_changed(
            "store_inside_repository",
            "COCOLO's private store may not be inside the project's repository.",
        )
    try:
        info = os.lstat(location)
    except FileNotFoundError:
        info = None
    if info is None:
        try:
            os.mkdir(location, 0o700)
            for name in ("objects", "refs"):
                os.mkdir(location / name, 0o700)
            _write_new(location / "HEAD", b"ref: refs/heads/cocolo-unused\n")
            _write_new(location / "config", _store_config(owner.object_format).encode())
            # The marker goes last: a store without it is incomplete and never used.
            _write_new(location / STORE_MARKER, json.dumps({
                "format": STORE_FORMAT, "objects": str(owner.objects),
                "object_format": owner.object_format,
            }, sort_keys=True).encode())
        except OSError:
            return unusable("it could not be created")
        return location
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        return unusable("it is not a private folder of this user")
    try:
        descriptor = os.open(location / STORE_MARKER, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            marker = json.loads(handle.read(65_536))
    except (OSError, ValueError):
        return unusable("it is not a COCOLO private store")
    if not isinstance(marker, dict) or marker.get("format") != STORE_FORMAT:
        return unusable("it is not a COCOLO private store")
    if marker.get("objects") != str(owner.objects) or marker.get("object_format") != owner.object_format:
        return _nothing_changed(
            "store_other_repository", "This private store belongs to another project's repository.",
        )
    return location


def _touched_paths(*listings: subprocess.CompletedProcess) -> list[str] | None:
    """Every path `apply --numstat -z` names, forward and reversed (so both sides of a rename or
    copy: apply names only the new side), or None."""

    paths: set[str] = set()
    for completed in listings:
        if completed.returncode != 0:
            return None
        for field in filter(None, completed.stdout.split("\0")):
            parts = field.split("\t")
            if len(parts) != 3 or not parts[2]:
                return None
            paths.add(parts[2])
    return sorted(paths)


def _base_blobs(git, base_tree: str, paths: list[str], private: PrivateRepository, oid_length: int) -> list[str] | None:
    """The base tree's blob ids at (or under) the given paths: the preimages the patch reads."""

    listed = git("ls-tree", "-r", "-z", "--full-tree", base_tree, "--", *paths, private=private)
    if listed.returncode != 0:
        return None
    blobs: set[str] = set()
    for entry in filter(None, listed.stdout.split("\0")):
        header, _, _path = entry.partition("\t")
        fields = header.split(" ")
        if len(fields) != 3:
            return None
        if fields[1] == "blob":
            if len(fields[2]) != oid_length or _HEX.fullmatch(fields[2]) is None:
                return None
            blobs.add(fields[2])
    return sorted(blobs)


def build_candidate_tree(
    project_root: Path | str,
    store: Path | str,
    base_commit: str,
    patch: bytes,
    *,
    executable: str | None = None,
    deadline: float | None = None,
) -> CandidateTree | Refusal:
    """The tree of `base_commit` plus `patch` (a git patch), built in COCOLO's private store.

    The store is a folder outside the project's repository; it is created on first use and may be
    reused for the same repository. Reads the owner's objects; writes only in the store. Every
    refusal leaves the owner's repository and the store's contents as they were (objects git may
    have added to the store are unreferenced and harmless).
    """

    project_root = Path(project_root)
    if executable is None:
        executable = git_executable([project_root, Path(store)])
    problem = git_version_problem(executable, deadline=deadline)
    if problem is not None:
        return problem
    if type(patch) is not bytes:
        return _nothing_changed("patch_invalid", "The change is not a patch COCOLO can read.")
    if not patch:
        return _nothing_changed("patch_empty", "The change is empty.")
    if len(patch) > MAX_PATCH_BYTES:
        return _nothing_changed(
            "patch_too_large", f"The change is larger than COCOLO's limit of {MAX_PATCH_BYTES} bytes.",
        )
    if type(base_commit) is not str or _HEX.fullmatch(base_commit) is None:
        return _nothing_changed("base_commit_invalid", "The base commit is not a full commit id.")
    try:
        return _build(executable, project_root, store, base_commit, patch, deadline)
    except subprocess.TimeoutExpired:
        return _nothing_changed("git_failed", "Git did not finish building the change in time.")
    except (OSError, UnicodeError, subprocess.SubprocessError, GitCallRefused):
        return _nothing_changed("git_failed", "Git could not build the change.")


def _build(
    executable: str, project_root: Path, store: Path | str, base_commit: str, patch: bytes,
    deadline: float | None,
) -> CandidateTree | Refusal:
    owner = _owner_repository(executable, project_root, deadline)
    if isinstance(owner, Refusal):
        return owner
    if len(base_commit) != OID_LENGTHS[owner.object_format]:
        return _nothing_changed("base_commit_invalid", "The base commit is not a full commit id.")
    location = _open_store(store, owner)
    if isinstance(location, Refusal):
        return location
    oid_length = OID_LENGTHS[owner.object_format]
    reader = PrivateRepository(location, None, (owner.objects,))

    def git(*arguments: str, private: PrivateRepository = reader) -> subprocess.CompletedProcess:
        # Text output: ids and -z listings; a path that is not UTF-8 refuses the build (git_failed).
        return run_hardened_git(
            executable, location, *arguments, private=private, text=True,
            timeout=_call_timeout(deadline), deadline=deadline,
        )

    kind = git("cat-file", "-t", base_commit)
    if kind.returncode != 0 or kind.stdout.strip() != "commit":
        return _nothing_changed("base_commit_missing", "The base commit is not in this project's repository.")
    resolved = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{base_commit}^{{tree}}")
    base_tree = resolved.stdout.strip()
    if resolved.returncode != 0 or len(base_tree) != oid_length or _HEX.fullmatch(base_tree) is None:
        return _nothing_changed("git_failed", "Git could not read the base commit's tree.")
    token = secrets.token_hex(8)
    index = location / f"index-{token}"
    patch_file = location / f"patch-{token}"
    blob_file = location / f"blob-{token}"
    # Phase 1 sees the owner's objects and writes nothing but the private index; phase 2 writes
    # objects and sees only the private store.
    reading = PrivateRepository(location, index, (owner.objects,))
    writing = PrivateRepository(location, index, ())
    try:
        _write_new(patch_file, patch)
        if git("read-tree", base_tree, private=reading).returncode != 0:
            return _nothing_changed("git_failed", "Git could not read the base commit's tree.")
        checked = git("apply", "--cached", "--check", "--whitespace=nowarn", str(patch_file), private=reading)
        if checked.returncode != 0:
            return Refusal(
                "patch_does_not_apply",
                "The change does not apply to the base commit, so nothing was changed.",
            )
        touched = _touched_paths(*(
            git("apply", "--cached", *reverse, "--numstat", "-z", str(patch_file), private=reading)
            for reverse in ((), ("-R",))
        ))
        if touched is None:
            return _nothing_changed("git_failed", "Git could not list the paths the change touches.")
        blobs = _base_blobs(git, base_tree, touched, reading, oid_length) if touched else []
        if blobs is None:
            return _nothing_changed("git_failed", "Git could not read the files the change touches.")
        for oid in blobs:
            try:
                content = run_hardened_git(
                    executable, location, "cat-file", "blob", oid, private=reading,
                    timeout=_call_timeout(deadline), max_output_bytes=MAX_BASE_BLOB_BYTES, deadline=deadline,
                )
            except GitOutputLimitError:
                return _nothing_changed(
                    "base_file_too_large",
                    f"A file the change touches is larger than COCOLO's limit of {MAX_BASE_BLOB_BYTES} bytes.",
                )
            if content.returncode != 0:
                return _nothing_changed("git_failed", "Git could not read a file the change touches.")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(blob_file)
            _write_new(blob_file, content.stdout)
            copied = git("hash-object", "-w", "--no-filters", "--", str(blob_file), private=writing)
            if copied.returncode != 0 or copied.stdout.strip() != oid:
                return _nothing_changed("git_failed", "Git could not copy a file the change touches.")
        if git("apply", "--cached", "--whitespace=nowarn", str(patch_file), private=writing).returncode != 0:
            return Refusal(
                "patch_does_not_apply",
                "The change does not apply to the base commit, so nothing was changed.",
            )
        written = git("write-tree", "--missing-ok", private=writing)
        tree = written.stdout.strip()
        if written.returncode != 0 or len(tree) != oid_length or _HEX.fullmatch(tree) is None:
            return _nothing_changed("git_failed", "Git could not write the candidate tree.")
    finally:
        for leftover in (patch_file, blob_file, index, Path(f"{index}.lock")):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(leftover)
    return CandidateTree(tree, base_commit, base_tree, location, owner.object_format)
