#!/usr/bin/env python3
"""Block personal and machine identifiers from entering this public repository.

Findings report only a category, path and line, never the matched text, so CI logs
cannot leak a denylisted value. A path that itself holds a value (an identifier, a denylisted
term, a control character or invalid UTF-8) is printed as ``[redacted]``. Owner-specific terms live in a private denylist file
outside the repository (one term per line, ``#`` comments allowed):

- ``$AO_IDENTIFIER_DENYLIST`` when set, otherwise
- ``~/.config/codex-claude-agent-orchestration/identifier-denylist.txt``.

Usage:
    check_identifiers.py                 scan tracked files plus untracked files that are not ignored
                                         (the git file manifest; refuses when git cannot produce it)
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
DEFAULT_DENYLIST = Path.home() / ".config" / "codex-claude-agent-orchestration" / "identifier-denylist.txt"

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
# next character ends it (a path separator, the end of the line, or a quote or bracket); anything else,
# including a space or a non-ASCII letter, might continue a real account name.
ALLOWED_HOME_NAMES = frozenset({"x", "user", "you", "name", "username", "yourname", "example"})
COMPONENT_END = frozenset("/\\\"'`)]}>,;:")
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


def load_denylist(path: Path | None = None) -> tuple[str, ...]:
    """Return lower-cased private terms; a missing file yields no terms, an unreadable one refuses."""

    if path is None:
        configured = os.environ.get("AO_IDENTIFIER_DENYLIST")
        path = Path(configured) if configured else DEFAULT_DENYLIST
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError):
        raise ScanRefused("the private denylist exists but cannot be read") from None
    terms = (line.strip() for line in text.splitlines())
    return tuple(term.casefold() for term in terms if term and not term.startswith("#"))


def email_allowed(address: str) -> bool:
    return any(pattern.fullmatch(address) for pattern in ALLOWED_EMAILS)


def _placeholder_home(match: re.Match[str], line: str) -> bool:
    """True only for an exact, lower-case placeholder token that is a whole path component."""

    if re.split(r"[\\/]+", match.group(0))[-1] not in ALLOWED_HOME_NAMES:
        return False
    following = line[match.end():match.end() + 1]
    return following == "" or following in COMPONENT_END


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


def _git(*args: str | bytes) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _git_environment() -> dict[str, str]:
    """No GIT_* variables and no user or system git configuration: the same file list on every machine."""

    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "xcrun_nocache": "1",
    })
    return environment


def parse_manifest(data: bytes) -> list[bytes]:
    """NUL-separated git path names to a sorted, de-duplicated list; entries ending in ``/`` are nested repositories."""

    return sorted({name for name in data.split(b"\0") if name and not name.endswith(b"/")})


def git_manifest(root: Path) -> list[bytes]:
    """The one file list every identifier scan uses, straight from git: tracked files (even when an
    ignore rule names them) plus untracked files that no ``.gitignore`` ignores.

    Only ``.gitignore`` files count (not ``.git/info/exclude`` or a global ignore file), so a clean
    checkout in CI lists exactly what QA lists. Refuses when git cannot produce the list.
    """

    git = shutil.which("git")
    if git is None:
        raise ScanRefused("git is unavailable, so the files to scan cannot be listed")
    try:
        completed = subprocess.run(
            [git, "-c", "core.fsmonitor=false", "-C", str(root), *GIT_LIST_ARGS],
            capture_output=True, env=_git_environment(), stdin=subprocess.DEVNULL, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ScanRefused("git could not be run, so the files to scan cannot be listed") from None
    if completed.returncode != 0:
        raise ScanRefused("git could not list the files to scan (not a repository?)")
    return parse_manifest(completed.stdout)


def read_manifest(path: Path) -> list[bytes]:
    try:
        return parse_manifest(path.read_bytes())
    except OSError:
        raise ScanRefused("the file manifest supplied by the host cannot be read") from None


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


def _display_name(name: str, denylist: tuple[str, ...]) -> str:
    """The path as printed: itself only if it passes the scan and holds no control character."""

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or scan_text(name, "", denylist):
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
    display = _display_name(name, denylist)
    findings.extend(Finding(f.category, f"{REDACTED}:0") for f in scan_text(name, "", denylist))
    data = content()
    if data is not None and (text := _decode(data)) is not None:
        findings.extend(scan_text(text, display, denylist))
    return findings


def scan_tree(root: Path, denylist: tuple[str, ...], *, manifest: list[bytes] | None = None) -> list[Finding]:
    """Scan every file in the manifest (git's own list when none is given).

    A symbolic link is never followed: its name and its target text are scanned. A listed path that
    does not exist or is not a regular file (a deleted file, a submodule) has only its name scanned.
    """

    def reader(raw: bytes) -> Callable[[], bytes | None]:
        def read() -> bytes | None:
            path = os.path.join(os.fsencode(root), raw)
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    return os.readlink(path)
                if not stat.S_ISREG(info.st_mode):
                    return None
                with open(path, "rb") as handle:
                    return handle.read(MAX_BYTES + 1)
            except OSError:
                return None
        return read

    findings: list[Finding] = []
    for raw in git_manifest(root) if manifest is None else manifest:
        findings.extend(_scan_named(raw, reader(raw), denylist))
    return findings


def scan_paths(staged: bool, denylist: tuple[str, ...]) -> list[Finding]:
    if not staged:
        return scan_tree(Path.cwd(), denylist)
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    findings = []
    for raw in filter(None, names.split(b"\0")):
        findings.extend(_scan_named(raw, lambda raw=raw: _git("show", b":" + raw), denylist))
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
    log = _git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", revision).decode("utf-8", "replace")
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
    return report(findings, len(denylist))


if __name__ == "__main__":
    sys.exit(main())
