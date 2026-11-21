#!/usr/bin/env python3
"""Block personal and machine identifiers from entering this public repository.

Findings report only a category, path and line, never the matched text, so CI logs
cannot leak a denylisted value. Owner-specific terms live in a private denylist file
outside the repository (one term per line, ``#`` comments allowed):

- ``$AO_IDENTIFIER_DENYLIST`` when set, otherwise
- ``~/.config/codex-claude-agent-orchestration/identifier-denylist.txt``.

Usage:
    check_identifiers.py                 scan tracked files plus untracked files that are not ignored
    check_identifiers.py --staged        scan staged content (pre-commit)
    check_identifiers.py --identity      check the configured commit identity
    check_identifiers.py --commits REV   check author/committer emails reachable from REV
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
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
# Obvious placeholder account names in documentation and evidence (/Users/x, /home/user). A real-looking
# name (anything else, including names that merely start with one of these) is still a finding.
ALLOWED_HOME_NAMES = frozenset({"x", "user", "you", "name", "username", "yourname", "example"})
# Project namespaces that look like mDNS hostnames but identify no machine.
ALLOWED_HOSTNAMES = frozenset({"codex-agents.local", "claude-agents.local"})


@dataclass(frozen=True)
class Finding:
    category: str
    location: str

    def __str__(self) -> str:
        return f"{self.location}: {self.category}"


def load_denylist(path: Path | None = None) -> tuple[str, ...]:
    """Return lower-cased private terms; a missing file yields no terms."""

    if path is None:
        configured = os.environ.get("AO_IDENTIFIER_DENYLIST")
        path = Path(configured) if configured else DEFAULT_DENYLIST
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    terms = (line.strip() for line in text.splitlines())
    return tuple(term.casefold() for term in terms if term and not term.startswith("#"))


def email_allowed(address: str) -> bool:
    return any(pattern.fullmatch(address) for pattern in ALLOWED_EMAILS)


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
                if category == "home-path" and re.split(r"[\\/]+", match.group(0))[-1].casefold() in ALLOWED_HOME_NAMES:
                    continue
                findings.append(Finding(category, where))
        folded = line.casefold()
        if any(term in folded for term in denylist):
            findings.append(Finding("denylisted-term", where))
    return findings


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _decode(data: bytes) -> str | None:
    if len(data) > MAX_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _gitignore_rules(directory: Path, prefix: str) -> list[tuple[str, bool, bool, bool]]:
    """Parse one .gitignore as (pattern, anchored, directory_only, negated); anchored to ``prefix``."""

    try:
        lines = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rules = []
    for raw in lines:
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line[1:] if negated else line
        directory_only = line.endswith("/")
        line = line.strip("/") if directory_only else line
        anchored = "/" in line
        rules.append((prefix + line.lstrip("/") if anchored else line, anchored, directory_only, negated))
    return rules


def _ignored(relative: str, is_dir: bool, rules: list[tuple[str, bool, bool, bool]]) -> bool:
    ignored = False
    for pattern, anchored, directory_only, negated in rules:
        if directory_only and not is_dir:
            continue
        target = relative if anchored else relative.rsplit("/", 1)[-1]
        if fnmatch.fnmatchcase(target, pattern):
            ignored = not negated
    return ignored


def _walk_files(root: Path) -> list[str]:
    """In-process file set (no subprocess) matching ``git ls-files --cached --others --exclude-standard``.

    Supports plain .gitignore patterns (globs, anchoring, trailing slash, negation); a file that is
    tracked yet ignored is not listed here, which only matters outside a clean checkout.
    """

    found: list[str] = []

    def visit(directory: Path, prefix: str, rules: list[tuple[str, bool, bool, bool]]) -> None:
        rules = rules + _gitignore_rules(directory, prefix)
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            relative = prefix + entry.name
            if entry.name == ".git":
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if _ignored(relative, is_dir, rules):
                continue
            if is_dir:
                if not (Path(entry.path) / ".git").exists():  # a nested repository is not ours to scan
                    visit(Path(entry.path), relative + "/", rules)
            else:
                found.append(relative)

    visit(root, "", [])
    return sorted(found)


def list_scan_files(root: Path, *, use_git: bool = True) -> list[str]:
    """The one file set every identifier scan uses: tracked files plus untracked files not ignored.

    ``use_git=False`` is for the QA sandbox, which cannot start git; it walks the disk in-process.
    """

    if not use_git:
        return _walk_files(root)
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, check=True, capture_output=True,
    ).stdout
    names = (raw.decode("utf-8", errors="replace") for raw in filter(None, out.split(b"\0")))
    return sorted({name for name in names if not name.endswith("/")})


def scan_tree(
    root: Path, denylist: tuple[str, ...], *, use_git: bool = True, exclude: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Scan list_scan_files(root); ``exclude`` names top-level entries to skip (private installation files)."""

    findings = []
    for name in list_scan_files(root, use_git=use_git):
        if name.split("/", 1)[0] in exclude:
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        findings.extend(scan_text(name, f"{name} (path)", denylist))
        text = _decode(path.read_bytes())
        if text is not None:
            findings.extend(scan_text(text, name, denylist))
    return findings


def scan_paths(staged: bool, denylist: tuple[str, ...]) -> list[Finding]:
    if not staged:
        return scan_tree(Path.cwd(), denylist)
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    findings = []
    for raw in filter(None, names.split(b"\0")):
        path = raw.decode("utf-8", errors="replace")
        findings.extend(scan_text(path, f"{path} (path)", denylist))
        text = _decode(_git("show", f":{path}"))
        if text is not None:
            findings.extend(scan_text(text, path, denylist))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="scan staged content")
    parser.add_argument("--identity", action="store_true", help="check the configured commit identity")
    parser.add_argument("--commits", metavar="REV", help="check commit identities reachable from REV")
    args = parser.parse_args(argv)

    denylist = load_denylist()
    findings = scan_paths(args.staged, denylist)
    if args.identity:
        findings += check_identity(denylist)
    if args.commits:
        findings += check_commits(args.commits, denylist)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"FAIL: {len(findings)} identifier finding(s); values are not printed.", file=sys.stderr)
        return 1
    print(f"PASS: no identifiers found ({len(denylist)} private denylist terms loaded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
