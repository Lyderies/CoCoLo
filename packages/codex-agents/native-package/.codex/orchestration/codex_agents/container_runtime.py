"""One child run in a hardened podman container: launch, check, stop, and "gone" asked of the OS.

Owner decisions 2026-11-21 (PLAN-0.4.0.md, "Containment decided" and "Health monitor"): every
child runs in a podman container with a hardened profile checked at each start; "gone" is asked
of the OS; a deterministic health monitor whose first check is "every COCOLO container belongs to
a live run". Evidence: RESEARCH-0.4.0-podman-spike.md (the tested profile, PC-1..7) and
RESEARCH-0.4.0-supervision.md (SIGTERM, grace, kill the group; ask the OS; "stop failed" holds
the slot).

Requirements:
- CS-1 Launch: a run's container is identified (its name, derived from the run id, and the labels
  `cocolo.run=<run id>` and `cocolo.project=<project id>`) and that identity is handed to the
  caller's `record` before the container is created, so the caller records it first. The container
  uses the spike's hardened profile: read-only root, capped tmpfs for /tmp and the home folder,
  cap-drop ALL, no-new-privileges, a seccomp profile denying nested user namespaces, keep-id user
  1000:1000, pids/memory/cpu limits, only the given workspace mounted (read-only unless asked), and
  network none unless a per-run internal network is given (one that is not internal is refused).
  The image is never pulled.
- CS-2 Check at start: after start, the effective limits are read back from the container's cgroup
  on the podman host (pids.max, memory.max, memory.swap.max, cpu.max: the tightest value from the
  container's cgroup down to the one its first process sits in, so neither podman's report nor the
  image is trusted for them), with the capability sets, no-new-privileges and seccomp mode of its
  first process (/proc/<pid>/status there), and
  `unshare -Ur` must fail inside with "Operation not permitted". The configured read-only root,
  mounts and network are compared too. Any mismatch, or a check that cannot run, stops and removes
  the container and refuses plainly (fail closed). The command itself waits behind a gate (the
  entrypoint waits for GATE_FILE, then execs the command) that is opened only after every check
  passed, so a refused container never ran the command.
- CS-3 Stop: SIGTERM to the container's first process, a grace (default 10 s), then SIGKILL to
  every process in it (`podman stop --time`), then removal (`podman rm --force`, repeated while
  waiting for "gone").
- CS-4 Gone: the container no longer exists, or it has exited; and its cgroup, when known (recorded
  at launch or read while it ran), is gone or reports `populated 0`. A cgroup still populated, a
  container still running, or anything unreadable after the kill and a timeout is "stop_failed"
  (the caller must hold the slot and tell the owner), never "gone".
- CS-5 Health monitor check 1 (`sweep_orphans`): list containers carrying this project's label;
  any whose run id is not in the caller's set of live runs is stopped and removed through CS-3/CS-4
  and reported. A row without this project's label (another project, or no COCOLO label at all) is
  never touched; a row with the label but a name that is not that run's identity is reported
  "skipped" and left alone.
- CS-6 Unavailable podman: every operation answers a plain Refusal (reason and one-line message),
  never an exception trace, when podman is missing, hangs, or its machine or service is stopped.
- C2 (PLAN-0.4.0-supervisor-slices.md): every run's container also carries `cocolo.kind=run`; a spec
  may set the run's absolute deadline on the container itself (`timeout_seconds`: podman's
  `--timeout`, enforced by conmon, so it fires with the service gone), and the start check compares
  the timeout podman reports with it (fail closed). `attach_arguments` names the local command that
  streams the container's output and exits with its exit code.

- C4 (PLAN-0.4.0-supervisor-slices.md): a per-run network must carry the run's own `cocolo.run` and
  `cocolo.project` labels, or the launch is refused before anything exists (`network_not_this_run`).
  `stop` may be given the labels the container must carry: a container under that name whose labels
  differ is not the run's and is left alone (`stop_failed`, never "gone"). The project's listing
  (`list_project`) and the orphan stop (`stop_orphans`) are separate steps, so a caller can read its
  live set after the listing (an identity recorded before its container was created is then never
  swept).

- HM-1 (PLAN-0.4.0-supervisor-slices.md): `list_runs` lists every run container on this podman
  (`cocolo.kind=run`), whatever its project, for the collector's health check (`health_containers`),
  which then stops each project's orphans through `stop_orphans` with that project's own live set.

- Decision 49 (ANALYSIS-0.4.0-container-stop.md, CS-1..CS-6 of that analysis): `stop` is the ONE stop tool, used
  by the supervisor (C3's stop action, C4's recovery, HM-1's health check) and by the detached watchdog. Nothing
  it starts survives it: every podman command runs in a process group of its own, under a small guard that kills
  the group when the stop tool's process dies (a pipe that closes with it: SIGKILL included) and the stop tool
  kills the group when the command ends, even a command that exited and left a child, and reaps it (CS-1, CS-2).
  One absolute deadline (`deadline`, on the runtime's clock; one is made when none is given) bounds every command,
  wait and reap of the stop: a command is given only the time left less the reap's, and a stop that runs out of
  time answers "deadline" (CS-3). The grace is the smaller of the configured one and the time left at the moment
  of the stop, less STOP_GRACE_MARGIN_SECONDS (CS-4). A group that cannot be killed or reaped, or a removal that
  fails, is reported in the answer and ends the stop, never swallowed (CS-5). Gone is only the OS's word: an
  unreadable state is never gone (CS-6). The run's own time limit (`podman create --timeout`) is a backstop that
  fires with the service gone; it is set only when `podman version` shows a client and server of at least
  MIN_TIMEOUT_PODMAN_VERSION, and a launch that skips it says why in its checks ("timeout backstop").

The caller's duties (not done here): include every run whose identity it recorded in the live set
it gives the sweep (a launch between its snapshot and the listing would otherwise be swept), keep
the slot of any run whose stop is not "gone", and keep the seccomp profile file owner-only.

Standard library only.
"""

from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence

RUN_LABEL = "cocolo.run"
PROJECT_LABEL = "cocolo.project"
KIND_LABEL = "cocolo.kind"
RUN_KIND = "run"
# Longest deadline a container is given (podman's --timeout, in seconds): a week.
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600
DEFAULT_NAME_PREFIX = "cocolo-run-"
WORKSPACE_TARGET = "/workspace"
CONTAINER_HOME = "/var/cocolo-home"
CONTAINER_USER = (1000, 1000)
GATE_FILE = "/tmp/.cocolo-start"
DEFAULT_GRACE_SECONDS = 10
DEFAULT_GONE_TIMEOUT_SECONDS = 10.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
# Decision 49: what a stop keeps back from the time it has left for the kill, the removal and asking the OS "gone"
# (the grace is cut to what remains after it), and the longest the end of a command's process group may take.
STOP_GRACE_MARGIN_SECONDS = 4.0
REAP_SECONDS = 1.0
# The oldest podman (client and server) whose `create --timeout` the backstop relies on. Not verified against a
# release list: a podman older than this runs without the backstop, and says so (see the module notes).
MIN_TIMEOUT_PODMAN_VERSION = (4, 0)
# The command guard is started like the watchdog itself (host_paths.PYTHON_ISOLATION_FLAGS, repeated here: the
# watchdog loads this file from its own folder).
GUARD_FLAGS = ("-I", "-S", "-B")
CPU_PERIOD_US = 100_000
CLONE_NEWUSER = 0x10000000
DEFAULT_SECCOMP_PATH = "/usr/share/containers/seccomp.json"
# Fixed paths, never a search path (the same rule as process_tree's ps).
PODMAN_PATHS = ("/opt/podman/bin/podman", "/opt/homebrew/bin/podman", "/usr/local/bin/podman", "/usr/bin/podman")
# C3: what the podman client reads from its environment to find its machine or service (a process
# started with a bare environment, such as the detached watchdog, is given these and nothing else).
PODMAN_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                      "CONTAINER_HOST", "CONTAINER_CONNECTION", "CONTAINER_SSHKEY", "CONTAINERS_CONF",
                      "CONTAINERS_REGISTRIES_CONF", "CONTAINERS_STORAGE_CONF")
_ZERO_CAPS = "0000000000000000"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,30}-")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}")
_NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_HOST_PATH = re.compile(r"/[A-Za-z0-9@._:/-]{1,1023}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
_GATE_SCRIPT = (f'while [ ! -e {GATE_FILE} ]; do sleep 0.1; done; rm -f {GATE_FILE}; exec "$@"')
# $1 the podman client, $2 the container, then podman's global options.
_ATTACH_SCRIPT = ('p=$1; n=$2; shift 2; "$p" "$@" logs --follow "$n" || exit 125; '
                  'c=$("$p" "$@" wait "$n") || exit 125; '
                  'case $c in ""|*[!0-9]*) exit 125;; esac; [ "$c" -le 255 ] || exit 125; exit "$c"')
_UNREACHABLE_MARKS = ("cannot connect to podman", "unable to connect to podman")
_NO_SUCH_CONTAINER = ("no such container", "no container with name or id")
_RUNNING_STATES = ("running", "paused", "stopping", "removing", "restarting", "unknown")


# ------------------------------------------------------------------------------------------------
# Values
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ContainerLimits:
    pids: int = 128
    memory_bytes: int = 512 * 1024 * 1024
    cpu_millicores: int = 1000
    tmpfs_bytes: int = 64 * 1024 * 1024

    def problems(self) -> list[str]:
        return [f"{name} must be a positive whole number" for name, value in (
            ("pids", self.pids), ("memory_bytes", self.memory_bytes), ("cpu_millicores", self.cpu_millicores),
            ("tmpfs_bytes", self.tmpfs_bytes)) if type(value) is not int or value <= 0]

    @property
    def cpu_quota_us(self) -> int:
        return self.cpu_millicores * CPU_PERIOD_US // 1000


@dataclass(frozen=True)
class ContainerSpec:
    run_id: str
    image: str
    command: tuple[str, ...]
    workspace: str
    seccomp_profile: str
    workspace_writable: bool = False
    network: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    limits: ContainerLimits = field(default_factory=ContainerLimits)
    # C2: the run's deadline, set on the container (podman --timeout); None sets none.
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class ContainerIdentity:
    name: str
    run_id: str
    project_id: str

    @property
    def labels(self) -> dict[str, str]:
        return {RUN_LABEL: self.run_id, PROJECT_LABEL: self.project_id, KIND_LABEL: RUN_KIND}


@dataclass(frozen=True)
class StopResult:
    """outcome: "gone"; "stop_failed" (not proven gone: hold the slot); "refused" (podman unusable, or a podman
    command's process group could not be ended); "deadline" (the stop's time ran out before the container was
    proven gone: hold the slot)."""

    outcome: str
    detail: str
    exit_code: int | None = None
    refusal: "Refusal | None" = None

    @property
    def gone(self) -> bool:
        return self.outcome == "gone"


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str
    cleanup: StopResult | None = None

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class Launched:
    identity: ContainerIdentity
    container_id: str
    cgroup_path: str
    checks: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class GoneState:
    """state: "gone", "present" or "unknown" (never treat unknown as gone)."""

    state: str
    detail: str
    refusal: Refusal | None = None

    @property
    def gone(self) -> bool:
        return self.state == "gone"


@dataclass(frozen=True)
class SweepEntry:
    run_id: str
    name: str
    container_id: str
    outcome: str  # "removed", "stop_failed", "refused" or "skipped"
    detail: str


@dataclass(frozen=True)
class SweepReport:
    entries: tuple[SweepEntry, ...] = ()
    kept: int = 0
    refusal: Refusal | None = None


@dataclass(frozen=True)
class PodmanResult:
    returncode: int
    stdout: str
    stderr: str


class PodmanRunner(Protocol):
    def __call__(self, args: Sequence[str], timeout: float) -> PodmanResult: ...


class HostFiles(Protocol):
    def read(self, paths: Sequence[str]) -> dict[str, str | None]: ...


class HostUnreadable(Exception):
    """The podman host's files could not be read (not the same as a file being absent)."""


class _Refused(Exception):
    def __init__(self, refusal: Refusal):
        super().__init__(refusal.message)
        self.refusal = refusal


class ProcessCleanupError(Exception):
    """A podman command's process group could not be killed or reaped: something it started may still run."""


class _DeadlineHit(Exception):
    """The stop's one deadline ran out (the answer is "deadline")."""


class _Budget:
    """One absolute deadline on the runtime's clock, charged by every command of one stop."""

    def __init__(self, deadline: float, monotonic: Callable[[], float]):
        self.deadline, self._monotonic = deadline, monotonic

    def left(self) -> float:
        return self.deadline - self._monotonic()


# ------------------------------------------------------------------------------------------------
# Running podman and reading the podman host's files
# ------------------------------------------------------------------------------------------------

# Runs one podman command (argv follows the lifeline's and the status pipe's descriptor numbers) as the leader of
# a process group of its own, and keeps it there: when the stop tool's process dies, the pipe it holds the write
# end of closes, the guard reads the end of file and kills the whole group (itself, podman and everything podman
# started), so a SIGKILLed watchdog or service leaves nothing running (decision 49, CS-1). The guard does not
# leave when podman does: it reports podman's exit status on the status pipe and keeps watching the lifeline, so
# what podman left behind is covered until the stop tool ends the group itself (`_end_group`), with the leader
# still unreaped, so the group id is still its own (r4 G1, G2).
_GUARD = (
    "import os, select, signal, subprocess, sys\n"
    "fd, status = int(sys.argv[1]), int(sys.argv[2])\n"
    "try:\n"
    "    child = subprocess.Popen(sys.argv[3:], stdin=subprocess.DEVNULL)\n"
    "except OSError as error:\n"
    "    sys.stderr.write(str(error) + chr(10))\n"
    "    sys.exit(126 if isinstance(error, PermissionError) else 127)\n"
    "def end():\n"
    "    os.killpg(os.getpgrp(), signal.SIGKILL)\n"
    "while child.poll() is None:\n"
    "    if select.select([fd], [], [], 0.02)[0] and not os.read(fd, 1):\n"
    "        end()\n"
    "try:\n"
    "    os.write(status, b'%d\\n' % (child.returncode if child.returncode >= 0 else 128 - child.returncode))\n"
    "    os.close(status)\n"
    "except OSError:\n"
    "    pass\n"
    "while os.read(fd, 1):\n"
    "    pass\n"
    "end()\n")


def _reap(process: subprocess.Popen, seconds: float) -> bool:
    """Wait for the (killed) leader; False when it is still not reapable after `seconds`."""
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _end_group(process: subprocess.Popen) -> None:
    """Kill every process left in the command's group and reap the leader, or raise ProcessCleanupError.

    The group id is the leader's pid, and the leader is still unreaped here (a zombie keeps the id), so the id
    cannot belong to another group (r4 G2); a group already empty (ESRCH) is the usual, good answer. Anything
    else is reported (CS-5)."""
    problem = None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:  # an empty group: nothing left but, at most, the leader itself
        with contextlib.suppress(OSError):
            process.kill()
    except OSError as error:
        problem = f"its process group could not be killed ({error.strerror or type(error).__name__})"
        with contextlib.suppress(OSError):
            process.kill()
    if process.returncode is None and not _reap(process, REAP_SECONDS):
        problem = (problem + "; " if problem else "") + f"its leader was not reaped within {REAP_SECONDS:g} s"
    if problem is not None:
        raise ProcessCleanupError(f"a podman command's processes may still be running: {problem}")


def _await_status(status_read: int, end: float) -> int | None:
    """The command's exit status from the guard; None when the guard ended without one (it could not start the
    command); subprocess.TimeoutExpired once `end` (monotonic) passed."""
    data = b""
    while b"\n" not in data:
        left = end - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired("podman", 0)
        if select.select([status_read], [], [], left)[0]:
            chunk = os.read(status_read, 64)
            if not chunk:
                break
            data += chunk
    try:
        return int(data.strip())
    except ValueError:
        return None


def run_guarded(argv: Sequence[str], timeout: float) -> "PodmanResult":
    """Run one command to the end under the guard: its result, subprocess.TimeoutExpired once `timeout` passed
    (counted from this call, setting up and starting included), or ProcessCleanupError. When this returns or
    raises, nothing the command started is left running, and the kill and the reap are inside `timeout` plus
    at most REAP_SECONDS."""
    end = time.monotonic() + timeout
    open_fds: set[int] = set()

    def close(descriptor: int) -> None:
        if descriptor in open_fds:
            open_fds.discard(descriptor)
            os.close(descriptor)

    try:
        read_end, write_end = os.pipe()
        open_fds.update((read_end, write_end))
        status_read, status_write = os.pipe()
        open_fds.update((status_read, status_write))
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen([sys.executable, *GUARD_FLAGS, "-c", _GUARD, str(read_end),
                                            str(status_write), *argv],
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           pass_fds=(read_end, status_write), start_new_session=True)
            finally:
                close(read_end)
                close(status_write)
            timed_out, code = False, None
            try:
                code = _await_status(status_read, end)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                _end_group(process)
            if timed_out:
                raise subprocess.TimeoutExpired(list(argv), timeout)
            out.seek(0)
            err.seek(0)
            return PodmanResult(process.returncode if code is None else code, out.read().decode("utf-8", "replace"),
                                err.read().decode("utf-8", "replace"))
    finally:
        for descriptor in tuple(open_fds):
            close(descriptor)


class SubprocessPodman:
    """Runs the podman client at a fixed path, each command under `run_guarded` (nothing it starts outlives
    the call); FileNotFoundError when there is none."""

    def __init__(self, podman_path: str | None = None, *, global_args: Sequence[str] = ()):
        self.podman_path = podman_path
        self.global_args = tuple(global_args)

    def path(self) -> str:
        if self.podman_path is not None:
            return self.podman_path
        for candidate in PODMAN_PATHS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise FileNotFoundError("podman")

    def __call__(self, args: Sequence[str], timeout: float) -> PodmanResult:
        path = self.path()
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise FileNotFoundError(path)
        return run_guarded([path, *self.global_args, *args], timeout)


def _check_host_paths(paths: Sequence[str]) -> None:
    for path in paths:
        if not _HOST_PATH.fullmatch(path) or "/../" in path + "/" or "//" in path:
            raise HostUnreadable(f"refusing to read an unexpected path on the podman host: {path!r}")


class LocalHostFiles:
    """Linux: podman runs on this machine, so its cgroup and /proc files are local."""

    def __init__(self, root: str = "/"):
        self.root = root

    def read(self, paths: Sequence[str]) -> dict[str, str | None]:
        _check_host_paths(paths)
        found: dict[str, str | None] = {}
        for path in paths:
            try:
                with open(os.path.join(self.root, path.lstrip("/")), encoding="utf-8") as handle:
                    found[path] = handle.read()
            except FileNotFoundError:
                found[path] = None
            except OSError as error:
                raise HostUnreadable(f"could not read {path} on the podman host: {error.strerror}") from None
        return found


_RECORD = "\x1e"
_ABSENT = "\x18"
_FAILED = "\x15"


class MachineHostFiles:
    """macOS: the files live in podman's Linux VM, read with `podman machine ssh` (read only)."""

    def __init__(self, runner: PodmanRunner, machine: str | None = None, timeout: float = 30.0):
        self.runner = runner
        self.machine = machine
        self.timeout = timeout

    def read(self, paths: Sequence[str], timeout: float | None = None) -> dict[str, str | None]:
        _check_host_paths(paths)
        if not paths:
            return {}
        script = ("for p in " + " ".join(shlex.quote(p) for p in paths) + "; do printf '\\036%s\\n' \"$p\"; "
                  "if [ -e \"$p\" ]; then cat \"$p\" || printf '\\025'; else printf '\\030'; fi; done")
        args = ["machine", "ssh", *([self.machine] if self.machine else []), script]
        try:
            result = self.runner(args, self.timeout if timeout is None else min(self.timeout, timeout))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostUnreadable(f"podman machine ssh failed: {type(error).__name__}") from None
        if result.returncode != 0:
            raise HostUnreadable("podman machine ssh failed: " + _first_line(result.stderr))
        found: dict[str, str | None] = {}
        for record in result.stdout.split(_RECORD)[1:]:
            path, _, content = record.partition("\n")
            if content.endswith(_FAILED):
                raise HostUnreadable(f"could not read {path} on the podman host")
            found[path] = None if content == _ABSENT else content
        if set(found) != set(paths):
            raise HostUnreadable("the podman host's answer did not cover every file asked for")
        return found


def default_host_files(runner: PodmanRunner, machine: str | None = None) -> HostFiles:
    return MachineHostFiles(runner, machine) if sys.platform == "darwin" else LocalHostFiles()


# ------------------------------------------------------------------------------------------------
# The seccomp profile: the podman host's default with nested user namespaces denied
# ------------------------------------------------------------------------------------------------

def derive_seccomp_profile(default_profile: Mapping) -> dict:
    """Spike PC-2 gap 1: the default allows `unshare -Ur` (fake root). Remove unconditional clone,
    clone3 and unshare; allow clone/unshare only without CLONE_NEWUSER; deny them with it (EPERM);
    clone3 answers ENOSYS so libc falls back to clone, whose flags seccomp can see."""
    profile = copy.deepcopy(dict(default_profile))
    removed = False
    for rule in profile.get("syscalls", []):
        if rule.get("action") == "SCMP_ACT_ALLOW" and not rule.get("args") and not rule.get("includes"):
            names = rule.get("names", [])
            kept = [name for name in names if name not in ("clone", "clone3", "unshare")]
            if len(kept) != len(names):
                removed = removed or "clone" in names
                rule["names"] = kept
    if not removed:
        raise ValueError("the default seccomp profile has no unconditional allow rule for clone to restrict")

    def masked(value: int) -> list[dict]:
        # The flags are argument 0 of clone and unshare on x86_64 and arm64 (s390x differs: not supported).
        return [{"index": 0, "value": CLONE_NEWUSER, "valueTwo": value, "op": "SCMP_CMP_MASKED_EQ"}]

    profile["syscalls"] = [rule for rule in profile["syscalls"] if rule.get("names")] + [
        {"names": ["clone", "unshare"], "action": "SCMP_ACT_ALLOW", "args": masked(0)},
        {"names": ["clone", "unshare"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1, "args": masked(CLONE_NEWUSER)},
        {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
    ]
    return profile


def read_default_seccomp_profile(host_files: HostFiles) -> dict | Refusal:
    try:
        text = host_files.read([DEFAULT_SECCOMP_PATH])[DEFAULT_SECCOMP_PATH]
    except HostUnreadable as error:
        return Refusal("seccomp_unreadable", str(error))
    if text is None:
        return Refusal("seccomp_unreadable", f"the podman host has no {DEFAULT_SECCOMP_PATH}")
    try:
        return json.loads(text)
    except ValueError:
        return Refusal("seccomp_unreadable", f"{DEFAULT_SECCOMP_PATH} on the podman host is not JSON")


# ------------------------------------------------------------------------------------------------
# The runtime
# ------------------------------------------------------------------------------------------------

def _tightest(values: Sequence[str | None], *, cpu: bool = False) -> str | None:
    """The effective cgroup v2 limit along a path: the smallest value ("max" is no limit); for
    cpu.max the smallest quota/period share. None when a level's file is missing or unreadable."""
    best: tuple[float, str] | None = None
    for raw in values:
        text = (raw or "").strip()
        if not text:
            return None
        try:
            if cpu:
                quota, period = text.split()
                key = float("inf") if quota == "max" else int(quota) / int(period)
            else:
                key = float("inf") if text == "max" else int(text)
        except ValueError:
            return None
        if best is None or key < best[0]:
            best = (key, text)
    return None if best is None else best[1]


def _stable_version(text: str) -> tuple[int, int, int, int]:
    """A podman version as a sortable key: (major, minor, patch, 0 for a pre-release or 1 for a release);
    ValueError for anything that is not exactly `[v]X.Y.Z[-pre-release]` (r4 V1)."""
    found = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?", text)
    if found is None:
        raise ValueError(f"not a version: {text!r}")
    return int(found[1]), int(found[2]), int(found[3]), 0 if found[4] else 1


def _call_podman(runner: PodmanRunner, args: Sequence[str], timeout: float) -> PodmanResult:
    """One podman command; a podman that is missing, hangs or cannot reach its machine is a _Refused."""
    try:
        result = runner(list(args), timeout)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        raise _Refused(Refusal("podman_missing", "podman is not installed (looked in "
                               + ", ".join(PODMAN_PATHS) + "); containers cannot run")) from None
    except subprocess.TimeoutExpired:
        raise _Refused(Refusal("podman_timeout", f"podman did not answer `{args[0]}` within "
                               f"{timeout:g} s; its machine may be stuck")) from None
    except ProcessCleanupError as error:
        raise _Refused(Refusal("podman_cleanup_failed", str(error))) from None
    except OSError as error:
        raise _Refused(Refusal("podman_unavailable", f"podman could not be run: {error.strerror}")) from None
    if result.returncode == 125 and any(mark in result.stderr.lower() for mark in _UNREACHABLE_MARKS):
        raise _Refused(Refusal("podman_unavailable", "podman is installed but not reachable: its machine or "
                               "service is stopped (start it with `podman machine start`)"))
    return result


def _listing(runner: PodmanRunner, label: str, timeout: float) -> list[dict] | Refusal:
    """`podman ps --all` filtered on one label (`key=value`): its rows with a labels map, or a Refusal."""
    try:
        listing = _call_podman(runner, ["ps", "--all", f"--filter=label={label}", "--format=json"], timeout)
    except _Refused as refused:
        return refused.refusal
    if listing.returncode != 0:
        return Refusal("podman_failed", "podman could not list containers: " + _first_line(listing.stderr))
    try:
        rows = json.loads(listing.stdout or "[]")
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        return Refusal("podman_failed", "podman's container list was not readable")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("Labels"), dict)]


def list_runs(runner: PodmanRunner, *, command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> list[dict] | Refusal:
    """HM-1: every run container on this podman, whatever its project (`cocolo.kind=run`), or a Refusal.

    A listing only: nothing is started, stopped or changed, and no machine is started (a stopped one is
    the `podman_unavailable` refusal)."""
    rows = _listing(runner, f"{KIND_LABEL}={RUN_KIND}", command_timeout)
    if isinstance(rows, Refusal):
        return rows
    return [row for row in rows if row["Labels"].get(KIND_LABEL) == RUN_KIND]


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no message"


class ContainerRuntime:
    def __init__(self, project_id: str, *, name_prefix: str = DEFAULT_NAME_PREFIX,
                 runner: PodmanRunner | None = None, host_files: HostFiles | None = None,
                 machine: str | None = None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS, poll_seconds: float = 0.25):
        if not _ID.fullmatch(project_id or ""):
            raise ValueError("project id must be 1-128 letters, digits or : . _ -")
        if not _PREFIX.fullmatch(name_prefix):
            raise ValueError("name prefix must be lowercase letters, digits and dashes, ending with a dash")
        self.project_id = project_id
        self.name_prefix = name_prefix
        self.runner = runner if runner is not None else SubprocessPodman()
        self.host_files = host_files if host_files is not None else default_host_files(self.runner, machine)
        self.monotonic = monotonic
        self.sleep = sleep
        self.command_timeout = command_timeout
        self.poll_seconds = poll_seconds

    # -- identity (CS-1)
    def identity(self, run_id: str) -> ContainerIdentity:
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError("run id must be 1-128 letters, digits or : . _ -")
        digest = hashlib.sha256(f"{self.project_id}\0{run_id}".encode()).hexdigest()[:32]
        return ContainerIdentity(self.name_prefix + digest, run_id, self.project_id)

    # -- podman
    def _podman(self, args: Sequence[str], timeout: float | None = None, *, budget: _Budget | None = None
                ) -> PodmanResult:
        """One podman command. With a `budget` (a stop's one deadline) it is given only the time left, less
        REAP_SECONDS for ending its process group; no time left, or a command the deadline cut short, is the
        stop's `_DeadlineHit`, not a hung podman."""
        limit = timeout or self.command_timeout
        cut = False
        if budget is not None:
            room = budget.left() - REAP_SECONDS
            if room <= 0:
                raise _DeadlineHit(f"no time was left to run `podman {args[0]}`")
            if room < limit:
                limit, cut = room, True
        try:
            return _call_podman(self.runner, args, limit)
        except _Refused as refused:
            if cut and refused.refusal.reason == "podman_timeout":
                raise _DeadlineHit(f"`podman {args[0]}` was cut short by the deadline") from None
            raise

    def _inspect(self, ref: str, budget: _Budget | None = None) -> dict | None:
        result = self._podman(["container", "inspect", ref], budget=budget)
        if result.returncode != 0:
            if any(mark in result.stderr.lower() for mark in _NO_SUCH_CONTAINER):
                return None
            raise _Refused(Refusal("podman_failed", "podman could not inspect the container: "
                                   + _first_line(result.stderr)))
        try:
            rows = json.loads(result.stdout)
            return rows[0] if rows else None
        except (ValueError, IndexError, TypeError):
            raise _Refused(Refusal("podman_failed", "podman's inspect answer was not readable")) from None

    # -- validation and arguments (CS-1)
    def _spec_problems(self, spec: ContainerSpec) -> list[str]:
        problems = []
        if not isinstance(spec.run_id, str) or not _ID.fullmatch(spec.run_id):
            problems.append("the run id must be 1-128 letters, digits or : . _ -")
        if not isinstance(spec.image, str) or not _IMAGE.fullmatch(spec.image):
            problems.append("the image must be a plain image reference")
        if not spec.command or not all(isinstance(part, str) and "\0" not in part for part in spec.command):
            problems.append("the command must be a non-empty list of strings")
        workspace = spec.workspace
        if not isinstance(workspace, str) or not os.path.isabs(workspace):
            problems.append("the workspace must be an absolute path")
        elif any(char in workspace for char in ",:\n\0"):
            problems.append("the workspace path must not contain , : or a newline")
        elif os.path.realpath(workspace) != workspace or not os.path.isdir(workspace):
            problems.append("the workspace must be an existing folder named without symlinks")
        profile = spec.seccomp_profile
        if not isinstance(profile, str) or not os.path.isabs(profile) or "," in profile or not os.path.isfile(profile):
            problems.append("the seccomp profile must be an existing file named by an absolute path")
        if spec.network is not None and not _NETWORK.fullmatch(spec.network):
            problems.append("the network must be a plain network name")
        for name, value in dict(spec.environment).items():
            if not _ENV_NAME.fullmatch(str(name)) or not isinstance(value, str) or "\0" in value:
                problems.append(f"environment entry {name!r} is not a plain NAME=value")
        if "HOME" in dict(spec.environment):
            problems.append("environment entry HOME is set by the profile")
        timeout = spec.timeout_seconds
        if timeout is not None and (type(timeout) is not int or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
            problems.append(f"the timeout must be a whole number of seconds from 1 to {MAX_TIMEOUT_SECONDS}")
        if not isinstance(spec.limits, ContainerLimits):
            problems.append("the limits must be ContainerLimits")
        else:
            problems.extend(spec.limits.problems())
        return problems

    def create_arguments(self, spec: ContainerSpec, identity: ContainerIdentity) -> list[str]:
        """The hardened profile (spike, PC-1..4). Every option uses the --name=value form."""
        limits = spec.limits
        uid, gid = CONTAINER_USER
        args = [
            "create", f"--name={identity.name}",
            *(f"--label={key}={value}" for key, value in identity.labels.items()),
            "--pull=never", "--read-only",
            f"--tmpfs=/tmp:size={limits.tmpfs_bytes}",
            f"--tmpfs={CONTAINER_HOME}:size={limits.tmpfs_bytes},mode=0700,U",
            f"--env=HOME={CONTAINER_HOME}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            f"--security-opt=seccomp={spec.seccomp_profile}",
            f"--userns=keep-id:uid={uid},gid={gid}",
            f"--pids-limit={limits.pids}",
            f"--memory={limits.memory_bytes}", f"--memory-swap={limits.memory_bytes}",
            f"--cpu-period={CPU_PERIOD_US}", f"--cpu-quota={limits.cpu_quota_us}",
            f"--network={spec.network or 'none'}",
            f"--volume={spec.workspace}:{WORKSPACE_TARGET}:{'rw' if spec.workspace_writable else 'ro'}",
            "--stop-signal=SIGTERM",
            *([f"--timeout={spec.timeout_seconds}"] if spec.timeout_seconds is not None else []),
            *(f"--env={name}={value}" for name, value in dict(spec.environment).items()),
            "--entrypoint=" + json.dumps(["/bin/sh", "-c", _GATE_SCRIPT, "cocolo-gate"]),
            spec.image, *spec.command,
        ]
        return args

    # -- launch (CS-1, CS-2)
    def launch(self, spec: ContainerSpec, record: Callable[[ContainerIdentity], None]) -> Launched | Refusal:
        problems = self._spec_problems(spec)
        if problems:
            return Refusal("invalid_spec", "the container request is invalid: " + "; ".join(problems))
        identity = self.identity(spec.run_id)
        try:
            if spec.network is not None:
                refusal = self._network_refusal(spec.network, identity)
                if refusal is not None:
                    return refusal
            try:
                record(identity)
            except Exception as error:  # the caller's store: refuse rather than run unrecorded
                return Refusal("record_failed", "the container's identity could not be recorded, so it was not "
                               f"created: {type(error).__name__}: {_first_line(str(error))}")
            backstop, note = self._timeout_backstop(spec)
            arguments = self.create_arguments(spec, identity)
            if not backstop:  # a podman that does not take `--timeout`: the run goes without it (and says so)
                arguments = [argument for argument in arguments if not argument.startswith("--timeout=")]
            created = self._podman(arguments)
            if created.returncode != 0:
                text = created.stderr.lower()
                if "already in use" in text:
                    return Refusal("name_in_use", f"a container named {identity.name} already exists; it was "
                                   "left alone")
                cleanup = self.stop(identity.name, grace_seconds=0)
                if "image not known" in text or "no such image" in text:
                    return Refusal("image_missing", f"the image {spec.image} is not present (it is never pulled "
                                   "automatically)", cleanup)
                return Refusal("create_failed", "podman could not create the container: "
                               + _first_line(created.stderr), cleanup)
            started = self._podman(["start", identity.name])
            if started.returncode != 0:
                return Refusal("start_failed", "podman could not start the container: " + _first_line(started.stderr),
                               self.stop(identity.name, grace_seconds=0))
            return self._check_and_open(spec, identity, backstop, note)
        except _Refused as refused:
            return refused.refusal

    def _timeout_backstop(self, spec: ContainerSpec) -> tuple[bool, str]:
        """Decision 49: whether the run's deadline can also be podman's own (`create --timeout`), and the plain
        line that says so, or why not ("" when the spec has no deadline). Both the client and the server it
        talks to must be at least MIN_TIMEOUT_PODMAN_VERSION; a version that cannot be read is not supported."""
        if spec.timeout_seconds is None:
            return False, ""
        try:
            result = self._podman(["version", "--format=json"])
        except _Refused as refused:
            if refused.refusal.reason == "podman_cleanup_failed":  # something it started may still run: a failure
                raise
            return False, ("skipped: podman's version could not be asked, so its --timeout cannot be relied on ("
                           + refused.refusal.reason + ")")
        try:
            document = json.loads(result.stdout) if result.returncode == 0 else None
            found = {side: str(document[side]["Version"]) for side in ("Client", "Server")}
            numbers = {side: _stable_version(text) for side, text in found.items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            return False, ("skipped: podman's client and server versions could not both be read, so its --timeout "
                           "cannot be relied on ("
                           + _first_line(result.stderr if result.returncode else "unreadable answer") + ")")
        old = [f"{side.lower()} {found[side]}" for side, number in numbers.items()
               if number < (*MIN_TIMEOUT_PODMAN_VERSION, 0, 1)]
        if old:
            return False, (f"skipped: podman {', '.join(old)} is older than "
                           f"{'.'.join(map(str, MIN_TIMEOUT_PODMAN_VERSION))}, so --timeout is not relied on")
        return True, "applied (podman " + ", ".join(f"{side.lower()} {text}" for side, text in found.items()) + ")"

    def _network_refusal(self, network: str, identity: ContainerIdentity) -> Refusal | None:
        result = self._podman(["network", "inspect", "--format=json", network])
        if result.returncode != 0:
            return Refusal("network_missing", f"the network {network} does not exist")
        try:
            rows = json.loads(result.stdout)
            internal = rows[0].get("internal") is True and len(rows) == 1
            labels = rows[0].get("labels") or {}
        except (ValueError, IndexError, AttributeError, TypeError):
            internal, labels = False, {}
        if not internal:
            return Refusal("network_not_internal", f"the network {network} is not an internal (per-run) network; "
                           "a container gets no network or an internal one")
        # C4: the per-run network is this run's own, by its labels.
        if (not isinstance(labels, dict) or labels.get(RUN_LABEL) != identity.run_id
                or labels.get(PROJECT_LABEL) != identity.project_id):
            return Refusal("network_not_this_run", f"the network {network} is not this run's own (its "
                           f"{RUN_LABEL} and {PROJECT_LABEL} labels do not name this run); it was not used")
        return None

    def _check_and_open(self, spec: ContainerSpec, identity: ContainerIdentity, backstop: bool = True,
                        note: str = "") -> Launched | Refusal:
        cgroup_path, container_id, checks = "", "", []
        try:
            info = self._inspect(identity.name)
            if info is None:
                problems = ["the container vanished right after it started"]
            else:
                cgroup_path = (info.get("State") or {}).get("CgroupPath") or ""
                container_id = str(info.get("Id") or "")
                checks, problems = self._start_checks(spec, identity, info, backstop)
            if not problems:
                # Every check passed: let the command run.
                opened = self._podman(["exec", identity.name, "touch", GATE_FILE])
                if opened.returncode != 0:
                    problems = ["the start gate could not be opened: " + _first_line(opened.stderr)]
        except _Refused as refused:
            problems = [refused.refusal.message]
        if problems:
            cleanup = self.stop(identity.name, grace_seconds=0, cgroup_path=cgroup_path or None)
            return Refusal("start_check_failed", "the container was refused and removed because its start check "
                           "failed: " + "; ".join(problems), cleanup)
        return Launched(identity, container_id, cgroup_path,
                        tuple(checks) + (("timeout backstop", note),) if note else tuple(checks))

    def _start_checks(self, spec: ContainerSpec, identity: ContainerIdentity,
                      info: Mapping, backstop: bool = True) -> tuple[list[tuple[str, str]], list[str]]:
        checks: list[tuple[str, str]] = []
        problems: list[str] = []

        def expect(label: str, found: str | None, wanted: str) -> None:
            checks.append((label, "" if found is None else found))
            if found != wanted:
                problems.append(f"{label} is {found if found is not None else 'missing'}, expected {wanted}")

        state = info.get("State") or {}
        host = info.get("HostConfig") or {}
        if state.get("Status") != "running":
            problems.append(f"the container is {state.get('Status') or 'in an unknown state'}, not running")
            return checks, problems
        cgroup_path, pid = state.get("CgroupPath") or "", state.get("Pid")
        # Configured values (from podman): read-only root, the one mount, the network, the deadline.
        if spec.timeout_seconds is not None and backstop:
            reported = (info.get("Config") or {}).get("Timeout")
            expect("timeout", str(reported) if type(reported) is int else None, str(spec.timeout_seconds))
        if host.get("ReadonlyRootfs") is not True:
            problems.append("the root filesystem is not read-only")
        mounts = [(m.get("Source"), m.get("Destination"), m.get("RW")) for m in info.get("Mounts") or []]
        if mounts != [(spec.workspace, WORKSPACE_TARGET, bool(spec.workspace_writable))]:
            problems.append(f"the mounts are {mounts}, expected only the workspace")
        networks = sorted((info.get("NetworkSettings") or {}).get("Networks") or {})
        if spec.network is None:
            if host.get("NetworkMode") != "none" or networks not in ([], ["none"]):
                problems.append(f"the network mode is {host.get('NetworkMode')} {networks}, expected none")
        elif networks != [spec.network]:
            problems.append(f"the networks are {networks}, expected [{spec.network}]")
        # Effective values (from the kernel, on the podman host).
        if not cgroup_path or type(pid) is not int or pid <= 0:
            problems.append("podman did not report the container's cgroup and process")
            return checks, problems
        limits = spec.limits
        status_path, membership_path = f"/proc/{pid}/status", f"/proc/{pid}/cgroup"
        try:
            process = self.host_files.read([status_path, membership_path])
            # The first process may sit in a child of the container's cgroup (crun's "container"
            # leaf under the systemd scope): every level from the container's cgroup down to it
            # limits it, so the effective value is the tightest one on that path.
            membership = (process.get(membership_path) or "").strip()
            own = membership[len("0::"):] if membership.startswith("0::") and "\n" not in membership else ""
            if own != cgroup_path and not own.startswith(cgroup_path.rstrip("/") + "/"):
                problems.append(f"the container's first process is not inside its cgroup {cgroup_path}")
                return checks, problems
            parts = [part for part in own[len(cgroup_path):].split("/") if part]
            levels = [cgroup_path] + [cgroup_path.rstrip("/") + "/" + "/".join(parts[:depth])
                                      for depth in range(1, len(parts) + 1)]
            names = ("pids.max", "memory.max", "memory.swap.max", "cpu.max")
            files = self.host_files.read([f"/sys/fs/cgroup{level}/{name}" for level in levels for name in names])
        except HostUnreadable as error:
            problems.append(f"the effective limits could not be read: {error}")
            return checks, problems
        values = {name: _tightest([files.get(f"/sys/fs/cgroup{level}/{name}") for level in levels],
                                  cpu=name == "cpu.max") for name in names}
        expect("pids.max", values["pids.max"], str(limits.pids))
        expect("memory.max", values["memory.max"], str(limits.memory_bytes))
        expect("memory.swap.max", values["memory.swap.max"], "0")
        expect("cpu.max", values["cpu.max"], f"{limits.cpu_quota_us} {CPU_PERIOD_US}")
        status = {}
        for line in (process.get(status_path) or "").splitlines():
            key, _, value = line.partition(":")
            status[key.strip()] = value.strip()
        for key in ("CapPrm", "CapEff", "CapBnd"):
            expect(key, status.get(key), _ZERO_CAPS)
        expect("NoNewPrivs", status.get("NoNewPrivs"), "1")
        expect("Seccomp", status.get("Seccomp"), "2")
        # Nested user namespaces must be denied by the kernel, inside the container.
        result = self._podman(["exec", identity.name, "unshare", "-Ur", "true"])
        if result.returncode == 0:
            verdict = "allowed"
        elif "operation not permitted" in (result.stderr + result.stdout).lower() and result.returncode not in (
                125, 126, 127):
            verdict = "denied"
        else:
            verdict = "not checked (" + _first_line(result.stderr) + ")"
        expect("nested user namespace", verdict, "denied")
        return checks, problems

    # -- gone (CS-4)
    def gone(self, ref: str, *, cgroup_path: str | None = None) -> GoneState:
        try:
            return self._gone_state(ref, cgroup_path)
        except _Refused as refused:
            return GoneState("unknown", refused.refusal.message, refused.refusal)
        except ProcessCleanupError as error:
            refusal = Refusal("podman_cleanup_failed", str(error))
            return GoneState("unknown", refusal.message, refusal)

    def _read_host(self, paths: Sequence[str], budget: _Budget | None) -> dict[str, str | None]:
        """The podman host's files; on a podman machine (a command of its own) within a stop's deadline."""
        if budget is None or not isinstance(self.host_files, MachineHostFiles):
            return self.host_files.read(paths)
        room = budget.left() - REAP_SECONDS
        if room <= 0:
            raise _DeadlineHit("no time was left to read the podman host's files")
        return self.host_files.read(paths, timeout=room)

    def _gone_state(self, ref: str, cgroup_path: str | None, budget: _Budget | None = None) -> GoneState:
        info = self._inspect(ref, budget)
        if info is not None:
            status = str((info.get("State") or {}).get("Status") or "unknown")
            if status in _RUNNING_STATES or status not in ("exited", "stopped", "created", "configured"):
                return GoneState("present", f"the container is {status}")
            if not cgroup_path:
                return GoneState("unknown", f"the container is {status} but its cgroup was not recorded")
        elif not cgroup_path:
            return GoneState("gone", "the container no longer exists")
        events_path = f"/sys/fs/cgroup{cgroup_path}/cgroup.events"
        try:
            events = self._read_host([events_path], budget)[events_path]
        except HostUnreadable as error:
            return GoneState("unknown", f"its cgroup could not be read: {error}")
        where = "the container no longer exists" if info is None else "the container has exited"
        if events is None:
            return GoneState("gone", f"{where} and its cgroup is gone")
        fields = dict(line.split(" ", 1) for line in events.splitlines() if " " in line)
        if fields.get("populated", "").strip() == "0":
            return GoneState("gone", f"{where} and its cgroup reports populated 0")
        if fields.get("populated", "").strip() == "1":
            return GoneState("present", f"{where} but its cgroup still reports populated 1")
        return GoneState("unknown", "its cgroup's events could not be understood")

    # -- stop (CS-3, CS-4)
    def stop(self, ref: str, *, grace_seconds: int = DEFAULT_GRACE_SECONDS,
             gone_timeout: float = DEFAULT_GONE_TIMEOUT_SECONDS, cgroup_path: str | None = None,
             labels: Mapping[str, str] | None = None, deadline: float | None = None) -> StopResult:
        """Stop and remove the container; with `labels`, only one that carries every one of them (C4).

        Decision 49: `deadline` is the stop's ONE absolute deadline, on this runtime's clock (`monotonic`): every
        command, wait and reap of the stop is charged to it, and a stop that runs out of it answers "deadline".
        None: the stop gives itself the grace, the gone timeout and four commands' worth of time."""
        if deadline is None:
            deadline = self.monotonic() + (grace_seconds if type(grace_seconds) is int else 0) + gone_timeout \
                + 4 * self.command_timeout
        budget = _Budget(deadline, self.monotonic)
        try:
            return self._stop(ref, grace_seconds, gone_timeout, cgroup_path, labels, budget)
        except _Refused as refused:
            return StopResult("refused", refused.refusal.message + "; the container is not known to be gone",
                              refusal=refused.refusal)
        except ProcessCleanupError as error:
            refusal = Refusal("podman_cleanup_failed", str(error))
            return StopResult("refused", refusal.message + "; the container is not known to be gone",
                              refusal=refusal)
        except _DeadlineHit as hit:
            return StopResult("deadline", f"the stop's time ran out before the container was proven gone ({hit}); "
                              "hold its slot and tell the owner")

    def _stop(self, ref: str, grace_seconds: int, gone_timeout: float, cgroup_path: str | None,
              labels: Mapping[str, str] | None, budget: _Budget) -> StopResult:
        if type(grace_seconds) is not int or grace_seconds < 0:
            raise ValueError("grace_seconds must be a whole number of seconds, 0 or more")
        if cgroup_path is not None and not _HOST_PATH.fullmatch(cgroup_path):
            raise ValueError("cgroup_path is not a cgroup path")
        info = self._inspect(ref, budget)
        exit_code = None
        removal_error = None
        if info is not None and labels is not None and not self._carries(info, labels):
            return StopResult("stop_failed", f"a container named {ref} exists but its labels are not this run's; it "
                              "was left alone. Hold its slot and tell the owner")
        if info is not None:
            state = info.get("State") or {}
            cgroup_path = cgroup_path or state.get("CgroupPath") or None
            if state.get("Status") in _RUNNING_STATES:
                # SIGTERM to the first process, the grace, then SIGKILL to every process in the container. The
                # grace is what the time left allows once the margin for the kill, the removal and asking the
                # OS is kept back (CS-4), whole seconds.
                grace = min(grace_seconds, max(0, int(budget.left() - STOP_GRACE_MARGIN_SECONDS)))
                self._podman(["stop", f"--time={grace}", ref], timeout=grace + self.command_timeout, budget=budget)
            after = self._inspect(ref, budget)
            if after is not None:
                code = (after.get("State") or {}).get("ExitCode")
                exit_code = code if type(code) is int else None
                removal_error = self._remove(ref, budget) or removal_error
        end = min(self.monotonic() + gone_timeout, budget.deadline)
        while True:
            state = self._gone_state(ref, cgroup_path, budget)
            if state.gone:
                return StopResult("gone", state.detail, exit_code)
            if self.monotonic() >= end:
                if budget.left() <= 0:
                    raise _DeadlineHit(state.detail)
                return StopResult("stop_failed", f"not gone {gone_timeout:g} s after the kill: {state.detail}"
                                  + (f"; podman rm said: {removal_error}" if removal_error else "")
                                  + "; hold its slot and tell the owner", exit_code)
            self.sleep(max(0.0, min(self.poll_seconds, budget.left())))
            again = self._inspect(ref, budget)
            if again is not None:
                if labels is not None and not self._carries(again, labels):
                    return StopResult("stop_failed", f"a container named {ref} that is not this run's appeared; it "
                                      "was left alone. Hold its slot and tell the owner", exit_code)
                removal_error = self._remove(ref, budget) or removal_error

    @staticmethod
    def _carries(info: Mapping, labels: Mapping[str, str]) -> bool:
        found = (info.get("Config") or {}).get("Labels")
        return isinstance(found, dict) and all(found.get(name) == value for name, value in labels.items())

    def attach_arguments(self, ref: str) -> list[str]:
        """C2: a local command that streams the container's output (`podman logs --follow`, from its
        start, so nothing written before it began is lost) and then exits with the container's exit
        code (`podman wait`), or 125 when either cannot be read. Only for the subprocess runner."""
        if not isinstance(self.runner, SubprocessPodman):
            raise ValueError("attaching needs the podman client at a fixed path")
        if not isinstance(ref, str) or not _ID.fullmatch(ref):
            raise ValueError("the container reference is not a plain name")
        return ["/bin/sh", "-c", _ATTACH_SCRIPT, "cocolo-attach", self.runner.path(), ref, *self.runner.global_args]

    def _remove(self, ref: str, budget: _Budget | None = None) -> str | None:
        """Kill whatever is left in the container and remove it; podman's own words when it said it could not."""
        result = self._podman(["rm", "--force", "--time=0", ref], budget=budget)
        if result.returncode != 0 and not any(mark in result.stderr.lower() for mark in _NO_SUCH_CONTAINER):
            return _first_line(result.stderr)
        return None

    # -- health monitor check 1 (CS-5), and C4's recovery listing
    def list_project(self) -> list[dict] | Refusal:
        """Every container carrying this project's label (`podman ps --all`), or a Refusal (podman unusable)."""
        rows = _listing(self.runner, f"{PROJECT_LABEL}={self.project_id}", self.command_timeout)
        if isinstance(rows, Refusal):
            return rows
        return [row for row in rows if row["Labels"].get(PROJECT_LABEL) == self.project_id]

    def sweep_orphans(self, live_run_ids: Iterable[str], *, grace_seconds: int = DEFAULT_GRACE_SECONDS,
                      gone_timeout: float = DEFAULT_GONE_TIMEOUT_SECONDS) -> SweepReport:
        rows = self.list_project()
        if isinstance(rows, Refusal):
            return SweepReport(refusal=rows)
        return self.stop_orphans(rows, live_run_ids, grace_seconds=grace_seconds, gone_timeout=gone_timeout)

    def stop_orphans(self, rows: Sequence[Mapping], live_run_ids: Iterable[str], *,
                     grace_seconds: int = DEFAULT_GRACE_SECONDS,
                     gone_timeout: float = DEFAULT_GONE_TIMEOUT_SECONDS) -> SweepReport:
        """Stop each listed run container of this project whose run id is not live; report every one."""
        live = set(live_run_ids)
        entries: list[SweepEntry] = []
        kept = 0
        for row in rows:
            labels = row.get("Labels") if isinstance(row, dict) else None
            if not isinstance(labels, dict) or labels.get(PROJECT_LABEL) != self.project_id:
                continue  # not this project's: never touched
            run_id = labels.get(RUN_LABEL)
            names = row.get("Names") or []
            name = names[0] if isinstance(names, list) and len(names) == 1 else ""
            container_id = str(row.get("Id") or "")
            try:
                expected = self.identity(run_id).name if isinstance(run_id, str) else None
            except ValueError:
                expected = None
            if expected is None or name != expected or not _CONTAINER_ID.fullmatch(container_id):
                entries.append(SweepEntry(str(run_id), str(name), container_id, "skipped",
                                          "carries this project's label but is not a COCOLO run's container; "
                                          "left alone"))
                continue
            if run_id in live:
                kept += 1
                continue
            result = self.stop(container_id, grace_seconds=grace_seconds, gone_timeout=gone_timeout,
                               labels={PROJECT_LABEL: self.project_id, RUN_LABEL: run_id})
            outcome = {"gone": "removed"}.get(result.outcome, result.outcome)
            entries.append(SweepEntry(run_id, name, container_id, outcome,
                                      f"no live run {run_id}: {result.detail}"))
        return SweepReport(tuple(entries), kept)
