"""T8h: the wizard terminal tests' own bodies, driven in-process instead of through a pty.

DirectDriverMixin replaces only WizardTerminalTests' terminal driver (start, expect, settle, send,
finish): the real flow.Wizard runs in a thread, keys arrive through a queue, and what it would start
as a process goes through `emulate`, a branch-for-branch copy of the fake python3 wrapper that
test_setup_sh's setUp writes, logging the same python.log, python-args.log, python-full.log,
register.log, status.count, attest.count and scratch.log. The installer, its git tool and the
path helper run in-process (loaded once) instead of as a Python start each; every other command runs
as the wrapper would run it. So each inherited test keeps its exact assertions and its own deadlines.
"""

import contextlib
import io
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import effects, flow, terminal  # noqa: E402
from test_setup_sh import KEPT_TERMINAL_RUNS, TOKEN, TRUSTED_PY  # noqa: E402

_EOF = object()


class _Output:
    """The wizard's terminal output, read as a pty would be."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        with self.lock:
            self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def take(self) -> str:
        with self.lock:
            text, self.chunks = "".join(self.chunks), []
        return text


class _DirectTerminal(terminal.Terminal):
    """The real terminal, with launches emulated by the driver."""

    def __init__(self, driver, **kwargs) -> None:
        super().__init__(**kwargs)
        self.driver = driver

    def run_cmd(self, label, argv, *, logdir, slow_seconds=10, width=None, quiet=False, reason=None, draw=None):
        if draw is not None:
            draw()
        self.command_count = getattr(self, "command_count", 0) + 1
        status, out, err = self.driver.emulate(list(argv))
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
        status, out, err = self.driver.emulate(list(argv), options)
        if options.get("text"):
            out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        return subprocess.CompletedProcess(argv, status, out, err)


class DirectDriverMixin:
    """Mixed in before test_setup_sh.WizardTerminalTests: its tests, driven in-process."""

    def setUp(self) -> None:
        if self._testMethodName in KEPT_TERMINAL_RUNS:
            self.skipTest("kept as a real terminal run (WizardTerminalTests)")
        super().setUp()
        self.wrapper = os.path.realpath(self.fakes / "python3")
        self.thread = None
        self.pid = 0

    # The wrapper, branch for branch ---------------------------------------------------------

    def _log_launch(self, args: list[str]) -> None:
        env = os.environ
        with open(self.py_log, "a") as log:
            log.write(env.get("PYTHONDONTWRITEBYTECODE", "") + "\n")
        with open(self.py_args, "a") as log:
            log.write(" ".join((args + [""] * 5)[:5]) + "\n")
        with open(self.py_full, "a") as log:
            log.write(" ".join(args) + "\n")

    def emulate(self, argv: list[str], options: dict | None = None) -> tuple[int, bytes, bytes]:
        """What the fake python3 (or, for git, the real git) does with this argument list."""
        if argv[0] != self.wrapper:  # Git, run for real with the wizard's environment and options.
            done = subprocess.run(argv, capture_output=True, env=(options or {}).get("env"),
                                  stdin=subprocess.DEVNULL)
            return done.returncode, done.stdout, done.stderr
        args = argv[1:]
        self._log_launch(args)
        joined = " ".join(args)
        env = os.environ

        def matches(pattern: str) -> bool:
            from fnmatch import fnmatchcase
            return fnmatchcase(joined, pattern)
        if env.get("FAKE_PLANT") and matches("*install.py*--apply*"):
            status, out, err = self._real(args)
            subprocess.run(["git", "-C", env["FAKE_PLANT"], "config", "filter.planted.clean",
                            env["FAKE_PLANT_SCRIPT"]], check=False, capture_output=True)
            return status, out, err
        if env.get("FAKE_FAIL") and matches("*install.py*--apply*"):
            return 2, f'{{"error": "boom {TOKEN} user@example.invalid"}}\n'.encode(), f"{TOKEN}\n".encode()
        if env.get("FAKE_SLOW") and matches("*install.py*install*"):
            raise AssertionError("FAKE_SLOW needs a real process: kept as a real terminal run")
        if env.get("FAKE_PJ_FAIL") and "paths" in args:
            return 1, b"", b""
        if env.get("FAKE_ATTEST") and matches('*attest.py --*'):
            counter = Path(env["FAKE_ATTEST"])
            n = (int(counter.read_text().strip()) if counter.exists() else 0) + 1
            counter.write_text(f"{n}\n")
            status = 1 if n <= int(env.get("FAKE_ATTEST_FAILS") or 0) else 0
            return status, f"attest stub run {n}\n".encode(), b""
        if env.get("FAKE_UNTRUSTED") and matches("*host-command*"):
            return 2, b'{"ready": false, "error": "the Python interpreter cannot be trusted: x"}\n', b""
        if env.get("FAKE_RULE") and (matches("*cli.py host-command*") or matches("*cli.py install-codex-rule*")):
            return self._process([TRUSTED_PY, str(self.fakes / "fake_rule.py"), *args])
        if env.get("FAKE_CLIENT_DIRS") and matches("*attest.py client-status *"):
            return self._process([TRUSTED_PY, "-I", "-B", "-S", str(self.fakes / "fake_client_tool.py"), *args])
        if env.get("FAKE_SCAN") and matches("*cli.py codex-session-scan *"):
            return 0, (env["FAKE_SCAN"] + "\n").encode(), b""
        if env.get("FAKE_UPGRADE"):
            if matches("*cli.py learning-register *--upgrade-ledger*"):
                self._append(self.base / "register.log", joined)
                upgraded = env["FAKE_UPGRADE"]
                return (2 if '"ready": false' in upgraded else 0), (upgraded + "\n").encode(), b""
            if matches("*cli.py learning-register *"):
                self._append(self.base / "register.log", joined)
                return 2, (b'{"ready": false, "reason_code": "ledger_upgrade_required", "ledger_schema_version": 6,'
                           b' "state_moved": null}\n'), b""
            if matches("*cli.py learning-status *"):
                return 0, b'{"ready": true, "collector_runtime_version": "0.0.0"}\n', b""
        if env.get("FAKE_REGISTER"):
            if matches("*cli.py learning-register *"):
                self._append(self.base / "register.log", joined)
                return 0, (env["FAKE_REGISTER"] + "\n").encode(), b""
            if matches("*cli.py learning-status *"):
                counter = self.base / "status.count"
                n = (int(counter.read_text().strip()) if counter.exists() else 0) + 1
                counter.write_text(f"{n}\n")
                if n <= int(env.get("FAKE_STATUS_STARTING") or 0):
                    reason = env.get("FAKE_STATUS_REASON") or "status_collector_transport_unavailable"
                    return 2, f'{{"ready": false, "reason_code": "{reason}"}}\n'.encode(), b""
                return 0, b'{"ready": true, "collector_runtime_version": "0.0.0"}\n', b""
        if matches('*attest.py scratch setup-*'):
            status, out, err = self._real(args)
            self._append(self.scratch_log, out.decode().rstrip("\n"))
            return status, out, err
        return self._real(args)

    @staticmethod
    def _append(path: Path, line: str) -> None:
        with open(path, "a") as log:
            log.write(line + "\n")

    @staticmethod
    def _process(argv: list[str]) -> tuple[int, bytes, bytes]:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr

    def _real(self, args: list[str]) -> tuple[int, bytes, bytes]:
        """`exec TRUSTED_PY "$@"`: the installer, its git tool, the owner tool and the path helper run
        in-process (loaded once); anything else as a real process under the trusted interpreter."""
        assert args[:5] == ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"], args
        rest = args[5:]
        if rest[:1] == [str(flow.INSTALLER)]:
            return self._in_process(effects.installer().main, rest[1:])
        if rest[:2] == ["-c", effects.OWNER_GIT] and rest[2:3] == [str(flow.INSTALLER)]:
            return self._in_process(effects.installer().git_tool, rest[3:])
        if rest[:2] == ["-c", flow.OWNER_TOOL] and rest[2:3] == [str(flow.ATTEST)]:
            return self._in_process(effects.attest().owner_tool, rest[3:])
        if rest[:2] == [str(ROOT / "scripts" / "setup-wizard.py"), "--helper"]:
            return self._helper(rest[2:])
        return self._process([TRUSTED_PY, *args])

    @staticmethod
    def _in_process(function, args) -> tuple[int, bytes, bytes]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = function(list(args))
            except SystemExit as exit:  # As a process: no code is 0; a message goes to stderr and is 1.
                if exit.code is None:
                    status = 0
                elif isinstance(exit.code, int):
                    status = exit.code
                else:
                    print(exit.code, file=sys.stderr)
                    status = 1
        return status or 0, out.getvalue().encode(), err.getvalue().encode()

    @staticmethod
    def _helper(args) -> tuple[int, bytes, bytes]:
        import importlib.util
        spec = importlib.util.spec_from_file_location("setup_wizard_entry", ROOT / "scripts" / "setup-wizard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.BytesIO()
        fake_stdout = mock.Mock(buffer=out, write=lambda text: out.write(text.encode()), flush=lambda: None)
        with mock.patch.object(module.sys, "stdout", fake_stdout):
            status = module.helper(list(args))
        return status, out.getvalue(), b""

    # The driver: WizardTerminalTests' start, expect, settle, send and finish -----------------

    def start(self, cwd: Path, extra_env: dict | None = None, *, direct: bool = False) -> None:
        # setup.sh exports PYTHONDONTWRITEBYTECODE=1 for everything it starts.
        env = dict(self.env, **(extra_env or {}), PYTHONDONTWRITEBYTECODE="1")
        environment = mock.patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        here = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, here)
        # In-process, the installer's own trusted-interpreter check answers with the interpreter the
        # wrapper runs it under.
        trusted = mock.patch.object(effects.installer().attest, "trusted_python", lambda: TRUSTED_PY)
        trusted.start()
        self.addCleanup(trusted.stop)
        # What setup.sh's launcher logs before the wizard: its version check and its exec.
        self._log_launch(["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", "-c",
                          "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"])
        self._log_launch(["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null",
                          str(ROOT / "scripts" / "setup-wizard.py")])
        self.screen = self.transcript = ""
        self.keys: queue.Queue = queue.Queue()
        self.output = _Output()
        pending = bytearray()
        closed = []

        def read(count, timeout):
            deadline = None if timeout is None else time.monotonic() + timeout
            while not pending and not closed:
                try:
                    item = self.keys.get(timeout=0.05)
                except queue.Empty:
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    continue
                if item is _EOF:
                    closed.append(True)
                else:
                    pending.extend(item)
            if not pending:
                return b""
            chunk = bytes(pending[:count])
            del pending[:count]
            return chunk
        term = _DirectTerminal(self, out=self.output, read=read, colours=terminal.terminal_colours(),
                               size=lambda: (100, 30))
        wizard = flow.Wizard(term, start=str(cwd), python=os.path.realpath(self.wrapper),
                             owner_path=env.get("PATH", ""), bash_version="3.2.57")

        def handoff(argv):  # flow.Wizard.handoff, with attest.py's run emulated (its output on the terminal)
            term.show_cursor()
            term.write("\033[H\033[2J")
            status, out, err = self.emulate(list(argv))
            term.write((out + err).decode("utf-8", "replace"))
            term.write(f"\n  attest.py finished (exit code {status}). Press Enter to continue. ")
            term._line()
            return status
        wizard.handoff = handoff
        self.result: list = []

        def main():
            code = 0
            with mock.patch("signal.signal"):
                try:
                    wizard.main()
                except terminal.Stop as stop:
                    code = stop.code
                    if stop.message:
                        self.output.write(stop.message + "\n")
                finally:
                    wizard.cleanup()
            self.result.append(code)
        self.thread = threading.Thread(target=main, daemon=True)
        self.pid = 1
        self.thread.start()
        self.addCleanup(self._reap)

    def _pull(self) -> None:
        text = self.output.take()
        if text:
            self.screen += text
            self.transcript += text

    def expect(self, text: str, timeout: float = 25.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._pull()
            if text in self.screen:
                return
            if self.thread is not None and not self.thread.is_alive():
                self._pull()
                if text in self.screen:
                    return
                break
            time.sleep(0.02)
        self.fail(f"never saw {text!r}; last screen: {re.sub(chr(27) + r'.*?[A-Za-z]', '', self.screen[-1500:])!r}")

    def settle(self, seconds: float = 0.6) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._pull()
            time.sleep(0.02)
        self._pull()

    def send(self, data: str) -> None:
        self.screen = ""
        self.keys.put(data.encode())

    def wait_for_line_editor(self, timeout: float = 25.0) -> None:
        pass  # In-process the editor reads every key itself.

    def finish(self, timeout: float = 10.0) -> int:
        self.thread.join(timeout)
        self._pull()
        if self.thread.is_alive():
            self.fail("the wizard did not exit")
        self.pid = 0
        return self.result[0]

    def _reap(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.keys.put(_EOF)
            self.thread.join(15)
