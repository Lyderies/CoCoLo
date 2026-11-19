"""The wizard's steps (S5): setup.sh's step functions and main loop, ported one for one. Decisions
come from `steps`, file and Git work from `effects`, the screen and keys from `terminal`."""

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import effects, steps
from .terminal import Stop

CLONE = effects.CLONE
ORCH_DIR = CLONE / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
CLONE_CLI = ORCH_DIR / "cli.py"
NATIVE_PACKAGE = CLONE / "packages" / "codex-agents" / "native-package"
INSTALLER = CLONE / "scripts" / "install.py"
ATTEST = CLONE / "scripts" / "attest.py"
PYF = ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"]
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
GIT_ENV = {"xcrun_nocache": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}
CLAUDE_MODELS_ALL = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
FAIL_HINT_DEFAULT = "Press ? to see what ran in this step and why it stopped."
COMMIT_MSG_1 = "Install COCOLO"
COMMIT_MSG_2 = "Adds the managed Codex and Claude agent packages and the provider routing configuration."
PROFILE_CONTENT = 'sandbox_mode = "workspace-write"\n\n[sandbox_workspace_write]\nnetwork_access = true'
MANAGED = (".agents", ".claude", ".codex")
# setup.sh's owner_tool loader, word for word: attest.py loaded by path, its owner_tool run.
OWNER_TOOL = ("import importlib.util, sys\n"
              "spec = importlib.util.spec_from_file_location(\"agent_orchestration_attest\", sys.argv[1])\n"
              "module = importlib.util.module_from_spec(spec)\n"
              "spec.loader.exec_module(module)\n"
              "sys.exit(module.owner_tool(sys.argv[2:]))")


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _resolve_dir(path: str) -> str:
    """setup.sh's resolve_dir: the folder's physical path, or "" when it cannot be entered."""
    try:
        if path and os.path.isdir(path) and os.access(path, os.X_OK):
            return os.path.realpath(path)
    except (OSError, ValueError):
        pass
    return ""


def _regular_size(path: str) -> int:
    """A regular file's size without following a link; -1 otherwise."""
    import stat
    try:
        info = os.lstat(path)
    except OSError:
        return -1
    return info.st_size if stat.S_ISREG(info.st_mode) else -1


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class Wizard:
    def __init__(self, term, *, start: str, python: str, owner_path: str, bash_version: str) -> None:
        self.t = term
        self.start_dir = start
        self.py = python
        self.owner_path = owner_path
        self.bash_version = bash_version
        self.home = os.environ.get("HOME") or str(Path.home())
        self.git = ""
        self.logdir: Path | None = None
        self.slow_seconds = int(os.environ.get("RUN_SLOW_SECONDS") or 10)
        self.collector_ready_seconds = int(os.environ.get("COLLECTOR_READY_SECONDS") or 90)
        self.go_back = False
        self.reset_attempt()

    # Bookkeeping ----------------------------------------------------------------------------

    def reset_attempt(self) -> None:
        """setup.sh's reset_attempt: nothing from one project or mode reaches the next (W9.4)."""
        for number in range(2, 10):
            self.t.stat.pop(number, None)
        self.note: dict[int, str] = {1: getattr(self, "note", {}).get(1, "")}
        self.failed_any = False
        self.failures: list[str] = []
        self.fail_reason = ""
        self.mode = "full"
        self.install_exists = False
        self.profile_wanted = self.profile_ready = self.codex_primary = False
        self.rule_file = self.rule_copy = self.rule_status = self.rule_reason = ""
        self.rule_ready = self.rule_exists = self.temp_project = False
        self.routing_content = ""
        self.routing_write = False
        self.install_opts: list[str] = []
        self.preview: dict | None = None
        self.plan: list = []
        self.planned: list[str] = []
        self.commit_paths: list[str] = []
        self.commit_state = ""
        self.uncommitted = ""
        self.verify_limited = False
        self.project = ""
        self.last_reply = None
        self.last_fixed = False
        self.runtime_why = ""

    def begin_step(self, number: int) -> None:
        if self.t.step != number:
            self.t.step_log = []
        self.t.step = number
        self.t.screen_reset()

    def mark_step(self, number: int, state: str, note: str) -> None:
        self.t.stat[number] = state
        self.note[number] = note

    def stopped(self, code: int) -> Stop:
        return Stop(code, "Stopped.")

    @property
    def last_rc(self) -> int:
        return getattr(self.t, "last_rc", 0)

    @last_rc.setter
    def last_rc(self, value: int) -> None:
        self.t.last_rc = value

    def record_failure(self, label: str, hint: str) -> None:
        self.failed_any = True
        name = self.t.names[self.t.step - 1] if self.t.step > 0 else ""
        self.failures.append(steps.failure_line(self.t.step, name, label, self.last_rc, hint))

    def failure_detail(self, label: str, hint: str = FAIL_HINT_DEFAULT) -> None:
        why = steps.plain(steps.reason(self.last_reply, self.last_fixed)) if self.last_reply is not None else ""
        self.record_failure(label, hint)
        self.t.list_add("fail", f"{label} (exit {self.last_rc})")
        if why:
            self.t.list_sub(f"Reason: {why}")
        self.t.list_sub(hint)

    def failure_menu(self) -> None:
        choice = self.t.menu(0, ["Back to the main menu", "Continue to the summary", "Quit"])
        if choice == 0:
            self.go_back = True
        elif choice == 2:
            raise Stop(0, "Stopped.")

    def retry_menu(self) -> str:
        if self.mode == "full":
            choice = self.t.menu(0, ["Try again", "Skip for now", "Back to the main menu", "Quit"])
            if choice == 3:
                raise Stop(0, "Stopped.")
            return ("try", "skip", "back")[choice]
        choice = self.t.menu(0, ["Try again", "Back to the main menu", "Quit"])
        if choice == 2:
            raise Stop(0, "Stopped.")
        return ("try", "back")[choice]

    # Running --------------------------------------------------------------------------------

    def run(self, label: str, argv: list[str], *, quiet: bool = False, fixed: bool = False, draw=None) -> int:
        """setup.sh's run_cmd; the reply (standard output as JSON) is kept for the step's checks. `draw`
        draws the screen once the command has started."""
        self.last_fixed = fixed
        status = self.t.run_cmd(label, argv, logdir=self.logdir, slow_seconds=self.slow_seconds, quiet=quiet,
                                reason=lambda path: steps.reason(_load(path), fixed), draw=draw)
        self.last_reply = None if quiet else _load(self.t.last_out)
        return status

    def python_argv(self, *args: str) -> list[str]:
        """The trusted interpreter, isolated (W1.3): the one way the wizard starts Python."""
        return [self.py, *PYF, *args]

    def git_argv(self, *args: str, project: str | None = None) -> list[str]:
        """The trusted git with every helper and maintenance setting off (W1.4): the one way the wizard
        starts Git directly."""
        return [self.git, *(["-C", project] if project is not None else []), *GIT_SAFE, *args]

    def run_installer(self, label: str, *args: str, draw=None) -> int:
        return self.run(label, self.python_argv(str(INSTALLER), *args), draw=draw)

    def run_owner_cli(self, label: str, *args: str, draw=None) -> int:
        """This clone's own orchestrator CLI; the project's copy never runs (W1.6)."""
        status = self.run(label, self.python_argv(str(CLONE_CLI), *args), fixed=True, draw=draw)
        self.last_fixed = True
        return status

    def owner_tool(self, *args: str) -> subprocess.CompletedProcess:
        """setup.sh's owner_tool: one of attest.py's owner checks, in the trusted Python, isolated. A
        process, as in setup.sh, so the argv the tests intercept is the same."""
        return self.t.call(self.python_argv("-c", OWNER_TOOL, str(ATTEST), *args))

    def client_status(self, name: str, top: str | None) -> tuple[int, bytes]:
        """W1.5: a client's sign-in status as the runtime runs it (attest.py client-status)."""
        done = self.owner_tool("client-status", name, *([top] if top else []))
        return done.returncode, done.stdout

    def session_scan(self):
        """W2.1: the runtime's scan of recent Codex sessions (this clone's codex-session-scan)."""
        done = self.t.call(self.python_argv(str(CLONE_CLI), "codex-session-scan", "--clone-root", str(CLONE)))
        if done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout)
        except ValueError:
            return None

    def g(self, project: str, *args: str) -> subprocess.CompletedProcess:
        return self.t.call(self.git_argv(*args, project=project), text=True, env={**os.environ, **GIT_ENV})

    def manual_installer(self, operation: str, project: str, apply: bool = False) -> str:
        return (f"python3 -I -B -S -X pycache_prefix=/dev/null {steps.shell_quote(str(INSTALLER))} {operation} "
                f"--target {steps.shell_quote(project)}" + (" --apply" if apply else ""))

    def runtime_matches(self) -> bool:
        problem = effects.runtime_problem(self.project)
        if not problem:
            return True
        self.last_rc = 1
        self.runtime_why = steps.plain(problem.replace("\n", " ")) or "the installed orchestrator could not be checked"
        return False

    def runtime_refusal(self) -> None:
        self.failure_detail("The installed orchestrator does not match this setup",
                            "This step needs the project's installed orchestrator to be this setup's exact version. "
                            "Update the install (choose 'Update the install and walk through every step'), or "
                            "restore the project's files with git.")
        self.t.list_sub(f"Reason: {self.runtime_why}")

    def explain_installer(self, label: str, manual: str) -> None:
        message, known = steps.installer_refusal(steps.raw(self.last_reply), manual)
        if not known:
            self.record_failure(label, f"installer refusal not recognised; run: {manual}")
            self.t.list_add("fail", label)
            self.t.list_sub(message)
            self.t.list_cmd(manual)
            return
        self.record_failure(label, message)
        self.t.list_add("fail", label)
        self.t.list_sub(message)

    def git_helpers_text(self, found: list[str] | None) -> str:
        shown = " ".join(found)[:120] if found else "(the settings could not be read)"
        return (f"This project's Git settings name programs that Git would run ({shown}). Setup runs Git as you, "
                "so it will not run Git commands that could start them.")

    def handoff(self, argv: list[str]) -> int:
        """Hand the terminal to attest.py, then keep its summary on screen until Enter (W7.1)."""
        self.t.show_cursor()
        self.t.write("\033[H\033[2J")
        # In the foreground and the wizard's own group, as setup.sh: attest.py needs the terminal for its
        # passphrase prompts, so it cannot run in a group of its own (review 040-s5-wizard c).
        status = subprocess.run(argv).returncode
        status = status if status >= 0 else 128 - status
        self.t.write(f"\n  attest.py finished (exit code {status}). Press Enter to continue. ")
        self.t._line()
        return status

    # Step 1: checks -------------------------------------------------------------------------

    def client_problem(self, name: str, product: str, login: str, code: int) -> None:
        if code == 11:
            if self.owner_path_lookup(name):
                self.t.list_add("fail", f"{name}: found on your PATH, but not where the orchestrator runs it from, so it "
                                        "was not run")
                self.t.list_sub(f"The orchestrator runs {name} only from /opt/homebrew/bin, /usr/local/bin, /usr/bin, "
                                "/bin or ~/.local/bin.")
                self.t.list_sub(f"Fix: install {product} with Homebrew or its official installer, then run: {login}")
            else:
                self.t.list_add("fail", f"{name} not found")
                self.t.list_sub(f"Fix: install {product}, then run: {login}")
        elif code == 12:
            self.t.list_add("fail", f"{name}: refused, so it was not run")
            self.t.list_sub("Its program file is not in a protected installation folder, or a folder on the way to it "
                            "is a link or can be changed by others, so an AI agent could replace it.")
            self.t.list_sub(f"Fix: reinstall {product} with Homebrew or its official installer, then re-check.")
        else:
            self.t.list_add("fail", f"{name}: its sign-in could not be checked")
            self.t.list_sub(f"Fix: run: {login}, then re-check.")

    def owner_path_lookup(self, name: str) -> str:
        for folder in self.owner_path.split(":"):
            if folder.startswith("/"):
                candidate = os.path.join(folder, name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        return ""

    def work_tree_top(self, start: str) -> str | None:
        folder = _resolve_dir(start)
        while folder:
            if os.path.lexists(os.path.join(folder, ".git")):
                return folder
            if folder == "/":
                return None
            folder = os.path.dirname(folder)
        return None

    def step_checks(self) -> None:
        while True:
            self.begin_step(1)
            hard = soft = False
            t = self.t
            t.body("Welcome to COCOLO\nClaude Orchestrator, Codex Orchestrator, Local Orchestrator")
            t.body("This wizard sets up delegation between Codex and Claude children in one of your Git projects. It "
                   "uses only your existing subscription logins and never any metered API billing. First, a quick "
                   "look at this machine.")
            t.body("Keep this setup folder outside every folder you open in Codex, so no Codex session can change the "
                   "code that runs with your authority.")
            system = platform.system()
            if system == "Darwin":
                t.list_add("ok", f"Operating system: {system}")
            elif system == "Linux":
                t.list_add("ok", f"Operating system: {system}")
                t.list_add("warn", "Operational checks need macOS today, so verify reports not ready on Linux.")
                t.list_sub("Read-only setup still installs; this is a known limitation, not an error.")
            else:
                t.list_add("fail", f"Operating system: {system}")
                t.list_sub("Fix: run this on macOS or Linux (WSL counts as Linux).")
                hard = True
            t.list_add("ok", f"bash: {self.bash_version}")
            t.list_add("ok", "python3: %d.%d.%d" % sys.version_info[:3])
            top = self.work_tree_top(self.start_dir)
            self.git = effects.git_executable([Path(self.start_dir)]) or ""
            if self.git:
                version = self.t.call(self.git_argv("--version"), text=True, env={**os.environ, "xcrun_nocache": "1"})
                words = version.stdout.split(" ")
                t.list_add("ok", f"git: {words[2].strip() if len(words) > 2 else ''}")
            else:
                t.list_add("fail", "No trusted Git found")
                t.list_sub("Fix: install the Command Line Tools (xcode-select --install) or Homebrew's git on macOS, or "
                           "git in /usr/bin on Linux.")
                hard = True
            if not hard:
                code, _ = self.client_status("codex", top)
                if code == 0:
                    t.list_add("ok", "codex: installed, signed in")
                elif code == 10:
                    t.list_add("fail", "codex: installed but not signed in")
                    t.list_sub("Fix: run: codex login")
                    soft = True
                else:
                    self.client_problem("codex", "the Codex CLI", "codex login", code)
                    soft = True
                code, out = self.client_status("claude", top)
                if code in (0, 10):
                    logged, plan, method = steps.claude_login(out)
                    if logged:
                        t.list_add("ok", f"claude: signed in (plan: {plan}, login: {method})")
                    else:
                        t.list_add("fail", "claude: installed but not signed in")
                        t.list_sub("Fix: run: claude auth login")
                        soft = True
                else:
                    self.client_problem("claude", "Claude Code", "claude auth login", code)
                    soft = True
            else:
                t.list_add("skip", "codex and claude sign-ins: checked once the failures above are fixed")
            anthropic, openai = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("OPENAI_API_KEY")
            if anthropic or openai:
                t.list_add("fail", "API key set in the environment (name only shown)")
                if anthropic:
                    t.list_sub("ANTHROPIC_API_KEY is set")
                if openai:
                    t.list_sub("OPENAI_API_KEY is set")
                t.list_sub("Metered billing is never used, so child runs would be refused while these are set.")
                t.list_sub("Fix: quit (q), run 'unset ANTHROPIC_API_KEY OPENAI_API_KEY' in your shell, then run "
                           "./setup.sh again.")
                soft = True
            else:
                t.list_add("ok", "No ANTHROPIC_API_KEY or OPENAI_API_KEY set")
            warned = False
            if not hard:
                for index, line in enumerate(steps.session_warnings(self.session_scan())):
                    if line.startswith("Open Codex on the project itself"):
                        t.list_sub(line)
                    else:
                        t.list_add("warn", line)
                        warned = True
            if hard:
                t.body("Fix the failures above, then re-check. Setup cannot continue without them.")
                if t.menu(0, ["Re-check", "Quit"]) == 1:
                    raise Stop(1, "Stopped.")
                continue
            if soft:
                t.body("Some checks failed. You can continue now, but providers that are not signed in cannot be "
                       "attested or used until fixed.")
                choice = t.menu(0, ["Re-check", "Continue anyway", "Quit"])
                if choice == 0:
                    continue
                if choice == 2:
                    raise Stop(1, "Stopped.")
                self.mark_step(1, "ok", "checks done with warnings")
            elif warned:
                t.body("Read the warning above, then continue.")
                if t.menu(0, ["Continue", "Quit"]) == 1:
                    raise Stop(0, "Stopped.")
                self.mark_step(1, "ok", "checks passed with a Codex session warning")
            else:
                t.body("Everything looks good.")
                if t.menu(0, ["Continue", "Quit"]) == 1:
                    raise Stop(0, "Stopped.")
                self.mark_step(1, "ok", "all checks passed")
            return

    # Step 2: project ------------------------------------------------------------------------

    def projects_file(self) -> Path | None:
        return effects.projects_file()

    def remember_project(self, folder: str) -> None:
        path = self.projects_file()
        if path is None:
            return
        lines = effects.read_projects(path)
        kept = steps.remembered(lines, folder)
        if kept is not None:
            effects.write_projects(path, kept)

    def pick_project(self, default: str) -> str | None:
        path = self.projects_file()
        lines = effects.read_projects(path) if path is not None else []
        paths, items = steps.project_choices(
            lines, default, str(CLONE),
            lambda folder: os.path.isfile(os.path.join(folder, ".codex", "orchestration", "install-manifest.json")),
            lambda folder: os.path.isdir(os.path.join(folder, ".git")))
        if not paths:
            return None
        choice = self.t.menu(0, items)
        if choice < len(paths):
            self.answer_raw = paths[choice]
            return paths[choice]
        if choice == len(paths):
            return None
        raise Stop(0, "Stopped.")

    def basic_problem(self, folder: str) -> str:
        if not os.path.isdir(folder):
            return "That path is not a directory."
        result = self.g(folder, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            return "That directory is not inside a Git working tree. Run: git init"
        top = _resolve_dir(result.stdout.rstrip("\n"))  # As $( ): only the line ending, never spaces.
        if top != folder:
            return f"That is inside a Git project but not its top. Use the project root instead: {top}"
        if folder == _resolve_dir(str(CLONE)):
            return ("That is this orchestrator's own folder, where setup runs from, not a project to set up. Type the "
                    "folder of the project you want to use it in.")
        if _inside(self.py, folder):
            return ("The python3 on your PATH is inside this project, where an AI agent could change it. Put a system "
                    "Python, such as Homebrew's or python.org's, first on PATH.")
        if _inside(self.git, folder):
            return "The trusted Git found is inside this project, where an AI agent could change it."
        return ""

    def managed_problem(self, folder: str) -> str:
        helpers = effects.git_helpers(folder)
        if helpers != []:
            return (self.git_helpers_text(helpers) + " Remove those settings from .git/config, or install and commit by "
                    "hand (docs/manual-setup.md).")
        why: list[str] = []
        dirty = effects.git_changes(folder, list(MANAGED), why)
        if dirty is None:
            return ("Git could not read the project, so setup cannot tell whether its managed paths are clean: "
                    + steps.plain(" ".join(why))[:200])
        if dirty:
            return "Uncommitted changes exist in managed paths (.codex, .agents, .claude). Commit or discard them first."
        return ""

    def prepare_project_inputs(self, folder: str) -> None:
        self.install_opts = []
        inputs = self.logdir / "inputs"
        if not os.path.isfile(os.path.join(folder, ".codex", "orchestration", "project-agent-routing.json")):
            (inputs / "project-agent-routing.json").write_text('{"version": 1, "roles": {}, "verifiers": {}}\n')
            self.install_opts += ["--project-agent-routing", str(inputs / "project-agent-routing.json")]
        if not os.path.isfile(os.path.join(folder, ".codex", "orchestration", "operational-checks.json")):
            while True:
                self.t.screen_reset()
                self.begin_step(2)
                self.t.body("This project has no operational checks file yet. The installer needs one: it names the "
                            "exact test and security commands, with their hashes, that operational QA may run. It cannot "
                            "be guessed. docs/manual-setup.md, section 'Operational checks', has a short recipe to build "
                            "it (two wrapper scripts and their hashes). A future wizard step will generate it; for now it "
                            "is a manual preparation step.")
                self.t.body("Enter the path to your prepared operational-checks.json (it is copied into the project), or "
                            "type q to quit.")
                path, _ = self.t.ask_text("Path to operational-checks.json", "", home=self.home)
                snapshot = inputs / "operational-checks.json"
                snapshot.unlink(missing_ok=True)
                if os.path.isfile(path) and effects.copy_input(path, snapshot):
                    self.install_opts += ["--operational-checks", str(snapshot)]
                    return
                self.t.screen_reset()
                self.begin_step(2)
                self.t.list_add("fail", f"No such file, or not a readable regular file of at most 1 MiB: {path}")
                if self.t.menu(0, ["Try again", "Quit"]) == 1:
                    raise Stop(1, "Stopped.")

    def choose_another(self) -> None:
        if self.t.menu(0, ["Choose another path", "Quit"]) == 1:
            raise Stop(1, "Stopped.")

    def step_project(self, default: str) -> None:
        t = self.t
        intro = ("Choose the project to set up. It must be the top folder of a Git working tree. Nothing is changed in "
                 "this step; it only previews.")
        while True:
            self.begin_step(2)
            t.body(intro)
            answer = self.pick_project(default)
            if answer is None:
                t.screen_reset()
                self.begin_step(2)
                t.body(intro)
                answer, self.answer_raw = t.ask_text("Project path", default, home=self.home)
                if not answer:
                    continue
            folder = _resolve_dir(answer)
            t.screen_reset()
            self.begin_step(2)
            if not folder:
                guess = _resolve_dir(self.home + "/" + answer.lstrip("/")) if answer else ""
                for index, line in enumerate(steps.cannot_open(answer, self.answer_raw, guess or None)):
                    t.list_add("fail", line) if index == 0 else t.list_sub(line)
                if guess:
                    choice = t.menu(0, ["Use the path shown above", "Choose another path", "Quit"])
                    if choice == 1:
                        continue
                    if choice == 2:
                        raise Stop(1, "Stopped.")
                    folder = guess
                    t.screen_reset()
                    self.begin_step(2)
                else:
                    if t.menu(0, ["Choose another path", "Quit"]) == 1:
                        raise Stop(1, "Stopped.")
                    continue
            problem = self.basic_problem(folder)
            if problem:
                t.list_add("fail", folder)
                t.list_sub(problem)
                self.choose_another()
                default = folder
                continue
            self.mode = "full"
            self.install_exists = False
            if os.path.isfile(os.path.join(folder, ".codex", "orchestration", "install-manifest.json")):
                self.install_exists = True
                self.remember_project(folder)
                t.list_add("ok", f"Project: {folder}")
                self.owner_key_notice()
                t.body("An existing install was found in this project. Choose what to do.")
                choice = t.menu(0, ["Update the install and walk through every step", "Verify the install only",
                                    "Re-run the billing attestation only", "Change the owner key passphrase",
                                    "Replace a lost owner key", "Choose another path", "Quit"])
                if choice == 5:
                    default = folder
                    continue
                if choice == 6:
                    raise Stop(0, "Stopped.")
                self.mode = ("full", "verify", "attest", "passphrase", "replace")[choice]
                t.screen_reset()
                self.begin_step(2)
            if self.mode != "full":
                self.project = folder
                self.mark_step(2, "ok", self.project)
                return
            problem = self.managed_problem(folder)
            if problem:
                t.list_add("fail", folder)
                t.list_sub(problem)
                self.choose_another()
                default = folder
                continue
            self.prepare_project_inputs(folder)
            t.screen_reset()
            self.begin_step(2)
            t.body(f"Previewing the install for: {folder}")
            status = self.run_installer("Previewing the install", "install", "--target", folder, *self.install_opts, draw=self.t.render)
            self.preview = self.last_reply
            t.screen_reset()
            self.begin_step(2)
            if status != 0:
                self.explain_installer("The install preview was refused", self.manual_installer("install", folder))
                self.choose_another()
                default = folder
                continue
            entries = effects.plan_entries(self.preview, folder, NATIVE_PACKAGE)
            planned = effects.plan_paths(entries) if self.preview is not None else None
            if planned is None:
                t.list_add("fail", folder)
                if self.preview is not None:
                    t.list_sub("Unsafe file name in managed paths (a control character, edge space or invalid text in a "
                               "file name). Nothing was changed.")
                else:
                    t.list_sub("The install plan could not be read. Nothing was changed.")
                self.record_failure("unsafe file name in managed paths", "refused before any change")
                self.choose_another()
                default = folder
                continue
            self.project = folder
            self.plan, self.planned = entries, planned
            t.list_add("ok", f"Project: {self.project}")
            if self.install_exists:
                t.body("The install can be updated. Continue to choose providers.")
            else:
                t.body("A fresh install is possible. Continue to choose providers.")
            choice = t.menu(0, ["Continue", "Choose another path", "Quit"])
            if choice == 1:
                default = folder
                continue
            if choice == 2:
                raise Stop(0, "Stopped.")
            self.mark_step(2, "ok", self.project)
            return

    # Step 3: providers ----------------------------------------------------------------------

    def step_providers(self) -> bool:
        t = self.t
        existing = os.path.join(self.project, ".codex", "orchestration", "provider-routing.json")
        self.routing_write = True
        self.begin_step(3)
        status, raw_routing = effects.read_routing(existing)
        if status == "refused" and _regular_size(existing) > effects.ROUTING_READ_LIMIT:
            t.list_add("fail", "provider-routing.json is larger than 1 MiB; refused")
            t.list_sub("Replace it with a regular file in the project, commit, and run setup again.")
            self.mark_step(3, "fail", "provider-routing.json refused")
            self.fail_reason = "provider configuration refused"
            self.failure_menu()
            return False
        if status == "refused":
            t.list_add("fail", "provider-routing.json is a symlink or not a regular file; refused")
            t.list_sub("Replace it with a regular file in the project, commit, and run setup again.")
            self.mark_step(3, "fail", "provider-routing.json refused")
            self.fail_reason = "provider configuration refused"
            self.failure_menu()
            return False
        if status == "present":
            t.body("This project already has a provider configuration (.codex/orchestration/provider-routing.json). "
                   "Keep it, or choose providers again and replace it.")
            if t.menu(0, ["Keep the existing configuration", "Choose providers again"]) == 0:
                self.routing_write = False
                self.routing_content = raw_routing.decode("utf-8", "replace")
                import re
                if re.search(r'"codex_primary_may_bridge_to_claude": *true', self.routing_content):
                    self.profile_wanted = True
                if steps.codex_primary(raw_routing):
                    self.codex_primary = True
                self.mark_step(3, "ok", "kept existing configuration")
                return True
        state, error = "111110", ""
        while True:
            self.begin_step(3)
            t.body("Choose which providers may run child agents in this project. Use Space to toggle, Enter to accept. "
                   "The Claude model rows only matter while Claude children are on.")
            t.body("'Let Codex sessions orchestrate Claude children' lets a Codex primary start Claude children. Codex "
                   "runs commands without network by default, so this needs the Codex orchestrator profile, which step 7 "
                   "offers to create.")
            if error:
                t.list_add("fail", error)
            state = t.toggles(state, ["Codex children", "Claude children", *(f"    {m}" for m in CLAUDE_MODELS_ALL),
                                      "Let Codex sessions orchestrate Claude children"])
            codex = "on" if state[0] == "1" else "off"
            claude = "on" if state[1] == "1" else "off"
            models = ",".join(m for index, m in enumerate(CLAUDE_MODELS_ALL) if state[2 + index] == "1")
            bridge = "on" if state[5] == "1" else "off"
            error = ""
            if claude == "off":
                models = "-"
                if bridge == "on":
                    error = "Bridging to Claude needs Claude children turned on."
            if not error:
                try:
                    self.routing_content = steps.provider_routing(codex, claude, models, bridge)
                except steps.ChoiceError as exc:
                    error, self.routing_content = str(exc), ""
            if not error:
                break
        self.begin_step(3)
        t.body("This will be written to .codex/orchestration/provider-routing.json in step 4:")
        t.body(self.routing_content.rstrip("\n"))
        if bridge == "on":
            self.profile_wanted = True
            t.body("Step 7 will offer the Codex orchestrator profile that bridging needs.")
        choice = t.menu(0, ["Looks right, continue", "Choose again", "Quit"])
        if choice == 1:
            return self.step_providers()  # As setup.sh: a profile wanted earlier stays wanted.
        if choice == 2:
            raise Stop(0, "Stopped.")
        self.codex_primary = codex == "on" or bridge == "on"
        self.mark_step(3, "ok", f"codex={codex} claude={claude} bridge={bridge}")
        return True

    # Step 4: install ------------------------------------------------------------------------

    def show_plan(self) -> None:
        t = self.t
        for kind, label, cap in (("ADD", "Files to add", 6), ("UPDATE", "Files to update (content differs)", 15),
                                 ("REMOVE", "Obsolete files to remove", 15)):
            rels = [rel for entry, rel in self.plan if entry == kind]
            t.list_add("info", f"{label}: {len(rels)}")
            for rel in rels[:cap]:
                t.list_sub(f"{kind} {rel}")
            if len(rels) > cap:
                t.list_sub(f"... and {len(rels) - cap} more")
        t.list_add("info", f"Files unchanged: {sum(entry == 'KEEP' for entry, _ in self.plan)}")
        local = sum(entry == "LOCAL" for entry, _ in self.plan)
        if local:
            t.list_add("info", f"Learning hooks: written for this checkout only, never committed ({local} files)")
        for entry, rel in self.plan:
            if entry == "UNTRACK":
                t.list_sub(f"Taken out of Git (now written for this checkout only): {rel}")
        for entry, rel in self.plan:
            if entry == "CONFIG":
                t.list_add("info", f"Project config: {rel}")

    def view_plan(self) -> None:
        self.t.view_text("\n".join(f"{entry} {rel}" for entry, rel in self.plan
                                   if entry in ("ADD", "UPDATE", "REMOVE", "UNTRACK")), "Full plan")

    def verify_install(self, draw=None) -> bool:
        status = self.run_installer("Verifying the install", "verify", "--target", self.project, draw=draw)
        if status == 0:
            self.t.list_add("ok", "Verification passed: the install is ready")
            return True
        if platform.system() == "Linux" and steps.verify_class(self.last_reply) == "linux-limited":
            self.verify_limited = True
            self.t.list_add("warn", "Verification is not ready: expected on Linux, because operational checks need "
                                    "macOS today.")
            self.t.list_sub("Everything else verified.")
            return False
        self.explain_installer("Verification is not ready yet", self.manual_installer("verify", self.project))
        return False

    def hooks_trust_line(self, trusted: bool) -> None:
        if trusted:
            self.t.list_add("info", "In Codex, open /hooks and trust the orchestrator's learning hooks (main checkout "
                                    "only)")
        else:
            self.t.list_add("warn", f"Do not trust the orchestrator's learning hooks in Codex yet: {self.runtime_why}")

    def step_install(self) -> bool:
        t = self.t
        project = self.project
        self.begin_step(4)
        t.body("Close every Codex session in this project first.")
        t.body(f"Install into: {project}")
        self.show_plan()
        t.body("This adds or updates files under .codex, .agents and .claude, then writes "
               ".codex/orchestration/provider-routing.json. Nothing outside the project is touched.")
        while True:
            choice = t.menu(0, ["Install now", "Show the full list of files", "Skip the install", "Quit"])
            if choice == 1:
                self.view_plan()
                continue
            break
        if choice == 2:
            self.mark_step(4, "skip", "install skipped")
            return True
        if choice == 3:
            raise Stop(0, "Stopped.")
        t.screen_reset()
        self.begin_step(4)
        if self.run_installer("Installing", "install", "--target", project, *self.install_opts, "--apply",
                              draw=t.render) != 0:
            t.screen_reset()
            self.begin_step(4)
            self.explain_installer("The install failed", self.manual_installer("install", project, apply=True))
            t.list_sub("The installer rolls back a partial install where it can.")
            self.mark_step(4, "fail", "install failed")
            self.fail_reason = "install failed"
            self.failure_menu()
            return False
        applied = self.last_reply
        t.screen_reset()
        self.begin_step(4)
        t.list_add("ok", "Packages installed")
        before = self.last_rc
        self.hooks_trusted = self.runtime_matches()
        self.last_rc = before
        self.hooks_trust_line(self.hooks_trusted)
        if self.routing_write:
            try:
                effects.write_project_routing(project, self.routing_content)
                t.list_add("ok", "Wrote .codex/orchestration/provider-routing.json")
            except (effects.EffectRefused, OSError):
                t.list_add("fail", "Could not write provider-routing.json")
                t.list_sub("The destination is a symlink, or its folders are not real folders inside the project.")
                self.record_failure("provider-routing.json not written", "destination refused or not writable")
                self.mark_step(4, "fail", "provider routing not written")
                self.fail_reason = "provider routing not written"
                self.failure_menu()
                return False
        else:
            t.list_add("ok", "Kept the existing provider-routing.json")
        candidates, paths_rc = self.commit_candidates(applied)
        paths_problem = "unsafe file name in managed paths" if paths_rc == 3 else ("failed" if paths_rc else "")
        paths_ok = bool(candidates) and not paths_problem
        nothing = False
        ignored_n = 0
        git_safe = effects.git_helpers(project) == []
        if paths_ok:
            self.commit_paths = steps.changed_paths(effects.git_changes(project, candidates) if git_safe else None,
                                                    candidates)
            ignored = effects.git_ignored(project, candidates) if git_safe else None
            ignored_n = (len(ignored) if ignored is not None else 0) + (ignored is None)
            nothing = not self.commit_paths and ignored_n == 0
            if ignored is None:
                t.list_add("warn", "Git could not check whether installed files are ignored, so setup cannot tell "
                                   "whether anything needs committing.")
            if ignored:
                t.list_add("warn", "Some installed files are ignored by the project's .gitignore, so Git will not "
                                   "commit them:")
                for path in ignored[:5]:
                    t.list_sub(steps.plain(path))
                if len(ignored) > 5:
                    t.list_sub(f"... and {len(ignored) - 5} more")
        else:
            self.commit_paths = []
        commit_done = False
        if paths_ok and not self.commit_paths and ignored_n > 0:
            self.commit_state = "failed"
            self.last_rc = 1
            self.record_failure("installed files are ignored by .gitignore or could not be checked",
                                "check .gitignore and git status yourself, add the files with git add -f if needed, "
                                "then choose Verify")
            t.list_sub("Nothing was committed. Check .gitignore and git status yourself (git add -f adds an ignored "
                       "file), then run ./setup.sh again and choose Verify.")
        elif nothing:
            t.list_add("ok", "Nothing to commit: the project already has these files committed")
            self.commit_state = "nothing"
            commit_done = True
        elif not paths_ok:
            if paths_problem == "unsafe file name in managed paths":
                self.last_rc = 3
                self.record_failure("commit refused", "unsafe file name in managed paths")
                t.list_add("fail", "Unsafe file name in managed paths; nothing was staged or committed")
            else:
                self.last_rc = paths_rc
                self.record_failure("commit failed: no paths to commit", "the path list could not be built")
                t.list_add("fail", "commit failed: no paths to commit (nothing was staged)")
            self.commit_state = "failed"
        else:
            commit_done = self.commit_screen(project)
        self.uncommitted = steps.uncommitted_display(effects.git_changes(project, list(MANAGED)),
                                                     effects.git_helpers(project) == [])
        if self.commit_state == "committed" and self.uncommitted:
            commit_done = False
            self.commit_state = "failed"
            self.failure_detail("The commit left files uncommitted", "Some managed files are still uncommitted. Commit "
                                "them yourself, then run ./setup.sh again and choose Verify.")
        if ignored_n > 0 and self.commit_state != "failed":
            commit_done = False
            self.last_rc = 1
            self.record_failure("installed files are ignored by .gitignore or could not be checked",
                                "check .gitignore and git status yourself, add the files with git add -f if needed, "
                                "then choose Verify")
            t.list_sub("Some files were not committed. Check .gitignore and git status yourself (git add -f adds an "
                       "ignored file), then run ./setup.sh again and choose Verify.")
        self.commit_state, note = steps.commit_outcome(self.commit_state, "", ignored_n)
        if self.commit_state == "committed":
            t.list_add("ok", "Committed the installed files (not pushed)")
        elif self.commit_state == "not_committed":
            t.list_add("skip", "Commit left to you; verification needs the files committed")
        verified = self.verify_install(draw=t.render)
        if verified:
            base = "installed and verified"
        elif self.verify_limited:
            base = "installed; verify not ready on Linux (known limitation)"
        elif not commit_done:
            base = "installed, not yet verified"
        else:
            base = "installed but verification failed"
        if self.commit_state == "failed" or (not verified and not self.verify_limited and commit_done):
            self.mark_step(4, "fail", f"{base}; {note}")
            self.failed_any = True
        else:
            self.mark_step(4, "ok", f"{base}; {note}")
            self.remember_project(project)
        t.menu(0, ["Continue"])
        return True

    def commit_candidates(self, applied) -> tuple[list[str], int]:
        """setup.sh's `pj paths`, run as a process as there (the tests' path-helper fault stands in for
        it): the paths that may be committed, and the helper's exit status (3: an unsafe name)."""
        apply_file, plan_file = self.logdir / "apply.json", self.logdir / "plan-paths.nul"
        apply_file.write_text(json.dumps(applied if isinstance(applied, dict) else {}))
        plan_file.write_bytes(b"".join(rel.encode("utf-8") + b"\0" for rel in self.planned))
        done = self.t.call(self.python_argv(str(CLONE / "scripts" / "setup-wizard.py"), "--helper", "paths",
                                            str(apply_file), self.project, str(plan_file), self.git or "git"))
        if done.returncode != 0:
            return [], done.returncode
        return [item.decode("utf-8", "surrogateescape") for item in done.stdout.split(b"\0") if item], 0

    def commit_screen(self, project: str) -> bool:
        """W5.3: exactly the paths to commit, the commands to commit by hand, then Commit now."""
        t = self.t
        count = len(self.commit_paths)
        quoted = steps.shell_quote(project)
        t.body(steps.commit_body(count))
        for entry, rel in self.plan:
            if entry == "UNTRACK" and rel in self.commit_paths:
                t.list_cmd(f"git -C {quoted} --literal-pathspecs rm --cached -q -- {steps.shell_quote(rel)}   # first; "
                           "leave it out of the add below")
        t.list_cmd(f"git -C {quoted} --literal-pathspecs add -A -- <the {count} paths below>")
        t.list_cmd(f'git -C {quoted} --literal-pathspecs commit -m "{COMMIT_MSG_1}" -m "{COMMIT_MSG_2}" -- '
                   "<the same paths>")
        for line in steps.first_ten(self.commit_paths):
            t.list_sub(line)
        while True:
            choice = t.menu(0, ["Commit now (recommended)", "I will commit myself", "Show every path"])
            if choice != 2:
                break
            t.view_text("\n".join(self.commit_paths), "Paths to commit")
        t.screen_reset()
        self.begin_step(4)
        t.list_add("ok", "Packages installed")
        self.hooks_trust_line(self.hooks_trusted)
        self.commit_state = "not_committed"
        if choice != 0:
            return False
        t.render()
        self.commit_state = "failed"
        helpers = effects.git_helpers(project)
        if helpers != []:
            self.last_rc = 1
            self.failure_detail("Not committed", self.git_helpers_text(helpers) + " Commit the files yourself, then run "
                                "./setup.sh again and choose Verify.")
            return False
        # As setup.sh: the installer's git tool, under the runner, quiet (Git's output can name you).
        status = self.run("Committing", effects.owner_git_argv(self.py, "commit", project, COMMIT_MSG_1, COMMIT_MSG_2,
                                                               *self.commit_paths), quiet=True)
        state, line = steps.commit_status(status)
        if line:
            self.t.list_add("warn", line)
        if state == "committed":
            self.commit_state = "committed"
            return True
        self.failure_detail("The commit did not go through", steps.COMMIT_REFUSED_HINT)
        return False

    # Step 5: collector ----------------------------------------------------------------------

    def version_hint(self) -> None:
        why = steps.reason(self.last_reply, self.last_fixed)
        if any(word in why for word in ("authorized runtime", "newer", "other_version", "runtime_mismatch",
                                        "collector_runtime")):
            self.t.list_sub("The shared collector is authorised for a different version, so other projects on the old "
                            "version need updating (run this setup there).")

    def review_legacy_state(self) -> list[str] | None:
        """W6.2: the approval learning-register needs ([] with no old records); None when the owner
        declines or the old records cannot be checked."""
        import re
        t = self.t
        report = effects.legacy_report() or {}
        if steps.get(report, "present") != "true":
            return []
        created = steps.plain(steps.get(report, "created"))
        signer = steps.plain(steps.get(report, "signer_key"))
        current = steps.plain(steps.get(report, "current_key"))
        match = steps.get(report, "match")
        digest = steps.get(report, "signers_sha256")
        if steps.get(report, "usable") != "true" or not re.fullmatch(r"[0-9a-f]{64}|none", digest):
            t.list_add("fail", "Your old orchestrator records (~/Library/Application Support/codex-agents) cannot be "
                               "checked safely, so they are not moved.")
            t.list_sub("Move that folder out of the way yourself if it is not yours, then run setup again.")
            return None
        t.list_add("warn", "Your orchestrator records are still in the old folder ~/Library/Application "
                           f"Support/codex-agents (created {created or 'at an unknown time'}).")
        t.list_sub(f"Owner key in those records: {signer or 'none'}")
        t.list_sub(f"Your owner key file ~/.ssh/codex-agents-owner.pub: {current or 'not found'}")
        if match == "true":
            t.list_add("ok", "They match: these records name your owner key.")
        elif match == "false":
            t.list_add("warn", "They do not match: these records may not be yours. Choose No unless you know why.")
        else:
            t.list_add("info", "Setup cannot compare them; check the owner key above yourself.")
        t.body("Move these records to the protected folder? [Y/n]")
        choice = t.menu(0, ["Yes, move them", "No, leave them where they are"])
        t.screen_reset()
        self.begin_step(5)
        if choice != 0:
            t.list_add("skip", "The old records were not moved, so the collector was not registered.")
            return None
        return ["--move-legacy-state", digest]

    def approve_ledger_upgrade(self) -> bool:
        t = self.t
        t.list_add("warn", "Your orchestrator records were written by an earlier version of the orchestrator.")
        t.list_sub("Setup can upgrade them now. It first keeps a checked copy of everything as it is, in the same "
                   "protected folder.")
        t.list_sub("Every project on this machine shares these records. After the upgrade the other projects stop "
                   "working with the orchestrator until you run this setup for each of them.")
        t.list_sub("Do this while no child agent is running in any project.")
        t.body("Upgrade the records now? [Y/n]")
        choice = t.menu(0, ["Yes, upgrade them", "No, leave them as they are"])
        t.screen_reset()
        self.begin_step(5)
        if choice != 0:
            t.list_add("skip", "The records were not upgraded, so the collector was not registered. Nothing was "
                               "changed.")
            t.list_sub("This project cannot use the orchestrator until you upgrade them (run setup again) or install "
                       "the earlier version here again.")
            return False
        return True

    def wait_collector_ready(self, draw=None) -> bool:
        """W6.6: a collector that is still starting is asked again, with the time shown."""
        import time
        started = time.monotonic()
        label = "Checking collector status"
        while True:
            self.run_owner_cli(label, "learning-status", "--project-root", self.project, draw=draw)
            draw = None
            if steps.get(self.last_reply, "ready") == "true":
                return True
            if steps.get(self.last_reply, "reason_code") not in ("status_collector_transport_unavailable",
                                                                  "status_account_unavailable"):
                return False
            waited = int(time.monotonic() - started)
            if waited >= self.collector_ready_seconds:
                return False
            label = f"Waiting for the collector to start ({waited}s)"
            time.sleep(1)

    def step_collector(self) -> None:
        t = self.t
        self.begin_step(5)
        if not os.path.isfile(os.path.join(self.project, ".codex", "orchestration", "cli.py")):
            t.list_add("skip", "The project CLI is not installed, so the collector cannot be registered.")
            self.mark_step(5, "skip", "no installed CLI")
            t.pause()
            return
        t.body("The learning collector is a small local service that records run outcomes on this machine only. One "
               "collector is shared by every project on this machine. This step registers this project with it and "
               "starts it.")
        t.body("Command: this setup's own orchestrator, learning-register --project-root <project>, then "
               "learning-status.")
        choice = t.menu(0, ["Register now", "Skip this step", "Quit"])
        if choice == 1:
            self.mark_step(5, "skip", "skipped")
            return
        if choice == 2:
            raise Stop(0, "Stopped.")
        t.screen_reset()
        self.begin_step(5)
        if not self.runtime_matches():
            self.runtime_refusal()
            self.mark_step(5, "fail", "orchestrator differs from this setup")
            t.pause()
            return
        legacy = self.review_legacy_state()
        if legacy is None:
            self.mark_step(5, "skip", "old records not moved")
            t.pause()
            return
        status = self.run_owner_cli("Registering the collector", "learning-register", "--project-root", self.project,
                                    *legacy, draw=self.t.render)
        moved = steps.get(self.last_reply, "state_moved")
        if status != 0 and steps.get(self.last_reply, "reason_code") == "ledger_upgrade_required":
            if not self.approve_ledger_upgrade():
                self.mark_step(5, "skip", "records not upgraded")
                t.pause()
                return
            status = self.run_owner_cli("Upgrading your records and registering the collector", "learning-register",
                                        "--project-root", self.project, "--upgrade-ledger", *legacy, draw=self.t.render)
            copy = steps.get(self.last_reply, "state_moved")
            if copy.startswith("/"):
                moved = copy
        # setup.sh judges registration by its exit status, not by a field of the reply.
        reply = {**(self.last_reply if isinstance(self.last_reply, dict) else {}), "ready": status == 0}
        _, lines = steps.registration_outcome(reply, moved if moved.startswith("/") else None)
        if status != 0:
            self.failure_detail("Collector registration failed")
            self.version_hint()
            for line in lines[1:]:
                t.list_sub(line)
            self.mark_step(5, "fail", "registration failed")
            t.pause()
            return
        t.list_add("ok", lines[0])
        for line in lines[1:]:
            t.list_add("info", line)
        if self.wait_collector_ready(draw=t.render):
            version = steps.get(self.last_reply, "collector_runtime_version")
            t.list_add("ok", f"Collector status: ready (runtime {version})")
            self.mark_step(5, "ok", "registered, ready")
        else:
            self.failure_detail("Collector status: not ready")
            self.version_hint()
            self.mark_step(5, "fail", "not ready")
        t.pause()

    # Step 6: billing and the owner key ------------------------------------------------------

    def owner_key_notice(self) -> None:
        line = steps.owner_key_notice(effects.owner_key_report())
        if line == "mismatch":
            self.t.list_add("warn", "The owner key file does not match its record, so nothing can be signed or run.")
            self.t.list_sub("If you did not change it, a program may have. Choose Replace a lost owner key.")
        elif line:
            self.t.list_add("info", f"Owner key last replaced: {line}. If that was not you, replace it again.")

    def no_cli(self, step: int, text: str) -> bool:
        if os.path.isfile(os.path.join(self.project, ".codex", "orchestration", "cli.py")):
            return False
        self.t.list_add("skip", text)
        self.mark_step(step, "skip", "no installed CLI")
        self.t.pause()
        return True

    def runtime_refused(self, step: int) -> bool:
        if self.runtime_matches():
            return False
        self.t.screen_reset()
        self.begin_step(step)
        self.runtime_refusal()
        self.mark_step(step, "fail", "orchestrator differs from this setup")
        self.t.pause()
        return True

    def step_billing(self) -> None:
        t = self.t
        self.begin_step(6)
        if self.no_cli(6, "The project CLI is not installed, so attestation is not possible yet."):
            return
        t.body("No child agent runs until you confirm that each account cannot be charged beyond its subscription. "
               "This is a statement about your own billing settings, which only you can check.")
        t.body("Only you can sign it: attest.py asks for your owner key's passphrase, and a passphrase is what stops a "
               "program, including an AI agent, from signing in your name. The next screens belong to attest.py; this "
               "wizard will not touch them.")
        if self.mode == "full":
            choice = t.menu(0, ["Start the attestation", "Skip for now", "Quit"])
            if choice == 1:
                self.mark_step(6, "skip", "not attested yet")
                return
        else:
            choice = t.menu(0, ["Start the attestation", "Back to the main menu", "Quit"])
            if choice == 1:
                self.go_back = True
                return
        if choice == 2:
            raise Stop(0, "Stopped.")
        if self.runtime_refused(6):
            return
        while True:
            status = self.handoff(self.python_argv(str(ATTEST), "--target", self.project))
            t.screen_reset()
            self.begin_step(6)
            if status == 0:
                t.list_add("ok", "attest.py ran. Check its summary for which providers it listed as attested.")
                self.mark_step(6, "ok", "attestation ran; check which providers were attested")
                break
            t.list_add("fail", f"attest.py stopped with exit code {status} (see its message)")
            self.mark_step(6, "fail", "attestation incomplete")
            retry = self.retry_menu()
            if retry == "try":
                t.screen_reset()
                self.begin_step(6)
                continue
            if retry == "back":
                self.go_back = True
            return
        t.pause()

    def step_replace_key(self) -> None:
        t = self.t
        self.begin_step(6)
        if self.no_cli(6, "The project CLI is not installed, so the key cannot be replaced from here."):
            return
        t.body("Use this only if you can no longer unlock your owner key. It creates a new key with a new passphrase. "
               "Billing statements signed with the old key stop counting in every project, so each project needs "
               "signing again before its children run. This project is signed straight after.")
        choice = t.menu(0, ["Replace the owner key", "Back to the main menu", "Quit"])
        if choice == 1:
            self.go_back = True
            return
        if choice == 2:
            raise Stop(0, "Stopped.")
        if self.runtime_refused(6):
            return
        status = self.handoff(self.python_argv(str(ATTEST), "--target", self.project, "--replace-owner-key"))
        t.screen_reset()
        self.begin_step(6)
        if status == 0:
            t.list_add("ok", "The owner key was replaced and this project was signed.")
            self.mark_step(6, "ok", "owner key replaced; project signed")
        else:
            t.list_add("fail", f"This did not finish cleanly (exit code {status}, see its message)")
            t.list_sub("If the key was replaced, choose Re-run the billing attestation only. If not, choose this option "
                       "again.")
            self.mark_step(6, "fail", "owner key replacement incomplete")
        t.pause()

    def step_passphrase(self) -> None:
        t = self.t
        self.begin_step(6)
        t.body("This changes the passphrase on your owner key, the key you sign the billing statement with. You will be "
               "asked for the current passphrase, then a new one twice. You type them straight into ssh-keygen; this "
               "wizard never sees them.")
        choice = t.menu(0, ["Change the passphrase", "Back to the main menu", "Quit"])
        if choice == 1:
            self.go_back = True
            return
        if choice == 2:
            raise Stop(0, "Stopped.")
        while True:
            status = self.handoff(self.python_argv(str(ATTEST), "--change-passphrase"))
            t.screen_reset()
            self.begin_step(6)
            if status == 0:
                t.list_add("ok", "The owner key passphrase was changed.")
                self.mark_step(6, "ok", "owner key passphrase changed")
                break
            t.list_add("fail", f"The passphrase change did not finish cleanly (exit code {status}, see its message)")
            t.list_sub("If ssh-keygen got as far as the new passphrase, the key may already have changed. Try again and "
                       "choose a non-empty passphrase.")
            self.mark_step(6, "fail", "passphrase change incomplete; key may have changed")
            if self.retry_menu() != "try":
                self.go_back = True
                return
            t.screen_reset()
            self.begin_step(6)
        t.pause()

    # Step 7: the Codex orchestrator profile -------------------------------------------------

    def codex_profile_path(self) -> str:
        return os.path.join(os.environ.get("CODEX_HOME") or os.path.join(self.home, ".codex"),
                            "orchestrator.config.toml")

    def step_profile(self) -> None:
        t = self.t
        path = self.codex_profile_path()
        folder = os.path.dirname(path)
        self.begin_step(7)
        if not self.profile_wanted:
            t.list_add("skip", "Not chosen in step 3, so no profile is needed.")
            self.mark_step(7, "skip", "not chosen")
            t.pause()
            return
        t.body("Codex runs commands without network access by default, so a Codex session cannot reach Claude. The "
               "orchestrator profile turns network on for sessions you start with: codex -p orchestrator. Ordinary "
               "sessions keep network off. Your main config.toml is never edited.")
        t.body(f"File: {path} (mode 0600)\n\n{PROFILE_CONTENT}")
        if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
            t.list_add("skip", f"{path} is not a regular file; left alone.")
            self.mark_step(7, "skip", "left alone")
            t.pause()
            return
        if os.path.isfile(path):
            existing = effects.read_owned_file(path)
            if existing is not None and existing.decode("utf-8", "replace").rstrip("\n") == PROFILE_CONTENT:
                t.list_add("ok", "The profile already exists with this content.")
                self.profile_ready = True
                self.mark_step(7, "ok", "already present")
                t.pause()
                return
            difference = steps.profile_difference(existing)
            if difference == "same":
                t.list_add("ok", "Profile: exists with the same settings")
                self.profile_ready = True
                self.mark_step(7, "ok", "exists with the same settings")
            elif difference == "sandbox_mode":
                t.list_add("warn", 'The profile exists, but sandbox_mode is not "workspace-write". It is left unchanged '
                                   "and its contents are not shown.")
                self.mark_step(7, "skip", "sandbox_mode differs, left alone")
            elif difference == "network_access":
                t.list_add("warn", "The profile exists, but network_access is off. It is left unchanged and its contents "
                                   "are not shown.")
                self.mark_step(7, "skip", "network_access is off, left alone")
            else:
                t.list_add("warn", "The profile exists and differs from the proposed content. It is left unchanged and "
                                   "its contents are not shown.")
                self.mark_step(7, "skip", "exists, differs, left alone")
            t.pause()
            return
        choice = t.menu(0, ["Create the profile", "Skip", "Quit"])
        if choice == 1:
            self.mark_step(7, "skip", "skipped")
            return
        if choice == 2:
            raise Stop(0, "Stopped.")
        t.screen_reset()
        self.begin_step(7)
        if effects.create_profile(folder, PROFILE_CONTENT):
            t.list_add("ok", f"Created {path} (mode 0600)")
            self.profile_ready = True
            self.mark_step(7, "ok", "created")
        else:
            t.list_add("fail", f"Could not create {path}")
            self.mark_step(7, "fail", "not created")
        t.pause()

    # Step 8: the Codex commands rule --------------------------------------------------------

    def temp_project_refusal(self) -> None:
        self.temp_project = True
        self.t.list_add("fail", "This project is in a temporary folder, which Codex sessions can change, so its commands "
                                "can never safely run outside the sandbox.")
        self.t.list_sub("Move the project out of the temporary folder, for example into your home folder, and run setup "
                        "again. Nothing was written.")

    def check_rule(self, draw=None) -> None:
        import re
        self.run_owner_cli("Checking the Codex commands rule", "host-command", "--project-root", self.project,
                           draw=draw)
        self.rule_status = steps.rule_status(steps.get(self.last_reply, "codex_rule"))
        self.rule_file = steps.get(self.last_reply, "rule_file")
        self.rule_copy = steps.get(self.last_reply, "runtime_copy")
        self.rule_reason = re.sub(r"[\x00-\x1f\x7f]", " ", steps.get(self.last_reply, "reason"))

    def unsupported_home(self) -> None:
        lines = steps.unsupported_home(self.rule_reason)
        self.t.list_add("fail", lines[0])
        for line in lines[1:]:
            self.t.list_sub(line)
        self.record_failure("The Codex commands rule cannot be used with this Codex setup",
                            "use the real ~/.codex folder (unset CODEX_HOME, no symlink), then run setup again")

    def commands_screen(self) -> None:
        self.t.screen_reset()
        self.begin_step(8)
        lines = steps.commands_body(self.rule_file, self.rule_status)
        for line in lines[:2]:
            self.t.body(line)
        for line in lines[2:]:
            self.t.list_add("warn", line)

    def explain_rule_refusal(self) -> None:
        text = steps.raw(self.last_reply)
        if any(word in text for word in ("symlink", "not a regular file", "not a directory")):
            message = ("The Codex rules folder or the rule file is a symlink or not a regular file, so it was left alone. "
                       "Replace it with a real folder or file, then run setup again.")
        elif "another user" in text:
            message = "The Codex rules folder belongs to another user, so it was left alone."
        elif "cannot hold" in text:
            message = ("The project or Python path contains a line break or another control character, which a Codex "
                       "rule cannot hold. Move the project to a plain path.")
        else:
            message = "The rule could not be written. Check that your Codex folder is writable, then run setup again."
        self.failure_detail("The Codex commands rule was not written", message)

    def step_commands(self) -> None:
        t = self.t
        self.begin_step(8)
        if not self.codex_primary:
            self.mark_step(8, "skip", "Codex sessions do not orchestrate this project")
            return
        if self.no_cli(8, "The project CLI is not installed, so there is nothing to allow yet."):
            return
        if effects.project_in_temp(self.project):
            self.temp_project_refusal()
            self.mark_step(8, "fail", "project in a temporary folder")
            t.pause()
            return
        if not self.runtime_matches():
            self.runtime_refusal()
            self.mark_step(8, "fail", "orchestrator differs from this setup")
            t.pause()
            return
        self.check_rule(draw=t.render)
        if self.rule_status == "unsupported":
            t.screen_reset()
            self.begin_step(8)
            self.unsupported_home()
            self.mark_step(8, "fail", "Codex home unsupported")
            t.pause()
            return
        if not self.rule_status:
            if "python" in steps.raw(self.last_reply):
                text = ("This Python cannot be trusted to run outside Codex's sandbox: it is a virtual environment's, "
                        "sits in the project or a temporary folder, or other users can change it. Run setup with a "
                        "system Python, such as Homebrew's or python.org's.")
            else:
                text = ("The installed orchestrator may be older than this step. Update the install (step 4), then run "
                        "setup again.")
            t.screen_reset()
            self.begin_step(8)
            self.failure_detail("Could not check the Codex commands rule", text)
            self.mark_step(8, "fail", "not checked")
            t.pause()
            return
        self.commands_screen()
        if self.rule_status != "missing":
            self.rule_exists = True
        if self.rule_status == "installed":
            t.list_add("ok", "The rule already exists for this project with this content.")
            self.rule_ready = True
            self.mark_step(8, "ok", "already present")
            t.pause()
            return
        stale = self.rule_status == "stale"
        while True:
            if stale:
                choice = t.menu(0, ["Leave it unchanged", "Replace it", "Show the new rule", "Quit"])
            else:
                choice = t.menu(0, ["No, not now", "Yes, allow these commands", "Show the exact rule", "Quit"])
            if choice == 0:
                self.mark_step(8, "skip", "exists, differs, left alone" if stale else "not created")
                return
            if choice == 1:
                break
            if choice == 3:
                raise Stop(0, "Stopped.")
            self.run_owner_cli("Preparing the rule", "install-codex-rule", "--project-root", self.project, "--print")
            text = steps.get(self.last_reply, "rule_text")
            if text:
                t.view_text(text, "Codex commands rule")
            self.commands_screen()
        t.screen_reset()
        self.begin_step(8)
        self.run_owner_cli("Writing the Codex commands rule", "install-codex-rule", "--project-root", self.project,
                           *(["--replace"] if stale else []), draw=self.t.render)
        outcome = steps.get(self.last_reply, "codex_rule")
        t.screen_reset()
        self.begin_step(8)
        if outcome in ("created", "replaced", "unchanged"):
            t.list_add("ok", f"Rule {outcome}: {self.rule_file} (mode 0600)")
            t.list_sub("Start a new Codex session to use it.")
            self.rule_ready = self.rule_exists = True
            self.rule_status = "installed"
            self.mark_step(8, "ok", outcome)
        elif outcome == "differs":
            self.rule_exists = True
            self.rule_status = "stale"
            self.failure_detail("The Codex commands rule was not written", "Another rule appeared for this project while "
                                "setup ran. Run setup again to review it.")
            self.mark_step(8, "fail", "not written")
        else:
            self.explain_rule_refusal()
            self.mark_step(8, "fail", "not written")
        t.pause()

    def verify_rule(self) -> None:
        """W8.5: verify mode only reports the Codex commands rule; it never writes it."""
        status, routing = effects.read_routing(os.path.join(self.project, ".codex", "orchestration",
                                                            "provider-routing.json"))
        if status == "refused" or not steps.codex_primary(routing):
            self.mark_step(8, "skip", "Codex sessions do not orchestrate this project")
            return
        self.codex_primary = True
        if not os.path.isfile(os.path.join(self.project, ".codex", "orchestration", "cli.py")):
            self.mark_step(8, "skip", "no installed CLI")
            return
        if effects.project_in_temp(self.project):
            self.temp_project_refusal()
            self.mark_step(8, "fail", "project in a temporary folder")
            return
        saved = self.t.step
        if not self.runtime_matches():
            self.t.step = 8  # File the failure under step 8, not the verify screen's step 4.
            self.runtime_refusal()
            self.t.step = saved
            self.mark_step(8, "fail", "orchestrator differs from this setup")
            return
        self.check_rule()
        if self.rule_status in ("installed", "stale"):
            self.rule_exists = True
        if self.rule_status == "installed":
            self.rule_ready = True
        mark, note, lines = steps.verify_rule(self.rule_status, self.rule_reason)
        self.t.list_add({"ok": "ok", "skip": "warn"}.get(mark, "fail"), lines[0])
        for line in lines[1:]:
            self.t.list_sub(line)
        if mark == "fail":
            if self.last_rc == 0:
                self.last_rc = 1
            self.t.step = 8
            if self.rule_status == "unsupported":
                self.record_failure(lines[0], "use the real ~/.codex folder (unset CODEX_HOME, no symlink), then run "
                                    "setup again")
            else:
                self.record_failure(lines[0], "update the install, then verify again")
            self.t.step = saved
        self.mark_step(8, mark, note)

    # Verify only, the summary and the main loop ---------------------------------------------

    def step_verify_only(self) -> None:
        while True:
            self.begin_step(4)
            # Each attempt replaces the last one's failures, so a saved summary shows only what is left.
            self.failed_any = False
            self.failures = []
            if self.verify_install(draw=self.t.render):
                self.mark_step(4, "ok", "verified")
            elif self.verify_limited:
                self.mark_step(4, "ok", "not ready on Linux (known limitation)")
            else:
                self.mark_step(4, "fail", "verification failed")
                self.failed_any = True
                if self.retry_menu() == "try":
                    continue
                self.go_back = True
                return
            break
        self.verify_rule()
        self.t.pause()

    def skip_rest(self, reason: str) -> None:
        for number in (5, 6, 7, 8):
            self.mark_step(number, "skip", reason)

    def summary_screen(self) -> None:
        t = self.t
        self.begin_step(9)
        self.mark_step(9, "ok", "finished")
        for number, label in enumerate(t.names, start=1):
            state = t.stat.get(number, "")
            if state in ("ok", "fail", "skip"):
                t.list_add(state, f"{number}. {label}: {self.note.get(number, '')}")
            else:
                t.list_add("skip", f"{number}. {label}: skipped")
            if number == 4:
                for line in steps.left_uncommitted(self.uncommitted, self.commit_state):
                    t.list_sub(line)
        network = steps.profile_network_on(effects.read_owned_file(self.codex_profile_path()))
        command, why = steps.codex_hint(self.profile_wanted, self.profile_ready, network)
        t.list_plain("")
        t.list_heading("How to use it")
        t.list_plain(" - In Claude Code, open the project and use the agent-orchestration skill.")
        t.list_plain(f" - In Codex, start a session in the project's root folder with: {command}")
        t.list_sub(why)
        if self.codex_primary and not self.rule_ready:
            if self.temp_project:
                t.list_sub("This project is in a temporary folder, so Codex sessions cannot orchestrate it. Move it and "
                           "run ./setup.sh again.")
            elif self.rule_status == "unsupported":
                t.list_sub("The Codex commands rule works only in the real ~/.codex folder (not a CODEX_HOME elsewhere or "
                           "a symlinked ~/.codex), so Codex sessions cannot orchestrate this project yet.")
            elif self.rule_status == "stale":
                t.list_sub("The Codex commands rule for this project is stale: it differs from what setup would write now "
                           "(often after a Python upgrade), so Codex sessions may not reach the orchestrator. Run "
                           "./setup.sh again and replace it in step 8.")
            else:
                t.list_sub("Codex sessions cannot reach the orchestrator until the Codex commands rule exists. Run "
                           "./setup.sh again and allow it in step 8.")
        t.list_plain(" - To remove everything, including the Codex commands rule, run:")
        t.list_cmd(f"python3 -I -B -S -X pycache_prefix=/dev/null {steps.shell_quote(str(INSTALLER))} remove --target "
                   f"{steps.shell_quote(self.project)} --apply")
        if self.rule_exists and self.rule_file:
            t.list_plain(" - The Codex commands rule and its protected copy of the orchestrator are outside the project. "
                         "The remove command above deletes both; to undo only the rule, delete:")
            t.list_cmd(steps.shell_quote(self.rule_file))
            if self.rule_copy:
                t.list_plain("   and its copy:")
                t.list_cmd(steps.shell_quote(self.rule_copy))

    def save_summary(self) -> None:
        """W10.1-W10.4: a new owner-only file, by default in the home folder, never command output."""
        t = self.t
        destination, _ = t.ask_text("Save the summary to (new file)",
                                    f"{self.home}/agent-orchestration-setup-summary.txt", home=self.home)
        t.screen_reset()
        self.begin_step(9)
        refusal = steps.summary_destination_refusal(destination)
        if refusal:
            t.list_add("fail", refusal)
            t.menu(0, ["Continue"])
            return
        folder = os.path.dirname(destination) or "."
        if self.git and self.g(folder, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true":
            t.list_add("warn", "That folder is inside a Git working tree, so the summary could be committed by accident.")
            if t.menu(0, ["Save it somewhere else", "Save it here anyway"]) == 0:
                self.save_summary()
                return
            t.screen_reset()
            self.begin_step(9)
        if os.path.lexists(destination):
            t.list_add("fail", f"{destination} already exists; nothing written")
        else:
            try:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(steps.summary_text(self.failures))
                t.list_add("ok", "Saved a summary of what failed (step names, labels, exit codes and fixed hints only)")
            except OSError:
                t.list_add("fail", "Could not write the summary")
        t.menu(0, ["Continue"])

    def step_done(self) -> None:
        while True:
            self.summary_screen()
            if self.failed_any and self.failures:
                choice = self.t.menu(0, ["Exit", "Save a summary of what failed", "Back to the main menu"])
                if choice == 1:
                    self.save_summary()
                    continue
                if choice == 2:
                    self.go_back = True
            elif self.t.menu(0, ["Exit", "Back to the main menu"]) == 1:
                self.go_back = True
            break
        self.t.show_cursor()
        self.t.write("\n")

    def init_logdir(self) -> None:
        """W1.7: the private working folder, never in /tmp or $TMPDIR."""
        done = self.owner_tool("scratch", "setup-")
        folder = done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
        try:
            if not folder.startswith("/") or os.path.islink(folder) or not os.path.isdir(folder):
                raise OSError("no private working folder")
            self.logdir = Path(folder)
            (self.logdir / "inputs").mkdir(mode=0o700)
        except OSError as exc:
            self.logdir = None
            raise Stop(1, "Could not create a private working folder for setup, so nothing was changed.\nOn macOS it "
                          "lives in your user cache folder; on Linux setup needs XDG_RUNTIME_DIR, which a normal login "
                          "session sets.", stderr=True) from exc

    def main(self) -> None:
        start = self.start_dir
        # Started from this repository itself (the usual way): there is no project to suggest yet.
        if _resolve_dir(start) == _resolve_dir(str(CLONE)):
            start = ""
        self.t.term_size()
        self.step_checks()
        self.init_logdir()
        while True:
            self.go_back = False
            self.reset_attempt()
            self.answer_raw = ""
            self.hooks_trusted = False
            self.step_project(start)
            start = self.project
            if self.mode == "full":
                if not self.step_providers():
                    self.mark_step(4, "skip", self.fail_reason)
                    self.skip_rest(self.fail_reason)
                elif not self.step_install():
                    self.skip_rest(self.fail_reason)
                else:
                    self.step_collector()
                    self.step_billing()
                    if not self.go_back:
                        self.step_profile()
                        self.step_commands()
            elif self.mode == "verify":
                self.step_verify_only()
            elif self.mode == "attest":
                self.step_billing()
            elif self.mode == "passphrase":
                self.step_passphrase()
            elif self.mode == "replace":
                self.step_replace_key()
            if self.go_back:
                continue
            self.step_done()
            if not self.go_back:
                break

    def cleanup(self) -> None:
        import signal
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # A second signal cannot cut it short.
            signal.signal(number, signal.SIG_IGN)
        self.t.stop_active()
        self.t.show_cursor()
        self.t.write(self.t.c["reset"])
        if self.logdir is not None:
            shutil.rmtree(self.logdir, ignore_errors=True)
            if self.logdir.exists():
                print("Warning: a private working folder could not be removed; delete it yourself:", self.logdir,
                      file=sys.stderr)
