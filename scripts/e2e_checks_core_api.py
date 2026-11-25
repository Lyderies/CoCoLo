"""API-1..6, API-24, API-25, TERM-5: the core API's process, socket, framing, handshake, contract, limits and
neutrality (T7 slice S6, part 1; part 2 is scripts/e2e_checks_core_records.py).

A REAL HEADLESS CORE PROCESS: `python -m codex_agents.core_api --records DIR --socket S`, run from the INSTALLED
project's own copy of the package (never the source tree), through the World's stand-in Python, with no terminal and
no stdin. Everything the checks learn comes over the unix socket as newline-delimited JSON-RPC (the `Wire` client
below speaks the wire itself, so it does not trust the product's own client), plus reads that do not go through
the call under test: the socket file's stat, `ps`, `lsof`, the audit files, the records on disk. The product's own
`CoreClient` and `in_process` adapter are used in one helper process, for the API-6 parity assertion and the
TERM-5 separate-client assertion only.

The producer of the records (the runner, A5/A6 of the plan) is not built yet: the records the core reads are written
by hand here, in the shapes of packages/codex-agents/tests/test_core_api_terminal.py. Every check says so in its reason
code (`records_hand_written_runner_not_built`). API-3 (a connection from another Unix user) is NOT checked: it needs a
second Unix user the harness does not have.

Test-only wiring, all of it the constructor's own documented arguments, never a product knob: a limits core
(`Core(limits=...)`) is the same installed CoreService built with small caps or a fake clock, in its own process.
The one fault is chosen ONLY by the runner's explicit `--fault NAME` argument."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_shared import write_patches  # noqa: E402
from e2e_core import _SCENES, Phase, check  # noqa: E402

A_IDS = ("API-1", "API-2", "API-4", "API-5", "API-6", "API-24", "API-25", "TERM-5")
HAND_WRITTEN = "records_hand_written_runner_not_built"

# The slice's fault: a test-only rewrite of the installed core, applied by the stand-in Python as the named module is
# imported (e2e_support.BOOT). Chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets
# FAULT in the process that runs the scenes); no environment variable selects it.
FAULT: str | None = None
FAULTS = {
    "core_api_broken": [
        # API-5: a protocol of another major version is accepted.
        {"module": "codex_agents.core_api",
         "old": "        if protocol.split(\".\")[0] != PROTOCOL_VERSION.split(\".\")[0]:\n",
         "new": "        if False:\n"},
        # API-2: the socket is created and left readable and writable by everyone.
        {"module": "codex_agents.core_api",
         "old": "        os.chmod(path, 0o600)\n        listener.listen(16)",
         "new": "        os.chmod(path, 0o666)\n        listener.listen(16)"},
        # API-4: a malformed line is no longer answered.
        {"module": "codex_agents.core_api",
         "old": "            return self._transport_refusal(session, None, ApiError(\"parse_error\", \"the line is not valid JSON\"))\n",
         "new": "            return None\n"},
        # API-6: a response no longer says which event it was current to.
        {"module": "codex_agents.core_api",
         "old": "            result[\"seq_at\"] = self._seq\n            return result\n",
         "new": "            return result\n"},
        # API-24: the connection cap is never reached.
        {"module": "codex_agents.core_api",
         "old": "                conn.over_cap = len(self._connections) >= self.max_connections\n",
         "new": "                conn.over_cap = False\n"},
        # API-25: provider detail is kept at the top of a unit, not under `extra`.
        {"module": "codex_agents.core_api",
         "old": "        unit[\"extra\"] = extra\n        return unit\n",
         "new": "        unit.update(extra)\n        unit[\"extra\"] = {}\n        return unit\n"}]}
FAULT_TARGETS = {"core_api_broken": ("API-2", "API-4", "API-5", "API-6", "API-24", "API-25")}
FAULT_ALSO_RED: dict[str, tuple] = {name: () for name in FAULT_TARGETS}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


def fault_isolated(normal: dict, fault: dict, name: str = "core_api_broken", ids: tuple = A_IDS) -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID of the run: the fault must turn every
    target ID red and change no other ID."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    allowed = set(FAULT_TARGETS[name]) | set(FAULT_ALSO_RED.get(name, ()))
    outside = [i for i in changed if i not in allowed]
    not_red = sorted(i for i in FAULT_TARGETS[name] if (after.get(i) or {}).get("passed") is not False)
    missing = sorted(set(before) ^ set(after)) + sorted(i for i in ids if i not in before or i not in after)
    return {"isolated": not outside and not not_red and not missing, "changed": changed,
            "changed_outside_targets": outside, "targets_not_red": not_red, "ids_differ": missing}


# ---------------------------------------------------------------------------------------------------------
# The wire: newline-delimited JSON-RPC 2.0 over a unix socket, spoken directly.

class Refused(Exception):
    """A JSON-RPC error reply: `.code` is the product's fixed code (data.code), `.rpc` the JSON-RPC number."""

    def __init__(self, reply: dict):
        error = reply["error"]
        self.rpc, self.reply = error.get("code"), reply
        self.data = error.get("data") if isinstance(error.get("data"), dict) else {}
        self.code = self.data.get("code")
        super().__init__(str(self.code))


class Wire:
    """One connection. `request` sends one call and returns the whole reply message; `call` returns its result or
    raises Refused. Notifications that arrive meanwhile are kept in `.notes`."""

    def __init__(self, path, *, timeout: float = 10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.buffer, self.notes, self.next_id = b"", [], 0

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def line(self, timeout: float | None = None) -> bytes | None:
        """The next whole line (without its newline), or None at end of stream or on timeout."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.sock.gettimeout() or 10.0)
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                rest, self.buffer = self.buffer, b""
                return rest or None
            self.buffer += chunk
        data, self.buffer = self.buffer.split(b"\n", 1)
        return data

    def message(self, timeout: float | None = None) -> dict | None:
        raw = self.line(timeout)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"unparsable": True}
        return parsed if isinstance(parsed, dict) else {"unparsable": True}

    def request(self, method: str, params: dict | None = None, *, rid=None, timeout: float | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id if rid is None else rid
        envelope = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            envelope["params"] = params
        self.send((json.dumps(envelope) + "\n").encode())
        while True:
            reply = self.message(timeout)
            if reply is None:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": None, "data": {"code": "no_reply"}}}
            if "method" in reply and "id" not in reply:
                self.notes.append(reply)
                continue
            return reply

    def call(self, method: str, params: dict | None = None, **options) -> dict:
        reply = self.request(method, params, **options)
        if "error" in reply:
            raise Refused(reply)
        return reply["result"]

    def refusal(self, method: str, params: dict | None = None) -> str | None:
        """The fixed code a call is refused with, or None when it was answered."""
        try:
            self.call(method, params)
        except Refused as refused:
            return refused.code
        return None

    def init(self, token: str | None = None, protocol: str = "1.0") -> dict:
        params = {"protocol": protocol}
        if token:
            params["token"] = token
        return self.call("initialize", params)


# ---------------------------------------------------------------------------------------------------------
# The core process.

# What the stand-in Python runs: the installed package's own `python -m codex_agents.core_api` entry (the shipped
# defaults), or, for a limits core, the same CoreService built with documented constructor arguments.
SHIPPED = """\
import runpy, sys
sys.path.insert(0, {orchestration!r})
sys.argv = ["codex_agents.core_api", "--records", {records!r}, "--socket", {socket!r}]
runpy.run_module("codex_agents.core_api", run_name="__main__", alter_sys=True)
"""
MINT = """\
import runpy, sys
sys.path.insert(0, {orchestration!r})
sys.argv = ["codex_agents.core_api", "--records", {records!r}, "--mint", {scope!r}, "--label", {label!r}]
runpy.run_module("codex_agents.core_api", run_name="__main__", alter_sys=True)
"""
MINT_MANY = """\
import sys
sys.path.insert(0, {orchestration!r})
from codex_agents import core_api
for scope, label in {pairs!r}:
    print(core_api.mint_token({records!r}, scope, label))
"""
LIMITED = """\
import os, signal, sys, threading, time
sys.path.insert(0, {orchestration!r})
from codex_agents import core_api
offset_file = {offset!r}
def offset():
    try:
        return float(open(offset_file).read())
    except (OSError, ValueError):
        return 0.0
service = core_api.CoreService({records!r}, {socket!r}, clock=lambda: time.time() + offset(),
                               monotonic=lambda: time.monotonic() + offset(), **{limits!r})
service.slow_for_test = {slow!r}
service.start()
done = threading.Event()
for number in (signal.SIGTERM, signal.SIGINT):
    signal.signal(number, lambda *_: done.set())
done.wait()
service.stop()
"""
# One helper process of the product's own client library: CoreClient over the socket and the in-process adapter.
PARITY = """\
import json, sys, time
sys.path.insert(0, {orchestration!r})
from codex_agents import core_api
import os
service = core_api.CoreService({records!r}, os.path.join(os.path.dirname({socket!r}), "p.sock"))
service.start()  # the in-process adapter's own service on the same records (its own socket, never used)
try:
    local = core_api.in_process(service, {token!r})
    local.call("initialize", {{"protocol": "1.0"}})
    remote = core_api.CoreClient({socket!r}, token={token!r})
    remote.call("initialize", {{"protocol": "1.0"}})
    out = {{}}
    for method, params in {calls!r}:
        key = method + json.dumps(params, sort_keys=True)
        out[key] = [local.call(method, params), remote.call(method, params)]
finally:
    service.stop()
print(json.dumps(out))
"""
SEPARATE_CLIENT = """\
import json, os, sys
sys.path.insert(0, {orchestration!r})
from codex_agents import core_api
client = core_api.CoreClient({socket!r}, token={token!r})
client.call("initialize", {{"protocol": "1.0"}})
print(json.dumps({{"pid": os.getpid(), "state": client.call("state.get")["state"]}}))
"""

UNITS = [
    {"id": "U-0001", "kind": "child", "state": "running", "title": "Build B1", "model": "sonnet", "effort": "high"},
    {"id": "U-0002", "kind": "command", "state": "done", "title": "Run tests", "exit": 0}]
STATE = {"phase": "build", "session": "S-7", "ctx": 120000, "window": "unknown", "usage": {"input": 10, "output": 5},
         "ci": {"status": "green"}, "versions": {"installed": "0.8.60"}, "claude_session_id": "abc",
         "codex_thread": "t1"}
EVENTS = [{"seq": i, "ts": 1_000_000 + i, "kind": "unit.updated", "unit_id": "U-0001", "data": {"summary": f"step {i}"},
           "v": 1} for i in range(1, 6)]


def write_records(directory: Path, *, events=EVENTS) -> None:
    """The producer's records, written by hand (the runner is not built): units, state, events, empty inbox and commands."""
    def lines(rows) -> str:
        return "".join(json.dumps(row) + "\n" for row in rows)
    (directory / "units.jsonl").write_text(lines(UNITS))
    (directory / "state.json").write_text(json.dumps(STATE))
    (directory / "events.jsonl").write_text(lines(events))
    for name in ("inbox.jsonl", "commands.jsonl"):
        (directory / name).write_text("")


class CoreWorld(e2e_support.World):
    """A World that also stops the core processes it started (the scene's teardown calls __exit__)."""

    def __exit__(self, *exc) -> None:
        for core in list(getattr(self, "cores", [])):
            core.stop()
        super().__exit__(*exc)


class Core:
    """One core process of a World: its records folder, its socket, its pid. The process is a child of this one,
    started with no terminal (its own session), no stdin and its output in a file."""

    def __init__(self, world, name: str, *, limits: dict | None = None, slow: float = 0.0, events=EVENTS,
                 seed: bool = True):
        self.world, self.name, self.limits, self.slow = world, name, limits, slow
        self.folder = world.base / name
        self.records = self.folder / "r"
        self.sockets = self.folder / "s"
        self.socket = self.sockets / "o.sock"
        self.offset = self.folder / "offset"
        self.log = self.folder / "core.log"
        self.proc: subprocess.Popen | None = None
        self.wires: list[Wire] = []
        self.folder.mkdir()
        self.records.mkdir(mode=0o700)
        self.sockets.mkdir(mode=0o755)  # the core makes it private itself (API-2)
        if seed:
            write_records(self.records, events=events)
        if not hasattr(world, "cores"):
            world.cores = []
        world.cores.append(self)

    @property
    def orchestration(self) -> str:
        return str(self.world.project / ".codex" / "orchestration")

    def code(self) -> str:
        if self.limits is None and not self.slow:
            return SHIPPED.format(orchestration=self.orchestration, records=str(self.records), socket=str(self.socket))
        return LIMITED.format(orchestration=self.orchestration, records=str(self.records), socket=str(self.socket),
                              offset=str(self.offset), limits=self.limits or {}, slow=self.slow)

    def start(self, *, wait: float = 20.0) -> "Core":
        out = open(self.log, "ab")
        try:
            self.proc = subprocess.Popen([str(self.world.shim), "-c", self.code()], env=self.world.env,
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                         start_new_session=True, cwd=str(self.folder))
        finally:
            out.close()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise e2e_support.HarnessError("the core process ended while starting")
            try:
                probe = Wire(self.socket, timeout=2)
            except OSError:
                time.sleep(0.1)
                continue
            probe.close()
            return self
        raise e2e_support.HarnessError("the core process did not start listening")

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wire(self, token: str | None = None, *, init: bool = True, timeout: float = 10.0) -> Wire:
        wire = Wire(self.socket, timeout=timeout)
        self.wires.append(wire)
        if init:
            wire.init(token)
        return wire

    def reader(self) -> Wire:
        """A connection with a read token (minted once, at the pseudo-terminal)."""
        if not hasattr(self, "_read"):
            self._read = self.mint("read", "e2e-" + self.name)
        return self.wire(self._read)

    def stop(self) -> None:
        for wire in self.wires:  # a core waits for its open connections when it stops
            wire.close()
        self.wires = []
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)

    def kill9(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(10)

    def advance(self, seconds: float) -> None:
        """A limits core's clock moves on (both its wall and its monotonic reading)."""
        try:
            now = float(self.offset.read_text())
        except (OSError, ValueError):
            now = 0.0
        self.offset.write_text(str(now + seconds))

    def mint_many(self, pairs: list) -> list[str]:
        """Tokens minted the real way: the product's own `mint_token` at a pseudo-terminal (the owner's keyboard),
        one process for all of `pairs` ((scope, label) each). The screen is kept out of the collected outputs."""
        code = MINT_MANY.format(orchestration=self.orchestration, records=str(self.records), pairs=list(pairs))
        run = self.world.terminal([str(self.world.shim), "-c", code], label="mint_token", record=False, timeout=60,
                                  cwd=self.folder)
        found = re.findall(r"\b[0-9a-f]{64}\b", run.text)
        if run.code != 0 or len(found) != len(pairs):
            raise e2e_support.HarnessError("the tokens were not minted at the pseudo-terminal")
        return found

    def mint(self, scope: str, label: str = "") -> str:
        return self.mint_many([(scope, label)])[0]

    def mint_refused_without_terminal(self, scope: str = "read") -> subprocess.CompletedProcess:
        """The same mint with no terminal at all (a pipe): refused."""
        code = MINT.format(orchestration=self.orchestration, records=str(self.records), scope=scope, label="")
        return subprocess.run([str(self.world.shim), "-c", code], capture_output=True, text=True, env=self.world.env,
                              stdin=subprocess.DEVNULL, timeout=60, cwd=str(self.folder))

    def helper(self, template: str, **values) -> str:
        """Output of one helper process (the product's own client library), run through the stand-in Python."""
        code = template.format(orchestration=self.orchestration, socket=str(self.socket), records=str(self.records),
                               **values)
        done = subprocess.run([str(self.world.shim), "-c", code], capture_output=True, text=True, env=self.world.env,
                              stdin=subprocess.DEVNULL, timeout=60, cwd=str(self.folder))
        if done.returncode != 0:
            raise e2e_support.HarnessError("a helper process of the core's own client failed")
        return done.stdout

    def audit(self, name: str = "audit.jsonl") -> list[dict]:
        path = self.records / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def processes(world, *, skip: int | None = None) -> dict[int, str]:
    """Every process of this World (those whose command line holds the World's folder), by pid; read with `ps`."""
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             env=e2e_support.child_environment(os.environ)).stdout
    found = {}
    for line in listing.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and str(world.base) in parts[1] and int(parts[0]) not in (os.getpid(), skip):
            found[int(parts[0])] = parts[1]
    return found


def lsof(*args: str) -> str:
    """`lsof` output (a missing lsof fails the check: nothing is skipped)."""
    done = subprocess.run(["lsof", "-nP", *args], capture_output=True, text=True,
                          env=e2e_support.child_environment(os.environ), stdin=subprocess.DEVNULL, timeout=30)
    return done.stdout


def core_scene():
    """One installed fake project (a real setup), then core processes of its installed package. Built once."""
    if "core_api" in _SCENES:
        return _SCENES["core_api"]
    world = CoreWorld().__enter__()
    scene = SimpleNamespace(world=world, core=None)
    _SCENES["core_api"] = scene
    write_patches(world, fault_patches())
    scene.setup = world.setup(world.answers())
    scene.core = Core(world, "main").start()
    scene.read = scene.core.mint("read", "e2e-read")
    return scene


def secure_records_note(phase: Phase) -> None:
    phase.reason(HAND_WRITTEN)


# ---------------------------------------------------------------------------------------------------------
# API-1, TERM-5

@check("API-1", group="core_api")
def api1_core_starts_headless_and_the_installed_commands_do_not_need_it(phase: Phase) -> None:
    scene = core_scene()
    world = scene.world
    secure_records_note(phase)
    phase.expect(scene.setup.code == 0, "the scripted setup that installs the project exits 0")
    core = Core(world, "headless").start()
    tty = subprocess.run(["/bin/ps", "-o", "tty=", "-p", str(core.pid)], capture_output=True, text=True).stdout.strip()
    phase.expect(tty.startswith("?"), "the core runs with no controlling terminal (ps names none)")
    phase.expect(subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(core.pid)], capture_output=True,
                                text=True).stdout.strip() != "", "the core process exists")
    phase.expect(core.reader().call("state.get")["state"]["phase"] == "build",
                 "a headless core answers over its socket")
    # The services the installed project runs by itself (the collector and the project service: stand-ins for the
    # runner, which is not built yet) are processes of this World other than the cores. The owner commands and the
    # services must not depend on the core.
    code, before, _ = world.cli("learning-status")
    others = {pid: command for pid, command in processes(world, skip=core.pid).items() if "core_api" not in command}
    phase.reason("runner_not_built_services_stand_in")
    phase.expect(code == 0 and (before or {}).get("ready") is True, "an owner command answers while the core runs")
    phase.expect(any("collector_host" in command for command in others.values()), "the collector is running")
    phase.expect(any("project_host" in command or "service_host" in command for command in others.values()),
                 "the project service is running")
    core.kill9()
    phase.expect(not core.alive(), "the core is dead")
    survivors = processes(world, skip=core.pid)
    phase.expect(all(pid in survivors for pid in others), "every service that was running is still running after the core died")
    code, after, _ = world.cli("learning-status")
    phase.expect(code == 0 and (after or {}).get("ready") is True,
                 "an owner command still answers from the files after the core died")
    phase.expect((before or {}).get("started_at_ms") == (after or {}).get("started_at_ms"),
                 "it is answered by the same collector, which never stopped")
    refused = False
    try:
        Wire(core.socket, timeout=2).close()
    except OSError:
        refused = True
    phase.expect(refused, "a dead core's socket refuses connections")
    phase.expect((core.records / "units.jsonl").read_text() == "".join(json.dumps(u) + "\n" for u in UNITS),
                 "the records on disk are untouched by the core's death")


@check("TERM-5", group="core_api")
def term5_terminal_and_core_are_separate_processes_and_the_core_is_headless(phase: Phase) -> None:
    scene = core_scene()
    core = scene.core
    secure_records_note(phase)
    phase.expect(core.pid != os.getpid(), "the core is not this process")
    command = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(core.pid)], capture_output=True,
                             text=True).stdout
    phase.expect("core_api" in command, "the core's own command line names the core module")
    phase.expect(subprocess.run(["/bin/ps", "-o", "tty=", "-p", str(core.pid)], capture_output=True,
                                text=True).stdout.strip().startswith("?"), "the core has no terminal")
    listing = lsof("-a", "-p", str(core.pid), "-U")
    phase.expect(str(core.socket) in listing, "the socket is held by the core process (lsof)")
    phase.expect(str(core.socket) not in lsof("-a", "-p", str(os.getpid()), "-U"), "this process does not hold it")
    out = json.loads(core.helper(SEPARATE_CLIENT, token=scene.read))
    phase.expect(out["pid"] not in (core.pid, os.getpid()), "a terminal-side client is a third process")
    phase.expect(out["state"]["phase"] == "build", "that client read the state through the core")
    phase.expect(core.alive(), "the core keeps running after its client process has ended")
    phase.expect(core.wire(scene.read).call("state.get")["state"]["session"] == "S-7", "and still answers")


# ---------------------------------------------------------------------------------------------------------
# API-2

@check("API-2", group="core_api")
def api2_socket_private_short_stale_replaced_no_tcp(phase: Phase) -> None:
    scene = core_scene()
    world = scene.world
    secure_records_note(phase)
    core = Core(world, "socket").start()
    info = os.lstat(core.socket)
    phase.expect(stat.S_ISSOCK(info.st_mode), "the path is a unix socket")
    phase.expect(stat.S_IMODE(info.st_mode) == 0o600, "the socket is mode 0600")
    phase.expect(stat.S_IMODE(os.stat(core.sockets).st_mode) == 0o700,
                 "its folder (made 0755 before the core started) is mode 0700")
    phase.expect(len(str(core.socket).encode()) < 104, "the path is under 104 bytes")
    inet = lsof("-a", "-p", str(core.pid), "-i")
    phase.expect(not inet.strip(), "no internet socket (TCP or UDP) is open in the core (lsof)")
    phase.expect(str(core.socket) in lsof("-a", "-p", str(core.pid), "-U"), "the same lsof does see the unix socket")
    # A stale socket (the core was killed, its file remains) is replaced by the next start.
    first = os.stat(core.socket).st_ino
    core.kill9()
    phase.expect(stat.S_ISSOCK(os.lstat(core.socket).st_mode), "a killed core leaves its socket file behind")
    refused = False
    try:
        Wire(core.socket, timeout=2).close()
    except OSError:
        refused = True
    phase.expect(refused, "nothing listens on the stale socket")
    core.start()
    again = os.lstat(core.socket)
    phase.expect(stat.S_ISSOCK(again.st_mode) and stat.S_IMODE(again.st_mode) == 0o600, "the new core's socket is private")
    phase.expect(again.st_ino != first, "the stale socket was replaced by a new one")
    phase.expect(core.reader().call("state.get")["state"]["phase"] == "build", "the new core answers")
    phase.expect(not lsof("-a", "-p", str(core.pid), "-i").strip(), "still no internet socket")
    core.stop()
    phase.expect(not os.path.exists(core.socket), "a stopped core removes its own socket")


# ---------------------------------------------------------------------------------------------------------
# API-4, API-5

@check("API-4", group="core_api")
def api4_framing_json_rpc_lines_parse_error_and_oversize(phase: Phase) -> None:
    scene = core_scene()
    core = scene.core
    secure_records_note(phase)
    before = len(core.audit())
    wire = Wire(core.socket)
    wire.send(b"{not json\n")
    reply = wire.message()
    phase.expect(reply is not None and reply.get("error", {}).get("code") == -32700, "a malformed line gets a parse error (-32700)")
    phase.expect(reply.get("id") is None and reply.get("jsonrpc") == "2.0", "the parse error is JSON-RPC 2.0 with a null id")
    phase.expect(wire.call("initialize", {"protocol": "1.0"}, rid="abc")["protocol"] == "1.0",
                 "the same connection still works after a malformed line")
    answer = wire.request("schema.get", {}, rid=7)
    answered = wire.request("initialize", {"protocol": "1.0"}, rid="x-1")
    phase.expect(answered.get("jsonrpc") == "2.0" and answered.get("id") == "x-1" and "result" in answered,
                 "a reply is JSON-RPC 2.0 and echoes a string id")
    phase.expect(answer is not None and answer.get("id") == 7, "a reply echoes an integer id")
    wire.send(b'{"id": 3, "method": "initialize"}\n')
    phase.expect((wire.message() or {}).get("error", {}).get("code") == -32600, "a request that is not JSON-RPC 2.0 gets -32600")
    wire.send(b'{"jsonrpc":"2.0","id":4,"method":"nope.nothing"}\n')
    phase.expect((wire.message() or {}).get("error", {}).get("code") == -32601, "an unknown method gets -32601")
    # A notification (no id) is answered with nothing: the next line is the reply to the next request.
    wire.send(b'{"jsonrpc":"2.0","method":"schema.get"}\n')
    phase.expect(wire.request("schema.get", {}, rid=9).get("id") == 9, "a notification gets no reply")
    # Two requests in one write are two lines, answered in order.
    wire.send(b'{"jsonrpc":"2.0","id":11,"method":"schema.get"}\n{"jsonrpc":"2.0","id":12,"method":"schema.get"}\n')
    phase.expect([(wire.message() or {}).get("id"), (wire.message() or {}).get("id")] == [11, 12],
                 "newline-delimited requests are answered one line each, in order")
    # An oversize line (the shipped cap is 1 MiB) is refused and the connection lives.
    wire.send(b"x" * ((1 << 20) + 4096) + b"\n")
    big = wire.message(30)
    phase.expect(big is not None and (big.get("error") or {}).get("data", {}).get("code") == "too_large",
                 "an oversize line is refused with the fixed code too_large")
    phase.expect(wire.request("schema.get", {}, rid=13).get("id") == 13, "the connection lives after the oversize line")
    wire.close()
    # Read through another path: the refusals are rows of the audit file, under the fixed name `envelope`.
    rows = [r for r in core.audit() if r.get("method") == "envelope"]
    codes = [r.get("outcome") for r in rows[-8:]]
    phase.expect(len(core.audit()) > before, "the refusals were written to the audit file")
    phase.expect({"parse_error", "invalid_request", "too_large"} <= set(codes),
                 "the audit file holds the parse, request and size refusals")


@check("API-5", group="core_api")
def api5_handshake_version_and_gate(phase: Phase) -> None:
    scene = core_scene()
    core = scene.core
    secure_records_note(phase)
    result = Wire(core.socket).call("initialize", {"protocol": "1.0"})
    phase.expect(result.get("protocol") == "1.0", "initialize returns the protocol version")
    phase.expect(isinstance(result.get("features"), list) and {"events", "resume"} <= set(result["features"]),
                 "initialize returns the feature list (events, resume)")
    for protocol in ("2.0", "0.9", "9.1"):
        wire = Wire(core.socket)
        try:
            wire.call("initialize", {"protocol": protocol})
            refused, supported = None, None
        except Refused as error:
            refused, supported = error.code, error.data.get("supported")
        phase.expect(refused == "version_mismatch", "a major version mismatch is refused (version_mismatch)")
        phase.expect(supported == "1.0", "the refusal says which protocol the core speaks")
        phase.expect(wire.refusal("state.get") == "not_initialized", "a refused handshake does not open the gate")
        wire.close()
    phase.expect(Wire(core.socket).call("initialize", {"protocol": "1.5"})["protocol"] == "1.0",
                 "a minor difference is accepted")
    for method in ("state.get", "schema.get", "units.list", "events.subscribe", "health.get"):
        wire = Wire(core.socket)
        phase.expect(wire.refusal(method) == "not_initialized", "any method before initialize is refused: " + method)
        wire.close()
    wire = Wire(core.socket)
    phase.expect(wire.refusal("initialize", {}) == "invalid_params", "initialize without a protocol is refused")
    wire.init()
    phase.expect(wire.refusal("state.get") == "unauthorized", "after the handshake a token is still needed for any read")
    wire.close()
    unknown = Wire(core.socket)
    phase.expect(unknown.refusal("initialize", {"protocol": "1.0", "token": "0" * 64}) == "unauthorized",
                 "an unknown token is refused at the handshake")
    # Read through another path: the audit file names each refusal by the method that was asked.
    outcomes = {(r.get("method"), r.get("outcome")) for r in core.audit()}
    phase.expect(("initialize", "version_mismatch") in outcomes and ("state.get", "not_initialized") in outcomes,
                 "the audit file holds the version and gate refusals")


# ---------------------------------------------------------------------------------------------------------
# API-6

METHODS_EXPECTED = {
    "schema.get", "state.get", "units.list", "units.get", "units.history", "runs.list", "usage.get", "usage.history",
    "ci.get", "versions.get", "history.list", "decisions.list", "queries.list", "queries.get", "health.get",
    "events.subscribe", "query.ask", "query.reply", "context.push", "unit.cancel", "control.pause", "control.resume",
    "control.handoff", "control.stop", "decision.answer", "unit.verdict", "unit.message", "units.activity",
    "changes.list", "changes.diff", "messages.list", "routing.get", "routing.set", "commands.get", "commands.list"}
EVENT_KINDS_EXPECTED = {"unit.updated", "state.changed", "query.queued", "query.answered", "decision.answered",
                        "command.requested"}
ERROR_CODES_EXPECTED = {"forbidden", "confirm_required", "confirm_invalid", "confirm_expired", "cursor_expired",
                        "not_found", "unavailable", "owner_terminal_only", "too_large", "rate_limited",
                        "too_many_connections", "too_many_subscriptions", "timeout", "version_mismatch",
                        "not_initialized", "unauthorized", "invalid_params"}
PARITY_CALLS = [["schema.get", {}], ["state.get", {}], ["units.list", {}], ["units.get", {"id": "U-0001"}],
                ["runs.list", {}], ["usage.get", {}], ["ci.get", {}], ["versions.get", {}], ["health.get", {}],
                ["units.history", {"id": "U-0001"}]]


def last_seq(records: Path) -> int:
    rows = [json.loads(line) for line in (records / "events.jsonl").read_text().splitlines() if line.strip()]
    return max((row["seq"] for row in rows), default=0)


@check("API-6", group="core_api")
def api6_schema_lists_the_contract_seq_at_everywhere_and_adapters_agree(phase: Phase) -> None:
    scene = core_scene()
    core = scene.core
    secure_records_note(phase)
    wire = core.wire(scene.read)
    schema = wire.call("schema.get")
    phase.expect(METHODS_EXPECTED <= set(schema["methods"]), "schema.get lists every method of the contract")
    phase.expect(all(isinstance(info, dict) and info.get("scope") for info in schema["methods"].values()),
                 "each method names the scope that may call it")
    phase.expect(EVENT_KINDS_EXPECTED <= set(schema["event_kinds"]), "schema.get lists the event kinds")
    phase.expect(ERROR_CODES_EXPECTED <= set(schema["error_codes"]), "schema.get lists the error codes")
    phase.expect(schema.get("protocol") == "1.0" and "token.mint" in (schema.get("walled") or []),
                 "schema.get names the protocol and the walled actions")
    on_disk = last_seq(core.records)
    for method in ("state.get", "units.list", "usage.get", "health.get", "schema.get", "runs.list", "ci.get",
                   "decisions.list", "queries.list", "history.list"):
        phase.expect(wire.call(method).get("seq_at") == on_disk, "every response carries seq_at (the events file's last seq): " + method)
    phase.expect(wire.request("units.get", {"id": "U-9999"})["error"]["data"].get("seq_at") == on_disk,
                 "an error reply carries seq_at too")
    # The product's own in-process adapter and its socket client, run beside the core in a helper process, agree.
    out = json.loads(core.helper(PARITY, token=scene.read, calls=PARITY_CALLS))
    phase.expect(len(out) == len(PARITY_CALLS), "every parity call was made")
    for key, (local, remote) in out.items():
        phase.expect(local == remote, "the in-process adapter and the socket give identical results: " + key.split("{")[0])
    # ... and the independent wire reads the same as the library's client did.
    phase.expect(json.loads(json.dumps(wire.call("runs.list"))) == out["runs.list{}"][1],
                 "this wire client reads what the library's socket client read")


# ---------------------------------------------------------------------------------------------------------
# API-24

@check("API-24", group="core_api")
def api24_request_string_method_time_connection_and_subscription_caps(phase: Phase) -> None:
    scene = core_scene()
    world, core = scene.world, scene.core
    secure_records_note(phase)
    wire = core.wire(scene.read)
    # request size: the shipped line cap (1 MiB), and the free-text cap (2 KiB) with its own code.
    wire.send(b'{"jsonrpc":"2.0","id":1,"method":"schema.get","params":{"x":"' + b"y" * (1 << 20) + b'"}}\n')
    phase.expect(((wire.message(30) or {}).get("error") or {}).get("data", {}).get("code") == "too_large",
                 "a request line over the cap is refused (too_large)")
    phase.expect(wire.refusal("query.ask", {"text": "x" * 2049}) == "too_large", "free text over 2 KiB is refused (too_large)")
    # string length: an id over 128 characters.
    phase.expect(wire.refusal("units.get", {"id": "U-" + "9" * 200}) == "invalid_params",
                 "a string over its length cap is refused (invalid_params)")
    phase.expect(wire.refusal("units.get", {"id": "U-0001\nx"}) == "invalid_params",
                 "a string with a control character is refused")
    wire.close()
    # connection count: the shipped cap is 64.
    time.sleep(0.5)
    held = []
    try:
        for _ in range(64):
            held.append(Wire(core.socket))
            held[-1].init()
        over = Wire(core.socket)
        refusal = over.message(10)
        phase.expect(refusal is not None and (refusal.get("error") or {}).get("data", {}).get("code") == "too_many_connections",
                     "the 65th connection is refused (too_many_connections)")
        over.close()
    finally:
        for each in held:
            each.close()
    deadline = time.monotonic() + 10
    healthy = False
    while time.monotonic() < deadline and not healthy:
        try:
            probe = core.wire(scene.read)
            healthy = probe.call("state.get")["state"]["phase"] == "build"
            probe.close()
        except (Refused, OSError):
            time.sleep(0.3)
    phase.expect(healthy, "a slot is free again once the connections are closed")
    # subscription count: 8 per token, 16 in all.
    tokens = [scene.read, *core.mint_many([("read", "e2e-sub-b"), ("read", "e2e-sub-c")])]
    streams = []

    def subscribe(token: str) -> str | None:
        stream = core.wire(token)
        try:
            stream.call("events.subscribe", {})
        except Refused as refused:
            stream.close()
            return refused.code
        streams.append(stream)
        return None

    try:
        phase.expect([subscribe(tokens[0]) for _ in range(8)] == [None] * 8, "one token may hold 8 subscriptions")
        phase.expect(subscribe(tokens[0]) == "too_many_subscriptions", "a 9th subscription of one token is refused")
        phase.expect([subscribe(tokens[1]) for _ in range(8)] == [None] * 8, "another token holds 8 more (16 in all)")
        phase.expect(subscribe(tokens[2]) == "too_many_subscriptions", "the 17th subscription is refused (too_many_subscriptions)")
    finally:
        for each in streams:
            each.close()
    # method time: the same installed core, built with a 50 ms method limit and a slow-method hook, in its own process.
    slow = Core(world, "slow", limits={"method_seconds": 0.05}, slow=0.3).start()
    slow_token = slow.mint("read", "e2e-slow")
    slow_wire = slow.wire(slow_token)
    phase.expect(slow_wire.refusal("state.get") == "timeout", "a method over its time limit is refused (timeout)")
    phase.expect(any(r.get("outcome") == "timeout" for r in slow.audit()), "the timeout is a row of the audit file")
    slow.stop()


# ---------------------------------------------------------------------------------------------------------
# API-25

PROVIDER = re.compile(r"claude|codex|openai|anthropic|sonnet|opus|haiku|gpt|gemini|llama|mistral|^model$|_model$", re.I)


def provider_keys(value, path: str = "") -> list[str]:
    """Key paths holding a provider name, not looking inside an `extra` object (where provider detail belongs)."""
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "extra":
                continue
            if PROVIDER.search(str(key)):
                found.append(path + "/" + str(key))
            found += provider_keys(inner, path + "/" + str(key))
    elif isinstance(value, list):
        for inner in value:
            found += provider_keys(inner, path)
    return found


@check("API-25", group="core_api")
def api25_no_provider_names_in_contract_provider_detail_only_under_extra(phase: Phase) -> None:
    scene = core_scene()
    core = scene.core
    secure_records_note(phase)
    wire = core.wire(scene.read)
    schema = wire.call("schema.get")
    names = json.dumps({k: schema[k] for k in ("methods", "event_kinds", "notifications", "error_codes", "walled")}).lower()
    for provider in ("claude", "codex", "openai", "anthropic", "sonnet", "opus", "haiku", "gpt", "gemini"):
        phase.expect(provider not in names, "no provider name in a method, event or error name")
    # The records carry provider detail (a session id, a model); every read keeps it under `extra`.
    reads = {"state.get": wire.call("state.get"), "units.list": wire.call("units.list"),
             "units.get": wire.call("units.get", {"id": "U-0001"}), "runs.list": wire.call("runs.list"),
             "units.history": wire.call("units.history", {"id": "U-0001"}), "usage.get": wire.call("usage.get")}
    for name, result in reads.items():
        phase.expect(provider_keys(result) == [], "no provider name among the field names of " + name)
    state, unit = reads["state.get"]["state"], reads["units.get"]["unit"]
    phase.expect(state["extra"] == {"claude_session_id": "abc", "codex_thread": "t1"},
                 "provider detail of the state is under extra")
    phase.expect("model" not in unit and unit["extra"].get("model") == "sonnet",
                 "a unit's model is under extra, not a top-level field")
    events = Wire(core.socket)
    events.init(scene.read)
    events.call("events.subscribe", {"from_seq": 0})
    seen = []
    deadline = time.monotonic() + 5
    while len(seen) < 5 and time.monotonic() < deadline:
        message = events.message(2)
        if message and message.get("method") == "events.event":
            seen.append(message["params"])
    events.close()
    phase.expect(len(seen) == 5 and all(provider_keys(event) == [] for event in seen),
                 "the events carry no provider name in their field names")
    phase.expect(all(e["kind"] in schema["event_kinds"] for e in seen), "event kinds are those the schema lists")
