"""L8, L14 and L15: recovery, status and deletion (T7 slice S2): a killed project service, a killed or replaced
collector, status values at each phase, and project deletion.

Every value is read through a different path than the stimulus: the project history and the account ledger
(read-only sqlite), the account service's own per-run capacity read (`account.capacity_status`, asked over its socket
by the installed client library, polled with a bound), the live process table, and the installed CLI's status
commands. Nothing passes on missing or null data. The children are the fake clients; the installed CLI is the only CLI."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import signal
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
from e2e_core import CLAUDE_MODEL, PROBE, ROUTE_TASK, _SCENES, CheckFailed, Phase, check  # noqa: E402

INIT = {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none", "model": CLAUDE_MODEL,
        "plugins": [], "claude_code_version": "2.1.311", "mcp_servers": [], "tools": ["StructuredOutput"]}
S2_IDS = ("L8.1", "L8.2", "L8.3", "L14.1", "L15.1")
# The fault of this slice, chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in
# the process that runs the scenes). No environment variable selects it, and every World environment drops E2E_*.
FAULT: str | None = None
FAULTS: dict[str, list] = {}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


def write_patches(world, patches: list) -> None:
    """Test-only source rewrites the stand-in Python applies as the named module is imported (e2e_support.BOOT)."""
    (world.home / ".e2e-bin" / "patches.json").write_text(json.dumps(patches))


def project_view(world, name: str | None = None):
    """A view of `world` for one project (the first, or another made on it): commands, files and records of the
    world are shared; only the project differs."""
    view = copy.copy(world)
    if name is not None:
        view.project = world.base / name
    return view


def add_project(world, name: str):
    """A second fake project in the same HOME (one collector), committed and ready for setup."""
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


def set_up(view) -> None:
    """Scripted setup of the view's project, then the provider observations the Claude admission rests on."""
    done = view.setup(view.answers(providers={"codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL,
                                              "bridge": "off"}))
    if done.code != 0:
        raise e2e_support.HarnessError("the scripted setup of a recovery project did not finish")
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
         cwd: Path | None = None, refuses: bool = False) -> tuple[int, str]:
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
    view.steps.append({"step": label, "code": done.returncode, "refuses": refuses, "timed_out": False})
    view._capture(text, kind="output", code=done.returncode, refuses=refuses, timed_out=False)
    return done.returncode, done.stdout


class Ctl:
    """One project's orchestration and the runs made on it, as a primary session drives them."""

    def __init__(self, view, tag: str):
        self.view, self.tag, self.count, self.runs = view, tag, 0, []
        self.lock = threading.Lock()
        self.host = lambda *args, **kw: view.host_cli("claude", *args, **kw)
        self.orchestration = self.host("learning-open-orchestration")[1] or {}
        self.handle = self.orchestration.get("orchestration_handle", "")
        self.project_id = (self.orchestration.get("scope") or {}).get("project_id")
        self.grant = self.save("parent", WORK_GRANT)

    def save(self, name: str, data) -> str:
        path = self.view.base / f"{self.tag}-{name}.json"
        path.write_text(json.dumps(data))
        return str(path)

    def child(self, *, open_run: bool = True) -> SimpleNamespace:
        """Route a task, build its assignment and (by default) open its run: everything before the dispatch."""
        with self.lock:
            self.count += 1
            number = self.count
        name = f"c{number}"
        task = {**ROUTE_TASK, "id": f"{self.tag}-task-{number}", "owned_paths": [WORK_PATH]}
        task_file = self.save(name + "-task", task)
        route = self.host("learning-route", "--task", task_file)[1] or {}
        route_file = self.save(name + "-route", route)
        assignment = {
            "assignment_id": task["id"] + "-a1", "task_id": task["id"], "objective": "Read the fixture",
            "role": route.get("role"), "model": route.get("model"), "effort": route.get("effort"), "attempt": 1,
            "meaningful": True, "owned_paths": [WORK_PATH], "context": [WORK_PATH], "permissions": WORK_GRANT,
            "expected_output": "Each value, as name=value, in its evidence entry", "required_evidence": WORK_EVIDENCE,
            "verification_required": route.get("verification_required"), "bounds": route.get("bounds"),
            "stopping_conditions": ["every value found"], "prohibited_actions": ["network"]}
        assignment_file = self.save(name + "-assignment", assignment)
        common = ["--task", task_file, "--assignment", assignment_file, "--parent-grant", self.grant]
        run = SimpleNamespace(common=common, route_file=route_file, route=route, opened=None, open_code=None,
                              reply=None, code=None, seconds=None, thread=None, name=name, text="")
        if open_run:
            self.open(run)
        return run

    def open(self, run) -> None:
        self.host("provider-observe", "--provider", "claude", "--billing-probe")
        run.open_code, run.opened, _ = self.host("learning-open-host-run", *run.common, "--route", run.route_file,
                                                 "--orchestration-handle", self.handle)
        self.runs.append(run)

    def dispatch(self, run, *, refuses: bool = False) -> None:
        """A dispatch. Only one EXPECTED to refuse (or to be cut off by the stimulus) passes `refuses`; its reason is
        asserted by the check."""
        started = time.monotonic()
        run.code, run.reply, run.text = self.host("dispatch", *run.common, "--route", run.route_file,
                                                  "--run-handle", (run.opened or {}).get("run_handle", ""),
                                                  refuses=refuses, timeout=120)
        run.seconds = time.monotonic() - started

    def dispatch_in_background(self, run, *, refuses: bool = False) -> None:
        run.thread = threading.Thread(target=self.dispatch, args=(run,), kwargs={"refuses": refuses}, daemon=True)
        run.thread.start()

    def finish(self, run, timeout: float = 120) -> None:
        run.thread.join(timeout)

    def status(self, run, *, refuses: bool = False) -> dict:
        return self.host("learning-run-status", "--orchestration-handle", self.handle,
                         "--run-handle", (run.opened or {}).get("run_handle", ""), refuses=refuses)[1] or {}

    def wait_running(self, run, timeout: float = 40) -> dict:
        """The project service's own live status, until the child has started (its init frame was seen)."""
        deadline = time.monotonic() + timeout
        status = {}
        while time.monotonic() < deadline:
            status = self.status(run, refuses=True)
            if (status.get("liveness") or {}).get("last_activity_category") and not status.get("stop_requested"):
                return status
            time.sleep(0.4)
        return status

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


def count_by_type(world, project_id: str | None) -> dict:
    """The project's history counts by record type (kinds and numbers, never content)."""
    path = project_history(world, project_id)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(connection.execute("select record_type, count(*) from records group by record_type"))
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


# Processes -------------------------------------------------------------------------------------

def processes(world, script: str, project: Path | None = None) -> list[int]:
    """The pids of the running `script` (collector_host.py, project_host.py) started inside this World; with
    `project`, only the services started from that project's own folder (the command names the project's runtime)."""
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             env=e2e_support.child_environment(os.environ)).stdout
    found = []
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        flag = f"--project-root {project}" if project is not None else ""
        if (script in command and str(world.base) in command and int(pid) != os.getpid()
                and (project is None or command.endswith(flag) or flag + " " in command)):
            found.append(int(pid))
    return sorted(found)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_hard(pid: int) -> bool:
    """SIGKILL, then wait (bounded) until the process is gone: no cleanup of any kind runs in it."""
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


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
    """The account service's own per-run capacity read for this run (never the dispatch reply)."""
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


def wait_released(view, run, *, seconds: float = 40) -> dict:
    """Poll (bounded) the account's own per-run read until it says `released`; the last read either way. The
    release follows a child's end by a moment, so one immediate read would be a race, not a verdict."""
    deadline = time.monotonic() + seconds
    read = {}
    while True:
        read = capacity_read(view, run)
        if read.get("state") == "released" or time.monotonic() >= deadline:
            return read
        time.sleep(1.0)


# ---------------------------------------------------------------------------------------------
# Pure judges (guarded without a World by tests/test_e2e_recovery.py)

def persisted_terminal(binding: tuple | None, outcome: str | None = None) -> str | None:
    """What the persisted records say a run ended as: the ledger's (binding state, abandoned reason, released) and,
    for `completed`, the run's persisted terminal OUTCOME read explicitly (`outcome == "completed"`, never inferred
    from a released slot or from the absence of run_lost). Exactly `run_lost` (abandoned for a lost service, slot
    released) or `completed` (terminal, slot released, outcome completed); anything else is None (limbo)."""
    if not binding or len(binding) != 3 or binding[2] != 1:
        return None
    state, reason, _released = binding
    if state == "abandoned" and reason == "service_lost":
        return "run_lost"
    if state == "terminal" and reason is None and outcome == "completed":
        return "completed"
    return None


def status_agrees(terminal: str | None, status: dict | None) -> bool:
    """The run's own status says the same as the persisted terminal, explicitly: run_lost reads reason_code
    run_lost; completed reads ready and terminal_observed both true and no run_lost. A missing, empty or
    still-running status agrees with nothing."""
    status = status or {}
    if terminal == "run_lost":
        return status.get("reason_code") == "run_lost"
    if terminal == "completed":
        return (status.get("ready") is True and status.get("terminal_observed") is True
                and status.get("reason_code") != "run_lost")
    return False


def dispatch_g1_breaks(service_replaced_: bool, code: int) -> list[str]:
    """G1 for a dispatch whose refusal is expected only when the project's service was observed replaced: the
    capture marks it `refuses` at the time (the service is not known yet), this judges it again once the service
    is known. An untouched service's dispatch stays expected-success."""
    return e2e_support.capture_breaks("", kind="output", private={}, code=code, refuses=bool(service_replaced_),
                                      timed_out=False)


MIGRATION_PHASES = ("orchestration", "opened", "running", "lost", "next", "closed")


def migration_phases_judged(collected: dict, judged) -> bool:
    """Every collected phase that has a migration status is judged, exactly (dropping one fails). The idle phase
    collects the lifecycle status only (no migration status)."""
    return set(judged) == {name for name, view in collected.items() if "migration" in view} and len(tuple(judged)) == len(set(judged))


def persisted_outcome(world, run, project_id: str | None) -> str | None:
    """The run's persisted terminal outcome, read from the project's own history: the `execution_state` of the
    outcome record written for this run (matched on the run id or the bridge id); None when there is none or
    its state is not a string."""
    ids = {(run.opened or {}).get("run_id"), getattr(run, "bridge", None)} - {None}
    try:
        records = project_records(world, project_id, "outcome") if project_id else []
    except (sqlite3.Error, OSError):
        return None
    states = {record.get("execution_state") for record in records if record.get("run_id") in ids}
    return states.pop() if len(states) == 1 and isinstance(next(iter(states)), str) else None


def service_replaced(before: list, after: list, *, alive_fn=None) -> bool:
    """A project's service was replaced: one service before, a different single one now, the old one gone."""
    alive_fn = alive_fn or alive
    return len(before) == 1 and len(after) == 1 and after[0] != before[0] and not alive_fn(before[0])


def is_running(reply: dict | None) -> bool:
    """A run status that shows the child started (an activity was seen) and has not ended; missing is not running."""
    reply = reply or {}
    return bool((reply.get("liveness") or {}).get("last_activity_category")) and reply.get("terminal_observed") is False


def closed_ok(result: tuple) -> bool:
    """A close that worked: exit 0 and an explicit ready true (a missing or null ready is not a close)."""
    code, reply, _text = result
    return code == 0 and isinstance(reply, dict) and reply.get("ready") is True


def expect_each(phase: Phase, pairs: list) -> None:
    """Judge EVERY (condition, fixed text) pair, then fail naming each that did not hold: none is skipped because an
    earlier one failed."""
    failed = []
    for condition, what in pairs:
        phase.expectations += 1
        if not condition:
            failed.append(what)
    if failed:
        raise CheckFailed("; ".join(failed))


def is_tombstone(row: dict | None, project_id: str | None) -> bool:
    """A deleted registration scrubbed to a tombstone: deleted, with no root fields and a deletion time."""
    row = row or {}
    deleted_at = row.get("deleted_at_ms")
    return (project_id is not None and row.get("project_id") == project_id and row.get("status") == "deleted"
            and row.get("canonical_root") is None and row.get("root_digest") is None
            and isinstance(deleted_at, int) and not isinstance(deleted_at, bool))


def observation_problems(rows: list[dict], paths: list[str]) -> list[str]:
    """Why retained provider observations may not stay: any project path in any column, or any run identifier.
    (Basis for keeping them: account-level provider health readings tied to no run and no path.)"""
    problems = []
    for row in rows:
        if row.get("run_id") is not None or row.get("handle_digest") is not None:
            problems.append("run_identifier")
        if any(path and isinstance(value, str) and path in value for value in row.values() for path in paths):
            problems.append("project_path")
    return problems


def identity_digest(pairs: list) -> str:
    """A digest of record identities (id, content digest), order free: a substitution at the same count changes it."""
    return hashlib.sha256(json.dumps(sorted([list(pair) for pair in pairs]), separators=(",", ":")).encode()).hexdigest()


def run_binding(world, run) -> tuple | None:
    """(binding state, abandoned reason, released) of this run alone, from the ledger; None unless exactly one row.
    A run the project counts terminal from completion evidence (a consumed binding, or a released completion row:
    the same rule `learning-status` counts host_runs_terminal by) reads `terminal` here; its binding row alone
    stays `pending` after a normal completion."""
    wanted = (run.opened or {}).get("run_id")
    path = state_dir(world) / "learning.sqlite3"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [tuple(row) for row in connection.execute(
            "SELECT h.binding_state,h.abandoned_reason,a.released_at_ms IS NOT NULL,"
            "(h.consumed_at_ms IS NOT NULL OR EXISTS(SELECT 1 FROM capacity_completions cc WHERE "
            "cc.project_id=h.project_id AND cc.handle_digest=h.handle_digest AND cc.released_at_ms IS NOT NULL)) "
            "FROM active_run_capacity a JOIN host_run_bindings h ON h.project_id=a.project_id "
            "AND h.handle_digest=a.handle_digest WHERE a.run_id=?", (wanted,))] if wanted else []
    finally:
        connection.close()
    if len(rows) != 1:
        return None
    state, reason, released, completed = rows[0]
    return ("terminal" if state == "pending" and completed else state, reason, released)


def settled_binding(world, run, seconds: float = 20, outcome: str | None = None):
    """The run's persisted binding, polled (bounded) until it reads as a terminal with the given persisted outcome;
    the last reading either way."""
    poll(lambda: persisted_terminal(run_binding(world, run), outcome), seconds, every=1.0)
    return run_binding(world, run)


def _ledger(world):
    connection = sqlite3.connect(f"file:{state_dir(world) / 'learning.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def registration_row(world, project_id: str | None) -> dict | None:
    connection = _ledger(world)
    try:
        row = connection.execute("select * from projects where project_id = ?", (project_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def observation_rows(world, project_id: str | None) -> list[dict]:
    """The retained provider observations of one project (both observation tables), as rows."""
    connection = _ledger(world)
    try:
        rows = []
        for table in ("account_observation_requests", "account_observation_origins"):
            rows += [dict(row) for row in connection.execute(f'select * from "{table}" where project_id = ?',
                                                             (project_id,))]
        return rows
    finally:
        connection.close()


def survivor_identity(world, project_id: str | None) -> dict:
    """Record identities of one project, never only counts: the ledger's records and the project's own history
    (record id and content digest of each), as digests."""
    connection = _ledger(world)
    try:
        ledger = [tuple(row) for row in connection.execute(
            "select record_id, content_digest from records where project_id = ?", (project_id,))]
    finally:
        connection.close()
    history_connection = sqlite3.connect(f"file:{project_history(world, project_id)}?mode=ro", uri=True)
    try:
        history = [tuple(row) for row in history_connection.execute("select record_id, content_digest from records")]
    finally:
        history_connection.close()
    return {"ledger": identity_digest(ledger), "history": identity_digest(history),
            "sizes": [len(ledger), len(history)]}


# ---------------------------------------------------------------------------------------------
# More reads, and the answers the children give

RUN_TABLES = ("active_run_capacity", "host_run_bindings", "capacity_completions", "capacity_executions",
              "launch_claims", "launch_receipts", "launch_observations", "launch_inputs", "run_provenance",
              "account_open_receipts", "account_cleanup_receipts")


def run_counts(world, project_id: str | None) -> dict:
    """What a run leaves, counted by kind (never content): the account ledger's run tables and the project's
    own history by record type. Equal counts later mean nothing was recorded twice."""
    tables = world.ledger_counts()
    return {"ledger": {name: tables.get(name) for name in RUN_TABLES}, "history": count_by_type(world, project_id)}


def claimed_bindings(world) -> list[tuple]:
    """(binding state, abandoned reason, released) of every claimed run, oldest first, read-only from the ledger."""
    path = state_dir(world) / "learning.sqlite3"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT h.binding_state,h.abandoned_reason,a.released_at_ms IS NOT NULL FROM active_run_capacity a "
            "JOIN host_run_bindings h ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest "
            "ORDER BY a.claimed_at_ms")]
    finally:
        connection.close()


def ledger_run(world, run) -> dict | None:
    """The ledger's capacity row for this run (matched by the run id the open reply gave)."""
    wanted = (run.opened or {}).get("run_id")
    rows = [row for row in ledger_slots(world) if wanted and row["run_id"] == wanted]
    return rows[0] if len(rows) == 1 else None


def project_rows(world, project_id: str | None) -> dict:
    """The account ledger's rows of one project, by table (names and numbers only): what deletion must remove."""
    path = state_dir(world) / "learning.sqlite3"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        counts = {}
        for name in tables:
            columns = [row[1] for row in connection.execute(f'pragma table_info("{name}")')]
            if "project_id" in columns:
                counts[name] = connection.execute(f'select count(*) from "{name}" where project_id = ?',
                                                  (project_id,)).fetchone()[0]
        return counts
    finally:
        connection.close()


RESULT_FRAME = {"type": "result", "subtype": "success", "is_error": False,
                "usage": {"input_tokens": 120, "output_tokens": 30}, "total_cost_usd": 0.1, "permission_denials": []}


def arm(ctl, run, seconds: int) -> None:
    """The fake client answers this run correctly after `seconds` of silence (a child that runs for a while)."""
    from e2e_checks_run import child_answer
    template = (ctl.host("result-preflight", *run.common)[1] or {}).get("template")
    if template is None:
        raise e2e_support.HarnessError("the result preflight gave no template")
    answer = child_answer(template, False)
    run.answer = answer
    frames = [{"sleep": seconds}] if seconds else []
    child_frames(ctl.view, *frames, {**RESULT_FRAME, "result": json.dumps(answer), "structured_output": answer})


def record(ctl, run) -> tuple:
    """Record the run's result as the primary does (it frees the project's preparation slot)."""
    result = (run.reply or {}).get("result")
    if result is None:
        return (None, None, "")
    terminal = ((run.reply or {}).get("bridge_report") or {}).get("terminal_state")
    observation = {"run_handle": (run.opened or {}).get("run_handle"),
                   "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]}
    if terminal:
        observation["terminal_state"] = terminal
    run.recorded = ctl.host("learning-record-host-result", *run.common,
                            "--result", ctl.save(run.name + "-result", result),
                            "--observation", ctl.save(run.name + "-observation", observation))
    return run.recorded


def lifecycle(world) -> dict:
    """The project's counts as `learning-status` reports them (a read other than the run's own commands)."""
    reply = world.cli("learning-status")[1] or {}
    managed = (reply.get("ingestion") or {}).get("managed_lifecycle")
    return {"ready": reply.get("ready"), "managed": managed, "earlier": reply.get("earlier_version_runs"),
            "stuck": (reply.get("project_service") or {}).get("stuck_run_slots")}


def phase_view(ctl, run=None, *, expect_refusal: bool = False) -> dict:
    """What the status commands answer right now: learning-status, learning-orchestration-status, the run's
    learning-run-status and the migration status (its earlier_version_runs). Texts are kept for the privacy scan."""
    world = ctl.view
    out = {"lifecycle": lifecycle(world)}
    code, reply, text = ctl.host("learning-orchestration-status", "--orchestration-handle", ctl.handle)
    out["orchestration"] = {"code": code, "reply": reply}
    if run is not None:
        code, reply, text = ctl.host("learning-run-status", "--orchestration-handle", ctl.handle,
                                     "--run-handle", (run.opened or {}).get("run_handle", ""),
                                     refuses=expect_refusal)
        out["run"] = {"code": code, "reply": reply}
    code, reply, text = world.cli("learning-migrate-project", "--status")
    out["migration"] = {"code": code, "reply": reply}
    out["report"] = ctl.host("learning-orchestration-report", "--orchestration-id",
                             ctl.orchestration.get("orchestration_id", ""))[1]
    return out


def bridge_of(run) -> str | None:
    return getattr(run, "bridge", None) or bridge_run(run)


def capacity_state(view, run, *, seconds: float = 40) -> dict:
    return wait_released(view, run, seconds=seconds)


# ---------------------------------------------------------------------------------------------
# The fault (test only, chosen by the runner's --fault argument): a lost run's slot is never released, a finished
# run's slot is never released, and a deleted project's ledger records stay.

FAULTS["recovery_broken"] = [
    {"module": "codex_agents.account_lost_run",
     "old": "            ledger._release_slot_locked(project_id, context.handle, 'lost', ledger._clock_ms(),\n"
            "                                        ledger._capacity_clock_reference())\n",
     "new": "            pass\n"},
    {"module": "codex_agents.learning_store",
     "old": "                if not decided:\n                    self._connection.execute(\n"
            "                        \"UPDATE active_run_capacity SET released_at_ms",
     "new": "                if False:\n                    self._connection.execute(\n"
            "                        \"UPDATE active_run_capacity SET released_at_ms"},
    {"module": "codex_agents.account_cleanup",
     "old": "'WHERE project_id=? AND handle_digest=?', (timestamp, receipt_digest, project_id, key.handle_digest))\n"
            "            before_commit()\n            _write_journal",
     "new": "'WHERE project_id=? AND handle_digest=? AND 0', (timestamp, receipt_digest, project_id, key.handle_digest))\n"
            "            before_commit()\n            _write_journal"},
    {"module": "codex_agents.learning_store",
     "old": "                self._connection.execute(\"DELETE FROM records WHERE project_id=?\", (project_id,))\n"
            "                self._connection.execute(\"DELETE FROM sessions WHERE project_id=?\", (project_id,))",
     "new": "                self._connection.execute(\"DELETE FROM records WHERE project_id=? AND 0\", (project_id,))\n"
            "                self._connection.execute(\"DELETE FROM sessions WHERE project_id=?\", (project_id,))"}]
# What a fault run may newly turn red besides the S2 IDs: nothing (a refused dispatch or a failed read is judged by
# the S2 checks that made it; G1 is not declared because every step the fault could refuse passes `refuses`).
FAULT_ALSO_RED: tuple = ()


def fault_isolated(normal: dict, fault: dict) -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID: the fault run must hold the same IDs,
    turn every S2 ID red, and change no ID outside S2 (an ID red in both runs is a product finding, not isolation)."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    outside = [i for i in changed if i not in S2_IDS and i not in FAULT_ALSO_RED]
    green = sorted(i for i in S2_IDS if (after.get(i) or {}).get("passed") is not False)
    missing = sorted(set(before) ^ set(after)) + sorted(i for i in S2_IDS if i not in after)
    return {"isolated": not outside and not green and not missing, "changed": changed,
            "changed_outside_s2": outside, "s2_not_red": green, "ids_differ": missing}


# ---------------------------------------------------------------------------------------------
# Scene 1: a killed and replaced project service (L8.1, L8.3, L14.1)

def note_service(world) -> list[int]:
    return processes(world, "project_host.py")


def poll(condition, seconds: float, every: float = 0.7):
    """Run `condition` until it returns something true or `seconds` pass; its last value either way."""
    deadline = time.monotonic() + seconds
    while True:
        value = condition()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(every)


def service_scene():
    if "rec_service" in _SCENES:
        return _SCENES["rec_service"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world, phases={})
    _SCENES["rec_service"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    set_up(world)
    scene.phases["idle"] = {"lifecycle": lifecycle(world)}
    ctl = scene.ctl = Ctl(world, "p")
    scene.phases["orchestration"] = phase_view(ctl)
    run = scene.run = ctl.child(open_run=False)
    arm(ctl, run, 14)
    ctl.open(run)
    scene.phases["opened"] = phase_view(ctl, run, expect_refusal=True)
    ctl.dispatch_in_background(run, refuses=True)  # the stimulus below cuts it short: its refusal is expected
    scene.running = ctl.wait_running(run)
    scene.phases["running"] = phase_view(ctl, run)
    row = ledger_run(world, run)
    run.bridge = (row or {}).get("bridge_run_id")
    scene.slot_before = row
    scene.read_before = capacity_read(world, run)
    scene.old_service = note_service(world)
    scene.killed = bool(scene.old_service) and kill_hard(scene.old_service[0])
    scene.service_after_kill = note_service(world)
    ctl.finish(run)
    scene.dispatch_code, scene.dispatch_reply, scene.dispatch_text = run.code, run.reply, run.text
    scene.start = ctl.host("learning-start-project-service")
    scene.new_service = note_service(world)

    def lost_and_released():
        code, reply, _ = ctl.host("learning-run-status", "--orchestration-handle", ctl.handle,
                                  "--run-handle", run.opened["run_handle"], refuses=True)
        scene.lost_reads = getattr(scene, "lost_reads", []) + [(reply or {}).get("capacity_state")]
        return reply if (reply or {}).get("reason_code") == "run_lost" and (reply or {}).get("capacity_state") == "released" else None
    scene.lost_status = poll(lost_and_released, 60, every=1.5)
    scene.phases["lost"] = phase_view(ctl, run)
    scene.account_read = wait_released(world, run, seconds=40)
    scene.slot_after = ledger_run(world, run)
    scene.bindings = claimed_bindings(world)
    scene.counts_first = run_counts(world, ctl.project_id)
    # A second replacement: the settled run must not be settled, claimed or recorded again.
    scene.second_old = note_service(world)
    scene.second_killed = bool(scene.second_old) and kill_hard(scene.second_old[0])
    scene.second_start = ctl.host("learning-start-project-service")
    time.sleep(8)
    scene.counts_second = run_counts(world, ctl.project_id)
    scene.status_second = ctl.status(run)
    # The slot is free: the next child claims it and finishes.
    nxt = scene.next = ctl.child(open_run=False)
    arm(ctl, nxt, 0)
    ctl.open(nxt)
    ctl.dispatch(nxt)
    record(ctl, nxt)
    row = ledger_run(world, nxt)
    nxt.bridge = (row or {}).get("bridge_run_id")
    scene.next_outcome = persisted_outcome(world, nxt, ctl.project_id)
    scene.next_binding = settled_binding(world, nxt, outcome=scene.next_outcome)
    scene.next_read = wait_released(world, nxt, seconds=40)
    scene.phases["next"] = phase_view(ctl, nxt)
    # Can the orchestration close? (L8.3). If the close is refused, try the step the run's own status names.
    # Recovery and close are expected to succeed (judged by G1): a refusal here is a finding, not an expectation.
    scene.close_first = ctl.host("learning-close-orchestration", "--orchestration-handle", ctl.handle,
                                 "--status", "completed")
    scene.close_recovery = []
    if scene.close_first[0] != 0:
        scene.close_recovery.append(ctl.host("learning-pause-orchestration", "--orchestration-handle", ctl.handle))
        scene.close_recovery.append(ctl.host("learning-classify-host-run-abandoned", "--run-handle",
                                             run.opened["run_handle"], "--reason", "host_confirmed_gone"))
        scene.close_recovery.append(ctl.host("learning-close-orchestration", "--orchestration-handle", ctl.handle,
                                             "--status", "completed"))
    scene.phases["closed"] = phase_view(ctl, nxt)
    scene.report = ctl.host("learning-orchestration-report", "--orchestration-id",
                            ctl.orchestration.get("orchestration_id", ""))[1] or {}
    return scene


# ---------------------------------------------------------------------------------------------
# Scene 2: a killed collector (L8.2), a second project's setup during a dispatch (L8.3), a deleted project (L15.1)

def collector_scene():
    if "rec_collector" in _SCENES:
        return _SCENES["rec_collector"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["rec_collector"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    set_up(world)
    ctl = scene.ctl = Ctl(world, "a")
    # L8.2: the collector is killed while a child runs.
    run = scene.killed_run = ctl.child(open_run=False)
    arm(ctl, run, 14)
    ctl.open(run)
    ctl.dispatch_in_background(run)  # judged by G1: the product must not fail the dispatch
    ctl.wait_running(run)
    row = ledger_run(world, run)
    run.bridge = (row or {}).get("bridge_run_id")
    scene.slot_before = row
    scene.collector_old = processes(world, "collector_host.py")
    scene.service_old = note_service(world)
    scene.collector_killed = bool(scene.collector_old) and kill_hard(scene.collector_old[0])
    scene.collector_down = processes(world, "collector_host.py")
    scene.service_during = note_service(world)
    ctl.finish(run)
    scene.dispatch_code, scene.dispatch_reply = run.code, run.reply
    scene.back = world.cli("learning-status")  # the owner's next command brings the collector back
    scene.collector_new = poll(lambda: processes(world, "collector_host.py"), 20)
    scene.read_back = wait_released(world, run, seconds=60)
    scene.slot_after = ledger_run(world, run)
    scene.status_after = ctl.status(run)  # before the result is recorded (a recorded run is forgotten)
    record(ctl, run)
    scene.outcome = persisted_outcome(world, run, ctl.project_id)
    scene.binding = settled_binding(world, run, outcome=scene.outcome)
    scene.service_survived = (bool(scene.service_old) and scene.service_during == scene.service_old
                              and all(alive(pid) for pid in scene.service_old))
    scene.counts_first = run_counts(world, ctl.project_id)
    old = processes(world, "collector_host.py")
    scene.second_killed = bool(old) and kill_hard(old[0])
    scene.second_back = world.cli("learning-status")
    time.sleep(8)
    scene.counts_second = run_counts(world, ctl.project_id)
    # L8.3: another project's setup runs while this project's dispatch is running.
    other = scene.second_run = ctl.child(open_run=False)
    arm(ctl, other, 14)
    ctl.open(other)
    ctl.dispatch_in_background(other, refuses=True)  # refusal expected ONLY if the service is observed replaced: judged again below
    scene.other_running = ctl.wait_running(other)  # judged: the run really runs when the setup starts
    row = ledger_run(world, other)
    other.bridge = (row or {}).get("bridge_run_id")
    scene.other_slot_before = row  # THIS run's slot, never the earlier run's
    scene.collector_before_setup = processes(world, "collector_host.py")
    scene.services_before_setup = note_service(world)
    scene.dispatch_service_before = processes(world, "project_host.py", world.project)
    second = scene.second = add_project(world, "second")
    scene.second_setup = second.setup(second.answers(providers={
        "codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL, "bridge": "off"}))
    scene.collector_after_setup = processes(world, "collector_host.py")
    poll(lambda: service_replaced(scene.dispatch_service_before, processes(world, "project_host.py", world.project)), 25)
    scene.dispatch_service_after = processes(world, "project_host.py", world.project)
    ctl.finish(other)
    scene.other_code, scene.other_reply, scene.other_text = other.code, other.reply, other.text
    # The capture let this dispatch refuse; an untouched service must not have refused it (G1), a replaced one may.
    # The step was captured as expected-to-refuse, so the unexpected exit is added here, once the service is known.
    e2e_support.CAPTURE_BREAKS.extend(b for b in dispatch_g1_breaks(
        service_replaced(scene.dispatch_service_before, scene.dispatch_service_after), other.code)
        if b == "g1_unexpected_exit")
    scene.other_read = wait_released(world, other, seconds=60)
    scene.other_slot = ledger_run(world, other)
    scene.other_status = ctl.status(other)
    record(ctl, other)
    scene.close = ctl.host("learning-close-orchestration", "--orchestration-handle", ctl.handle, "--status",
                           "completed")
    scene.report = ctl.host("learning-orchestration-report", "--orchestration-id",
                            ctl.orchestration.get("orchestration_id", ""))[1] or {}
    # L15.1: delete the second project; the first must be untouched.
    second.claude_frames(e2e_support.claude_probe_frames())
    second.cli("provider-observe", "--provider", "codex")
    second.cli("provider-observe", "--provider", "claude")
    other_ctl = scene.second_ctl = Ctl(second, "b")
    scene.deleted_id = other_ctl.project_id
    scene.kept_id = ctl.project_id
    scene.deleted_rows_before = project_rows(world, scene.deleted_id)
    scene.kept_rows_before = project_rows(world, scene.kept_id)
    scene.kept_history_before = count_by_type(world, scene.kept_id)
    scene.kept_service_before = note_service(world)
    scene.deleted_service = [pid for pid in scene.kept_service_before if pid not in scene.services_before_setup]
    scene.deleted_history_before = project_history(world, scene.deleted_id).exists()
    scene.kept_status_before = lifecycle(world)
    scene.kept_identity_before = survivor_identity(world, scene.kept_id)
    scene.deleted_paths = sorted({str(second.project), os.path.realpath(second.project)})
    scene.delete = second.cli("learning-delete-project", "--confirm-project-id", scene.deleted_id)
    scene.deleted_rows_after = project_rows(world, scene.deleted_id)
    scene.kept_rows_after = project_rows(world, scene.kept_id)
    scene.kept_history_after = count_by_type(world, scene.kept_id)
    scene.kept_status_after = lifecycle(world)
    scene.kept_identity_after = survivor_identity(world, scene.kept_id)
    scene.kept_report_after = ctl.host("learning-orchestration-report", "--orchestration-id",
                                       ctl.orchestration.get("orchestration_id", ""))[1] or {}
    scene.deleted_service_gone = poll(lambda: not any(alive(pid) for pid in scene.deleted_service), 25)
    scene.deleted_history_after = project_history(world, scene.deleted_id).exists()
    scene.services_final = note_service(world)
    scene.deleted_service_final = processes(world, "project_host.py", second.project)  # identity, not one pid
    scene.deleted_registration = registration_row(world, scene.deleted_id)
    scene.kept_registration = registration_row(world, scene.kept_id)
    scene.deleted_observations = observation_rows(world, scene.deleted_id)
    return scene


# ---------------------------------------------------------------------------------------------
# The checks

def settled(reply: dict | None) -> bool:
    """A run that is not in limbo: it is observed ended with its cleanup confirmed, or reported lost, abandoned and
    released. A missing or null reply is never settled."""
    reply = reply or {}
    ended = reply.get("ready") is True and reply.get("terminal_observed") is True and reply.get("cleanup_confirmed") is True
    lost = (reply.get("reason_code") == "run_lost" and reply.get("terminal_state") == "abandoned"
            and reply.get("capacity_state") == "released")
    return ended or lost


def plain(reply: dict | None, text: str) -> bool:
    """The CLI says what happened and what to do, in words: an error and a next step, no traceback, and not the
    misleading authorization refusal the control project met (L8b)."""
    reply = reply or {}
    return (isinstance(reply.get("error"), str) and len(reply["error"]) > 8
            and isinstance(reply.get("next_step"), str) and len(reply["next_step"]) > 8
            and "Traceback" not in text and "dispatch_not_authorized" not in text
            and reply.get("reason_code") not in (None, "dispatch_not_authorized"))


def lost_run_facts(phase: Phase, scene) -> None:
    """What every lost-run check shares: the service really died and a different one answers."""
    phase.expect(len(scene.old_service) == 1, "exactly one project service ran before the stimulus")
    phase.expect(scene.killed is True, "the project service was killed with SIGKILL and is gone")
    phase.expect(scene.service_after_kill == [], "no project service ran right after the kill")
    phase.expect(scene.start[0] == 0 and (scene.start[1] or {}).get("ready") is True
                 and (scene.start[1] or {}).get("serving") is True, "the owner's next command starts a service")
    phase.expect(len(scene.new_service) == 1 and scene.new_service[0] != scene.old_service[0],
                 "the service that answers now is a different process")


@check("L8.1", group="rec_service")
def l8_1_killed_service_next_service_settles_run_lost_slot_freed_nothing_twice(phase: Phase) -> None:
    scene = service_scene()
    run, world = scene.run, scene.world
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    phase.expect(is_running(scene.running), "the child is running before the kill (explicit, never missing)")
    phase.expect(scene.slot_before is not None and scene.slot_before["released_at_ms"] is None,
                 "the account ledger holds the run's slot, unreleased, before the kill")
    phase.expect(isinstance(scene.read_before.get("state"), str) and scene.read_before["state"] != "released",
                 "the account's own per-run read does not say released while the child runs")
    lost_run_facts(phase, scene)
    phase.expect(scene.dispatch_code not in (None, 0), "the dispatch that was running when its service died ends, refused")
    status = scene.lost_status or {}
    phase.expect(status.get("reason_code") == "run_lost", "the next service reports the run as lost")
    phase.reason("run_lost")
    phase.expect(status.get("terminal_state") == "abandoned" and status.get("outcome") == "unknown",
                 "the lost run is abandoned with an unknown outcome")
    phase.expect(status.get("capacity_state") == "released", "the service reports the run's capacity released")
    phase.expect(scene.account_read.get("state") == "released",
                 "the account's own per-run read (polled, bounded) says the slot is released")
    phase.expect(scene.slot_after is not None and isinstance(scene.slot_after["released_at_ms"], int),
                 "the ledger's capacity table holds the slot released")
    phase.expect(scene.bindings == [("abandoned", "service_lost", 1)],
                 "the ledger settled the run abandoned, service_lost, released (and no other run is bound)")
    first, second = scene.counts_first, scene.counts_second
    phase.expect(first["ledger"]["active_run_capacity"] == 1 and first["ledger"]["host_run_bindings"] == 1
                 and first["history"].get("execution_provenance") == 1
                 and first["history"].get("host_open_provenance") == 1,
                 "the lost run left one slot, one binding and one provenance record (counts by kind)")
    phase.expect(first == second, "a second replacement of the service records nothing again (counts by kind)")
    phase.expect(scene.second_killed is True and (scene.second_start[1] or {}).get("serving") is True,
                 "the second replacement really replaced the service")
    phase.expect((scene.status_second or {}).get("reason_code") == "run_lost"
                 and (scene.status_second or {}).get("capacity_state") == "released",
                 "after the second replacement the run still reads lost and released")
    nxt = scene.next
    phase.expect(nxt.code == 0 and scene.next_outcome == "completed"
                 and persisted_terminal(scene.next_binding, scene.next_outcome) == "completed",
                 "the next child claims the freed slot and completes (persisted outcome record read explicitly: "
                 "outcome completed, ledger terminal and released; not the reply)")
    phase.expect(scene.next_read.get("state") == "released", "the next run's slot is released by the account's own read")


@check("L8.3", group="rec_service")
def l8_3_replaced_service_leaves_no_run_in_limbo_cli_says_so_plainly_orchestration_closes(phase: Phase) -> None:
    scene = service_scene()
    lost_run_facts(phase, scene)
    phase.expect(scene.dispatch_code not in (None, 0), "the dispatch that lost its service ends with a refusal")
    phase.expect(plain(scene.dispatch_reply, scene.dispatch_text),
                 "the dispatching CLI says in words what happened and what to do, never dispatch_not_authorized")
    phase.reason((scene.dispatch_reply or {}).get("reason_code") or "no_reason")
    status = scene.lost_status or {}
    phase.expect(status.get("reason_code") == "run_lost" and status.get("terminal_state") == "abandoned"
                 and status.get("capacity_state") == "released", "the run ends run_lost, abandoned, capacity released")
    phase.expect(isinstance(status.get("error"), str) and isinstance(status.get("next_step"), str)
                 and "stopped" in status["error"], "run-status says the service stopped, in words")
    phase.expect(scene.account_read.get("state") == "released", "the account's own read agrees the slot is freed")
    final = scene.close_recovery[-1] if scene.close_recovery else scene.close_first
    phase.expect(closed_ok(final),
                 "the orchestration closes (after the step the run's own status names, if one is needed)")
    phase.expect(scene.report.get("status") == "completed", "the orchestration report says completed")


@check("L14.1", group="rec_service")
def l14_1_status_commands_give_the_right_values_at_each_phase_and_none_is_private(phase: Phase) -> None:
    scene = service_scene()
    world, phases = scene.world, scene.phases
    zero = {"host_runs_abandoned": 0, "host_runs_opened": 0, "host_runs_pending": 0, "host_runs_terminal": 0,
            "orchestrations_active": 0, "orchestrations_closed": 0, "orchestrations_paused": 0}

    def counts(view: dict, *names: str) -> dict:
        return {name: ((view["lifecycle"] or {}).get("managed") or {}).get(name) for name in names}

    def report_runs(view: dict) -> dict:
        return (view.get("report") or {}).get("runs") or {}

    idle = phases["idle"]["lifecycle"]
    phase.expect(idle["ready"] is True and counts(phases["idle"], *zero) == zero,
                 "idle: learning-status is ready and counts no run and no orchestration")
    phase.expect(idle["earlier"] == {"finished_by_ms": None, "holding": 0} and idle["stuck"] == 0,
                 "idle: no earlier-version run holds a slot and no run slot is stuck")
    view = phases["orchestration"]
    phase.expect((view["orchestration"]["reply"] or {}).get("ready") is True
                 and (((view["orchestration"]["reply"] or {}).get("current") or {}).get("snapshot") or {}).get("status") == "active",
                 "orchestration open: learning-orchestration-status says active")
    phase.expect((view["report"] or {}).get("status") == "active" and report_runs(view).get("admitted") == 0
                 and report_runs(view).get("pending") == 0, "orchestration open: the report is active with no run")
    view = phases["opened"]
    phase.expect(counts(view, "host_runs_opened", "host_runs_pending", "host_runs_terminal")
                 == {"host_runs_opened": 1, "host_runs_pending": 1, "host_runs_terminal": 0},
                 "run opened: learning-status counts one pending run")
    phase.expect(report_runs(view).get("admitted") == 1 and report_runs(view).get("pending") == 1,
                 "run opened: the report counts one admitted, pending run")
    refusal = view["run"]
    phase.expect(refusal["code"] == 2 and (refusal["reply"] or {}).get("ready") is False
                 and isinstance((refusal["reply"] or {}).get("next_step"), str),
                 "run opened, not yet dispatched: learning-run-status refuses and says what to do")
    view = phases["running"]
    reply = view["run"]["reply"] or {}
    phase.expect(view["run"]["code"] == 0 and reply.get("ready") is True and reply.get("health") == "active"
                 and reply.get("terminal_observed") is False, "running: learning-run-status says active, not ended")
    phase.expect(report_runs(view).get("opened") == 1 and report_runs(view).get("pending") == 1
                 and report_runs(view).get("current_active") == 1 and report_runs(view).get("terminal") == 0,
                 "running: the report counts one run, pending and active")
    phase.expect(counts(view, "host_runs_pending")["host_runs_pending"] == 1,
                 "running: learning-status counts one pending run")
    view = phases["lost"]
    reply = view["run"]["reply"] or {}
    phase.expect(reply.get("reason_code") == "run_lost" and reply.get("terminal_state") == "abandoned"
                 and reply.get("capacity_state") == "released", "lost: learning-run-status says lost, abandoned, released")
    phase.expect(counts(view, "host_runs_pending", "host_runs_abandoned")
                 == {"host_runs_pending": 0, "host_runs_abandoned": 1}, "lost: learning-status counts one abandoned run")
    phase.expect(((view["migration"]["reply"] or {}).get("earlier_version_runs") or {}).get("holding") == 0,
                 "lost: the migration status reports no earlier-version run holding a slot")
    view = phases["next"]
    phase.expect(counts(view, "host_runs_opened", "host_runs_pending", "host_runs_terminal", "host_runs_abandoned")
                 == {"host_runs_opened": 2, "host_runs_pending": 0, "host_runs_terminal": 1, "host_runs_abandoned": 1},
                 "after the next run: learning-status counts two runs, one ended, one abandoned, none pending")
    phase.expect(migration_phases_judged(phases, MIGRATION_PHASES),
                 "the migration status is judged at every phase that collects one (idle collects the lifecycle status only)")
    for name in MIGRATION_PHASES:
        migration = phases[name]["migration"]
        phase.expect(migration["code"] == 0 and ((migration["reply"] or {}).get("earlier_version_runs") or {})
                     .get("holding") == 0, f"{name}: the migration status reports no earlier-version run holding a slot")
    view = phases["closed"]
    phase.expect(counts(view, "host_runs_pending", "host_runs_terminal", "host_runs_abandoned")
                 == {"host_runs_pending": 0, "host_runs_terminal": 1, "host_runs_abandoned": 1},
                 "after the close attempt: learning-status still counts one ended, one abandoned, none pending")
    texts = [text for text in world.outputs]
    phase.expect(len(texts) > 20 and not [name for text in texts for name in world.leaks(text)],
                 "no status output carries a private value (owner key, passphrase, key material, home path)")
    # The readings the product got wrong, last so that every other value is judged first; all are judged, none
    # is skipped because an earlier one failed.
    closed = counts(phases["closed"], "orchestrations_active", "orchestrations_closed")
    expect_each(phase, [
        (counts(phases["orchestration"], "orchestrations_active")["orchestrations_active"] == 1,
         "orchestration open: learning-status counts one active orchestration"),
        (report_runs(phases["lost"]).get("current_active") == 0 and report_runs(phases["lost"]).get("pending") == 0,
         "lost: the report no longer counts the released, abandoned run as active or pending"),
        (closed == {"orchestrations_active": 0, "orchestrations_closed": 1}
         and (phases["closed"]["report"] or {}).get("status") == "completed",
         "after the close attempt: learning-status counts the orchestration closed, the report says completed")])


@check("L8.2", group="rec_collector")
def l8_2_killed_collector_run_settles_once_back_slot_freed_nothing_twice(phase: Phase) -> None:
    scene = collector_scene()
    run = scene.killed_run
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    phase.expect(len(scene.collector_old) == 1 and scene.slot_before is not None
                 and scene.slot_before["released_at_ms"] is None, "one collector ran and the run held its slot")
    phase.expect(scene.collector_killed is True and scene.collector_down == [], "the collector was killed and is gone")
    phase.expect(run.thread is not None and not run.thread.is_alive() and scene.dispatch_code is not None,
                 "the dispatch running while the collector was down ends")
    phase.expect(scene.back[0] == 0 and (scene.back[1] or {}).get("ready") is True,
                 "the owner's next command answers and brings the collector back")
    phase.expect(len(scene.collector_new) == 1 and scene.collector_new[0] != scene.collector_old[0],
                 "a new collector process runs")
    phase.expect(scene.read_back.get("state") == "released",
                 "once it is back the account's own per-run read (polled, bounded) says released")
    phase.expect(scene.slot_after is not None and isinstance(scene.slot_after["released_at_ms"], int),
                 "the ledger's capacity table holds the slot released")
    terminal = persisted_terminal(scene.binding, scene.outcome)
    phase.expect(terminal in ("run_lost", "completed"),
                 "the persisted ledger fields and, for completed, the persisted outcome (outcome == completed, read "
                 "explicitly) settle the run as exactly run_lost or completed, released (never limbo)")
    phase.reason(terminal or "limbo")
    phase.expect(status_agrees(terminal, scene.status_after),
                 "the run's own status agrees explicitly with the persisted terminal (an empty or running status does not)")
    phase.expect(scene.service_during == scene.service_old and scene.service_survived is True,
                 "the project service survived the collector's death (the same process, alive)")
    first, second = scene.counts_first, scene.counts_second
    phase.expect(first["ledger"]["active_run_capacity"] == 1 and first["ledger"]["host_run_bindings"] == 1,
                 "the run left one slot and one binding (counts by kind)")
    phase.expect(scene.second_killed is True and (scene.second_back[1] or {}).get("ready") is True,
                 "a second collector death and return really happened")
    phase.expect(first == second, "nothing was recorded again after the second return (counts by kind)")


@check("L8.3", group="rec_collector")
def l8_3_second_projects_setup_during_a_dispatch_leaves_no_run_in_limbo(phase: Phase) -> None:
    scene = collector_scene()
    run = scene.second_run
    phase.expect(is_running(scene.other_running), "this run was running (explicit status) when the second project's setup began")
    phase.expect(scene.other_slot_before is not None and scene.other_slot_before["released_at_ms"] is None,
                 "this run held its own slot, unreleased, when the setup began")
    phase.expect(len(scene.collector_before_setup) == 1, "one shared collector ran while the dispatch ran")
    phase.expect(scene.second_setup.code == 0, "the second project's scripted setup finishes during the dispatch")
    phase.expect(len(scene.collector_after_setup) == 1, "a collector runs after the second project's setup")
    # 0.8.56 no longer replaces a project's service when another project is set up (the L8b incident is fixed), so the
    # scenario has two outcomes, each judged in full: the service WAS replaced (new process, old one gone; the
    # dispatch must then be cut short and say so), or it was NOT (the same single process, alive; the dispatch must
    # then complete untouched). Anything else (no service, two services) fails.
    replaced_service = service_replaced(scene.dispatch_service_before, scene.dispatch_service_after)
    kept_service = (len(scene.dispatch_service_before) == 1 and scene.dispatch_service_after == scene.dispatch_service_before
                    and alive(scene.dispatch_service_before[0]))
    phase.expect(replaced_service or kept_service,
                 "the dispatching project's service is either replaced (new process, old gone) or untouched (same live process)")
    phase.reason("service_replaced" if replaced_service else "service_untouched")
    replaced = scene.collector_after_setup[0] != scene.collector_before_setup[0]
    phase.reason("collector_replaced" if replaced else "collector_unchanged")
    phase.expect(run.thread is not None and not run.thread.is_alive() and scene.other_code is not None,
                 "the dispatch running during the setup ends")
    phase.expect("dispatch_not_authorized" not in scene.other_text and "Traceback" not in scene.other_text,
                 "the dispatching CLI never answers dispatch_not_authorized")
    if replaced_service:
        phase.expect(scene.other_code != 0 and plain(scene.other_reply, scene.other_text),
                     "a dispatch whose service was replaced is cut short and says in words what happened and what to do")
    else:
        phase.expect(scene.other_code == 0 and ((scene.other_reply or {}).get("result") or {}).get("execution_state") == "completed",
                     "a dispatch whose service was untouched completes")
    phase.expect(scene.other_read.get("state") == "released", "the account's own per-run read says the slot is released")
    phase.expect(scene.other_slot is not None and isinstance(scene.other_slot["released_at_ms"], int),
                 "the ledger's capacity table holds the slot released")
    phase.expect(settled(scene.other_status), "the run is not in limbo (settled or lost and released)")
    phase.expect(closed_ok(scene.close), "the orchestration closes")
    runs = scene.report.get("runs") or {}
    phase.expect(scene.report.get("status") == "completed" and runs.get("pending") == 0
                 and runs.get("current_active") == 0, "the report says completed with no run pending or active")


@check("L15.1", group="rec_collector")
def l15_1_delete_project_removes_records_and_service_leaves_the_other_untouched(phase: Phase) -> None:
    scene = collector_scene()
    phase.expect(scene.second_setup.code == 0 and len(scene.deleted_service) == 1,
                 "the second project was set up and has its own service")
    before, after = scene.deleted_rows_before, scene.deleted_rows_after
    phase.expect(before.get("records", 0) > 0, "the second project had ledger records before the deletion")
    code, reply, _ = scene.delete
    phase.expect(code == 0 and (reply or {}).get("ready") is True and (reply or {}).get("project_id") == scene.deleted_id
                 and isinstance((reply or {}).get("deletion_id"), str), "learning-delete-project deletes the project")
    # The registration is scrubbed to a tombstone (no root fields); a deletion tombstone is the only row kept, and
    # the retained provider observations hold no project path and no run identifier.
    phase.expect(is_tombstone(scene.deleted_registration, scene.deleted_id),
                 "the deleted registration is a scrubbed tombstone: deleted, no root path, no root digest")
    phase.expect(not is_tombstone(scene.kept_registration, scene.kept_id)
                 and (scene.kept_registration or {}).get("status") == "active"
                 and (scene.kept_registration or {}).get("canonical_root"),
                 "the surviving project's registration is still active with its root")
    phase.expect(not observation_problems(scene.deleted_observations, scene.deleted_paths),
                 "retained provider observations hold no project path and no run identifier")
    left = {name: count for name, count in after.items()
            if count and name not in ("tombstones", "projects", "account_observation_origins",
                                      "account_observation_requests")}
    phase.expect(after != {} and not left, "the account ledger holds no content row of the deleted project (table by table)")
    phase.expect(after.get("tombstones") == 1, "the deletion leaves its tombstone")
    history = project_history(scene.world, scene.deleted_id)
    phase.expect(not history.exists() or count_by_type(scene.world, scene.deleted_id) == {},
                 "the deleted project's own history holds no records")
    phase.expect(scene.deleted_service_gone is True and scene.deleted_service_final == [],
                 "the deleted project's service is gone and no replacement service runs for it (final process listing)")
    phase.expect(scene.kept_rows_before.get("records", 0) > 0 and scene.kept_rows_after == scene.kept_rows_before,
                 "the other project's ledger rows are untouched (table by table)")
    kept = scene.kept_identity_before
    phase.expect(kept["sizes"][0] > 0 and kept["sizes"][1] > 0 and scene.kept_identity_after == kept,
                 "the other project's record IDs and content digests are unchanged (ledger and own history)")
    phase.expect(scene.kept_history_before and scene.kept_history_after == scene.kept_history_before,
                 "the other project's own history is untouched (counts by kind)")
    phase.expect(scene.kept_status_before["ready"] is True and scene.kept_status_after == scene.kept_status_before,
                 "the other project's status reads the same")
    phase.expect(all(alive(pid) for pid in scene.kept_service_before if pid not in scene.deleted_service),
                 "the other project's service still runs")
    phase.expect(scene.kept_report_after.get("status") == "completed"
                 and scene.kept_report_after.get("runs") == scene.report.get("runs"),
                 "the other project's orchestration report is unchanged")
