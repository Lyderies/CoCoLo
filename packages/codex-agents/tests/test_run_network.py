"""CN-1..CN-5: a per-run internal network whose only way out is COCOLO's allowlist proxy.

Owner decision (PLAN-0.4.0.md, "Containment decided"): each run has "a per-run network allowing
only its provider". Evidence: RESEARCH-0.4.0-podman-spike.md PC-3.

- The proxy itself (`egress_proxy`) is tested in this process against a local origin: it is a
  real CONNECT proxy on 127.0.0.1, with the name lookup and the "public address" rule replaced
  only where a test says so.
- The podman side (`run_network`) is tested through a fake podman that keeps networks,
  containers and the policy pushed into the proxy.
- The real tests run only when podman is installed, its machine (or local service) answers and
  python:3.12-slim is present (never pulled); otherwise they skip with the reason. They create only
  containers and networks named `cocolo-test-*` carrying a per-test project label, and remove
  exactly those by name. They never prune. Their outbound traffic is a TLS handshake with an
  allowlisted host: no API call, no credentials.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import tests.support  # noqa: F401
from tests.support import SHORT_TMP

from codex_agents import container_runtime as cr
from codex_agents import egress_proxy as ep
from codex_agents import run_network as rn


# --------------------------------------------------------------------------------------------
# A local origin and a client that speaks CONNECT
# --------------------------------------------------------------------------------------------

class EchoOrigin:
    """A TCP server on 127.0.0.1 that answers every line with "echo: <line>"."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self.accepted = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(connection,), daemon=True).start()

    @staticmethod
    def _echo(connection):
        with connection:
            reader = connection.makefile("rb")
            for line in reader:
                connection.sendall(b"echo: " + line)

    def close(self):
        self.socket.close()


def proxy_request(port, request: bytes, timeout=5.0):
    """Send a raw request to the proxy; return (status line, socket) with the head consumed."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    connection.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = connection.recv(4096)
        if not chunk:
            break
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    return status, connection, head


def connect(port, target):
    return proxy_request(port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())


def write_policy(path, runs):
    Path(path).write_text(json.dumps({"version": 1, "runs": runs}))


class LocalProxyCase(unittest.TestCase):
    """A real proxy on 127.0.0.1, its policy file and records folder in a temporary folder."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.root = os.path.realpath(folder.name)
        self.records = os.path.join(self.root, "records")
        os.mkdir(self.records)
        self.policy = os.path.join(self.root, "policy.json")
        self.origin = EchoOrigin()
        self.addCleanup(self.origin.close)
        write_policy(self.policy, [{"run": "run:a", "subnets": ["127.0.0.0/8"],
                                    "allow": ["api.anthropic.com"]}])

    def start(self, **options):
        values = dict(listen="127.0.0.1", port=0, ports=frozenset({self.origin.port}),
                      resolve=lambda host, port: ["127.0.0.1"],
                      address_allowed=lambda address: address == "127.0.0.1")
        values.update(options)
        proxy = ep.EgressProxy(self.policy, self.records, **values)
        proxy.start()
        self.addCleanup(proxy.close)
        return proxy

    def records_now(self, run_id=None):
        # The record is written before the answer is sent; allow a moment for the thread anyway.
        deadline = time.monotonic() + 2
        rows = ep.read_records(self.records, run_id)
        while not rows and time.monotonic() < deadline:
            time.sleep(0.02)
            rows = ep.read_records(self.records, run_id)
        return rows


# --------------------------------------------------------------------------------------------
# CN-2 The allowlist proxy (local, real sockets)
# --------------------------------------------------------------------------------------------

class ProxyAllowlistTests(LocalProxyCase):
    """CN-2: CONNECT only, the run's own hosts only, 403 for everything else, no TLS interception."""

    def test_an_allowed_host_is_tunnelled_byte_for_byte(self):
        proxy = self.start()
        status, connection, head = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
        with connection:
            self.assertEqual("HTTP/1.1 200 Connection established", status)
            self.assertTrue(head.endswith(b"\r\n\r\n"))
            # Opaque bytes (as a TLS record would be) pass unchanged in both directions.
            connection.sendall(b"\x16\x03\x01 opaque\n")
            self.assertEqual(b"echo: \x16\x03\x01 opaque\n", connection.recv(4096))
        self.assertEqual(1, self.origin.accepted)

    def test_a_host_not_on_the_runs_list_is_denied_with_403(self):
        proxy = self.start()
        for target in (f"example.com:{self.origin.port}", f"chatgpt.com:{self.origin.port}",
                       f"api.anthropic.com.evil.example:{self.origin.port}", f"anthropic.com:{self.origin.port}"):
            with self.subTest(target):
                status, connection, _ = connect(proxy.port, target)
                connection.close()
                self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        self.assertEqual(0, self.origin.accepted)

    def test_direct_ip_addresses_are_denied(self):
        proxy = self.start()
        for target in (f"127.0.0.1:{self.origin.port}", f"[::1]:{self.origin.port}", "1.1.1.1:443",
                       f"2130706433:{self.origin.port}", f"0x7f000001:{self.origin.port}"):
            with self.subTest(target):
                status, connection, _ = connect(proxy.port, target)
                connection.close()
                self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        self.assertEqual(0, self.origin.accepted)

    def test_an_allowed_name_that_resolves_to_a_private_address_is_denied(self):
        # The default address rule: only public addresses (no loopback, private, link-local...).
        proxy = self.start(address_allowed=None)
        status, connection, _ = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
        connection.close()
        self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        self.assertEqual(0, self.origin.accepted)
        self.assertIn("public", self.records_now()[-1]["reason"])

    def test_only_the_allowed_ports(self):
        proxy = self.start()
        status, connection, _ = connect(proxy.port, "api.anthropic.com:22")
        connection.close()
        self.assertTrue(status.startswith("HTTP/1.1 403"), status)

    def test_plain_http_requests_are_denied(self):
        proxy = self.start()
        status, connection, _ = proxy_request(
            proxy.port, b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n")
        connection.close()
        self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        row = self.records_now()[-1]
        self.assertEqual(("deny", "api.anthropic.com"), (row["decision"], row["host"]))

    def test_a_client_from_no_known_run_is_denied(self):
        write_policy(self.policy, [{"run": "run:a", "subnets": ["10.89.7.0/24"], "allow": ["api.anthropic.com"]}])
        proxy = self.start()
        status, connection, _ = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
        connection.close()
        self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        self.assertIsNone(self.records_now()[-1]["run"])

    def test_a_missing_or_unreadable_policy_denies_everything(self):
        for text in (None, "not json", json.dumps({"version": 1, "runs": [{"run": "run:a"}]}),
                     json.dumps({"version": 1, "runs": [{"run": "run:a", "subnets": ["127.0.0.0/8"],
                                                         "allow": ["*.anthropic.com"]}]})):
            with self.subTest(text):
                if text is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(self.policy)
                else:
                    Path(self.policy).write_text(text)
                proxy = self.start()
                status, connection, _ = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
                connection.close()
                self.assertTrue(status.startswith("HTTP/1.1 403"), status)
        self.assertEqual(0, self.origin.accepted)

    def test_a_changed_policy_takes_effect_on_the_next_connection(self):
        proxy = self.start()
        status, connection, _ = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
        connection.close()
        self.assertEqual("HTTP/1.1 200 Connection established", status)
        write_policy(self.policy, [])
        status, connection, _ = connect(proxy.port, f"api.anthropic.com:{self.origin.port}")
        connection.close()
        self.assertTrue(status.startswith("HTTP/1.1 403"), status)

    def test_an_oversized_or_garbled_request_is_denied(self):
        proxy = self.start()
        for request in (b"CONNECT " + b"a" * 9000 + b":443 HTTP/1.1\r\n\r\n", b"\x00\x01\x02\r\n\r\n",
                        b"CONNECT api.anthropic.com HTTP/1.1\r\n\r\n"):
            with self.subTest(request[:20]):
                status, connection, _ = proxy_request(proxy.port, request)
                connection.close()
                self.assertTrue(status.startswith(("HTTP/1.1 403", "HTTP/1.1 400")), status)
        self.assertEqual(0, self.origin.accepted)


class DecisionTests(unittest.TestCase):
    """CN-2: the per-run decision (pure): the client's subnet names the run, the run names its hosts."""

    POLICY = {"version": 1, "runs": [
        {"run": "run:claude", "subnets": ["10.89.1.0/24"], "allow": ["api.anthropic.com"]},
        {"run": "run:codex", "subnets": ["10.89.2.0/24"], "allow": ["chatgpt.com", "auth.openai.com"]},
    ]}

    def decide(self, client, target_host, port=443):
        return ep.decide(ep.validate_policy(self.POLICY), client, target_host, port, frozenset({443}))

    def test_each_run_reaches_only_its_own_hosts(self):
        self.assertTrue(self.decide("10.89.1.5", "api.anthropic.com").allowed)
        self.assertFalse(self.decide("10.89.1.5", "chatgpt.com").allowed)
        self.assertTrue(self.decide("10.89.2.5", "chatgpt.com").allowed)
        self.assertTrue(self.decide("10.89.2.5", "AUTH.OpenAI.com.").allowed)
        self.assertFalse(self.decide("10.89.2.5", "api.anthropic.com").allowed)
        self.assertFalse(self.decide("10.89.2.5", "api.openai.com").allowed)
        self.assertEqual("run:codex", self.decide("10.89.2.5", "chatgpt.com").run)

    def test_unknown_clients_ips_and_other_ports_are_denied(self):
        self.assertIsNone(self.decide("10.89.3.5", "chatgpt.com").run)
        self.assertFalse(self.decide("10.89.3.5", "chatgpt.com").allowed)
        self.assertFalse(self.decide("10.89.2.5", "10.89.1.5").allowed)
        self.assertFalse(self.decide("10.89.2.5", "chatgpt.com", 80).allowed)

    def test_overlapping_runs_are_refused_as_a_policy(self):
        policy = {"version": 1, "runs": [
            {"run": "run:a", "subnets": ["10.89.1.0/24"], "allow": ["chatgpt.com"]},
            {"run": "run:b", "subnets": ["10.89.0.0/16"], "allow": ["chatgpt.com"]}]}
        with self.assertRaises(ValueError):
            ep.validate_policy(policy)

    def test_policy_values_are_checked(self):
        for runs in ([{"run": "run:a", "subnets": ["0.0.0.0/0"], "allow": ["chatgpt.com"]}],
                     [{"run": "run:a", "subnets": ["10.89.1.0/24"], "allow": ["10.0.0.1"]}],
                     [{"run": "run:a", "subnets": ["10.89.1.0/24"], "allow": ["*.openai.com"]}],
                     [{"run": "run:a", "subnets": ["nonsense"], "allow": ["chatgpt.com"]}],
                     [{"run": "", "subnets": ["10.89.1.0/24"], "allow": ["chatgpt.com"]}]):
            with self.subTest(runs), self.assertRaises(ValueError):
                ep.validate_policy({"version": 1, "runs": runs})


# --------------------------------------------------------------------------------------------
# CN-3 The record
# --------------------------------------------------------------------------------------------

class RecordTests(LocalProxyCase):
    """CN-3: every attempt recorded (run, host, port, allow/deny, time), readable, bounded."""

    def test_every_attempt_is_recorded(self):
        proxy = self.start()
        for target in (f"api.anthropic.com:{self.origin.port}", f"example.com:{self.origin.port}",
                       f"127.0.0.1:{self.origin.port}"):
            status, connection, _ = connect(proxy.port, target)
            connection.close()
        deadline = time.monotonic() + 2
        while len(ep.read_records(self.records)) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        rows = ep.read_records(self.records)
        self.assertEqual([("run:a", "api.anthropic.com", self.origin.port, "allow"),
                          ("run:a", "example.com", self.origin.port, "deny"),
                          ("run:a", "127.0.0.1", self.origin.port, "deny")],
                         [(row["run"], row["host"], row["port"], row["decision"]) for row in rows])
        for row in rows:
            self.assertEqual("127.0.0.1", row["client"])
            self.assertRegex(row["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
            self.assertTrue(row["reason"])
        self.assertEqual(rows[:1], ep.read_records(self.records, "run:a")[:1])
        self.assertEqual([], ep.read_records(self.records, "run:other"))

    def test_the_record_is_bounded_by_rotation(self):
        log = ep.RecordLog(self.records, max_bytes=2000)
        for number in range(200):
            log.write({"time": "2026-11-21T00:00:00Z", "run": "run:a", "client": "10.89.1.5",
                       "host": f"host{number}.example", "port": 443, "decision": "deny", "reason": "not allowed"})
        files = sorted(os.listdir(self.records))
        self.assertEqual([ep.RECORD_FILE, ep.RECORD_FILE + ".1"], files)
        for name in files:
            self.assertLessEqual(os.path.getsize(os.path.join(self.records, name)), 2000)
        rows = ep.read_records(self.records)
        self.assertEqual("host199.example", rows[-1]["host"])
        hosts = [int(row["host"][4:-8]) for row in rows]
        self.assertEqual(sorted(hosts), hosts)

    def test_unreadable_lines_are_skipped_not_fatal(self):
        Path(self.records, ep.RECORD_FILE).write_text('{"run": "run:a", "host": "x", "decision": "deny"}\nnot json\n')
        self.assertEqual(["x"], [row["host"] for row in ep.read_records(self.records)])
        self.assertEqual([], ep.read_records(os.path.join(self.root, "missing")))

    def test_hostile_targets_are_recorded_safely(self):
        proxy = self.start()
        status, connection, _ = connect(proxy.port, "evil\x1b[31m.example:443")
        connection.close()
        row = self.records_now()[-1]
        self.assertEqual("deny", row["decision"])
        self.assertLessEqual(len(row["host"]), 255)
        self.assertNotIn("\x1b", Path(self.records, ep.RECORD_FILE).read_text())


class PolicyCommandTests(unittest.TestCase):
    """The proxy's own policy writer (run inside the proxy container through podman exec)."""

    def test_set_policy_writes_atomically_and_prints_its_digest(self):
        with tempfile.TemporaryDirectory(dir=SHORT_TMP) as folder:
            path = os.path.join(folder, "egress", "policy.json")
            policy = {"version": 1, "runs": [{"run": "run:a", "subnets": ["10.89.1.0/24"], "allow": ["chatgpt.com"]}]}
            output = []
            code = ep.main(["--policy", path, "--set-policy", json.dumps(policy)], out=output.append)
            self.assertEqual(0, code)
            self.assertEqual([ep.policy_digest(ep.validate_policy(policy))], output)
            self.assertEqual(ep.validate_policy(policy), ep.validate_policy(json.loads(Path(path).read_text())))
            self.assertEqual(["policy.json"], os.listdir(os.path.dirname(path)))
            code = ep.main(["--policy", path, "--set-policy", "{broken"], out=output.append)
            self.assertNotEqual(0, code)
            self.assertEqual(ep.validate_policy(policy), ep.validate_policy(json.loads(Path(path).read_text())))


# --------------------------------------------------------------------------------------------
# Fake podman for networks and the proxy container
# --------------------------------------------------------------------------------------------

def option_values(args, flag):
    prefix = flag + "="
    return [arg[len(prefix):] for arg in args if arg.startswith(prefix)]


class FakeNetPodman:
    def __init__(self):
        self.networks: dict[str, dict] = {"podman": {"name": "podman", "internal": False, "dns_enabled": True,
                                                     "labels": {}, "subnets": [{"subnet": "10.88.0.0/16"}],
                                                     "containers": {}}}
        self.containers: dict[str, dict] = {}
        self.images = {"python:3.12-slim"}
        self.calls: list[tuple[str, ...]] = []
        self.policy = None
        self.policy_digest_override = None
        self.listening = True
        self.missing = self.unreachable = self.time_out = False
        self.create_fails = False
        self.network_create_ignores_internal = False
        self.connect_fails = False
        self._subnet = 0

    def commands(self, *head):
        return [call for call in self.calls if call[:len(head)] == head]

    def add_network(self, name, labels, internal=True, dns=False):
        self._subnet += 1
        self.networks[name] = {"name": name, "internal": internal, "dns_enabled": dns, "labels": dict(labels),
                               "subnets": [{"subnet": f"10.89.{self._subnet}.0/24"}], "containers": {}}
        return self.networks[name]

    def add_container(self, name, labels, network=None, running=True, args=()):
        self.containers[name] = {"name": name, "id": secrets.token_hex(32), "labels": dict(labels),
                                 "status": "running" if running else "exited", "networks": {}, "args": list(args)}
        if network:
            self._attach(network, name)
        return self.containers[name]

    def _attach(self, network, name):
        net = self.networks[network]
        subnet = net["subnets"][0]["subnet"].rsplit(".", 1)[0]
        address = f"{subnet}.{len(net['containers']) + 2}"
        net["containers"][name] = address
        self.containers[name]["networks"][network] = address

    def _json(self, value):
        return cr.PodmanResult(0, json.dumps(value), "")

    def _net_view(self, net):
        return {key: value for key, value in net.items() if key != "containers"}

    def __call__(self, args, timeout):
        args = tuple(args)
        self.calls.append(args)
        if self.missing:
            raise FileNotFoundError("podman")
        if self.time_out:
            raise subprocess.TimeoutExpired(["podman", *args], timeout)
        if self.unreachable:
            return cr.PodmanResult(125, "", "Cannot connect to Podman. Please verify your connection to the Linux "
                                            "system\nError: unable to connect to Podman socket: no such file")
        if args[:2] == ("network", "inspect"):
            net = self.networks.get(args[-1])
            if net is None:
                return cr.PodmanResult(125, "", f"Error: network {args[-1]}: unable to find network: network not found")
            return self._json([self._net_view(net)])
        if args[:2] == ("network", "ls"):
            wanted = [value[len("label="):].split("=", 1) for value in option_values(args, "--filter")]
            rows = [self._net_view(net) for net in self.networks.values()
                    if all(net["labels"].get(key) == value for key, value in wanted)]
            return self._json(rows)
        if args[:2] == ("network", "create"):
            name = args[-1]
            if name in self.networks:
                return cr.PodmanResult(125, "", f"Error: network name {name} already used: network already exists")
            labels = dict(value.split("=", 1) for value in option_values(args, "--label"))
            internal = "--internal" in args and not self.network_create_ignores_internal
            self.add_network(name, labels, internal=internal, dns="--disable-dns" not in args)
            return cr.PodmanResult(0, name + "\n", "")
        if args[:2] == ("network", "connect"):
            network, name = args[-2], args[-1]
            if self.connect_fails or network not in self.networks or name not in self.containers:
                return cr.PodmanResult(125, "", "Error: unable to connect")
            if network in self.containers[name]["networks"]:
                return cr.PodmanResult(125, "", f"Error: container {name} is already connected to network {network}")
            self._attach(network, name)
            return cr.PodmanResult(0, "", "")
        if args[:2] == ("network", "disconnect"):
            network, name = args[-2], args[-1]
            container = self.containers.get(name)
            if container is None or network not in container["networks"]:
                return cr.PodmanResult(125, "", f"Error: container {name} is not connected to network {network}")
            del container["networks"][network]
            del self.networks[network]["containers"][name]
            return cr.PodmanResult(0, "", "")
        if args[:2] == ("network", "rm"):
            name = args[-1]
            if "--force" in args or "-f" in args:
                raise AssertionError("network rm --force removes containers: never used")
            net = self.networks.get(name)
            if net is None:
                return cr.PodmanResult(1, "", f"Error: unable to find network with name or ID {name}: network not found")
            if net["containers"]:
                return cr.PodmanResult(2, "", f'Error: "{name}" has associated containers with it. Use -f to forcibly '
                                              "delete containers and pods: network is being used")
            del self.networks[name]
            return cr.PodmanResult(0, name + "\n", "")
        if args[0] == "create":
            name = option_values(args, "--name")[0]
            if self.create_fails:
                return cr.PodmanResult(125, "", "Error: something failed")
            if name in self.containers:
                return cr.PodmanResult(125, "", f'Error: the container name "{name}" is already in use')
            image = next(arg for arg in args[1:] if not arg.startswith("-"))
            if image not in self.images:
                return cr.PodmanResult(125, "", f"Error: {image}: image not known")
            labels = dict(value.split("=", 1) for value in option_values(args, "--label"))
            network = option_values(args, "--network")[0]
            container = self.add_container(name, labels, running=False, args=args)
            if network in self.networks:
                self._attach(network, name)
            container["status"] = "created"
            return cr.PodmanResult(0, container["id"] + "\n", "")
        if args[0] == "start":
            container = self.containers.get(args[-1])
            if container is None:
                return cr.PodmanResult(125, "", "Error: no such container")
            container["status"] = "running"
            return cr.PodmanResult(0, args[-1] + "\n", "")
        if args[:2] == ("container", "inspect"):
            container = self.containers.get(args[-1])
            if container is None:
                return cr.PodmanResult(125, "[]", f'Error: no such container "{args[-1]}"')
            return self._json([{
                "Id": container["id"], "Name": container["name"],
                "State": {"Status": container["status"], "Running": container["status"] == "running"},
                "Config": {"Labels": container["labels"]},
                "NetworkSettings": {"Networks": {net: {"IPAddress": address}
                                                 for net, address in container["networks"].items()}},
            }])
        if args[0] == "rm":
            name = args[-1]
            container = self.containers.pop(name, None)
            if container is not None:
                for network in container["networks"]:
                    self.networks[network]["containers"].pop(name, None)
            return cr.PodmanResult(0, "", "")
        if args[0] == "stop":
            container = self.containers.get(args[-1])
            if container is not None:
                container["status"] = "exited"
            return cr.PodmanResult(0, "", "")
        if args[0] == "exec":
            container = self.containers.get(args[1])
            if container is None or container["status"] != "running":
                return cr.PodmanResult(125, "", "Error: can only create exec sessions on running containers")
            command = list(args[2:])
            if "--set-policy" in command:
                text = command[command.index("--set-policy") + 1]
                policy = ep.validate_policy(json.loads(text))
                self.policy = policy
                digest = self.policy_digest_override or ep.policy_digest(policy)
                return cr.PodmanResult(0, digest + "\n", "")
            if "--check" in command:
                return cr.PodmanResult(0 if self.listening else 1, "", "" if self.listening else "refused")
            return cr.PodmanResult(127, "", "Error: executable file not found")
        return cr.PodmanResult(125, "", "Error: unknown command " + " ".join(args))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class NetworkCase(unittest.TestCase):
    project = "cocolo-test-project"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.root = os.path.realpath(folder.name)
        self.records = os.path.join(self.root, "records")
        os.mkdir(self.records)
        self.podman = FakeNetPodman()
        self.clock = FakeClock()
        self.networks = self.make()

    def make(self, project=None, **options):
        return rn.RunNetworks(project or self.project, self.records, "python:3.12-slim", runner=self.podman,
                              monotonic=self.clock.monotonic, sleep=self.clock.sleep, **options)

    def assert_refused(self, result, reason):
        self.assertIsInstance(result, cr.Refusal, result)
        self.assertEqual(reason, result.reason, result.message)
        self.assertTrue(result.message and "\n" not in result.message and "Traceback" not in result.message)

    def prepared(self, run_id="run:a", provider="codex"):
        result = self.networks.prepare_run(run_id, provider)
        self.assertIsInstance(result, rn.RunNetwork, result)
        return result

    def proxy(self):
        return self.podman.containers[self.networks.proxy_name]


# --------------------------------------------------------------------------------------------
# CN-1 One internal network per run
# --------------------------------------------------------------------------------------------

class PerRunNetworkTests(NetworkCase):
    """CN-1: created internal with COCOLO labels, never shared, removed when the run is gone, swept."""

    def test_a_run_gets_its_own_internal_network_with_labels(self):
        network = self.prepared("run:a", "codex")
        created = self.podman.commands("network", "create")[-1]
        self.assertEqual(network.name, created[-1])
        self.assertIn("--internal", created)
        self.assertIn("--disable-dns", created)
        labels = dict(value.split("=", 1) for value in option_values(created, "--label"))
        self.assertEqual({cr.RUN_LABEL: "run:a", cr.PROJECT_LABEL: self.project, rn.ROLE_LABEL: rn.RUN_ROLE,
                          rn.PROVIDER_LABEL: "codex"}, labels)
        self.assertEqual(network.name, self.networks.network_name("run:a"))
        self.assertNotEqual(network.name, self.networks.network_name("run:b"))
        self.assertNotEqual(network.name, self.make("cocolo-test-other").network_name("run:a"))
        self.assertLessEqual(len(network.name), 63)
        self.assertEqual("10.89.2.0/24", network.subnet)

    def test_a_network_is_never_shared_between_runs(self):
        self.prepared("run:a")
        second = self.networks.prepare_run("run:a", "codex")
        self.assert_refused(second, "network_in_use")
        b = self.prepared("run:b", "claude")
        self.assertNotEqual(self.networks.network_name("run:a"), b.name)
        self.assertNotEqual(self.podman.networks[self.networks.network_name("run:a")]["subnets"],
                            self.podman.networks[b.name]["subnets"])

    def test_release_disconnects_the_proxy_then_removes_the_network(self):
        network = self.prepared("run:a")
        before = len(self.podman.calls)
        released = self.networks.release_run("run:a")
        self.assertEqual("removed", released.outcome, released)
        self.assertNotIn(network.name, self.podman.networks)
        self.assertNotIn(network.name, self.proxy()["networks"])
        heads = [call[:2] for call in self.podman.calls[before:]]
        self.assertLess(heads.index(("network", "disconnect")), heads.index(("network", "rm")))
        self.assertEqual([], self.podman.policy["runs"])
        self.assertEqual("absent", self.networks.release_run("run:a").outcome)

    def test_release_of_a_network_still_in_use_keeps_it_and_says_so(self):
        network = self.prepared("run:a")
        self.podman.add_container("cocolo-run-x", {}, network=network.name)
        released = self.networks.release_run("run:a")
        self.assertEqual("in_use", released.outcome)
        self.assertIn(network.name, self.podman.networks)
        # Still cut off: the proxy left it and its policy entry is gone.
        self.assertNotIn(network.name, self.proxy()["networks"])
        self.assertEqual([], self.podman.policy["runs"])

    def test_release_leaves_a_network_that_is_not_this_runs_alone(self):
        name = self.networks.network_name("run:a")
        self.podman.add_network(name, {cr.PROJECT_LABEL: "someone-else"})
        released = self.networks.release_run("run:a")
        self.assertEqual("skipped", released.outcome)
        self.assertIn(name, self.podman.networks)
        self.assertEqual([], self.podman.commands("network", "rm"))

    def test_the_sweep_removes_networks_of_runs_that_are_gone(self):
        live = self.prepared("run:live")
        orphan = self.prepared("run:orphan", "claude")
        other = self.make("cocolo-test-other")
        foreign = self.podman.add_network(other.network_name("run:orphan"),
                                          {cr.PROJECT_LABEL: "cocolo-test-other", cr.RUN_LABEL: "run:orphan",
                                           rn.ROLE_LABEL: rn.RUN_ROLE})
        bare = self.podman.add_network("cocolo-test-bare", {})
        odd = self.podman.add_network("cocolo-test-odd", {cr.PROJECT_LABEL: self.project, cr.RUN_LABEL: "run:odd",
                                                          rn.ROLE_LABEL: rn.RUN_ROLE})
        report = self.networks.sweep_networks({"run:live"})
        self.assertIsNone(report.refusal)
        outcomes = {entry.run_id: entry.outcome for entry in report.entries}
        self.assertEqual({"run:orphan": "removed", "run:odd": "skipped"}, outcomes)
        self.assertEqual(1, report.kept)
        self.assertNotIn(orphan.name, self.podman.networks)
        for name in (live.name, foreign["name"], bare["name"], odd["name"], self.networks.egress_name):
            self.assertIn(name, self.podman.networks)
        self.assertEqual(["run:live"], [run["run"] for run in self.podman.policy["runs"]])


# --------------------------------------------------------------------------------------------
# CN-2 One proxy, per-run allowlists
# --------------------------------------------------------------------------------------------

class ProxyContainerTests(NetworkCase):
    """CN-2: one hardened proxy on its own egress network, connected to each run's network."""

    def test_one_hardened_proxy_on_its_own_egress_network(self):
        ready = self.networks.ensure_proxy()
        self.assertIsInstance(ready, rn.ProxyReady, ready)
        egress = self.podman.networks[self.networks.egress_name]
        self.assertFalse(egress["internal"])
        self.assertEqual({cr.PROJECT_LABEL: self.project, rn.ROLE_LABEL: rn.EGRESS_ROLE}, egress["labels"])
        args = self.podman.commands("create")[0]
        for expected in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never",
                         "--userns=keep-id:uid=1000,gid=1000", f"--network={self.networks.egress_name}",
                         f"--name={self.networks.proxy_name}", f"--volume={self.records}:/records:rw"):
            self.assertIn(expected, args)
        self.assertTrue(option_values(args, "--pids-limit") and option_values(args, "--memory"))
        self.assertEqual([f"{self.records}:/records:rw"], option_values(args, "--volume"))
        self.assertFalse([arg for arg in args if arg.startswith(("--privileged", "--cap-add", "--network=host",
                                                                 "--network=podman", "-v", "--mount"))])
        labels = self.proxy()["labels"]
        # Not a run's container: the container sweep (cocolo.project) must never see it.
        self.assertNotIn(cr.PROJECT_LABEL, labels)
        self.assertEqual(self.project, labels[rn.PROXY_LABEL])
        self.assertEqual(ep.validate_policy({"version": 1, "runs": []}), self.podman.policy)
        # Its program is the stdlib proxy module itself, run isolated.
        self.assertIn(Path(ep.__file__).read_text(), args)
        self.assertIn("-I", args)
        again = self.networks.ensure_proxy()
        self.assertIsInstance(again, rn.ProxyReady)
        self.assertEqual(1, len(self.podman.commands("create")))

    def test_each_run_gets_only_its_providers_hosts(self):
        codex = self.prepared("run:codex", "codex")
        claude = self.prepared("run:claude", "claude")
        self.assertEqual({codex.name, claude.name, self.networks.egress_name}, set(self.proxy()["networks"]))
        runs = {run["run"]: run for run in self.podman.policy["runs"]}
        self.assertEqual(["auth.openai.com", "chatgpt.com"], sorted(runs["run:codex"]["allow"]))
        self.assertEqual(list(rn.PROVIDER_HOSTS["claude"]), runs["run:claude"]["allow"])
        self.assertIn("api.anthropic.com", rn.PROVIDER_HOSTS["claude"])
        self.assertEqual([codex.subnet], runs["run:codex"]["subnets"])
        self.assertEqual([claude.subnet], runs["run:claude"]["subnets"])
        self.assertEqual(sorted(codex.allow), sorted(runs["run:codex"]["allow"]))
        self.assertEqual(self.proxy()["networks"][codex.name], codex.proxy_address)

    def test_an_unknown_provider_is_refused_without_a_network(self):
        result = self.networks.prepare_run("run:a", "gemini")
        self.assert_refused(result, "invalid_request")
        self.assertEqual([], self.podman.commands("network", "create"))

    def test_a_stale_proxy_is_replaced_and_reconnected(self):
        network = self.prepared("run:a")
        proxy = self.proxy()
        proxy["labels"][rn.DIGEST_LABEL] = "0" * 64
        ready = self.networks.ensure_proxy()
        self.assertIsInstance(ready, rn.ProxyReady, ready)
        self.assertEqual(2, len(self.podman.commands("create")))
        self.assertIn(network.name, self.proxy()["networks"])
        self.assertEqual(["run:a"], [run["run"] for run in self.podman.policy["runs"]])

    def test_a_stopped_proxy_is_replaced(self):
        self.prepared("run:a")
        self.proxy()["status"] = "exited"
        self.assertIsInstance(self.networks.ensure_proxy(), rn.ProxyReady)
        self.assertEqual("running", self.proxy()["status"])

    def test_a_foreign_container_or_network_under_the_proxy_names_is_refused(self):
        self.podman.add_container(self.networks.proxy_name, {"someone": "else"})
        self.assert_refused(self.networks.ensure_proxy(), "proxy_name_in_use")
        self.assertIn(self.networks.proxy_name, self.podman.containers)
        other = self.make("cocolo-test-p2")
        self.podman.add_network(other.egress_name, {}, internal=False)
        self.assert_refused(other.ensure_proxy(), "egress_network_foreign")

    def test_records_are_read_for_the_supervisor(self):
        ep.RecordLog(self.records, max_bytes=10_000).write(
            {"time": "2026-11-21T00:00:00Z", "run": "run:a", "client": "10.89.1.2", "host": "chatgpt.com",
             "port": 443, "decision": "allow", "reason": "on the run's list"})
        self.assertEqual(["chatgpt.com"], [row["host"] for row in self.networks.records("run:a")])
        self.assertEqual([], self.networks.records("run:b"))

    def test_invalid_setup_is_refused_without_podman(self):
        for label, call in (
                ("relative records", lambda: rn.RunNetworks(self.project, "records", "python:3.12-slim",
                                                            runner=self.podman)),
                ("bad project", lambda: rn.RunNetworks("a b", self.records, "python:3.12-slim", runner=self.podman)),
                ("bad image", lambda: rn.RunNetworks(self.project, self.records, "--privileged",
                                                     runner=self.podman))):
            with self.subTest(label), self.assertRaises(ValueError):
                call()
        self.assert_refused(self.networks.prepare_run("run:x\nmore", "codex"), "invalid_request")
        self.assertEqual([], self.podman.calls)


# --------------------------------------------------------------------------------------------
# CN-4 Launch
# --------------------------------------------------------------------------------------------

class LaunchTests(NetworkCase):
    """CN-4: the child gets the proxy variables and its own network; commands-only stays --network none."""

    def base_spec(self, **changes):
        workspace = os.path.join(self.root, "ws")
        os.makedirs(workspace, exist_ok=True)
        values = dict(run_id="run:a", image="python:3.12-slim", command=("claude", "-p", "x"),
                      workspace=workspace, seccomp_profile=os.path.join(self.root, "seccomp.json"))
        values.update(changes)
        return cr.ContainerSpec(**values)

    def test_a_client_spec_carries_the_proxy_and_its_own_network(self):
        network = self.prepared("run:a", "claude")
        spec = rn.client_spec(self.base_spec(environment={"TERM": "dumb"}), network)
        self.assertEqual(network.name, spec.network)
        url = f"http://{network.proxy_address}:{ep.PROXY_PORT}"
        self.assertEqual(url, network.proxy_url)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(url, spec.environment[name])
        for name in ("NO_PROXY", "no_proxy"):
            self.assertEqual("localhost,127.0.0.1,::1", spec.environment[name])
        self.assertEqual("dumb", spec.environment["TERM"])
        args = cr.ContainerRuntime(self.project, runner=self.podman).create_arguments(
            spec, cr.ContainerRuntime(self.project, runner=self.podman).identity("run:a"))
        self.assertIn(f"--network={network.name}", args)
        self.assertIn(f"--env=HTTPS_PROXY={url}", args)

    def test_a_client_spec_refuses_anything_but_its_own_network(self):
        network = self.prepared("run:a", "claude")
        for label, spec, net in (
                ("no network", self.base_spec(), None),
                ("another run's network", self.base_spec(run_id="run:b"), network),
                ("caller's own proxy", self.base_spec(environment={"HTTPS_PROXY": "http://evil:1"}), network),
                ("caller's lowercase proxy", self.base_spec(environment={"all_proxy": "socks5://evil:1"}), network),
                ("another network already set", self.base_spec(network="podman"), network)):
            with self.subTest(label), self.assertRaises(ValueError):
                rn.client_spec(spec, net)

    def test_commands_only_runs_keep_no_network(self):
        spec = self.base_spec()
        self.assertIsNone(spec.network)
        args = cr.ContainerRuntime(self.project, runner=self.podman).create_arguments(
            spec, cr.ContainerRuntime(self.project, runner=self.podman).identity("run:a"))
        self.assertIn("--network=none", args)
        self.assertFalse([arg for arg in args if "PROXY" in arg.upper()])


# --------------------------------------------------------------------------------------------
# CN-5 Fail closed
# --------------------------------------------------------------------------------------------

class FailClosedTests(NetworkCase):
    """CN-5: no proxy, no network: never a fallback; plain refusals without podman."""

    def assert_no_run_network(self):
        self.assertFalse([name for name, net in self.podman.networks.items()
                          if net["labels"].get(rn.ROLE_LABEL) == rn.RUN_ROLE])

    def test_a_proxy_that_cannot_be_created_means_no_run_network(self):
        self.podman.create_fails = True
        self.assert_refused(self.networks.prepare_run("run:a", "codex"), "proxy_unavailable")
        self.assert_no_run_network()

    def test_a_proxy_that_does_not_answer_means_no_run_network(self):
        self.podman.listening = False
        result = self.networks.prepare_run("run:a", "codex")
        self.assert_refused(result, "proxy_unavailable")
        self.assert_no_run_network()
        self.assertNotIn(self.networks.proxy_name, self.podman.containers)

    def test_a_missing_proxy_image_is_refused_and_never_pulled(self):
        networks = rn.RunNetworks(self.project, self.records, "example.org/nosuch:1", runner=self.podman,
                                  monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.assert_refused(networks.ensure_proxy(), "proxy_unavailable")
        self.assertIn("--pull=never", self.podman.commands("create")[0])

    def test_a_network_that_came_out_not_internal_is_removed_and_refused(self):
        self.podman.network_create_ignores_internal = True
        result = self.networks.prepare_run("run:a", "codex")
        self.assert_refused(result, "network_not_internal")
        self.assert_no_run_network()

    def test_a_policy_the_proxy_did_not_take_is_refused_and_undone(self):
        self.assertIsInstance(self.networks.ensure_proxy(), rn.ProxyReady)
        self.podman.policy_digest_override = "f" * 64
        result = self.networks.prepare_run("run:a", "codex")
        self.assert_refused(result, "proxy_policy_failed")
        self.assert_no_run_network()

    def test_a_failed_connect_is_refused_and_undone(self):
        self.assertIsInstance(self.networks.ensure_proxy(), rn.ProxyReady)
        self.podman.connect_fails = True
        self.assert_refused(self.networks.prepare_run("run:a", "codex"), "proxy_unavailable")
        self.assert_no_run_network()

    def test_the_egress_network_is_never_handed_to_a_run(self):
        self.assertIsInstance(self.networks.ensure_proxy(), rn.ProxyReady)
        self.podman.networks[self.networks.egress_name]["labels"][cr.RUN_LABEL] = "run:a"
        network = self.prepared("run:a")
        self.assertNotEqual(self.networks.egress_name, network.name)
        self.assertTrue(self.podman.networks[network.name]["internal"])

    def test_unavailable_podman_is_a_plain_refusal_everywhere(self):
        for flag, reason in (("missing", "podman_missing"), ("unreachable", "podman_unavailable"),
                             ("time_out", "podman_timeout")):
            with self.subTest(flag):
                podman = FakeNetPodman()
                setattr(podman, flag, True)
                networks = rn.RunNetworks(self.project, self.records, "python:3.12-slim", runner=podman,
                                          monotonic=self.clock.monotonic, sleep=self.clock.sleep)
                self.assert_refused(networks.ensure_proxy(), reason)
                self.assert_refused(networks.prepare_run("run:a", "codex"), reason)
                released = networks.release_run("run:a")
                self.assertEqual("refused", released.outcome)
                self.assertEqual(reason, released.refusal.reason)
                self.assertEqual(reason, networks.sweep_networks(set()).refusal.reason)
                self.assertEqual(reason, networks.stop_proxy().refusal.reason)

    def test_stop_proxy_removes_the_proxy_and_its_egress_network_only(self):
        network = self.prepared("run:a")
        stopped = self.networks.stop_proxy()
        self.assertEqual("removed", stopped.outcome, stopped)
        self.assertNotIn(self.networks.proxy_name, self.podman.containers)
        self.assertNotIn(self.networks.egress_name, self.podman.networks)
        self.assertIn(network.name, self.podman.networks)


# --------------------------------------------------------------------------------------------
# Real podman (skipped unless podman, its machine and python:3.12-slim are all present)
# --------------------------------------------------------------------------------------------

REAL_IMAGE = "python:3.12-slim"


def real_podman_reason():
    path = next((p for p in cr.PODMAN_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if path is None:
        return None, "podman is not installed"
    try:
        info = subprocess.run([path, "info", "--format", "{{.Host.NetworkBackend}}"], capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "podman did not answer within 30 s"
    if info.returncode != 0:
        return None, "podman's machine or service is not running: " + (info.stderr.strip().splitlines() or [""])[0]
    if info.stdout.strip() != "netavark":
        return None, f"the podman host uses the {info.stdout.strip()} network backend, not netavark"
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

# Run inside a child: each probe's outcome goes to /workspace/<name>.json.
PROBE = r"""
import json, os, socket, ssl, sys, threading, time

def proxy_parts():
    url = os.environ.get("HTTPS_PROXY", "")
    host, _, port = url.rsplit("/", 1)[-1].rpartition(":")
    return host, int(port or 0)

def via_proxy(target):
    host, port = proxy_parts()
    s = socket.create_connection((host, port), timeout=8)
    s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    return s, head.split(b"\r\n", 1)[0].decode("latin-1")

def run(kind, *args):
    try:
        if kind == "tls":
            s, status = via_proxy(args[0] + ":443")
            if " 200 " not in status:
                return status
            with ssl.create_default_context().wrap_socket(s, server_hostname=args[0]) as t:
                return "tls " + t.version()
        if kind == "connect":
            s, status = via_proxy(args[0])
            s.close()
            return status
        if kind == "direct":
            socket.create_connection((args[0], int(args[1])), timeout=4).close()
            return "open"
    except Exception as error:
        return "error " + type(error).__name__

name, listen = sys.argv[1], int(sys.argv[2])
if listen:
    server = socket.socket()
    server.bind(("0.0.0.0", listen))
    server.listen(8)
    threading.Thread(target=lambda: [server.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()
results = {" ".join(map(str, probe)): run(*probe) for probe in json.loads(sys.argv[3])}
open("/workspace/%s.json" % name, "w").write(json.dumps(results))
time.sleep(float(sys.argv[4]))
"""


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealNetworkTests(unittest.TestCase):
    """Real networks, proxy and children in the existing podman machine; names start cocolo-test-."""

    @classmethod
    def setUpClass(cls):
        runner = cr.SubprocessPodman(REAL_PODMAN)
        cls.host = cr.default_host_files(runner)
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
        folder = tempfile.TemporaryDirectory(prefix="cocolo-test-", dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.root = os.path.realpath(folder.name)
        self.records = os.path.join(self.root, "records")
        os.mkdir(self.records)
        runner = cr.SubprocessPodman(REAL_PODMAN)
        self.runtime = cr.ContainerRuntime(self.project, name_prefix="cocolo-test-", runner=runner)
        self.networks = rn.RunNetworks(self.project, self.records, REAL_IMAGE, name_prefix="cocolo-test-",
                                       runner=runner)
        self.addCleanup(self.remove_test_objects)

    def podman(self, *args, timeout=60):
        return subprocess.run([REAL_PODMAN, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def remove_test_objects(self):
        """Remove exactly this test's containers and networks, by name, after checking the labels."""
        listing = self.podman("ps", "--all", f"--filter=label={cr.PROJECT_LABEL}={self.project}", "--format=json")
        for row in json.loads(listing.stdout or "[]"):
            name = row["Names"][0]
            if name.startswith("cocolo-test-") and row["Labels"].get(cr.PROJECT_LABEL) == self.project:
                self.podman("rm", "--force", "--time=0", name)
        listing = self.podman("ps", "--all", f"--filter=label={rn.PROXY_LABEL}={self.project}", "--format=json")
        for row in json.loads(listing.stdout or "[]"):
            name = row["Names"][0]
            if name.startswith("cocolo-test-") and row["Labels"].get(rn.PROXY_LABEL) == self.project:
                self.podman("rm", "--force", "--time=0", name)
        listing = self.podman("network", "ls", f"--filter=label={cr.PROJECT_LABEL}={self.project}", "--format=json")
        for row in json.loads(listing.stdout or "[]"):
            if row["name"].startswith("cocolo-test-") and row.get("labels", {}).get(cr.PROJECT_LABEL) == self.project:
                self.podman("network", "rm", row["name"])

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return predicate()

    def child(self, network, name, probes, listen=0, linger=0.0):
        """Launch a hardened child on the run's network (or none) running PROBE; return (launched, workspace)."""
        workspace = os.path.join(self.root, name)
        os.mkdir(workspace)
        spec = cr.ContainerSpec(run_id=network.run_id if network else "run:" + name, image=REAL_IMAGE,
                                command=("python3", "-c", PROBE, name, str(listen), json.dumps(probes), str(linger)),
                                workspace=workspace, seccomp_profile=self.seccomp, workspace_writable=True)
        if network is not None:
            spec = rn.client_spec(spec, network)
        launched = self.runtime.launch(spec, lambda identity: None)
        self.assertTrue(launched.ok, launched)
        self.addCleanup(self.runtime.stop, launched.identity.name, grace_seconds=0)
        return launched, workspace

    def results(self, workspace, name, timeout=60):
        path = os.path.join(workspace, name + ".json")
        self.assertTrue(self.wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0, timeout),
                        f"{name} wrote no results")
        return json.loads(Path(path).read_text())

    def host_loopback_server(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def ip_on(self, container, network):
        info = json.loads(self.podman("container", "inspect", container).stdout)[0]
        return info["NetworkSettings"]["Networks"][network]["IPAddress"]

    def test_real_allowlist_isolation_and_record(self):
        """CN-2/3/4: allowed host via the proxy; denied host 403; direct IP and host loopback blocked;
        two runs cannot reach each other; each run only its own provider; the record shows each attempt."""
        started = time.monotonic()
        claude = self.networks.prepare_run("run:claude", "claude")
        self.assertIsInstance(claude, rn.RunNetwork, claude)
        codex = self.networks.prepare_run("run:codex", "codex")
        self.assertIsInstance(codex, rn.RunNetwork, codex)
        prepare_seconds = time.monotonic() - started
        for network in (claude, codex):
            info = json.loads(self.podman("network", "inspect", network.name).stdout)[0]
            self.assertEqual((True, False), (info["internal"], info["dns_enabled"]))
            self.assertEqual(network.run_id, info["labels"][cr.RUN_LABEL])
        loopback = self.host_loopback_server()
        # Control: a plain container on the proxy's egress network reaches the Mac's loopback server.
        control = "cocolo-test-ctl-" + secrets.token_hex(4)
        self.addCleanup(self.podman, "rm", "--force", "--time=0", control)
        ran = self.podman("run", "--name", control, "--label", f"{cr.PROJECT_LABEL}={self.project}", "--pull=never",
                          f"--network={self.networks.egress_name}", REAL_IMAGE, "python3", "-c",
                          "import socket;socket.create_connection(('host.containers.internal',%d),timeout=5).close();"
                          "print('open')" % loopback)
        control_reaches_loopback = ran.stdout.strip() == "open"
        # Run B (claude) listens on 8080 and probes its own provider and another's.
        b, b_workspace = self.child(claude, "b", [["tls", "api.anthropic.com"], ["connect", "chatgpt.com:443"],
                                                  ["direct", "127.0.0.1", 8080]], listen=8080, linger=60)
        b_results = self.results(b_workspace, "b")
        b_address = self.ip_on(b.identity.name, claude.name)
        proxy_on_b = self.ip_on(self.networks.proxy_name, claude.name)
        a_probes = [["tls", "chatgpt.com"], ["connect", "example.com:443"], ["connect", "api.anthropic.com:443"],
                    ["connect", "1.1.1.1:443"], ["direct", "1.1.1.1", 443],
                    ["connect", f"host.containers.internal:{loopback}"], ["direct", "host.containers.internal", loopback],
                    ["connect", f"{b_address}:8080"], ["direct", b_address, 8080], ["direct", proxy_on_b, 3128]]
        a, a_workspace = self.child(codex, "a", a_probes)
        a_results = self.results(a_workspace, "a")
        print(f"\n[CN real] prepare 2 runs {prepare_seconds:.2f} s; control reaches host loopback: "
              f"{control_reaches_loopback}\n  claude run: {b_results}\n  codex run: {a_results}", file=sys.stderr)
        self.assertTrue(b_results["tls api.anthropic.com"].startswith("tls TLSv1."), b_results)
        self.assertTrue(b_results["connect chatgpt.com:443"].startswith("HTTP/1.1 403"), b_results)
        self.assertEqual("open", b_results["direct 127.0.0.1 8080"])
        self.assertTrue(a_results["tls chatgpt.com"].startswith("tls TLSv1."), a_results)
        for probe in ("connect example.com:443", "connect api.anthropic.com:443", "connect 1.1.1.1:443",
                      f"connect host.containers.internal:{loopback}", f"connect {b_address}:8080"):
            self.assertTrue(a_results[probe].startswith("HTTP/1.1 403"), (probe, a_results[probe]))
        for probe in ("direct 1.1.1.1 443", f"direct host.containers.internal {loopback}",
                      f"direct {b_address} 8080", f"direct {proxy_on_b} 3128"):
            self.assertTrue(a_results[probe].startswith("error "), (probe, a_results[probe]))
        rows = self.networks.records()
        seen = {(row["run"], row["host"], row["decision"]) for row in rows}
        for expected in (("run:codex", "chatgpt.com", "allow"), ("run:codex", "example.com", "deny"),
                         ("run:codex", "api.anthropic.com", "deny"), ("run:codex", "1.1.1.1", "deny"),
                         ("run:codex", "host.containers.internal", "deny"), ("run:codex", b_address, "deny"),
                         ("run:claude", "api.anthropic.com", "allow"), ("run:claude", "chatgpt.com", "deny")):
            self.assertIn(expected, seen)
        print(f"  record: {len(rows)} rows, e.g. {rows[0]}", file=sys.stderr)

    def test_real_proxy_down_means_no_network(self):
        """CN-5: with the proxy stopped a child reaches nothing; ensure_proxy brings it back."""
        network = self.networks.prepare_run("run:down", "codex")
        self.assertIsInstance(network, rn.RunNetwork, network)
        self.assertEqual(0, self.podman("stop", "--time=0", self.networks.proxy_name).returncode)
        probes = [["tls", "chatgpt.com"], ["direct", "1.1.1.1", 443], ["direct", "host.containers.internal", 22]]
        _, workspace = self.child(network, "down", probes)
        down = self.results(workspace, "down")
        for probe, outcome in down.items():
            self.assertTrue(outcome.startswith("error "), (probe, outcome))
        ready = self.networks.ensure_proxy()
        self.assertIsInstance(ready, rn.ProxyReady, ready)
        again = self.networks.prepare_run("run:down", "codex")
        self.assertEqual("network_in_use", again.reason)
        network = self.networks.prepare_run("run:up", "codex")
        self.assertIsInstance(network, rn.RunNetwork, network)
        _, workspace = self.child(network, "up", [["tls", "chatgpt.com"]])
        up = self.results(workspace, "up")
        self.assertTrue(up["tls chatgpt.com"].startswith("tls TLSv1."), up)
        print(f"\n[CN-5 real] proxy stopped: {down}; restarted: {up}", file=sys.stderr)

    def test_real_release_and_sweep(self):
        """CN-1: the network goes with its run; a network still in use is kept; orphans are swept."""
        held = self.networks.prepare_run("run:held", "claude")
        self.assertIsInstance(held, rn.RunNetwork, held)
        launched, _ = self.child(held, "held", [], linger=60)
        released = self.networks.release_run("run:held")
        self.assertEqual("in_use", released.outcome, released)
        self.assertEqual(0, self.podman("network", "exists", held.name).returncode)
        self.assertEqual("gone", self.runtime.stop(launched.identity.name, grace_seconds=0).outcome)
        released = self.networks.release_run("run:held")
        self.assertEqual("removed", released.outcome, released)
        self.assertEqual(1, self.podman("network", "exists", held.name).returncode)
        orphan = self.networks.prepare_run("run:orphan", "codex")
        live = self.networks.prepare_run("run:live", "codex")
        report = self.networks.sweep_networks({"run:live"})
        self.assertIsNone(report.refusal)
        self.assertEqual([("run:orphan", "removed")], [(e.run_id, e.outcome) for e in report.entries])
        self.assertEqual(1, self.podman("network", "exists", orphan.name).returncode)
        self.assertEqual(0, self.podman("network", "exists", live.name).returncode)
        stopped = self.networks.stop_proxy()
        self.assertEqual("removed", stopped.outcome, stopped)
        self.assertEqual(1, self.podman("container", "exists", self.networks.proxy_name).returncode)

    def test_real_unreachable_podman_is_a_plain_refusal(self):
        runner = cr.SubprocessPodman(REAL_PODMAN, global_args=("--url", "unix:///private/tmp/cocolo-test-no.sock"))
        networks = rn.RunNetworks(self.project, self.records, REAL_IMAGE, name_prefix="cocolo-test-", runner=runner)
        self.assertEqual("podman_unavailable", networks.prepare_run("run:x", "codex").reason)
        self.assertEqual("refused", networks.release_run("run:x").outcome)
        self.assertEqual("podman_unavailable", networks.sweep_networks(set()).refusal.reason)


if __name__ == "__main__":
    unittest.main()
