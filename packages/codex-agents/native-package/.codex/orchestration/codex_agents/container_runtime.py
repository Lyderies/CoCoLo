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

The caller's duties (not done here): include every run whose identity it recorded in the live set
it gives the sweep (a launch between its snapshot and the listing would otherwise be swept), keep
the slot of any run whose stop is not "gone", and keep the seccomp profile file owner-only.

Standard library only.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
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
    """outcome: "gone"; "stop_failed" (not proven gone: hold the slot); "refused" (podman unusable)."""

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


# ------------------------------------------------------------------------------------------------
# Running podman and reading the podman host's files
# ------------------------------------------------------------------------------------------------

class SubprocessPodman:
    """Runs the podman client at a fixed path; FileNotFoundError when there is none."""

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
        completed = subprocess.run([self.path(), *self.global_args, *args], capture_output=True, text=True,
                                   timeout=timeout, stdin=subprocess.DEVNULL, check=False)
        return PodmanResult(completed.returncode, completed.stdout, completed.stderr)


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

    def read(self, paths: Sequence[str]) -> dict[str, str | None]:
        _check_host_paths(paths)
        if not paths:
            return {}
        script = ("for p in " + " ".join(shlex.quote(p) for p in paths) + "; do printf '\\036%s\\n' \"$p\"; "
                  "if [ -e \"$p\" ]; then cat \"$p\" || printf '\\025'; else printf '\\030'; fi; done")
        args = ["machine", "ssh", *([self.machine] if self.machine else []), script]
        try:
            result = self.runner(args, self.timeout)
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
    def _podman(self, args: Sequence[str], timeout: float | None = None) -> PodmanResult:
        try:
            result = self.runner(list(args), timeout or self.command_timeout)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise _Refused(Refusal("podman_missing", "podman is not installed (looked in "
                                   + ", ".join(PODMAN_PATHS) + "); containers cannot run")) from None
        except subprocess.TimeoutExpired:
            raise _Refused(Refusal("podman_timeout", f"podman did not answer `{args[0]}` within "
                                   f"{timeout or self.command_timeout:g} s; its machine may be stuck")) from None
        except OSError as error:
            raise _Refused(Refusal("podman_unavailable", f"podman could not be run: {error.strerror}")) from None
        if result.returncode == 125 and any(mark in result.stderr.lower() for mark in _UNREACHABLE_MARKS):
            raise _Refused(Refusal("podman_unavailable", "podman is installed but not reachable: its machine or "
                                   "service is stopped (start it with `podman machine start`)"))
        return result

    def _inspect(self, ref: str) -> dict | None:
        result = self._podman(["container", "inspect", ref])
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
            created = self._podman(self.create_arguments(spec, identity))
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
            return self._check_and_open(spec, identity)
        except _Refused as refused:
            return refused.refusal

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

    def _check_and_open(self, spec: ContainerSpec, identity: ContainerIdentity) -> Launched | Refusal:
        cgroup_path, container_id, checks = "", "", []
        try:
            info = self._inspect(identity.name)
            if info is None:
                problems = ["the container vanished right after it started"]
            else:
                cgroup_path = (info.get("State") or {}).get("CgroupPath") or ""
                container_id = str(info.get("Id") or "")
                checks, problems = self._start_checks(spec, identity, info)
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
        return Launched(identity, container_id, cgroup_path, tuple(checks))

    def _start_checks(self, spec: ContainerSpec, identity: ContainerIdentity,
                      info: Mapping) -> tuple[list[tuple[str, str]], list[str]]:
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
        if spec.timeout_seconds is not None:
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

    def _gone_state(self, ref: str, cgroup_path: str | None) -> GoneState:
        info = self._inspect(ref)
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
            events = self.host_files.read([events_path])[events_path]
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
             labels: Mapping[str, str] | None = None) -> StopResult:
        """Stop and remove the container; with `labels`, only one that carries every one of them (C4)."""
        try:
            return self._stop(ref, grace_seconds, gone_timeout, cgroup_path, labels)
        except _Refused as refused:
            return StopResult("refused", refused.refusal.message + "; the container is not known to be gone",
                              refusal=refused.refusal)

    def _stop(self, ref: str, grace_seconds: int, gone_timeout: float, cgroup_path: str | None,
              labels: Mapping[str, str] | None = None) -> StopResult:
        if type(grace_seconds) is not int or grace_seconds < 0:
            raise ValueError("grace_seconds must be a whole number of seconds, 0 or more")
        if cgroup_path is not None and not _HOST_PATH.fullmatch(cgroup_path):
            raise ValueError("cgroup_path is not a cgroup path")
        info = self._inspect(ref)
        exit_code = None
        if info is not None and labels is not None and not self._carries(info, labels):
            return StopResult("stop_failed", f"a container named {ref} exists but its labels are not this run's; it "
                              "was left alone. Hold its slot and tell the owner")
        if info is not None:
            state = info.get("State") or {}
            cgroup_path = cgroup_path or state.get("CgroupPath") or None
            if state.get("Status") in _RUNNING_STATES:
                # SIGTERM to the first process, the grace, then SIGKILL to every process in the container.
                self._podman(["stop", f"--time={grace_seconds}", ref], timeout=grace_seconds + self.command_timeout)
            after = self._inspect(ref)
            if after is not None:
                code = (after.get("State") or {}).get("ExitCode")
                exit_code = code if type(code) is int else None
                self._remove(ref)
        deadline = self.monotonic() + gone_timeout
        while True:
            state = self._gone_state(ref, cgroup_path)
            if state.gone:
                return StopResult("gone", state.detail, exit_code)
            if self.monotonic() >= deadline:
                return StopResult("stop_failed", f"not gone {gone_timeout:g} s after the kill: {state.detail}; "
                                  "hold its slot and tell the owner", exit_code)
            self.sleep(self.poll_seconds)
            again = self._inspect(ref)
            if again is not None:
                if labels is not None and not self._carries(again, labels):
                    return StopResult("stop_failed", f"a container named {ref} that is not this run's appeared; it "
                                      "was left alone. Hold its slot and tell the owner", exit_code)
                self._remove(ref)

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

    def _remove(self, ref: str) -> None:
        """Kill whatever is left in the container and remove it."""
        self._podman(["rm", "--force", "--time=0", ref])

    # -- health monitor check 1 (CS-5), and C4's recovery listing
    def list_project(self) -> list[dict] | Refusal:
        """Every container carrying this project's label (`podman ps --all`), or a Refusal (podman unusable)."""
        try:
            listing = self._podman(["ps", "--all", f"--filter=label={PROJECT_LABEL}={self.project_id}",
                                    "--format=json"])
            if listing.returncode != 0:
                return Refusal("podman_failed", "podman could not list containers: " + _first_line(listing.stderr))
            try:
                rows = json.loads(listing.stdout or "[]")
            except ValueError:
                rows = None
            if not isinstance(rows, list):
                return Refusal("podman_failed", "podman's container list was not readable")
        except _Refused as refused:
            return refused.refusal
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("Labels"), dict)
                and row["Labels"].get(PROJECT_LABEL) == self.project_id]

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
