"""CS-1..CS-6: one run's hardened podman container, its stop, "gone" asked of the OS, the orphan sweep.

Unit tests drive `ContainerRuntime` through a fake podman that derives the container's effective
limits from the arguments it was created with, so a missing flag shows up as a wrong limit, as it
would on a real host. The real-process tests run only when podman is installed, its machine (or
local service) answers, and the small python:3.12-slim image is already present (they never pull);
otherwise they skip with the reason. They create only containers and networks named `cocolo-test-*`
with a per-test project label, and remove exactly those, by name. They never prune.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest

import tests.support  # noqa: F401
from tests.support import SHORT_TMP

from codex_agents import container_runtime as cr

SECONDS_ZERO = "0000000000000000"


# --------------------------------------------------------------------------------------------
# Fake podman
# --------------------------------------------------------------------------------------------

class FakeContainer:
    def __init__(self, cid, name, labels, args):
        self.id = cid
        self.name = name
        self.labels = dict(labels)
        self.args = list(args)
        self.status = "created"
        self.pid = 0
        self.cgroup_path = ""
        self.exit_code = 0
        self.gate_open = False


def option_values(args, flag):
    """Values of `--flag=value` options in a podman argument list."""
    prefix = flag + "="
    return [arg[len(prefix):] for arg in args if arg.startswith(prefix)]


class FakeHostFiles:
    """The podman host's files (the VM on macOS): cgroup files and /proc status."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.unreadable = False
        self.reads: list[tuple[str, ...]] = []

    def read(self, paths):
        self.reads.append(tuple(paths))
        if self.unreadable:
            raise cr.HostUnreadable("the podman host did not answer")
        return {path: self.files.get(path) for path in paths}


class FakePodman:
    """Enough of podman for the runtime: create/start/inspect/exec/stop/rm/ps and network inspect."""

    def __init__(self, host: FakeHostFiles):
        self.host = host
        self.containers: dict[str, FakeContainer] = {}
        self.networks = {"podman": False}
        self.images = {"python:3.12-slim"}
        self.calls: list[tuple[str, ...]] = []
        self.missing = False
        self.unreachable = False
        self.time_out = False
        self.stop_does_nothing = False
        self.rm_fails = False
        self.cgroup_stays_populated = False
        self.start_fails = False
        self.unshare_result = None  # (returncode, stderr) override
        self.effective_overrides: dict[str, str] = {}
        self.status_overrides: dict[str, str] = {}
        self.extra_ps_rows: list[dict] = []
        self.on_create = None
        self._next = 1

    # -- helpers
    def find(self, ref):
        for container in self.containers.values():
            if ref in (container.name, container.id):
                return container
        return None

    def add_container(self, name, labels, status="running", args=()):
        cid = f"{self._next:064x}"
        self._next += 1
        container = FakeContainer(cid, name, labels, args)
        self.containers[name] = container
        if status == "running":
            self._run(container)
        else:
            container.status = status
        return container

    def _run(self, container):
        container.status = "running"
        container.pid = 40000 + self._next
        self._next += 1
        container.cgroup_path = "/machine.slice/libpod-" + container.id + ".scope"
        args = container.args
        base = "/sys/fs/cgroup" + container.cgroup_path
        pids = option_values(args, "--pids-limit")
        memory = option_values(args, "--memory")
        swap = option_values(args, "--memory-swap")
        quota = option_values(args, "--cpu-quota")
        period = option_values(args, "--cpu-period")
        values = {
            "pids.max": pids[0] if pids else "2048",
            "memory.max": memory[0] if memory else "max",
            "memory.swap.max": "0" if memory and swap == memory else "max",
            "cpu.max": f"{quota[0]} {period[0] if period else 100000}" if quota else "max 100000",
        }
        values.update(self.effective_overrides)
        for name, value in values.items():
            self.host.files[f"{base}/{name}"] = value + "\n"
        self.host.files[f"{base}/cgroup.events"] = "populated 1\nfrozen 0\n"
        dropped = "--cap-drop=ALL" in args
        status = {
            "CapPrm": SECONDS_ZERO if dropped else "00000000800405fb",
            "CapEff": SECONDS_ZERO if dropped else "00000000800405fb",
            "CapBnd": SECONDS_ZERO if dropped else "00000000800405fb",
            "NoNewPrivs": "1" if "--security-opt=no-new-privileges" in args else "0",
            "Seccomp": "2",
        }
        status.update(self.status_overrides)
        self.host.files[f"/proc/{container.pid}/status"] = (
            "Name:\tsh\n" + "".join(f"{key}:\t{value}\n" for key, value in status.items()))

    def _end(self, container, code):
        container.status = "exited"
        container.exit_code = code
        if container.pid:
            self.host.files.pop(f"/proc/{container.pid}/status", None)
        container.pid = 0
        base = "/sys/fs/cgroup" + container.cgroup_path
        for path in [p for p in self.host.files if p.startswith(base + "/")]:
            if self.cgroup_stays_populated and path.endswith("cgroup.events"):
                continue
            del self.host.files[path]

    def inspect_json(self, container):
        args = container.args
        network = option_values(args, "--network")
        workspace = [arg for arg in args if arg.startswith("--volume=")]
        mounts = []
        for value in workspace:
            source, target, mode = value[len("--volume="):].rsplit(":", 2)
            mounts.append({"Type": "bind", "Source": source, "Destination": target, "RW": mode == "rw"})
        mode = network[0] if network else "bridge"
        networks = {} if mode == "none" else {mode: {}}
        state = {"Status": container.status, "Running": container.status == "running",
                 "Pid": container.pid, "ExitCode": container.exit_code}
        if container.status == "running":
            state["CgroupPath"] = container.cgroup_path
        return {
            "Id": container.id, "Name": container.name, "State": state,
            "Config": {"Labels": container.labels},
            "HostConfig": {"ReadonlyRootfs": "--read-only" in args,
                           "NetworkMode": "none" if mode == "none" else "bridge"},
            "Mounts": mounts, "NetworkSettings": {"Networks": networks},
        }

    # -- the runner
    def __call__(self, args, timeout):
        args = tuple(args)
        self.calls.append(args)
        if self.missing:
            raise FileNotFoundError("podman")
        if self.time_out:
            raise subprocess.TimeoutExpired(["podman", *args], timeout)
        if self.unreachable:
            return cr.PodmanResult(125, "", (
                "Cannot connect to Podman. Please verify your connection to the Linux system using "
                "`podman system connection list`, or try `podman machine init` and `podman machine start`"
                "\nError: unable to connect to Podman socket: dial unix /x.sock: connect: no such file"))
        head = args[0]
        if head == "network" and args[1] == "inspect":
            name = args[-1]
            if name not in self.networks:
                return cr.PodmanResult(125, "", f"Error: network {name}: network not found")
            return cr.PodmanResult(0, json.dumps([{"name": name, "internal": self.networks[name]}]), "")
        if head == "create":
            name = option_values(args, "--name")[0]
            if name in self.containers:
                return cr.PodmanResult(125, "", f'Error: creating container storage: the container name "{name}" '
                                                "is already in use by abc. that name is already in use")
            image_index = next(i for i, arg in enumerate(args) if not arg.startswith("-") and i > 0)
            if args[image_index] not in self.images:
                return cr.PodmanResult(125, "", f"Error: {args[image_index]}: image not known")
            labels = dict(value.split("=", 1) for value in option_values(args, "--label"))
            if self.on_create:
                self.on_create(args)
            container = self.add_container(name, labels, status="created", args=args)
            return cr.PodmanResult(0, container.id + "\n", "")
        if head == "start":
            container = self.find(args[-1])
            if self.start_fails or container is None:
                return cr.PodmanResult(125, "", "Error: unable to start container: crun: something failed")
            self._run(container)
            return cr.PodmanResult(0, container.name + "\n", "")
        if head == "container" and args[1] == "inspect":
            container = self.find(args[-1])
            if container is None:
                return cr.PodmanResult(125, "[]\n", f'Error: no such container "{args[-1]}"')
            return cr.PodmanResult(0, json.dumps([self.inspect_json(container)]), "")
        if head == "exec":
            container = self.find(args[1])
            if container is None or container.status != "running":
                return cr.PodmanResult(125, "", "Error: can only create exec sessions on running containers")
            command = args[2:]
            if command[:1] == ("unshare",):
                if self.unshare_result is not None:
                    return cr.PodmanResult(self.unshare_result[0], "", self.unshare_result[1])
                if any(value.startswith("seccomp=") for value in option_values(container.args, "--security-opt")):
                    return cr.PodmanResult(1, "", "unshare: unshare failed: Operation not permitted\n")
                return cr.PodmanResult(0, "", "")
            if command == ("touch", cr.GATE_FILE):
                container.gate_open = True
                return cr.PodmanResult(0, "", "")
            return cr.PodmanResult(127, "", "Error: executable file not found")
        if head == "stop":
            container = self.find(args[-1])
            if container is None:
                return cr.PodmanResult(125, "", f'Error: no container with name or ID "{args[-1]}" found')
            if not self.stop_does_nothing and container.status == "running":
                self._end(container, 137)
            return cr.PodmanResult(0, container.name + "\n", "")
        if head == "rm":
            container = self.find(args[-1])
            if container is None:
                return cr.PodmanResult(0, "", "")
            if self.rm_fails:
                return cr.PodmanResult(125, "", "Error: cannot remove container: device or resource busy")
            if container.status == "running":
                self._end(container, 137)
            del self.containers[container.name]
            return cr.PodmanResult(0, container.id + "\n", "")
        if head == "ps":
            label = option_values(args, "--filter")[0]
            assert label.startswith("label="), label
            key, value = label[len("label="):].split("=", 1)
            rows = []
            for container in self.containers.values():
                if container.labels.get(key) == value:
                    rows.append({"Id": container.id, "Names": [container.name], "Labels": container.labels,
                                 "State": container.status})
            rows.extend(self.extra_ps_rows)
            return cr.PodmanResult(0, json.dumps(rows), "")
        return cr.PodmanResult(125, "", "Error: unknown command " + " ".join(args))

    def commands(self, head=None):
        return [call for call in self.calls if head is None or call[0] == head]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# --------------------------------------------------------------------------------------------
# Shared fixture
# --------------------------------------------------------------------------------------------

class RuntimeCase(unittest.TestCase):
    project = "cocolo-test-project"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.root = os.path.realpath(folder.name)
        self.workspace = os.path.join(self.root, "workspace")
        os.mkdir(self.workspace)
        self.seccomp = os.path.join(self.root, "seccomp.json")
        Path(self.seccomp).write_text("{}")
        self.host = FakeHostFiles()
        self.podman = FakePodman(self.host)
        self.clock = FakeClock()
        self.runtime = self.make_runtime()
        self.recorded = []

    def make_runtime(self, runtime_class=None):
        return (runtime_class or cr.ContainerRuntime)(
            self.project, runner=self.podman, host_files=self.host,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def spec(self, run_id="run:" + "a" * 64, **changes):
        values = dict(run_id=run_id, image="python:3.12-slim", command=("python3", "-c", "print(1)"),
                      workspace=self.workspace, seccomp_profile=self.seccomp)
        values.update(changes)
        return cr.ContainerSpec(**values)

    def record(self, identity):
        self.recorded.append(identity)

    def launch(self, spec=None, runtime=None):
        return (runtime or self.runtime).launch(spec or self.spec(), self.record)

    def assert_refused(self, result, reason):
        self.assertIsInstance(result, cr.Refusal, result)
        self.assertFalse(result.ok)
        self.assertEqual(reason, result.reason, result.message)
        self.assertTrue(result.message and "\n" not in result.message and "Traceback" not in result.message)


# --------------------------------------------------------------------------------------------
# CS-1 Launch
# --------------------------------------------------------------------------------------------

class LaunchIdentityTests(RuntimeCase):
    """CS-1: identity (name, run label, project label) returned before the container starts."""

    def test_identity_is_recorded_before_any_container_exists(self):
        seen = []

        def record(identity):
            seen.append((identity, list(self.podman.calls), dict(self.podman.containers)))

        launched = self.runtime.launch(self.spec(), record)
        self.assertTrue(launched.ok, launched)
        identity, calls_before, containers_before = seen[0]
        self.assertEqual({}, containers_before)
        self.assertFalse([call for call in calls_before if call[0] in ("create", "start")])
        self.assertEqual(identity, launched.identity)
        self.assertEqual(self.podman.containers[identity.name].id, launched.container_id)

    def test_identity_name_and_labels(self):
        run_id = "run:" + "b" * 64
        identity = self.runtime.identity(run_id)
        self.assertTrue(identity.name.startswith(cr.DEFAULT_NAME_PREFIX))
        self.assertEqual(identity, self.runtime.identity(run_id))
        self.assertNotEqual(identity.name, self.runtime.identity("run:" + "c" * 64).name)
        self.assertEqual({cr.RUN_LABEL: run_id, cr.PROJECT_LABEL: self.project}, identity.labels)
        self.launch(self.spec(run_id=run_id))
        self.assertEqual(identity.labels, self.podman.containers[identity.name].labels)

    def test_hardened_profile_arguments(self):
        launched = self.launch()
        self.assertTrue(launched.ok, launched)
        args = self.podman.commands("create")[0]
        name = launched.identity.name
        for expected in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                         f"--security-opt=seccomp={self.seccomp}", "--userns=keep-id:uid=1000,gid=1000",
                         "--pids-limit=128", f"--memory={512 * 1024 * 1024}", f"--memory-swap={512 * 1024 * 1024}",
                         "--cpu-period=100000", "--cpu-quota=100000", "--network=none", "--pull=never",
                         f"--name={name}", "--stop-signal=SIGTERM",
                         f"--tmpfs=/tmp:size={64 * 1024 * 1024}",
                         f"--tmpfs={cr.CONTAINER_HOME}:size={64 * 1024 * 1024},mode=0700,U",
                         f"--env=HOME={cr.CONTAINER_HOME}"):
            self.assertIn(expected, args)
        self.assertEqual([f"{self.workspace}:/workspace:ro"], option_values(args, "--volume"))
        self.assertFalse([arg for arg in args if arg.startswith(("--mount", "-v", "--privileged", "--cap-add",
                                                                 "--device", "--env-host", "--pid=", "--ipc=host"))])
        # The image, then the gate entrypoint's arguments: the command itself.
        self.assertEqual(("python:3.12-slim", "python3", "-c", "print(1)"), args[-4:])

    def test_writable_workspace_is_the_only_writable_mount(self):
        launched = self.launch(self.spec(workspace_writable=True))
        self.assertTrue(launched.ok, launched)
        args = self.podman.commands("create")[0]
        self.assertEqual([f"{self.workspace}:/workspace:rw"], option_values(args, "--volume"))

    def test_internal_network_is_used(self):
        self.podman.networks["cocolo-run-net"] = True
        launched = self.launch(self.spec(network="cocolo-run-net"))
        self.assertTrue(launched.ok, launched)
        self.assertIn("--network=cocolo-run-net", self.podman.commands("create")[0])

    def test_a_network_that_is_not_internal_is_refused_before_anything_exists(self):
        for network, reason in (("podman", "network_not_internal"), ("nosuch", "network_missing")):
            with self.subTest(network=network):
                result = self.launch(self.spec(network=network))
                self.assert_refused(result, reason)
                self.assertEqual([], self.recorded)
                self.assertEqual([], self.podman.commands("create"))

    def test_invalid_specs_are_refused_without_calling_podman(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.workspace, link)
        comma = os.path.join(self.root, "a,b")
        os.mkdir(comma)
        cases = {
            "relative workspace": dict(workspace="workspace"),
            "symlinked workspace": dict(workspace=link),
            "missing workspace": dict(workspace=os.path.join(self.root, "nope")),
            "comma in workspace": dict(workspace=comma),
            "run id with newline": dict(run_id="run:x\nmore"),
            "empty run id": dict(run_id=""),
            "missing seccomp profile": dict(seccomp_profile=os.path.join(self.root, "none.json")),
            "empty command": dict(command=()),
            "image option": dict(image="--privileged"),
            "network option": dict(network="host --privileged"),
            "bad environment name": dict(environment={"A B": "1"}),
            "zero pids": dict(limits=cr.ContainerLimits(pids=0)),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.podman.calls.clear()
                result = self.launch(self.spec(**change))
                self.assert_refused(result, "invalid_spec")
                self.assertEqual([], self.podman.calls)
                self.assertEqual([], self.recorded)

    def test_a_failing_record_refuses_without_creating(self):
        def record(identity):
            raise OSError("disk full")

        result = self.runtime.launch(self.spec(), record)
        self.assert_refused(result, "record_failed")
        self.assertEqual([], self.podman.commands("create"))

    def test_a_name_in_use_is_refused_and_the_existing_container_left_alone(self):
        identity = self.runtime.identity("run:" + "a" * 64)
        existing = self.podman.add_container(identity.name, identity.labels)
        result = self.launch()
        self.assert_refused(result, "name_in_use")
        self.assertIs(existing, self.podman.containers[identity.name])
        self.assertEqual("running", existing.status)
        self.assertEqual([], self.podman.commands("stop") + self.podman.commands("rm"))

    def test_a_missing_image_is_refused_and_never_pulled(self):
        result = self.launch(self.spec(image="example.org/nosuch:1"))
        self.assert_refused(result, "image_missing")
        self.assertIn("--pull=never", self.podman.commands("create")[0])
        self.assertEqual({}, self.podman.containers)

    def test_a_failed_start_removes_the_container(self):
        self.podman.start_fails = True
        result = self.launch()
        self.assert_refused(result, "start_failed")
        self.assertEqual({}, self.podman.containers)
        self.assertEqual("gone", result.cleanup.outcome)

    def test_the_command_runs_only_after_the_checks_open_the_gate(self):
        launched = self.launch()
        self.assertTrue(launched.ok, launched)
        args = self.podman.commands("create")[0]
        entrypoint = json.loads(option_values(args, "--entrypoint")[0])
        self.assertEqual(["/bin/sh", "-c"], entrypoint[:2])
        self.assertIn(cr.GATE_FILE, entrypoint[2])
        self.assertIn('exec "$@"', entrypoint[2])
        execs = self.podman.commands("exec")
        self.assertEqual(("touch", cr.GATE_FILE), execs[-1][2:])
        self.assertTrue(self.podman.containers[launched.identity.name].gate_open)


# --------------------------------------------------------------------------------------------
# CS-2 Check at start
# --------------------------------------------------------------------------------------------

class StartCheckTests(RuntimeCase):
    """CS-2: effective limits read back from the cgroup, `unshare -Ur` must fail, else fail closed."""

    def test_effective_limits_are_read_back_from_the_cgroup(self):
        launched = self.launch()
        self.assertTrue(launched.ok, launched)
        checks = dict(launched.checks)
        self.assertEqual("128", checks["pids.max"])
        self.assertEqual(str(512 * 1024 * 1024), checks["memory.max"])
        self.assertEqual("0", checks["memory.swap.max"])
        self.assertEqual("100000 100000", checks["cpu.max"])
        self.assertEqual("denied", checks["nested user namespace"])
        container = self.podman.containers[launched.identity.name]
        self.assertEqual(container.cgroup_path, launched.cgroup_path)
        read = {path for paths in self.host.reads for path in paths}
        self.assertIn(f"/sys/fs/cgroup{container.cgroup_path}/pids.max", read)
        self.assertIn(f"/proc/{container.pid}/status", read)

    def assert_refused_and_removed(self, result, needle):
        self.assert_refused(result, "start_check_failed")
        self.assertIn(needle, result.message)
        self.assertEqual({}, self.podman.containers)
        self.assertEqual("gone", result.cleanup.outcome)
        self.assertFalse(any(call[2:] == ("touch", cr.GATE_FILE) for call in self.podman.commands("exec")))

    def test_a_missing_pids_limit_is_refused_and_the_container_removed(self):
        class NoPidsLimit(cr.ContainerRuntime):
            def create_arguments(self, spec, identity):
                return [arg for arg in super().create_arguments(spec, identity)
                        if not arg.startswith("--pids-limit")]

        result = self.launch(runtime=self.make_runtime(NoPidsLimit))
        self.assert_refused_and_removed(result, "pids.max")

    def test_each_wrong_effective_value_is_refused(self):
        cases = {
            "memory.max": ({"memory.max": "max"}, {}),
            "memory.swap.max": ({"memory.swap.max": "max"}, {}),
            "cpu.max": ({"cpu.max": "max 100000"}, {}),
            "CapEff": ({}, {"CapEff": "00000000000000ff"}),
            "CapBnd": ({}, {"CapBnd": "0000000000000001"}),
            "NoNewPrivs": ({}, {"NoNewPrivs": "0"}),
            "Seccomp": ({}, {"Seccomp": "0"}),
        }
        for needle, (effective, status) in cases.items():
            with self.subTest(needle):
                self.setUp()
                self.podman.effective_overrides = effective
                self.podman.status_overrides = status
                self.assert_refused_and_removed(self.launch(), needle)

    def test_a_writable_root_or_wrong_network_is_refused(self):
        class Loose(cr.ContainerRuntime):
            drop = ""

            def create_arguments(self, spec, identity):
                args = super().create_arguments(spec, identity)
                return [arg if arg != "--network=none" else "--network=podman"
                        for arg in args if arg != self.drop]

        Loose.drop = "--read-only"
        self.assert_refused_and_removed(self.launch(runtime=self.make_runtime(Loose)), "read-only")
        self.setUp()
        Loose.drop = ""
        self.assert_refused_and_removed(self.launch(runtime=self.make_runtime(Loose)), "network")

    def test_nested_user_namespaces_allowed_is_refused(self):
        class NoSeccomp(cr.ContainerRuntime):
            def create_arguments(self, spec, identity):
                return [arg for arg in super().create_arguments(spec, identity)
                        if not arg.startswith("--security-opt=seccomp=")]

        result = self.launch(runtime=self.make_runtime(NoSeccomp))
        self.assert_refused_and_removed(result, "nested user namespace")

    def test_an_unshare_check_that_cannot_run_fails_closed(self):
        for code, stderr in ((127, "Error: executable file not found in $PATH"), (1, "unshare: bad usage")):
            with self.subTest(code=code):
                self.setUp()
                self.podman.unshare_result = (code, stderr)
                self.assert_refused_and_removed(self.launch(), "nested user namespace")

    def test_unreadable_host_files_fail_closed(self):
        self.host.unreadable = True
        result = self.launch()
        self.assert_refused(result, "start_check_failed")
        self.assertEqual({}, self.podman.containers)
        # The container is removed, but its recorded cgroup cannot be read: never claimed gone.
        self.assertEqual("stop_failed", result.cleanup.outcome)

    def test_a_failed_check_whose_removal_fails_reports_stop_failed(self):
        self.podman.effective_overrides = {"pids.max": "max"}
        self.podman.rm_fails = True
        self.podman.stop_does_nothing = True
        result = self.launch()
        self.assert_refused(result, "start_check_failed")
        self.assertEqual("stop_failed", result.cleanup.outcome)
        self.assertFalse(result.cleanup.gone)


# --------------------------------------------------------------------------------------------
# CS-3 Stop and CS-4 Gone
# --------------------------------------------------------------------------------------------

class StopTests(RuntimeCase):
    """CS-3: SIGTERM, a grace (10 s by default), kill, remove. CS-4: gone only on the OS's word."""

    def launched(self):
        launched = self.launch()
        self.assertTrue(launched.ok, launched)
        self.podman.calls.clear()
        return launched

    def test_stop_terms_with_the_default_grace_then_kills_and_removes(self):
        launched = self.launched()
        result = self.runtime.stop(launched.identity.name, cgroup_path=launched.cgroup_path)
        self.assertEqual("gone", result.outcome, result)
        self.assertTrue(result.gone)
        self.assertEqual(137, result.exit_code)
        heads = [call[0] for call in self.podman.calls if call[0] in ("stop", "rm")]
        self.assertEqual(["stop", "rm"], heads)
        self.assertEqual(("stop", "--time=10", launched.identity.name), self.podman.commands("stop")[0])
        self.assertEqual(("rm", "--force", "--time=0", launched.identity.name), self.podman.commands("rm")[0])
        self.assertEqual(10, cr.DEFAULT_GRACE_SECONDS)

    def test_stop_uses_the_given_grace(self):
        launched = self.launched()
        self.runtime.stop(launched.identity.name, grace_seconds=2)
        self.assertEqual(("stop", "--time=2", launched.identity.name), self.podman.commands("stop")[0])

    def test_stop_of_an_absent_container_is_gone_without_signalling(self):
        result = self.runtime.stop("cocolo-run-nosuch")
        self.assertEqual("gone", result.outcome)
        self.assertEqual([], self.podman.commands("stop") + self.podman.commands("rm"))

    def test_gone_states(self):
        launched = self.launched()
        name, cgroup = launched.identity.name, launched.cgroup_path
        self.assertEqual("present", self.runtime.gone(name).state)
        container = self.podman.containers[name]
        self.podman._end(container, 0)
        self.assertEqual("gone", self.runtime.gone(name, cgroup_path=cgroup).state)
        # Exited, cgroup unknown: cannot be claimed gone.
        self.assertEqual("unknown", self.runtime.gone(name).state)
        # Exited, cgroup still reporting processes.
        self.host.files[f"/sys/fs/cgroup{cgroup}/cgroup.events"] = "populated 1\nfrozen 0\n"
        self.assertEqual("present", self.runtime.gone(name, cgroup_path=cgroup).state)
        self.host.files[f"/sys/fs/cgroup{cgroup}/cgroup.events"] = "populated 0\nfrozen 0\n"
        self.assertEqual("gone", self.runtime.gone(name, cgroup_path=cgroup).state)
        self.host.unreadable = True
        self.assertEqual("unknown", self.runtime.gone(name, cgroup_path=cgroup).state)
        self.host.unreadable = False
        del self.podman.containers[name]
        self.assertEqual("gone", self.runtime.gone(name).state)

    def test_a_cgroup_still_populated_after_the_kill_is_stop_failed(self):
        launched = self.launched()
        self.podman.cgroup_stays_populated = True
        result = self.runtime.stop(launched.identity.name, cgroup_path=launched.cgroup_path, gone_timeout=5)
        self.assertEqual("stop_failed", result.outcome, result)
        self.assertFalse(result.gone)
        self.assertIn("populated", result.detail)
        self.assertGreaterEqual(self.clock.now, 1005.0)

    def test_a_container_still_running_after_the_kill_is_stop_failed(self):
        launched = self.launched()
        self.podman.stop_does_nothing = True
        self.podman.rm_fails = True
        result = self.runtime.stop(launched.identity.name, gone_timeout=3)
        self.assertEqual("stop_failed", result.outcome, result)
        self.assertIn("running", result.detail)
        # The kill and removal are retried while waiting.
        self.assertGreater(len(self.podman.commands("rm")), 1)

    def test_an_unreadable_cgroup_is_never_gone(self):
        launched = self.launched()
        self.host.unreadable = True
        result = self.runtime.stop(launched.identity.name, cgroup_path=launched.cgroup_path, gone_timeout=2)
        self.assertEqual("stop_failed", result.outcome, result)


# --------------------------------------------------------------------------------------------
# CS-5 Health monitor check 1: every COCOLO container belongs to a live run
# --------------------------------------------------------------------------------------------

class OrphanSweepTests(RuntimeCase):
    def test_orphans_are_stopped_removed_and_reported_and_nothing_else_is_touched(self):
        live = self.runtime.identity("run:live")
        orphan = self.runtime.identity("run:orphan")
        exited = self.runtime.identity("run:exited")
        self.podman.add_container(live.name, live.labels)
        self.podman.add_container(orphan.name, orphan.labels)
        self.podman.add_container(exited.name, exited.labels, status="exited")
        other_project = cr.ContainerRuntime("other-project", runner=self.podman).identity("run:x")
        self.podman.add_container(other_project.name, other_project.labels)
        self.podman.add_container("owners-database", {})
        self.podman.add_container("odd-name", {cr.PROJECT_LABEL: self.project, cr.RUN_LABEL: "run:odd"})
        # A listing row without the label (a filter podman did not apply) is still never touched.
        self.podman.extra_ps_rows = [{"Id": "f" * 64, "Names": ["owners-other"], "Labels": {}, "State": "running"}]

        report = self.runtime.sweep_orphans({"run:live"})
        self.assertIsNone(report.refusal)
        outcomes = {entry.run_id: entry.outcome for entry in report.entries}
        self.assertEqual({"run:orphan": "removed", "run:exited": "removed", "run:odd": "skipped"}, outcomes)
        self.assertEqual(1, report.kept)
        self.assertEqual({live.name, other_project.name, "owners-database", "odd-name"},
                         set(self.podman.containers))
        touched = {call[-1] for call in self.podman.calls if call[0] in ("stop", "rm", "exec")}
        for untouched in (live.name, other_project.name, "owners-database", "odd-name", "owners-other", "f" * 64,
                          self.podman.containers[live.name].id):
            self.assertNotIn(untouched, touched)
        self.assertEqual(("ps", "--all", f"--filter=label={cr.PROJECT_LABEL}={self.project}",
                          "--format=json"), self.podman.commands("ps")[0])

    def test_an_orphan_that_will_not_go_is_reported_stop_failed(self):
        orphan = self.runtime.identity("run:orphan")
        self.podman.add_container(orphan.name, orphan.labels)
        self.podman.cgroup_stays_populated = True
        report = self.runtime.sweep_orphans(set(), gone_timeout=2)
        self.assertEqual(["stop_failed"], [entry.outcome for entry in report.entries])

    def test_an_empty_listing_reports_nothing(self):
        report = self.runtime.sweep_orphans(set())
        self.assertEqual((), report.entries)
        self.assertEqual(0, report.kept)


# --------------------------------------------------------------------------------------------
# CS-6 Unavailable podman
# --------------------------------------------------------------------------------------------

class UnavailablePodmanTests(RuntimeCase):
    def every_operation(self):
        return {
            "launch": lambda: self.launch(),
            "stop": lambda: self.runtime.stop("cocolo-run-x"),
            "gone": lambda: self.runtime.gone("cocolo-run-x"),
            "sweep": lambda: self.runtime.sweep_orphans(set()),
            "network launch": lambda: self.launch(self.spec(network="net")),
        }

    def refusal_of(self, result):
        if isinstance(result, cr.Refusal):
            return result
        self.assertFalse(getattr(result, "gone", False))
        return result.refusal

    def check(self, reason, needle):
        for label, operation in self.every_operation().items():
            with self.subTest(label):
                refusal = self.refusal_of(operation())
                self.assertIsInstance(refusal, cr.Refusal)
                self.assertEqual(reason, refusal.reason)
                self.assertIn(needle, refusal.message)
                self.assertNotIn("Traceback", refusal.message)

    def test_missing_podman(self):
        self.podman.missing = True
        self.check("podman_missing", "podman")

    def test_unreachable_podman_or_stopped_machine(self):
        self.podman.unreachable = True
        self.check("podman_unavailable", "machine")

    def test_a_hung_podman(self):
        self.podman.time_out = True
        self.check("podman_timeout", "did not answer")

    def test_stop_and_gone_refusals_are_never_gone(self):
        self.podman.unreachable = True
        self.assertEqual("refused", self.runtime.stop("x").outcome)
        self.assertEqual("unknown", self.runtime.gone("x").state)

    def test_the_subprocess_runner_with_no_podman_binary(self):
        runner = cr.SubprocessPodman(podman_path=os.path.join(self.root, "no-podman"))
        runtime = cr.ContainerRuntime(self.project, runner=runner, host_files=self.host)
        self.assertEqual("podman_missing", runtime.gone("x").refusal.reason)
        self.assertEqual("podman_missing", runtime.sweep_orphans(set()).refusal.reason)


class SeccompProfileTests(unittest.TestCase):
    """The profile: the host's default with nested user namespaces denied (spike PC-2 gap 1)."""

    def test_derived_profile_denies_new_user_namespaces_only(self):
        default = {"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 38, "syscalls": [
            {"names": ["read", "clone", "clone3", "unshare", "write"], "action": "SCMP_ACT_ALLOW"},
            {"names": ["setns"], "action": "SCMP_ACT_ALLOW", "includes": {"caps": ["CAP_SYS_ADMIN"]}},
        ]}
        derived = cr.derive_seccomp_profile(default)
        self.assertEqual(["read", "write"], derived["syscalls"][0]["names"])
        self.assertEqual(default["syscalls"][1], derived["syscalls"][1])
        rules = derived["syscalls"][2:]
        allow = [rule for rule in rules if rule["action"] == "SCMP_ACT_ALLOW"]
        deny = [rule for rule in rules if rule["action"] == "SCMP_ACT_ERRNO"]
        self.assertEqual({"clone", "unshare"}, {name for rule in allow for name in rule["names"]})
        for rule in allow:
            self.assertEqual([{"index": 0, "value": cr.CLONE_NEWUSER, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}],
                             rule["args"])
        by_name = {name: rule for rule in deny for name in rule["names"]}
        self.assertEqual(1, by_name["unshare"]["errnoRet"])
        self.assertEqual(cr.CLONE_NEWUSER, by_name["clone"]["args"][0]["valueTwo"])
        self.assertEqual(38, by_name["clone3"]["errnoRet"])  # ENOSYS: libc falls back to clone
        # The input is not changed.
        self.assertIn("clone", default["syscalls"][0]["names"])

    def test_a_profile_without_an_allow_rule_for_clone_is_refused(self):
        with self.assertRaises(ValueError):
            cr.derive_seccomp_profile({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []})


# --------------------------------------------------------------------------------------------
# Real podman (skipped unless podman, its machine and python:3.12-slim are all present)
# --------------------------------------------------------------------------------------------

REAL_IMAGE = "python:3.12-slim"


def real_podman_reason():
    path = next((p for p in cr.PODMAN_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if path is None:
        return None, "podman is not installed"
    try:
        info = subprocess.run([path, "info", "--format", "{{.Host.CgroupsVersion}}"], capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "podman did not answer within 30 s"
    if info.returncode != 0:
        return None, "podman's machine or service is not running: " + (info.stderr.strip().splitlines() or [""])[0]
    if info.stdout.strip() != "v2":
        return None, f"the podman host uses cgroup {info.stdout.strip()}, not v2"
    image = subprocess.run([path, "image", "exists", REAL_IMAGE], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    if image.returncode != 0:
        return None, f"{REAL_IMAGE} is not present (these tests never pull)"
    return path, ""


REAL_PODMAN, REAL_SKIP_REASON = (None, "not probed")
if os.environ.get("COCOLO_SKIP_REAL_PODMAN") != "1":
    REAL_PODMAN, REAL_SKIP_REASON = real_podman_reason()
else:
    REAL_SKIP_REASON = "COCOLO_SKIP_REAL_PODMAN=1"

# The survivor: main forks, the child calls setsid and forks again; the grandchild ignores
# TERM, HUP and INT and sleeps. Main then sleeps (or ignores TERM and sleeps forever).
SURVIVOR = r"""
import os, signal, sys, time
marker, main_seconds, main_ignores_term = sys.argv[1], float(sys.argv[2]), sys.argv[3] == "1"
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(number, signal.SIG_IGN)
        time.sleep(3600)
    os._exit(0)
if main_ignores_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("survivor started", flush=True)
time.sleep(main_seconds)
"""

FORK_BOMB = r"""
import errno, os, time
count = 0
error = ""
while count < 10000:
    try:
        pid = os.fork()
    except OSError as exc:
        error = errno.errorcode.get(exc.errno, str(exc.errno))
        break
    if pid == 0:
        time.sleep(30)
        os._exit(0)
    count += 1
open("/workspace/bomb.txt", "w").write(f"{count} {error}\n")
time.sleep(30)
"""

PROBE = r"""
import os, socket
lines = []
try:
    open("/root-write-test", "w")
    lines.append("root writable")
except OSError as exc:
    lines.append("root " + exc.strerror)
lines.append("uid %d gid %d" % (os.getuid(), os.getgid()))
lines.append("interfaces " + ",".join(sorted(name for _, name in socket.if_nameindex())))
lines.append("home " + os.environ.get("HOME", ""))
mounts = [line.split()[4] for line in open("/proc/self/mountinfo")]
lines.append("mounts " + ",".join(sorted(m for m in mounts if m.startswith(("/workspace", "/Users", "/private")))))
open("/workspace/probe.txt", "w").write("\n".join(lines) + "\n")
"""


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealPodmanTests(unittest.TestCase):
    """Real containers in the existing podman machine. Names and the project label start cocolo-test-."""

    @classmethod
    def setUpClass(cls):
        cls.host = cr.default_host_files(cr.SubprocessPodman(REAL_PODMAN))
        profile = cr.read_default_seccomp_profile(cls.host)
        if isinstance(profile, cr.Refusal):
            raise unittest.SkipTest("the podman host's default seccomp profile is unreadable: " + profile.message)
        cls.folder = tempfile.mkdtemp(prefix="cocolo-test-", dir=SHORT_TMP)
        cls.seccomp = os.path.join(os.path.realpath(cls.folder), "seccomp.json")
        Path(cls.seccomp).write_text(json.dumps(cr.derive_seccomp_profile(profile)))

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(OSError):
            os.unlink(cls.seccomp)
            os.rmdir(cls.folder)

    def setUp(self):
        self.project = "cocolo-test-" + secrets.token_hex(4)
        self.runtime = cr.ContainerRuntime(self.project, name_prefix="cocolo-test-", runner=cr.SubprocessPodman(REAL_PODMAN))
        folder = tempfile.TemporaryDirectory(prefix="cocolo-test-", dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.workspace = os.path.realpath(folder.name)
        self.addCleanup(self.remove_test_containers)

    def podman(self, *args, timeout=60):
        return subprocess.run([REAL_PODMAN, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def remove_test_containers(self):
        """Remove exactly this test's containers: those carrying its own cocolo-test- project label."""
        listing = self.podman("ps", "--all", f"--filter=label={cr.PROJECT_LABEL}={self.project}", "--format=json")
        for row in json.loads(listing.stdout or "[]"):
            name = row["Names"][0]
            if name.startswith("cocolo-test-") and row["Labels"].get(cr.PROJECT_LABEL) == self.project:
                self.podman("rm", "--force", "--time=0", name)

    def spec(self, run_id, command, **changes):
        values = dict(run_id=run_id, image=REAL_IMAGE, command=command, workspace=self.workspace,
                      seccomp_profile=self.seccomp)
        values.update(changes)
        return cr.ContainerSpec(**values)

    def launch(self, spec, runtime=None):
        recorded = []

        def record(identity):
            # The identity is known before the container exists.
            recorded.append((identity, self.podman("container", "exists", identity.name).returncode))

        launched = (runtime or self.runtime).launch(spec, record)
        if recorded:
            self.assertEqual(1, recorded[0][1], "the container existed before its identity was recorded")
        return launched

    def processes_in_host(self, marker):
        """Processes on the podman host (the VM on macOS) whose command line carries the marker."""
        pattern = "[" + marker[0] + "]" + marker[1:]
        if sys.platform == "darwin":
            result = self.podman("machine", "ssh", f"pgrep -f '{pattern}' || true")
        else:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=30)
        return [line for line in result.stdout.split() if line.strip().isdigit()]

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return predicate()

    def test_real_hardened_launch(self):
        """CS-1 and CS-2 on a real container: profile in force, checks pass, the workspace is the only mount."""
        started = time.monotonic()
        launched = self.launch(self.spec("run:hardened", ("python3", "-c", PROBE), workspace_writable=True))
        launch_seconds = time.monotonic() - started
        self.assertTrue(launched.ok, launched)
        checks = dict(launched.checks)
        self.assertEqual("128", checks["pids.max"])
        self.assertEqual("denied", checks["nested user namespace"])
        self.assertTrue(self.wait_for(lambda: os.path.exists(os.path.join(self.workspace, "probe.txt")), 20))
        self.assertTrue(self.wait_for(lambda: self.runtime.gone(launched.identity.name,
                                                                cgroup_path=launched.cgroup_path).gone, 20))
        probe = Path(self.workspace, "probe.txt").read_text()
        self.assertIn("root Read-only file system", probe)
        self.assertIn("uid 1000 gid 1000", probe)
        self.assertIn("interfaces lo\n", probe)
        self.assertIn(f"home {cr.CONTAINER_HOME}", probe)
        self.assertIn("mounts /workspace\n", probe)
        labels = json.loads(self.podman("container", "inspect", launched.identity.name).stdout)[0]["Config"]["Labels"]
        self.assertEqual("run:hardened", labels[cr.RUN_LABEL])
        self.assertEqual(self.project, labels[cr.PROJECT_LABEL])
        stopped = self.runtime.stop(launched.identity.name, cgroup_path=launched.cgroup_path)
        self.assertEqual("gone", stopped.outcome, stopped)
        print(f"\n[CS-1/2 real] launch with checks {launch_seconds:.2f} s; checks {checks}", file=sys.stderr)

    def test_real_internal_network_and_a_non_internal_one_refused(self):
        internal = "cocolo-test-net-" + secrets.token_hex(4)
        external = "cocolo-test-ext-" + secrets.token_hex(4)
        self.assertEqual(0, self.podman("network", "create", "--internal", internal).returncode)
        self.assertEqual(0, self.podman("network", "create", external).returncode)

        def remove_networks():
            self.remove_test_containers()
            self.podman("network", "rm", internal)
            self.podman("network", "rm", external)

        self.addCleanup(remove_networks)
        refused = self.launch(self.spec("run:external", ("true",), network=external))
        self.assertIsInstance(refused, cr.Refusal)
        self.assertEqual("network_not_internal", refused.reason)
        launched = self.launch(self.spec("run:internal", ("sleep", "30"), network=internal))
        self.assertTrue(launched.ok, launched)
        networks = json.loads(self.podman("container", "inspect", launched.identity.name).stdout)[0][
            "NetworkSettings"]["Networks"]
        self.assertEqual([internal], list(networks))
        self.assertEqual("gone", self.runtime.stop(launched.identity.name, grace_seconds=1).outcome)

    def test_real_double_fork_survivor_ends_with_the_container(self):
        """Spike PC-1: a setsid double-forked grandchild ignoring TERM/HUP/INT ends when main exits."""
        marker = "cocolo-test-marker-" + secrets.token_hex(6)
        launched = self.launch(self.spec("run:survivor", ("python3", "-c", SURVIVOR, marker, "3", "0")))
        self.assertTrue(launched.ok, launched)
        self.assertTrue(self.wait_for(lambda: len(self.processes_in_host(marker)) == 2, 10),
                        "main and the grandchild were not both seen")
        self.assertEqual("present", self.runtime.gone(launched.identity.name, cgroup_path=launched.cgroup_path).state)
        started = time.monotonic()
        self.assertTrue(self.wait_for(
            lambda: self.runtime.gone(launched.identity.name, cgroup_path=launched.cgroup_path).gone, 15))
        seconds = time.monotonic() - started
        self.assertEqual([], self.processes_in_host(marker))
        state = json.loads(self.podman("container", "inspect", launched.identity.name).stdout)[0]["State"]
        self.assertEqual(("exited", 0), (state["Status"], state["ExitCode"]))
        stopped = self.runtime.stop(launched.identity.name, cgroup_path=launched.cgroup_path)
        self.assertEqual("gone", stopped.outcome, stopped)
        self.assertEqual(1, self.podman("container", "exists", launched.identity.name).returncode)
        print(f"\n[CS-1/4 real] survivor gone {seconds:.2f} s after it was seen; 0 processes left", file=sys.stderr)

    def test_real_stop_of_a_term_ignoring_main_with_a_survivor(self):
        """CS-3/CS-4: TERM ignored by main and grandchild; the grace, then the kill; gone asked of the OS."""
        marker = "cocolo-test-marker-" + secrets.token_hex(6)
        launched = self.launch(self.spec("run:ignorer", ("python3", "-c", SURVIVOR, marker, "3600", "1")))
        self.assertTrue(launched.ok, launched)
        self.assertTrue(self.wait_for(lambda: len(self.processes_in_host(marker)) == 2, 10))
        events = self.host.read([f"/sys/fs/cgroup{launched.cgroup_path}/cgroup.events"])
        self.assertIn("populated 1", next(iter(events.values())))
        started = time.monotonic()
        stopped = self.runtime.stop(launched.identity.name, grace_seconds=2, cgroup_path=launched.cgroup_path)
        seconds = time.monotonic() - started
        self.assertEqual("gone", stopped.outcome, stopped)
        self.assertEqual(137, stopped.exit_code)
        self.assertGreaterEqual(seconds, 2.0)
        self.assertLess(seconds, 10.0)
        self.assertEqual([], self.processes_in_host(marker))
        events = self.host.read([f"/sys/fs/cgroup{launched.cgroup_path}/cgroup.events"])
        self.assertEqual([None], list(events.values()), "the cgroup is still there")
        print(f"\n[CS-3/4 real] stop with grace 2 s took {seconds:.2f} s, exit {stopped.exit_code}, "
              "cgroup removed, 0 processes left", file=sys.stderr)

    def test_real_fork_bomb_is_held_by_pids_max(self):
        launched = self.launch(self.spec("run:bomb", ("python3", "-c", FORK_BOMB), workspace_writable=True))
        self.assertTrue(launched.ok, launched)
        result = os.path.join(self.workspace, "bomb.txt")
        self.assertTrue(self.wait_for(lambda: os.path.exists(result) and Path(result).read_text().endswith("\n"), 30))
        count, error = Path(result).read_text().split()
        self.assertLess(int(count), 128)
        self.assertEqual("EAGAIN", error)
        stopped = self.runtime.stop(launched.identity.name, grace_seconds=1, cgroup_path=launched.cgroup_path)
        self.assertEqual("gone", stopped.outcome, stopped)
        print(f"\n[CS-2 real] fork bomb held at {count} forks ({error}) by pids.max 128; stopped and gone",
              file=sys.stderr)

    def test_real_start_check_catches_a_missing_limit_and_open_user_namespaces(self):
        class NoPidsLimit(cr.ContainerRuntime):
            def create_arguments(self, spec, identity):
                return [arg for arg in super().create_arguments(spec, identity)
                        if not arg.startswith("--pids-limit")]

        class NoSeccomp(cr.ContainerRuntime):
            def create_arguments(self, spec, identity):
                return [arg for arg in super().create_arguments(spec, identity)
                        if not arg.startswith("--security-opt=seccomp=")]

        for runtime_class, run_id, needle in ((NoPidsLimit, "run:nopids", "pids.max"),
                                              (NoSeccomp, "run:noseccomp", "nested user namespace")):
            with self.subTest(needle):
                runtime = runtime_class(self.project, name_prefix="cocolo-test-",
                                        runner=cr.SubprocessPodman(REAL_PODMAN))
                marker = Path(self.workspace, run_id.replace(":", "-"))
                result = self.launch(self.spec(run_id, ("touch", "/workspace/" + marker.name),
                                               workspace_writable=True), runtime=runtime)
                self.assertIsInstance(result, cr.Refusal, result)
                self.assertEqual("start_check_failed", result.reason)
                self.assertIn(needle, result.message)
                self.assertEqual("gone", result.cleanup.outcome)
                self.assertEqual(1, self.podman("container", "exists", runtime.identity(run_id).name).returncode)
                self.assertFalse(marker.exists(), "the command ran although the check failed")
                print(f"\n[CS-2 real] refused: {result.message}", file=sys.stderr)

    def test_real_orphan_sweep(self):
        live = self.launch(self.spec("run:live", ("sleep", "60")))
        orphan = self.launch(self.spec("run:orphan", ("sleep", "60")))
        self.assertTrue(live.ok and orphan.ok, (live, orphan))
        # A container of another (test) project and one with no labels at all: never touched.
        other = cr.ContainerRuntime(self.project + "-other", name_prefix="cocolo-test-",
                                    runner=cr.SubprocessPodman(REAL_PODMAN))
        self.addCleanup(self.podman, "rm", "--force", "--time=0", other.identity("run:orphan").name)
        foreign = other.launch(self.spec("run:orphan", ("sleep", "60")), lambda identity: None)
        self.assertTrue(foreign.ok, foreign)
        bare = "cocolo-test-bare-" + secrets.token_hex(4)
        self.addCleanup(self.podman, "rm", "--force", "--time=0", bare)
        self.assertEqual(0, self.podman("run", "-d", "--name", bare, "--network=none", "--pull=never",
                                        REAL_IMAGE, "sleep", "60").returncode)
        started = time.monotonic()
        report = self.runtime.sweep_orphans({"run:live"}, grace_seconds=1)
        seconds = time.monotonic() - started
        self.assertIsNone(report.refusal)
        self.assertEqual([("run:orphan", "removed")], [(e.run_id, e.outcome) for e in report.entries])
        self.assertEqual(1, report.kept)
        self.assertEqual(1, self.podman("container", "exists", orphan.identity.name).returncode)
        for name in (live.identity.name, foreign.identity.name, bare):
            state = json.loads(self.podman("container", "inspect", name).stdout)[0]["State"]["Status"]
            self.assertEqual("running", state, name)
        print(f"\n[CS-5 real] sweep removed 1 orphan, kept 1 live, left 2 others running, {seconds:.2f} s",
              file=sys.stderr)

    def test_real_unreachable_podman_is_a_plain_refusal(self):
        runner = cr.SubprocessPodman(REAL_PODMAN, global_args=("--url", "unix:///private/tmp/cocolo-test-no.sock"))
        runtime = cr.ContainerRuntime(self.project, name_prefix="cocolo-test-", runner=runner,
                                      host_files=self.host)
        result = runtime.sweep_orphans(set())
        self.assertEqual("podman_unavailable", result.refusal.reason)
        launch = runtime.launch(self.spec("run:x", ("true",)), lambda identity: None)
        self.assertEqual("podman_unavailable", launch.reason)
        self.assertEqual("refused", runtime.stop("cocolo-test-x").outcome)


if __name__ == "__main__":
    unittest.main()
