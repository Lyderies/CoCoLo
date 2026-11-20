"""The check-in gate's policy: one pure decision per hook request (M3-30 decisions, M3-2 at the gate).

Every write, edit, patch and command a write child makes is sent, before it runs, to the run's
supervisor gate. This module is the gate's judgement and nothing else: no I/O, no clock, no
process. `decide(policy, request)` takes one PreToolUse input as Claude Code or Codex sends it and
answers allow, refuse or escalate (ask the owner), with the rule that decided and a plain reason
the child is shown. `build_policy(grant, owner_policy)` makes the run's policy from the
requirement's grant intersected with the owner's per-project policy; the intersection is exact
and never wider than either side. `record_row` shapes the ledger row for one request and answer.

The policy (RESEARCH-0.4.0-checkin "Recommended design", made default-deny after the slice-3
adversarial review):
- Always refused, whatever the grant: anything outside /workspace; '..' anywhere; control and
  invisible characters in names; git internals and `.gitmodules` at any depth and in any case;
  agent configuration (`.claude/`, `.codex/`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md` and their
  local and override forms) at any depth; `.env*`; COCOLO's own `.cocolo*` names; web tools and
  MCP tools; tools and tool fields the gate does not know; network tools, package installs,
  privilege changes, git commands that change history, configuration or the network, links, mode
  changes, recursive deletes outside the scope, find's exec options; bare or interactive
  interpreters and shells, code read from standard input or a terminal; shells other than bash,
  sh and dash, and shell options other than -e, -u, -x, -l and -o errexit|nounset|pipefail|xtrace;
  NAME=value words after a command name; arguments read from a file (pytest @file); inline code
  (awk, -c/-e forms, perl -M, node loaders given as URLs, sed scripts outside a small grammar);
  wrappers that run another command in a changed context; shell constructs whose effect cannot be
  read.
- Default deny over arguments: every argument, of every program, is split on '=', ':' and ',' (and
  the attached values of clustered short options are included); any piece naming an always-
  refused path is refused, and absolute pieces must lie inside /workspace. For each program the
  gate knows (`_GRAMMARS`, git's subcommands, find, sed, python, pytest, node, perl, the shells)
  an explicit list of options says which take a value; long options are matched exactly, an
  abbreviation of a refused option is refused, and any other option the list does not name
  escalates (or refuses, when the owner escalates nothing). The writes a known program makes
  (output options, copy and move destinations and what lands inside them, in-place edits,
  deletions) are checked as writes; a separate option value that looks like an option is not
  taken as the value (the program may read it as an option). A shell or eval runs its command only
  when it is itself listed. A program with no grammar runs without asking only when its
  whole argv is listed exactly in every layer of the policy. Runners (`uv run`, `poetry run`,
  ...) are unwrapped and the inner command is checked as a command of its own.
- Grant-only paths (code that runs later: CI, hooks, build and test configuration, lock files):
  refused unless the requirement asked for the path and the owner's policy grants it; a folder
  copied or moved onto a place that could hold one is refused; a recursive copy escalates.
- Allowed: writes inside the requirement's paths that the owner's policy also covers, commands
  that pass the above and match a rule in both lists, and reads inside /workspace.
- Everything else that is not refused escalates by default (the owner's choice O5, a parameter):
  unlisted writes and commands, unknown options, edits of existing tests, deletions, dependency
  manifests. An escalation names every reason found in the request and, for a command, the
  full command.

Paths are compared case-insensitively and after NFKC for the deny classes (the macOS workspace
is case-insensitive) and exactly for grants, so a variant spelling can only narrow. Commands are
read with a quote-aware scanner that refuses anything it cannot split safely, then split into
words by `shlex`; it assumes a POSIX shell (bash, sh or dash) and GNU tools run them. Codex
patches are read by their `*** Add File:`, `*** Update File:`, `*** Delete File:` and
`*** Move to:` lines, relative to the call's workdir; a header with extra whitespace, an unknown
header, or a misplaced one refuses the whole patch.

The gate is one layer, not the wall (DESIGN-0.4.0-M3 "Gate review, default deny"). What it
cannot see: effects inside an allowed program (a test run, a script, what an allowed tool reads
from its own configuration); symlinks already in the workspace; what a folder being moved holds;
a Claude `Write` that overwrites an existing test (treated as a create); package.json and
pyproject.toml lifecycle content. The container (workspace the only writable mount, .git and
agent configuration read-only) and the slice-7 patch gate (deny classes and grant-only paths
checked on the final diff before any merge) hold those.
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
    command_layers: tuple[frozenset[tuple[str, ...]], ...]
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
    "request_field_unknown": "the request has the field {s}, which the supervisor does not check",
    "request_field_value": "the request's {s} setting is not allowed",
    "request_ambiguous": "the request gives {s} twice, so what would happen cannot be known",
    "pattern_unreadable": "the pattern {s} could not be read safely",
    "cwd_outside_workspace": "the working directory is outside /workspace",
    "tool_unknown": "the tool {s} is not available to this assignment",
    "network_tool": "web access is not available to this assignment",
    "mcp_tool": "MCP tools are not available to this assignment",
    "path_odd_name": "the path has control or invisible characters",
    "path_traversal": "{s} uses '..'; name the path inside /workspace directly",
    "path_outside_workspace": "{s} is outside /workspace",
    "write_git": "{s} names git's own files; changes there are never allowed",
    "write_agent_config": "{s} names agent configuration; changes there are never allowed",
    "write_env": "{s} names an environment or secrets file; changes there are never allowed",
    "write_cocolo": "{s} names COCOLO's own files; changes there are never allowed",
    "write_conditional_ungranted": ("{s} holds code that runs later (CI, hooks, build or test "
                                    "configuration) and this assignment was not granted it"),
    "write_outside_scope": "{s} is outside this assignment's allowed paths",
    "write_tree": ("{s} may receive a folder or link whose contents the supervisor cannot see (agent "
                   "configuration or code that runs later could be among them)"),
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
    "command_shell_option": ("the shell option {s} changes how commands are read; only -e, -u, -x, -l "
                             "and -o errexit, nounset, pipefail or xtrace are allowed"),
    "command_shell_unsupported": "{s} is not a shell the supervisor can read; use bash or sh",
    "command_argument_file": "{s} reads more arguments from a file the supervisor cannot see",
    "command_write_option": "{s} writes files where the supervisor cannot check them",
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
    "command_unknown_program": ("the supervisor cannot read the arguments of {s}, so only a "
                                "command listed exactly runs without asking"),
    "command_unknown_option": "{s} uses an option or form the supervisor does not know",
    "command_allowed": "the command is on this assignment's command list",
}

# Which escalation wins when several parts of one request escalate.
_ESCALATION_ORDER = ("write_outside_scope", "command_not_allowed", "command_unknown_program",
                     "command_unknown_option", "write_deletion", "write_tree", "write_test_edit",
                     "write_dependency_manifest")


def _show(value: object, limit: int | None = 160) -> str:
    text = value if isinstance(value, str) else repr(value)
    shown = "".join("?" if unicodedata.category(c)[0] == "C" or unicodedata.category(c) in
                    ("Zl", "Zp") else c for c in text)
    return shown if limit is None or len(shown) <= limit else shown[:limit - 3] + "..."


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
    glob with `*`, `?` and `**`) and `commands` (argv prefix rules for programs whose arguments
    the gate reads, exact argv for any other program, which then must be listed exactly in every
    layer; the intersection itself is unchanged). A path is writable only if
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
        "command_layers": tuple(frozenset(layer["commands"]) for layer in layers)
        if state == "present" else (frozenset(),),
        "escalate": escalate,
        "record_reads": record_reads,
        "owner_wait_seconds": owner_wait_seconds,
        "owner_policy_state": state,
        "owner_policy_digest": digest,
    }
    plain = dict(fields, escalate=sorted(escalate),
                 write_layers=[list(x) for x in fields["write_layers"]],
                 conditional_layers=[list(x) for x in fields["conditional_layers"]],
                 command_rules=[list(x) for x in fields["command_rules"]],
                 command_layers=[sorted(list(x) for x in layer) for layer in fields["command_layers"]])
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
    "bun.lockb", "bun.lock", "pdm.lock", "pixi.lock", "deno.lock",
    ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml", ".drone.yml",
    "jenkinsfile", "build.rs", "justfile", ".justfile",
    ".pytest.ini", "pytest.toml", ".pytest.toml", "mypy.ini", ".rspec", "rakefile", "build.gradle",
    "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml", "cmakelists.txt",
    "bunfig.toml", "deno.json", "deno.jsonc", "taskfile.yml", "taskfile.yaml",
    ".pnpmfile.cjs", "phpunit.xml", "phpunit.xml.dist", "package.swift", "mix.exs", "gradlew",
    "gradlew.bat", "mvnw", "mvnw.cmd", "gradle.properties", "lefthook.yml", "lefthook.yaml",
    ".lefthook.yml", "hatch.toml", "pixi.toml", "meson.build", "meson_options.txt",
    "rust-toolchain", "rust-toolchain.toml", "containerfile", "dockerfile",
})
_CONDITIONAL_SUFFIXES = (".pth", ".mk", ".props", ".targets", ".bazel", ".bzl", ".dockerfile")
# Name patterns, since a list of names is never complete: tool configuration (`x.config.js`,
# `x.workspace.ts`), dot-rc files (`.babelrc`, `.eslintrc.js`) and Dockerfile variants. Matched on the
# file name only, so `src/config.py` or `src/rc.py` stay ordinary source.
_CONDITIONAL_PATTERN = re.compile(
    r"^(.+\.(config|workspace)\.[^.]+|\.[a-z0-9_.-]*rc|\.[a-z0-9_-]*rc\.[a-z0-9]+"
    r"|(dockerfile|containerfile)\..+)$")
# Bazel's BUILD and WORKSPACE files, matched in their own case so that build/ output folders stay
# ordinary.
_CONDITIONAL_EXACT_CASE = frozenset({"BUILD", "WORKSPACE"})
_CONDITIONAL_PREFIXES = ("jest.config.", "vitest.config.", ".mocharc", "karma.conf.")
_CONDITIONAL_SEQUENCES = ((".github",), (".gitlab",), (".circleci",), (".buildkite",), (".mvn",),
                          ("gradle", "wrapper"),
                          (".devcontainer",), (".githooks",), (".husky",), (".agents",), (".vscode",),
                          (".idea",), (".cargo", "config.toml"), (".cargo", "config"))
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
            or base.startswith(_CONDITIONAL_PREFIXES) or _CONDITIONAL_PATTERN.match(base)
            or rel[-1] in _CONDITIONAL_EXACT_CASE):
        return True
    return any(keys[i:i + len(seq)] == seq for seq in _CONDITIONAL_SEQUENCES
               for i in range(len(keys)))


def _is_test(rel: tuple[str, ...]) -> bool:
    keys = [_key(p) for p in rel]
    return any(k in _TEST_DIRS for k in keys[:-1]) or bool(_TEST_NAME.match(keys[-1]))


def _holds_conditional(rel: tuple[str, ...]) -> bool:
    """True when a folder written at rel could hold a grant-only path below it."""

    keys = tuple(_key(p) for p in rel)
    return any(len(keys) - i < len(seq) and seq[:len(keys) - i] == keys[i:]
               for seq in _CONDITIONAL_SEQUENCES for i in range(len(keys)))


def _is_dependency(rel: tuple[str, ...]) -> bool:
    base = _key(rel[-1])
    if len(rel) > 1 and _key(rel[-2]) == "requirements" and base.endswith((".txt", ".in")):
        return True
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
        if raw.startswith("~"):
            raise _Refused("path_outside_workspace", raw)
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
_ASCII_DIGITS = re.compile(r"^[0-9]+$")
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
# Runners that run the command after their subcommand (`uv run pytest`): unwrapped, and the inner
# command is checked as a command of its own.
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run",
            "rye": "run", "pixi": "run", "conda": "run", "mamba": "run", "micromamba": "run",
            "bundle": "exec"}
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
_GIT_GLOBAL_ALLOWED = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs",
                                 "--noglob-pathspecs", "--glob-pathspecs", "--icase-pathspecs",
                                 "--no-replace-objects", "--version"})
_GIT_DENIED = frozenset({
    "push", "commit", "reset", "clean", "config", "remote", "fetch", "pull", "clone",
    "submodule", "update-ref", "update-index", "symbolic-ref", "apply", "am", "checkout",
    "restore", "switch", "stash", "merge", "rebase", "cherry-pick", "revert", "mv", "rm",
    "worktree", "filter-branch", "filter-repo", "replace", "notes", "gc", "prune", "repack",
    "init", "hook", "daemon", "instaweb", "send-email", "difftool", "mergetool", "credential",
    "bundle", "fast-import", "archive", "upload-pack", "receive-pack", "ls-remote",
    "maintenance", "sparse-checkout", "lfs", "add", "stage", "bisect", "checkout-index",
    "format-patch", "fetch-pack", "send-pack", "http-fetch", "http-push", "read-tree",
    "write-tree", "commit-tree", "update-server-info", "upload-archive", "merge-file",
    "merge-index", "mktag", "mktree", "hash-object", "unpack-objects", "pack-refs", "reflog",
    "rerere", "imap-send", "svn", "p4", "cvsimport", "quiltimport", "request-pull",
})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_PIP = re.compile(r"^pip\d*(\.\d+)?$")
_PYTHON = re.compile(r"^(python\d*(\.\d+)?|pypy\d*(\.\d+)?)$")
_INTERPRETER = re.compile(
    r"^(python\d*(\.\d+)?|pypy\d*(\.\d+)?|node|nodejs|bun|deno|perl\d*(\.\d+)*|ruby\d*(\.\d+)*"
    r"|php\d*(\.\d+)*|lua\d*(\.\d+)*|luajit|R|Rscript|julia|tclsh\d*(\.\d+)*|wish|osascript)$")
# Per interpreter family: inline-code letters, interactive letters, module letters, and letters
# whose value is the next word when not attached.
_FAMILIES = {
    "python": ("c", "i", "m", "WX"),
    "node": ("ep", "i", "", "rC"),
    "perl": ("eEmM", "d", "", "I"),
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
# Python modules that read code from the terminal, or run code given on the command line.
_MODULES_INTERACTIVE = frozenset({"code", "pdb", "asyncio", "idlelib", "sqlite3", "turtledemo",
                                  "tkinter"})
_MODULES_INLINE = frozenset({"timeit"})
_PACKAGE_SUBCOMMANDS = {
    "npm": {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
            "isntall", "add", "ci", "clean-install", "ic", "install-clean", "isntall-clean",
            "install-test", "it", "install-ci-test", "cit", "clean-install-test", "sit", "update",
            "upgrade", "up", "udpate", "exec", "x", "link", "ln", "create", "init", "innit",
            "rebuild", "rb", "uninstall", "remove", "rm", "r", "un", "unlink", "dedupe", "ddp",
            "prune"},
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
    "pdm": {"add", "install", "sync", "update", "remove", "lock", "self"},
    "pixi": {"add", "install", "update", "upgrade", "remove", "global", "exec"},
    "deno": {"add", "install", "uninstall", "remove"},
    "composer": {"install", "require", "update", "remove"},
    "bundle": {"install", "add", "update"},
    "uv": {"add", "remove", "sync", "tool", "pip", "lock", "venv"},
    "hatch": {"env", "dep"},
}
_PACKAGE_ALWAYS = frozenset({"npx", "pnpx", "bunx", "pipx", "uvx", "brew", "apt", "apt-get",
                             "dpkg", "dnf", "yum", "apk", "pacman", "zypper", "port", "snap",
                             "flatpak", "easy_install"})
_PIP_INSTALL = frozenset({"install", "download", "wheel", "uninstall"})
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class _Grammar:
    """The options one program accepts; anything else is unknown (default deny).

    Long options are matched exactly, never as abbreviations: an unknown long option that is a
    prefix of a refused one is refused, any other unknown option escalates.
    """

    short: str = ""                       # letters without a value
    short_value: str = ""                 # letters with a value (attached or the next word)
    short_optional: str = ""              # letters whose value can only be attached
    long: frozenset = frozenset()         # long options without a value
    long_value: frozenset = frozenset()   # long options with a value (=VALUE or the next word)
    long_optional: frozenset = frozenset()  # long options whose value can only follow '='
    long_equals: frozenset = frozenset()  # long options whose value must follow '='
    refuse: Mapping = None                # option ('-x' or '--name') -> the refusing rule
    stop: str = ""                        # value letters after which every word is an operand
    numeric: bool = False                 # -NUM is an option (a count)
    permute: bool = True                  # GNU: options may follow operands
    literal: bool = False                 # echo: words that are not all known letters are text
    operands: bool = True                 # operands are accepted at all


def _g(short="", short_value="", short_optional="", long=(), long_value=(), long_optional=(),
       refuse=None, long_equals=(), **kwargs) -> _Grammar:
    return _Grammar(short, short_value, short_optional, frozenset(long), frozenset(long_value),
                    frozenset(long_optional), frozenset(long_equals), dict(refuse or {}), **kwargs)


def _parse(grammar: _Grammar, args: list[str], clean: set[int] | None = None, offset: int = 0):
    """(options, operands, unknown) as the program would read args. Indices (plus offset) of
    short-option clusters read completely are added to clean: their attached values are known,
    so the argument check uses those values instead of every tail of the word."""

    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    unknown: list[str] = []
    refuse = grammar.refuse or {}
    i, n = 0, len(args)
    while i < n:
        arg = args[i]
        if arg == "--":
            operands.extend(args[i + 1:])
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in refuse:
                raise _Refused(refuse[name], arg)
            if name in grammar.long_value:
                if not eq:
                    if i + 1 >= n or args[i + 1].startswith("-"):
                        # A next word that looks like an option: the program may read it as one.
                        unknown.append(arg)
                        i += 1
                        continue
                    i += 1
                    value = args[i]
                options.append((name, value))
            elif name in grammar.long_equals:
                if eq:
                    options.append((name, value))
                else:
                    unknown.append(arg)
            elif name in grammar.long_optional:
                options.append((name, value if eq else None))
            elif name in grammar.long and not eq:
                options.append((name, None))
            else:
                for refused, rule in refuse.items():
                    if refused.startswith("--") and len(name) > 2 and refused.startswith(name):
                        raise _Refused(rule, arg)
                unknown.append(arg)
            i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            if grammar.numeric and _ASCII_DIGITS.match(letters):
                options.append(("-NUM", letters))
                i += 1
                continue
            if grammar.literal and not all(c in grammar.short for c in letters):
                operands.append(arg)
                i += 1
                continue
            stop = False
            start, complete = i, True
            for index, letter in enumerate(letters):
                key = "-" + letter
                if key in refuse:
                    raise _Refused(refuse[key], arg)
                if letter in grammar.short_value:
                    value = letters[index + 1:]
                    if not value:
                        if i + 1 >= n or args[i + 1].startswith("-"):
                            unknown.append(arg)
                            complete = False
                            break
                        i += 1
                        value = args[i]
                    options.append((key, value))
                    stop = letter in grammar.stop
                    break
                if letter in grammar.short_optional:
                    options.append((key, letters[index + 1:] or None))
                    break
                if letter in grammar.short:
                    options.append((key, None))
                    continue
                unknown.append(arg)
                complete = False
                break
            if complete and clean is not None:
                clean.add(start + offset)
            i += 1
            if stop:
                operands.extend(args[i:])
                break
            continue
        if not grammar.permute:
            operands.extend(args[i:])
            break
        operands.append(arg)
        i += 1
    return options, operands, unknown


def _has(options: list[tuple[str, str | None]], *names: str) -> bool:
    return any(name in names for name, _ in options)


def _values(options: list[tuple[str, str | None]], *names: str) -> list[str]:
    return [value for name, value in options if name in names and value is not None]


_LS = _g("aAlhRtrS1dFpinsxmCgoGUvcuXQkLHB",
         long=("--all", "--almost-all", "--human-readable", "--recursive", "--reverse", "--classify",
               "--directory", "--inode", "--numeric-uid-gid", "--size", "--group-directories-first",
               "--full-time", "--dereference", "--literal", "--quote-name", "--no-group", "--si",
               "--ignore-backups"),
         long_value=("--sort", "--time", "--format", "--width", "--block-size", "--ignore", "--hide",
                     "--quoting-style"),
         long_optional=("--color", "--colour"))
_CAT = _g("AbeEnstTuv", long=("--show-all", "--number-nonblank", "--show-ends", "--number",
                              "--squeeze-blank", "--show-tabs", "--show-nonprinting"))
_HEAD = _g("qvz", "nc", numeric=True, long=("--quiet", "--silent", "--verbose", "--zero-terminated"),
           long_value=("--lines", "--bytes"))
_WC = _g("clmwL", long=("--bytes", "--chars", "--lines", "--words", "--max-line-length"))
_GREP = _g("EFGPiyvwxcLloqsbHhnTZzarRU", "efmABC", numeric=True,
           long=("--extended-regexp", "--fixed-strings", "--basic-regexp", "--perl-regexp",
                 "--ignore-case", "--no-ignore-case", "--invert-match", "--word-regexp",
                 "--line-regexp", "--count", "--files-without-match", "--files-with-matches",
                 "--only-matching", "--quiet", "--silent", "--no-messages", "--byte-offset",
                 "--with-filename", "--no-filename", "--line-number", "--initial-tab", "--null",
                 "--null-data", "--text", "--recursive", "--dereference-recursive",
                 "--line-buffered"),
           long_value=("--regexp", "--file", "--max-count", "--after-context", "--before-context",
                       "--context", "--include", "--exclude", "--exclude-dir", "--label",
                       "--binary-files"),
           long_optional=("--color", "--colour"))
_ECHO = _g("neE", literal=True)
_SORT = _g("bdfgiMhnRrsuVcCzm", "ktSo",
           long=("--ignore-leading-blanks", "--dictionary-order", "--ignore-case",
                 "--general-numeric-sort", "--ignore-nonprinting", "--month-sort",
                 "--human-numeric-sort", "--numeric-sort", "--random-sort", "--reverse",
                 "--version-sort", "--merge", "--stable", "--unique", "--zero-terminated"),
           long_value=("--key", "--field-separator", "--output", "--buffer-size", "--parallel", "--sort"),
           long_optional=("--check",),
           refuse={"--compress-program": "command_exec_option"})
_DIFF = _g("uqrNwbBiTtasy", "Cx", "U",
           long=("--recursive", "--brief", "--new-file", "--ignore-all-space", "--ignore-space-change",
                 "--ignore-blank-lines", "--ignore-case", "--text", "--report-identical-files",
                 "--side-by-side", "--no-dereference", "--strip-trailing-cr", "--expand-tabs"),
           long_value=("--exclude", "--context"), long_optional=("--unified", "--color"))
_TOUCH = _g("acm", "dt", long=("--no-create",), long_value=("--date",))
_MKDIR = _g("pv", long=("--parents", "--verbose"))
_RM = _g("rRfvd", long=("--recursive", "--force", "--verbose", "--dir", "--preserve-root"),
         refuse={"--no-preserve-root": "command_delete_outside_scope"})
_RMDIR = _g("pv", long=("--parents", "--ignore-fail-on-non-empty", "--verbose"))
_SYMLINK = {"-s": "command_symlink", "-l": "command_symlink", "--symbolic-link": "command_symlink",
            "--link": "command_symlink"}
# cp options that copy a link as a link (from its new depth a relative link may point elsewhere).
_CP_LINKS = {"-P": "command_symlink", "-d": "command_symlink", "-a": "command_symlink",
             "--no-dereference": "command_symlink", "--archive": "command_symlink"}
_CP = _g("RrfinpuvLHTx", "t",
         long=("--recursive", "--force", "--no-clobber", "--interactive", "--verbose",
               "--dereference", "--no-target-directory", "--one-file-system"),
         long_value=("--target-directory",),
         long_optional=("--preserve", "--update", "--reflink", "--sparse"),
         refuse={**_SYMLINK, **_CP_LINKS})
_MV = _g("finuvT", "t", long=("--force", "--no-clobber", "--interactive", "--verbose",
                              "--no-target-directory"),
         long_value=("--target-directory",), long_optional=("--update",), refuse=_SYMLINK)
_TEE = _g("ai", long=("--append", "--ignore-interrupts"))
_PLAIN = _g()
_PWD = _g("LP")
_PYTEST = _g("qvxsl", "kmWr",
             long=("--quiet", "--verbose", "--exitfirst", "--lf", "--last-failed", "--ff",
                   "--failed-first", "--nf", "--new-first", "--sw", "--stepwise", "--co",
                   "--collect-only", "--showlocals", "--strict-markers", "--strict-config",
                   "--no-header", "--no-summary", "--disable-warnings", "--version", "--markers",
                   "--fixtures", "--setup-show", "--runxfail"),
             long_value=("--maxfail", "--tb", "--durations", "--durations-min", "--deselect",
                         "--ignore", "--import-mode", "--capture"),
             long_optional=("--color",),
             refuse={"--pdb": "command_interactive", "--trace": "command_interactive",
                     "--pdbcls": "command_interactive"})
_UNITTEST = _g("vqfcb", "kspt", long=("--verbose", "--quiet", "--failfast", "--catch", "--buffer",
                                      "--locals"), long_value=("--durations",))
_PYTHON_GRAMMAR = _g("BdEhIOPqsSuvVbRx", "WXm", stop="m", permute=False,
                     long=("--version", "--help"), long_value=("--check-hash-based-pycs",),
                     refuse={"-c": "command_inline_code", "-i": "command_interactive"})
def _python_x_values(args: list[str]) -> frozenset[int]:
    """Indices of separate `-X` values among python's own options (read by the fixed -X list, so
    not checked as NAME=value); options end at -m, -c, '-', '--' or the first operand."""

    found = set()
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] not in ("-", "--"):
        word = args[i]
        if word.startswith("--"):
            i += 1
            continue
        for index, letter in enumerate(word[1:]):
            if letter in "WXmc":
                if index + 2 == len(word):
                    if letter == "X":
                        found.add(i + 1)
                    i += 1
                if letter in "mc":
                    return frozenset(found)
                break
        i += 1
    return frozenset(found)


# python -X options: the ones that only change how the run behaves, and the ones that write files.
_PYTHON_X = frozenset({"dev", "utf8", "importtime", "faulthandler", "tracemalloc",
                       "warn_default_encoding", "no_debug_ranges", "frozen_modules",
                       "int_max_str_digits", "showrefcount", "cpu_count", "disable_remote_debug"})
_PYTHON_X_WRITES = frozenset({"pycache_prefix", "perf", "perf_jit"})
_NODE_LOADERS = frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader"})
_NODE = _g("", "rC", permute=False,
           long=("--test", "--test-only", "--enable-source-maps", "--no-warnings", "--trace-warnings",
                 "--trace-uncaught", "--experimental-vm-modules", "--no-deprecation",
                 "--throw-deprecation"),
           long_value=("--require", "--import", "--loader", "--experimental-loader",
                       "--max-old-space-size", "--stack-size", "--test-reporter", "--test-name-pattern",
                       "--test-concurrency", "--unhandled-rejections", "--conditions"),
           refuse={"-e": "command_inline_code", "-p": "command_inline_code",
                   "--eval": "command_inline_code", "--print": "command_inline_code",
                   "-i": "command_interactive", "--interactive": "command_interactive"})
_PERL = _g("wWXTtcnpas", "I", "l0", permute=False,
           refuse={"-e": "command_inline_code", "-E": "command_inline_code",
                   "-M": "command_inline_code", "-m": "command_inline_code",
                   "-d": "command_interactive"})

_GRAMMARS = {
    "ls": _LS, "cat": _CAT, "head": _HEAD, "tail": _HEAD, "wc": _WC, "grep": _GREP, "echo": _ECHO,
    "sort": _SORT, "diff": _DIFF, "touch": _TOUCH, "mkdir": _MKDIR, "rm": _RM, "rmdir": _RMDIR,
    "cp": _CP, "mv": _MV, "tee": _TEE, "true": _PLAIN, "false": _PLAIN, "pwd": _PWD,
    "pytest": _PYTEST, "py.test": _PYTEST, "node": _NODE, "nodejs": _NODE,
}

# git: global options other than these are refused (the subcommand could not be found); each
# subcommand the gate knows has its own grammar; the others are refused or escalate.
_GIT_REFUSE_DIFF = {"--output": "command_git_denied", "--ext-diff": "command_git_denied",
                    "--textconv": "command_git_denied"}
_GIT_DIFF_LONG = ("--numstat", "--shortstat", "--name-only", "--name-status", "--summary", "--patch",
                  "--no-patch", "--raw", "--cached", "--staged", "--no-color", "--ignore-all-space",
                  "--ignore-space-change", "--ignore-blank-lines", "--ignore-space-at-eol",
                  "--exit-code", "--quiet", "--check", "--text", "--no-renames", "--full-index",
                  "--binary", "--minimal", "--patience", "--histogram", "--function-context",
                  "--no-ext-diff", "--no-textconv", "--no-prefix", "--merge-base",
                  "--compact-summary", "--irreversible-delete")
_GIT_DIFF_OPTIONAL = ("--stat", "--color", "--word-diff", "--color-words", "--find-renames",
                      "--find-copies", "--abbrev", "--relative", "--dirstat", "--unified")
_GIT_DIFF = _g("pusRawzbWD", "", "MCBU", long=_GIT_DIFF_LONG,
               long_value=("--diff-filter", "--inter-hunk-context", "--src-prefix", "--dst-prefix"),
               long_optional=_GIT_DIFF_OPTIONAL, refuse=_GIT_REFUSE_DIFF)
_GIT_LOG_LONG = _GIT_DIFF_LONG + (
    "--oneline", "--graph", "--no-decorate", "--all", "--reverse", "--first-parent", "--merges",
    "--no-merges", "--follow", "--abbrev-commit", "--no-abbrev-commit", "--date-order",
    "--topo-order", "--left-right", "--cherry-pick", "--boundary", "--source", "--use-mailmap",
    "--full-history", "--simplify-merges", "--ancestry-path", "--regexp-ignore-case",
    "--extended-regexp", "--fixed-strings", "--all-match", "--invert-grep", "--count")
_GIT_LOG = _g("pusRawzbWDgiEFmct", "nSG", "MCBU", numeric=True, long=_GIT_LOG_LONG,
              long_value=("--diff-filter", "--max-count", "--skip", "--since", "--after", "--until",
                          "--before", "--author", "--committer", "--grep", "--date",
                          "--inter-hunk-context"),
              long_optional=_GIT_DIFF_OPTIONAL + ("--decorate", "--pretty", "--branches", "--tags",
                                                  "--remotes", "--no-walk"),
              long_equals=("--format",),
              refuse=_GIT_REFUSE_DIFF)
_GIT_GRAMMARS = {
    "diff": _GIT_DIFF,
    "log": _GIT_LOG,
    "show": _GIT_LOG,
    "status": _g("sbvz", "", "u",
                 long=("--short", "--branch", "--long", "--verbose", "--ahead-behind",
                       "--no-ahead-behind", "--show-stash", "--renames", "--no-renames", "--no-column"),
                 long_optional=("--porcelain", "--untracked-files", "--ignored", "--ignore-submodules",
                                "--find-renames", "--column")),
    "grep": _g("nilvwcEFPGhHIqzLo", "efABCm", numeric=True,
               long=("--cached", "--untracked", "--exclude-standard", "--line-number", "--column",
                     "--ignore-case", "--invert-match", "--word-regexp", "--count",
                     "--files-with-matches", "--name-only", "--files-without-match",
                     "--extended-regexp", "--fixed-strings", "--perl-regexp", "--basic-regexp",
                     "--full-name", "--heading", "--break", "--show-function", "--function-context",
                     "--no-color", "--null", "--quiet", "--all-match", "--and", "--or", "--not",
                     "--text", "--only-matching"),
               long_value=("--threads", "--max-depth", "--context", "--after-context",
                           "--before-context", "--max-count"),
               long_optional=("--color",),
               refuse={"-O": "command_git_denied", "--open-files-in-pager": "command_git_denied"}),
    "blame": _g("lstnfecwpb", "L", "MC",
                long=("--porcelain", "--line-porcelain", "--show-name", "--show-number",
                      "--show-email", "--root", "--show-stats"),
                long_optional=("--abbrev",)),
    "ls-files": _g("cdmoiskuvftz", "x",
                   long=("--cached", "--deleted", "--modified", "--others", "--ignored", "--stage",
                         "--killed", "--unmerged", "--directory", "--no-empty-directory",
                         "--exclude-standard", "--full-name", "--error-unmatch", "--eol",
                         "--deduplicate"),
                   long_value=("--exclude", "--format"), long_optional=("--abbrev",)),
    "rev-parse": _g("q", long=("--show-toplevel", "--show-prefix", "--show-cdup", "--git-dir",
                               "--git-common-dir", "--is-inside-work-tree", "--is-inside-git-dir",
                               "--is-bare-repository", "--verify", "--quiet", "--symbolic",
                               "--symbolic-full-name", "--all", "--absolute-git-dir"),
                    long_optional=("--short", "--abbrev-ref")),
    "describe": _g(long=("--tags", "--all", "--always", "--long", "--exact-match", "--first-parent",
                         "--contains"),
                   long_value=("--match", "--exclude", "--candidates"),
                   long_optional=("--abbrev", "--dirty", "--broken")),
    "rev-list": _g("", "n", numeric=True,
                   long=("--count", "--all", "--reverse", "--first-parent", "--merges", "--no-merges",
                         "--topo-order", "--date-order", "--left-right", "--cherry-pick",
                         "--boundary"),
                   long_value=("--max-count", "--skip", "--since", "--until", "--after", "--before",
                               "--author", "--committer", "--grep")),
    "merge-base": _g("a", long=("--is-ancestor", "--all", "--octopus", "--independent",
                                "--fork-point")),
    "ls-tree": _g("rdtlz", long=("--name-only", "--name-status", "--full-name", "--full-tree", "--long",
                                 "--object-only"),
                  long_value=("--format",), long_optional=("--abbrev",)),
    "cat-file": _g("tspe"),
    "shortlog": _g("nsec", long=("--numbered", "--summary", "--email", "--committer")),
    "branch": _g("arvl", operands=False,
                 long=("--all", "--remotes", "--verbose", "--list", "--show-current", "--no-color",
                       "--no-column"),
                 long_value=("--sort", "--format"), long_optional=("--color", "--abbrev")),
    "version": _g(operands=False),
}

_FIND_PLAIN = frozenset({"-print", "-print0", "-empty", "-not", "-a", "-and", "-o", "-or", "-true",
                         "-false", "-prune", "-depth", "-xdev", "-mount", "-readable", "-writable",
                         "-executable", "-ls", "-quit", "-daystart", "(", ")", "!", ","})
_FIND_VALUE = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-type",
                         "-xtype", "-maxdepth", "-mindepth", "-newer", "-anewer", "-cnewer", "-size",
                         "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin", "-perm", "-regex",
                         "-iregex", "-regextype", "-samefile", "-links", "-printf", "-user",
                         "-group", "-uid", "-gid", "-inum", "-used", "-lname", "-ilname",
                         "-newermt"})
_SED_LONG = frozenset({"--quiet", "--silent", "--regexp-extended", "--separate", "--unbuffered",
                       "--null-data", "--zero-terminated", "--posix", "--sandbox", "--debug"})
# The shells the gate reads (POSIX parsing), and the only options they may be given: anything
# else (-k and -o keyword put NAME=value words into the environment, rc files run code, shopt
# options change parsing) is refused.
_READABLE_SHELLS = frozenset({"bash", "sh", "dash"})
_SHELL_LETTERS = frozenset("euxlc")
_SHELL_O_VALUES = frozenset({"errexit", "nounset", "pipefail", "xtrace"})
_SHELL_LONG = frozenset({"--norc", "--noprofile"})
_ARG_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


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
            if not _ASCII_DIGITS.match(target):
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
                if not _ASCII_DIGITS.match(raw):
                    raise self.construct("a descriptor number in non-ASCII digits")
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


@dataclass
class _Reading:
    """What the gate read from one simple command's arguments under its program's grammar."""

    known: bool                                        # the program has a grammar
    unknown: list[str]                                 # options or forms not in the grammar
    writes: list[tuple[str, str]]                      # (path, kind) the command writes
    trees: list[str]                                   # destinations that may receive a folder
    copies_tree: bool = False                          # a recursive copy (contents unseen)
    recursive_delete: bool = False
    exempt: frozenset = frozenset()                    # argument indices checked by the grammar
    clean: set = None                                  # argv indices of completely read clusters
    values: list = None                                # every option value the grammar read


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
        values = _python_x_values(args) if _PYTHON.match(name) else frozenset()
        for index, word in enumerate(args):
            if _ASSIGNMENT.match(word) and index not in values:
                # NAME=value after a command name: an environment setting under bash -k, make's
                # variables, and the like.
                raise _Refused("command_assignment")
        parts = self.redirections(redirects)
        if name in _SHELLS:
            if name not in _READABLE_SHELLS:
                try:
                    mode = self.shell_mode(args)[0]
                except _Refused:
                    mode = None
                raise _Refused("command_interactive" if mode == "interactive"
                               else "command_shell_unsupported", name)
            mode, script = self.shell_mode(args)
            if mode == "interactive":
                raise _Refused("command_interactive", name)
            if mode == "inline":
                self.argument(argv[0], program=True)
                nested = self.nested(script, depth)
                self.policy_state()
                return parts + nested + [self.rule(argv, True)]
        if name == "eval":
            if not args:
                raise _Refused("command_malformed", "eval without a command")
            nested = self.nested(" ".join(args), depth)
            self.policy_state()
            return parts + nested + [self.rule(argv, True)]
        module = None
        if _INTERPRETER.match(name):
            mode, module = self.interpreter_mode(name, args)
            if mode == "interactive":
                raise _Refused("command_interactive", name)
            if mode == "inline":
                raise _Refused("command_inline_code", name)
            if module is not None:
                root = module.partition("\0")[0].split(".")[0]
                if root in _MODULES_INTERACTIVE:
                    raise _Refused("command_interactive", f"{name} -m {root}")
                if root in _MODULES_INLINE:
                    raise _Refused("command_inline_code", f"{name} -m {root}")
        if name in _INLINE_ALWAYS:
            raise _Refused("command_inline_code", name)
        if name in _NETWORK:
            raise _Refused("command_network", name)
        if _RUNNERS.get(name) is not None and args[:1] == [_RUNNERS[name]]:
            return parts + self.runner(argv, depth)
        if self.installs(name, args, module):
            raise _Refused("command_package_install", " ".join(argv[:3]))
        if name == "ln":
            raise _Refused("command_symlink")
        if name in _MODE_CHANGE:
            raise _Refused("command_mode_change")
        reading = self.read(name, args)
        clean = reading.clean or set()
        for index, word in enumerate(argv):
            if index - 1 not in reading.exempt:
                self.argument(word, program=index == 0, tails=index not in clean)
        for value in reading.values or ():
            self.argument(value, tails=False)
        self.policy_state()
        parts.extend(self.targets(reading))
        for word in reading.unknown:
            parts.append(self.checker.unlisted("unlisted_command", "command_unknown_option",
                                               f"{name} {word}"))
        parts.append(self.rule(argv, reading.known))
        return parts

    def policy_state(self) -> None:
        if self.policy.owner_policy_state == "missing":
            raise _Refused("owner_policy_missing")
        if self.policy.owner_policy_state == "changed":
            raise _Refused("owner_policy_changed")

    def rule(self, argv: list[str], known: bool) -> _Part:
        """Allowed by the command rules: a prefix rule for a program whose arguments the gate
        read completely; an exact rule in every layer for any other program."""

        words = tuple(argv)
        shown = shlex.join(argv)
        prefixed = any(words[:len(rule)] == rule for rule in self.policy.command_rules)
        if known and prefixed:
            return _Part(ALLOW, "command_allowed", posixpath.basename(argv[0]))
        if not known and all(words in layer for layer in self.policy.command_layers):
            return _Part(ALLOW, "command_allowed", posixpath.basename(argv[0]))
        if prefixed:
            return self.checker.unlisted("unlisted_command", "command_unknown_program", shown)
        return self.checker.unlisted("unlisted_command", "command_not_allowed", shown)

    def runner(self, argv: list[str], depth: int) -> list[_Part]:
        """`uv run CMD...` and the like: CMD is checked as a command of its own, and the runner
        itself must be listed."""

        for index, word in enumerate(argv[:2]):
            self.argument(word, program=index == 0)
        inner = argv[2:]
        if not inner:
            raise _Refused("command_malformed", "a runner without a command")
        if inner[0].startswith("-"):
            # The runner's own options (environments, extra packages): not read.
            for word in inner:
                self.argument(word)
            self.policy_state()
            return [self.checker.unlisted("unlisted_command", "command_unknown_option",
                                          f"{' '.join(argv[:2])} {inner[0]}"),
                    self.rule(argv, True)]
        if depth + 1 > MAX_NESTING:
            raise _Refused("command_nesting_too_deep")
        parts = self.simple(inner, [], depth + 1)
        parts.append(self.rule(argv, True))
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
                if arg not in _SHELL_LONG:
                    raise _Refused("command_shell_option", arg)
                i += 1
                continue
            if arg[0] in "-+":
                letters = arg[1:]
                if "i" in letters or "s" in letters:
                    return "interactive", None
                for index, letter in enumerate(letters):
                    if letter == "o" and index == len(letters) - 1:
                        if i + 1 >= len(args) or args[i + 1] not in _SHELL_O_VALUES:
                            value = args[i + 1] if i + 1 < len(args) else ""
                            raise _Refused("command_shell_option", f"{arg} {value}".strip())
                        i += 1
                    elif letter not in _SHELL_LETTERS or (letter == "c" and arg[0] == "+"):
                        raise _Refused("command_shell_option", arg)
                if "c" in letters:
                    inline = True
                i += 1
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
        return "script", None

    @classmethod
    def installs(cls, name: str, args: list[str], module: str | None) -> bool:
        if module is not None:
            module_name, _, rest = module.partition("\0")
            root = module_name.split(".")[0]
            if root == "ensurepip":
                return True
            if _PIP.match(root) or root in _PACKAGE_SUBCOMMANDS or root in _PACKAGE_ALWAYS:
                return cls.installs(root, rest.split(), None)
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

    # reading a program's arguments ------------------------------------------------------------

    def read(self, name: str, args: list[str]) -> _Reading:
        """The program's arguments under its grammar: refusals raise; unknown forms are listed."""

        clean: set[int] = set()
        if name == "git":
            values: list[str] = []
            return _Reading(True, self.git(args, clean, values), [], [], clean=clean, values=values)
        if name == "find":
            return _Reading(True, self.find(args), [], [])
        if name == "sed":
            return self.sed(args)
        if _PYTHON.match(name):
            return self.python(args, clean)
        if name.startswith("perl") and _INTERPRETER.match(name):
            options, _, unknown = _parse(_PERL, args, clean, 1)
            return _Reading(True, unknown, [], [], clean=clean,
                            values=[v for _, v in options if v is not None])
        if name in _SHELLS:
            return _Reading(True, [], [], [])
        grammar = _GRAMMARS.get(name)
        if grammar is None:
            return _Reading(False, [], [], [])
        options, operands, unknown = _parse(grammar, args, clean, 1)
        if not grammar.operands and operands:
            unknown = unknown + operands
        reading = _Reading(True, unknown, [], [], clean=clean,
                           values=[v for _, v in options if v is not None])
        if grammar is _NODE:
            for option, value in options:
                if option in _NODE_LOADERS:
                    if value is not None and _URL_SCHEME.match(value):
                        raise _Refused("command_inline_code", f"{name} {option}")
                    reading.unknown.append(f"{option} {value}")
        elif grammar is _PYTEST:
            self.argument_files(name, args)
        elif name in ("touch", "mkdir"):
            reading.writes = [(p, "replace") for p in operands]
        elif name == "tee":
            kind = "modify" if _has(options, "-a", "--append") else "replace"
            reading.writes = [(p, kind) for p in operands]
        elif name in ("rm", "rmdir"):
            reading.writes = [(p, "delete") for p in operands]
            reading.recursive_delete = name == "rm" and _has(options, "-r", "-R", "--recursive")
        elif name == "sort":
            reading.writes = [(p, "replace") for p in _values(options, "-o", "--output")]
        elif name in ("cp", "mv"):
            # cp takes abbreviations of --preserve and of its attributes (l, li, al): any value
            # that can mean links or all keeps links as links.
            preserved = _values(options, "--preserve") + [
                word.partition("=")[2] for word in reading.unknown
                if len(word.partition("=")[0]) > 2 and "--preserve".startswith(word.partition("=")[0])]
            for value in preserved:
                if any(item and ("links".startswith(item) or "all".startswith(item))
                       for item in value.split(",")):
                    raise _Refused("command_symlink")
            self.copy(name, options, operands, reading)
        return reading

    @staticmethod
    def copy(name: str, options, operands: list[str], reading: _Reading) -> None:
        directories = _values(options, "-t", "--target-directory")
        if directories:
            sources, destination, into = operands, directories[-1], True
        elif operands[1:]:
            sources, destination = operands[:-1], operands[-1]
            into = not _has(options, "-T", "--no-target-directory")
        else:
            return
        landed = [destination]
        if into:
            for source in sources:
                base = posixpath.basename(source.rstrip("/"))
                if base not in ("", ".", ".."):
                    landed.append(destination.rstrip("/") + "/" + base)
        if name == "mv":
            # A move may carry a folder or a link (a relative link points elsewhere from a new
            # depth): it asks as a tree, whatever the deletion setting.
            reading.writes = [(s, "delete") for s in sources] + [(p, "replace") for p in landed]
            reading.trees = landed
            reading.copies_tree = True
        else:
            reading.writes = [(p, "replace") for p in landed]
            if _has(options, "-a", "-r", "-R", "--recursive", "--archive"):
                reading.copies_tree = True
                reading.trees = landed

    def git(self, args: list[str], clean: set[int], values: list[str]) -> list[str]:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] not in _GIT_GLOBAL_ALLOWED:
                raise _Refused("command_git_denied")
            i += 1
        if i >= len(args):
            return []
        sub, rest = args[i], args[i + 1:]
        if sub in _GIT_DENIED:
            raise _Refused("command_git_denied")
        grammar = _GIT_GRAMMARS.get(sub)
        if grammar is None:
            return [sub]
        options, operands, unknown = _parse(grammar, rest, clean, i + 2)
        values.extend(v for _, v in options if v is not None)
        if not grammar.operands and operands:
            unknown = unknown + operands
        return unknown

    @staticmethod
    def find(args: list[str]) -> list[str]:
        if any(a in _FIND_EXEC for a in args):
            raise _Refused("command_exec_option", "find")
        unknown: list[str] = []
        i = 0
        while i < len(args) and args[i] == "-P":
            i += 1
        while i < len(args) and not args[i].startswith("-") and args[i] not in _FIND_PLAIN:
            i += 1
        while i < len(args):
            word = args[i]
            if word in _FIND_PLAIN:
                i += 1
            elif word in _FIND_VALUE and i + 1 < len(args):
                i += 2
            else:
                unknown.append(word)
                i += 1
        return unknown

    @staticmethod
    def argument_files(name: str, args: list[str]) -> None:
        """pytest expands any argument starting with '@' into the arguments in that file."""

        for word in args:
            if word.startswith("@"):
                raise _Refused("command_argument_file", f"{name} {word}")

    def python(self, args: list[str], clean: set[int]) -> _Reading:
        options, operands, unknown = _parse(_PYTHON_GRAMMAR, args, clean, 1)
        values = [v for _, v in options if v is not None]
        reading = _Reading(True, unknown, [], [], clean=clean, values=values)
        for x_option in _values(options, "-X"):
            x_name = x_option.split("=", 1)[0]
            if x_name in _PYTHON_X_WRITES:
                raise _Refused("command_write_option", f"-X {x_name}")
            if x_name not in _PYTHON_X:
                reading.unknown.append(f"-X {x_option}")
        modules = _values(options, "-m")
        if not modules:
            return reading
        module = modules[-1]
        grammar = {"pytest": _PYTEST, "unittest": _UNITTEST}.get(module)
        if grammar is None:
            reading.unknown.append(f"-m {module}")
            return reading
        if grammar is _PYTEST:
            self.argument_files("python -m pytest", operands)
        more_options, _, more = _parse(grammar, operands, clean, 1 + len(args) - len(operands))
        values.extend(v for _, v in more_options if v is not None)
        reading.unknown.extend(more)
        return reading

    def sed(self, args: list[str]) -> _Reading:
        """GNU sed: options anywhere; with -e or -f every operand is a file, else the first
        operand is the script. Scripts are checked by their own small grammar."""

        scripts: list[str] = []
        exempt: set[int] = set()
        operands: list[tuple[int, str]] = []
        unknown: list[str] = []
        in_place = file_script = False
        i, n = 0, len(args)
        while i < n:
            arg = args[i]
            if arg == "--":
                operands.extend((j, args[j]) for j in range(i + 1, n))
                break
            if arg.startswith("--"):
                name, eq, value = arg.partition("=")
                if name in ("--expression", "--file"):
                    file_script = file_script or name == "--file"
                    if eq:
                        scripts.append(value)
                        exempt.add(i)
                    elif i + 1 < n:
                        scripts.append(args[i + 1])
                        exempt.add(i + 1)
                        i += 1
                elif len(name) > 2 and ("--expression".startswith(name) or "--file".startswith(name)):
                    raise _Refused("command_inline_code", "sed")
                elif len(name) > 3 and "--in-place".startswith(name):
                    in_place = True
                    if eq or name != "--in-place":
                        unknown.append(arg)
                elif name == "--line-length" and eq:
                    pass
                elif name not in _SED_LONG or eq:
                    unknown.append(arg)
                i += 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                letters = arg[1:]
                for index, letter in enumerate(letters):
                    attached = letters[index + 1:]
                    if letter == "i":
                        in_place = True
                        if attached:
                            unknown.append(arg)
                        break
                    if letter in "ef":
                        file_script = file_script or letter == "f"
                        if attached:
                            scripts.append(attached)
                            exempt.add(i)
                        elif i + 1 < n:
                            scripts.append(args[i + 1])
                            exempt.add(i + 1)
                            i += 1
                        break
                    if letter == "l":
                        if not attached:
                            i += 1
                        break
                    if letter not in "nErsuz":
                        unknown.append(arg)
                        break
                i += 1
                continue
            operands.append((i, arg))
            i += 1
        if not scripts and not file_script and operands:
            index, script = operands.pop(0)
            scripts.append(script)
            exempt.add(index)
        if file_script or not scripts or not all(_sed_safe(x) for x in scripts):
            raise _Refused("command_inline_code", "sed")
        files = [arg for _, arg in operands]
        writes = [(f, "modify") for f in files] if in_place else []
        return _Reading(True, unknown, writes, [], exempt=frozenset(exempt))

    # arguments and targets ----------------------------------------------------------------

    def argument(self, arg: str, program: bool = False, tails: bool = True) -> None:
        """Every argument, whatever the program: no '..'; no part naming git internals, agent
        configuration, secrets files or COCOLO's own names; no standard input; absolute paths
        only inside the workspace. For a short-option cluster the gate did not read completely,
        every tail of the word is checked too, since any of them could be an attached value."""

        words = [arg]
        if tails and arg.startswith("-") and not arg.startswith("--"):
            words.extend(arg[k:] for k in range(2, len(arg)))
        pieces = [piece for word in words for piece in re.split(r"[=:,]", word)]
        for piece in pieces:
            if ".." in piece.split("/"):
                raise _Refused("path_traversal", arg)
        for piece in pieces:
            denied = _deny_class(tuple(p for p in piece.split("/") if p) or ("",))
            if denied:
                raise _Refused(denied, arg)
        if program:
            return
        for piece in pieces:
            if piece == "/dev/stdin":
                raise _Refused("command_interactive", arg)
            if piece.startswith("/") and piece not in _ARG_DEVICES:
                self.checker.resolve(piece, self.base)

    def targets(self, reading: _Reading) -> list[_Part]:
        """The paths the command writes, each checked as a write."""

        parts = []
        recursive = reading.recursive_delete
        for raw, kind in reading.writes:
            if raw in _DEVICES:
                continue
            rel = self.checker.resolve(raw, self.base)
            if recursive and kind == "delete" and not rel:
                raise _Refused("command_delete_outside_scope", raw)
            try:
                part = self.checker.write(rel, kind, raw)
            except _Refused as refused:
                if recursive and kind == "delete" and refused.rule == "write_outside_scope":
                    raise _Refused("command_delete_outside_scope", raw) from None
                raise
            if recursive and kind == "delete" and part.rule == "write_outside_scope":
                raise _Refused("command_delete_outside_scope", raw)
            parts.append(part)
        for raw in reading.trees:
            rel = self.checker.resolve(raw, self.base)
            if rel and _holds_conditional(rel):
                raise _Refused("write_conditional_ungranted", raw)
        if reading.copies_tree and reading.trees:
            parts.append(self.checker.unlisted("unlisted_write", "write_tree", reading.trees[0]))
        return parts


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

# The fields each tool may carry (Claude Code 2.1 and Codex 0.169); any other field is refused,
# since what it would change is not known.
_FIELDS = {
    "Write": {"file_path", "content"},
    "Edit": {"file_path", "old_string", "new_string", "replace_all"},
    "MultiEdit": {"file_path", "edits"},
    "NotebookEdit": {"notebook_path", "new_source", "cell_id", "cell_type", "edit_mode"},
    "Read": {"file_path", "offset", "limit", "pages"},
    "LS": {"path", "ignore"},
    "NotebookRead": {"notebook_path", "cell_id"},
    "view_image": {"path"},
    "Glob": {"pattern", "path"},
    "Grep": {"pattern", "path", "glob", "type", "output_mode", "-i", "-n", "-A", "-B", "-C",
             "context", "multiline", "head_limit", "offset"},
    "Bash": {"command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox",
             "workdir"},
    "exec_command": {"cmd", "workdir", "shell", "login", "tty", "yield_time_ms", "max_output_tokens",
                     "sandbox_permissions", "justification", "prefix_rule", "additional_permissions",
                     "environment_id"},
    "shell": {"command", "workdir", "timeout_ms", "timeout", "sandbox_permissions", "justification",
              "prefix_rule", "additional_permissions"},
    "local_shell": {"command", "workdir", "timeout_ms", "env", "user"},
    "apply_patch": {"command", "input", "workdir", "call_id"},
}
_UNSET_FIELDS = ("prefix_rule", "additional_permissions", "environment_id", "user")
_SHELL_PATHS = frozenset({"bash", "sh", "dash", "/bin/bash", "/bin/sh", "/bin/dash", "/usr/bin/bash",
                          "/usr/bin/sh", "/usr/bin/dash"})
MAX_PATTERN_ALTERNATIVES = 256


def _check_fields(tool: str, tool_input: Mapping) -> None:
    allowed = _FIELDS.get(tool, set())
    for key in tool_input:
        if key not in allowed:
            raise _Refused("request_field_unknown", key)
    for key in _UNSET_FIELDS:
        if tool_input.get(key) is not None:
            raise _Refused("request_field_value", key)
    if tool_input.get("sandbox_permissions") not in (None, "use_default"):
        raise _Refused("request_field_value", "sandbox_permissions")
    if tool_input.get("dangerouslyDisableSandbox"):
        raise _Refused("request_field_value", "dangerouslyDisableSandbox")
    if tool_input.get("env"):
        raise _Refused("request_field_value", "env")
    if tool_input.get("login") not in (None, True, False):
        raise _Refused("request_field_value", "login")
    shell = tool_input.get("shell")
    if shell is not None and shell not in _SHELL_PATHS:
        raise _Refused("request_field_value", "shell")
    tty = tool_input.get("tty")
    if tty is not None and tty is not False:
        raise _Refused("command_interactive", "a terminal (tty)")


def _alternatives(pattern: object) -> list[str]:
    """A glob pattern's brace alternatives, each a pattern without braces."""

    if not isinstance(pattern, str) or not pattern:
        raise _Refused("request_malformed")
    if "\\" in pattern:
        raise _Refused("pattern_unreadable", pattern)

    def sequence(i: int, stops: str) -> tuple[list[str], int]:
        outs = [""]
        while i < len(pattern):
            c = pattern[i]
            if c in stops:
                return outs, i
            if c == "}":
                raise _Refused("pattern_unreadable", pattern)
            if c == "{":
                alternatives: list[str] = []
                i += 1
                while True:
                    sub, i = sequence(i, ",}")
                    alternatives.extend(sub)
                    if i >= len(pattern):
                        raise _Refused("pattern_unreadable", pattern)
                    i += 1
                    if pattern[i - 1] == "}":
                        break
                outs = [o + a for o in outs for a in alternatives]
                if len(outs) > MAX_PATTERN_ALTERNATIVES:
                    raise _Refused("pattern_unreadable", pattern)
                continue
            outs = [o + c for o in outs]
            i += 1
        return outs, i

    outs, _ = sequence(0, "")
    return outs


def _read_pattern(checker: _Checker, pattern: object, base: tuple[str, ...] | None) -> None:
    for alternative in _alternatives(pattern):
        checker.read_path(alternative, base)


def _base(checker: _Checker, workdir: object) -> tuple[str, ...]:
    if workdir is None:
        return checker.cwd
    try:
        return checker.resolve(workdir)
    except _Refused:
        raise _Refused("cwd_outside_workspace", workdir) from None


def _patch_text(tool_input: Mapping) -> object:
    if "command" in tool_input and "input" in tool_input:
        raise _Refused("request_ambiguous", "the patch")
    text = tool_input.get("command", tool_input.get("input"))
    if isinstance(text, list) and len(text) == 2 and text[0] == "apply_patch":
        text = text[1]
    return text


def _decide(policy: GatePolicy, request: object) -> tuple[list[_Part], bool, str | None]:
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
    if tool not in _FIELDS:
        raise _Refused("tool_unknown", tool)
    _check_fields(tool, tool_input)
    if tool in _WRITE_TOOLS:
        field, kind = _WRITE_TOOLS[tool]
        return [checker.write_path(tool_input.get(field), kind)], True, None
    if tool in _READ_TOOLS:
        checker.read_path(tool_input.get(_READ_TOOLS[tool]))
        return [_Part(ALLOW, "read_in_workspace", "")], policy.record_reads, None
    if tool == "Glob":
        base = checker.read_path(tool_input["path"]) if tool_input.get("path") is not None else None
        _read_pattern(checker, tool_input.get("pattern"), base)
        return [_Part(ALLOW, "read_in_workspace", "")], policy.record_reads, None
    if tool == "Grep":
        base = checker.read_path(tool_input["path"]) if tool_input.get("path") is not None else None
        if tool_input.get("glob") is not None:
            _read_pattern(checker, tool_input["glob"], base if base is not None else checker.cwd)
        return [_Part(ALLOW, "read_in_workspace", "")], policy.record_reads, None
    if tool in _PATCH_TOOLS:
        text = _patch_text(tool_input)
        base = _base(checker, tool_input.get("workdir"))
        parts = [checker.write_path(path, kind, base) for path, kind in _patch_ops(text)]
        return parts, True, None
    # A command tool.
    if tool_input.get("run_in_background"):
        raise _Refused("command_background")
    commands = _Commands(checker, _base(checker, tool_input.get("workdir")))
    command = tool_input.get("cmd" if tool == "exec_command" else "command")
    if isinstance(command, str):
        return commands.shell(command, 0), True, command
    parts = commands.argv(command)
    return parts, True, shlex.join(command)


def _order(part: _Part) -> int:
    return (_ESCALATION_ORDER.index(part.rule) if part.rule in _ESCALATION_ORDER
            else len(_ESCALATION_ORDER))


def decide(policy: GatePolicy, request: object) -> Decision:
    """Allow, refuse or escalate one PreToolUse request. Pure: no I/O, no clock, no state.

    An escalation names every reason found in the request (one per part of a compound command
    or patch), and a command's escalation ends with the full command, so the owner answers for
    all of it at once."""

    try:
        parts, record, full = _decide(policy, request)
    except _Refused as refused:
        return Decision(REFUSE, refused.rule, _reason(refused.rule, REFUSE, refused.subject), True)
    escalations = sorted((p for p in parts if p.outcome == ESCALATE), key=_order)
    if escalations:
        texts: list[str] = []
        for part in escalations:
            text = _REASONS[part.rule].format(s=_show(part.subject))
            if text not in texts:
                texts.append(text)
        reason = _PREFIX + "asking the owner: " + "; ".join(texts)
        if full is not None:
            reason += "; the full command: " + _show(full, limit=None)
        return Decision(ESCALATE, escalations[0].rule, reason, True)
    if full is not None:
        return Decision(ALLOW, "command_allowed", _reason("command_allowed", ALLOW), record)
    part = parts[0]
    return Decision(part.outcome, part.rule, _reason(part.rule, part.outcome, part.subject), record)


# --------------------------------------------------------------------------------------------
# The record row
# --------------------------------------------------------------------------------------------

def _subject(request: object) -> str:
    if not isinstance(request, Mapping) or not isinstance(request.get("tool_input"), Mapping):
        return ""
    tool, tool_input = request.get("tool_name"), request["tool_input"]
    if tool in _PATCH_TOOLS:
        try:
            text = _patch_text(tool_input)
        except _Refused:
            return "\n".join(t for t in (tool_input.get("command"), tool_input.get("input"))
                             if isinstance(t, str))
        try:
            return "\n".join(path for path, _ in _patch_ops(text))
        except _Refused:
            return text if isinstance(text, str) else ""
    if tool in _COMMAND_TOOLS:
        command = tool_input.get("cmd" if tool == "exec_command" else "command")
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
