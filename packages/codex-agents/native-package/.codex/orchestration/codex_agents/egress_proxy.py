"""COCOLO's allowlist proxy: the only way out of a run's internal network.

Owner decision 2026-11-21 (PLAN-0.4.0.md, "Containment decided"): each run has "a per-run network
allowing only its provider". Evidence: RESEARCH-0.4.0-podman-spike.md PC-3 (an internal network
plus an allowlist CONNECT proxy allowed the providers, denied everything else with 403, blocked
direct IPs and host loopback; both clients honour HTTPS_PROXY; the proxy log is the record).

Requirements served here (run_network.py serves the podman side):
- CN-2 HTTPS by CONNECT only, tunnelled byte for byte (no TLS interception). The client's address
  names its run (each run has its own subnet); the run names its hosts (exact names, no
  wildcards). Everything else is answered 403: other hosts, other ports, plain HTTP, IP literals in
  any form, a name that resolves to any non-public address (loopback, private, link-local, the
  podman host), a client on no run's network, and every request while the policy is missing or
  unreadable.
- CN-3 Every attempt is one JSON line (time, run, client, host, port, decision, reason) in
  `egress.log` in the records folder, written before the client is answered; an attempt that
  cannot be recorded is not allowed. The record is bounded: past `max_bytes` the file becomes
  `egress.log.1` (replacing the older one), so at most two files of `max_bytes` each.

The policy is pushed into the proxy container with `podman exec` (`--set-policy`, written
atomically to its own tmpfs, the digest printed back), never through a shared folder: on macOS a
file replaced in a virtiofs mount was missing or stale inside the VM for seconds.

Standard library only, and self-contained: run_network passes this file's text to
`python3 -I -c` inside the proxy container (Python 3.9 or newer), so it imports nothing else.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import socketserver
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

PROXY_PORT = 3128
POLICY_PATH = "/tmp/cocolo-egress/policy.json"
RECORDS_TARGET = "/records"
RECORD_FILE = "egress.log"
DEFAULT_MAX_BYTES = 1024 * 1024
ALLOWED_PORTS = frozenset({443})
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 3600.0
MAX_CONNECTIONS = 256
POLICY_VERSION = 1

_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
_UNPRINTABLE = re.compile(r"[^\x21-\x7e]")


# ------------------------------------------------------------------------------------------------
# Names, addresses and the policy
# ------------------------------------------------------------------------------------------------

def _looks_like_address(host: str) -> bool:
    """An IP literal in any form the resolver would accept (127.1, 0x7f000001, 2130706433, [::1])."""
    text = host.strip("[]")
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(text)
        return True
    except (OSError, ValueError):
        pass
    last = text.rstrip(".").rsplit(".", 1)[-1].lower()
    return last.isdigit() or bool(re.fullmatch(r"0x[0-9a-f]*", last))


def normalize_host(host: str) -> Optional[str]:
    """The lower-case host name without one trailing dot, or None when it is not a plain name."""
    if not isinstance(host, str):
        return None
    text = host.lower()
    if text.endswith("."):
        text = text[:-1]
    if not _HOST.match(text) or _looks_like_address(text):
        return None
    return text


def public_address(address: str) -> bool:
    """Only public unicast addresses: never loopback, private, link-local, shared or reserved."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_policy(value) -> dict:
    """The policy in its one canonical form, or ValueError. Subnets of different runs never overlap."""
    if not isinstance(value, dict) or set(value) != {"version", "runs"} or value["version"] != POLICY_VERSION:
        raise ValueError("the policy must be {version: 1, runs: [...]}")
    if not isinstance(value["runs"], list):
        raise ValueError("the policy's runs must be a list")
    runs, seen, networks = [], set(), []
    for entry in value["runs"]:
        if not isinstance(entry, dict) or set(entry) != {"run", "subnets", "allow"}:
            raise ValueError("each run must be {run, subnets, allow}")
        run = entry["run"]
        if not isinstance(run, str) or not _RUN.match(run) or run in seen:
            raise ValueError("each run needs its own plain run id")
        seen.add(run)
        if not isinstance(entry["subnets"], list) or not entry["subnets"]:
            raise ValueError(f"run {run} needs its subnets")
        subnets = []
        for text in entry["subnets"]:
            try:
                network = ipaddress.ip_network(text, strict=True)
            except (TypeError, ValueError):
                raise ValueError(f"run {run} has a subnet that is not one: {text!r}") from None
            if network.prefixlen < 8 or not network.is_private:
                raise ValueError(f"run {run}'s subnet {text} is not a private run network")
            subnets.append(network)
        if not isinstance(entry["allow"], list):
            raise ValueError(f"run {run}'s allowed hosts must be a list")
        allow = set()
        for host in entry["allow"]:
            name = normalize_host(host) if isinstance(host, str) else None
            if name is None or name != host:
                raise ValueError(f"run {run} allows something that is not a plain lower-case host name: {host!r}")
            allow.add(name)
        for network in subnets:
            for other_run, other in networks:
                if network.version == other.version and network.overlaps(other):
                    raise ValueError(f"runs {other_run} and {run} share addresses")
        networks.extend((run, network) for network in subnets)
        runs.append({"run": run, "subnets": sorted(str(network) for network in subnets), "allow": sorted(allow)})
    return {"version": POLICY_VERSION, "runs": sorted(runs, key=lambda item: item["run"])}


def policy_text(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))


def policy_digest(policy: dict) -> str:
    return hashlib.sha256(policy_text(policy).encode("utf-8")).hexdigest()


def load_policy(path: str) -> Optional[dict]:
    """The policy at path, or None when it is missing or unreadable (and everything is denied)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return validate_policy(json.load(handle))
    except (OSError, ValueError):
        return None


def write_policy(path: str, policy: dict) -> str:
    """Write the policy atomically (a temporary file in the same folder, then a rename); its digest."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".policy-", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(policy_text(policy))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return policy_digest(policy)


@dataclass(frozen=True)
class Decision:
    run: Optional[str]
    allowed: bool
    reason: str


def _client_address(client: str):
    try:
        ip = ipaddress.ip_address(client)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def decide(policy: Optional[dict], client: str, host: str, port: int, ports=ALLOWED_PORTS) -> Decision:
    """The client's subnet names its run; the run names its hosts; everything else is denied."""
    if policy is None:
        return Decision(None, False, "no readable policy: everything is denied")
    ip = _client_address(client)
    matches = [] if ip is None else [entry for entry in policy["runs"]
                                    if any(ip in ipaddress.ip_network(subnet) for subnet in entry["subnets"])]
    if len(matches) != 1:
        return Decision(None, False, "the client is on no run's network")
    entry = matches[0]
    run = entry["run"]
    if _looks_like_address(host):
        return Decision(run, False, "direct IP addresses are not allowed")
    name = normalize_host(host)
    if name is None:
        return Decision(run, False, "not a plain host name")
    if port not in ports:
        return Decision(run, False, f"port {port} is not allowed")
    if name not in entry["allow"]:
        return Decision(run, False, "not on the run's list")
    return Decision(run, True, "on the run's list")


# ------------------------------------------------------------------------------------------------
# The record
# ------------------------------------------------------------------------------------------------

def _printable(text, limit: int = 255) -> str:
    return _UNPRINTABLE.sub("?", str(text))[:limit]


def _now() -> str:
    seconds = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int(seconds % 1 * 1000):03d}Z"


class RecordLog:
    """JSON lines in `egress.log`; past max_bytes it becomes `egress.log.1` (two files at most)."""

    def __init__(self, directory: str, max_bytes: int = DEFAULT_MAX_BYTES):
        if type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError("max_bytes must be a whole number of bytes, 1024 or more")
        self.path = os.path.join(directory, RECORD_FILE)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        try:
            self.size = os.path.getsize(self.path)
        except OSError:
            self.size = 0

    def write(self, record: dict) -> None:
        line = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        with self.lock:
            if self.size and self.size + len(line) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
                self.size = 0
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
            self.size += len(line)


def read_records(directory: str, run_id: Optional[str] = None) -> list:
    """Every readable record, oldest first (the rotated file, then the current one)."""
    rows = []
    for name in (RECORD_FILE + ".1", RECORD_FILE):
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (run_id is None or row.get("run") == run_id):
                rows.append(row)
    return rows


# ------------------------------------------------------------------------------------------------
# The proxy
# ------------------------------------------------------------------------------------------------

def _resolve(host: str, port: int) -> list:
    addresses = []
    for *_, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if address[0] not in addresses:
            addresses.append(address[0])
    return addresses


def _read_head(connection: socket.socket):
    """(head, rest): the request head (through the blank line) and the bytes after it; when the
    head is unreadable, (None, what was received)."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEAD_BYTES:
            return None, data
        try:
            chunk = connection.recv(4096)
        except OSError:
            return None, data
        if not chunk:
            return None, data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HEAD_BYTES:
        return None, data
    return head, rest


def _host_of_url(target: str) -> str:
    rest = target.split("://", 1)[-1]
    authority = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if authority.startswith("["):
        return authority[1:].split("]", 1)[0]
    return authority.split(":", 1)[0]


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        for side in (source, target):
            with contextlib.suppress(OSError):
                side.shutdown(socket.SHUT_RDWR)
        return
    with contextlib.suppress(OSError):
        target.shutdown(socket.SHUT_WR)


class EgressProxy:
    def __init__(self, policy_path: str, records_dir: str, *, listen: str = "0.0.0.0", port: int = PROXY_PORT,
                 ports=ALLOWED_PORTS, max_bytes: int = DEFAULT_MAX_BYTES,
                 resolve: Optional[Callable[[str, int], list]] = None,
                 address_allowed: Optional[Callable[[str], bool]] = None,
                 connect_timeout: float = CONNECT_TIMEOUT_SECONDS, max_connections: int = MAX_CONNECTIONS):
        self.policy_path = policy_path
        self.log = RecordLog(records_dir, max_bytes)
        self.ports = frozenset(ports)
        self.resolve = resolve or _resolve
        self.address_allowed = address_allowed or public_address
        self.connect_timeout = connect_timeout
        self.slots = threading.BoundedSemaphore(max_connections)
        self._thread = None
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request, self.client_address[0])

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 64

        self.server = Server((listen, port), Handler)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.server.serve_forever(poll_interval=poll_interval)

    def start(self) -> None:
        """Serve on a thread of this process (tests and in-process use); close() stops it."""
        self._thread = threading.Thread(target=self.serve_forever, args=(0.05,), name="egress-proxy", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self.server.shutdown()
            self._thread.join(5)
            self._thread = None
        self.server.server_close()

    # -- one connection
    def _record(self, run, client, host, port, decision, reason) -> bool:
        try:
            self.log.write({"time": _now(), "run": run, "client": _printable(client, 64), "host": _printable(host),
                            "port": port, "decision": decision, "reason": reason})
            return True
        except OSError:
            return False

    @staticmethod
    def _answer(connection: socket.socket, status: str, reason: str) -> None:
        body = (reason + "\n").encode("ascii", "replace")
        with contextlib.suppress(OSError):
            connection.sendall(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                               "Connection: close\r\n\r\n".encode("ascii") + body)
            connection.shutdown(socket.SHUT_WR)
            # Read what the client still sends, so closing does not reset the answer away.
            connection.settimeout(1.0)
            for _ in range(16):
                if not connection.recv(4096):
                    break

    def _handle(self, connection: socket.socket, client: str) -> None:
        if not self.slots.acquire(blocking=False):
            self._record(None, client, "", None, "deny", "too many connections")
            self._answer(connection, "503 Service Unavailable", "too many connections")
            return
        try:
            self._serve(connection, client)
        except Exception as error:  # one connection's failure never stops the proxy
            self._record(None, client, "", None, "error", f"proxy failure: {type(error).__name__}")
        finally:
            self.slots.release()

    def _serve(self, connection: socket.socket, client: str) -> None:
        connection.settimeout(HEAD_TIMEOUT_SECONDS)
        head, rest = _read_head(connection)
        if head is None and not rest and _client_address(client) is not None and _client_address(client).is_loopback:
            return  # the proxy's own health check (`--check`): children never reach its loopback
        line = head.split(b"\r\n", 1)[0].decode("latin-1") if head is not None else ""
        parts = line.split(" ")
        if head is None or len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            self._record(None, client, "", None, "deny", "not a readable HTTP request")
            self._answer(connection, "400 Bad Request", "not a readable HTTP request")
            return
        method, target = parts[0], parts[1]
        policy = load_policy(self.policy_path)
        if method != "CONNECT":
            host = _host_of_url(target)
            run = decide(policy, client, "invalid.invalid", 0).run
            self._record(run, client, host, None, "deny", "only CONNECT (HTTPS) is allowed")
            self._answer(connection, "403 Forbidden", "only CONNECT (HTTPS) is allowed")
            return
        host, separator, port_text = target.rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        port = int(port_text) if separator and port_text.isdigit() and 0 < int(port_text) < 65536 else None
        if port is None:
            run = decide(policy, client, "invalid.invalid", 0).run
            self._record(run, client, target, None, "deny", "the target must be host:port")
            self._answer(connection, "403 Forbidden", "the target must be host:port")
            return
        decision = decide(policy, client, host, port, self.ports)
        name = normalize_host(host) or host
        if not decision.allowed:
            self._record(decision.run, client, name, port, "deny", decision.reason)
            self._answer(connection, "403 Forbidden", decision.reason)
            return
        try:
            addresses = self.resolve(name, port)
        except OSError:
            self._record(decision.run, client, name, port, "error", "the name could not be resolved")
            self._answer(connection, "502 Bad Gateway", "the name could not be resolved")
            return
        if not addresses or not all(self.address_allowed(address) for address in addresses):
            reason = "the name resolves to an address that is not public"
            self._record(decision.run, client, name, port, "deny", reason)
            self._answer(connection, "403 Forbidden", reason)
            return
        upstream = None
        for address in addresses:
            try:
                upstream = socket.create_connection((address, port), timeout=self.connect_timeout)
                break
            except OSError:
                continue
        if upstream is None:
            self._record(decision.run, client, name, port, "error", "the host could not be reached")
            self._answer(connection, "502 Bad Gateway", "the host could not be reached")
            return
        with upstream:
            if not self._record(decision.run, client, name, port, "allow", decision.reason):
                self._answer(connection, "503 Service Unavailable", "the attempt could not be recorded")
                return
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            connection.settimeout(IDLE_TIMEOUT_SECONDS)
            upstream.settimeout(IDLE_TIMEOUT_SECONDS)
            back = threading.Thread(target=_pipe, args=(upstream, connection), daemon=True)
            back.start()
            _pipe(connection, upstream)
            back.join(IDLE_TIMEOUT_SECONDS)


# ------------------------------------------------------------------------------------------------
# Command line (inside the proxy container)
# ------------------------------------------------------------------------------------------------

def main(argv=None, out: Callable[[str], object] = print) -> int:
    parser = argparse.ArgumentParser(prog="cocolo-egress-proxy")
    parser.add_argument("--policy", default=POLICY_PATH)
    parser.add_argument("--records", default=RECORDS_TARGET)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PROXY_PORT)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--set-policy")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.set_policy is not None:
        try:
            policy = validate_policy(json.loads(args.set_policy))
        except ValueError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        out(write_policy(args.policy, policy))
        return 0
    if args.check:
        try:
            socket.create_connection(("127.0.0.1", args.port), timeout=2).close()
        except OSError as error:
            print(f"not listening: {error}", file=sys.stderr)
            return 1
        return 0
    # PID 1 in the container: without a handler SIGTERM would be ignored.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    proxy = EgressProxy(args.policy, args.records, listen=args.listen, port=args.port, max_bytes=args.max_bytes)
    proxy.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
