"""T8h: drive the Python wizard's real flow in-process, with scripted keys, to test its steps directly.

The real flow.Wizard and terminal.Terminal run with keys typed into a string. What the wizard would
start as a process is dispatched by its argument list instead: the installer runs in-process (loaded
once), Git runs for real, and the orchestrator CLI, the client checks, the session scan and attest are
stand-ins equal to the FAKE_* wrappers of tests/test_setup_sh.py. No pty and no Python start per
command, so a step test takes milliseconds to seconds instead of a terminal run's ten.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import effects, flow, terminal  # noqa: E402

ENTER, DOWN = "\r", "\x1b[B"


def to_commit(checks: Path) -> list[str]:
    """The keys of test_setup_sh's drive_to_commit_menu: to the commit screen of a fresh install."""
    return [ENTER, ENTER, f"{checks}\r", ENTER, ENTER, ENTER, ENTER]


# test_setup_sh's finish_after_install from its Continue: skip register, attestation, the profile, the
# temporary-folder rule refusal, then Exit.
FINISH = [ENTER, DOWN + ENTER, DOWN + ENTER, ENTER, ENTER, ENTER]

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789SECRETTOKEN"  # As test_setup_sh's FAKE_FAIL.
CLAUDE_SIGNED_IN = b'{"loggedIn": true, "subscriptionType": "max", "authMethod": "x"}'
PLAIN = __import__("re").compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")


def plain(text: str) -> str:
    return PLAIN.sub("", text)


class FlowTerminal(terminal.Terminal):
    """The real terminal with launches dispatched in-process by the harness."""

    def __init__(self, harness, **kwargs) -> None:
        super().__init__(**kwargs)
        self.harness = harness

    def run_cmd(self, label, argv, *, logdir, slow_seconds=10, width=None, quiet=False, reason=None, draw=None):
        if draw is not None:
            draw()
        self.command_count = getattr(self, "command_count", 0) + 1
        status, out, err = self.harness.dispatch(list(argv))
        if quiet:
            self.last_out = self.last_err = None
        else:
            self.last_out = str(Path(logdir) / f"cmd{self.command_count}.out")
            self.last_err = str(Path(logdir) / f"cmd{self.command_count}.err")
            Path(self.last_out).write_bytes(out)
            Path(self.last_err).write_bytes(err)
        self.last_rc = status
        self.log_step(label, status, reason(self.last_out) if reason and status and not quiet else "")
        return status

    def call(self, argv, **options):
        status, out, err = self.harness.dispatch(list(argv), options)
        if options.get("text"):
            out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        return subprocess.CompletedProcess(argv, status, out, err)


class Harness:
    """One wizard run in a private home. Set the stand-ins' answers before `run`."""

    def __init__(self, test) -> None:
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.test = test
        self.base = Path(os.path.realpath(directory.name))
        self.home = self.base / "home"
        state = self.home / ".local" / "state"
        state.mkdir(parents=True, mode=0o700)
        for folder in (self.home, self.home / ".local", state):
            folder.chmod(0o700)
        self.scratch = self.base / "scratch"
        self.scratch.mkdir(mode=0o700)
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("CODEX_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NO_COLOR", "COLUMNS")}
        environ.update(HOME=str(self.home), XDG_STATE_HOME=str(state), GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        test.addCleanup(patcher.stop)
        cwd = os.getcwd()
        test.addCleanup(os.chdir, cwd)
        # The stand-ins' answers (the FAKE_* wrappers' equivalents).
        self.clients = {"codex": (0, b""), "claude": (0, CLAUDE_SIGNED_IN)}
        self.scan = {"ready": True, "warnings": []}
        self.cli = {}  # subcommand -> callable(args) -> (status, stdout bytes)
        self.attest = []  # exit statuses for successive hand-offs; 0 when run out
        self.helper_fails = False  # FAKE_PJ_FAIL: the path helper exits 1
        self.apply_fails = False  # FAKE_FAIL: the install's --apply prints a secret and exits 2
        self.plant_after_apply = None  # FAKE_PLANT: (project, script) planted as a filter after --apply
        self.launched: list[list[str]] = []
        self.handoffs: list[list[str]] = []

    # Projects --------------------------------------------------------------------------------

    def git(self, project: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, text=True).stdout

    def make_project(self, name: str = "project", base: Path | None = None) -> tuple[Path, Path]:
        """A committed Git project and an operational-checks file for it, as test_setup_sh does."""
        import hashlib
        project = (base or self.base) / name
        project.mkdir(parents=True)
        project.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "qa-check").write_text("#!/usr/bin/python3\nprint(1)\n")
        (project / "qa-check").chmod(0o700)
        self.git(project, "add", ".")
        self.git(project, "commit", "-qm", "init")
        interpreter = hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest()
        check = {"driver": "python-wrapper", "argv": ["./qa-check"],
                 "executable_sha256": hashlib.sha256((project / "qa-check").read_bytes()).hexdigest(),
                 "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 5}
        checks = self.base / f"{name}-checks.json"
        checks.write_text(json.dumps({"version": 1, "deterministic_checks": [{"check_id": "tests", **check}],
                                      "security_checks": [{"check_id": "security", **check}]}))
        return project, checks

    # Dispatch --------------------------------------------------------------------------------

    def dispatch(self, argv: list[str], options: dict | None = None) -> tuple[int, bytes, bytes]:
        self.launched.append(argv)
        if argv and argv[0] == getattr(getattr(self, "wizard", None), "git", None):
            done = subprocess.run(argv, capture_output=True, env=(options or {}).get("env"))
            return done.returncode, done.stdout, done.stderr
        rest = argv[len(flow.PYF) + 1:] if argv[1:len(flow.PYF) + 1] == flow.PYF else argv
        if rest[:1] == [str(flow.INSTALLER)]:
            if self.apply_fails and "--apply" in rest:
                return 2, ('{"error": "boom %s user@example.invalid"}\n' % TOKEN).encode(), (TOKEN + "\n").encode()
            done = self._in_process(effects.installer().main, rest[1:])
            if self.plant_after_apply is not None and "--apply" in rest:
                project, script = self.plant_after_apply
                self.git(project, "config", "filter.planted.clean", str(script))
            return done
        if rest[:1] == [str(flow.CLONE_CLI)]:
            command = rest[1] if len(rest) > 1 else ""
            if command == "codex-session-scan":
                if self.scan == "real":  # The runtime's own scan of this home's Codex sessions.
                    done = subprocess.run([sys.executable, *flow.PYF, *rest], capture_output=True,
                                          stdin=subprocess.DEVNULL)
                    return done.returncode, done.stdout, done.stderr
                return 0, json.dumps(self.scan).encode(), b""
            if command in self.cli:
                status, out = self.cli[command](rest[2:])
                return status, out, b""
            raise AssertionError(f"no stand-in for the CLI command {command!r}")
        if rest[:2] == ["-c", flow.OWNER_TOOL]:
            tool = rest[3:]
            if tool[:1] == ["client-status"]:
                status, out = self.clients[tool[1]]
                return status, out, b""
            if tool == ["scratch", "setup-"]:
                folder = Path(tempfile.mkdtemp(prefix="setup-", dir=self.scratch))
                return 0, (str(folder) + "\n").encode(), b""
            raise AssertionError(f"no stand-in for the owner tool {tool!r}")
        if rest[:2] == [str(ROOT / "scripts" / "setup-wizard.py"), "--helper"]:
            return self._helper(rest[2:])
        if rest[:2] == ["-c", effects.OWNER_GIT]:
            return self._in_process(effects.installer().git_tool, rest[3:])
        raise AssertionError(f"no stand-in for {argv!r}")

    @staticmethod
    def _in_process(function, args) -> tuple[int, bytes, bytes]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = function(list(args))
            except SystemExit as exit:
                status = exit.code if isinstance(exit.code, int) else 1
        return status or 0, out.getvalue().encode(), err.getvalue().encode()

    def _helper(self, args) -> tuple[int, bytes, bytes]:
        if self.helper_fails and args[:1] == ["paths"]:
            return 1, b"", b""
        import importlib.util
        spec = importlib.util.spec_from_file_location("setup_wizard_entry", ROOT / "scripts" / "setup-wizard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.BytesIO()
        fake_stdout = mock.Mock(buffer=out, write=lambda text: out.write(text.encode()), flush=lambda: None)
        with mock.patch.object(module.sys, "stdout", fake_stdout):
            status = module.helper(list(args))
        return status, out.getvalue(), b""

    # CLI stand-ins, each equal to its FAKE_* wrapper in test_setup_sh ------------------------

    def fake_register(self, answer: str) -> None:
        """FAKE_REGISTER: learning-register answers `answer`; every call is logged."""
        self.register_log: list[list[str]] = []

        def register(args):
            self.register_log.append(list(args))
            return 0, (answer + "\n").encode()
        self.cli["learning-register"] = register
        self.fake_status()

    def fake_upgrade(self, upgraded: str) -> None:
        """FAKE_UPGRADE: registering is refused (ledger_upgrade_required) until --upgrade-ledger is
        given, then answers `upgraded` (exit 2 when it says ready false)."""
        self.register_log = []

        def register(args):
            self.register_log.append(list(args))
            if "--upgrade-ledger" in args:
                return (2 if '"ready": false' in upgraded else 0), (upgraded + "\n").encode()
            return 2, (b'{"ready": false, "reason_code": "ledger_upgrade_required", "ledger_schema_version": 6,'
                       b' "state_moved": null}\n')
        self.cli["learning-register"] = register
        self.fake_status()

    def fake_status(self, starting: int = 0, reason: str = "status_collector_transport_unavailable") -> None:
        """FAKE_STATUS_STARTING/REASON: learning-status answers `reason` `starting` times, then ready."""
        self.status_count = 0

        def status(args):
            self.status_count += 1
            if self.status_count <= starting:
                return 2, json.dumps({"ready": False, "reason_code": reason}).encode()
            return 0, b'{"ready": true, "collector_runtime_version": "0.0.0"}\n'
        self.cli["learning-status"] = status

    def plant_legacy_records(self) -> str:
        """An old-format records folder in the test home, as a session on ~/Library could make."""
        import hashlib
        legacy = self.home / "Library" / "Application Support" / "codex-agents"
        legacy.mkdir(parents=True, mode=0o700)
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.base / "k")],
                       check=True, capture_output=True)
        fields = (self.base / "k.pub").read_text().split()
        signers = f"codex-agents-owner {fields[0]} {fields[1]}\n".encode()
        (legacy / "owner-allowed-signers").write_bytes(signers)
        return hashlib.sha256(signers).hexdigest()

    def commit_count(self, project: Path) -> int:
        return int(self.git(project, "rev-list", "--count", "HEAD").strip())

    @staticmethod
    def final(screen: str) -> str:
        """The last screen drawn: the summary, when the run reached it."""
        return screen.rsplit("COCOLO setup", 1)[-1]

    def remembered(self, *projects: Path) -> None:
        """Projects setup already set up, as it lists them in the records' installer folder."""
        installer = self.home / ".local" / "state" / "codex-agents" / "installer"
        installer.mkdir(parents=True, mode=0o700, exist_ok=True)
        for folder in (installer.parent, installer):
            folder.chmod(0o700)
        for project in projects:
            manifest = project / ".codex" / "orchestration" / "install-manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n")
        (installer / "setup-projects").write_text("".join(f"{p}\n" for p in projects) + "relative/is-skipped\n")

    def write_codex_session(self, workspace: Path, thread: str) -> None:
        """A synthetic Codex 0.164 session record for a session opened on `workspace` (as test_setup_sh)."""
        folder = self.home / ".codex" / "sessions" / "2026" / "11" / "01"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
                   {"path": {"type": "path", "path": str(workspace)}, "access": "write"}]
        lines = [{"timestamp": "2026-11-01T10:00:00Z", "type": "session_meta", "payload": {
                     "id": thread, "cwd": str(workspace), "runtime_workspace_roots": [str(workspace)]}},
                 {"timestamp": "2026-11-01T10:00:01Z", "type": "turn_context", "payload": {
                     "cwd": str(workspace), "sandbox_policy": {"type": "workspace-write"},
                     "file_system_sandbox_policy": {"entries": entries}}}]
        (folder / f"rollout-2026-11-01T10-00-00-{thread}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines))

    def project_under_home(self) -> tuple[Path, Path]:
        project, checks = self.make_project("staging")
        (self.home / "Code").mkdir()
        moved = self.home / "Code" / "proj"
        project.rename(moved)
        return moved, checks

    # Running ---------------------------------------------------------------------------------

    def run(self, *keys: str, start: Path | str, cols: int = 100, lines: int = 30) -> tuple[int, str]:
        """Run the wizard from `start` with the keys typed in order; returns (exit code, screen text).
        Running out of keys ends the run the way closed input does."""
        os.chdir(start)
        data = bytearray("".join(keys).encode())

        def read(count, timeout):
            if not data:
                return b""
            chunk = bytes(data[:count])
            del data[:count]
            return chunk
        out = io.StringIO()
        term = FlowTerminal(self, out=out, read=read, colours=terminal.NO_COLOURS, size=lambda: (cols, lines))
        wizard = flow.Wizard(term, start=str(start), python=sys.executable, owner_path=os.environ.get("PATH", ""),
                             bash_version="3.2.57")
        self.wizard = wizard
        attest_results = list(self.attest)

        def handoff(argv):
            self.handoffs.append(list(argv))
            status = attest_results.pop(0) if attest_results else 0
            term.write(f"attest stub run {len(self.handoffs)}\n")
            term.write(f"\n  attest.py finished (exit code {status}). Press Enter to continue. ")
            term._line()
            return status
        wizard.handoff = handoff
        code, message = 0, ""
        started = time.monotonic()
        try:
            wizard.main()
        except terminal.Stop as stop:
            code, message = stop.code, stop.message
        finally:
            wizard.cleanup()
        self.seconds = time.monotonic() - started
        return code, plain(out.getvalue()) + ("\n" + message if message else "")
