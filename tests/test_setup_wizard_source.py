"""S5: the Python wizard keeps the properties setup.sh's source tests checked (retired by mapping).

Each test names the SetupScriptTests test it replaces. They read the wizard's own source, the
launcher's and the entry script's, never running them.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ROOT / "scripts"))
WIZARD = ROOT / "scripts" / "setup_wizard"
SOURCES = {path.name: path.read_text(encoding="utf-8") for path in sorted(WIZARD.glob("*.py"))}
SOURCES["setup-wizard.py"] = (ROOT / "scripts" / "setup-wizard.py").read_text(encoding="utf-8")
ALL = "\n".join(SOURCES.values())
LAUNCHER = (ROOT / "setup.sh").read_text(encoding="utf-8")


def launches(source: str):
    """Every list given as the first argument of subprocess.run/Popen, as source text."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.args):
            yield ast.get_source_segment(source, node.args[0])


# Review 040-s5-wizard-3 (b): one audited way to start a process. Only these functions may use
# subprocess, and each passes its argument list through unchanged; every launch in the flow takes its
# list from a builder that adds the isolation flags or Git's safety settings.
SUBPROCESS_USERS = {("terminal.py", "Terminal", "run_cmd"), ("terminal.py", "Terminal", "call"),
                    ("effects.py", None, "git_commit"), ("flow.py", "Wizard", "handoff")}
BUILDERS = {"self.python_argv", "self.git_argv", "effects.owner_git_argv"}
LAUNCH_POSITION = {"run": 1, "run_cmd": 1, "call": 0, "handoff": 0}
GIT_CALLS = {"self.g", "self.git_argv", "install._git_run"}
PORCELAIN = {"status", "add", "commit", "diff"}


def _functions(tree):
    """(class, function, node) for every function, with its enclosing class."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    yield node.name, item.name, item
        elif isinstance(node, ast.Module):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    yield None, item.name, item


def launch_problems(source: str, name: str = "flow.py") -> list[str]:
    tree = ast.parse(source)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias for alias in node.names]
            module = getattr(node, "module", None)
            if module == "subprocess" or any(a.name == "subprocess" and a.asname for a in names):
                problems.append(ast.get_source_segment(source, node))
    permitted = set()
    for owner, function, node in _functions(tree):
        if (name, owner, function) in SUBPROCESS_USERS:
            permitted.update(id(n) for n in ast.walk(node))
    starters = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    for node in ast.walk(tree):  # Anywhere, module level included, except inside a permitted function.
        starts = (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "subprocess" and node.attr in starters)
        aliased = (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name)
                   and node.value.id == "subprocess")
        if (starts or aliased) and id(node) not in permitted:
            problems.append(f"subprocess started or aliased at line {node.lineno}")
    for owner, function, node in _functions(tree):
        if (name, owner, function) in SUBPROCESS_USERS:
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
                    if any(isinstance(t, ast.Name) and t.id == "argv" for t in targets):
                        problems.append(f"argv reassigned in {owner}.{function}")
    if name == "flow.py":
        forwarded = set()  # Wizard.run forwards its own argv parameter, unchanged, to the runner.
        for owner, function, node in _functions(tree):
            if (owner, function) == ("Wizard", "run"):
                assigned = any(isinstance(t, ast.Name) and t.id == "argv" for inner in ast.walk(node)
                               if isinstance(inner, ast.Assign) for t in inner.targets)
                if not assigned:
                    forwarded.update(id(inner) for inner in ast.walk(node)
                                     if isinstance(inner, ast.Name) and inner.id == "argv")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LAUNCH_POSITION):
                continue
            receiver = ast.get_source_segment(source, node.func.value)
            if receiver not in ("self", "self.t"):
                continue
            argv = next((k.value for k in node.keywords if k.arg in ("argv", "args")), None)
            position = LAUNCH_POSITION[node.func.attr]
            if argv is None and len(node.args) > position:
                argv = node.args[position]
            if argv is None:
                continue
            if id(argv) in forwarded:
                continue
            builder = ast.get_source_segment(source, argv.func) if isinstance(argv, ast.Call) else ""
            if builder not in BUILDERS or (builder == "effects.owner_git_argv"
                                           and ast.get_source_segment(source, argv.args[0]) != "self.py"):
                problems.append(ast.get_source_segment(source, node))
    return problems


def git_problems(source: str) -> list[str]:
    """Git arguments are literal (no variable, concatenation or f-string after the project); ls-files only
    through install._git_run; no porcelain command."""
    tree = ast.parse(source)
    problems, allowed = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.get_source_segment(source, node.func) in GIT_CALLS:
            func = ast.get_source_segment(source, node.func)
            rest = node.args[1:] if func != "self.git_argv" else node.args
            for arg in rest:
                if isinstance(arg, ast.Constant) and arg.value == "--":
                    break  # Paths follow, as literal pathspecs.
                if isinstance(arg, ast.Starred) and func == "install._git_run":
                    continue
                if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name) and arg.value.id == "args":
                    continue  # self.g forwarding its own literal-checked arguments
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    problems.append(ast.get_source_segment(source, node))
                    break
                if arg.value in PORCELAIN:
                    problems.append(ast.get_source_segment(source, node))
            if func == "install._git_run":
                allowed.update(id(arg) for arg in node.args)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "ls-files" in node.value:
            if id(node) not in allowed:
                problems.append(ast.get_source_segment(source, node))
    return problems


class WizardSourceTests(unittest.TestCase):
    def test_the_checks_reject_every_reviewed_bypass(self) -> None:
        """Reviews 040-s5-wizard-2 and -3 (b): each bypass a review built is rejected."""
        launches = {
            "variable argv": "program = 'python3'\nargv = [program]\nsubprocess.run(argv)\n",
            "aliased module": "sp = subprocess\nsp.run(['git'])\n",
            "keyword args": "def f():\n    subprocess.run(args=['git', 'status'])\n",
            "aliased import": "import subprocess as sp\nsp.run(['git'])\n",
            "from import": "from subprocess import run\n",
            "replaced argv in a wrapper": ("class Terminal:\n    def call(self, argv):\n"
                                            "        argv = ['git', 'status']\n        subprocess.Popen(argv)\n"),
            "git list without the builder": ("class Wizard:\n    def f(self, project):\n"
                                              "        self.t.call([self.git, '-C', project, 'status'])\n"),
            "python list without the builder": "class Wizard:\n    def f(self):\n        self.run('x', [ self.py, '-c', 'pass'])\n",
            "keyword argv without the builder": "class Wizard:\n    def f(self):\n        self.t.call(argv=[self.py])\n",
            "argv changed in place in a wrapper": ("class Terminal:\n    def call(self, argv):\n"
                                                   "        argv[:] = ['git', 'status']\n        subprocess.Popen(argv)\n"),
            "a wrapper launching its own list": ("class Terminal:\n    def call(self, argv):\n"
                                                 "        subprocess.Popen(['git', 'status'])\n"),
            "a wrapper appending to argv": ("class Terminal:\n    def call(self, argv):\n"
                                            "        argv.append('x')\n        subprocess.Popen(argv)\n"),
            "an aliased runner": "class Wizard:\n    def f(self):\n        runner = self.t.call\n        runner(['git'])\n",
            "os.system": "import os\nos.system('git status')\n",
            "os.execv": "import os\nos.execv('/usr/bin/git', ['git'])\n",
            "os.posix_spawn": "import os\nos.posix_spawn('/usr/bin/git', ['git'], {})\n",
            "pty.spawn": "import pty\npty.spawn(['git'])\n",
            "a forwarder that rewrites argv": ("class Wizard:\n    def run(self, label, argv):\n        argv = ['git']\n"
                                               "        return self.t.run_cmd(label, argv)\n"),
        }
        for case, source in launches.items():
            with self.subTest(case=case):
                self.assertTrue(launch_problems(source, "flow.py"))
        gits = {
            "concatenated ls-files": "self.g(project, 'ls-' + 'files', '--', name)\n",
            "variable command": "command = 'commit'\nself.g(str(project), command)\n",
            "porcelain": "self.g(project, 'status')\n",
            "f-string": "self.g(project, f'{x}')\n",
            "ls-files outside the installer": "self.g(project, 'ls-files')\n",
        }
        for case, source in gits.items():
            with self.subTest(case=case):
                self.assertTrue(git_problems(source))
        self.assertEqual([], git_problems("install._git_run(Path(p), 'ls-files', '-z', '--', *candidates)\n"))

    def test_every_launch_uses_the_audited_builders(self) -> None:
        for name, source in SOURCES.items():
            with self.subTest(source=name):
                self.assertEqual([], launch_problems(source, name))

    def test_every_git_call_is_literal_and_never_porcelain(self) -> None:
        for name, source in SOURCES.items():
            with self.subTest(source=name):
                self.assertEqual([], git_problems(source))

    def test_the_git_builder_accepts_only_the_wizards_read_only_invocations(self) -> None:
        """Review 040-s5-wizard-4 (b): the builder is the boundary, not the scanner: no configuration
        override, alias, other directory or other command can follow the safety settings."""
        from types import SimpleNamespace
        from setup_wizard import flow
        wizard = SimpleNamespace(git="/trusted/git")
        for args in (("--version",), ("rev-parse", "--show-toplevel"), ("rev-parse", "--is-inside-work-tree")):
            with self.subTest(args=args):
                self.assertEqual(list(args), flow.Wizard.git_argv(wizard, *args, project="/p")[-len(args):])
        for args in (("-c", "alias.owned=!./hook", "owned"), ("-c", "core.fsmonitor=./x", "status"), ("status",),
                     ("-C", "/elsewhere", "rev-parse", "--show-toplevel"), ("--exec-path=/x", "rev-parse"),
                     ("rev-parse", "--show-toplevel", "--git-dir"), ("commit", "-m", "x"), ("config", "--list"), ()):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    flow.Wizard.git_argv(wizard, *args, project="/p")

    def test_the_builders_add_the_isolation_flags_and_gits_safety_settings(self) -> None:
        from types import SimpleNamespace
        from setup_wizard import flow
        wizard = SimpleNamespace(py="/trusted/python3", git="/trusted/git")
        self.assertEqual(["/trusted/python3", "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", "a.py", "x"],
                         flow.Wizard.python_argv(wizard, "a.py", "x"))
        self.assertEqual(["/trusted/git", "-C", "/p", *flow.GIT_SAFE, "rev-parse"],
                         flow.Wizard.git_argv(wizard, "rev-parse", project="/p"))
        self.assertEqual(["/trusted/git", *flow.GIT_SAFE, "--version"], flow.Wizard.git_argv(wizard, "--version"))

    def test_every_python_the_wizard_starts_is_isolated(self) -> None:
        """Replaces SetupScriptTests.test_every_python_the_wizard_starts_is_isolated (W1.3, W1.6)."""
        # Every argument list that starts with the trusted interpreter, whether it goes to subprocess
        # directly or through the runner (self.run, handoff).
        # Since review 040-s5-wizard-3 every Python launch goes through the one builder, which adds the
        # flags (test_the_builders_add_...); the only list naming the interpreter is the builder's own.
        self.assertGreaterEqual(SOURCES["flow.py"].count("self.python_argv("), 8)
        self.assertEqual(["[self.py, *PYF, *args]"], re.findall(r"\[self\.py\b[^\]]*\]", ALL))
        self.assertIn('PYF = ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"]', SOURCES["flow.py"])
        self.assertIn('exec "$PY" "${PYF[@]}" "$ROOT_DIR/scripts/setup-wizard.py" "$@"', LAUNCHER)
        self.assertIn("PYF=(-I -B -S -X pycache_prefix=/dev/null)", LAUNCHER)
        for line in ALL.splitlines():
            if "python3 -I" in line:
                self.assertIn("python3 -I -B -S -X pycache_prefix=/dev/null", line)
        # No other interpreter is ever started: no list starting with a python name or sys.executable.
        self.assertIsNone(re.search(r'\[\s*["\'](/[^"\']*/)?python[0-9.]*["\']', ALL))
        self.assertNotRegex(ALL, r"\[\s*sys\.executable")
        # Every launch call's argument list starts with the trusted interpreter, git or a variable
        # (handoff's argv is built from [self.py, *PYF, ...] above).
        for source in SOURCES.values():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("run", "Popen", "call", "run_cmd") and node.args
                        and isinstance(node.args[0], ast.List) and node.args[0].elts):
                    first = ast.get_source_segment(source, node.args[0].elts[0])
                    with self.subTest(launch=first):
                        self.assertIn(first, ("self.py", "self.git"))
        # Owner commands run this clone's CLI, never the project's copy.
        self.assertIn("self.python_argv(str(CLONE_CLI), *args)", SOURCES["flow.py"])
        self.assertNotIn('".codex", "orchestration", "cli.py")]', ALL)

    def test_every_git_call_uses_the_trusted_binary_with_helpers_off(self) -> None:
        """Replaces SetupScriptTests.test_every_git_call_uses_the_trusted_binary_with_helpers_off (W1.4)."""
        for source in SOURCES.values():
            for arg in launches(source):
                with self.subTest(launch=arg):
                    self.assertFalse(re.match(r'\[\s*"git"', arg), arg)
        self.assertIn('[self.git, *(["-C", project] if project is not None else []), *GIT_SAFE, *args]',
                      SOURCES["flow.py"])
        from setup_wizard import flow
        self.assertEqual(["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
                          "-c", "tag.gpgSign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false"], flow.GIT_SAFE)
        self.assertIn("GIT_SAFE=(-c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgSign=false "
                      "-c tag.gpgSign=false -c gc.auto=0 -c maintenance.auto=false)", LAUNCHER)
        self.assertIn('GIT_ENV = {"xcrun_nocache": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}',
                      SOURCES["flow.py"])
        # No porcelain: status, add and commit go through the installer's plumbing.
        # No porcelain: no self.g call names status, add, commit or diff (the syntax tree, so nested
        # parentheses or quotes cannot hide one); commits go through the installer's git tool.
        for name, source in SOURCES.items():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and ast.get_source_segment(source, node.func) == "self.g":
                    words = {arg.value for arg in node.args if isinstance(arg, ast.Constant)}
                    self.assertFalse(words & {"status", "add", "commit", "diff"}, (name, words))
        self.assertIn("sys.exit(module.git_tool(sys.argv[2:]))", SOURCES["effects.py"])
        self.assertIn('effects.owner_git_argv(self.py, "commit"', SOURCES["flow.py"])

    def test_every_git_call_with_paths_is_literal(self) -> None:
        """Replaces SetupScriptTests.test_every_git_call_with_paths_is_literal (W5.2)."""
        for needle in (" add -A -- ", " commit -m "):
            lines = [line for line in ALL.splitlines() if needle in line]
            self.assertTrue(lines, needle)
            for line in lines:
                self.assertIn("--literal-pathspecs", line)
        # ls-files runs through the installer's _git_run, which always passes --literal-pathspecs.
        self.assertIn('"--literal-pathspecs", *args]', (ROOT / "scripts" / "install.py").read_text(encoding="utf-8"))
        found = list(re.finditer(r'"ls-files"', ALL))
        self.assertGreaterEqual(len(found), 2)
        for match in found:
            call = ALL[ALL.rfind("\n", 0, match.start()) - 200:match.start()]
            with self.subTest(at=ALL[match.start() - 60:match.end()]):
                self.assertIn("install._git_run(", call)  # Always --literal-pathspecs (install.py's _git_run).

    def test_provider_clients_are_never_run_by_name(self) -> None:
        """Replaces SetupScriptTests.test_provider_clients_are_never_run_by_name (W1.5)."""
        for source in SOURCES.values():
            for arg in launches(source):
                with self.subTest(launch=arg):
                    self.assertFalse(re.match(r'\[\s*"(codex|claude)"', arg), arg)
        self.assertNotRegex(ALL, r"shutil\.which\(\s*\"(codex|claude)\"")
        self.assertIn('self.owner_tool("client-status", name', SOURCES["flow.py"])
        # The message naming where the orchestrator runs clients from is the runtime's list.
        sys.path.insert(0, str(ORCHESTRATION))
        try:
            from codex_agents.providers import TRUSTED_SEARCH_DIRECTORIES
        finally:
            sys.path.remove(str(ORCHESTRATION))
        message = re.search(r"The orchestrator runs \{name\} only from ([^\"]*?)\.\"", ALL.replace('"\n', "").replace(
            '                                "', "")).group(1)
        self.assertEqual(re.split(r", | or ", message), list(TRUSTED_SEARCH_DIRECTORIES))

    def test_commands_and_commit_screens_say_what_the_rule_and_commit_do(self) -> None:
        """Replaces SetupScriptTests.test_commands_and_commit_screens_say_what_the_rule_and_commit_do (W8.2, W5.3)."""
        from setup_wizard import steps
        rule = " ".join(steps.commands_body("$RULE_FILE", "missing"))
        self.assertIn("Every Codex session on this machine loads this rule from $RULE_FILE (mode 0600). "
                      "It runs a protected copy of the orchestrator in ~/.codex that sessions opened on a "
                      "project cannot change. Never open Codex on the orchestrator clone, a folder that "
                      "contains a set-up project, your home folder, its parent or / while the rule exists.", rule)
        self.assertIn("Setup commits with Git's plumbing commands only (hash-object, update-index, write-tree, "
                      "commit-tree, update-ref), which start no hook, filter, signing program, pager or "
                      "maintenance whatever the project's Git settings say", steps.commit_body(3))
        self.assertNotIn("It applies to Codex sessions started in this project's root folder", ALL)
        self.assertIn("Setup commits with Git's plumbing commands only (hash-object, update-index, write-tree, ", ALL)
        self.assertNotIn("no script in the project runs as you", ALL)
        self.assertNotIn("or a commit hook stopped it", ALL)
        self.assertIn("Keep this setup folder outside every folder you open in Codex", ALL)
        self.assertIn("including the Codex commands rule", ALL)

    def test_header_and_welcome_name_the_product(self) -> None:
        """Replaces SetupScriptTests.test_header_and_welcome_name_the_product (W9.2)."""
        self.assertIn(" COCOLO setup ", SOURCES["terminal.py"])
        self.assertNotIn("Agent orchestration setup", ALL)
        self.assertIn('"Welcome to COCOLO\\nClaude Orchestrator, Codex Orchestrator, Local Orchestrator"', ALL)

    def test_wizard_messages_for_the_polished_screens(self) -> None:
        """Replaces SetupScriptTests.test_wizard_messages_for_the_polished_screens (W5.4, W7, W8.1, W10.1)."""
        for text in ("Nothing to commit: the project already has these files committed", "The install can be updated.",
                     "A fresh install is possible.", "Profile: exists with the same settings", "network_access is off",
                     '"Change the owner key passphrase"', '"Replace a lost owner key"',
                     '"--target", self.project, "--replace-owner-key")', '"--change-passphrase")',
                     'f"{self.home}/agent-orchestration-setup-summary.txt"'):
            with self.subTest(text=text):
                self.assertIn(text, ALL)
        self.assertNotIn("getcwd()}/agent-orchestration-setup-summary.txt", ALL)


if __name__ == "__main__":
    unittest.main()
