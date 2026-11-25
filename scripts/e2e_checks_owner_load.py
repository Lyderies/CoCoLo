"""L16: the sole ledger owner under load (T7 S7, PLAN-0.4.0.md M4b E2/E3): hook events and account calls at once.

Through the installed product only (decision 259): the installed hook delivers events to the running collector,
the installed CLI makes account calls (`provider-observe`, a request that is recorded once per request id) and
reads status, the installed client library asks the collector for health. Two projects share the one collector and
its one ledger owner: a large one whose hooks queue up and a small one whose few events must still get their turns.

Every value is read through a different path than the one that produced it: the account ledger (read-only SQLite:
the event rows in the order the owner wrote them, the recorded observation requests), the collector's status counters
(`learning-status`), the process table (the collector is the same process before and after) and a health prober run
in its own process. The load is released together by a barrier; every wait is for a process or thread to end or for
a prober line, never for a fixed time. The judges are plain functions (tests/test_e2e_owner_load.py proves each goes
red on the failure it names)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_run import trusted_interpreter_checks  # noqa: E402
from e2e_core import _SCENES, Phase, check  # noqa: E402
import e2e_checks_shared as shared  # noqa: E402
from e2e_checks_shared import add_project, state_dir, write_patches  # noqa: E402

S7_IDS = ("L16.1", "L16.2", "L16.3")
# The fault of this slice, chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in
# the process that runs the scenes). No environment variable selects it, and every World environment drops E2E_*.
FAULT: str | None = None
FAULTS = {
    # About a quarter of the hook events the collector acknowledges are never written (the owner skips the append).
    "ingest_lost": [
        {"module": "codex_agents.learning_store",
         "old": "                    result = self.ledger.append(\n                        binding.project_id, record, producer=binding.producer\n                    )",
         "new": "                    result = {} if int(record['record_id'][-1], 16) % 4 == 0 else self.ledger.append(\n"
                "                        binding.project_id, record, producer=binding.producer\n                    )"}],
    # Every project health waits 2.5 s before it answers while the stall flag file exists (the check makes it just
    # before the load, so set-up runs normally): health that does not stay responsive.
    "health_stalls": [
        {"module": "codex_agents.learning_store",
         "old": "                    # Read from the filesystem before the hold, so it covers ledger reads only.\n"
                "                    client_failures = client_health_snapshot(self.ledger.state, project_root)",
         "new": "                    if (self.ledger.state.root / 'e2e-health-stall').exists():\n"
                "                        time.sleep(2.5)\n"
                "                    # Read from the filesystem before the hold, so it covers ledger reads only.\n"
                "                    client_failures = client_health_snapshot(self.ledger.state, project_root)"}],
}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


# The load, in counts. The large project's four callers stay inside its share of the owner queue (6) and the two
# projects' callers inside the queue's 12 retained requests; a small project of two callers must be served among them.
BIG_WORKERS, BIG_EVENTS = 4, 6
SMALL_WORKERS, SMALL_EVENTS = 2, 2
ACCOUNT_WORKERS, ACCOUNT_IDS, ACCOUNT_REPEATS = 3, 2, 2  # each id is sent twice: the second is the same request again
# Health must answer within this while the load runs. The owner's own queue wait is 0.8 s; health does not wait for
# the owner, so this is generous for a loaded machine (a stalled health takes longer than every bound a client has).
HEALTH_LIMIT_MS = 2000

HEALTH_PROBE = """\
import json, sys, time
sys.path.insert(0, {orchestration!r})
from pathlib import Path
from codex_agents.collector_client import CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.collector_paths import CollectorPaths
state, stop, project = CollectorPaths(Path({state!r})), Path({stop!r}), Path({project!r})
client = CollectorClient(state.socket, timeout=10)
kinds = {{"plain": lambda: client.health(), "project": lambda: client.health(project)}}
seen = {{name: {{"answered": 0, "refused": 0, "max_ms": 0.0, "codes": []}} for name in kinds}}
first = True
while True:
    for name, ask in kinds.items():
        began = time.monotonic()
        try:
            ask()
            seen[name]["answered"] += 1
        except CollectorError as error:
            seen[name]["refused"] += 1
            seen[name]["codes"].append(error.code or "refused")
        seen[name]["max_ms"] = max(seen[name]["max_ms"], round((time.monotonic() - began) * 1000, 1))
    if first:
        print("ready", flush=True)
        first = False
    if stop.exists():
        break
print(json.dumps(seen))
"""


# Judges: plain functions over plain values, proven red by tests/test_e2e_owner_load.py --------------------------


def written_exactly_once(sent: int, turns: list) -> list[str]:
    """What is wrong with the events of one project: the count written against the count sent, and any turn twice."""
    broken = []
    if len(turns) < sent:
        broken.append("events_lost")
    if len(turns) > sent:
        broken.append("events_extra")
    if len(set(turns)) != len(turns):
        broken.append("events_duplicated")
    return broken


def served_among(order: list, small, large, small_count: int, large_count: int) -> list[str]:
    """Fair turns: `order` is each event's project in the order the owner wrote them. The small project's last event
    comes before the large one's last, and none of its events waits for most of the large project's backlog."""
    broken = []
    if order.count(small) != small_count or order.count(large) != large_count:
        return ["events_missing"]
    small_at = [index for index, project in enumerate(order) if project == small]
    large_at = [index for index, project in enumerate(order) if project == large]
    if small_at[-1] >= large_at[-1]:
        broken.append("small_project_last")
    if small_at[0] >= len(order) // 2:
        broken.append("small_project_waited_for_half")
    return broken


MINIMUM_HEALTH_ANSWERS = 3


def _count(value) -> bool:
    return type(value) is int and value >= 0


def health_verdict(seen: dict, limit_ms: float) -> list[str]:
    """The prober's own counts, for the plain and the project's health: answered at least `MINIMUM_HEALTH_ANSWERS` times
    (a reply that is `ok` with `ledger_busy` is an answer), never refused for any reason (a `response_invalid` or a
    transport refusal is not an expected answer under load), and none slower than the limit."""
    broken = []
    for name in ("plain", "project"):
        got = seen.get(name) or {}
        if not _count(got.get("answered")) or got["answered"] < MINIMUM_HEALTH_ANSWERS:
            broken.append(f"{name}_health_barely_answered")
        if not _count(got.get("refused")) or got["refused"] or got.get("codes"):
            broken.append(f"{name}_health_refused")
        if not isinstance(got.get("max_ms"), (int, float)) or got["max_ms"] > limit_ms:
            broken.append(f"{name}_health_slow")
    return broken


def _stamp(value) -> bool:
    return _count(value)


def account_verdict(replies: list, rows: list, projects: dict) -> list[str]:
    """The account calls. `replies`: what each call answered, with the project name it was made for and its request id;
    `rows`: (project id, observed_at_ms, state) of every observation request the ledger recorded during the load;
    `projects`: project name -> ledger project id. Every call is answered ok for its provider with an observation time;
    one (project, request id) always answers the same observation; the ledger holds exactly one committed row for each
    expected (project, request id), attributed to its project and carrying the observation time its callers were told."""
    broken = []
    if any(not isinstance(r, dict) or r.get("code") != 0 or r.get("provider") != "codex" or r.get("ready") is not True
           for r in replies):
        broken.append("account_call_not_answered")
    by_identity: dict = {}
    for r in replies:
        if isinstance(r, dict):
            by_identity.setdefault((r.get("project"), r.get("request_id")), set()).add(r.get("observed_at_ms"))
    if any(not _stamp(stamp) for stamps in by_identity.values() for stamp in stamps) \
            or any(not _stamp(row[1]) for row in rows):
        broken.append("observation_timestamp_missing")
    if any(len(stamps) != 1 for stamps in by_identity.values()):
        broken.append("account_request_answered_twice_differently")
    if any(row[2] != "committed" for row in rows):
        broken.append("account_record_not_committed")
    if len(rows) < len(by_identity):
        broken.append("account_request_lost")
    elif len(rows) > len(by_identity):
        broken.append("account_request_duplicated")
    else:
        expected = Counter((projects.get(name), min(stamps)) for (name, _), stamps in by_identity.items()
                           if all(_stamp(value) for value in stamps))
        recorded = Counter((row[0], row[1]) for row in rows)
        if expected != recorded:
            broken.append("account_record_misattributed")
    return broken


# Reads that do not go through the commands under load ---------------------------------------------------------


def ledger(world) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{state_dir(world) / 'learning.sqlite3'}?mode=ro", uri=True)


def event_rows(world) -> list[tuple]:
    """(project id, turn id) of every hook event, in the order the owner wrote them."""
    connection = ledger(world)
    try:
        return [(project, json.loads(bytes(content)).get("turn_id")) for project, content in connection.execute(
            "select project_id, content from records where record_type = 'event' order by rowid")]
    finally:
        connection.close()


def project_id(view) -> str | None:
    """The ledger's id for this view's project, by its registered root."""
    connection = ledger(view)
    try:
        row = connection.execute("select project_id from projects where canonical_root = ?",
                                 (os.path.realpath(view.project),)).fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def observation_requests(world) -> list[tuple]:
    """(project id, request key, observed_at_ms, state) of every recorded observation request."""
    connection = ledger(world)
    try:
        return [tuple(row) for row in connection.execute(
            "select project_id, request_key, observed_at_ms, state from account_observation_requests")]
    finally:
        connection.close()


def accepted_events(view) -> int | None:
    """The collector's own count of the hook events it accepted for this view's project (learning-status)."""
    code, status, _ = view.cli("learning-status")
    value = ((status or {}).get("ingestion") or {}).get("accepted_hook_event_count") if code == 0 else None
    return value if type(value) is int else None


def load_event(view, session: str, number: int) -> dict:
    return {"session_id": session, "cwd": str(view.project), "permission_mode": "default",
            "hook_event_name": "UserPromptSubmit", "turn_id": f"{session}-turn-{number}", "model": "gpt-6-luna",
            "transcript_path": None, "prompt": "load"}


def start_probe(view, stop: Path):
    """The health prober, in its own process through the installed client library; returns once it has had its first
    answers (its `ready` line), so the load starts with health already being asked."""
    code = HEALTH_PROBE.format(orchestration=str(view.project / ".codex" / "orchestration"),
                               state=str(state_dir(view)), stop=str(stop), project=str(view.project))
    process = subprocess.Popen([str(view.shim), "-I", "-B", "-S", "-c", code], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=view.env,
                               cwd=str(view.project))
    line = process.stdout.readline()
    if line.strip() != "ready":
        process.kill()
        out, err = process.communicate()
        view._capture(line + out + err, kind="output", code=process.returncode, refuses=False, timed_out=False)
        raise e2e_support.HarnessError("the health prober did not start")
    return process


def end_probe(view, process, stop: Path) -> dict:
    """Stop the prober (its stop file), judge it like any step (G1, G8) and return its counts."""
    stop.write_text("stop")
    out, err = process.communicate(timeout=60)
    text = out + err
    view.outputs.append(text)
    view.steps.append({"step": "health-probe", "code": process.returncode, "refuses": False, "timed_out": False})
    view._capture(text, kind="output", code=process.returncode, refuses=False, timed_out=False)
    try:
        seen = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return seen if isinstance(seen, dict) else {}


# The scene ---------------------------------------------------------------------------------------------------------


def owner_load_scene():
    """Two set-up projects on one collector, then the load, once; the checks read what it left."""
    if "owner_load" in _SCENES:
        return _SCENES["owner_load"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["owner_load"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    shared.set_up(world, "owner-load")
    scene.big, scene.small = world, add_project(world, "small")
    shared.set_up(scene.small, "owner-load")
    # One event each, so the collector is up before the load (what these write is not read back: only what follows is).
    for view, session in ((scene.big, "warm-big"), (scene.small, "warm-small")):
        view.run_hook(load_event(view, session, 0))
    scene.projects = {"big": project_id(scene.big), "small": project_id(scene.small)}
    scene.rows_before = len(event_rows(world))
    scene.accepted_before = {"big": accepted_events(scene.big), "small": accepted_events(scene.small)}
    scene.observations_before = observation_requests(world)
    scene.collectors_before = _collectors(world)
    drive_load(scene)
    return scene


def _collectors(world) -> list:
    from e2e_checks_recovery import processes
    return processes(world, "collector_host.py")


def drive_load(scene) -> None:
    world, big, small = scene.world, scene.big, scene.small
    stop = world.base / "probe-stop"
    if FAULT == "health_stalls":  # the fault is switched on for the load only
        (state_dir(world) / "e2e-health-stall").write_text("on")
    probe = start_probe(big, stop)
    workers = BIG_WORKERS + SMALL_WORKERS + ACCOUNT_WORKERS
    barrier = threading.Barrier(workers + 1)
    scene.hooks, scene.replies, errors = [], [], []
    lock = threading.Lock()

    def hooks(view, session, count):
        barrier.wait(120)
        for number in range(1, count + 1):
            code, _out, _text = view.run_hook(load_event(view, session, number))
            with lock:
                scene.hooks.append((session, code))

    def account(worker):
        barrier.wait(120)
        for index in range(ACCOUNT_IDS):
            request_id = f"load-{worker}-{index}"
            for _ in range(ACCOUNT_REPEATS):
                code, reply, _text = (small if worker % 2 else big).cli(
                    "provider-observe", "--provider", "codex", "--request-id", request_id)
                with lock:
                    scene.replies.append({"code": code, "request_id": request_id,
                                          "project": "small" if worker % 2 else "big",
                                          "provider": (reply or {}).get("provider"), "ready": (reply or {}).get("ready"),
                                          "observed_at_ms": (reply or {}).get("observed_at_ms")})

    jobs = [(hooks, (big, f"load-big-{n}", BIG_EVENTS)) for n in range(BIG_WORKERS)]
    jobs += [(hooks, (small, f"load-small-{n}", SMALL_EVENTS)) for n in range(SMALL_WORKERS)]
    jobs += [(account, (n,)) for n in range(ACCOUNT_WORKERS)]
    threads = [threading.Thread(target=_guarded, args=(function, arguments, errors), daemon=True)
               for function, arguments in jobs]
    for thread in threads:
        thread.start()
    barrier.wait(120)  # released together
    for thread in threads:
        thread.join(240)
    scene.worker_errors = errors + ["worker_still_running" for thread in threads if thread.is_alive()]
    scene.probe = end_probe(big, probe, stop)
    rows = event_rows(world)
    scene.rows = rows[scene.rows_before:]
    before = {(project, key) for project, key, _stamp_ms, _state in scene.observations_before}
    scene.observation_rows = [(project, stamp, state) for project, key, stamp, state in observation_requests(world)
                              if (project, key) not in before]
    scene.accepted_after = {"big": accepted_events(big), "small": accepted_events(small)}
    scene.collectors_after = _collectors(world)
    code, status, _ = big.cli("learning-status")
    scene.status = status if code == 0 and isinstance(status, dict) else {}


def _guarded(function, arguments, errors) -> None:
    try:
        function(*arguments)
    except BaseException as error:  # a worker's failure is the check's finding, never a hung thread
        errors.append(type(error).__name__)


# Checks ------------------------------------------------------------------------------------------------------------


def sent_per_project() -> dict:
    return {"big": BIG_WORKERS * BIG_EVENTS, "small": SMALL_WORKERS * SMALL_EVENTS}


@check("L16.1", group="owner_load")
def l16_1_hooks_and_account_calls_at_once_lose_and_duplicate_nothing(phase: Phase) -> None:
    s = owner_load_scene()
    phase.expect(s.worker_errors == [], "every load worker ran to its end")
    sent = sent_per_project()
    phase.expect(None not in s.projects.values(), "both projects are registered in the ledger")
    phase.expect(len(s.hooks) == sum(sent.values()) and all(code == 0 for _, code in s.hooks),
                 "every hook command ran and exited 0")
    for name, project in s.projects.items():
        turns = [turn for owner, turn in s.rows if owner == project]
        problems = written_exactly_once(sent[name], turns)
        phase.expect(problems == [], f"the {name} project's events are written exactly once")
        counted = (s.accepted_after[name] or 0) - (s.accepted_before[name] or 0)
        phase.expect(s.accepted_after[name] is not None and counted == sent[name],
                     f"the collector's own count of the {name} project's accepted events agrees with the ledger")
    phase.expect(len(s.rows) == sum(sent.values()), "no event was written for any other project")
    unique = ACCOUNT_WORKERS * ACCOUNT_IDS
    phase.expect(len(s.replies) == unique * ACCOUNT_REPEATS, "every account call came back")
    problems = account_verdict(s.replies, s.observation_rows, s.projects)
    phase.expect(problems == [],
                 "every account request was answered once and recorded once, committed, for its own project")
    refusals = (s.status.get("ledger_waits") or {}) if s.status else None
    phase.expect(refusals == {}, "the owner queue refused nothing inside its limits")
    phase.reason("hooks_and_account_calls_written_once")


@check("L16.2", group="owner_load")
def l16_2_small_project_gets_its_turns_while_the_large_projects_backlog_drains(phase: Phase) -> None:
    s = owner_load_scene()
    order = [owner for owner, _turn in s.rows]
    problems = served_among(order, s.projects["small"], s.projects["big"], sent_per_project()["small"],
                            sent_per_project()["big"])
    phase.expect(problems == [], "the small project's events are written among the large project's, not after them")
    phase.reason("projects_take_fair_turns")


@check("L16.3", group="owner_load")
def l16_3_health_stays_responsive_and_the_collector_is_not_restarted(phase: Phase) -> None:
    s = owner_load_scene()
    problems = health_verdict(s.probe, HEALTH_LIMIT_MS)
    phase.expect(problems == [], "health answered every time within its limit while the load ran")
    phase.expect(len(s.collectors_before) == 1 and s.collectors_before == s.collectors_after,
                 "the same one collector process served before and after the load")
    phase.expect(s.status.get("ready") is True, "the collector reports itself ready after the load")
    phase.expect(s.status.get("collector_previous_exit") in (None, {}), "the collector never stopped during the load")
    phase.reason("health_responsive_under_load")
