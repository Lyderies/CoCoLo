"""E1 (RS-11, decision 90.11 A): the collector's login-start entry.

A launchd user agent on macOS or a systemd user unit on Linux that starts the installed collector
when the owner logs in and restarts it after a crash, with a wait between restarts. Setup installs
it only when the owner says yes; the owner can remove it later (Configure). This module writes and
removes the entry and runs the system's own tool through a runner handed in by the caller, so a
test never touches the real home or the real launchctl or systemctl. It starts no process itself.

S6 (E1-8, E1-9): the entry's status is on, off or stale (its command or the authorized runtime it was written for
no longer matches the installed one); an update with consent already kept rewrites a stale entry, never asking.
"""

import json
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


def _has_control(text) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in str(text))


def _command_problem(argv) -> str:
    """Why this command is not an installed collector's own, or nothing. Absolute paths only."""
    argv = list(argv)
    if not argv or not all(type(item) is str and item for item in argv):
        return "the collector command is empty"
    if any(_has_control(item) for item in argv):
        return "the collector command holds a line break or other control character"
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
    word = word.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_./=:+,@-]+", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(argv, log_dir) -> str:
    log = str(Path(log_dir) / LOG_NAME).replace("%", "%%")  # systemd expands % in a path, but not $
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


def _link_problem(home, path: Path, log_dir) -> str:
    """A symbolic link in the way: any folder between the home and the entry, or the log folder itself.
    The home itself may be a link (many systems do that); nothing below it may be."""
    home = Path(home)
    folder = path.parent
    while folder != home and home in folder.parents:
        if folder.is_symlink():
            return f"{folder} is a symbolic link, so nothing was written through it"
        folder = folder.parent
    if Path(log_dir).is_symlink():
        return f"{log_dir} is a symbolic link, so nothing was written through it"
    return ""


def _read_old(path: Path) -> tuple[bool, bytes | None]:
    """(readable, bytes): (True, None) when there is no old entry; (False, None) when it cannot be read."""
    if not os.path.lexists(path):
        return True, None
    try:
        return True, path.read_bytes()
    except OSError:
        return False, None


def install(home, kind: str | None, argv, log_dir, runner, *, uid: int) -> Result:
    """Write the entry and load it. Nothing is changed when a check fails; when the load fails, a new entry
    is removed again and an older working one is put back, and started again only if it was running."""
    if kind not in ("launchd", "systemd"):
        return Result(False, "this system has no login-start support (macOS and Linux only)")
    problem = _command_problem(argv) or ("the log folder has a line break or other control character"
                                          if _has_control(log_dir) else "")
    if problem:
        return Result(False, problem)
    path = entry_path(home, kind)
    problem = _link_problem(home, path, log_dir)
    if problem:
        return Result(False, problem)
    if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
        return Result(False, f"{path} already exists and is not a plain file, so it was left alone")
    readable, old = _read_old(path)
    if not readable:
        return Result(False, f"{path} could not be read, so it was left alone")
    try:
        Path(log_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
    except OSError as exc:
        return Result(False, f"the log folder could not be made ({exc.strerror or 'refused'})")
    data = render_plist(argv, log_dir) if kind == "launchd" else render_unit(argv, log_dir).encode("utf-8")
    was_on = False  # Whether the older entry was loaded (launchd) or running (systemd) before this change.
    if old is not None:
        if kind == "launchd":
            was_on = runner([LAUNCHCTL, "print", f"gui/{uid}/{LABEL}"]) == 0
        else:
            was_on = runner([SYSTEMCTL, "--user", "is-active", UNIT]) == 0
    reason = _write_entry(path, data)  # A new file renamed over the old one: the old stays until the swap.
    if reason:
        return Result(False, reason)
    if kind == "launchd":
        runner([LAUNCHCTL, "bootout", f"gui/{uid}/{LABEL}"])  # Replaces an older load; fine when none.
        loaded = runner([LAUNCHCTL, "bootstrap", f"gui/{uid}", str(path)]) == 0
    else:
        loaded = (runner([SYSTEMCTL, "--user", "daemon-reload"]) == 0
                  and runner([SYSTEMCTL, "--user", "enable", "--now", UNIT]) == 0
                  # enable --now leaves a running unit as it was; a changed command needs a restart.
                  and (old is None or old == data or runner([SYSTEMCTL, "--user", "try-restart", UNIT]) == 0))
    if loaded:
        return Result(True)
    if old is None:
        path.unlink(missing_ok=True)
        if kind == "systemd":
            runner([SYSTEMCTL, "--user", "daemon-reload"])
        return Result(False, "the system's login tool did not accept the entry, so it was removed again")
    if _write_entry(path, old):
        return Result(False, "the system's login tool did not accept the new entry, and the earlier one could not "
                             f"be put back; check {path}")
    if kind == "launchd":
        runner([LAUNCHCTL, "bootout", f"gui/{uid}/{LABEL}"])
        back = not was_on or runner([LAUNCHCTL, "bootstrap", f"gui/{uid}", str(path)]) == 0
    else:
        # try-restart never starts a stopped unit, so a unit the failed change stopped needs a real restart.
        back = runner([SYSTEMCTL, "--user", "daemon-reload"]) == 0 and (
            not was_on or runner([SYSTEMCTL, "--user", "restart", UNIT]) == 0)
    if not back:
        return Result(False, "the system's login tool did not accept the new entry; the earlier one was put back "
                             f"but could not be started again, so check {path} and the collector")
    return Result(False, "the system's login tool did not accept the new entry, so the earlier one was kept")


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


RECORD_NAME = "login-start-runtime"
AUTHORIZATION_NAME = "collector-authorization.json"  # in the state folder: the runtime the owner authorized
_DIGEST = re.compile(r"[0-9a-f]{64}")


class Status(NamedTuple):
    state: str  # "on", "off" or "stale"
    reason: str = ""  # plain words and no private value; only for "stale"


def record_path(answer_file) -> Path:
    """Where the runtime identity the entry was last written for is kept: beside the answer."""
    return Path(answer_file).with_name(RECORD_NAME)


def read_record(answer_file) -> str | None:
    """The runtime digest the entry was last written for; None when none or not a plain private file."""
    if answer_file is None:
        return None
    try:
        path = record_path(answer_file)
        if path.is_symlink() or not path.is_file():
            return None
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value if _DIGEST.fullmatch(value) else None


def write_record(answer_file, digest) -> bool:
    """Keep the digest in one step: a new owner-only file renamed into place."""
    if answer_file is None or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return False
    path = record_path(answer_file)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(digest + "\n")
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        return False
    return True


def runtime_digest(state_root) -> str | None:
    """The digest of the runtime the owner authorized (the collector's own authorization file); None when it
    is missing, not a plain owner-only file, too large or not a digest. Read only, never followed through a link."""
    if state_root is None:
        return None
    path = Path(state_root) / AUTHORIZATION_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        raw = os.read(descriptor, 1025)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw) if len(raw) <= 1024 else None
    except ValueError:
        return None
    digest = value.get("runtime_digest") if isinstance(value, dict) else None
    return digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None


def entry_status(home, kind: str | None, argv, log_dir, answer_file, digest) -> Status:
    """E1-8: is the entry on, off, or stale? Stale: its command is not the installed collector's, or it was
    written for another authorized runtime than the one now installed (or this cannot be told). The reason
    is plain words, never a digest, a key or a folder."""
    if not is_installed(home, kind):
        return Status("off")
    if _command_problem(argv) or _has_control(log_dir):
        return Status("stale", "the installed collector's command is not available here to compare it with")
    path = entry_path(home, kind)
    readable, old = _read_old(path)
    wanted = render_plist(argv, log_dir) if kind == "launchd" else render_unit(argv, log_dir).encode("utf-8")
    if not readable or old != wanted:
        return Status("stale", "its command no longer matches the installed collector")
    if digest is None:
        return Status("stale", "the installed runtime could not be read to compare it with")
    if read_record(answer_file) != digest:
        return Status("stale", "the installed runtime changed since the entry was written")
    return Status("on")


def refresh(*, home, kind, answer_file, argv, log_dir, runner, uid: int, digest) -> tuple[Status, bool]:
    """E1-9: with consent already kept and the entry there but stale, write it again for the installed
    collector (never asking, E1-4). Returns the status afterwards and whether it was rewritten; when it
    cannot be rewritten the status is stale with the plain reason and the earlier entry is kept."""
    status = entry_status(home, kind, argv, log_dir, answer_file, digest)
    if status.state != "stale" or read_answer(answer_file) != "yes":
        return status, False
    if digest is None:
        return status, False
    result = install(home, kind, argv, log_dir, runner, uid=uid)
    if not result.ok:
        return Status("stale", "the entry could not be rewritten for the installed collector: " + result.reason), False
    if not write_record(answer_file, digest):
        return Status("stale", "the entry was rewritten, but setup could not keep a record of the runtime it is "
                               "for, so it may be rewritten again next time"), True
    return Status("on"), True


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


def offer(*, home, kind, answer_file, argv, log_dir, runner, uid: int, ask, show, configure: bool,
          digest=None) -> str:
    """The question, once. ask(items) -> index of the chosen item (None when nobody can be asked); show(text)
    puts a line or paragraph on the screen. Returns what happened: installed, declined, answered, kept,
    removed, left_off, failed, unsupported, no_place, no_command, not_remembered, cancelled or not_asked."""
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
    if not (configure and on):  # The collector command is checked before the owner is asked to agree to it.
        problem = _command_problem(argv)
        if problem:
            show(f"Login start was not offered: {problem}.")
            return "no_command"
    if ask is None:
        show("Login start was not asked about. Run ./setup.sh and choose Configure -> Configure step by step to decide.")
        return "not_asked"

    def keep(value: str) -> bool:
        """Remember the answer; when that fails, say so in plain words (the entry itself is already as reported)."""
        if write_answer(answer_file, value):
            return True
        show("Login start is " + ("on" if value == "yes" else "off") + ", but setup could not keep your answer in "
             f"{answer_file}, so it may ask again.")
        return False

    def choose(items: list[str]) -> int | None:
        while True:
            pick = ask(items)
            if pick == 2:
                for line in details(kind, home, argv, log_dir):
                    show(line)
                continue
            return pick if pick in (0, 1) else None  # Cancelled or unexpected: nothing is decided.

    def cancelled() -> str:
        show("No answer was given, so login start was left as it was.")
        return "cancelled"

    if configure and on:
        show("Login start is on: the collector starts when you log in and restarts after a crash.")
        pick = choose(["Keep it", "Remove it", "Show details"])
        if pick is None:
            return cancelled()
        if pick == 0:
            return "kept" if keep("yes") else "not_remembered"
        result = remove(home, kind, runner, uid=uid)
        if not result.ok:
            show(result.reason)
            return "failed"
        record_path(answer_file).unlink(missing_ok=True)
        return "removed" if keep("no") else "not_remembered"
    show(REASON)
    if configure and answer is not None:
        items = ["Turn it on", "Leave it off", "Show details"]
    else:
        items = ["Yes, start the collector when I log in", "Not now", "Show details"]
    pick = choose(items)
    if pick is None:
        return cancelled()
    if pick != 0:
        if not keep("no"):
            return "not_remembered"
        return "left_off" if configure and answer is not None else "declined"
    result = install(home, kind, argv, log_dir, runner, uid=uid)
    if not result.ok:
        show(result.reason)
        return "failed"
    if digest is not None:
        write_record(answer_file, digest)  # what the entry was written for; a miss only means a later rewrite
    return "installed" if keep("yes") else "not_remembered"
