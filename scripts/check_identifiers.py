#!/usr/bin/env python3
"""Block personal and machine identifiers from entering this public repository.

Findings report only a category, path and line, never the matched text, so CI logs
cannot leak a denylisted value. A path that itself holds a value (an identifier, a denylisted
term, a control or format character, or bytes that are not valid UTF-8) is printed as ``[redacted]``,
and no error message ever names a file. A listed file that cannot be read, a path that leaves the
project, a link above a listed file, a pipe, socket or device, a repository whose configuration moves
its work tree, a run from a subdirectory and an oversize file list are all refusals (exit 2), never a
smaller scan. Owner-specific terms live in a private denylist file
outside the repository (one term per line, ``#`` comments allowed):

- ``$AO_IDENTIFIER_DENYLIST`` when set, otherwise
- ``~/.config/codex-claude-agent-orchestration/identifier-denylist.txt``.

Usage:
    check_identifiers.py                 scan tracked files plus untracked files that are not ignored
                                         (the git file manifest; refuses when git cannot produce it, and
                                         refuses to run anywhere but the top of the repository)
    check_identifiers.py --staged        scan staged content (pre-commit)
    check_identifiers.py --identity      check the configured commit identity
    check_identifiers.py --commits REV   check author/committer emails reachable from REV
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024  # the same bound the QA host puts on the list it hands over
MAX_DENYLIST_BYTES = 1024 * 1024
GIT_SECONDS = 120
DEFAULT_DENYLIST_PARTS = (".config", "codex-claude-agent-orchestration", "identifier-denylist.txt")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home-path", re.compile(r"(?<![\w/])/Users/(?!Shared\b)[A-Za-z0-9._-]+")),
    ("home-path", re.compile(r"(?<![\w/])/home/[A-Za-z0-9._-]+")),
    ("home-path", re.compile(r"\b[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+", re.IGNORECASE)),
    ("hostname", re.compile(r"\b[A-Za-z0-9-]+\.local\b(?!\.)")),
    ("hostname", re.compile(r"\b[A-Za-z0-9]+-MacBook(?:-[A-Za-z0-9]+)*\b")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)
ALLOWED_EMAILS = (
    re.compile(r"^\d+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$"),
    re.compile(r"^noreply@github\.com$"),
    re.compile(r"^noreply@anthropic\.com$"),
    re.compile(r"^[A-Za-z0-9._%+-]+@example\.(?:com|org|net)$"),
    re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]*\.(?:invalid|test|example|localhost)$"),
    re.compile(r"^[A-Za-z0-9._-]+@openssh\.com$"),  # SSH key-type names, not mailboxes
)
# Obvious placeholder account names in documentation and evidence (/Users/x, /home/user). The exemption is
# exact: the whole path component must be one of these lower-case tokens, so "User", "X", "User Smith" and
# a name that merely starts with a token are all findings. A token counts as a whole component only when the
# next character ends it. A path separator, the end of the line and the characters Windows forbids in an
# account name (a double quote, ``<``, ``>``, ``:``) always end it. A character that is legal inside a name
# (an apostrophe, backquote, bracket, comma, semicolon) ends it only as prose punctuation: when what
# follows is the end of the line, a space or more punctuation, never more name (``user'smith`` is an
# account). Anything else, including a space or a non-ASCII letter, might continue a real account name.
ALLOWED_HOME_NAMES = frozenset({"x", "user", "you", "name", "username", "yourname", "example"})
COMPONENT_END = frozenset("/\\\"<>:")
PROSE_PUNCTUATION = frozenset("'`)]},;")
AFTER_PROSE = frozenset(" \t'`)]},;:!?\"<>")
REDACTED = "[redacted]"
# Command-line git, run with no user or system configuration so every machine lists the same files.
GIT_LIST_ARGS = ("ls-files", "-z", "--cached", "--others", "--exclude-per-directory=.gitignore")
MANIFEST_ENVIRONMENT = "AO_QA_FILE_MANIFEST"
# Project namespaces that look like mDNS hostnames but identify no machine.
ALLOWED_HOSTNAMES = frozenset({"codex-agents.local", "claude-agents.local"})


@dataclass(frozen=True)
class Finding:
    category: str
    location: str

    def __str__(self) -> str:
        return f"{self.location}: {self.category}"


class ScanRefused(Exception):
    """The scan cannot be done as specified; it refuses rather than scan fewer files."""


def _terms(text: str) -> tuple[str, ...]:
    terms = (line.strip() for line in text.splitlines())
    return tuple(term.casefold() for term in terms if term and not term.startswith("#"))


def load_denylist(path: Path | None = None) -> tuple[str, ...]:
    """Return lower-cased private terms; a missing file yields no terms, an unreadable one refuses."""

    if path is None:
        configured = os.environ.get("AO_IDENTIFIER_DENYLIST")
        if configured:
            path = Path(configured)
        else:
            try:  # QA's sandbox has no home folder; it is handed AO_IDENTIFIER_DENYLIST instead
                path = Path.home().joinpath(*DEFAULT_DENYLIST_PARTS)
            except (RuntimeError, KeyError):
                raise ScanRefused("the home folder is unknown, so the private denylist cannot be found") from None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError):
        raise ScanRefused("the private denylist exists but cannot be read") from None
    return _terms(text)


def load_required_denylist() -> tuple[str, ...]:
    """The denylist the QA host provisioned, and nothing else.

    QA has no home folder, and the host writes the owner's denylist (an empty file when the owner has none)
    and names it in ``$AO_IDENTIFIER_DENYLIST``. Unset, missing, oversize, unreadable or not UTF-8 is a
    refusal: falling back to a default location, or to no terms, would scan with a weaker list than CI.
    """

    configured = os.environ.get("AO_IDENTIFIER_DENYLIST")
    if not configured:
        raise ScanRefused("the host supplied no private denylist; QA must not scan without the owner's terms")
    try:
        with open(configured, "rb") as handle:
            data = handle.read(MAX_DENYLIST_BYTES + 1)
        if len(data) > MAX_DENYLIST_BYTES:
            raise ScanRefused("the private denylist supplied by the host is too large")
        return _terms(data.decode("utf-8"))
    except (OSError, UnicodeError):
        raise ScanRefused("the private denylist supplied by the host cannot be read") from None


def email_allowed(address: str) -> bool:
    return any(pattern.fullmatch(address) for pattern in ALLOWED_EMAILS)


def _placeholder_home(match: re.Match[str], line: str) -> bool:
    """True only for an exact, lower-case placeholder token that is a whole path component."""

    if re.split(r"[\\/]+", match.group(0))[-1] not in ALLOWED_HOME_NAMES:
        return False
    following = line[match.end():match.end() + 1]
    if following == "" or following in COMPONENT_END:
        return True
    if following in PROSE_PUNCTUATION:
        rest = line[match.end() + 1:]
        return rest == "" or rest[0] in AFTER_PROSE or (rest[0] == "." and rest[1:2] in ("", " ", "\t"))
    return False


def scan_text(text: str, location: str, denylist: tuple[str, ...]) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{location}:{number}"
        for category, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if category == "email" and email_allowed(match.group(0)):
                    continue
                if category == "hostname" and match.group(0).casefold() in ALLOWED_HOSTNAMES:
                    continue
                if category == "home-path" and _placeholder_home(match, line):
                    continue
                findings.append(Finding(category, where))
        folded = line.casefold()
        if any(term in folded for term in denylist):
            findings.append(Finding("denylisted-term", where))
    return findings


def _git_environment() -> dict[str, str]:
    """No GIT_* variables and no user or system git configuration: the same file list on every machine."""

    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "xcrun_nocache": "1",
    })
    return environment


# Repository-local settings that change which files git lists: ignore rules are matched case-sensitively
# whatever a checkout's own config says, so every machine lists what a case-sensitive CI checkout lists.
GIT_FIXED_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.ignorecase=false")


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _git_executable(project: Path | None) -> str:
    """The git on PATH, ignoring relative entries and entries inside the project (project code could plant one)."""

    entries = [entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep) if os.path.isabs(entry)]
    if project is not None:
        top = os.path.realpath(project)
        entries = [entry for entry in entries if not _within(os.path.realpath(entry), top)]
    git = shutil.which("git", path=os.pathsep.join(entries))
    if git is None:
        raise ScanRefused("git is unavailable, so the files to scan cannot be listed")
    return git


def _run_git(
    args: tuple[str | bytes, ...], *, root: Path | None, limit: int, hardened: bool = True,
) -> tuple[int, bytes, bool]:
    """Run git; return (exit status, at most ``limit`` bytes of output, whether there was more).

    Never raises anything that names an argument: every failure is a fixed ScanRefused message. The
    ``hardened`` runs (the file list) use no user or system configuration and no GIT_* variables; the
    staged and commit checks keep the caller's environment, which a pre-commit hook relies on.
    """

    command: list[str | bytes] = [_git_executable(root), *(GIT_FIXED_CONFIG if hardened else ())]
    if root is not None:
        command += ["-C", str(root)]
    command += args
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            env=_git_environment() if hardened else None,
        )
    except (OSError, ValueError):
        raise ScanRefused("git could not be run") from None
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        process.kill()

    timer = threading.Timer(GIT_SECONDS, expire)
    timer.start()
    try:
        data = process.stdout.read(limit + 1)
        over = len(data) > limit
        if over:
            process.kill()
        process.wait()
    except OSError:
        process.kill()
        raise ScanRefused("git output could not be read") from None
    finally:
        timer.cancel()
        process.stdout.close()
    if expired.is_set():
        raise ScanRefused("git took too long")
    return process.returncode, data[:limit], over


def parse_manifest(data: bytes) -> list[bytes]:
    """NUL-separated git path names to a sorted, de-duplicated list; entries ending in ``/`` are nested repositories."""

    return sorted({name for name in data.split(b"\0") if name and not name.endswith(b"/")})


def git_manifest(root: Path) -> list[bytes]:
    """The one file list every identifier scan uses, straight from git: tracked files (even when an
    ignore rule names them) plus untracked files that no ``.gitignore`` ignores.

    Only ``.gitignore`` files count (not ``.git/info/exclude`` or a global ignore file), so a clean
    checkout in CI lists exactly what QA lists. Refuses when git cannot produce the list, when the
    repository's configuration puts its work tree somewhere other than ``root``, or when the list is
    larger than the bound the QA host applies.
    """

    status, data, over = _run_git(("rev-parse", "--show-toplevel"), root=root, limit=65_536)
    if status != 0 or over or os.path.realpath(os.fsdecode(data).strip()) != os.path.realpath(root):
        raise ScanRefused("the project is not the top of its git work tree, so the files to scan cannot be listed")
    status, data, over = _run_git(GIT_LIST_ARGS, root=root, limit=MAX_MANIFEST_BYTES)
    if over:
        raise ScanRefused("the git file list is larger than the scan allows")
    if status != 0:
        raise ScanRefused("git could not list the files to scan (not a repository?)")
    return parse_manifest(data)


def read_manifest(path: Path) -> list[bytes]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise ScanRefused("the file manifest supplied by the host cannot be read") from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise ScanRefused("the file manifest supplied by the host is larger than the scan allows")
    return parse_manifest(data)


def manifest_from_environment() -> list[bytes]:
    """The manifest the QA host wrote from git before the sandbox started (QA cannot run git itself)."""

    configured = os.environ.get(MANIFEST_ENVIRONMENT)
    if not configured:
        raise ScanRefused("the host supplied no git file manifest; QA must not scan a smaller set of files")
    return read_manifest(Path(configured))


def _decode(data: bytes) -> str | None:
    if len(data) > MAX_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def list_scan_files(root: Path, *, manifest: list[bytes] | None = None) -> list[str]:
    """Display form of the scan manifest (git's when none is given); for tests and reporting."""

    return [os.fsdecode(name) for name in (git_manifest(root) if manifest is None else manifest)]


def _display_name(raw: bytes, denylist: tuple[str, ...]) -> str:
    """The path as printed: itself only if it is valid UTF-8, passes the scan and holds no control,
    format, surrogate or unassigned character (which could hide or reorder what is printed)."""

    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return REDACTED
    if any(unicodedata.category(ch)[0] == "C" for ch in name) or scan_text(name, "", denylist):
        return REDACTED
    return name


def _scan_named(raw: bytes, content: Callable[[], bytes | None], denylist: tuple[str, ...]) -> list[Finding]:
    """Scan one listed path: its name, then its text. A name that is not valid UTF-8 is a finding."""

    name = os.fsdecode(raw)
    findings: list[Finding] = []
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding("non-utf8-name", f"{REDACTED}:0"))
    display = _display_name(raw, denylist)
    findings.extend(Finding(f.category, f"{REDACTED}:0") for f in scan_text(name, "", denylist))
    data = content()
    if data is not None and (text := _decode(data)) is not None:
        findings.extend(scan_text(text, display, denylist))
    return findings


def _unsafe() -> ScanRefused:
    return ScanRefused("a listed file cannot be read safely (unreadable, replaced, below a link, or not a plain file)")


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_BYTES:
        chunk = os.read(descriptor, MAX_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _read_listed(root_descriptor: int, raw: bytes) -> bytes | None:
    """The content of one listed path, or its link target text; None when only its name can be scanned.

    The path is walked one component at a time from the project's directory descriptor, and no
    component is ever followed through a link. A path that is gone (a deleted file) or is a directory (a
    submodule) has only its name scanned. Everything else that cannot be read exactly as listed is a refusal:
    an unreadable or replaced file, a link above the file, a pipe, socket or device.
    """

    parts = raw.split(b"/")
    if raw.startswith(b"/") or any(part in (b"", b".", b"..") for part in parts):
        raise ScanRefused("a listed path is not a plain path inside the project")
    opened: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        current = root_descriptor
        for part in parts[:-1]:
            try:
                current = os.open(part, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                return None
            opened.append(current)
        leaf = parts[-1]
        try:
            info = os.stat(leaf, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return os.readlink(leaf, dir_fd=current)
        if stat.S_ISDIR(info.st_mode):
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _unsafe()
        descriptor = os.open(leaf, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        opened.append(descriptor)
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
            raise _unsafe()
        return _read_all(descriptor)
    except OSError:
        raise _unsafe() from None
    finally:
        for descriptor in opened:
            os.close(descriptor)


def scan_tree(root: Path, denylist: tuple[str, ...], *, manifest: list[bytes] | None = None) -> list[Finding]:
    """Scan every file in the manifest (git's own list when none is given).

    A symbolic link is never followed: its name and its target text are scanned. A listed path that
    does not exist or is a directory (a deleted file, a submodule) has only its name scanned; any other
    path that cannot be read exactly as listed refuses the whole scan (see ``_read_listed``).
    """

    names = git_manifest(root) if manifest is None else manifest
    try:
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise ScanRefused("the project directory cannot be opened") from None
    try:
        findings: list[Finding] = []
        for raw in names:
            findings.extend(_scan_named(raw, lambda raw=raw: _read_listed(root_descriptor, raw), denylist))
        return findings
    finally:
        os.close(root_descriptor)


def scan_paths(staged: bool, denylist: tuple[str, ...]) -> list[Finding]:
    if not staged:
        return scan_tree(Path.cwd(), denylist)
    # Staged content: names and modes from the index. A submodule (mode 160000) has no blob to read here,
    # so only its name is scanned.
    status, data, over = _run_git(
        ("diff", "--cached", "--raw", "-z", "--no-renames", "--diff-filter=ACMR"),
        root=None, limit=MAX_MANIFEST_BYTES, hardened=False,
    )
    if status != 0 or over:
        raise ScanRefused("git could not list the staged files")
    fields = data.split(b"\0")
    findings: list[Finding] = []
    for meta, raw in zip(fields[0::2], fields[1::2]):
        modes = meta.lstrip(b":").split(b" ")
        if len(modes) < 2 or not raw:
            raise ScanRefused("git listed the staged files in an unexpected form")

        def staged_blob(raw: bytes = raw) -> bytes:
            code, blob, over = _run_git(("show", b":" + raw), root=None, limit=MAX_BYTES + 1, hardened=False)
            if code != 0 and not over:
                raise ScanRefused("a staged file could not be read")
            return blob

        findings.extend(_scan_named(raw, (lambda: None) if modes[1] == b"160000" else staged_blob, denylist))
    return findings


def check_person(name: str, email: str, role: str, denylist: tuple[str, ...]) -> list[Finding]:
    findings = []
    if not email_allowed(email):
        findings.append(Finding("non-noreply-email", role))
    if any(term in f"{name} {email}".casefold() for term in denylist):
        findings.append(Finding("denylisted-term", role))
    return findings


def check_identity(denylist: tuple[str, ...]) -> list[Finding]:
    def config(key: str) -> str:
        result = subprocess.run(["git", "config", key], capture_output=True, text=True)
        return result.stdout.strip()

    return check_person(config("user.name"), config("user.email"), "git config user", denylist)


def check_commits(revision: str, denylist: tuple[str, ...]) -> list[Finding]:
    status, data, over = _run_git(
        ("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", revision),
        root=None, limit=MAX_MANIFEST_BYTES, hardened=False,
    )
    if status != 0 or over:
        raise ScanRefused("git could not list the commits to check")
    log = data.decode("utf-8", "replace")
    findings = []
    for line in filter(None, log.splitlines()):
        commit, author, author_email, committer, committer_email = line.split("\0")
        findings += check_person(author, author_email, f"commit {commit[:12]} author", denylist)
        findings += check_person(committer, committer_email, f"commit {commit[:12]} committer", denylist)
    return findings


def report(findings: list[Finding], denylist_terms: int = 0) -> int:
    """Print value-free findings; the exit status is 1 with any finding, else 0."""

    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"FAIL: {len(findings)} identifier finding(s); values are not printed.", file=sys.stderr)
        return 1
    print(f"PASS: no identifiers found ({denylist_terms} private denylist terms loaded).")
    return 0


def refuse(reason: ScanRefused) -> int:
    print(f"REFUSED: {reason}. Nothing was scanned.", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="scan staged content")
    parser.add_argument("--identity", action="store_true", help="check the configured commit identity")
    parser.add_argument("--commits", metavar="REV", help="check commit identities reachable from REV")
    args = parser.parse_args(argv)

    try:
        denylist = load_denylist()
        findings = scan_paths(args.staged, denylist)
        if args.identity:
            findings += check_identity(denylist)
        if args.commits:
            findings += check_commits(args.commits, denylist)
    except ScanRefused as reason:
        return refuse(reason)
    except Exception as error:  # an error message can hold a file name, so only its type is reported
        return refuse(ScanRefused(f"unexpected {type(error).__name__}"))
    return report(findings, len(denylist))


if __name__ == "__main__":
    sys.exit(main())
