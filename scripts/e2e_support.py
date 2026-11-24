"""The shared harness of the lifecycle end-to-end test (PLAN-0.4.0.md T7; scripts/e2e_lifecycle.py).

One `World` is a complete, isolated machine for one run:

- a scratch folder with its own HOME, XDG state, Codex home and ~/.ssh, a fake project (a Git
  repository holding a committed fixture and its operational checks), fake Codex and Claude
  clients, and an owner key with a passphrase made for the run;
- the product's own programs run unchanged (`./setup.sh`, the orchestrator CLI) through a Python
  stand-in in the scratch folder that answers "the account's home" with the scratch HOME and finds
  the fake clients only, so the real home, the real clients, the real collector and the real
  accounts are never read, written or started.

Why the scratch folder is under ~/.cache and not in a temporary folder: the runtime refuses any
program, client or Python that lies in a temporary folder or outside a hidden folder of the
account's home (scripts/attest.py and codex_agents/host_paths.py; tests/test_setup_sh.py keeps its
fakes in ~/.cache/agent-orch-tests for the same reason). Nothing in it is read from the real
home, and the whole folder is removed at the end, after every process started inside it is stopped.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import pty
import re
import secrets
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
PROVIDER_FIXTURES = ROOT / "packages" / "codex-agents" / "tests" / "fixtures" / "providers"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B|\x1b[=>]")
SETUP_TIMEOUT_SECONDS = 150

# The reasons a step may never answer with by themselves (G1): each names a missing thing, not a cause.
BARE_UNAVAILABLE = ("run_unavailable", "account_unavailable", "probe_unavailable")

# What the stand-in Python does before it runs the product's own script, `-c` code or `-m` module.
BOOT = '''\
import os, sys, pwd, runpy, shutil
HOME = %HOME%
_account = pwd.getpwuid
def getpwuid(uid):
    r = _account(uid)
    return pwd.struct_passwd((r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, HOME, r.pw_shell))
pwd.getpwuid = getpwuid
_which = shutil.which
def which(cmd, mode=os.F_OK | os.X_OK, path=None):
    if cmd in ("codex", "claude"):  # the fake clients only, never one the machine has
        path = HOME + "/.local/bin"
    return _which(cmd, mode, path)
shutil.which = which
sys.executable = %SHIM%
# T7 S1 (test only, decision 128): source rewrites listed in HOME/.e2e-bin/patches.json, applied to the named
# product module as it is imported (a scaled liveness limit, an injected fault). One that does not apply raises.
import importlib.machinery, json as _json
_patch_file = HOME + "/.e2e-bin/patches.json"
if os.path.exists(_patch_file):
    _patches = _json.load(open(_patch_file))
    class _Patched(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = self.get_data(self.path).decode()
            for item in _patches:
                if item["module"] == fullname:
                    if source.count(item["old"]) != 1:
                        raise ImportError("e2e patch does not apply: " + fullname)
                    source = source.replace(item["old"], item["new"])
            return compile(source, self.path, "exec")
    class _Finder:
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if not any(item["module"] == name for item in _patches):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = _Patched(spec.loader.name, spec.loader.path)
            return spec
    sys.meta_path.insert(0, _Finder)
args = sys.argv[1:]
if args[0] == "-c":
    code = args[1]
    sys.argv = ["-c"] + args[2:]
    exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})
elif args[0] == "-m":
    sys.argv = [args[1]] + args[2:]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
'''

SHIM = '''\
#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -I|-B|-S|-E|-s) shift;;
    -X) shift 2;;
    *) break;;
  esac
done
exec "%PYTHON%" -I -B -S -X pycache_prefix=/dev/null "%BOOT%" "$@"
'''

# A fake client: `fake_child.py` of the repository's own provider fakes (packages/codex-agents/tests/
# learning_support.py), launched from the client name it was started by.
CLIENT = '#!/bin/sh\nexec "%PYTHON%" "${0%/*}/fake_child.py" "${0%/*}" "${0##*/}" "$@"\n'


# Every private value any World of this process has held, so the report can be scanned for them (G8).
PRIVATE_VALUES: set[str] = set()


# G1 and G8, judged as each output is captured, in every World of the process (a World is one scene's machine;
# a check that only reads one World would leave the others unjudged). Each entry is a fixed reason code; the
# runner fails the check during which it was captured (scripts/e2e_lifecycle.py, _execute).
CAPTURE_BREAKS: list[str] = []
PRIVATE_KEY_BLOCK = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]*PRIVATE KEY-----")
# Owner decision 36: an owner's own screen may show the key tool's fingerprint line and file paths, never the
# passphrase or key material; a saved or AI-read output carries no private value at all.
SCREEN_SECRETS = ("passphrase", "owner_key_material", "owner_private_key")


def capture_breaks(text: str, *, kind: str, private: dict, code: int, refuses: bool | str, timed_out: bool) -> list[str]:
    """The rules one captured step breaks: `kind` is "output" (saved or AI-read) or "screen" (an owner's terminal).
    `private` maps a value's name to the value (World.private_values); only names reach the result."""
    found = set()
    if timed_out:
        found.add("g1_step_timed_out")
    elif code != 0 and not refuses:
        found.add("g1_unexpected_exit")
    elif code != 0 and isinstance(refuses, str) and refuses not in text:
        found.add("g1_refusal_reason_unexpected")  # decision 191: an expected refusal names its expected reason
    found.update("g1_bare_" + reason for reason in bare_unavailable(text))
    found.update(f"g8_{kind}_leaks_" + re.sub(r"(_[0-9]+)+$", "", name) for name, value in private.items()
                 if value and value in text and (kind == "output" or name.startswith(SCREEN_SECRETS)))
    if PRIVATE_KEY_BLOCK.search(text):
        found.add(f"g8_{kind}_leaks_private_key_material")
    return sorted(found)


def child_environment(environ) -> dict:
    """The environment a World's processes start from: never a harness variable (E2E_*), so no switch of the test
    runner (a fault selection, for one) reaches the product or a child it starts (T7 S1 fault isolation)."""
    return {key: value for key, value in environ.items() if not key.startswith("E2E_")}


class HarnessError(Exception):
    """The harness itself could not be built (a missing tool, a refused location): the check fails."""


def strip_ansi(data: bytes | str) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return ANSI.sub("", text).replace("\r", "")


def bare_unavailable(text: str) -> list[str]:
    """G1: the bare reason codes in a step's output (one that is the whole reason, not part of another word)."""
    return sorted({code for code in BARE_UNAVAILABLE if re.search(rf"(?<![a-z0-9_]){code}(?![a-z0-9_])", text)})


def fake_child_source() -> str:
    """The repository's fake provider client, read from its own test support (no second copy)."""
    tree = ast.parse((ROOT / "packages" / "codex-agents" / "tests" / "learning_support.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "FAKE_CHILD" for t in node.targets):
            return ast.literal_eval(node.value)
    raise HarnessError("the repository's fake provider client was not found")


def claude_probe_frames(**rate_limit) -> list[dict]:
    """What the fake Claude client prints for a billing probe: the init frame, one rate-limit event (the
    favourable reading, changed by `rate_limit`, e.g. isUsingOverage=True) and a successful result.
    The shapes follow packages/codex-agents/tests/test_bridge.py (init_frame, RATE_LIMIT, result_frame)."""
    info = {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2, "isUsingOverage": False,
            "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled", **rate_limit}
    return [
        {"type": "system", "subtype": "init", "session_id": "session-1", "apiKeySource": "none",
         "model": "claude-haiku-5-5", "plugins": [], "claude_code_version": "2.1.311", "mcp_servers": [],
         "tools": []},
        {"type": "rate_limit_event", "session_id": "session-1", "uuid": "rate-1", "rate_limit_info": info},
        {"type": "result", "subtype": "success", "is_error": False, "result": "OK",
         "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0},
         "total_cost_usd": 0.42, "permission_denials": []}]


@dataclass
class Run:
    """One program run on a terminal: its exit status, whole screen text and seconds."""
    code: int
    text: str
    seconds: float
    timed_out: bool = False


@dataclass
class World:
    """An isolated machine for one run (see the module note); a context manager that removes it."""
    base: Path = field(init=False)
    home: Path = field(init=False)
    project: Path = field(init=False)
    passphrase: str = field(init=False)
    python: str = field(init=False)
    env: dict = field(init=False)
    outputs: list = field(default_factory=list)  # every saved or AI-readable output a step produced, for G8
    screens: list = field(default_factory=list)  # what an owner's terminal showed (setup, the owner's tools): G8's owner screens
    steps: list = field(default_factory=list)  # every step run: {"step": label, "code": exit status, "refuses": bool}
    known_passphrases: set = field(default_factory=set)  # every other passphrase a step typed (G8)

    def __enter__(self) -> "World":
        try:
            self._build()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.stop_processes()
        if getattr(self, "base", None) is not None:
            shutil.rmtree(self.base, ignore_errors=True)

    # Building ------------------------------------------------------------------------------------

    def _build(self) -> None:
        account = Path(os.path.realpath(os.path.expanduser("~")))
        cache = account / ".cache"
        cache.mkdir(mode=0o700, exist_ok=True)
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="e2e-", dir=cache)))
        self.base.chmod(0o700)
        self.home = self.base / "h"  # short: a socket path may not pass 100 bytes (service_host.MAX_SOCKET_PATH_BYTES)
        self.home.mkdir()
        for folder in (".ssh", ".codex", ".local", ".local/bin", ".local/state", ".e2e-bin"):
            (self.home / folder).mkdir(mode=0o700)
        (self.base / "tmp").mkdir()
        printed = subprocess.run(["/bin/bash", str(SETUP), "--print-python"], capture_output=True, text=True,
                                 check=False, stdin=subprocess.DEVNULL, env=child_environment(os.environ)).stdout.strip()
        if not printed.startswith("/"):
            raise HarnessError("no trusted python3 (setup.sh --print-python refused)")
        self.python = printed
        self._stand_in_python()
        self._fake_clients()
        self.passphrase = secrets.token_urlsafe(18)
        self.env = self._environment()
        self._fake_project()

    def _stand_in_python(self) -> None:
        folder = self.home / ".e2e-bin"
        shim = folder / "python3"
        (folder / "boot.py").write_text(BOOT.replace("%HOME%", repr(str(self.home))).replace("%SHIM%", repr(str(shim))))
        shim.write_text(SHIM.replace("%PYTHON%", self.python).replace("%BOOT%", str(folder / "boot.py")))
        shim.chmod(0o755)
        self.shim = shim

    def _fake_clients(self) -> None:
        tools = self.home / ".local" / "bin"
        (tools / "fake_child.py").write_text(fake_child_source())
        (tools / "codex-result.json").write_bytes((PROVIDER_FIXTURES / "codex-rate-limits.json").read_bytes())
        (tools / "claude-status.json").write_bytes((PROVIDER_FIXTURES / "claude-auth-status.json").read_bytes())
        for name in ("codex", "claude"):
            (tools / name).write_text(CLIENT.replace("%PYTHON%", self.python))
            (tools / name).chmod(0o755)

    def key_values(self) -> tuple[str, str]:
        """The test owner key's fingerprint and public blob, once the product has made the key."""
        return (self.all_key_values() or [("", "")])[0]

    def all_key_values(self) -> list[tuple[str, str]]:
        """Every owner key the run has made (a replacement makes the next one): (fingerprint, public blob)."""
        found = []
        for pub in sorted((self.home / ".ssh").glob("codex-agents-owner*.pub")):
            done = subprocess.run(["/usr/bin/ssh-keygen", "-lf", str(pub)], capture_output=True, text=True,
                                  env=child_environment(os.environ))
            found.append(((done.stdout.split() or ["", ""])[1], pub.read_text().split()[1]))
        return found

    def _environment(self) -> dict:
        env = {key: value for key, value in child_environment(os.environ).items()
               if key not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NO_COLOR", "PYTHONDONTWRITEBYTECODE",
                              "CODEX_HOME", "CODEX_THREAD_ID", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
                              "XDG_DATA_HOME", "XDG_CACHE_HOME", "CLAUDECODE") and not key.startswith(("CODEX_", "CLAUDE_"))}
        env.update(
            HOME=str(self.home), TERM="xterm", CODEX_HOME=str(self.home / ".codex"),
            PATH=f"{self.home / '.e2e-bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            XDG_STATE_HOME=str(self.home / ".local" / "state"), TMPDIR=str(self.base / "tmp"),
            GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="e2e", GIT_AUTHOR_EMAIL="e2e@example.invalid",
            GIT_COMMITTER_NAME="e2e", GIT_COMMITTER_EMAIL="e2e@example.invalid")
        env.pop("XDG_RUNTIME_DIR", None)
        return env

    def git(self, *args: str, project: Path | None = None) -> subprocess.CompletedProcess:
        git = subprocess.run(["/bin/bash", str(SETUP), "--print-git"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env=child_environment(os.environ)).stdout.strip()
        if not git.startswith("/"):
            raise HarnessError("no trusted git")
        return subprocess.run([git, "-C", str(project or self.project), *args], capture_output=True, text=True,
                              env=self.env, stdin=subprocess.DEVNULL)

    def _fake_project(self) -> None:
        """A Git repository holding a committed fixture, its answer oracle and two QA wrappers (PLAN T7)."""
        self.project = self.base / "project"
        self.project.mkdir()
        self.project.chmod(0o755)
        self.git("init", "-q")
        (self.project / "fixture").mkdir()
        (self.project / "fixture" / "facts.txt").write_text("alpha 3\nbeta 5\n")
        (self.project / "fixture" / "oracle.json").write_text(json.dumps({"alpha": 3, "beta": 5}, sort_keys=True))
        wrapper = "#!/usr/bin/python3\nprint(1)\n"
        interpreter = hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest()
        entries = {}
        for kind, name in (("deterministic_checks", "tests"), ("security_checks", "security")):
            path = self.project / "qa-check"
            path.write_text(wrapper)
            path.chmod(0o700)
            entries[kind] = [{
                "check_id": name, "driver": "python-wrapper", "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 5}]
        self.checks = self.base / "operational-checks.json"
        self.checks.write_text(json.dumps({"version": 1, **entries}))
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def answers(self, **changes) -> Path:
        """The S1 answers file (docs/setup-wizard.md); `changes` replace or, with None, drop an answer."""
        data = {"project": str(self.project), "operational_checks": str(self.checks),
                "providers": {"codex": "on", "claude": "on", "claude_models": "claude-sonnet-5-5", "bridge": "off"},
                "commit": True, "commands_rule": True, "move_records": False, "upgrade_records": False}
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path = self.base / "answers.json"
        path.write_text(json.dumps(data))
        return path

    # Running -------------------------------------------------------------------------------------

    def terminal(self, argv: list[str], *, label: str = "setup", refuses: bool = False,
                 cwd: Path | None = None, timeout: float = SETUP_TIMEOUT_SECONDS,
                 passphrase: str | None = None, answers: tuple = (), record: bool = True) -> Run:
        """Run `argv` on a pseudo-terminal, as an owner at a terminal does, answering what it asks.

        The owner's three kinds of question are answered the way the test owner would: a yes or no
        statement about billing is answered yes, and a passphrase is typed. Anything else that waits is
        a failure (the run times out and is stopped). Output is collected for the G8 scan.

        `passphrase` types another passphrase than the world's; `answers` are further (pattern, reply)
        pairs tried first; `record=False` keeps a tool's output (one that names scratch paths) out of the
        G8 outputs, for a step that is not a product output."""
        started = time.monotonic()
        pid, descriptor = pty.fork()
        if pid == 0:  # the child: its own session, the terminal its controlling one
            with contextlib.suppress(Exception):
                os.chdir(cwd or ROOT)
                os.execve(argv[0], argv, self.env)
            os._exit(127)
        raw, mark, timed_out, status = b"", 0, False, None
        typed = self.passphrase if passphrase is None else passphrase
        self.known_passphrases.add(typed)
        rules = (*((re.compile(pattern), reply) for pattern, reply in answers),
                 (re.compile(r"\[y/N\]\s*$"), "y"), (re.compile(r"[Pp]assphrase[^\n]*:\s*$"), typed))
        try:
            while True:
                ready, _, _ = select.select([descriptor], [], [], 0.25)
                if ready:
                    try:
                        chunk = os.read(descriptor, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    raw += chunk
                tail = strip_ansi(raw[mark:])[-400:]
                for pattern, reply in rules:
                    if pattern.search(tail):
                        time.sleep(0.2)
                        os.write(descriptor, reply.encode() + b"\r")
                        mark = len(raw)
                        break
                if time.monotonic() - started > timeout:
                    timed_out = True
                    break
        finally:
            if timed_out:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.close(descriptor)
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                status = 0
        text = strip_ansi(raw)
        code = os.waitstatus_to_exitcode(status) if status is not None else -1
        if record:
            self.screens.append(text)
            self.steps.append({"step": label, "code": code, "refuses": bool(refuses), "timed_out": timed_out})
        self.register_private_values()
        # Every screen is judged, kept in `screens` or not (record=False only keeps a tool's screen out of the
        # list one check reads; the screen rule still holds for it).
        self._capture(text, kind="screen", code=code, refuses=refuses, timed_out=timed_out)
        return Run(code, text, round(time.monotonic() - started, 3), timed_out)

    def setup(self, answers: Path, *, refuses: bool = False) -> Run:
        """`./setup.sh --answers FILE`, from the clone, through the stand-in Python on the PATH."""
        return self.terminal(["/bin/bash", str(SETUP), "--answers", str(answers)], label="setup_answers",
                             refuses=refuses)

    def update(self) -> Run:
        """`./setup.sh --update PROJECT`: setup again on the installed project, no menu."""
        return self.terminal(["/bin/bash", str(SETUP), "--update", str(self.project)], label="setup_update")

    @property
    def installed_cli(self) -> Path:
        """The orchestrator CLI the installer put in the project: the only CLI a check may run (decision 128d).
        The source tree's own copy has no install manifest, which hid the 0.8.54 live-root defect."""
        return self.project / ".codex" / "orchestration" / "cli.py"

    def cli(self, *args: str, timeout: float = 60, refuses: bool = False, program: Path | None = None,
            environment: dict | None = None, cwd: Path | None = None) -> tuple[int, dict | None, str]:
        """An owner command of the project's installed orchestrator CLI; (exit, JSON reply, text).

        `refuses` marks a step whose refusal the check expects (G1); `program` may only name the installed CLI
        again (anything else raises); `environment` adds markers, as host_cli does."""
        program = program or self.installed_cli
        if Path(os.path.realpath(program)) != Path(os.path.realpath(self.installed_cli)):
            raise HarnessError("a check ran a CLI that is not the project's installed one (decision 128d)")
        try:
            done = subprocess.run([str(self.shim), "-I", "-B", "-S", str(program), *args,
                                   "--project-root", str(self.project)],
                                  capture_output=True, text=True, env={**self.env, **(environment or {})},
                                  stdin=subprocess.DEVNULL, timeout=timeout, cwd=str(cwd or self.base))
        except subprocess.TimeoutExpired as expired:
            # G1/G8 (S2 and S3): any caller's (a check's or the shared setup's) step that waited too long is captured
            # (partial output judged, a fixed reason) and answered as a failed step (exit -1, never an expected
            # refusal); the exception (it carries the arguments and paths) is never raised or printed.
            text = "".join(part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
                           for part in (expired.stdout, expired.stderr))
            self.outputs.append(text)
            self.steps.append({"step": args[0], "code": -1, "refuses": False, "timed_out": True})
            self._capture(text, kind="output", code=-1, refuses=False, timed_out=True)
            return -1, None, text
        text = done.stdout + done.stderr
        self.outputs.append(text)
        self.steps.append({"step": args[0], "code": done.returncode, "refuses": bool(refuses), "timed_out": False})
        self._capture(text, kind="output", code=done.returncode, refuses=refuses, timed_out=False)
        try:
            reply = json.loads(done.stdout)
        except ValueError:
            reply = None
        return done.returncode, reply if isinstance(reply, dict) else None, text

    # Host sessions and the fake Claude client's scripts ------------------------------------------

    CLAUDE_HOST = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "e2e-claude-session"}

    def host_cli(self, host: str, *args: str, refuses: bool = False, environment: dict | None = None,
                 timeout: float = 90) -> tuple[int, dict | None, str]:
        """A command as a primary host session runs it (the host's markers set; the project's own CLI).

        Only the Claude host is built here: a Codex primary's commands go through the protected copy and
        its session guard, which needs a recorded Codex session (a later group)."""
        if host != "claude":
            raise HarnessError("only a Claude primary session is built by this harness so far")
        return self.cli(*args, refuses=refuses,
                        environment={**self.CLAUDE_HOST, **(environment or {})}, cwd=self.project, timeout=timeout)

    CODEX_HOST = {"CODEX_THREAD_ID": "e2e-codex-thread"}

    def codex_host_cli(self, *args: str, refuses: bool = False, environment: dict | None = None,
                       timeout: float = 90) -> tuple[int, dict | None, str]:
        """A command as a Codex primary session runs it (its thread marker set; the project's own CLI).

        The marker is only what the product's host detection reads; the session guard and its recorded
        Codex session belong to a later group."""
        return self.cli(*args, refuses=refuses,
                        environment={**self.CODEX_HOST, **(environment or {})}, cwd=self.project, timeout=timeout)

    def validate_package(self, primary: str) -> tuple[int, dict | None, str]:
        """`validate --root PROJECT` as a primary session runs it (this command takes `--root`, not
        `--project-root`, so it cannot go through `cli`); (exit, JSON reply, text)."""
        marker = self.CLAUDE_HOST if primary == "claude" else self.CODEX_HOST
        done = subprocess.run([str(self.shim), "-I", "-B", "-S", str(self.installed_cli),
                               "validate", "--root", str(self.project)],
                              capture_output=True, text=True, env={**self.env, **marker},
                              stdin=subprocess.DEVNULL, timeout=90, cwd=str(self.project))
        text = done.stdout + done.stderr
        self.outputs.append(text)
        self.steps.append({"step": "validate", "code": done.returncode, "refuses": False, "timed_out": False})
        self._capture(text, kind="output", code=done.returncode, refuses=False, timed_out=False)
        try:
            reply = json.loads(done.stdout)
        except ValueError:
            reply = None
        return done.returncode, reply if isinstance(reply, dict) else None, text

    @property
    def tools(self) -> Path:
        return self.home / ".local" / "bin"

    def claude_frames(self, frames: list[dict]) -> None:
        """The stream the fake Claude client prints when it runs a turn (a billing probe or a child)."""
        (self.tools / "claude-frames.jsonl").write_text("".join(json.dumps(frame) + "\n" for frame in frames))

    def claude_version(self, text: str | None) -> None:
        """What `claude --version` prints (None: the client's reviewed default)."""
        (self.tools / "claude-version.txt").unlink(missing_ok=True)
        if text is not None:
            (self.tools / "claude-version.txt").write_text(text + "\n")

    def claude_signed_in(self, signed_in: bool) -> None:
        """Sign the fake Claude client out or in. A real signed-out `claude auth status --json` prints its
        state and exits 1, so the stand-in does the same while the status file says `loggedIn: false`."""
        status = json.loads((PROVIDER_FIXTURES / "claude-auth-status.json").read_text())
        status["loggedIn"] = signed_in
        (self.tools / "claude-status.json").write_text(json.dumps(status))
        script = self.tools / "claude"
        script.unlink(missing_ok=True)  # never write through a shared link
        script.write_text(
            '#!/bin/sh\nif [ "$1 $2" = "auth status" ] && grep -q \'"loggedIn": false\' "${0%/*}/claude-status.json"; '
            'then cat "${0%/*}/claude-status.json"; exit 1; fi\n' + CLIENT.split("\n", 1)[1].replace("%PYTHON%", self.python))
        script.chmod(0o755)

    def claude_account(self, email: str | None) -> None:
        """Make the fake Claude client another account (None: the fixture's own), keeping it signed in."""
        status = json.loads((PROVIDER_FIXTURES / "claude-auth-status.json").read_text())
        if email is not None:
            status["email"] = email
        (self.tools / "claude-status.json").write_text(json.dumps(status))

    # Snapshots -----------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        """What a second setup must leave alone: every file and digest, and the records by kind.

        Files: the project (except Git's own objects), the scratch HOME (Codex rule and protected copy,
        ~/.ssh, the state folder's files), each as a relative name and a SHA-256. Volatile files that only
        a running service rewrites (sockets, locks, write-ahead logs, the database files themselves) are
        named but not hashed; their content is judged through the records counted below. Git: HEAD and
        the number of commits. Records: the collector's counts by kind (learning-status), never content."""
        files = {}
        for top, label in ((self.project, "project"), (self.home, "home")):
            for folder, names, leaves in os.walk(top):
                names[:] = sorted(name for name in names if not (label == "project" and name == ".git"
                                                                  and folder == str(top)) and name != ".e2e-bin"
                                  and not (label == "home" and folder == str(self.home / ".local") and name == "bin"))
                for leaf in sorted(leaves):
                    path = Path(folder) / leaf
                    relative = f"{label}/{path.relative_to(top)}"
                    if path.is_symlink() or not path.is_file():
                        files[relative] = "-"
                    elif re.search(r"\.(sqlite3(-wal|-shm)?|lock|sock|pid)$|/rt/", relative):
                        files[relative] = "volatile"
                    else:
                        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        head = self.git("rev-parse", "HEAD").stdout.strip()
        count = self.git("rev-list", "--count", "HEAD").stdout.strip()
        dirty = self.git("status", "--porcelain").stdout
        return {"files": files, "head": head, "commits": count, "dirty": dirty, "records": self.records(),
                "ledger": self.ledger_counts()}

    def records(self) -> dict:
        """The collector's counts by kind: capacity `live` counts, ingestion counts, attestations."""
        code, status, _ = self.cli("learning-status")
        if code != 0 or status is None:
            return {"status": "unreadable"}
        counts = {}
        for group in ("account_capacity", "project_capacity"):
            for kind, value in (status.get(group) or {}).items():
                counts[f"{group}.{kind}"] = (value or {}).get("live")
        for kind, value in ((status.get("ingestion") or {}).get("hook_event_counts") or {}).items():
            counts[f"hook_events.{kind}"] = value
        for provider, value in (status.get("provider_attestations") or {}).items():
            counts[f"attestation.{provider}"] = (value or {}).get("state")
        counts["owner_key.replacements"] = (status.get("owner_key") or {}).get("replacements")
        return counts

    def ledger_counts(self) -> dict:
        """Row counts by table in the account ledger, read-only (names and numbers, never content)."""
        path = self.home / ".local" / "state" / "codex-agents" / "learning.sqlite3"
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in connection.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
            return {name: connection.execute(f'select count(*) from "{name}"').fetchone()[0] for name in tables}
        finally:
            connection.close()

    # The project's own history, read-only (a path other than the commands' replies) ----------------

    def history_records(self, record_type: str) -> list[dict]:
        """The project history's records of one kind, oldest first, as the service stored them."""
        found = sorted((self.home / ".local" / "state" / "codex-agents" / "projects").glob("*/history.sqlite3"))
        if len(found) != 1:
            raise HarnessError("the project history is not exactly one file")
        connection = sqlite3.connect(f"file:{found[0]}?mode=ro", uri=True)
        try:
            rows = connection.execute("select content from records where record_type = ? order by source_sequence",
                                      (record_type,)).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def history_qa(self) -> list[dict]:
        """The operational QA rows of the project history (the QA record each row holds), oldest first."""
        found = sorted((self.home / ".local" / "state" / "codex-agents" / "projects").glob("*/history.sqlite3"))
        if len(found) != 1:
            raise HarnessError("the project history is not exactly one file")
        connection = sqlite3.connect(f"file:{found[0]}?mode=ro", uri=True)
        try:
            rows = connection.execute("select qa from project_operational_qa order by source_sequence").fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    # T7 S3 helper block: the learning hook, a recorded Codex session and the protected copy ----------------

    SESSION_ROLLOUT = "rollout-{day}T10-00-00-{thread}.jsonl"

    def _run_captured(self, argv: list[str], label: str, *, refuses: bool = False, environment: dict | None = None,
                      cwd: Path | None = None, timeout: float = 90, stdin: str | None = None) -> tuple[int, str, str]:
        """One program of this World, judged like every step (G1, G8) even when it times out: (exit, stdout, text).

        A timeout is captured with whatever the program had printed (`timed_out`), then answers exit -1."""
        try:
            feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            done = subprocess.run(argv, capture_output=True, text=True, env={**self.env, **(environment or {})},
                                  timeout=timeout, cwd=str(cwd or self.project), **feed)
        except subprocess.TimeoutExpired as expired:
            partial = [part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
                       for part in (expired.stdout, expired.stderr)]
            text = "".join(partial)
            self.outputs.append(text)
            self.steps.append({"step": label, "code": -1, "refuses": refuses, "timed_out": True})
            self._capture(text, kind="output", code=-1, refuses=refuses, timed_out=True)
            return -1, partial[0], text
        text = done.stdout + done.stderr
        self.outputs.append(text)
        self.steps.append({"step": label, "code": done.returncode, "refuses": refuses, "timed_out": False})
        self._capture(text, kind="output", code=done.returncode, refuses=refuses, timed_out=False)
        return done.returncode, done.stdout, text

    def hook_argv(self, checkout: Path | None = None) -> list[str]:
        """The command the installed `.codex/hooks.json` runs for an event (as Codex splits it), optionally for
        another checkout: only the `--checkout` value is replaced, so the program is still the installed hook."""
        hooks = json.loads((self.project / ".codex" / "hooks.json").read_text())["hooks"]
        commands = {item["command"] for group in hooks.values() for entry in group for item in entry["hooks"]}
        if len(commands) != 1:
            raise HarnessError("the installed hooks do not all run one command")
        argv = shlex.split(commands.pop())
        if checkout is not None:
            at = argv.index("--checkout")
            argv[at + 1] = str(checkout)
        return argv

    def run_hook(self, event: dict, *, checkout: Path | None = None, cwd: Path | None = None,
                 timeout: float = 60) -> tuple[int, str, str]:
        """Deliver one hook event to the installed `learning_hook.py` as Codex does (the event on its standard
        input); (exit, stdout, text). The hook always exits 0."""
        return self._run_captured(self.hook_argv(checkout), "hook-" + str(event.get("hook_event_name")),
                                  stdin=json.dumps(event), cwd=cwd, timeout=timeout)

    def codex_session(self, workspace: Path, *, thread: str, sandbox: str = "workspace-write",
                      entries: list[dict] | None = None, roots: list[str] | None = None,
                      disabled_profile: bool = False) -> Path:
        """Write the record a Codex 0.164 session leaves under the account's ~/.codex/sessions (what the session
        guard reads): the session's workspace, its sandbox policy and the folders it can write."""
        def entry(path: Path, access: str = "write", **extra) -> dict:
            return {"path": {"type": "path", "path": str(path)}, "access": access, **extra}

        def special(kind: str, access: str = "write") -> dict:
            return {"path": {"type": "special", "value": {"kind": kind}}, "access": access}

        if disabled_profile:
            entries = None  # Codex omits the filesystem entries when its sandbox is disabled, and writes the profile instead
        elif entries is None:
            entries = [special("root", "read"), entry(workspace), special("slash_tmp"), special("tmpdir"),
                       *(entry(workspace / name, "read", missing_path_behavior="skip")
                         for name in (".git", ".agents", ".codex"))]
        day = time.strftime("%Y-%m-%d", time.gmtime())
        folder = self.home / ".codex" / "sessions" / day.replace("-", "/")
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        for part in (folder, folder.parent, folder.parent.parent):
            part.chmod(0o700)
        record = lambda kind, payload: json.dumps(  # noqa: E731
            {"timestamp": "2026-11-25T10:00:00.000Z", "type": kind, "payload": payload}, separators=(",", ":")) + "\n"
        path = folder / self.SESSION_ROLLOUT.format(day=day, thread=thread)
        path.write_text(
            record("session_meta", {"id": thread, "cwd": str(workspace),
                                    "runtime_workspace_roots": roots if roots is not None else [str(workspace)]})
            + record("turn_context", {"cwd": str(workspace), "sandbox_policy": {"type": sandbox},
                                      **({"permission_profile": {"type": "disabled"}} if disabled_profile
                                         else {"file_system_sandbox_policy": {"entries": entries}})}))
        path.chmod(0o600)
        return path

    def protected_copies(self) -> dict[str, Path]:
        """Each protected runtime copy's CLI by the project its owner-only binding names (read from the binding file)."""
        found = {}
        for binding in sorted((self.home / ".codex" / "agent-orchestration").glob("*/*/.codex/orchestration/runtime-binding.json")):
            project = json.loads(binding.read_text()).get("project_root")
            if isinstance(project, str):
                found[os.path.realpath(project)] = binding.parent / "cli.py"
        return found

    def copy_cli(self, project: Path, *args: str, thread: str | None = None, refuses: bool = False,
                 timeout: float = 90) -> tuple[int, dict | None, str]:
        """A command as a Codex primary session runs it: through `project`'s protected runtime copy (the program
        the owner's command rule allows), with `thread` as the session's CODEX_THREAD_ID (None: none set, as when
        the command did not come from a Codex session). No other program is run; (exit, JSON reply, text)."""
        program = self.protected_copies().get(os.path.realpath(project))
        if program is None:
            raise HarnessError("the project has no protected runtime copy")
        environment = {"CODEX_THREAD_ID": thread} if thread is not None else {}
        label = args[0] if args else "copy"
        code, out, text = self._run_captured([str(self.shim), "-I", "-B", "-S", str(program), *args], "copy-" + label,
                                             refuses=refuses, environment=environment, timeout=timeout)
        try:
            reply = json.loads(out)
        except ValueError:
            reply = None
        return code, reply if isinstance(reply, dict) else None, text

    # Cleanup -------------------------------------------------------------------------------------

    def stop_processes(self) -> None:
        """Stop every process started inside this scratch folder (the collector, project services)."""
        base = getattr(self, "base", None)
        if base is None:
            return
        for _ in range(3):
            listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                                     env=child_environment(os.environ)).stdout
            pids = [int(line.split(None, 1)[0]) for line in listing.splitlines()
                    if str(base) in line and int(line.split(None, 1)[0]) != os.getpid()]
            if not pids:
                return
            for pid in pids:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            time.sleep(0.6)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    # Private values (G8) -------------------------------------------------------------------------

    def private_values(self) -> dict:
        """The values no output may carry: name -> the text to look for (the names are what is reported)."""
        values = {"owner_key_fingerprint": "", "owner_key_material": "",
                  "passphrase": self.passphrase, "temporary_home_path": str(self.home),
                  "temporary_home_realpath": os.path.realpath(self.home)}
        for number, (fingerprint, blob) in enumerate(self.all_key_values()):
            suffix = f"_{number}" if number else ""
            values[f"owner_key_fingerprint{suffix}"] = fingerprint
            values[f"owner_key_material{suffix}"] = blob
        for number, lines in enumerate(self.owner_private_keys()):
            for line_number, line in enumerate(lines):
                values[f"owner_private_key_{number}_{line_number}"] = line
        for number, other in enumerate(sorted(self.known_passphrases - {self.passphrase})):
            values[f"passphrase_other_{number}"] = other
        return values

    def owner_private_keys(self) -> list[list[str]]:
        """Every owner private key file's base64 body lines (a replacement makes the next key)."""
        keys = []
        for path in sorted((self.home / ".ssh").glob("codex-agents-owner*")):
            if path.suffix == ".pub" or not path.is_file():
                continue
            keys.append([line.strip() for line in path.read_text(errors="replace").splitlines()
                         if len(line.strip()) >= 16 and not line.startswith("-----")])
        return keys

    def _capture(self, text: str, *, kind: str, code: int, refuses: bool, timed_out: bool) -> None:
        CAPTURE_BREAKS.extend(capture_breaks(text, kind=kind, private=self.private_values(), code=code,
                                             refuses=refuses, timed_out=timed_out))

    def register_private_values(self) -> None:
        """Remember the values for the report's own scan (kept in memory only, never written)."""
        PRIVATE_VALUES.update(value for value in self.private_values().values() if value)

    def leaks(self, text: str) -> list[str]:
        return sorted({name for name, value in self.private_values().items() if value and value in text})


def python_version() -> str:
    return "%d.%d.%d" % sys.version_info[:3]
