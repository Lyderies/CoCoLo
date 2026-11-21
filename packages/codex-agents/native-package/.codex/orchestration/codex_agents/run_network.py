"""A run's own internal podman network, whose only way out is COCOLO's allowlist proxy.

Owner decision 2026-11-21 (PLAN-0.4.0.md, "Containment decided"): each run has "a per-run network
allowing only its provider". Evidence: RESEARCH-0.4.0-podman-spike.md PC-3 (the default rootless
network reaches the internet and the Mac's loopback; an --internal network plus an allowlist
CONNECT proxy allowed only the providers; children on one internal network reach each other, so
one network per run; both clients honour HTTPS_PROXY; the proxy log is the record).

Requirements:
- CN-1 A run's network is created `--internal --disable-dns` with the labels `cocolo.run`,
  `cocolo.project`, `cocolo.role=run-network` and `cocolo.provider`, named from the project and
  run id, and never shared: a second request for a run whose network exists is refused. It is
  removed when the run is gone (`release_run`: the proxy leaves it first, so the run is cut off
  even when a container still holds the network, which is then kept and reported "in_use"), and by
  the health monitor's sweep (`sweep_networks`) when its run is not live. `network rm` is never
  forced (that would remove containers).
- CN-2 One proxy container per project (`egress_proxy.py`, run isolated with `python3 -I -c`) on
  the project's own egress network, hardened (read-only root, cap-drop ALL, no-new-privileges,
  keep-id user, pids/memory/cpu limits, only the records folder mounted, never pulled), connected
  to every run network of the project. Its policy, derived from podman's own labels (the run
  networks the proxy is connected to), gives each run its provider's hosts only (PROVIDER_HOSTS).
  The proxy carries `cocolo.proxy=<project>`, never `cocolo.project`, so the container sweep
  never sees it.
- CN-3 `records()` reads the proxy's record (egress_proxy.read_records).
- CN-4 `client_spec` gives a client's ContainerSpec its own network and the proxy variables
  (HTTPS_PROXY, HTTP_PROXY, NO_PROXY and their lower-case forms); a caller's own *_PROXY variable,
  another run's network or no network is refused. Commands-only runs do not use it and keep
  `--network none` (container_runtime's default).
- CN-5 Fail closed: no proxy, no network. A run network is handed out only after the proxy
  answers on its port, is connected to that network, and has confirmed (by digest) the policy
  naming it; any failure undoes what was made and refuses plainly. A network that came out not
  internal or with DNS is removed and refused. Nothing ever falls back to podman's default
  network: a child's only path out is the proxy, so when the proxy is down the child reaches
  nothing. Every operation answers a plain Refusal when podman is missing, hangs or is stopped.

The caller's duties (not done here): call `release_run` after the run's container is gone, keep
`sweep_networks`'s live set as complete as the container sweep's, run `ensure_proxy` from the
health monitor (a stopped proxy is replaced and reconnected), and keep the records folder
owner-only.

Standard library only.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Iterable, Sequence

from . import egress_proxy
from .container_runtime import (
    CONTAINER_USER, CPU_PERIOD_US, PODMAN_PATHS, PROJECT_LABEL, RUN_LABEL, ContainerSpec, PodmanResult, PodmanRunner,
    Refusal, SubprocessPodman,
)

ROLE_LABEL = "cocolo.role"
PROVIDER_LABEL = "cocolo.provider"
PROXY_LABEL = "cocolo.proxy"
DIGEST_LABEL = "cocolo.proxy.digest"
RUN_ROLE = "run-network"
EGRESS_ROLE = "egress"
PROXY_ROLE = "egress-proxy"
DEFAULT_NAME_PREFIX = "cocolo-"
# Each provider's hosts (spike PC-3: unauthenticated Claude contacted only api.anthropic.com;
# Codex used chatgpt.com and auth.openai.com, and api.openai.com was denied without harm).
PROVIDER_HOSTS = {
    "claude": ("api.anthropic.com",),
    "codex": ("auth.openai.com", "chatgpt.com"),
}
NO_PROXY = "localhost,127.0.0.1,::1"
PROXY_PIDS = 1024  # threads count: two per open tunnel, at most 256 tunnels
PROXY_MEMORY_BYTES = 256 * 1024 * 1024
PROXY_TMPFS_BYTES = 16 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_READY_TIMEOUT_SECONDS = 10.0

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,30}-")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}")
_UNREACHABLE_MARKS = ("cannot connect to podman", "unable to connect to podman")
_NOT_FOUND = ("not found", "no such network", "unable to find network")
_NO_SUCH_CONTAINER = ("no such container", "no container with name or id")
_IN_USE = ("being used", "associated containers", "in use")


# ------------------------------------------------------------------------------------------------
# Values
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunNetwork:
    run_id: str
    name: str
    provider: str
    subnet: str
    proxy_address: str
    allow: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return True

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_address}:{egress_proxy.PROXY_PORT}"

    @property
    def environment(self) -> dict[str, str]:
        url = self.proxy_url
        return {"HTTPS_PROXY": url, "https_proxy": url, "HTTP_PROXY": url, "http_proxy": url,
                "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY}


@dataclass(frozen=True)
class ProxyReady:
    name: str
    egress_network: str
    connected: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class NetworkRelease:
    """outcome: "removed", "absent", "in_use" (a container still holds it: kept, cut off),
    "skipped" (not this run's network: left alone) or "refused" (podman unusable)."""

    run_id: str
    name: str
    outcome: str
    detail: str
    refusal: Refusal | None = None


@dataclass(frozen=True)
class NetworkSweepEntry:
    run_id: str
    name: str
    outcome: str
    detail: str


@dataclass(frozen=True)
class NetworkSweep:
    entries: tuple[NetworkSweepEntry, ...] = ()
    kept: int = 0
    refusal: Refusal | None = None


class _Refused(Exception):
    def __init__(self, refusal: Refusal):
        super().__init__(refusal.message)
        self.refusal = refusal


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no message"


def _has(text: str, marks: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    return any(mark in lowered for mark in marks)


# ------------------------------------------------------------------------------------------------
# Launch (CN-4)
# ------------------------------------------------------------------------------------------------

def client_spec(spec: ContainerSpec, network: RunNetwork) -> ContainerSpec:
    """A client's spec on its own run network with the proxy variables; anything else is refused."""
    if not isinstance(network, RunNetwork):
        raise ValueError("a client run needs its own run network from prepare_run; it never gets another one")
    if spec.run_id != network.run_id:
        raise ValueError(f"the network {network.name} belongs to run {network.run_id}, not {spec.run_id}")
    if spec.network not in (None, network.name):
        raise ValueError(f"the spec already names the network {spec.network}; a client gets only its own")
    environment = dict(spec.environment)
    own = sorted(name for name in environment if str(name).upper().endswith("PROXY"))
    if own:
        raise ValueError("the proxy variables are set by COCOLO, not the caller: " + ", ".join(own))
    environment.update(network.environment)
    return dataclasses.replace(spec, network=network.name, environment=environment)


# ------------------------------------------------------------------------------------------------
# Networks and the proxy
# ------------------------------------------------------------------------------------------------

class RunNetworks:
    def __init__(self, project_id: str, records_dir: str, image: str, *, name_prefix: str = DEFAULT_NAME_PREFIX,
                 runner: PodmanRunner | None = None, command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
                 ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
                 max_record_bytes: int = egress_proxy.DEFAULT_MAX_BYTES,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 poll_seconds: float = 0.1):
        if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
            raise ValueError("project id must be 1-128 letters, digits or : . _ -")
        if not isinstance(name_prefix, str) or not _PREFIX.fullmatch(name_prefix):
            raise ValueError("name prefix must be lowercase letters, digits and dashes, ending with a dash")
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ValueError("the proxy image must be a plain image reference")
        if (not isinstance(records_dir, str) or not os.path.isabs(records_dir)
                or any(char in records_dir for char in ",:\n\0")):
            raise ValueError("the records folder must be an absolute path without , : or a newline")
        if type(max_record_bytes) is not int or max_record_bytes < 1024:
            raise ValueError("max_record_bytes must be 1024 or more")
        self.project_id = project_id
        self.records_dir = records_dir
        self.image = image
        self.name_prefix = name_prefix
        self.runner = runner if runner is not None else SubprocessPodman()
        self.command_timeout = command_timeout
        self.ready_timeout = ready_timeout
        self.max_record_bytes = max_record_bytes
        self.monotonic = monotonic
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        digest = hashlib.sha256(project_id.encode()).hexdigest()[:24]
        self.proxy_name = f"{name_prefix}proxy-{digest}"
        self.egress_name = f"{name_prefix}egress-{digest}"
        self.proxy_source = Path(egress_proxy.__file__).with_suffix(".py").read_text(encoding="utf-8")

    # -- names
    def network_name(self, run_id: str) -> str:
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError("run id must be 1-128 letters, digits or : . _ -")
        digest = hashlib.sha256(f"{self.project_id}\0{run_id}".encode()).hexdigest()[:32]
        return f"{self.name_prefix}net-{digest}"

    def _network_labels(self, run_id: str, provider: str) -> dict[str, str]:
        return {RUN_LABEL: run_id, PROJECT_LABEL: self.project_id, ROLE_LABEL: RUN_ROLE, PROVIDER_LABEL: provider}

    # -- podman
    def _podman(self, args: Sequence[str], timeout: float | None = None) -> PodmanResult:
        try:
            result = self.runner(list(args), timeout or self.command_timeout)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise _Refused(Refusal("podman_missing", "podman is not installed (looked in "
                                   + ", ".join(PODMAN_PATHS) + "); no run network can be made")) from None
        except subprocess.TimeoutExpired:
            raise _Refused(Refusal("podman_timeout", f"podman did not answer `{args[0]}` within "
                                   f"{timeout or self.command_timeout:g} s; its machine may be stuck")) from None
        except OSError as error:
            raise _Refused(Refusal("podman_unavailable", f"podman could not be run: {error.strerror}")) from None
        if result.returncode == 125 and _has(result.stderr, _UNREACHABLE_MARKS):
            raise _Refused(Refusal("podman_unavailable", "podman is installed but not reachable: its machine or "
                                   "service is stopped (start it with `podman machine start`)"))
        return result

    def _quiet(self, args: Sequence[str]) -> None:
        """Best effort (undoing): podman's answer, or its absence, changes nothing."""
        try:
            self._podman(args)
        except _Refused:
            pass

    def _json(self, result: PodmanResult, what: str):
        try:
            return json.loads(result.stdout or "null")
        except ValueError:
            raise _Refused(Refusal("podman_failed", f"podman's answer about {what} was not readable")) from None

    def _inspect_network(self, name: str) -> dict | None:
        result = self._podman(["network", "inspect", "--format=json", name])
        if result.returncode != 0:
            if _has(result.stderr, _NOT_FOUND):
                return None
            raise _Refused(Refusal("podman_failed", f"podman could not inspect the network {name}: "
                                   + _first_line(result.stderr)))
        rows = self._json(result, f"the network {name}")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise _Refused(Refusal("podman_failed", f"podman's answer about the network {name} was not one network"))
        return rows[0]

    def _inspect_container(self, name: str) -> dict | None:
        result = self._podman(["container", "inspect", name])
        if result.returncode != 0:
            if _has(result.stderr, _NO_SUCH_CONTAINER):
                return None
            raise _Refused(Refusal("podman_failed", f"podman could not inspect {name}: " + _first_line(result.stderr)))
        rows = self._json(result, name)
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None

    def _project_networks(self) -> list[dict]:
        result = self._podman(["network", "ls", "--format=json", f"--filter=label={PROJECT_LABEL}={self.project_id}"])
        if result.returncode != 0:
            raise _Refused(Refusal("podman_failed", "podman could not list networks: " + _first_line(result.stderr)))
        rows = self._json(result, "the networks")
        if not isinstance(rows, list):
            raise _Refused(Refusal("podman_failed", "podman's network list was not readable"))
        return [row for row in rows if isinstance(row, dict)
                and (row.get("labels") or {}).get(PROJECT_LABEL) == self.project_id]

    def _run_networks(self) -> list[dict]:
        return [row for row in self._project_networks() if (row.get("labels") or {}).get(ROLE_LABEL) == RUN_ROLE]

    @staticmethod
    def _networks_of(info: dict | None) -> dict:
        networks = ((info or {}).get("NetworkSettings") or {}).get("Networks") or {}
        return networks if isinstance(networks, dict) else {}

    # -- the proxy container (CN-2)
    def _proxy_arguments(self) -> tuple[list[str], str]:
        uid, gid = CONTAINER_USER
        options = [
            f"--name={self.proxy_name}", f"--label={PROXY_LABEL}={self.project_id}", f"--label={ROLE_LABEL}={PROXY_ROLE}",
            "--pull=never", "--read-only", f"--tmpfs=/tmp:size={PROXY_TMPFS_BYTES}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--userns=keep-id:uid={uid},gid={gid}",
            f"--pids-limit={PROXY_PIDS}", f"--memory={PROXY_MEMORY_BYTES}", f"--memory-swap={PROXY_MEMORY_BYTES}",
            f"--cpu-period={CPU_PERIOD_US}", f"--cpu-quota={CPU_PERIOD_US}",
            f"--network={self.egress_name}",
            f"--volume={self.records_dir}:{egress_proxy.RECORDS_TARGET}:rw",
            "--stop-signal=SIGTERM", "--entrypoint=" + json.dumps(["python3"]),
        ]
        command = [self.image, "-I", "-c", self.proxy_source, "--policy", egress_proxy.POLICY_PATH,
                   "--records", egress_proxy.RECORDS_TARGET, "--port", str(egress_proxy.PROXY_PORT),
                   "--max-bytes", str(self.max_record_bytes)]
        digest = hashlib.sha256(json.dumps([options, command]).encode()).hexdigest()
        return ["create", *options, f"--label={DIGEST_LABEL}={digest}", *command], digest

    def _proxy_exec(self, *args: str) -> PodmanResult:
        return self._podman(["exec", self.proxy_name, "python3", "-I", "-c", self.proxy_source, *args])

    def _remove_proxy(self) -> None:
        self._quiet(["rm", "--force", "--time=0", self.proxy_name])

    def _ensure_egress(self) -> None:
        info = self._inspect_network(self.egress_name)
        if info is None:
            created = self._podman(["network", "create", f"--label={PROJECT_LABEL}={self.project_id}",
                                    f"--label={ROLE_LABEL}={EGRESS_ROLE}", self.egress_name])
            if created.returncode != 0:
                raise _Refused(Refusal("proxy_unavailable", "the proxy's egress network could not be created: "
                                       + _first_line(created.stderr)))
            info = self._inspect_network(self.egress_name)
        labels = (info or {}).get("labels") or {}
        if labels.get(PROJECT_LABEL) != self.project_id or labels.get(ROLE_LABEL) != EGRESS_ROLE:
            raise _Refused(Refusal("egress_network_foreign", f"a network named {self.egress_name} exists but is not "
                                   "this project's egress network; it was left alone"))
        if info.get("internal") is not False:
            raise _Refused(Refusal("egress_network_foreign", f"the network {self.egress_name} is internal, so the "
                                   "proxy could reach nothing; it was left alone"))

    def _ensure_proxy(self) -> ProxyReady:
        if os.path.realpath(self.records_dir) != self.records_dir or not os.path.isdir(self.records_dir):
            raise _Refused(Refusal("records_unusable", f"the records folder {self.records_dir} must exist and be "
                                   "named without symlinks"))
        self._ensure_egress()
        arguments, digest = self._proxy_arguments()
        info = self._inspect_container(self.proxy_name)
        if info is not None:
            labels = (info.get("Config") or {}).get("Labels") or {}
            if labels.get(PROXY_LABEL) != self.project_id:
                raise _Refused(Refusal("proxy_name_in_use", f"a container named {self.proxy_name} exists but is not "
                                       "this project's proxy; it was left alone"))
            if (info.get("State") or {}).get("Status") != "running" or labels.get(DIGEST_LABEL) != digest:
                self._remove_proxy()
                if self._inspect_container(self.proxy_name) is not None:
                    raise _Refused(Refusal("proxy_unavailable", f"the old proxy {self.proxy_name} could not be "
                                           "removed"))
                info = None
        if info is None:
            created = self._podman(arguments)
            if created.returncode != 0:
                self._remove_proxy()
                missing = _has(created.stderr, ("image not known", "no such image"))
                raise _Refused(Refusal("proxy_unavailable", (
                    f"the proxy image {self.image} is not present (it is never pulled automatically)" if missing
                    else "the egress proxy could not be created: " + _first_line(created.stderr))))
            started = self._podman(["start", self.proxy_name])
            if started.returncode != 0:
                self._remove_proxy()
                raise _Refused(Refusal("proxy_unavailable", "the egress proxy could not be started: "
                                       + _first_line(started.stderr)))
            info = self._inspect_container(self.proxy_name)
        self._wait_ready()
        # A replaced proxy rejoins every run network of this project (an orphan's is swept later).
        joined = self._networks_of(info)
        for row in self._run_networks():
            name = row.get("name")
            if isinstance(name, str) and name not in joined:
                self._quiet(["network", "connect", name, self.proxy_name])
        policy = self._push_policy()
        return ProxyReady(self.proxy_name, self.egress_name, tuple(sorted(run["run"] for run in policy["runs"])))

    def _wait_ready(self) -> None:
        deadline = self.monotonic() + self.ready_timeout
        while True:
            result = self._proxy_exec("--check", "--port", str(egress_proxy.PROXY_PORT))
            if result.returncode == 0:
                return
            if self.monotonic() >= deadline:
                self._remove_proxy()
                raise _Refused(Refusal("proxy_unavailable", f"the egress proxy did not answer on its port within "
                                       f"{self.ready_timeout:g} s and was removed: " + _first_line(result.stderr)))
            self.sleep(self.poll_seconds)

    def _policy(self) -> dict:
        """Derived from podman: each run network of this project that the proxy is connected to."""
        joined = self._networks_of(self._inspect_container(self.proxy_name))
        runs = []
        for row in self._run_networks():
            labels = row.get("labels") or {}
            run_id, provider, name = labels.get(RUN_LABEL), labels.get(PROVIDER_LABEL), row.get("name")
            try:
                expected = self.network_name(run_id)
            except ValueError:
                continue
            if name != expected or name not in joined or provider not in PROVIDER_HOSTS or row.get("internal") is not True:
                continue
            subnets = [entry.get("subnet") for entry in row.get("subnets") or [] if isinstance(entry, dict)]
            runs.append({"run": run_id, "subnets": subnets, "allow": sorted(PROVIDER_HOSTS[provider])})
        try:
            return egress_proxy.validate_policy({"version": egress_proxy.POLICY_VERSION, "runs": runs})
        except ValueError as error:
            raise _Refused(Refusal("proxy_policy_failed", f"the proxy's policy could not be made: {error}")) from None

    def _push_policy(self) -> dict:
        policy = self._policy()
        result = self._proxy_exec("--policy", egress_proxy.POLICY_PATH, "--set-policy", egress_proxy.policy_text(policy))
        if result.returncode != 0 or result.stdout.strip() != egress_proxy.policy_digest(policy):
            raise _Refused(Refusal("proxy_policy_failed", "the proxy did not confirm its new policy: "
                                   + _first_line(result.stderr or result.stdout)))
        return policy

    def ensure_proxy(self) -> ProxyReady | Refusal:
        """The project's proxy running, answering, on every run network, with the current policy."""
        try:
            return self._ensure_proxy()
        except _Refused as refused:
            return refused.refusal

    # -- a run's network (CN-1, CN-2, CN-5)
    def prepare_run(self, run_id: str, provider: str) -> RunNetwork | Refusal:
        if provider not in PROVIDER_HOSTS:
            return Refusal("invalid_request", f"unknown provider {provider!r}: a run network allows one of "
                           + ", ".join(sorted(PROVIDER_HOSTS)))
        try:
            name = self.network_name(run_id)
        except ValueError as error:
            return Refusal("invalid_request", str(error))
        try:
            self._ensure_proxy()
            if self._inspect_network(name) is not None:
                return Refusal("network_in_use", f"run {run_id} already has the network {name}; a network is never "
                               "shared or reused (release it first)")
            labels = self._network_labels(run_id, provider)
            created = self._podman(["network", "create", "--internal", "--disable-dns",
                                    *(f"--label={key}={value}" for key, value in labels.items()), name])
            if created.returncode != 0:
                if _has(created.stderr, ("already",)):
                    return Refusal("network_in_use", f"the network {name} already exists; it was left alone")
                return Refusal("network_failed", "podman could not create the run's network: "
                               + _first_line(created.stderr))
            try:
                return self._join(run_id, provider, name, labels)
            except _Refused:
                self._undo(name)
                raise
        except _Refused as refused:
            return refused.refusal

    def _join(self, run_id: str, provider: str, name: str, labels: dict[str, str]) -> RunNetwork:
        info = self._inspect_network(name) or {}
        found = info.get("labels") or {}
        if info.get("internal") is not True or info.get("dns_enabled") is not False:
            raise _Refused(Refusal("network_not_internal", f"the network {name} came out internal="
                                   f"{info.get('internal')} dns={info.get('dns_enabled')}; it was removed"))
        if any(found.get(key) != value for key, value in labels.items()):
            raise _Refused(Refusal("network_failed", f"the network {name} does not carry its labels; it was removed"))
        subnets = [entry.get("subnet") for entry in info.get("subnets") or [] if isinstance(entry, dict)]
        try:
            subnet = ipaddress.ip_network(subnets[0]) if len(subnets) == 1 else None
        except (TypeError, ValueError):
            subnet = None
        if subnet is None:
            raise _Refused(Refusal("network_failed", f"the network {name} does not have exactly one subnet"))
        joined = self._podman(["network", "connect", name, self.proxy_name])
        if joined.returncode != 0:
            raise _Refused(Refusal("proxy_unavailable", "the proxy could not join the run's network: "
                                   + _first_line(joined.stderr)))
        address = (self._networks_of(self._inspect_container(self.proxy_name)).get(name) or {}).get("IPAddress")
        try:
            inside = ipaddress.ip_address(address) in subnet
        except (TypeError, ValueError):
            inside = False
        if not inside:
            raise _Refused(Refusal("proxy_unavailable", f"the proxy has no address on the network {name}"))
        policy = self._push_policy()
        entry = next((run for run in policy["runs"] if run["run"] == run_id), None)
        if entry is None or entry["subnets"] != [str(subnet)]:
            raise _Refused(Refusal("proxy_policy_failed", f"the proxy's policy does not name run {run_id}"))
        return RunNetwork(run_id, name, provider, str(subnet), str(address), tuple(entry["allow"]))

    def _undo(self, name: str) -> None:
        """Take a half-made run network apart again (best effort; a leftover is swept later)."""
        self._quiet(["network", "disconnect", name, self.proxy_name])
        self._quiet(["network", "rm", name])
        try:
            self._push_policy()
        except _Refused:
            pass

    def release_run(self, run_id: str) -> NetworkRelease:
        try:
            name = self.network_name(run_id)
        except ValueError as error:
            return NetworkRelease(str(run_id), "", "refused", str(error), Refusal("invalid_request", str(error)))
        try:
            info = self._inspect_network(name)
            if info is None:
                return NetworkRelease(run_id, name, "absent", "the run has no network")
            labels = info.get("labels") or {}
            if (labels.get(PROJECT_LABEL), labels.get(RUN_LABEL), labels.get(ROLE_LABEL)) != (
                    self.project_id, run_id, RUN_ROLE):
                return NetworkRelease(run_id, name, "skipped", "the network is not this run's; it was left alone")
            # The proxy leaves first: from here the run reaches nothing, whatever happens next.
            left = self._podman(["network", "disconnect", name, self.proxy_name])
            if left.returncode != 0 and not _has(left.stderr, ("not connected", "no such container", "not found")):
                return NetworkRelease(run_id, name, "refused", "the proxy could not leave the network: "
                                      + _first_line(left.stderr),
                                      Refusal("podman_failed", _first_line(left.stderr)))
            if self._inspect_container(self.proxy_name) is not None:
                try:
                    self._push_policy()
                except _Refused:
                    pass  # the proxy is off the network already; a later push replaces the entry
            removed = self._podman(["network", "rm", name])
            if removed.returncode == 0:
                return NetworkRelease(run_id, name, "removed", "the network was removed")
            if _has(removed.stderr, _IN_USE):
                return NetworkRelease(run_id, name, "in_use", "a container still holds the network; it was kept, "
                                      "cut off from the proxy (remove the run's container, then release again)")
            if _has(removed.stderr, _NOT_FOUND):
                return NetworkRelease(run_id, name, "absent", "the network was already gone")
            return NetworkRelease(run_id, name, "refused", "podman could not remove the network: "
                                  + _first_line(removed.stderr), Refusal("podman_failed", _first_line(removed.stderr)))
        except _Refused as refused:
            return NetworkRelease(run_id, name, "refused", refused.refusal.message, refused.refusal)

    # -- health monitor (CN-1)
    def sweep_networks(self, live_run_ids: Iterable[str]) -> NetworkSweep:
        live = set(live_run_ids)
        try:
            rows = self._run_networks()
        except _Refused as refused:
            return NetworkSweep(refusal=refused.refusal)
        entries: list[NetworkSweepEntry] = []
        kept = 0
        for row in rows:
            labels = row.get("labels") or {}
            run_id, name = labels.get(RUN_LABEL), row.get("name")
            try:
                expected = self.network_name(run_id)
            except ValueError:
                expected = None
            if expected is None or name != expected:
                entries.append(NetworkSweepEntry(str(run_id), str(name), "skipped", "carries this project's label "
                                                 "but is not a COCOLO run's network; left alone"))
                continue
            if run_id in live:
                kept += 1
                continue
            result = self.release_run(run_id)
            entries.append(NetworkSweepEntry(run_id, name, result.outcome, f"no live run {run_id}: {result.detail}"))
        return NetworkSweep(tuple(entries), kept)

    # -- shutdown and the record
    def stop_proxy(self) -> NetworkRelease:
        """Remove this project's proxy and its egress network (run networks are released separately)."""
        try:
            info = self._inspect_container(self.proxy_name)
            if info is not None:
                if ((info.get("Config") or {}).get("Labels") or {}).get(PROXY_LABEL) != self.project_id:
                    return NetworkRelease("", self.proxy_name, "skipped", "not this project's proxy; left alone")
                self._podman(["rm", "--force", "--time=0", self.proxy_name])
                if self._inspect_container(self.proxy_name) is not None:
                    return NetworkRelease("", self.proxy_name, "refused", "the proxy could not be removed",
                                          Refusal("podman_failed", "the proxy could not be removed"))
            egress = self._inspect_network(self.egress_name)
            if egress is not None and (egress.get("labels") or {}).get(ROLE_LABEL) == EGRESS_ROLE and (
                    egress.get("labels") or {}).get(PROJECT_LABEL) == self.project_id:
                removed = self._podman(["network", "rm", self.egress_name])
                if removed.returncode != 0 and not _has(removed.stderr, _NOT_FOUND):
                    return NetworkRelease("", self.egress_name, "in_use", "the egress network was kept: "
                                          + _first_line(removed.stderr))
            return NetworkRelease("", self.proxy_name, "removed", "the proxy and its egress network were removed")
        except _Refused as refused:
            return NetworkRelease("", self.proxy_name, "refused", refused.refusal.message, refused.refusal)

    def records(self, run_id: str | None = None) -> list[dict]:
        """The proxy's record of every attempt, oldest first (CN-3)."""
        return egress_proxy.read_records(self.records_dir, run_id)
