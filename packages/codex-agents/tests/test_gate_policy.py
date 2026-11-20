"""M3 slice 3: the check-in gate's policy, as a pure function (M3-30 decisions, M3-2 at the gate).

`gate_policy.decide(policy, request)` answers one hook request (Claude's or Codex's PreToolUse
input) with allow, refuse or escalate, the rule that decided and the plain reason the child sees.
`gate_policy.build_policy(grant, owner_policy)` makes the run's policy from the requirement's grant
intersected with the owner's per-project policy; it never widens either.

Case IDs: K01 in-scope allowed; K02 out-of-scope refused or escalated; K03 each deny class;
K04 compound commands and shell constructs; K05 interpreters and shells; K06 the record row;
A01 no owner policy refused (at the gate); A03 a grant-only path the task did not request refused;
A04 an owner policy changed after routing never widens. Every table row is one subTest named by
its case, so a failure names the row.
"""
from __future__ import annotations

import hashlib
import unittest

import tests.support  # noqa: F401

from codex_agents import gate_policy as gp

ALLOW, REFUSE, ESCALATE = "allow", "refuse", "escalate"
SEVERITY = {ALLOW: 0, ESCALATE: 1, REFUSE: 2}

GRANT = {
    "write_paths": [
        "src/",
        "tests/",
        "docs/*.md",
        ".github/workflows/ci.yml",
        "package-lock.json",
        "requirements.txt",
        "src/requirements-dev.txt",
    ],
    "conditional_paths": [".github/workflows/ci.yml"],
    "commands": [
        ["pytest"],
        ["python3", "-m", "pytest"],
        ["python3"],
        ["bash"],
        ["git", "diff"],
        ["git", "status"],
        ["git", "log"],
        ["git", "commit"],
        ["ls"],
        ["cat"],
        ["grep"],
        ["rm"],
        ["cp"],
        ["mv"],
        ["touch"],
        ["mkdir"],
        ["tee"],
        ["sed"],
        ["find"],
        ["curl"],
        ["pip"],
        ["eval"],
    ],
}
OWNER = {
    "write_paths": ["**"],
    "conditional_paths": [".github/workflows/ci.yml", ".gitattributes"],
    "commands": [
        ["pytest"],
        ["python3"],
        ["bash"],
        ["git"],
        ["ls"],
        ["cat"],
        ["grep"],
        ["rm"],
        ["cp"],
        ["mv"],
        ["touch"],
        ["mkdir"],
        ["tee"],
        ["sed"],
        ["find"],
        ["curl"],
        ["pip"],
        ["eval"],
    ],
    "written_by": "owner",
    "written_at": "2026-11-21T00:00:00Z",
}


def policy(grant=GRANT, owner=OWNER, **kwargs):
    return gp.build_policy(grant, owner, **kwargs)


def request(tool, cwd="/workspace", tool_use_id="toolu_01", **tool_input):
    return {
        "session_id": "session-1",
        "tool_use_id": tool_use_id,
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


def write(path, cwd="/workspace"):
    return request("Write", cwd=cwd, file_path=path, content="x\n")


def edit(path, cwd="/workspace"):
    return request("Edit", cwd=cwd, file_path=path, old_string="a", new_string="b")


def read(path, cwd="/workspace"):
    return request("Read", cwd=cwd, file_path=path)


def bash(command, cwd="/workspace", **extra):
    return request("Bash", cwd=cwd, command=command, **extra)


def patch(*ops, begin="*** Begin Patch", end="*** End Patch"):
    lines = [begin] if begin is not None else []
    for op in ops:
        lines.extend(op.split("\n"))
    if end is not None:
        lines.append(end)
    text = "\n".join(lines) + "\n"
    return request("apply_patch", call_id="call_1", command=text)


def add(path, body="+x"):
    return f"*** Add File: {path}\n{body}"


def update(path, move_to=None):
    lines = [f"*** Update File: {path}"]
    if move_to is not None:
        lines.append(f"*** Move to: {move_to}")
    lines.extend(["@@", "-a", "+b"])
    return "\n".join(lines)


def delete(path):
    return f"*** Delete File: {path}"


class TableMixin:
    def check_rows(self, rows, pol=None):
        pol = pol if pol is not None else policy()
        for case, req, outcome, rule in rows:
            with self.subTest(case=case):
                decision = gp.decide(pol, req)
                self.assertEqual((outcome, rule), (decision.outcome, decision.rule),
                                 f"{case}: reason was {decision.reason!r}")
                self.assertIsInstance(decision.reason, str)
                self.assertTrue(decision.reason.strip())
                self.assertNotIn("\n", decision.reason)


class K01InScopeAllowed(TableMixin, unittest.TestCase):
    def test_writes_reads_and_commands_inside_scope_are_allowed(self):
        self.check_rows([
            ("write absolute in a granted directory", write("/workspace/src/a.py"), ALLOW, "write_in_scope"),
            ("write nested in a granted directory", write("/workspace/src/pkg/b.py"), ALLOW, "write_in_scope"),
            ("write relative to the workspace", write("src/a.py"), ALLOW, "write_in_scope"),
            ("write relative to a sub cwd", write("a.py", cwd="/workspace/src"), ALLOW, "write_in_scope"),
            ("write with dot and double slash", write("/workspace/./src//a.py"), ALLOW, "write_in_scope"),
            ("write by single-star glob", write("/workspace/docs/guide.md"), ALLOW, "write_in_scope"),
            ("new test file is a create", write("/workspace/tests/test_new.py"), ALLOW, "write_in_scope"),
            ("edit a source file", edit("/workspace/src/a.py"), ALLOW, "write_in_scope"),
            ("multi edit", request("MultiEdit", file_path="/workspace/src/a.py", edits=[]), ALLOW, "write_in_scope"),
            ("notebook edit", request("NotebookEdit", notebook_path="/workspace/src/n.ipynb", new_source="x"),
             ALLOW, "write_in_scope"),
            ("src/.gitignore is not git", write("/workspace/src/.gitignore"), ALLOW, "write_in_scope"),
            ("env-like but not .env", write("/workspace/src/environment.py"), ALLOW, "write_in_scope"),
            ("granted grant-only path", write("/workspace/.github/workflows/ci.yml"), ALLOW, "write_in_scope"),
            ("non-ascii name", write("/workspace/src/café.py"), ALLOW, "write_in_scope"),
            ("read inside the workspace", read("/workspace/src/a.py"), ALLOW, "read_in_workspace"),
            ("read git internals inside the workspace", read("/workspace/.git/config"), ALLOW,
             "read_in_workspace"),
            ("read the workspace root", request("LS", path="/workspace"), ALLOW, "read_in_workspace"),
            ("glob relative", request("Glob", pattern="**/*.py"), ALLOW, "read_in_workspace"),
            ("glob with a path", request("Glob", pattern="*.py", path="/workspace/src"), ALLOW,
             "read_in_workspace"),
            ("grep pattern is a regex, not a path", request("Grep", pattern="../../etc"), ALLOW,
             "read_in_workspace"),
            ("grep with path and glob", request("Grep", pattern="x", path="src", glob="*.py"), ALLOW,
             "read_in_workspace"),
            ("codex view_image", request("view_image", path="/workspace/src/a.png"), ALLOW,
             "read_in_workspace"),
            ("pytest", bash("pytest"), ALLOW, "command_allowed"),
            ("pytest with args", bash("pytest -q tests/test_a.py"), ALLOW, "command_allowed"),
            ("python -m pytest", bash("python3 -m pytest -q"), ALLOW, "command_allowed"),
            ("python script", bash("python3 tools/run.py"), ALLOW, "command_allowed"),
            ("git diff with revision", bash("git diff --stat HEAD~1"), ALLOW, "command_allowed"),
            ("git diff range is not traversal", bash("git diff HEAD..main"), ALLOW, "command_allowed"),
            ("git log symmetric range", bash("git log --oneline a...b"), ALLOW, "command_allowed"),
            ("git status", bash("git status --short"), ALLOW, "command_allowed"),
            ("ls", bash("ls -la src"), ALLOW, "command_allowed"),
            ("ls the workspace by absolute path", bash("ls /workspace"), ALLOW, "command_allowed"),
            ("cat quoted absolute path", bash("cat '/workspace/src/a b.py'"), ALLOW, "command_allowed"),
            ("cat git internals is a read", bash("cat .git/config"), ALLOW, "command_allowed"),
            ("quoted semicolon is a word", bash("ls 'a;b'"), ALLOW, "command_allowed"),
            ("escaped semicolon is a word", bash("ls a\\;b"), ALLOW, "command_allowed"),
            ("escaped dollar is a word", bash("ls \\$HOME"), ALLOW, "command_allowed"),
            ("single-quoted pipe and dollar", bash("grep -n 'foo|$bar' src/a.py"), ALLOW, "command_allowed"),
            ("double-quoted plain text", bash('grep -n "a b" src/a.py'), ALLOW, "command_allowed"),
            ("tilde inside a word", bash("git log HEAD~3"), ALLOW, "command_allowed"),
            ("copy inside scope", bash("cp src/a.py src/b.py"), ALLOW, "command_allowed"),
            ("touch inside scope", bash("touch src/new.py"), ALLOW, "command_allowed"),
            ("sed in place inside scope", bash("sed -i -e s/a/b/ src/a.py"), ALLOW, "command_allowed"),
            ("codex command as argv", request("Bash", command=["ls", "-la"]), ALLOW, "command_allowed"),
            ("codex command as shell argv", request("Bash", command=["bash", "-lc", "git status"]), ALLOW,
             "command_allowed"),
            ("codex exec_command with cmd", request("exec_command", cmd="git status"), ALLOW, "command_allowed"),
            ("codex workdir inside the workspace", request("Bash", command="ls", workdir="/workspace/src"),
             ALLOW, "command_allowed"),
            ("codex patch add in scope", patch(add("src/new.py")), ALLOW, "write_in_scope"),
            ("codex patch update in scope", patch(update("src/a.py")), ALLOW, "write_in_scope"),
            ("codex patch absolute path in scope", patch(add("/workspace/src/x.py")), ALLOW, "write_in_scope"),
            ("codex patch content that looks like a header", patch(add("src/x.py", "+*** Add File: .git/x")),
             ALLOW, "write_in_scope"),
            ("codex patch two files in scope", patch(add("src/x.py"), update("src/a.py")), ALLOW,
             "write_in_scope"),
            ("codex patch input key", request("apply_patch", input="*** Begin Patch\n*** Add File: src/q.py\n"
                                              "+x\n*** End Patch\n"), ALLOW, "write_in_scope"),
        ])


class K02OutOfScope(TableMixin, unittest.TestCase):
    def test_outside_the_workspace_is_refused(self):
        self.check_rows([
            ("system file", write("/etc/passwd"), REFUSE, "path_outside_workspace"),
            ("tmp", write("/tmp/x"), REFUSE, "path_outside_workspace"),
            ("home", write("/root/.ssh/authorized_keys"), REFUSE, "path_outside_workspace"),
            ("prefix trick", write("/workspacex/a.py"), REFUSE, "path_outside_workspace"),
            ("sibling prefix", write("/workspace2/src/a.py"), REFUSE, "path_outside_workspace"),
            ("case variant of the root", write("/Workspace/src/a.py"), REFUSE, "path_outside_workspace"),
            ("the root itself", write("/workspace"), REFUSE, "path_outside_workspace"),
            ("the root with slash", write("/workspace/"), REFUSE, "path_outside_workspace"),
            ("proc cwd link", write("/proc/self/cwd/src/a.py"), REFUSE, "path_outside_workspace"),
            ("proc root link", write("/proc/self/root/workspace/src/a.py"), REFUSE, "path_outside_workspace"),
            ("fd link", write("/dev/fd/3"), REFUSE, "path_outside_workspace"),
            ("read environ", read("/proc/self/environ"), REFUSE, "path_outside_workspace"),
            ("read outside", read("/etc/hosts"), REFUSE, "path_outside_workspace"),
            ("glob absolute outside", request("Glob", pattern="/etc/*"), REFUSE, "path_outside_workspace"),
            ("glob path outside", request("Glob", pattern="*.py", path="/etc"), REFUSE,
             "path_outside_workspace"),
            ("grep path root", request("Grep", pattern="secret", path="/"), REFUSE, "path_outside_workspace"),
            ("patch absolute outside", patch(add("/etc/cron.d/x")), REFUSE, "path_outside_workspace"),
            ("cat environ", bash("cat /proc/self/environ"), REFUSE, "path_outside_workspace"),
            ("ls root", bash("ls /"), REFUSE, "path_outside_workspace"),
            ("option value path", bash("cat --file=/etc/x"), REFUSE, "path_outside_workspace"),
            ("grep recursive outside", bash("grep -r x /etc"), REFUSE, "path_outside_workspace"),
            ("url argument", bash("cat https://example.com/x"), REFUSE, "path_outside_workspace"),
            ("input redirection outside", bash("cat < /etc/passwd"), REFUSE, "path_outside_workspace"),
            ("output redirection outside", bash("ls > /tmp/x"), REFUSE, "path_outside_workspace"),
            ("cwd outside", write("/workspace/src/a.py", cwd="/"), REFUSE, "cwd_outside_workspace"),
            ("cwd traversal", write("/workspace/src/a.py", cwd="/workspace/.."), REFUSE,
             "cwd_outside_workspace"),
            ("cwd relative", write("/workspace/src/a.py", cwd="workspace"), REFUSE, "cwd_outside_workspace"),
            ("codex workdir outside", request("Bash", command="ls", workdir="/etc"), REFUSE,
             "cwd_outside_workspace"),
        ])

    def test_path_traversal_is_refused_before_anything_else(self):
        self.check_rows([
            ("dotdot out of the root", write("/workspace/../etc/passwd"), REFUSE, "path_traversal"),
            ("dotdot back into git", write("/workspace/src/../.git/config"), REFUSE, "path_traversal"),
            ("dotdot that stays inside", write("/workspace/src/../src/a.py"), REFUSE, "path_traversal"),
            ("relative dotdot", write("../x"), REFUSE, "path_traversal"),
            ("relative double dotdot", write("src/../../etc/x"), REFUSE, "path_traversal"),
            ("read dotdot", read("/workspace/../etc/passwd"), REFUSE, "path_traversal"),
            ("glob dotdot", request("Glob", pattern="../*"), REFUSE, "path_traversal"),
            ("grep glob dotdot", request("Grep", pattern="x", glob="../*"), REFUSE, "path_traversal"),
            ("patch dotdot", patch(add("src/../../x")), REFUSE, "path_traversal"),
            ("move to dotdot", patch(update("src/a.py", move_to="../x")), REFUSE, "path_traversal"),
            ("cat dotdot", bash("cat ../secret"), REFUSE, "path_traversal"),
            ("option value dotdot", bash("cat --file=../x"), REFUSE, "path_traversal"),
            ("redirection dotdot", bash("ls > ../x"), REFUSE, "path_traversal"),
        ])

    def test_odd_names_and_malformed_requests_are_refused(self):
        self.check_rows([
            ("nul in name", write("/workspace/src/a\x00.py"), REFUSE, "path_odd_name"),
            ("cr in name", write("/workspace/src/a\r.py"), REFUSE, "path_odd_name"),
            ("newline in name", write("/workspace/src/a\n.py"), REFUSE, "path_odd_name"),
            ("delete char", write("/workspace/src/a\x7f.py"), REFUSE, "path_odd_name"),
            ("zero-width non-joiner hides .git", write("/workspace/.g‌it/config"), REFUSE,
             "path_odd_name"),
            ("byte order mark", write("/workspace/src/﻿a.py"), REFUSE, "path_odd_name"),
            ("bidi override", write("/workspace/src/‮a.py"), REFUSE, "path_odd_name"),
            ("empty path", write(""), REFUSE, "request_malformed"),
            ("path not a string", write(["src/a.py"]), REFUSE, "request_malformed"),
            ("missing file_path", request("Write", content="x"), REFUSE, "request_malformed"),
            ("tool_input not an object", {"tool_name": "Write", "tool_input": "x", "cwd": "/workspace"},
             REFUSE, "request_malformed"),
            ("tool_name missing", {"tool_input": {}, "cwd": "/workspace"}, REFUSE, "request_malformed"),
            ("cwd missing", {"tool_name": "Read", "tool_input": {"file_path": "/workspace/a"}}, REFUSE,
             "request_malformed"),
            ("request not an object", ["Write"], REFUSE, "request_malformed"),
        ])

    def test_unlisted_actions_escalate_by_default_and_refuse_when_the_owner_says_so(self):
        rows = [
            ("write outside the granted paths", write("/workspace/README.md"), "write_outside_scope"),
            ("single star does not cross a directory", write("/workspace/docs/sub/x.md"),
             "write_outside_scope"),
            (".gitignore at the root", write("/workspace/.gitignore"), "write_outside_scope"),
            ("not git: .git-blame-ignore-revs", write("/workspace/.git-blame-ignore-revs"),
             "write_outside_scope"),
            ("case variant of a granted directory", write("/workspace/SRC/a.py"), "write_outside_scope"),
            ("patch outside scope", patch(add("README.md")), "write_outside_scope"),
            ("move outside scope", patch(update("src/a.py", move_to="README.md")), "write_outside_scope"),
            ("redirection outside scope", bash("pytest > out.txt"), "write_outside_scope"),
            ("non-recursive rm outside scope", bash("rm README.md"), "write_outside_scope"),
            ("copy outside scope", bash("cp src/a.py README.md"), "write_outside_scope"),
            ("unlisted program", bash("make"), "command_not_allowed"),
            ("unlisted subcommand", bash("npm test"), "command_not_allowed"),
            ("absolute program path is not the listed name", bash("/usr/bin/git diff"),
             "command_not_allowed"),
            ("git global flag before the subcommand", bash("git --no-pager diff"), "command_not_allowed"),
            ("shell running a script it is not granted", bash("sh build.sh"), "command_not_allowed"),
            ("compound with one unlisted part", bash("ls && make"), "command_not_allowed"),
        ]
        self.check_rows([(c, r, ESCALATE, rule) for c, r, rule in rows])
        self.check_rows([(c, r, REFUSE, rule) for c, r, rule in rows], pol=policy(escalate=()))

    def test_owner_review_categories_escalate_inside_scope(self):
        rows = [
            ("edit an existing test", edit("/workspace/tests/test_a.py"), "write_test_edit"),
            ("edit a go-style test", edit("/workspace/src/a_test.go"), "write_test_edit"),
            ("edit a js spec", edit("/workspace/src/a.spec.ts"), "write_test_edit"),
            ("patch update of a test", patch(update("tests/test_a.py")), "write_test_edit"),
            ("patch delete", patch(delete("src/a.py")), "write_deletion"),
            ("patch rename deletes the source", patch(update("src/a.py", move_to="src/b.py")),
             "write_deletion"),
            ("rm inside scope", bash("rm src/a.py"), "write_deletion"),
            ("recursive rm inside scope", bash("rm -rf src/build"), "write_deletion"),
            ("mv inside scope deletes the source", bash("mv src/a.py src/b.py"), "write_deletion"),
            ("dependency manifest", write("/workspace/requirements.txt"), "write_dependency_manifest"),
            ("nested dependency manifest", write("/workspace/src/requirements-dev.txt"),
             "write_dependency_manifest"),
        ]
        self.check_rows([(c, r, ESCALATE, rule) for c, r, rule in rows])
        self.check_rows([(c, r, ALLOW, "write_in_scope" if r["tool_name"] != "Bash" else "command_allowed")
                         for c, r, _ in rows], pol=policy(escalate=()))


class K03DenyClasses(TableMixin, unittest.TestCase):
    """Always refused, whatever the grant, the owner's policy or the escalation choice."""

    ROWS = [
        # git internals, every case and depth, .git as a file, .gitmodules
        ("git config", write("/workspace/.git/config"), "write_git"),
        ("git upper case", write("/workspace/.GIT/config"), "write_git"),
        ("git mixed case", write("/workspace/.Git/HEAD"), "write_git"),
        ("git odd case", write("/workspace/.gIt/hooks/pre-commit"), "write_git"),
        ("git as a file", write("/workspace/.git"), "write_git"),
        ("nested git file inside a granted dir", write("/workspace/src/.git"), "write_git"),
        ("nested git hooks inside a granted dir", write("/workspace/src/sub/.git/hooks/post-checkout"),
         "write_git"),
        ("gitmodules", write("/workspace/.gitmodules"), "write_git"),
        ("gitmodules nested upper", write("/workspace/src/.GITMODULES"), "write_git"),
        ("git by relative dot", write("./.git/config"), "write_git"),
        ("edit git", edit("/workspace/.git/config"), "write_git"),
        ("patch add git", patch(add(".git/hooks/pre-commit")), "write_git"),
        ("patch update git case", patch(update(".GIT/config")), "write_git"),
        ("patch delete git", patch(delete(".git/index")), "write_git"),
        ("patch move into git", patch(update("src/a.py", move_to=".git/hooks/pre-push")), "write_git"),
        ("patch move out of git", patch(update(".git/config", move_to="src/a.py")), "write_git"),
        ("redirect into git", bash("ls > .git/hooks/pre-commit"), "write_git"),
        ("mkdir git", bash("mkdir .git"), "write_git"),
        ("rm git", bash("rm -rf .git"), "write_git"),
        ("sed in place on git config", bash("sed -i -e s/a/b/ .git/config"), "write_git"),
        # agent configuration, every depth and case
        ("claude settings", write("/workspace/.claude/settings.json"), "write_agent_config"),
        ("claude settings case", write("/workspace/.Claude/settings.local.json"), "write_agent_config"),
        ("nested claude dir", write("/workspace/src/.claude/commands/x.md"), "write_agent_config"),
        ("codex config", write("/workspace/.codex/config.toml"), "write_agent_config"),
        ("codex hooks upper", write("/workspace/.CODEX/hooks.json"), "write_agent_config"),
        ("mcp json", write("/workspace/.mcp.json"), "write_agent_config"),
        ("nested mcp json upper", write("/workspace/src/.MCP.json"), "write_agent_config"),
        ("CLAUDE.md", write("/workspace/CLAUDE.md"), "write_agent_config"),
        ("claude.md lower", write("/workspace/claude.md"), "write_agent_config"),
        ("CLAUDE.md in a granted glob", write("/workspace/docs/CLAUDE.md"), "write_agent_config"),
        ("CLAUDE.local.md", write("/workspace/CLAUDE.local.md"), "write_agent_config"),
        ("AGENTS.md", write("/workspace/AGENTS.md"), "write_agent_config"),
        ("agents.md nested lower", write("/workspace/src/agents.md"), "write_agent_config"),
        ("AGENTS.override.md", write("/workspace/AGENTS.override.md"), "write_agent_config"),
        ("patch add claude settings", patch(add(".claude/settings.json")), "write_agent_config"),
        ("copy over claude settings", bash("cp src/a.py .claude/settings.json"), "write_agent_config"),
        ("copy into a target directory option", bash("cp --target-directory=.claude src/a.py"),
         "write_agent_config"),
        ("copy with an attached target directory", bash("cp -t.codex src/a.py"), "write_agent_config"),
        ("move into a target directory option", bash("mv --target-directory=.git src/a.py"), "write_git"),
        ("tee mcp json", bash("tee .mcp.json"), "write_agent_config"),
        # secrets files
        (".env", write("/workspace/.env"), "write_env"),
        (".env.development", write("/workspace/.env.development"), "write_env"),
        ("nested .env in a granted dir", write("/workspace/src/.env"), "write_env"),
        (".ENV upper", write("/workspace/.ENV"), "write_env"),
        (".envrc", write("/workspace/.envrc"), "write_env"),
        ("nested .env.production", write("/workspace/config/.env.production"), "write_env"),
        ("touch .env", bash("touch .env"), "write_env"),
        # COCOLO's own names
        ("cocolo merge temp", write("/workspace/.cocolo-merge-1-2"), "write_cocolo"),
        ("nested cocolo dir upper", write("/workspace/src/.COCOLO/x"), "write_cocolo"),
        # grant-only (conditional) paths without an effective grant
        ("workflow not granted", write("/workspace/.github/workflows/release.yml"),
         "write_conditional_ungranted"),
        ("workflow case variant", write("/workspace/.GITHUB/Workflows/x.yml"), "write_conditional_ungranted"),
        ("owner grants, task did not request (A03)", write("/workspace/.gitattributes"),
         "write_conditional_ungranted"),
        ("task requests as a write path, no grant", write("/workspace/package-lock.json"),
         "write_conditional_ungranted"),
        ("conftest inside a granted dir", write("/workspace/src/conftest.py"), "write_conditional_ungranted"),
        ("conftest in tests", edit("/workspace/tests/conftest.py"), "write_conditional_ungranted"),
        ("sitecustomize", write("/workspace/src/sitecustomize.py"), "write_conditional_ungranted"),
        ("pth file", write("/workspace/src/evil.pth"), "write_conditional_ungranted"),
        ("Makefile", write("/workspace/Makefile"), "write_conditional_ungranted"),
        ("MAKEFILE upper in src", write("/workspace/src/MAKEFILE"), "write_conditional_ungranted"),
        ("GNUmakefile", write("/workspace/src/GNUmakefile"), "write_conditional_ungranted"),
        ("husky", write("/workspace/.husky/pre-commit"), "write_conditional_ungranted"),
        ("pre-commit config", write("/workspace/.pre-commit-config.yaml"), "write_conditional_ungranted"),
        ("vscode tasks", write("/workspace/.vscode/tasks.json"), "write_conditional_ungranted"),
        (".agents", write("/workspace/.agents/skills/x/SKILL.md"), "write_conditional_ungranted"),
        (".lfsconfig", write("/workspace/.lfsconfig"), "write_conditional_ungranted"),
        ("setup.py", write("/workspace/src/setup.py"), "write_conditional_ungranted"),
        ("pytest.ini", write("/workspace/pytest.ini"), "write_conditional_ungranted"),
        ("tox.ini", write("/workspace/tox.ini"), "write_conditional_ungranted"),
        ("yarn.lock", write("/workspace/yarn.lock"), "write_conditional_ungranted"),
        ("uv.lock", write("/workspace/uv.lock"), "write_conditional_ungranted"),
        ("poetry.lock", write("/workspace/src/poetry.lock"), "write_conditional_ungranted"),
        ("Cargo.lock", write("/workspace/Cargo.lock"), "write_conditional_ungranted"),
        ("patch add workflow", patch(add(".github/workflows/x.yml")), "write_conditional_ungranted"),
        ("patch move onto conftest", patch(update("src/a.py", move_to="src/conftest.py")),
         "write_conditional_ungranted"),
        # network and web tools, MCP, unknown tools
        ("WebFetch", request("WebFetch", url="https://example.com", prompt="x"), "network_tool"),
        ("WebSearch", request("WebSearch", query="x"), "network_tool"),
        ("codex web_search", request("web_search", query="x"), "network_tool"),
        ("mcp tool", request("mcp__github__create_issue", title="x"), "mcp_tool"),
        ("subagent", request("Agent", prompt="x"), "tool_unknown"),
        ("task tool", request("Task", prompt="x"), "tool_unknown"),
        ("unknown tool", request("Frobnicate"), "tool_unknown"),
    ]

    def test_each_deny_class_is_refused(self):
        self.check_rows([(c, r, REFUSE, rule) for c, r, rule in self.ROWS])

    def test_deny_classes_hold_with_escalation_off_and_reads_recorded(self):
        pol = policy(escalate=(), record_reads=True)
        self.check_rows([(c, r, REFUSE, rule) for c, r, rule in self.ROWS], pol=pol)

    def test_a_grant_cannot_open_an_always_denied_path(self):
        grant = dict(GRANT, write_paths=["**"], conditional_paths=["**"])
        owner = dict(OWNER, write_paths=["**"], conditional_paths=["**"])
        pol = policy(grant, owner)
        rows = [
            (c, r, REFUSE, rule) for c, r, rule in self.ROWS
            if rule in {"write_git", "write_agent_config", "write_env", "write_cocolo"}
        ]
        self.assertGreater(len(rows), 30)
        self.check_rows(rows, pol=pol)
        self.check_rows([
            ("conditional path with a full grant", write("/workspace/Makefile"), ALLOW, "write_in_scope"),
        ], pol=pol)

    def test_a_conditional_grant_alone_does_not_make_a_path_writable(self):
        grant = dict(GRANT, write_paths=["src/"], conditional_paths=[".gitattributes"])
        owner = dict(OWNER, conditional_paths=[".gitattributes"])
        self.check_rows([
            ("granted conditional but not an owned path", write("/workspace/.gitattributes"), ESCALATE,
             "write_outside_scope"),
        ], pol=policy(grant, owner))

    def test_commands_always_denied(self):
        self.check_rows([
            # privilege
            ("sudo", bash("sudo ls"), REFUSE, "command_privilege"),
            ("su", bash("su root"), REFUSE, "command_privilege"),
            ("doas", bash("doas ls"), REFUSE, "command_privilege"),
            # network tools, also when listed by the grant and the owner
            ("curl listed", bash("curl https://example.com"), REFUSE, "command_network"),
            ("curl by absolute path", bash("/usr/bin/curl x"), REFUSE, "command_network"),
            ("wget", bash("wget x"), REFUSE, "command_network"),
            ("nc", bash("nc -l 9000"), REFUSE, "command_network"),
            ("ssh", bash("ssh host"), REFUSE, "command_network"),
            ("scp", bash("scp a b"), REFUSE, "command_network"),
            ("rsync", bash("rsync a b"), REFUSE, "command_network"),
            ("ping", bash("ping host"), REFUSE, "command_network"),
            ("socat", bash("socat a b"), REFUSE, "command_network"),
            # package installs
            ("pip install listed", bash("pip install requests"), REFUSE, "command_package_install"),
            ("pip3 install", bash("pip3 install requests"), REFUSE, "command_package_install"),
            ("versioned pip", bash("pip3.12 install x"), REFUSE, "command_package_install"),
            ("python -m pip install", bash("python3 -m pip install x"), REFUSE, "command_package_install"),
            ("uv pip install", bash("uv pip install x"), REFUSE, "command_package_install"),
            ("uv add", bash("uv add x"), REFUSE, "command_package_install"),
            ("npm install", bash("npm install"), REFUSE, "command_package_install"),
            ("npm i", bash("npm i left-pad"), REFUSE, "command_package_install"),
            ("npm ci", bash("npm ci"), REFUSE, "command_package_install"),
            ("npx", bash("npx cowsay"), REFUSE, "command_package_install"),
            ("yarn add", bash("yarn add x"), REFUSE, "command_package_install"),
            ("pnpm add", bash("pnpm add x"), REFUSE, "command_package_install"),
            ("gem install", bash("gem install x"), REFUSE, "command_package_install"),
            ("cargo install", bash("cargo install x"), REFUSE, "command_package_install"),
            ("go get", bash("go get x"), REFUSE, "command_package_install"),
            ("brew", bash("brew install x"), REFUSE, "command_package_install"),
            ("apt-get", bash("apt-get install x"), REFUSE, "command_package_install"),
            ("poetry add", bash("poetry add x"), REFUSE, "command_package_install"),
            ("conda install", bash("conda install x"), REFUSE, "command_package_install"),
            # git that writes history, config or the network; global options that reconfigure git
            ("git commit listed", bash("git commit -m x"), REFUSE, "command_git_denied"),
            ("git push", bash("git push"), REFUSE, "command_git_denied"),
            ("git reset", bash("git reset --hard"), REFUSE, "command_git_denied"),
            ("git clean", bash("git clean -fdx"), REFUSE, "command_git_denied"),
            ("git config", bash("git config core.hooksPath x"), REFUSE, "command_git_denied"),
            ("git fetch", bash("git fetch"), REFUSE, "command_git_denied"),
            ("git pull", bash("git pull"), REFUSE, "command_git_denied"),
            ("git clone", bash("git clone x"), REFUSE, "command_git_denied"),
            ("git remote", bash("git remote add o x"), REFUSE, "command_git_denied"),
            ("git submodule", bash("git submodule update"), REFUSE, "command_git_denied"),
            ("git update-ref", bash("git update-ref HEAD x"), REFUSE, "command_git_denied"),
            ("git -c", bash("git -c core.pager=x log"), REFUSE, "command_git_denied"),
            ("git -C", bash("git -C /tmp status"), REFUSE, "command_git_denied"),
            ("git --git-dir", bash("git --git-dir=x log"), REFUSE, "command_git_denied"),
            ("git --work-tree", bash("git --work-tree=x status"), REFUSE, "command_git_denied"),
            ("git --exec-path", bash("git --exec-path=x log"), REFUSE, "command_git_denied"),
            ("git diff --output", bash("git diff --output=x"), REFUSE, "command_git_denied"),
            ("git diff --ext-diff", bash("git diff --ext-diff"), REFUSE, "command_git_denied"),
            ("git log --output", bash("git log --output x"), REFUSE, "command_git_denied"),
            # deletes outside the scope, symlinks, mode changes, exec options
            ("rm -rf root", bash("rm -rf /"), REFUSE, "path_outside_workspace"),
            ("rm -rf the workspace", bash("rm -rf /workspace"), REFUSE, "command_delete_outside_scope"),
            ("rm -rf dot", bash("rm -rf ."), REFUSE, "command_delete_outside_scope"),
            ("rm -r outside scope", bash("rm -r README.md"), REFUSE, "command_delete_outside_scope"),
            ("rm --recursive outside scope", bash("rm --recursive docs"), REFUSE,
             "command_delete_outside_scope"),
            ("rm -R outside scope", bash("rm -R docs"), REFUSE, "command_delete_outside_scope"),
            ("ln -s", bash("ln -s /etc/passwd src/x"), REFUSE, "command_symlink"),
            ("ln", bash("ln src/a src/b"), REFUSE, "command_symlink"),
            ("cp -s makes symlinks", bash("cp -s src/a.py src/b.py"), REFUSE, "command_symlink"),
            ("cp -l makes hard links", bash("cp -rl src/a src/b"), REFUSE, "command_symlink"),
            ("cp --symbolic-link", bash("cp --symbolic-link src/a.py src/b.py"), REFUSE, "command_symlink"),
            ("chmod", bash("chmod +x src/a.py"), REFUSE, "command_mode_change"),
            ("chown", bash("chown x src/a.py"), REFUSE, "command_mode_change"),
            ("find -delete", bash("find . -name x -delete"), REFUSE, "command_exec_option"),
            ("find -exec", bash("find . -exec rm '{}' ';'"), REFUSE, "command_exec_option"),
            ("find -execdir", bash("find . -execdir x ';'"), REFUSE, "command_exec_option"),
            ("find -fprint", bash("find . -fprint out"), REFUSE, "command_exec_option"),
            ("claude background bash", bash("pytest", run_in_background=True), REFUSE, "command_background"),
        ])


class K04CompoundCommands(TableMixin, unittest.TestCase):
    def test_compound_commands_need_every_part_allowed(self):
        self.check_rows([
            ("pipe of allowed parts", bash("git diff | cat"), ALLOW, "command_allowed"),
            ("and of allowed parts", bash("pytest -q && git status"), ALLOW, "command_allowed"),
            ("or of allowed parts", bash("pytest || cat log.txt"), ALLOW, "command_allowed"),
            ("sequence of allowed parts", bash("ls; ls src"), ALLOW, "command_allowed"),
            ("stderr dup into a pipe", bash("pytest -q 2>&1 | cat"), ALLOW, "command_allowed"),
            ("output to dev null", bash("ls > /dev/null"), ALLOW, "command_allowed"),
            ("stderr to dev null", bash("ls 2>/dev/null"), ALLOW, "command_allowed"),
            ("output into scope", bash("pytest > src/out.txt"), ALLOW, "command_allowed"),
            ("append into scope", bash("pytest >> src/out.txt"), ALLOW, "command_allowed"),
            ("input from the workspace", bash("cat < src/a.py"), ALLOW, "command_allowed"),
            ("bash -c of an allowed command", bash("bash -c 'ls src'"), ALLOW, "command_allowed"),
            ("bash -lc", bash("bash -lc 'git status'"), ALLOW, "command_allowed"),
            ("bash -ec cluster", bash("bash -ec 'ls'"), ALLOW, "command_allowed"),
            ("nested bash -c", bash("bash -c \"bash -c 'ls'\""), ALLOW, "command_allowed"),
            ("eval of an allowed command", bash("eval ls src"), ALLOW, "command_allowed"),
            ("allowed then denied", bash("pytest; curl x"), REFUSE, "command_network"),
            ("denied in a pipe", bash("ls | curl -d @- x"), REFUSE, "command_network"),
            ("denied after or", bash("ls || sudo ls"), REFUSE, "command_privilege"),
            ("bash -c of a denied command", bash("sh -c 'curl x'"), REFUSE, "command_network"),
            ("bash -c of an unlisted command", bash("bash -c make"), ESCALATE, "command_not_allowed"),
            ("eval of a denied command", bash("eval 'curl http://x'"), REFUSE, "command_network"),
            ("nesting too deep", bash("bash -c \"bash -c 'bash -c \\\"bash -c ls\\\"'\""), REFUSE,
             "command_nesting_too_deep"),
            ("codex argv shell of a denied command", request("Bash", command=["bash", "-lc", "curl x"]),
             REFUSE, "command_network"),
        ])

    def test_shell_constructs_that_cannot_be_checked_are_refused(self):
        rows = [
            ("command substitution", "ls $(rm -rf /)"),
            ("backticks", "ls `id`"),
            ("substitution in double quotes", 'ls "$(id)"'),
            ("variable", "echo $HOME"),
            ("variable in double quotes", 'cat "$HOME/x"'),
            ("escaped dollar in double quotes", 'cat "\\$HOME"'),
            ("braced variable", "ls ${X}"),
            ("arithmetic", "ls $((1+1))"),
            ("ansi-c quote", "ls $'\\x41'"),
            ("process substitution", "cat <(ls)"),
            ("output process substitution", "ls > >(cat)"),
            ("subshell", "(ls)"),
            ("brace group", "{ ls; }"),
            ("brace expansion", "ls a{b,c}"),
            ("background", "ls &"),
            ("background then command", "pytest & rm -rf /"),
            ("comment", "ls # ; rm -rf /"),
            ("glob star", "ls *.py"),
            ("glob question", "ls src/?.py"),
            ("glob bracket", "ls [ab].py"),
            ("tilde", "ls ~/x"),
            ("tilde after equals", "ls --dir=~/x"),
            ("negation", "! ls"),
            ("pipe stderr", "ls |& cat"),
            ("case terminator", "ls ;; ls"),
            ("heredoc", "cat <<EOF"),
            ("herestring", "cat <<< x"),
            ("clobber", "ls >| src/x"),
            ("read-write redirection", "ls <> src/x"),
            ("dup to a word", "ls >&src/x"),
            ("close fd", "ls 2>&-"),
            ("all output to file", "ls &> src/x"),
            ("zsh equals expansion", "=ls"),
        ]
        self.check_rows([(c, bash(cmd), REFUSE, "command_shell_construct") for c, cmd in rows])

    def test_malformed_commands_are_refused(self):
        self.check_rows([
            ("newline joins two commands", bash("ls\nrm -rf /"), REFUSE, "command_malformed"),
            ("carriage return", bash("ls\rcat"), REFUSE, "command_malformed"),
            ("tab", bash("ls\tsrc"), REFUSE, "command_malformed"),
            ("unterminated single quote", bash("ls 'x"), REFUSE, "command_malformed"),
            ("unterminated double quote", bash('ls "x'), REFUSE, "command_malformed"),
            ("trailing backslash", bash("ls \\"), REFUSE, "command_malformed"),
            ("empty", bash(""), REFUSE, "command_malformed"),
            ("blank", bash("   "), REFUSE, "command_malformed"),
            ("dangling and", bash("ls &&"), REFUSE, "command_malformed"),
            ("leading and", bash("&& ls"), REFUSE, "command_malformed"),
            ("empty pipe part", bash("ls | | cat"), REFUSE, "command_malformed"),
            ("redirection only", bash("> src/x"), REFUSE, "command_malformed"),
            ("redirection without target", bash("ls >"), REFUSE, "command_malformed"),
            ("too long", bash("ls " + "a" * 20000), REFUSE, "command_malformed"),
            ("command not a string", request("Bash", command=7), REFUSE, "request_malformed"),
            ("argv with a non-string", request("Bash", command=["ls", 3]), REFUSE, "request_malformed"),
            ("empty argv", request("Bash", command=[]), REFUSE, "request_malformed"),
            ("assignment prefix", bash("PYTHONPATH=x pytest"), REFUSE, "command_assignment"),
            ("preload assignment", bash("LD_PRELOAD=/x ls"), REFUSE, "command_assignment"),
            ("shell apply_patch heredoc", bash("apply_patch <<'EOF'\n*** Begin Patch\n*** End Patch\nEOF"),
             REFUSE, "command_malformed"),
        ])


class K05Interpreters(TableMixin, unittest.TestCase):
    def test_bare_or_interactive_interpreters_and_shells_are_refused_even_when_listed(self):
        rows = [
            ("bare bash", "bash", "command_interactive"),
            ("bare sh", "sh", "command_interactive"),
            ("interactive zsh", "zsh -i", "command_interactive"),
            ("bash -i with -c", "bash -i -c ls", "command_interactive"),
            ("bash reading stdin", "bash -s", "command_interactive"),
            ("pipe into a shell", "cat src/a.sh | bash", "command_interactive"),
            ("pipe into python", "cat src/a.py | python3", "command_interactive"),
            ("bare python", "python3", "command_interactive"),
            ("python with options only", "python3 -u", "command_interactive"),
            ("python -i", "python3 -i tools/x.py", "command_interactive"),
            ("python stdin dash", "python3 -", "command_interactive"),
            ("versioned python", "python3.12", "command_interactive"),
            ("absolute python", "/usr/bin/python3", "command_interactive"),
            ("node", "node", "command_interactive"),
            ("irb", "irb", "command_interactive"),
            ("ipython", "ipython", "command_interactive"),
            ("vim", "vim src/a.py", "command_interactive"),
            ("less", "less src/a.py", "command_interactive"),
            ("top", "top", "command_interactive"),
            ("python -c", "python3 -c 'print(1)'", "command_inline_code"),
            ("python -Ic cluster", "python3 -Ic 'print(1)'", "command_inline_code"),
            ("node -e", "node -e 'x'", "command_inline_code"),
            ("node --eval", "node --eval x", "command_inline_code"),
            ("perl -e", "perl -e 'x'", "command_inline_code"),
            ("ruby -e", "ruby -e x", "command_inline_code"),
            ("awk program", "awk '{print}' src/a.py", "command_inline_code"),
            ("exec", "exec ls", "command_wrapper"),
            ("env", "env ls", "command_wrapper"),
            ("env with assignment", "env FOO=1 ls", "command_wrapper"),
            ("nohup", "nohup pytest", "command_wrapper"),
            ("timeout", "timeout 10 pytest", "command_wrapper"),
            ("xargs", "xargs rm", "command_wrapper"),
            ("nice", "nice pytest", "command_wrapper"),
            ("time", "time pytest", "command_wrapper"),
            ("command builtin", "command ls", "command_wrapper"),
            ("source", "source x.sh", "command_wrapper"),
            ("dot source", ". x.sh", "command_wrapper"),
            ("stdbuf", "stdbuf -o0 ls", "command_wrapper"),
            ("strace", "strace ls", "command_wrapper"),
            ("busybox", "busybox sh", "command_wrapper"),
            ("cd", "cd src", "command_wrapper"),
            ("cd then command", "cd /tmp && ls", "command_wrapper"),
        ]
        self.check_rows([(c, bash(cmd), REFUSE, rule) for c, cmd, rule in rows])

    def test_listed_interpreter_running_a_script_follows_the_rules(self):
        self.check_rows([
            ("python script listed", bash("python3 tools/run.py --x"), ALLOW, "command_allowed"),
            ("python -m listed", bash("python3 -m pytest"), ALLOW, "command_allowed"),
            ("bash script listed", bash("bash -x tools/run.sh"), ALLOW, "command_allowed"),
            ("python -m pip install", bash("python3 -m pip install x"), REFUSE, "command_package_install"),
            ("node script unlisted", bash("node tools/x.js"), ESCALATE, "command_not_allowed"),
        ])

    def test_a_listed_sed_runs_only_scripts_that_cannot_write_or_execute(self):
        self.check_rows([
            ("print a range", bash("sed -n '1,200p' src/a.py"), ALLOW, "command_allowed"),
            ("substitute", bash("sed -e 's/a/b/g' src/a.py"), ALLOW, "command_allowed"),
            ("regex address and delete", bash("sed '/^#/d;s|x|y|2' src/a.py"), ALLOW, "command_allowed"),
            ("w command", bash("sed -n 'w .git/x' src/a.py"), REFUSE, "command_inline_code"),
            ("w flag", bash("sed 's/a/b/w out' src/a.py"), REFUSE, "command_inline_code"),
            ("e command", bash("sed 'e curl x' src/a.py"), REFUSE, "command_inline_code"),
            ("e flag", bash("sed 's/a/b/e' src/a.py"), REFUSE, "command_inline_code"),
            ("r command", bash("sed 'r /etc/passwd' src/a.py"), REFUSE, "command_inline_code"),
            ("script file", bash("sed -f src/x.sed src/a.py"), REFUSE, "command_inline_code"),
            ("bracket that hides a delimiter", bash("sed 's/[/]/x/w f' src/a.py"), REFUSE,
             "command_inline_code"),
            ("expression option value", bash("sed --expression='1e id' src/a.py"), REFUSE,
             "command_inline_code"),
        ])


class M32Intersection(TableMixin, unittest.TestCase):
    SAMPLE = [
        write("/workspace/src/a.py"), write("/workspace/README.md"), write("/workspace/Makefile"),
        write("/workspace/.gitattributes"), write("/workspace/.github/workflows/ci.yml"),
        write("/workspace/docs/a.md"), write("/workspace/.git/config"), edit("/workspace/tests/test_a.py"),
        write("/workspace/requirements.txt"), patch(delete("src/a.py")),
        bash("pytest"), bash("git diff"), bash("git status"), bash("git push"), bash("make"),
        bash("python3 tools/x.py"), bash("ls && git log"), bash("npm test"), bash("rm src/a.py"),
        bash("git diff --stat"), bash("curl x"), read("/workspace/src/a.py"), read("/etc/hosts"),
    ]

    def test_the_effective_policy_is_never_wider_than_the_request_or_the_owner(self):
        grant = {
            "write_paths": ["src/", "docs/a.md", "Makefile", "README.md"],
            "conditional_paths": ["Makefile", ".github/workflows/ci.yml"],
            "commands": [["pytest"], ["git"], ["make"], ["rm"]],
        }
        owner = {
            "write_paths": ["src/a.py", "docs/", "Makefile", ".github/"],
            "conditional_paths": [".gitattributes", "Makefile"],
            "commands": [["git", "diff"], ["python3"], ["make"], ["ls"]],
        }
        as_owner = {k: grant[k] for k in ("write_paths", "conditional_paths", "commands")}
        as_grant = {k: owner[k] for k in ("write_paths", "conditional_paths", "commands")}
        for escalate in (gp.RECOMMENDED_ESCALATE, ()):
            effective = policy(grant, owner, escalate=escalate)
            request_only = policy(grant, as_owner, escalate=escalate)
            owner_only = policy(as_grant, owner, escalate=escalate)
            for index, req in enumerate(self.SAMPLE):
                with self.subTest(escalate=bool(escalate), case=index):
                    eff = SEVERITY[gp.decide(effective, req).outcome]
                    self.assertGreaterEqual(eff, SEVERITY[gp.decide(request_only, req).outcome])
                    self.assertGreaterEqual(eff, SEVERITY[gp.decide(owner_only, req).outcome])

    def test_command_rules_intersect_to_the_narrower_prefix(self):
        pol = policy({"write_paths": [], "conditional_paths": [], "commands": [["git"], ["ls", "-la"]]},
                     {"write_paths": ["**"], "conditional_paths": [],
                      "commands": [["git", "diff"], ["ls"], ["make"]]})
        self.assertEqual({("git", "diff"), ("ls", "-la")}, set(pol.command_rules))
        self.check_rows([
            ("narrowed by the owner", bash("git diff"), ALLOW, "command_allowed"),
            ("owner did not list status", bash("git status"), ESCALATE, "command_not_allowed"),
            ("narrowed by the request", bash("ls -la src"), ALLOW, "command_allowed"),
            ("request did not list plain ls", bash("ls src"), ESCALATE, "command_not_allowed"),
            ("only the owner listed make", bash("make"), ESCALATE, "command_not_allowed"),
        ], pol=pol)

    def test_owner_write_paths_narrow_the_request(self):
        pol = policy(dict(GRANT, write_paths=["src/"]), dict(OWNER, write_paths=["src/a.py"]))
        self.check_rows([
            ("both allow", write("/workspace/src/a.py"), ALLOW, "write_in_scope"),
            ("owner narrower", write("/workspace/src/b.py"), ESCALATE, "write_outside_scope"),
        ], pol=pol)

    def test_a01_no_owner_policy_refuses_writes_and_commands(self):
        pol = gp.build_policy(GRANT, None)
        self.check_rows([
            ("write in the requested scope", write("/workspace/src/a.py"), REFUSE, "owner_policy_missing"),
            ("command in the request", bash("pytest"), REFUSE, "owner_policy_missing"),
            ("deny class still names its class", write("/workspace/.git/config"), REFUSE, "write_git"),
            ("reads still allowed", read("/workspace/src/a.py"), ALLOW, "read_in_workspace"),
        ], pol=pol)

    def test_a04_a_changed_owner_policy_never_widens(self):
        digest = gp.owner_policy_digest(OWNER)
        self.assertEqual(digest, gp.owner_policy_digest(dict(OWNER)))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        same = policy(routed_owner_policy_digest=digest)
        self.check_rows([("same digest", write("/workspace/src/a.py"), ALLOW, "write_in_scope")], pol=same)

        stale = policy(routed_owner_policy_digest="0" * 64)
        self.check_rows([
            ("write after a change", write("/workspace/src/a.py"), REFUSE, "owner_policy_changed"),
            ("command after a change", bash("pytest"), REFUSE, "owner_policy_changed"),
            ("deny class still names its class", write("/workspace/.env"), REFUSE, "write_env"),
            ("reads still allowed", read("/workspace/src/a.py"), ALLOW, "read_in_workspace"),
        ], pol=stale)

        routed = dict(OWNER, conditional_paths=[".github/workflows/ci.yml"])
        narrower = dict(OWNER, conditional_paths=[])
        pol = policy(owner=narrower, routed_owner_policy=routed,
                     routed_owner_policy_digest=gp.owner_policy_digest(routed))
        self.check_rows([("narrower current refuses", write("/workspace/.github/workflows/ci.yml"), REFUSE,
                          "write_conditional_ungranted")], pol=pol)
        pol = policy(owner=routed, routed_owner_policy=narrower)
        self.check_rows([("wider current does not widen", write("/workspace/.github/workflows/ci.yml"),
                          REFUSE, "write_conditional_ungranted")], pol=pol)
        with self.assertRaises(gp.GatePolicyError):
            policy(routed_owner_policy=routed, routed_owner_policy_digest="0" * 64)

    def test_invalid_grants_and_policies_are_rejected(self):
        bad_grants = [
            ("not a mapping", ["src/"]),
            ("unknown key", dict(GRANT, extra=[])),
            ("missing key", {"write_paths": [], "commands": []}),
            ("dotdot", dict(GRANT, write_paths=["../x"])),
            ("absolute", dict(GRANT, write_paths=["/workspace/src"])),
            ("empty entry", dict(GRANT, write_paths=[""])),
            ("dot component", dict(GRANT, write_paths=["src/./a"])),
            ("empty component", dict(GRANT, write_paths=["src//a"])),
            ("unsupported glob", dict(GRANT, write_paths=["src/[ab].py"])),
            ("control char", dict(GRANT, write_paths=["src/\na"])),
            ("not a string", dict(GRANT, write_paths=[3])),
            ("empty command rule", dict(GRANT, commands=[[]])),
            ("empty command word", dict(GRANT, commands=[[""]])),
            ("command rule as a string", dict(GRANT, commands=["pytest"])),
        ]
        for case, grant in bad_grants:
            with self.subTest(case=case), self.assertRaises(gp.GatePolicyError):
                gp.build_policy(grant, OWNER)
        with self.subTest(case="owner unknown key"), self.assertRaises(gp.GatePolicyError):
            gp.build_policy(GRANT, dict(OWNER, extra=1))
        with self.subTest(case="unknown escalation category"), self.assertRaises(gp.GatePolicyError):
            policy(escalate=("everything",))
        with self.subTest(case="wait not positive"), self.assertRaises(gp.GatePolicyError):
            policy(owner_wait_seconds=0)


class OwnerChoices(TableMixin, unittest.TestCase):
    def test_recommended_defaults_are_the_research_recommendation(self):
        self.assertEqual(
            {"unlisted_command", "unlisted_write", "test_edit", "deletion", "dependency_manifest"},
            set(gp.RECOMMENDED_ESCALATE))
        pol = policy()
        self.assertEqual(frozenset(gp.RECOMMENDED_ESCALATE), pol.escalate)
        self.assertFalse(pol.record_reads)
        self.assertEqual(600, pol.owner_wait_seconds)
        self.assertEqual("/workspace", pol.workspace)

    def test_reads_are_recorded_only_when_the_owner_chooses(self):
        self.assertFalse(gp.decide(policy(), read("/workspace/src/a.py")).record)
        self.assertTrue(gp.decide(policy(record_reads=True), read("/workspace/src/a.py")).record)
        for req in (read("/etc/hosts"), write("/workspace/src/a.py"), bash("pytest"), bash("make"),
                    write("/workspace/.git/x")):
            with self.subTest(tool=req["tool_name"]):
                self.assertTrue(gp.decide(policy(), req).record)

    def test_partial_escalation_choice(self):
        pol = policy(escalate=("deletion",))
        self.check_rows([
            ("deletion still asks", patch(delete("src/a.py")), ESCALATE, "write_deletion"),
            ("unlisted write refused", write("/workspace/README.md"), REFUSE, "write_outside_scope"),
            ("unlisted command refused", bash("make"), REFUSE, "command_not_allowed"),
            ("test edit allowed", edit("/workspace/tests/test_a.py"), ALLOW, "write_in_scope"),
        ], pol=pol)

    def test_the_policy_digest_changes_with_every_choice(self):
        digests = {
            policy().digest,
            policy(escalate=()).digest,
            policy(record_reads=True).digest,
            policy(owner_wait_seconds=60).digest,
            policy(dict(GRANT, write_paths=["src/"])).digest,
            policy(owner=dict(OWNER, commands=[["ls"]])).digest,
        }
        self.assertEqual(6, len(digests))
        self.assertEqual(policy().digest, policy().digest)


class CodexPatches(TableMixin, unittest.TestCase):
    def test_patch_headers_are_parsed_strictly(self):
        self.check_rows([
            ("one bad file among good ones", patch(add("src/x.py"), add(".git/x"), update("src/a.py")), REFUSE,
             "write_git"),
            ("escalation among allowed", patch(add("src/x.py"), add("README.md")), ESCALATE,
             "write_outside_scope"),
            ("refusal wins over escalation", patch(add("README.md"), add(".env")), REFUSE, "write_env"),
            ("indented header after a hunk", patch(update("src/a.py") + "\n *** Add File: .git/x\n+x"),
             REFUSE, "patch_ambiguous"),
            ("header with carriage return", patch("*** Add File: src/a.py\r\n+x"), REFUSE, "patch_ambiguous"),
            ("path with leading space", patch("*** Add File:  src/a.py\n+x"), REFUSE, "patch_ambiguous"),
            ("unknown header", patch("*** Copy File: src/a.py"), REFUSE, "patch_malformed"),
            ("header without space", patch("***Add File: .git/x\n+x"), REFUSE, "patch_malformed"),
            ("move header form not in the grammar", patch(update("src/a.py") + "\n*** Move File: .git/x"),
             REFUSE, "patch_malformed"),
            ("move without update", patch("*** Move to: src/b.py"), REFUSE, "patch_malformed"),
            ("move after add", patch(add("src/x.py") + "\n*** Move to: src/y.py"), REFUSE, "patch_malformed"),
            ("missing begin", patch(add("src/x.py"), begin=None), REFUSE, "patch_malformed"),
            ("missing end", patch(add("src/x.py"), end=None), REFUSE, "patch_malformed"),
            ("no file operations", patch(), REFUSE, "patch_malformed"),
            ("empty path after the marker", patch("*** Add File: \n+x"), REFUSE, "patch_ambiguous"),
            ("second begin", patch(add("src/x.py"), "*** Begin Patch"), REFUSE, "patch_malformed"),
            ("heredoc wrapper", request("apply_patch", command="<<'EOF'\n*** Begin Patch\n*** Add File: "
                                        "src/a.py\n+x\n*** End Patch\nEOF\n"), REFUSE, "patch_malformed"),
            ("patch text not a string", request("apply_patch", command=5), REFUSE, "request_malformed"),
            ("too large", request("apply_patch", command="*** Begin Patch\n*** Add File: src/a.py\n"
                                  + "+x\n" * (3 * 1024 * 1024) + "*** End Patch\n"), REFUSE,
             "patch_malformed"),
            ("end of file marker inside a hunk", patch(update("src/a.py") + "\n*** End of File"), ALLOW,
             "write_in_scope"),
        ])


class K06RecordRow(unittest.TestCase):
    def test_the_row_matches_the_client_ids_and_bounds_the_subject(self):
        pol = policy()
        command = "pytest -q " + "x" * 2000
        req = bash(command, tool_use_id="toolu_abc")
        decision = gp.decide(pol, req)
        row = gp.record_row(run_id="run-1", request=req, decision=decision, policy=pol,
                            requested_at="2026-11-21T00:00:00Z", decided_at="2026-11-21T00:00:01Z")
        self.assertEqual("run-1", row["run_id"])
        self.assertEqual("toolu_abc", row["tool_use_id"])
        self.assertEqual("Bash", row["tool_name"])
        self.assertEqual(decision.outcome, row["outcome"])
        self.assertEqual(decision.rule, row["rule"])
        self.assertEqual(decision.reason, row["reason"])
        self.assertEqual("gate", row["decided_by"])
        self.assertEqual(pol.digest, row["policy_digest"])
        self.assertEqual("2026-11-21T00:00:00Z", row["requested_at"])
        self.assertEqual("2026-11-21T00:00:01Z", row["decided_at"])
        self.assertLessEqual(len(row["subject"]), gp.RECORD_SUBJECT_LIMIT)
        self.assertTrue(row["subject_truncated"])
        self.assertEqual(hashlib.sha256(command.encode("utf-8")).hexdigest(), row["subject_sha256"])

    def test_codex_call_id_and_patch_paths(self):
        req = patch(add("src/x.py"), update("src/a.py", move_to="src/b.py"))
        req["tool_use_id"] = "call_xyz"
        row = gp.record_row(run_id="run-2", request=req, decision=gp.decide(policy(), req), policy=policy(),
                            requested_at="a", decided_at="b", decided_by="owner")
        self.assertEqual("call_xyz", row["tool_use_id"])
        self.assertEqual("owner", row["decided_by"])
        self.assertEqual("src/x.py\nsrc/a.py\nsrc/b.py", row["subject"])
        self.assertFalse(row["subject_truncated"])

    def test_malformed_requests_still_make_a_row(self):
        req = ["not", "a", "request"]
        row = gp.record_row(run_id="run-3", request=req, decision=gp.decide(policy(), req), policy=policy(),
                            requested_at="a", decided_at="b")
        self.assertEqual("refuse", row["outcome"])
        self.assertIsNone(row["tool_use_id"])
        self.assertEqual("", row["subject"])

    def test_decide_is_pure(self):
        pol = policy()
        req = patch(add("src/x.py"))
        snapshot = repr(req)
        first = gp.decide(pol, req)
        self.assertEqual(snapshot, repr(req))
        self.assertEqual(first, gp.decide(pol, req))
        with self.assertRaises(Exception):
            pol.workspace = "/"


if __name__ == "__main__":
    unittest.main()
