"""Building blocks shared by the T7 run-control (S1, e2e_checks_run_control.py) and recovery (S2,
e2e_checks_recovery.py) scenes: the second-project views, the primary-session driver (Ctl) and the read-only
reads that do not go through the command that made a run (project history, account ledger capacity table, the
account service's own per-run capacity read). This module declares no check; each slice keeps its own checks,
faults and judges. Merged from the two slices' copies (batch 0.8.58); each slice's own parameters (its dispatch
and finish timeouts, its setup label) stay its own."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_run import WORK_EVIDENCE, WORK_GRANT, WORK_PATH, trusted_interpreter_checks  # noqa: E402
from e2e_core import CLAUDE_MODEL, PROBE, ROUTE_TASK  # noqa: E402

INIT = {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none", "model": CLAUDE_MODEL,
        "plugins": [], "claude_code_version": "2.1.311", "mcp_servers": [], "tools": ["StructuredOutput"]}


def write_patches(world, patches: list) -> None:
    """Test-only source rewrites the stand-in Python applies as the named module is imported (e2e_support.BOOT)."""
    (world.home / ".e2e-bin" / "patches.json").write_text(json.dumps(patches))


def project_view(world, name: str | None = None):
    """A view of `world` for one project (the first, or another made on it): the commands, files and records of
    the world are shared; only the project differs."""
    view = copy.copy(world)
    if name is not None:
        view.project = world.base / name
    return view


def add_project(world, name: str):
    """A second or third fake project in the same HOME (one collector), committed and ready for setup."""
    view = project_view(world, name)
    view.project.mkdir()
    view.project.chmod(0o755)
    view.git("init", "-q")
    (view.project / "fixture").mkdir()
    (view.project / "fixture" / "facts.txt").write_text("alpha 3\nbeta 5\n")
    (view.project / "fixture" / "oracle.json").write_text(json.dumps({"alpha": 3, "beta": 5}, sort_keys=True))
    view.git("add", ".")
    view.git("commit", "-qm", "fixture")
    trusted_interpreter_checks(view)
    return view


def set_up(view, kind: str) -> None:
    """Scripted setup of the view's project, then the provider observations the Claude admission rests on."""
    done = view.setup(view.answers(providers={"codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL,
                                              "bridge": "off"}))
    if done.code != 0:
        raise e2e_support.HarnessError(f"the scripted setup of a {kind} project did not finish")
    view.claude_frames(e2e_support.claude_probe_frames())
    view.cli("provider-observe", "--provider", "codex")
    view.cli("provider-observe", "--provider", "claude")
    view.host_cli("claude", *PROBE)


def child_frames(world, *frames) -> None:
    world.claude_frames([INIT, *frames])


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def step(view, label: str, argv: list, *, timeout: float, environment: dict | None = None,
         cwd: Path | None = None, refuses: bool | str = False) -> tuple[int, str]:
    """One helper command through the stand-in Python, judged like any step (G1, G8), a timeout included."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              env={**view.env, **(environment or {})}, cwd=str(cwd or view.project))
    except subprocess.TimeoutExpired as expired:
        text = _text(expired.stdout) + _text(expired.stderr)
        view.outputs.append(text)
        view.steps.append({"step": label, "code": -1, "refuses": False, "timed_out": True})
        view._capture(text, kind="output", code=-1, refuses=False, timed_out=True)
        return -1, text
    text = done.stdout + done.stderr
    view.outputs.append(text)
    view.steps.append({"step": label, "code": done.returncode, "refuses": bool(refuses), "timed_out": False})
    view._capture(text, kind="output", code=done.returncode, refuses=refuses, timed_out=False)
    return done.returncode, done.stdout


class Ctl:
    """One project's orchestration and the runs made on it, as a primary session drives them.

    A slice sets its own DISPATCH_TIMEOUT (None: the harness default for a host command) and FINISH_TIMEOUT."""

    DISPATCH_TIMEOUT: float | None = None
    FINISH_TIMEOUT: float = 90

    def __init__(self, view, tag: str):
        self.view, self.tag, self.count, self.runs = view, tag, 0, []
        self.lock = threading.Lock()
        self.host = lambda *args, **kw: view.host_cli("claude", *args, **kw)
        self.orchestration = self.host("learning-open-orchestration")[1] or {}
        self.handle = self.orchestration.get("orchestration_handle", "")
        self.project_id = (self.orchestration.get("scope") or {}).get("project_id")
        self.runtime_digest = (self.orchestration.get("scope") or {}).get("runtime_digest")
        self.grant = self.save("parent", WORK_GRANT)

    def save(self, name: str, data) -> str:
        path = self.view.base / f"{self.tag}-{name}.json"
        path.write_text(json.dumps(data))
        return str(path)

    def child(self, *, bounds: dict | None = None, path: str = WORK_PATH, open_run: bool = True,
              probe: bool = True) -> SimpleNamespace:
        """Route a task, build its assignment and (by default) open its run: everything before the dispatch."""
        with self.lock:
            self.count += 1
            number = self.count
        name = f"c{number}"
        task = {**ROUTE_TASK, "id": f"{self.tag}-task-{number}", "owned_paths": [path]}
        task_file = self.save(name + "-task", task)
        route = self.host("learning-route", "--task", task_file)[1] or {}
        route_file = self.save(name + "-route", route)
        permissions = {**WORK_GRANT, "filesystem": [f"{path}:read"]}
        assignment = {
            "assignment_id": task["id"] + "-a1", "task_id": task["id"], "objective": "Read the fixture",
            "role": route.get("role"), "model": route.get("model"), "effort": route.get("effort"), "attempt": 1,
            "meaningful": True, "owned_paths": [path], "context": [path], "permissions": permissions,
            "expected_output": "Each value, as name=value, in its evidence entry", "required_evidence": WORK_EVIDENCE,
            "verification_required": route.get("verification_required"),
            "bounds": {**(route.get("bounds") or {}), **(bounds or {})},
            "stopping_conditions": ["every value found"], "prohibited_actions": ["network"]}
        assignment_file = self.save(name + "-assignment", assignment)
        grant = self.save(name + "-grant", permissions)
        common = ["--task", task_file, "--assignment", assignment_file, "--parent-grant", grant]
        run = SimpleNamespace(project_id=self.project_id, common=common, route_file=route_file, route=route,
                              opened=None, open_code=None, reply=None, code=None, recorded=None, started=None,
                              seconds=None, thread=None, name=name, text="")
        if open_run:
            self.open(run, probe=probe)
        return run

    def open(self, run, *, probe: bool = True, refuses: bool = False) -> None:
        if probe:
            self.host("provider-observe", "--provider", "claude", "--billing-probe")
        run.open_code, run.opened, _ = self.host("learning-open-host-run", *run.common, "--route", run.route_file,
                                                 "--orchestration-handle", self.handle, refuses=refuses)
        self.runs.append(run)

    def dispatch(self, run, *, refuses: bool = False) -> None:
        """A dispatch. Only a dispatch EXPECTED to refuse (or to be cut off by a stimulus) passes the refuses flag (its
        reason is asserted by the check); every other dispatch is judged by G1 (exit 0) at capture, and its check
        asserts `run.code` too."""
        started = time.monotonic()
        limit = {} if self.DISPATCH_TIMEOUT is None else {"timeout": self.DISPATCH_TIMEOUT}
        run.code, run.reply, run.text = self.host("dispatch", *run.common, "--route", run.route_file,
                                                  "--run-handle", (run.opened or {}).get("run_handle", ""),
                                                  refuses=refuses, **limit)
        run.seconds = time.monotonic() - started

    def record(self, run) -> None:
        """Record the run's result as the primary does: it is what frees the project's preparation slot."""
        result = (run.reply or {}).get("result")
        if result is None:
            return
        terminal = ((run.reply or {}).get("bridge_report") or {}).get("terminal_state")
        observation = {"run_handle": (run.opened or {}).get("run_handle"),
                       "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]}
        if terminal:
            observation["terminal_state"] = terminal
        run.recorded = self.host("learning-record-host-result", *run.common,
                                 "--result", self.save(run.name + "-result", result),
                                 "--observation", self.save(run.name + "-observation", observation))

    def dispatch_in_background(self, run, *, refuses: bool = False) -> None:
        run.thread = threading.Thread(target=self.dispatch, args=(run,), kwargs={"refuses": refuses}, daemon=True)
        run.thread.start()

    def finish(self, run, timeout: float | None = None) -> None:
        run.thread.join(self.FINISH_TIMEOUT if timeout is None else timeout)

    def status(self, run, *, refuses: bool | str = False) -> dict:
        return self.host("learning-run-status", "--orchestration-handle", self.handle,
                         "--run-handle", (run.opened or {}).get("run_handle", ""), refuses=refuses)[1] or {}

    def wait_running(self, run, timeout: float = 40) -> dict:
        """The project service's own live status, until the child has started (its init frame was seen)."""
        deadline = time.monotonic() + timeout
        status = {}
        while time.monotonic() < deadline:
            status = self.status(run, refuses=True)  # until the child has started the service may refuse
            if (status.get("liveness") or {}).get("last_activity_category") and not status.get("stop_requested"):
                return status
            time.sleep(0.4)
        return status

    def cancel(self, run) -> dict:
        return self.host("learning-cancel-run", "--orchestration-handle", self.handle,
                         "--run-handle", (run.opened or {}).get("run_handle", ""))[1] or {}

    def close(self) -> dict:
        self.host("learning-close-orchestration", "--orchestration-handle", self.handle, "--status", "completed")
        return self.host("learning-orchestration-report", "--orchestration-id",
                         self.orchestration.get("orchestration_id", ""))[1] or {}


# Reads that do not go through the command that made the run ------------------------------------

def bridge_run(run) -> str | None:
    """The run's bridge id: from the ledger row noted before a stimulus cut the dispatch short, else the reply's."""
    return getattr(run, "bridge", None) or ((run.reply or {}).get("bridge_report") or {}).get("bridge_run_id")


def state_dir(world) -> Path:
    return world.home / ".local" / "state" / "codex-agents"


def project_history(world, project_id: str | None) -> Path:
    return state_dir(world) / "projects" / str(project_id) / "history.sqlite3"


def project_records(world, project_id: str | None, record_type: str) -> list[dict]:
    """One project's own history, read-only; a missing history is an error (never an empty answer)."""
    path = project_history(world, project_id)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [json.loads(row[0]) for row in connection.execute(
            "select content from records where record_type = ? order by source_sequence", (record_type,))]
    finally:
        connection.close()


def ledger_slots(world) -> list[dict]:
    """The account ledger's capacity table, read-only: one row per claimed run (never content)."""
    path = state_dir(world) / "learning.sqlite3"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "select project_id, bridge_run_id, run_id, released_at_ms from active_run_capacity")]
    finally:
        connection.close()


def slot_of(world, run) -> dict | None:
    rows = [row for row in ledger_slots(world) if row["bridge_run_id"] == bridge_run(run)]
    return rows[0] if len(rows) == 1 else None


ACCOUNT_READ = """\
import json, sys
sys.path.insert(0, {orchestration!r})
from pathlib import Path
from codex_agents.account_api import AccountClient
from codex_agents.client_runtime import _account_target, enroll_clients
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
state, project = CollectorPaths(Path({state!r})), Path({project!r})
project_id, endpoint = _account_target(state, project)
enroll_clients(state, project, None, account=endpoint)
print(json.dumps(AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=20)
                 .capacity_status({handle!r}, {bridge!r})))
"""


def capacity_read(view, run, *, timeout: float = 60) -> dict:
    """The supported per-run capacity read: the account service's `account.capacity_status` for this run, asked over
    its own socket by the installed client library (state held, released or unknown; never the dispatch reply).

    A read that times out is judged like any step (its partial output through capture, G1 and G8) and answers the
    fixed failure `capacity_read_timed_out`; the command, HOME and run handle never reach a traceback."""
    bridge = bridge_run(run)
    if not bridge or not (run.opened or {}).get("run_handle"):
        return {}
    code = ACCOUNT_READ.format(orchestration=str(view.project / ".codex" / "orchestration"),
                               state=str(state_dir(view)), project=str(view.project),
                               handle=run.opened["run_handle"], bridge=bridge)
    status, out = step(view, "capacity-read", [str(view.shim), "-I", "-B", "-S", "-c", code], timeout=timeout,
                       environment=view.CLAUDE_HOST)
    if status == -1:
        return {"failure": "capacity_read_timed_out"}
    try:
        reply = json.loads(out)
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}
