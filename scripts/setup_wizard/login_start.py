"""E1 (RS-11, decision 90.11 A): the collector's login-start entry.

A launchd user agent on macOS or a systemd user unit on Linux that starts the installed collector
when the owner logs in and restarts it after a crash, with a wait between restarts. Setup installs
it only when the owner says yes; the owner can remove it later (Configure). This module writes and
removes the entry and runs the system's own tool through a runner handed in by the caller, so a
test never touches the real home or the real launchctl or systemctl. It starts no process itself.
"""

import os
import plistlib
import re
import stat
import tempfile
from pathlib import Path
from typing import NamedTuple

LABEL = "com.cocolo.collector"
UNIT = "cocolo-collector.service"
LAUNCHCTL = "/bin/launchctl"
SYSTEMCTL = "/usr/bin/systemctl"
LOG_NAME = "collector.log"
# The wait between restarts: launchd waits at least this long between starts; systemd waits RESTART_SECONDS,
# growing toward RESTART_MAX_SECONDS where it supports it, and gives up after START_BURST starts in START_WINDOW.
THROTTLE_SECONDS = 30
RESTART_SECONDS = 10
RESTART_MAX_SECONDS = 300
START_BURST = 5
START_WINDOW = 600
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # As the collector's own start: no inherited environment.


class Result(NamedTuple):
    ok: bool
    reason: str = ""


def kind_for(system: str) -> str | None:
    """The kind of login-start entry this system has (sys.platform), or None."""
    if system == "darwin":
        return "launchd"
    if system.startswith("linux"):
        return "systemd"
    return None


def entry_path(home, kind: str) -> Path:
    home = Path(home)
    if kind == "launchd":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return home / ".config" / "systemd" / "user" / UNIT


def log_folder(state_root) -> Path:
    """The CoCoLo log folder: beside the owner's state folder, not inside it."""
    return Path(state_root).parent / "cocolo-logs"


def is_installed(home, kind: str | None) -> bool:
    return kind is not None and os.path.lexists(entry_path(home, kind))


def _command_problem(argv) -> str:
    """Why this command is not an installed collector's own, or nothing. Absolute paths only."""
    argv = list(argv)
    if not argv or not all(type(item) is str and item for item in argv):
        return "the collector command is empty"
    if not argv[0].startswith("/"):
        return "the collector command does not start with an absolute path to Python"
    if "learning-serve" not in argv[1:] or argv.index("learning-serve") < 2:
        return "the collector command does not run learning-serve"
    if not argv[argv.index("learning-serve") - 1].startswith("/"):
        return "the collector's program is not given by an absolute path"
    if "--state-root" not in argv or argv.index("--state-root") + 1 >= len(argv) \
            or not argv[argv.index("--state-root") + 1].startswith("/"):
        return "the collector's state folder is not given by an absolute path"
    return ""


def render_plist(argv, log_dir) -> bytes:
    log = str(Path(log_dir) / LOG_NAME)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        # Restarted after a crash or a failed exit; a clean stop stays stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "WorkingDirectory": "/",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
    })


def _unit_word(word: str) -> str:
    word = word.replace("%", "%%")
    if re.fullmatch(r"[A-Za-z0-9_./=:+,@-]+", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(argv, log_dir) -> str:
    log = str(Path(log_dir) / LOG_NAME)
    return (
        "[Unit]\n"
        "Description=CoCoLo collector (records run outcomes on this machine only)\n"
        f"StartLimitIntervalSec={START_WINDOW}\n"
        f"StartLimitBurst={START_BURST}\n"
        "\n[Service]\n"
        f"ExecStart={' '.join(_unit_word(word) for word in argv)}\n"
        "WorkingDirectory=/\n"
        "Restart=on-failure\n"
        f"RestartSec={RESTART_SECONDS}\n"
        f"RestartMaxDelaySec={RESTART_MAX_SECONDS}\n"
        "RestartSteps=5\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
        "\n[Install]\n"
        "WantedBy=default.target\n"
    )


def _write_entry(path: Path, data: bytes) -> str:
    """Write the entry in one step; a reason (and no change) when the place is not safe."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
            return f"{path} already exists and is not a plain file, so it was left alone"
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    except OSError as exc:
        return f"the entry could not be written ({exc.strerror or 'refused'})"
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o644 if path.suffix == ".plist" else 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        Path(temporary).unlink(missing_ok=True)
        return f"the entry could not be written ({exc.strerror or 'refused'})"
    return ""


def install(home, kind: str | None, argv, log_dir, runner, *, uid: int) -> Result:
    """Write the entry and load it. Nothing is changed when a check fails; a failed load removes the entry again."""
    if kind not in ("launchd", "systemd"):
        return Result(False, "this system has no login-start support (macOS and Linux only)")
    problem = _command_problem(argv)
    if problem:
        return Result(False, problem)
    path = entry_path(home, kind)
    if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
        return Result(False, f"{path} already exists and is not a plain file, so it was left alone")
    try:
        Path(log_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
    except OSError as exc:
        return Result(False, f"the log folder could not be made ({exc.strerror or 'refused'})")
    if kind == "launchd":
        reason = _write_entry(path, render_plist(argv, log_dir))
        if reason:
            return Result(False, reason)
        runner([LAUNCHCTL, "bootout", f"gui/{uid}/{LABEL}"])  # Replaces an older load; fine when none.
        loaded = runner([LAUNCHCTL, "bootstrap", f"gui/{uid}", str(path)]) == 0
    else:
        reason = _write_entry(path, render_unit(argv, log_dir).encode("utf-8"))
        if reason:
            return Result(False, reason)
        loaded = (runner([SYSTEMCTL, "--user", "daemon-reload"]) == 0
                  and runner([SYSTEMCTL, "--user", "enable", "--now", UNIT]) == 0)
    if not loaded:
        path.unlink(missing_ok=True)
        return Result(False, "the system's login tool did not accept the entry, so it was removed again")
    return Result(True)


def remove(home, kind: str | None, runner, *, uid: int) -> Result:
    """Unload the entry and delete its file. A missing file or an unload that fails is not an error."""
    if kind not in ("launchd", "systemd"):
        return Result(True)
    path = entry_path(home, kind)
    if kind == "launchd":
        runner([LAUNCHCTL, "bootout", f"gui/{uid}/{LABEL}"])
    else:
        runner([SYSTEMCTL, "--user", "disable", "--now", UNIT])
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return Result(False, f"{path} could not be deleted ({exc.strerror or 'refused'})")
    if kind == "systemd":
        runner([SYSTEMCTL, "--user", "daemon-reload"])
    return Result(True)


def read_answer(path) -> str | None:
    """The owner's kept answer, "yes" or "no"; None when none, or the file is not a plain file."""
    if path is None:
        return None
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value if value in ("yes", "no") else None


def write_answer(path, value: str) -> bool:
    """Keep the answer in one step: a new owner-only file renamed into place."""
    if value not in ("yes", "no"):
        return False
    path = Path(path)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value + "\n")
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        return False
    return True


def terminal_runner(term):
    """The runner setup uses: the wizard terminal's own audited helper start, waited on, as a status."""
    def run(argv):
        return term.call(list(argv)).returncode
    return run


REASON = ("The CoCoLo collector is the small local service that records run outcomes on this machine. If it stops "
          "(a crash, or you log out or restart), nothing records until something starts it again. Login start "
          "makes your computer start it when you log in, and restart it after a crash, waiting between restarts.")


def details(kind: str, home, argv, log_dir) -> list[str]:
    where = entry_path(home, kind)
    tool = "a launchd user agent" if kind == "launchd" else "a systemd user unit"
    return [
        f"What it adds: {tool}, one file: {where}",
        "What it runs: " + " ".join(argv),
        f"Restarts: after a crash it starts again, waiting at least {THROTTLE_SECONDS if kind == 'launchd' else RESTART_SECONDS} "
        "seconds between starts" + ("." if kind == "launchd" else f", and giving up after {START_BURST} starts in "
                                   f"{START_WINDOW // 60} minutes."),
        f"Log: {Path(log_dir) / LOG_NAME} (nothing secret is written to the file or the log).",
        "It runs as you, never as an administrator, and only the collector.",
        "To remove it later: run ./setup.sh, choose Configure -> Configure step by step, and answer at the "
        "collector step (Remove it). Nothing else is touched.",
    ]


def offer(*, home, kind, answer_file, argv, log_dir, runner, uid: int, ask, show, configure: bool) -> str:
    """The question, once. ask(items) -> index of the chosen item (None when nobody can be asked); show(text)
    puts a line or paragraph on the screen. Returns what happened: installed, declined, answered, kept,
    removed, left_off, failed, unsupported, no_place or not_asked."""
    if kind is None:
        show("Login start for the collector is not available here (macOS and Linux only).")
        return "unsupported"
    if answer_file is None:
        show("Login start was not offered: setup has no private place on this machine to keep your answer.")
        return "no_place"
    answer = read_answer(answer_file)
    on = is_installed(home, kind)
    if answer is not None and not configure:
        return "answered"
    if ask is None:
        show("Login start was not asked about. Run ./setup.sh and choose Configure -> Configure step by step to decide.")
        return "not_asked"

    def choose(items: list[str]) -> int:
        while True:
            pick = ask(items)
            if pick != 2:
                return pick
            for line in details(kind, home, argv, log_dir):
                show(line)

    if configure and on:
        show("Login start is on: the collector starts when you log in and restarts after a crash.")
        if choose(["Keep it", "Remove it", "Show details"]) == 0:
            write_answer(answer_file, "yes")
            return "kept"
        result = remove(home, kind, runner, uid=uid)
        if not result.ok:
            show(result.reason)
            return "failed"
        write_answer(answer_file, "no")
        return "removed"
    show(REASON)
    if configure and answer is not None:
        items = ["Turn it on", "Leave it off", "Show details"]
    else:
        items = ["Yes, start the collector when I log in", "Not now", "Show details"]
    if choose(items) != 0:
        write_answer(answer_file, "no")
        return "left_off" if configure and answer is not None else "declined"
    result = install(home, kind, argv, log_dir, runner, uid=uid)
    if not result.ok:
        show(result.reason)
        return "failed"
    write_answer(answer_file, "yes")
    return "installed"
