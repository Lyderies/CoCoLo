"""The check-in gate's policy: one pure decision per hook request (M3-30 decisions, M3-2 at the gate).

Every write, edit, patch and command a write child makes is sent, before it runs, to the run's
supervisor gate. This module is the gate's judgement and nothing else: no I/O, no clock, no
process. `decide(policy, request)` takes one PreToolUse input as Claude Code or Codex sends it and
answers allow, refuse or escalate (ask the owner), with the rule that decided and a plain reason
the child is shown. `build_policy(grant, owner_policy)` makes the run's policy from the
requirement's grant intersected with the owner's per-project policy; the intersection is exact
and never wider than either side. `record_row` shapes the ledger row for one request and answer.

The policy (RESEARCH-0.4.0-checkin "Recommended design"):
- Always refused, whatever the grant: anything outside /workspace; '..' anywhere; control and
  invisible characters in names; git internals and `.gitmodules` at any depth and in any case;
  agent configuration (`.claude/`, `.codex/`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md` and their
  local and override forms) at any depth; `.env*`; COCOLO's own `.cocolo*` names; web tools and
  MCP tools; network tools, package installs, privilege changes, git commands that change
  history, configuration or the network, links, mode changes, recursive deletes outside the
  scope, find's exec options; bare or interactive interpreters and shells; inline code (awk, and
  sed scripts outside a small grammar of p, d, q, =, s and y with no w, r or e); wrappers that
  run another command; shell constructs whose effect cannot be read from the text.
- Grant-only paths (code that runs later: CI, hooks, build and test configuration, lock files):
  refused unless the requirement asked for the path and the owner's policy grants it.
- Allowed: writes inside the requirement's paths that the owner's policy also covers, commands
  whose argv starts with a rule in both lists, and reads inside /workspace.
- Everything else that is not refused escalates by default (the owner's choice O5, a parameter):
  unlisted writes and commands, edits of existing tests, deletions, dependency manifests.

Paths are compared case-insensitively and after NFKC for the deny classes (the macOS workspace
is case-insensitive) and exactly for grants, so a variant spelling can only narrow. Commands are
read with a quote-aware scanner that refuses anything it cannot split safely, then split into
words by `shlex`; it assumes a POSIX shell (bash or sh) runs them. Codex patches are read by
their `*** Add File:`, `*** Update File:`, `*** Delete File:` and `*** Move to:` lines; a header
with extra whitespace, an unknown header, or a misplaced one refuses the whole patch.

What the gate cannot see (the container is the wall): effects inside an allowed program (a test
run, a script, any other allowed program's own file writes); symlinks already in the workspace; a
Claude `Write` that overwrites an existing test (treated as a create; the patch gate flags test
changes); package.json and pyproject.toml lifecycle content (the patch gate's content checks).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
import shlex
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

ALLOW = "allow"
REFUSE = "refuse"
ESCALATE = "escalate"

WORKSPACE = "/workspace"

# The owner's open choices (DESIGN-0.4.0-M3 draft 3 O5, O7), with the research's recommendations.
ESCALATION_CATEGORIES = ("unlisted_command", "unlisted_write", "test_edit", "deletion",
                         "dependency_manifest")
RECOMMENDED_ESCALATE = frozenset(ESCALATION_CATEGORIES)
RECOMMENDED_RECORD_READS = False
RECOMMENDED_OWNER_WAIT_SECONDS = 600

MAX_COMMAND_BYTES = 16 * 1024
MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_NESTING = 2
RECORD_SUBJECT_LIMIT = 512

_PREFIX = "COCOLO supervisor: "


class GatePolicyError(ValueError):
    """A grant or owner policy that cannot be turned into a gate policy."""


@dataclass(frozen=True)
class Decision:
    outcome: str
    rule: str
    reason: str
    record: bool


@dataclass(frozen=True)
class GatePolicy:
    workspace: str
    write_layers: tuple[tuple[str, ...], ...]
    conditional_layers: tuple[tuple[str, ...], ...]
    command_rules: tuple[tuple[str, ...], ...]
    escalate: frozenset[str]
    record_reads: bool
    owner_wait_seconds: int
    owner_policy_state: str
    owner_policy_digest: str | None
    digest: str


# --------------------------------------------------------------------------------------------
# Reasons
# --------------------------------------------------------------------------------------------

_REASONS = {
    "request_malformed": "the request could not be read, so it is refused",
    "cwd_outside_workspace": "the working directory is outside /workspace",
    "tool_unknown": "the tool {s} is not available to this assignment",
    "network_tool": "web access is not available to this assignment",
    "mcp_tool": "MCP tools are not available to this assignment",
    "path_odd_name": "the path has control or invisible characters",
    "path_traversal": "{s} uses '..'; name the path inside /workspace directly",
    "path_outside_workspace": "{s} is outside /workspace",
    "write_git": "{s} is in git's own files; changes there are never allowed",
    "write_agent_config": "{s} is agent configuration; changes there are never allowed",
    "write_env": "{s} is an environment or secrets file; changes there are never allowed",
    "write_cocolo": "{s} belongs to COCOLO; changes there are never allowed",
    "write_conditional_ungranted": ("{s} holds code that runs later (CI, hooks, build or test "
                                    "configuration) and this assignment was not granted it"),
    "write_outside_scope": "{s} is outside this assignment's allowed paths",
    "write_deletion": "deleting or moving {s} needs the owner's yes",
    "write_test_edit": "changing the existing test {s} needs the owner's yes",
    "write_dependency_manifest": "changing the dependency list {s} needs the owner's yes",
    "write_in_scope": "{s} is inside this assignment's allowed paths",
    "read_in_workspace": "reading inside /workspace is allowed",
    "owner_policy_missing": "the owner has not set a write policy for this project",
    "owner_policy_changed": ("the owner's policy changed after this task was routed; the task "
                             "must be routed again"),
    "patch_malformed": "the patch could not be read safely ({s})",
    "patch_ambiguous": ("a patch header has extra spaces or line endings, so the file it names "
                        "cannot be read safely"),
    "command_malformed": "the command could not be read safely ({s})",
    "command_shell_construct": ("the command uses {s}, which the supervisor cannot check; run "
                                "plain commands without it"),
    "command_assignment": "setting variables in front of a command is not allowed",
    "command_interactive": "{s} would start an interactive session or read code from its input",
    "command_inline_code": ("{s} runs inline code that cannot be checked; write a file inside "
                            "your paths and run that"),
    "command_wrapper": "{s} runs commands in a changed context; run the command directly",
    "command_nesting_too_deep": "the command nests shells too deeply",
    "command_privilege": "{s} changes privileges and is never allowed",
    "command_network": "{s} uses the network and is never allowed",
    "command_package_install": "{s} installs packages and is never allowed",
    "command_git_denied": ("this git command changes history, configuration or the network, or "
                           "writes where the supervisor cannot check"),
    "command_delete_outside_scope": ("a recursive delete of {s} reaches outside this "
                                     "assignment's allowed paths"),
    "command_symlink": "creating links is not allowed",
    "command_mode_change": "changing file modes or owners is not allowed",
    "command_exec_option": "{s} with that option runs commands or deletes files unchecked",
    "command_background": "background commands are not allowed",
    "command_not_allowed": "{s} is not on this assignment's command list",
    "command_allowed": "the command is on this assignment's command list",
}

# Which escalation wins when several parts of one request escalate.
_ESCALATION_ORDER = ("write_outside_scope", "command_not_allowed", "write_deletion",
                     "write_test_edit", "write_dependency_manifest")


def _show(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    shown = "".join("?" if unicodedata.category(c)[0] == "C" or unicodedata.category(c) in
                    ("Zl", "Zp") else c for c in text)
    return shown if len(shown) <= 160 else shown[:157] + "..."


def _reason(rule: str, outcome: str, subject: object = "") -> str:
    text = _REASONS[rule].format(s=_show(subject))
    if outcome == ESCALATE:
        return _PREFIX + "asking the owner: " + text
    return _PREFIX + text


class _Refused(Exception):
    def __init__(self, rule: str, subject: object = ""):
        super().__init__(rule)
        self.rule = rule
        self.subject = subject


@dataclass(frozen=True)
class _Part:
    outcome: str
    rule: str
    subject: object


# --------------------------------------------------------------------------------------------
# Building the policy (M3-2: the effective grant is the request intersected with the owner's)
# --------------------------------------------------------------------------------------------

_GRANT_KEYS = frozenset({"write_paths", "conditional_paths", "commands"})
_OWNER_METADATA = frozenset({"written_by", "written_at"})


def _odd_char(c: str) -> bool:
    category = unicodedata.category(c)
    return category[0] == "C" or category in ("Zl", "Zp")


def _check_entry(entry: object, where: str) -> str:
    if not isinstance(entry, str) or not entry:
        raise GatePolicyError(f"{where}: a path entry must be a non-empty string")
    if any(_odd_char(c) for c in entry):
        raise GatePolicyError(f"{where}: a path entry has control or invisible characters")
    if entry.startswith("/"):
        raise GatePolicyError(f"{where}: a path entry must be relative to the workspace")
    body = entry[:-1] if entry.endswith("/") else entry
    for part in body.split("/"):
        if part in ("", ".", ".."):
            raise GatePolicyError(f"{where}: a path entry has an empty, '.' or '..' part")
        if any(c in part for c in "[]{}\\"):
            raise GatePolicyError(f"{where}: only '*', '?' and '**' are supported in path entries")
        if "**" in part and part != "**":
            raise GatePolicyError(f"{where}: '**' must be a whole path part")
    return entry


def _check_rule(rule: object, where: str) -> tuple[str, ...]:
    if not isinstance(rule, (list, tuple)) or not rule:
        raise GatePolicyError(f"{where}: a command rule must be a non-empty list of words")
    for word in rule:
        if not isinstance(word, str) or not word or any(_odd_char(c) for c in word):
            raise GatePolicyError(f"{where}: a command rule word must be a plain non-empty string")
    return tuple(rule)


def _check_layer(data: object, where: str, metadata: frozenset[str]) -> dict:
    if not isinstance(data, Mapping):
        raise GatePolicyError(f"{where} must be a mapping")
    missing = sorted(_GRANT_KEYS - set(data))
    unknown = sorted(set(data) - _GRANT_KEYS - metadata)
    if missing or unknown:
        raise GatePolicyError(f"{where}: missing {missing}, unknown {unknown}")
    for key in ("write_paths", "conditional_paths", "commands"):
        if not isinstance(data[key], (list, tuple)):
            raise GatePolicyError(f"{where}.{key} must be a list")
    for key in metadata & set(data):
        if not isinstance(data[key], str):
            raise GatePolicyError(f"{where}.{key} must be a string")
    return {
        "write_paths": tuple(sorted({_check_entry(e, f"{where}.write_paths")
                                     for e in data["write_paths"]})),
        "conditional_paths": tuple(sorted({_check_entry(e, f"{where}.conditional_paths")
                                           for e in data["conditional_paths"]})),
        "commands": tuple(sorted({_check_rule(r, f"{where}.commands") for r in data["commands"]})),
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def owner_policy_digest(owner_policy: Mapping) -> str:
    """The digest a route records for the owner's policy it was checked against (A04)."""

    _check_layer(owner_policy, "owner_policy", _OWNER_METADATA)
    plain = {key: [list(v) if isinstance(v, (list, tuple)) else v for v in value]
             if isinstance(value, (list, tuple)) else value
             for key, value in owner_policy.items()}
    return hashlib.sha256(_canonical(plain)).hexdigest()


def _intersect_rules(left: Iterable[tuple[str, ...]],
                     right: Iterable[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    # The argv set of a prefix rule is every argv starting with it; two such sets meet in the
    # longer rule when one is a prefix of the other, and nowhere otherwise.
    out = set()
    right = tuple(right)
    for a in left:
        for b in right:
            if a[:len(b)] == b:
                out.add(a)
            elif b[:len(a)] == a:
                out.add(b)
    return tuple(sorted(out))


def build_policy(
    grant: Mapping,
    owner_policy: Mapping | None,
    *,
    escalate: Iterable[str] = RECOMMENDED_ESCALATE,
    record_reads: bool = RECOMMENDED_RECORD_READS,
    owner_wait_seconds: int = RECOMMENDED_OWNER_WAIT_SECONDS,
    routed_owner_policy: Mapping | None = None,
    routed_owner_policy_digest: str | None = None,
    workspace: str = WORKSPACE,
) -> GatePolicy:
    """The run's policy: the requirement's grant intersected with the owner's per-project policy.

    `grant` and `owner_policy` each hold `write_paths` and `conditional_paths` (path entries
    relative to the workspace: an exact path, a directory covering everything under it, or a
    glob with `*`, `?` and `**`) and `commands` (argv prefix rules). A path is writable only if
    every layer covers it, and a grant-only path also needs every layer's conditional grant.
    With `routed_owner_policy` the policy the route was checked against is a third layer, so a
    later change can only narrow (A04); with only its digest, a different current policy
    refuses every write and command until the task is routed again. No owner policy refuses
    every write and command (A01).
    """

    request = _check_layer(grant, "grant", frozenset())
    layers = [request]
    if owner_policy is None:
        state, digest = "missing", None
    else:
        layers.append(_check_layer(owner_policy, "owner_policy", _OWNER_METADATA))
        digest = owner_policy_digest(owner_policy)
        state = "present"
        if routed_owner_policy is not None:
            routed_digest = owner_policy_digest(routed_owner_policy)
            if routed_owner_policy_digest is not None and routed_owner_policy_digest != routed_digest:
                raise GatePolicyError("the routed owner policy does not match its recorded digest")
            layers.append(_check_layer(routed_owner_policy, "routed_owner_policy", _OWNER_METADATA))
        elif routed_owner_policy_digest is not None and routed_owner_policy_digest != digest:
            state = "changed"
    if isinstance(escalate, str):
        raise GatePolicyError("escalate must be a collection of category names")
    escalate = frozenset(escalate)
    unknown = sorted(escalate - set(ESCALATION_CATEGORIES))
    if unknown:
        raise GatePolicyError(f"unknown escalation categories: {unknown}")
    if not isinstance(record_reads, bool):
        raise GatePolicyError("record_reads must be a boolean")
    if type(owner_wait_seconds) is not int or owner_wait_seconds <= 0:
        raise GatePolicyError("owner_wait_seconds must be a positive integer")
    if (not isinstance(workspace, str) or not workspace.startswith("/") or workspace == "/"
            or posixpath.normpath(workspace) != workspace or ".." in workspace.split("/")):
        raise GatePolicyError("workspace must be a normalized absolute path other than /")
    rules = request["commands"] if state == "present" else ()
    if state == "present":
        for layer in layers[1:]:
            rules = _intersect_rules(rules, layer["commands"])
    fields = {
        "workspace": workspace,
        "write_layers": tuple(layer["write_paths"] for layer in layers),
        "conditional_layers": tuple(layer["conditional_paths"] for layer in layers),
        "command_rules": tuple(rules),
        "escalate": escalate,
        "record_reads": record_reads,
        "owner_wait_seconds": owner_wait_seconds,
        "owner_policy_state": state,
        "owner_policy_digest": digest,
    }
    plain = dict(fields, escalate=sorted(escalate),
                 write_layers=[list(x) for x in fields["write_layers"]],
                 conditional_layers=[list(x) for x in fields["conditional_layers"]],
                 command_rules=[list(x) for x in fields["command_rules"]])
    return GatePolicy(**fields, digest=hashlib.sha256(_canonical(plain)).hexdigest())


# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------

_AGENT_DIRS = frozenset({".claude", ".codex"})
_AGENT_FILES = frozenset({".mcp.json", "claude.md", "claude.local.md", "agents.md",
                          "agents.override.md"})
_CONDITIONAL_NAMES = frozenset({
    ".gitattributes", ".lfsconfig", ".pre-commit-config.yaml", ".npmrc", ".yarnrc", ".yarnrc.yml",
    "conftest.py", "sitecustomize.py", "usercustomize.py", "noxfile.py",
    "makefile", "gnumakefile", "setup.py", "pytest.ini", "tox.ini",
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "pipfile.lock", "cargo.lock", "gemfile.lock", "composer.lock", "go.sum",
})
_CONDITIONAL_SUFFIXES = (".pth", ".mk")
_CONDITIONAL_PREFIXES = ("jest.config.", "vitest.config.", ".mocharc", "karma.conf.")
_CONDITIONAL_SEQUENCES = ((".github", "workflows"), (".husky",), (".agents",),
                          (".vscode", "tasks.json"))
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_NAME = re.compile(r"^(test_.*\.py|.*_test\.[a-z0-9]+|.*\.(test|spec)\.[cm]?[jt]sx?|.*_spec\.rb)$")
_DEPENDENCY_NAMES = frozenset({"package.json", "pyproject.toml", "pipfile", "setup.cfg",
                               "cargo.toml", "go.mod", "gemfile", "composer.json",
                               "environment.yml"})
_DEPENDENCY_NAME = re.compile(r"^requirements.*\.(txt|in)$")
_DEVICES = frozenset({"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"})


def _key(part: str) -> str:
    return unicodedata.normalize("NFKC", part).casefold()


def _deny_class(rel: tuple[str, ...]) -> str | None:
    keys = [_key(p) for p in rel]
    if any(k in (".git", ".gitmodules") for k in keys):
        return "write_git"
    if any(k in _AGENT_DIRS or k in _AGENT_FILES for k in keys):
        return "write_agent_config"
    if any(k.startswith(".env") for k in keys):
        return "write_env"
    if any(k.startswith(".cocolo") for k in keys):
        return "write_cocolo"
    return None


def _conditional(rel: tuple[str, ...]) -> bool:
    keys = tuple(_key(p) for p in rel)
    base = keys[-1]
    if (base in _CONDITIONAL_NAMES or base.endswith(_CONDITIONAL_SUFFIXES)
            or base.startswith(_CONDITIONAL_PREFIXES)):
        return True
    return any(keys[i:i + len(seq)] == seq for seq in _CONDITIONAL_SEQUENCES
               for i in range(len(keys)))


def _is_test(rel: tuple[str, ...]) -> bool:
    keys = [_key(p) for p in rel]
    return any(k in _TEST_DIRS for k in keys[:-1]) or bool(_TEST_NAME.match(keys[-1]))


def _is_dependency(rel: tuple[str, ...]) -> bool:
    base = _key(rel[-1])
    return base in _DEPENDENCY_NAMES or bool(_DEPENDENCY_NAME.match(base))


def _glob(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        return any(_glob(pattern[1:], path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], head) and _glob(pattern[1:], path[1:])


def _entry_covers(entry: str, rel: tuple[str, ...]) -> bool:
    directory = entry.endswith("/")
    parts = tuple(entry.rstrip("/").split("/"))
    if not any(c in entry for c in "*?"):
        return rel[:len(parts)] == parts
    if directory:
        parts = parts + ("**",)
    return _glob(parts, rel)


def _covered(layers: tuple[tuple[str, ...], ...], rel: tuple[str, ...]) -> bool:
    return all(any(_entry_covers(e, rel) for e in layer) for layer in layers)


class _Checker:
    def __init__(self, policy: GatePolicy, cwd: tuple[str, ...]):
        self.policy = policy
        self.root = tuple(p for p in policy.workspace.split("/") if p)
        self.cwd = cwd

    # path resolution ------------------------------------------------------------------------

    def resolve(self, raw: object, base: tuple[str, ...] | None = None) -> tuple[str, ...]:
        """Relative path parts inside the workspace, or a refusal. Never follows anything."""

        if not isinstance(raw, str) or not raw:
            raise _Refused("request_malformed")
        if any(_odd_char(c) for c in raw):
            raise _Refused("path_odd_name")
        parts = raw.split("/")
        if ".." in parts:
            raise _Refused("path_traversal", raw)
        named = [p for p in parts if p not in ("", ".")]
        within = self.cwd if base is None else base
        absolute = list(named) if raw.startswith("/") else [*self.root, *within, *named]
        if tuple(absolute[:len(self.root)]) != self.root:
            raise _Refused("path_outside_workspace", raw)
        return tuple(absolute[len(self.root):])

    # writes ---------------------------------------------------------------------------------

    def write(self, rel: tuple[str, ...], kind: str, shown: object) -> _Part:
        """One written path: kind is 'replace' (create or overwrite), 'modify' or 'delete'."""

        policy = self.policy
        if not rel:
            raise _Refused("path_outside_workspace", shown)
        denied = _deny_class(rel)
        if denied:
            raise _Refused(denied, shown)
        if policy.owner_policy_state == "missing":
            raise _Refused("owner_policy_missing")
        if policy.owner_policy_state == "changed":
            raise _Refused("owner_policy_changed")
        if _conditional(rel) and not _covered(policy.conditional_layers, rel):
            raise _Refused("write_conditional_ungranted", shown)
        if not _covered(policy.write_layers, rel):
            return self.unlisted("unlisted_write", "write_outside_scope", shown)
        if kind == "delete" and "deletion" in policy.escalate:
            return _Part(ESCALATE, "write_deletion", shown)
        if kind in ("modify", "delete") and "test_edit" in policy.escalate and _is_test(rel):
            return _Part(ESCALATE, "write_test_edit", shown)
        if "dependency_manifest" in policy.escalate and _is_dependency(rel):
            return _Part(ESCALATE, "write_dependency_manifest", shown)
        return _Part(ALLOW, "write_in_scope", shown)

    def unlisted(self, category: str, rule: str, shown: object) -> _Part:
        if category in self.policy.escalate:
            return _Part(ESCALATE, rule, shown)
        raise _Refused(rule, shown)

    def write_path(self, raw: object, kind: str, base: tuple[str, ...] | None = None) -> _Part:
        return self.write(self.resolve(raw, base), kind, raw)

    def read_path(self, raw: object, base: tuple[str, ...] | None = None) -> tuple[str, ...]:
        return self.resolve(raw, base)


# --------------------------------------------------------------------------------------------
# Codex patches
# --------------------------------------------------------------------------------------------

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_HEADERS = (("*** Add File: ", "replace"), ("*** Update File: ", "modify"),
            ("*** Delete File: ", "delete"), ("*** Move to: ", "move"))


def _patch_ops(text: object) -> list[tuple[str, str]]:
    """The (path, kind) pairs a Codex patch touches; a move deletes its source."""

    if not isinstance(text, str):
        raise _Refused("request_malformed")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_PATCH_BYTES:
        raise _Refused("patch_malformed", "too large")
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 2 or lines[0] != _BEGIN or lines[-1] != _END:
        raise _Refused("patch_malformed", "it must start with '*** Begin Patch' and end with "
                                          "'*** End Patch'")
    ops: list[tuple[str, str]] = []
    can_move = False
    for line in lines[1:-1]:
        stripped = line.strip()
        if not stripped.startswith("***"):
            can_move = False
            continue
        if line != stripped:
            raise _Refused("patch_ambiguous")
        if line == "*** End of File":
            can_move = False
            continue
        for marker, kind in _HEADERS:
            if line.startswith(marker):
                path = line[len(marker):]
                break
        else:
            raise _Refused("patch_malformed", "an unknown or misplaced '***' line")
        if path != path.strip():
            raise _Refused("patch_ambiguous")
        if kind == "move":
            if not can_move:
                raise _Refused("patch_malformed", "'*** Move to:' must follow '*** Update File:'")
            source, _ = ops[-1]
            ops[-1] = (source, "delete")
            ops.append((path, "replace"))
            can_move = False
        else:
            ops.append((path, kind))
            can_move = kind == "modify"
    if not ops:
        raise _Refused("patch_malformed", "no file operations")
    return ops


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------

_WORD_STOP = frozenset(" ;&|<>")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "ash",
                     "rbash", "yash", "posh"})
_PRIVILEGE = frozenset({"sudo", "su", "doas", "pkexec", "runuser", "sg", "newgrp"})
_WRAPPERS = frozenset({
    "exec", "env", "nohup", "timeout", "xargs", "nice", "ionice", "time", "command", "builtin",
    "source", ".", "stdbuf", "strace", "ltrace", "gdb", "lldb", "chroot", "setsid", "unshare",
    "nsenter", "busybox", "toybox", "flock", "watch", "script", "parallel", "taskset", "chrt",
    "prlimit", "systemd-run", "cd", "pushd", "popd", "export", "set", "unset", "alias", "trap",
    "ulimit", "umask", "declare", "typeset", "local", "readonly", "shopt", "hash", "enable",
})
_REPLS = frozenset({
    "irb", "ipython", "ipython3", "ghci", "pry", "jshell", "vi", "vim", "nvim", "view", "vimdiff",
    "nano", "pico", "emacs", "ed", "ex", "less", "more", "most", "man", "top", "htop", "tmux",
    "screen", "sqlite3", "psql", "mysql", "mongo", "mongosh", "redis-cli",
})
_INLINE_ALWAYS = frozenset({"awk", "gawk", "mawk", "nawk"})
_NETWORK = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "lftp", "tftp",
    "telnet", "socat", "ping", "ping6", "dig", "nslookup", "host", "http", "https", "aria2c",
    "mosh",
})
_MODE_CHANGE = frozenset({"chmod", "chown", "chgrp", "chattr", "setfacl"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                        "-fprintf", "-fls"})
_DELETERS = frozenset({"rm", "rmdir", "unlink", "shred", "mv"})
_WRITERS = frozenset({"cp", "install", "touch", "mkdir", "tee", "truncate", "mkfifo", "mknod"})
_GIT_GLOBAL_DENIED = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--exec-path",
                                "--namespace", "--config-env", "--super-prefix", "-p",
                                "--paginate", "--list-cmds"})
_GIT_DENIED = frozenset({
    "push", "commit", "reset", "clean", "config", "remote", "fetch", "pull", "clone",
    "submodule", "update-ref", "update-index", "symbolic-ref", "apply", "am", "checkout",
    "restore", "switch", "stash", "merge", "rebase", "cherry-pick", "revert", "mv", "rm",
    "worktree", "filter-branch", "filter-repo", "replace", "notes", "gc", "prune", "repack",
    "init", "hook", "daemon", "instaweb", "send-email", "difftool", "mergetool", "credential",
    "bundle", "fast-import", "archive", "upload-pack", "receive-pack", "ls-remote",
    "maintenance", "sparse-checkout", "lfs",
})
_GIT_DENIED_OPTIONS = frozenset({"--output", "--ext-diff", "--textconv", "--exec",
                                 "--upload-pack", "--receive-pack", "--open-files-in-pager"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_PIP = re.compile(r"^pip\d*(\.\d+)?$")
_INTERPRETER = re.compile(
    r"^(python\d*(\.\d+)?|pypy\d*(\.\d+)?|node|nodejs|bun|deno|perl\d*(\.\d+)*|ruby\d*(\.\d+)*"
    r"|php\d*(\.\d+)*|lua\d*(\.\d+)*|luajit|R|Rscript|julia|tclsh\d*(\.\d+)*|wish|osascript)$")
# Per interpreter family: inline-code letters, interactive letters, module letters, and letters
# whose value is the next word when not attached.
_FAMILIES = {
    "python": ("c", "i", "m", "WX"),
    "node": ("ep", "i", "", "rC"),
    "perl": ("eE", "", "", ""),
    "ruby": ("e", "", "", ""),
    "php": ("r", "a", "", ""),
    "lua": ("e", "i", "", "l"),
    "r": ("e", "", "", ""),
    "julia": ("eE", "i", "", ""),
    "other": ("e", "i", "", ""),
}
_LONG_INLINE = frozenset({"--eval", "--print", "--command", "--exec", "--expression"})
_LONG_VALUE = frozenset({"--require", "--import", "--loader", "--experimental-loader",
                         "--conditions", "--check-hash-based-pycs"})
_PACKAGE_SUBCOMMANDS = {
    "npm": {"install", "i", "ci", "add", "update", "upgrade", "up", "exec", "x", "link", "create",
            "init", "rebuild", "uninstall", "remove", "rm"},
    "pnpm": {"install", "i", "add", "update", "up", "dlx", "exec", "create", "remove", "rm",
             "link"},
    "yarn": {"install", "add", "upgrade", "up", "dlx", "create", "remove", "link"},
    "bun": {"install", "i", "add", "update", "x", "create", "remove", "link"},
    "gem": {"install", "update", "uninstall"},
    "cargo": {"install", "add", "update", "fetch", "uninstall"},
    "go": {"get", "install", "mod"},
    "conda": {"install", "create", "update", "remove"},
    "mamba": {"install", "create", "update", "remove"},
    "micromamba": {"install", "create", "update", "remove"},
    "poetry": {"add", "install", "update", "lock", "remove", "self"},
    "pipenv": {"install", "update", "sync", "lock", "uninstall"},
    "composer": {"install", "require", "update", "remove"},
    "bundle": {"install", "add", "update"},
    "uv": {"add", "remove", "sync", "tool", "pip", "lock", "venv"},
    "hatch": {"env", "dep"},
}
_PACKAGE_ALWAYS = frozenset({"npx", "pnpx", "bunx", "pipx", "uvx", "brew", "apt", "apt-get",
                             "dpkg", "dnf", "yum", "apk", "pacman", "zypper", "port", "snap",
                             "flatpak", "easy_install"})
_PIP_INSTALL = frozenset({"install", "download", "wheel", "uninstall"})


def _first_word(args: list[str]) -> str | None:
    for arg in args:
        if not arg.startswith("-"):
            return arg
    return None


def _family(name: str) -> str:
    if name.startswith(("python", "pypy")):
        return "python"
    if name in ("node", "nodejs", "bun"):
        return "node"
    if name.startswith("perl"):
        return "perl"
    if name.startswith("ruby"):
        return "ruby"
    if name.startswith("php"):
        return "php"
    if name.startswith("lua"):
        return "lua"
    if name in ("R", "Rscript"):
        return "r"
    if name == "julia":
        return "julia"
    return "other"


class _Shell:
    """Splits one command string into simple commands, refusing what it cannot read safely."""

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)

    def construct(self, what: str) -> _Refused:
        return _Refused("command_shell_construct", what)

    def word(self, i: int) -> tuple[str, int]:
        text, n = self.text, self.n
        start = i
        previous = ""
        while i < n:
            c = text[i]
            if c in _WORD_STOP:
                break
            if c in "()":
                raise self.construct("a subshell or substitution")
            if c == "'":
                end = text.find("'", i + 1)
                if end < 0:
                    raise _Refused("command_malformed", "an unclosed quote")
                i, previous = end + 1, ""
                continue
            if c == '"':
                j = i + 1
                while True:
                    if j >= n:
                        raise _Refused("command_malformed", "an unclosed quote")
                    d = text[j]
                    if d == '"':
                        break
                    if d in "$`":
                        raise self.construct("an expansion inside double quotes")
                    if d == "\\":
                        if j + 1 >= n:
                            raise _Refused("command_malformed", "an unclosed quote")
                        if text[j + 1] in "$`":
                            raise self.construct("an expansion inside double quotes")
                        j += 2
                        continue
                    j += 1
                i, previous = j + 1, ""
                continue
            if c == "\\":
                if i + 1 >= n:
                    raise _Refused("command_malformed", "a trailing backslash")
                i, previous = i + 2, ""
                continue
            if c in "$`":
                raise self.construct("an expansion or substitution")
            if c in "*?[":
                raise self.construct("a wildcard; spell the file names out")
            if c in "{}":
                raise self.construct("braces")
            if i == start and c in "#~!=":
                raise self.construct({"#": "a comment", "~": "a home-directory tilde",
                                      "!": "negation", "=": "an equals expansion"}[c])
            if c == "~" and previous in ("=", ":"):
                raise self.construct("a home-directory tilde")
            previous = c
            i += 1
        return text[start:i], i

    def skip(self, i: int) -> int:
        while i < self.n and self.text[i] == " ":
            i += 1
        return i

    def redirect(self, i: int, fd: str | None) -> tuple[tuple[str, str], int]:
        text, n = self.text, self.n
        op = text[i]
        nxt = text[i + 1:i + 2]
        i += 1
        if op == "<":
            if nxt in ("<", ">", "(", "&"):
                raise self.construct("a here-document, process substitution or descriptor "
                                     "redirection")
            kind = "read"
        elif nxt == ">":
            i, kind = i + 1, "append"
        elif nxt == "&":
            i = self.skip(i + 1)
            target, i = self.word(i) if i < n and text[i] not in _WORD_STOP else ("", i)
            if not target.isdigit():
                raise self.construct("a redirection to a descriptor other than a number")
            return ("dup", target), i
        elif nxt in ("|", "("):
            raise self.construct("a clobbering redirection or process substitution")
        else:
            kind = "write"
        i = self.skip(i)
        if i >= n:
            raise _Refused("command_malformed", "a redirection without a target")
        if text[i] in _WORD_STOP:
            raise self.construct("a redirection to something other than a file")
        target, i = self.word(i)
        return (kind, target), i

    def segments(self) -> list[tuple[list[str], list[tuple[str, str]]]]:
        text, n = self.text, self.n
        segments: list[tuple[list[str], list[tuple[str, str]]]] = []
        words: list[str] = []
        redirects: list[tuple[str, str]] = []
        last_op = None

        def close(op: str) -> None:
            nonlocal words, redirects, last_op
            if not words:
                raise _Refused("command_malformed", "an empty command or redirection alone")
            segments.append((words, redirects))
            words, redirects, last_op = [], [], op

        i = self.skip(0)
        while i < n:
            c = text[i]
            two = text[i:i + 2]
            if two in (";;", "|&", "&>"):
                raise self.construct({";;": "a case terminator", "|&": "a stderr pipe",
                                      "&>": "a combined output redirection"}[two])
            if two in ("&&", "||"):
                close(two)
                i = self.skip(i + 2)
                continue
            if c == "&":
                raise self.construct("a background job")
            if c in ";|":
                close(c)
                i = self.skip(i + 1)
                continue
            if c in "<>":
                target, i = self.redirect(i, None)
                redirects.append(target)
                i = self.skip(i)
                continue
            raw, i = self.word(i)
            if i < n and text[i] in "<>" and raw.isdigit():
                target, i = self.redirect(i, raw)
                redirects.append(target)
            else:
                words.append(raw)
            i = self.skip(i)
        if words or redirects:
            close(";")
        elif last_op in (None, "&&", "||", "|"):
            raise _Refused("command_malformed", "an empty command")
        out = []
        for raw_words, raw_redirects in segments:
            argv = [self.split(w) for w in raw_words]
            redirect_words = [(kind, self.split(t)) for kind, t in raw_redirects]
            out.append((argv, redirect_words))
        return out

    @staticmethod
    def split(raw: str) -> str:
        try:
            parts = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            raise _Refused("command_malformed", "a word shlex cannot split") from None
        if len(parts) != 1:
            raise _Refused("command_malformed", "a word shlex cannot split")
        return parts[0]


class _Commands:
    def __init__(self, checker: _Checker, base: tuple[str, ...]):
        self.checker = checker
        self.policy = checker.policy
        self.base = base

    def shell(self, text: str, depth: int) -> list[_Part]:
        if len(text.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
            raise _Refused("command_malformed", "too long")
        if any(_odd_char(c) for c in text):
            raise _Refused("command_malformed", "a line break or control character")
        if not text.strip():
            raise _Refused("command_malformed", "an empty command")
        parts: list[_Part] = []
        for argv, redirects in _Shell(text).segments():
            parts.extend(self.simple(argv, redirects, depth))
        return parts

    def argv(self, argv: object) -> list[_Part]:
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(w, str) for w in argv)):
            raise _Refused("request_malformed")
        if any(_odd_char(c) for w in argv for c in w) or not argv[0]:
            raise _Refused("command_malformed", "a line break or control character")
        if sum(len(w.encode("utf-8", "surrogatepass")) for w in argv) > MAX_COMMAND_BYTES:
            raise _Refused("command_malformed", "too long")
        return self.simple(list(argv), [], 0)

    def nested(self, script: str, depth: int) -> list[_Part]:
        if depth + 1 > MAX_NESTING:
            raise _Refused("command_nesting_too_deep")
        return self.shell(script, depth + 1)

    # one simple command ----------------------------------------------------------------------

    def simple(self, argv: list[str], redirects: list[tuple[str, str]], depth: int) -> list[_Part]:
        program = argv[0]
        if _ASSIGNMENT.match(program):
            raise _Refused("command_assignment")
        name = posixpath.basename(program) or program
        args = argv[1:]
        if name in _PRIVILEGE:
            raise _Refused("command_privilege", name)
        if name in _WRAPPERS:
            raise _Refused("command_wrapper", name)
        if name in _REPLS:
            raise _Refused("command_interactive", name)
        parts = self.redirections(redirects)
        if name in _SHELLS:
            mode, script = self.shell_mode(args)
            if mode == "interactive":
                raise _Refused("command_interactive", name)
            if mode == "inline":
                return parts + self.nested(script, depth)
        if name == "eval":
            if not args:
                raise _Refused("command_malformed", "eval without a command")
            return parts + self.nested(" ".join(args), depth)
        module = None
        if _INTERPRETER.match(name):
            mode, module = self.interpreter_mode(name, args)
            if mode == "interactive":
                raise _Refused("command_interactive", name)
            if mode == "inline":
                raise _Refused("command_inline_code", name)
        if name in _INLINE_ALWAYS:
            raise _Refused("command_inline_code", name)
        if name in _NETWORK:
            raise _Refused("command_network", name)
        if self.installs(name, args, module):
            raise _Refused("command_package_install", " ".join(argv[:3]))
        if name == "git" and self.git_denied(args):
            raise _Refused("command_git_denied")
        if name == "ln" or (name == "cp" and any(
                a in ("--symbolic-link", "--link") or (a.startswith("-") and not a.startswith("--")
                                                       and ("s" in a or "l" in a)) for a in args)):
            raise _Refused("command_symlink")
        if name in _MODE_CHANGE:
            raise _Refused("command_mode_change")
        if name == "find" and any(a in _FIND_EXEC for a in args):
            raise _Refused("command_exec_option", name)
        checked = args
        if name == "sed":
            scripts, indices, _, _, file_script = self.sed_parse(args)
            if file_script or not scripts or not all(_sed_safe(x) for x in scripts):
                raise _Refused("command_inline_code", name)
            checked = [a for index, a in enumerate(args) if index not in indices]
        for arg in checked:
            self.argument(arg)
        if self.policy.owner_policy_state == "missing":
            raise _Refused("owner_policy_missing")
        if self.policy.owner_policy_state == "changed":
            raise _Refused("owner_policy_changed")
        parts.extend(self.targets(name, args))
        if any(tuple(argv[:len(rule)]) == rule for rule in self.policy.command_rules):
            parts.append(_Part(ALLOW, "command_allowed", name))
        else:
            parts.append(self.checker.unlisted("unlisted_command", "command_not_allowed",
                                               " ".join(argv[:2])))
        return parts

    def redirections(self, redirects: list[tuple[str, str]]) -> list[_Part]:
        parts = []
        for kind, target in redirects:
            if kind == "dup":
                continue
            if target in _DEVICES:
                continue
            if kind == "read":
                self.checker.read_path(target, self.base)
                continue
            parts.append(self.checker.write_path(target, "modify" if kind == "append" else "replace",
                                                 self.base))
        return parts

    @staticmethod
    def shell_mode(args: list[str]) -> tuple[str, str | None]:
        inline = False
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                i += 1
                break
            if arg in ("-", "+"):
                return "interactive", None
            if arg.startswith("--"):
                i += 2 if arg in ("--rcfile", "--init-file") else 1
                continue
            if arg[0] in "-+":
                letters = arg[1:]
                if "i" in letters or "s" in letters:
                    return "interactive", None
                if "c" in letters:
                    inline = True
                i += 2 if ("o" in letters or "O" in letters) else 1
                continue
            break
        rest = args[i:]
        if inline:
            if not rest:
                raise _Refused("command_malformed", "a shell -c without a command")
            return "inline", rest[0]
        if not rest:
            return "interactive", None
        return "script", None

    @staticmethod
    def interpreter_mode(name: str, args: list[str]) -> tuple[str, str | None]:
        if name == "deno":
            first = _first_word(args)
            if first is None or first == "repl":
                return "interactive", None
            if first == "eval" or any(a in ("-e", "--eval") for a in args):
                return "inline", None
            return "script", None
        inline, interactive, module_letters, value_letters = _FAMILIES[_family(name)]
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "-":
                return "interactive", None
            if arg == "--":
                i += 1
                break
            if arg.startswith("--"):
                option = arg.split("=", 1)[0]
                if option in _LONG_INLINE:
                    return "inline", None
                if option == "--interactive":
                    return "interactive", None
                i += 2 if option in _LONG_VALUE and "=" not in arg else 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                letters = arg[1:]
                skip = 1
                for index, letter in enumerate(letters):
                    if letter in inline:
                        return "inline", None
                    if letter in interactive:
                        return "interactive", None
                    if letter in module_letters:
                        attached = letters[index + 1:]
                        if attached:
                            return "module", attached + "\0" + " ".join(args[i + 1:])
                        if i + 1 >= len(args):
                            return "interactive", None
                        return "module", args[i + 1] + "\0" + " ".join(args[i + 2:])
                    if letter in value_letters:
                        if not letters[index + 1:]:
                            skip = 2
                        break
                i += skip
                continue
            break
        rest = args[i:]
        if not rest:
            return "interactive", None
        if "/" not in rest[0] and "." not in rest[0]:
            # Not recognisably a script: an option's value read as one would leave the
            # interpreter reading code from its input.
            return "interactive", None
        return "script", None

    @staticmethod
    def installs(name: str, args: list[str], module: str | None) -> bool:
        if module is not None:
            module_name, _, rest = module.partition("\0")
            root = module_name.split(".")[0]
            if root == "ensurepip":
                return True
            if _PIP.match(root):
                return any(word in _PIP_INSTALL for word in rest.split())
            return False
        # Any word, not only the first: an option's value can stand before the subcommand.
        if name in _PACKAGE_ALWAYS:
            return True
        if _PIP.match(name):
            return any(word in _PIP_INSTALL for word in args)
        if name == "yarn" and _first_word(args) is None:
            return True
        subcommands = _PACKAGE_SUBCOMMANDS.get(name)
        if subcommands is None:
            return False
        # Single letters (npm i, npm x) count only as the subcommand itself.
        return _first_word(args) in subcommands or any(
            len(word) > 1 and word in subcommands for word in args)

    @staticmethod
    def git_denied(args: list[str]) -> bool:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i].split("=", 1)[0] in _GIT_GLOBAL_DENIED:
                return True
            i += 1
        if i >= len(args):
            return False
        sub, rest = args[i], args[i + 1:]
        if sub in _GIT_DENIED:
            return True
        for arg in rest:
            if arg.split("=", 1)[0] in _GIT_DENIED_OPTIONS:
                return True
            if sub == "grep" and arg.startswith("-O"):
                return True
        return False

    def argument(self, arg: str) -> None:
        """Paths inside an argument: no '..', and absolute paths only inside the workspace."""

        pieces = re.split(r"[=:,]", arg)
        attached = re.match(r"^-[A-Za-z](/.*)$", arg)
        if attached:
            pieces.append(attached.group(1))
        for piece in pieces:
            if ".." in piece.split("/"):
                raise _Refused("path_traversal", arg)
        for piece in pieces:
            if piece.startswith("/") and piece not in _DEVICES:
                self.checker.resolve(piece, self.base)

    def targets(self, name: str, args: list[str]) -> list[_Part]:
        """The paths a known file-writing command writes, each checked as a write."""

        targets: list[str] = []
        kind = "delete" if name in _DELETERS else "replace"
        if name in _DELETERS or name in _WRITERS:
            options_done = False
            skip = False
            for arg in args:
                if skip:
                    skip = False
                    continue
                if not options_done and arg == "--":
                    options_done = True
                    continue
                if not options_done and arg.startswith("-") and arg != "-":
                    if (name, arg) in (("truncate", "-s"), ("mkdir", "-m"), ("install", "-m")):
                        skip = True
                    elif arg.startswith("--") and "=" in arg:
                        # --target-directory=PATH and the like: the value may be written.
                        targets.append(arg.split("=", 1)[1])
                    elif (name in ("cp", "mv", "install") and not arg.startswith("--")
                          and "t" in arg[1:] and arg[1:].split("t", 1)[1]):
                        targets.append(arg[1:].split("t", 1)[1])
                    continue
                targets.append(arg)
        elif name == "dd":
            targets = [a[3:] for a in args if a.startswith("of=")]
        elif name == "sed":
            _, _, files, in_place, _ = self.sed_parse(args)
            targets = files if in_place else []
            kind = "modify"
        recursive = name == "rm" and any(
            a == "--recursive" or (a.startswith("-") and not a.startswith("--")
                                   and ("r" in a or "R" in a)) for a in args)
        parts = []
        for raw in targets:
            if raw in _DEVICES:
                continue
            rel = self.checker.resolve(raw, self.base)
            if recursive and not rel:
                raise _Refused("command_delete_outside_scope", raw)
            try:
                part = self.checker.write(rel, kind, raw)
            except _Refused as refused:
                if recursive and refused.rule == "write_outside_scope":
                    raise _Refused("command_delete_outside_scope", raw) from None
                raise
            if recursive and part.rule == "write_outside_scope":
                raise _Refused("command_delete_outside_scope", raw)
            parts.append(part)
        return parts

    @staticmethod
    def sed_parse(args: list[str]) -> tuple[list[str], set[int], list[str], bool, bool]:
        """sed's scripts, the argument indices holding them, its files, -i, and -f."""

        scripts: list[str] = []
        indices: set[int] = set()
        files: list[str] = []
        in_place = file_script = False
        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("--"):
                option, has_value, value = arg.partition("=")
                if option == "--in-place":
                    in_place = True
                elif option in ("--expression", "--file"):
                    file_script = file_script or option == "--file"
                    if has_value:
                        scripts.append(value)
                        indices.add(i)
                    elif i + 1 < len(args):
                        scripts.append(args[i + 1])
                        indices.add(i + 1)
                        i += 1
                i += 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                letters = arg[1:]
                for index, letter in enumerate(letters):
                    if letter == "i":
                        in_place = True
                        break
                    if letter in "ef":
                        file_script = file_script or letter == "f"
                        attached = letters[index + 1:]
                        if attached:
                            scripts.append(attached)
                            indices.add(i)
                        elif i + 1 < len(args):
                            scripts.append(args[i + 1])
                            indices.add(i + 1)
                            i += 1
                        break
                    if letter == "l" and not letters[index + 1:]:
                        i += 1
                        break
                i += 1
                continue
            if not scripts:
                scripts.append(arg)
                indices.add(i)
            else:
                files.append(arg)
            i += 1
        return scripts, indices, files, in_place, file_script


def _sed_part_end(script: str, start: int, delimiter: str) -> int:
    i = start
    while i < len(script):
        if script[i] == "\\":
            i += 2
            continue
        if script[i] == delimiter:
            return i
        i += 1
    return -1


def _sed_address(script: str, i: int) -> tuple[int, bool]:
    n = len(script)
    if i < n and script[i].isdigit():
        while i < n and (script[i].isdigit() or script[i] == "~"):
            i += 1
        return i, True
    if i < n and script[i] == "$":
        return i + 1, True
    if i < n and script[i] == "/":
        end = _sed_part_end(script, i + 1, "/")
        if end < 0:
            return -1, False
        i = end + 1
        while i < n and script[i] in "IM":
            i += 1
        return i, True
    return i, False


def _sed_safe(script: str) -> bool:
    """True only for scripts of addresses with p, d, q, =, n, hold-space, s and y commands and
    harmless flags: nothing that writes (w, W), reads (r, R) or executes (e)."""

    if "[" in script or any(_odd_char(c) for c in script):
        return False
    i, n = 0, len(script)
    while i < n:
        if script[i] in " ;{}":
            i += 1
            continue
        i, had = _sed_address(script, i)
        if i < 0:
            return False
        if had and i < n and script[i] == ",":
            i, had = _sed_address(script, i + 1)
            if i < 0 or not had:
                return False
        while i < n and script[i] in " !":
            i += 1
        if i >= n:
            return False
        command = script[i]
        if command in "pdPDnNgGhHxl=":
            i += 1
        elif command in "qQ":
            i += 1
            while i < n and script[i].isdigit():
                i += 1
        elif command in "sy":
            if i + 1 >= n:
                return False
            delimiter = script[i + 1]
            if delimiter in "\\ ;{}" or delimiter.isalnum():
                return False
            middle = _sed_part_end(script, i + 2, delimiter)
            last = _sed_part_end(script, middle + 1, delimiter) if middle >= 0 else -1
            if last < 0:
                return False
            i = last + 1
            if command == "s":
                while i < n and script[i] in "gpiImM0123456789":
                    i += 1
        else:
            return False
        if i < n and script[i] not in " ;}":
            return False
    return True


# --------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------

_WRITE_TOOLS = {"Write": ("file_path", "replace"), "Edit": ("file_path", "modify"),
                "MultiEdit": ("file_path", "modify"), "NotebookEdit": ("notebook_path", "modify")}
_READ_TOOLS = {"Read": "file_path", "LS": "path", "NotebookRead": "notebook_path",
               "view_image": "path"}
_COMMAND_TOOLS = frozenset({"Bash", "exec_command", "shell", "local_shell"})
_PATCH_TOOLS = frozenset({"apply_patch"})
_NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch", "web_search", "web_fetch"})


def _combine(parts: list[_Part]) -> _Part:
    for part in parts:
        if part.outcome == REFUSE:
            return part
    escalations = [p for p in parts if p.outcome == ESCALATE]
    if escalations:
        return min(escalations, key=lambda p: _ESCALATION_ORDER.index(p.rule)
                   if p.rule in _ESCALATION_ORDER else len(_ESCALATION_ORDER))
    return parts[0]


def _decide(policy: GatePolicy, request: object) -> tuple[_Part, bool]:
    if not isinstance(request, Mapping):
        raise _Refused("request_malformed")
    tool = request.get("tool_name")
    tool_input = request.get("tool_input")
    cwd = request.get("cwd")
    if not isinstance(tool, str) or not tool or not isinstance(tool_input, Mapping):
        raise _Refused("request_malformed")
    if not isinstance(cwd, str):
        raise _Refused("request_malformed")
    checker = _Checker(policy, ())
    if not cwd.startswith("/"):
        raise _Refused("cwd_outside_workspace", cwd)
    try:
        checker.cwd = checker.resolve(cwd)
    except _Refused:
        raise _Refused("cwd_outside_workspace", cwd) from None

    if tool in _NETWORK_TOOLS:
        raise _Refused("network_tool")
    if tool.startswith("mcp__"):
        raise _Refused("mcp_tool")
    if tool in _WRITE_TOOLS:
        field, kind = _WRITE_TOOLS[tool]
        return checker.write_path(tool_input.get(field), kind), True
    if tool in _READ_TOOLS:
        checker.read_path(tool_input.get(_READ_TOOLS[tool]))
        return _Part(ALLOW, "read_in_workspace", ""), policy.record_reads
    if tool == "Glob":
        base = checker.read_path(tool_input["path"]) if tool_input.get("path") is not None else None
        checker.read_path(tool_input.get("pattern"), base)
        return _Part(ALLOW, "read_in_workspace", ""), policy.record_reads
    if tool == "Grep":
        base = checker.read_path(tool_input["path"]) if tool_input.get("path") is not None else None
        if tool_input.get("glob") is not None:
            checker.read_path(tool_input["glob"], base if base is not None else checker.cwd)
        return _Part(ALLOW, "read_in_workspace", ""), policy.record_reads
    if tool in _PATCH_TOOLS:
        text = tool_input.get("command", tool_input.get("input"))
        if isinstance(text, list) and len(text) == 2 and text[0] == "apply_patch":
            text = text[1]
        parts = [checker.write_path(path, kind) for path, kind in _patch_ops(text)]
        return _combine(parts), True
    if tool in _COMMAND_TOOLS:
        if tool_input.get("run_in_background"):
            raise _Refused("command_background")
        base = checker.cwd
        workdir = tool_input.get("workdir")
        if workdir is not None:
            try:
                base = checker.resolve(workdir)
            except _Refused:
                raise _Refused("cwd_outside_workspace", workdir) from None
        commands = _Commands(checker, base)
        command = tool_input.get("command", tool_input.get("cmd"))
        parts = commands.shell(command, 0) if isinstance(command, str) else commands.argv(command)
        combined = _combine(parts)
        if combined.outcome == ALLOW:
            combined = _Part(ALLOW, "command_allowed", "")
        return combined, True
    raise _Refused("tool_unknown", tool)


def decide(policy: GatePolicy, request: object) -> Decision:
    """Allow, refuse or escalate one PreToolUse request. Pure: no I/O, no clock, no state."""

    try:
        part, record = _decide(policy, request)
    except _Refused as refused:
        return Decision(REFUSE, refused.rule, _reason(refused.rule, REFUSE, refused.subject), True)
    if part.outcome != ALLOW:
        record = True
    return Decision(part.outcome, part.rule, _reason(part.rule, part.outcome, part.subject), record)


# --------------------------------------------------------------------------------------------
# The record row
# --------------------------------------------------------------------------------------------

def _subject(request: object) -> str:
    if not isinstance(request, Mapping) or not isinstance(request.get("tool_input"), Mapping):
        return ""
    tool, tool_input = request.get("tool_name"), request["tool_input"]
    if tool in _PATCH_TOOLS:
        text = tool_input.get("command", tool_input.get("input"))
        if isinstance(text, list) and len(text) == 2 and text[0] == "apply_patch":
            text = text[1]
        try:
            return "\n".join(path for path, _ in _patch_ops(text))
        except _Refused:
            return text if isinstance(text, str) else ""
    if tool in _COMMAND_TOOLS:
        command = tool_input.get("command", tool_input.get("cmd"))
        if isinstance(command, list) and all(isinstance(w, str) for w in command):
            return shlex.join(command)
        return command if isinstance(command, str) else ""
    for field in ("file_path", "notebook_path", "path", "pattern", "url", "query"):
        value = tool_input.get(field)
        if isinstance(value, str):
            return value
    return ""


def record_row(*, run_id: str, request: object, decision: Decision, policy: GatePolicy,
               requested_at: str, decided_at: str, decided_by: str = "gate") -> dict:
    """One ledger row for one request and its answer, matched to the client's tool id."""

    mapping = request if isinstance(request, Mapping) else {}
    tool_use_id = mapping.get("tool_use_id")
    tool_name = mapping.get("tool_name")
    subject = _subject(request)
    return {
        "run_id": run_id,
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
        "session_id": mapping.get("session_id") if isinstance(mapping.get("session_id"), str) else None,
        "tool_name": tool_name if isinstance(tool_name, str) else None,
        "subject": subject[:RECORD_SUBJECT_LIMIT],
        "subject_truncated": len(subject) > RECORD_SUBJECT_LIMIT,
        "subject_sha256": hashlib.sha256(subject.encode("utf-8", "surrogatepass")).hexdigest(),
        "outcome": decision.outcome,
        "rule": decision.rule,
        "reason": decision.reason,
        "decided_by": decided_by,
        "policy_digest": policy.digest,
        "requested_at": requested_at,
        "decided_at": decided_at,
    }
