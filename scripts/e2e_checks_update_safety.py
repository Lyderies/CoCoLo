"""L11.2, L12.2 to L12.5 and G6: update safety and rollback (T7 slice S5).

The update is `./setup.sh --update PROJECT` of this checkout, run on a project installed by an EARLIER build. The earlier
build is the unmodified catalogue-listed 0.8.66 archive for the collector, the project services and the install record,
installed through its own `setup.sh --answers`.

Every value is read through a different path than the command that made it: the process table (which collector and
service run, from which tree, and whether the old one is gone), the collector's own `learning-status`, the snapshot of
every file and record count, Git, the account ledger (read-only sqlite) and the project history. The update's own screen
is read only for the words it says (is the refusal plain). The harness never stops, kills or signals a process: an update
that needs a hand stop must fail its check. The children are the fake clients; the installed CLI is the only CLI run."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_run import trusted_interpreter_checks  # noqa: E402
from e2e_core import CLAUDE_MODEL, ORCHESTRATION_VERSION, PROBE, _SCENES, Phase, check  # noqa: E402
import e2e_checks_recovery as recovery  # noqa: E402
import e2e_checks_shared as shared  # noqa: E402
from e2e_checks_setup import summary_states  # noqa: E402
from e2e_checks_shared import add_project, child_frames, project_records, project_view, write_patches  # noqa: E402

S5_IDS = ("L11.2", "L12.2", "L12.3", "L12.4", "L12.5", "G6")
# L12.2 is unchecked ON PURPOSE (decision 259: a check is green only through the real product path): the requirement's 0.2.x
# leftovers need a historical ledger that no supported product path plants (writing one into the product's ledger is refused).
UNCHECKED = {"L12.2": "0.2.x leftovers need a historical ledger: slice S4 / L12.6"}
CHECKED_IDS = tuple(i for i in S5_IDS if i not in UNCHECKED)
GROUP = "upd_all"  # G6 judges every update of the slice, so every check of the slice reads its scenes in one process
CHECK_NAMES = (
    "l12_3_update_with_a_running_child_goes_ahead_and_the_child_ends_lost",
    "l12_3_finished_result_stays_recordable_after_the_update",
    "l12_3_no_half_installed_file_at_any_step",
    "l12_4_update_with_uncommitted_managed_files_is_refused_and_changes_nothing",
    "l12_5_second_project_updated_after_the_first_works_and_the_first_still_works",
    "l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds",
    "g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build",
)

# The faults of this slice, chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in the
# process that runs the scenes); no environment variable selects it and World environments carry no E2E_ variable.
FAULT: str | None = None
FAULTS = {
    # The earlier collector refuses every handover, as if a child were always running: the update can only finish if the
    # owner stops the collector by hand. (G6's own red proof.)
    "update_handover_broken": [
        {"module": "codex_agents.takeover_owner",
         "old": "    def stop_collector(self, journal):\n",
         "new": "    def stop_collector(self, journal):\n        raise TakeoverError('collector_stop_failed')\n"}],
    # The forced failure also leaves a file the part-applied update did not report, so the installer's rollback cannot
    # put the project back as it was. (L11.2's own red proof.)
    "failed_update_leaves_files": [
        {"module": "claude_agents.packaging",
         "old": "        report = _operate(source_root, target, operation, apply, previous_source_root)\n",
         "new": "        if apply and operation == \"update\":\n"
                "            (target / \"e2e-left-by-the-failed-update.txt\").write_text(\"left\\n\")\n"
                "            _fail(\"e2e_forced_failure\")\n"
                "        report = _operate(source_root, target, operation, apply, previous_source_root)\n"}],
    # The forced failure also rewrites a NON-content column (`ingested_at_ms`) of every record in the project history stores,
    # leaving every record's content and the files untouched: a rollback that does not keep the history exactly. (L11.2's own
    # red proof for the history store, review t7s5-r2c.)
    "failed_update_alters_history": [
        {"module": "claude_agents.packaging",
         "old": "        report = _operate(source_root, target, operation, apply, previous_source_root)\n",
         "new": "        if apply and operation == \"update\":\n"
                "            import sqlite3 as _sqlite3\n"
                "            for _db in (Path.home() / \".local\" / \"state\" / \"codex-agents\" / \"projects\").glob(\"*/history.sqlite3\"):\n"
                "                _c = _sqlite3.connect(str(_db))\n"
                "                _c.execute(\"update records set ingested_at_ms = ingested_at_ms + 1\")\n"
                "                _c.commit()\n"
                "                _c.close()\n"
                "            _fail(\"e2e_forced_failure\")\n"
                "        report = _operate(source_root, target, operation, apply, previous_source_root)\n"}],
    # The update does not stop at uncommitted managed files. (L12.4's own red proof.)
    "update_ignores_uncommitted_files": [
        {"module": "setup_wizard.flow",
         "old": "        if dirty:\n            return \"Uncommitted changes exist in managed paths",
         "new": "        if False:\n            return \"Uncommitted changes exist in managed paths"}],
    # The hint removal for an earlier service of ANOTHER project than the last one (the second project's update) takes every
    # project's service hint with it, so the first project's service stops. (L12.5's own red proof.)
    "update_stops_other_projects": [
        {"module": "codex_agents.takeover_owner",
         "old": "            runtime_hint_path(self.state, self.project_root).unlink(missing_ok=True)\n",
         "new": "            runtime_hint_path(self.state, self.project_root).unlink(missing_ok=True)\n"
                "            _mark = runtime_hint_path(self.state, self.project_root).parent / 'e2e-withdrew-for'\n"
                "            if _mark.exists() and _mark.read_text() != str(self.project_root):\n"
                "                for _other in _mark.parent.glob('*.json'):\n"
                "                    _other.unlink(missing_ok=True)\n"
                "            _mark.write_text(str(self.project_root))\n"}]}
# What each fault must turn from green (normal run) to red (fault run), judged per ID on its own assertion, and the only
# other IDs it may turn red with it (named, never a prefix). An ID red in BOTH runs proves nothing and is not accepted.
# L12.3 has no fault: it is red in a normal run by a product bug (S5-STATUS.md finding 1), so a transition cannot be shown.
FAULT_TARGETS = {"update_handover_broken": ("G6",), "failed_update_leaves_files": ("L11.2",),
                 "failed_update_alters_history": ("L11.2",),
                 "update_ignores_uncommitted_files": ("L12.4",), "update_stops_other_projects": ("L12.5",)}
FAULT_ALSO_RED = {"update_handover_broken": ("G1", "G8", "L11.2", "L12.4", "L12.5"),  # their next updates need the handover too
                  "failed_update_leaves_files": (), "failed_update_alters_history": (), "update_ignores_uncommitted_files": (),
                  "update_stops_other_projects": ()}
# A forced failure part-way through an update (L11.2): not a fault of the product but the stimulus of that requirement. The
# Claude package's update step, which runs after the Codex package's has been applied, fails once.
FORCED_FAILURE = [
    {"module": "claude_agents.packaging",
     "old": "        report = _operate(source_root, target, operation, apply, previous_source_root)\n",
     "new": "        if apply and operation == \"update\":\n            _fail(\"e2e_forced_failure\")\n"
            "        report = _operate(source_root, target, operation, apply, previous_source_root)\n"}]


def fault_patches(*, forced: bool = False) -> list:
    """The patches a scene writes: the chosen fault's and, for the one update that is forced to fail, the forced failure
    (the failed-update fault REPLACES the plain forced failure: it is the same failure plus what it leaves behind)."""
    chosen = FAULTS[FAULT] if FAULT else []
    if FAULT in ("failed_update_leaves_files", "failed_update_alters_history"):  # only the forced update's own failure carries it
        return list(chosen) if forced else []
    return [*chosen, *(FORCED_FAILURE if forced else [])]


def registry_ids(flows=None) -> set:
    """Every requirement ID of the registry that a run of `flows` judges (every-flow requirements that have a check)."""
    items = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]
    return {item["id"] for item in items if flows is None or item["flow"] in flows or (item["flow"] == "all" and item["checks"])}


# Every fault requires a green-to-red transition, including busy updates.
RED_BY_PRODUCT_BUG_IN_NORMAL = ()
# The exact updates G6 judges (its reason codes are `g6_<name>_ok` / `g6_<name>_failed`) and the one update that is red in a
# normal run by the UPDFIX product bug (the retry after the refused update still needs a hand stop).
G6_UPDATES = ("first_update", "second_update", "running_child", "after_discarding_the_file", "after_the_forced_failure")
G6_KNOWN_BASELINE_FAILURE = "g6_retry_after_refusal_failed"


def g6_transitioned(was: dict, now: dict) -> bool:
    """G6 red in both runs counts as proved only if the normal run's ONLY failing update is the known product bug, every
    update the fault run fails is a named one, and at least one named update is `_ok` in the normal run and `_failed` in the
    fault run. Any other `_ok` to `_failed` pair (an unrelated name) is no proof."""
    before, after = set(was.get("reasons") or ()), set(now.get("reasons") or ())
    named_failed = {f"g6_{name}_failed" for name in G6_UPDATES}
    failed_before = {code for code in before if code.startswith("g6_") and code.endswith("_failed")}
    failed_after = {code for code in after if code.startswith("g6_") and code.endswith("_failed")}
    if failed_before != {G6_KNOWN_BASELINE_FAILURE} or not failed_after <= named_failed:
        return False
    return any(f"g6_{name}_ok" in before and f"g6_{name}_failed" in after for name in G6_UPDATES)


def fault_isolated(normal: dict, fault: dict, name: str = "update_handover_broken", expected_ids=None) -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID. Every targeted ID must be a real transition
    (green in the normal run, red in the fault run; red in both is no proof, except an ID red by a product bug, whose proof
    is an update `_ok` in the normal run and `_failed` in the fault run), no other ID may change except the named collateral
    ones, and both reports must hold the same IDs (a named collateral ID, such as G8, may show up in only one)."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    collateral = set(FAULT_ALSO_RED.get(name, ()))
    allowed = set(FAULT_TARGETS[name]) | collateral

    def transitioned(i):
        was, now = before.get(i) or {}, after.get(i) or {}
        if was.get("passed") is True and now.get("passed") is False:
            return True
        if i in RED_BY_PRODUCT_BUG_IN_NORMAL and was.get("passed") is False and now.get("passed") is False:
            return g6_transitioned(was, now)
        return False
    outside = [i for i in changed if i not in allowed]
    not_transitioned = sorted(i for i in FAULT_TARGETS[name] if not transitioned(i))
    wanted = set(registry_ids(normal.get("flows") or None) if expected_ids is None else expected_ids)
    missing = sorted(i for i in set(before) ^ set(after) if i not in collateral)
    missing += sorted(i for i in wanted if (i not in before or i not in after) and i not in collateral)
    return {"isolated": not outside and not not_transitioned and not missing, "changed": changed,
            "changed_outside_targets": outside, "targets_not_transitioned": not_transitioned, "ids_differ": missing}


def requirement_map(entries: list) -> dict:
    """The per-ID verdicts of a `--execute` worker's entries (an ID is green when every check naming it passed; its reason
    codes are kept), in the shape `fault_isolated` compares."""
    verdicts: dict = {}
    reasons: dict = {}
    for entry in entries:
        for requirement in entry.get("requirements") or []:
            verdicts[requirement] = verdicts.get(requirement, True) and bool(entry.get("passed"))
            reasons.setdefault(requirement, []).extend(entry.get("reasons") or [])
    return {"requirements": {i: {"passed": v, "reasons": reasons.get(i, [])} for i, v in verdicts.items()}}


# Every `refuses=True` of this module is one expected refusal with the reason its check asserts (G1: any other step that
# does not exit 0 fails the check that captured it, and G1 with it).
EXPECTED_REFUSALS = {
    "update_with_uncommitted_managed_files": "the update's project step: `managed_files_uncommitted ... Nothing was changed.`",
    "update_with_a_forced_failure_part_way": "the update's install step: the Claude package's update fails after the Codex package's was applied",
    "status_after_a_refused_update": "learning-status of a project whose files a refused update changed: it may refuse (collector and files differ)",
}


# ---------------------------------------------------------------------------------------------
# Pure judges (guarded without a World by tests/test_e2e_update_safety.py)

def previous_version(version: str) -> str:
    """The release before `version` (three numeric parts): the patch one lower, or the minor one lower at patch 99."""
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("a version is three numeric parts")
    major, minor, patch = (int(part) for part in parts)
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.99"
    raise ValueError("there is no release before this one")


def changed_anything(before: dict, after: dict) -> list[str]:
    """What differs between two snapshots, as fixed names: files (any digest, added or removed file), head, commits,
    dirty (the uncommitted listing itself), records (the collector's counts by kind), tables (the ledger's row counts)."""
    names = []
    if before.get("files") != after.get("files"):
        names.append("files")
    if before.get("head") != after.get("head"):
        names.append("head")
    if before.get("commits") != after.get("commits"):
        names.append("commits")
    if before.get("dirty") != after.get("dirty"):
        names.append("dirty")
    if before.get("records") != after.get("records"):
        names.append("records")
    if before.get("ledger") != after.get("ledger"):
        names.append("tables")
    return sorted(names)


def replaced(before: list, after: list, is_alive) -> bool:
    """One process was replaced by another: there was one before, exactly one runs now, it is another, and the old is gone."""
    return (len(before) == 1 and len(after) == 1 and after[0] != before[0] and not is_alive(before[0]))


def child_refusal_is_plain(text: str) -> bool:
    """The refusal of an update while a child runs says the reason in words, that nothing was changed and what to do."""
    flat = " ".join(text.split())
    return "a child run is active" in flat and "nothing was changed" in flat and "try again when none is running" in flat


def dirty_refusal_is_plain(text: str) -> bool:
    """The refusal of an update with uncommitted managed files names its fixed reason and says nothing was changed."""
    flat = " ".join(text.split())
    return "managed_files_uncommitted" in flat and "Nothing was changed." in flat


# ---------------------------------------------------------------------------------------------
# The earlier build, the update and what is read around it

def current_version() -> str:
    return ORCHESTRATION_VERSION.read_text().strip()


PREVIOUS_BUILD_MARK = "\n# the earlier build of the lifecycle end-to-end test (a runtime file of its own digest)\n"


PREDECESSOR_VERSION = '0.8.66'
PREDECESSOR_REVISION = 'afb6f610'
PREDECESSOR_ARCHIVE_SHA256 = '81b24a3ed530dabd306bbcb69fb794ec16e460ed7078e223a43be8af33a2e7a3'


def previous_clone(world) -> Path:
    """An unmodified, catalogue-listed archive, never relabelled current code."""
    import io
    import tarfile
    artifact = ROOT / 'tests/fixtures/b4t/0.8.66.tar.gz'
    try:
        raw = artifact.read_bytes()
    except OSError:
        raise e2e_support.HarnessError('vendored 0.8.66 predecessor artifact is missing') from None
    if hashlib.sha256(raw).hexdigest() != PREDECESSOR_ARCHIVE_SHA256:
        raise e2e_support.HarnessError('vendored 0.8.66 predecessor artifact digest mismatch')
    clone = world.base / 'previous-build'
    clone.mkdir()
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        archive.extractall(clone, filter='data')
    return clone


def set_up_previous(view, clone: Path) -> None:
    """Scripted setup of the view's project by the EARLIER build, then the provider observations the Claude admission rests on."""
    answers = view.answers(providers={"codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL, "bridge": "off"})
    done = view.terminal(["/bin/bash", str(clone / "setup.sh"), "--answers", str(answers)], label="setup_answers")
    if done.code != 0:
        raise e2e_support.HarnessError("the scripted setup of the earlier build did not finish")
    view.claude_frames(e2e_support.claude_probe_frames())
    view.cli("provider-observe", "--provider", "codex")
    view.cli("provider-observe", "--provider", "claude")
    view.host_cli("claude", *PROBE)


class Ctl(recovery.Ctl):
    """The recovery slice's primary-session driver (a dispatch may take 120 s)."""


def listing(view, script: str, project: Path | None = None) -> list[tuple[int, str]]:
    """The running `script` processes started inside this World as (pid, command): the command names the tree it runs from."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                         env=e2e_support.child_environment(os.environ)).stdout
    flag = f"--project-root {project}" if project is not None else ""
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if (script in command and str(view.base) in command and int(pid) != os.getpid()
                and (project is None or command.endswith(flag) or flag + " " in command)):
            found.append((int(pid), command))
    return sorted(found)


def runs_from(entries: list[tuple[int, str]], tree: Path) -> bool:
    """Every listed process was started from `tree` (the clone whose runtime it is)."""
    return bool(entries) and all(str(tree) + "/" in command for _pid, command in entries)


def observe(view, *, ready: bool = False) -> SimpleNamespace:
    """The processes and the collector's own report, right now. `ready` says the status must answer (otherwise it may refuse)."""
    code, status, _ = view.cli("learning-status", refuses=not ready)
    collector = listing(view, "collector_host.py")
    service = listing(view, "project_host.py", view.project)
    status = status or {}
    return SimpleNamespace(collector=collector, service=service, pids=[pid for pid, _ in collector],
                           service_pids=[pid for pid, _ in service], status_code=code, ready=status.get("ready"),
                           version=status.get("collector_runtime_version"), stale=status.get("collector_code_stale"),
                           earlier=status.get("earlier_version_runs"))


def take(view) -> dict:
    """World.snapshot with the collector's status allowed to refuse (a project whose files and collector differ may refuse it)."""
    original = view.cli

    def tolerant(*args, **kwargs):
        if args and args[0] == "learning-status":
            kwargs["refuses"] = True
        return original(*args, **kwargs)
    view.cli = tolerant
    try:
        return view.snapshot()
    finally:
        del view.cli


def update_run(view, *, refuses: bool = False):
    """`./setup.sh --update PROJECT` of this checkout (World.update, with the expected refusal marked for G1)."""
    return view.terminal(["/bin/bash", str(e2e_support.SETUP), "--update", str(view.project)], label="setup_update",
                         refuses=refuses)


def do_update(view, *, refuses: bool = False) -> SimpleNamespace:
    """One update with what was read right before and right after it, by paths other than the update's own screen."""
    update = SimpleNamespace(refuses=refuses)
    update.snapshot_before, update.before = take(view), observe(view)
    update.alive_before = {pid: recovery.alive(pid) for pid in update.before.pids}
    update.steps_before = len(view.steps)
    update.run = update_run(view, refuses=refuses)
    update.steps = [step["step"] for step in view.steps[update.steps_before:]]
    update.alive_after = {pid: recovery.alive(pid) for pid in update.before.pids}  # right now, not when a check runs
    update.after = observe(view)
    update.snapshot_after = take(view)
    return update


def states_of(update) -> dict:
    return {number: state for number, (state, *_rest) in summary_states(update.run.text).items()}


def arm_gated(ctl, run, gate: Path) -> None:
    """The fake client holds the child until `gate` exists, then answers this run correctly (a child that really runs)."""
    from e2e_checks_run import child_answer
    template = (ctl.host("result-preflight", *run.common)[1] or {}).get("template")
    if template is None:
        raise e2e_support.HarnessError("the result preflight gave no template")
    answer = child_answer(template, False)
    child_frames(ctl.view, {"wait_for": str(gate)}, {**recovery.RESULT_FRAME, "result": json.dumps(answer), "structured_output": answer})


def record_after_refusal(ctl, run) -> tuple:
    """Record the child's result as the primary does, after the refused update: it may be refused (marked for G1; the
    check that judges it asserts the exit)."""
    result = (run.reply or {}).get("result")
    if result is None:
        return (None, None, "")
    terminal = ((run.reply or {}).get("bridge_report") or {}).get("terminal_state")
    observation = {"run_handle": (run.opened or {}).get("run_handle"),
                   "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]}
    if terminal:
        observation["terminal_state"] = terminal
    return ctl.host("learning-record-host-result", *run.common, "--result", ctl.save(run.name + "-result", result),
                    "--observation", ctl.save(run.name + "-observation", observation), refuses=True)


def settled(world, timeout: float = 30) -> list:
    """The ledger's capacity table once every slot is released (the release follows a child's end by a moment)."""
    deadline = time.monotonic() + timeout
    while True:
        rows = shared.ledger_slots(world)
        if all(row["released_at_ms"] is not None for row in rows) or time.monotonic() >= deadline:
            return rows
        time.sleep(0.3)


# The one column of the history store that a LIVE service moves on its own, with no update or command involved: the project
# service advances `result_identity.last_observed_at_ms` (the store's trusted wall-clock floor) about every ten seconds
# (measured in a World: three reads, 0 s / 8 s / after the update, 10 s apart, no command in between). It is never compared
# for equality; it must only never go back (a rollback that restored an older value would resurrect expired results).
LIVE_CLOCK_COLUMNS = {"result_identity": ("last_observed_at_ms",)}


def _cell(value) -> list:
    """One stored value as fixed, comparable text (a blob as its hex), tagged by its type."""
    return [type(value).__name__, value.hex() if isinstance(value, bytes) else str(value)]


def history_dump(world, project_id) -> dict:
    """The project history's own store, read-only and by its own path (never through a command): EVERY column of EVERY row of
    EVERY table (sha256 of the row's whole contents; a record is keyed `type:sequence`, another table's row `table:rowid`),
    the schema's own text, and SQLite's own integrity verdict. A store that cannot be read says so."""
    path = shared.project_history(world, project_id)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in connection.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
            schema = [list(row) for row in connection.execute("select type, name, sql from sqlite_master order by type, name")]
            rows: dict = {}
            floors: dict = {}
            for table in tables:
                columns = [row[1] for row in connection.execute(f'pragma table_info("{table}")')]
                try:
                    cursor = connection.execute(f'select rowid, * from "{table}" order by rowid')
                except sqlite3.Error:  # a table without a rowid: numbered by its sorted rows
                    cursor = connection.execute(f'select null, * from "{table}" order by 2')
                for number, row in enumerate(cursor):
                    cells = list(row[1:])
                    named = dict(zip(columns, cells))
                    for column in LIVE_CLOCK_COLUMNS.get(table, ()):
                        floors[f"{table}:{row[0] if row[0] is not None else number}:{column}"] = named.pop(column)
                    cells = [value for column, value in zip(columns, cells) if column in named]
                    key = (f"{named['record_type']}:{named['source_sequence']}" if table == "records"
                           else f"{table}:{row[0] if row[0] is not None else number}")
                    kept = [column for column in columns if column in named]
                    rows[key] = hashlib.sha256(json.dumps([kept, [_cell(cell) for cell in cells]]).encode()).hexdigest()
            return {"rows": rows, "floors": floors, "schema": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
                    "integrity": connection.execute("pragma integrity_check").fetchone()[0]}
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return {"rows": {}, "integrity": "unreadable"}


def history_kept(before: dict, after: dict, *, exact: bool) -> bool:
    """The project history still holds what it held: some row before, SQLite says `ok` both times, and every earlier
    row is there with every column the same except the live clock floor, which may only have advanced (`exact`: nothing else
    either, as right after a failed update)."""
    rows_before, rows_after = before.get("rows") or {}, after.get("rows") or {}
    if not rows_before or before.get("integrity") != "ok" or after.get("integrity") != "ok":
        return False
    floors_before, floors_after = before.get("floors") or {}, after.get("floors") or {}
    if not all(isinstance(floors_after.get(key), int) and floors_after[key] >= value for key, value in floors_before.items()):
        return False  # the trusted clock floor went back or its row is gone
    if exact:
        return rows_before == rows_after and before.get("schema") == after.get("schema")
    return all(rows_after.get(key) == value for key, value in rows_before.items())


# Pinned independently from the released 22 layout and accepted unreleased 23
# DDL. Exact hashes include every table/index/trigger and SQLite autoindex.
EXPECTED_PROJECT_SCHEMA_22 = 'a4a6bb95c99e1c30520e00649a882f7ec7743346b2450c2fce37b1fe4588069c'
EXPECTED_PROJECT_SCHEMA_23 = '559e4a289190d4f9462c680d7739330c731c217b3d6cfe2204619321a99c61af'


def handover_history_kept(before: dict, after: dict) -> bool:
    """The authorized hand-over changes the writer epoch, never an original row.

    New additive authority rows are allowed; all pre-existing record columns,
    holds, bindings and receipts retain the same comparisons as before.
    """
    def original_rows(snapshot):
        return {**snapshot, 'rows': {key: value for key, value in snapshot.get('rows', {}).items()
                                    if not key.startswith('runtime_takeover:')}}
    epoch_keys = lambda value: {key for key in value.get('rows', {}) if key.startswith('runtime_takeover:')}
    if epoch_keys(before) != epoch_keys(after):
        return False
    left, right = original_rows(before), original_rows(after)
    allowed = ('takeover_record:', 'takeover_runs:', 'takeover_obligations:', 'runtime_floor:')
    right['rows'] = {key: value for key, value in right['rows'].items()
                     if key in left['rows'] or not key.startswith(allowed)}
    # Schema23 is pinned independently from a fresh store, never accepted from
    # the candidate's own dump. An already-current failed update is byte-exact.
    expected = EXPECTED_PROJECT_SCHEMA_23
    if (before.get('schema') not in (EXPECTED_PROJECT_SCHEMA_22, EXPECTED_PROJECT_SCHEMA_23)
            or after.get('schema') != expected):
        return False
    left['schema'] = expected
    return history_kept(left, right, exact=True)


def history_differences(before: dict, after: dict, *, earlier_only: bool = False) -> list[str]:
    """Which parts of the history store differ, as fixed names (`records:<type>` for a record, the table's name for another
    table's row, `schema`), for the report's reason codes."""
    rows_before, rows_after = before.get("rows") or {}, after.get("rows") or {}
    names = {key.split(":")[0] if key.split(":")[0] in ("history_identity", "terminal_outbox", "result_identity",
                                                         "result_reservations", "project_holds", "project_orchestrations") else "records:" + key.split(":")[0]
             for key in (set(rows_before) if earlier_only else set(rows_before) | set(rows_after))
             if rows_before.get(key) != rows_after.get(key)}
    if before.get("schema") != after.get("schema"):
        names.add("schema")
    return sorted(names)


def quiet(view, seconds: float = 90) -> dict:
    """The project's snapshot once nothing is changing any more: a child that just ended is still being settled by the
    services (its per-run working files, `.context-*` and `.result-*`, are removed some seconds after it ends; its
    records are counted), and a snapshot taken before that would make an update look as if it changed them. Waits until
    no such working file is left and two consecutive snapshots are equal; the last one either way after `seconds`."""
    deadline = time.monotonic() + seconds
    last = take(view)
    while time.monotonic() < deadline:
        time.sleep(3.0)
        current = take(view)
        working = [name for name in current["files"] if "/.context-" in name or "/.result-" in name]
        if current == last and not working:
            return current
        last = current
    return last


def run_a_child(view, tag: str) -> SimpleNamespace:
    """A fresh orchestration on the project and one child run to the end (completed), recorded as the primary does."""
    ctl = Ctl(view, tag)
    view.claude_frames(e2e_support.claude_probe_frames())
    run = ctl.child()
    recovery.arm(ctl, run, 0)
    ctl.dispatch(run)
    recovery.record(ctl, run)
    work = SimpleNamespace(ctl=ctl, run=run, project_id=ctl.project_id)
    work.read = recovery.wait_released(view, run, seconds=40)
    work.outcome = recovery.persisted_outcome(view, run, ctl.project_id)  # the project history's own outcome record
    return work


def works(phase: Phase, view, work: SimpleNamespace, what: str) -> None:
    """A child ran to the end on this project: its dispatch answered, its history holds it completed, its slot is released."""
    run = work.run
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", f"{what}: a run opens admitted")
    phase.expect(run.code == 0 and ((run.reply or {}).get("result") or {}).get("execution_state") == "completed",
                 f"{what}: the child runs and answers")
    phase.expect(work.outcome == "completed", f"{what}: the project history holds the run's outcome as completed")
    phase.expect(work.read.get("state") == "released", f"{what}: the account's own per-run read says the slot is released")


# ---------------------------------------------------------------------------------------------
# Scene 1: two projects on the earlier build, updated one after the other (L12.2, L12.5, G6)

def main_scene():
    if "upd_main" in _SCENES:
        return _SCENES["upd_main"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["upd_main"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    scene.clone = previous_clone(world)
    first, second = project_view(world), add_project(world, "project-b")
    scene.first, scene.second = first, second
    for view in (first, second):  # every setup first: a later setup restarts the shared collector
        set_up_previous(view, scene.clone)
    scene.start = {"first": observe(first, ready=True), "second": observe(second, ready=True)}
    ctl = scene.ctl = Ctl(first, "a")
    ctl.host("provider-observe", "--provider", "claude", "--billing-probe")
    scene.admitted = ctl.child()  # opened and admitted: never dispatched, so never launched
    scene.admitted_ledger = world.ledger_counts()
    scene.u1 = do_update(first)
    scene.admitted_ledger_after = world.ledger_counts()
    scene.u2 = do_update(second)
    scene.after_u2 = observe(first, ready=True)
    scene.work = {"first": run_a_child(first, "a2"), "second": run_a_child(second, "b2")}
    return scene


@check("L12.3", group=GROUP)
def l12_3_update_with_a_running_child_goes_ahead_and_the_child_ends_lost(phase: Phase) -> None:
    scene = child_scene()
    update, run = scene.updated, scene.run
    phase.expect(run.open_code == 0 and (run.opened or {}).get('admission') == 'admitted', 'the run opens admitted')
    phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup'
                 and scene.running.get('stop_requested') is False, 'the child really runs before the update')
    phase.expect(update.steps == ['setup_update'], 'the busy update is one command')
    phase.expect(not update.run.timed_out and update.run.code == 0 and 'FAIL' not in states_of(update).values(),
                 'the busy update goes ahead without an idle wait or manual stop')
    phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive)
                 and update.after.ready is True and update.after.version == current_version(),
                 'the old collector is gone and the successor is healthy')
    phase.expect(scene.child_before and all(not alive for _, alive in scene.child_after),
                 'the updated project service ends its old child')
    phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost',
                 'the new service and the stored binding independently say the old run is lost')
    phase.expect(scene.slots and all(row['released_at_ms'] is not None for row in scene.slots),
                 'B4s confirms the lost slot release')
    phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text,
                 'status names the hand-over and the ended run')
    phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')


@check("L12.3", group=GROUP)
def l12_3_finished_result_stays_recordable_after_the_update(phase: Phase) -> None:
    scene = child_scene()
    phase.expect(scene.finished.run.code == 0 and scene.finished.outcome == 'completed',
                 'a genuine finished result was committed on the predecessor')
    phase.expect(scene.recorded[0] == 0 and scene.rechecked[0] == 0,
                 'the primary can record and recheck that result on the successor')
    before, after = scene.finished_history_before['rows'], scene.finished_history_after['rows']
    kept = {name: value for name, value in before.items() if name.startswith(
        ('outcome:', 'dispatch_result:', 'host_result_admission:'))}
    phase.expect(bool(kept) and all(after.get(name) == value for name, value in kept.items()),
                 'all original outcome, result and admission record bytes are retained')
    phase.expect(scene.finished_history_after['integrity'] == 'ok', 'the final store is sound')
    phase.expect(scene.finished_admissions_before == scene.finished_admissions_after,
                 'record/recheck creates no second admission for the committed result')


@check("L12.3", group=GROUP)
def l12_3_no_half_installed_file_at_any_step(phase: Phase) -> None:
    scene = child_scene()
    phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')
    phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')
    names = set(scene.managed_before) | set(scene.managed_after)
    bad = {name for sample in scene.managed_samples for name in names
           if sample.get(name) not in (scene.managed_before.get(name), scene.managed_after.get(name))}
    phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')
    phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True,
                 'rerunning the completed update remains healthy')


@check("L12.4", group=GROUP)
def l12_4_update_with_uncommitted_managed_files_is_refused_and_changes_nothing(phase: Phase) -> None:
    scene = dirty_scene()
    update = scene.refused
    phase.expect(scene.dirty_listing.strip().startswith("M ") or scene.dirty_listing.startswith(" M "),
                 "a managed file of the project has an uncommitted change before the update")
    phase.expect(update.steps == ["setup_update"], "the update was one command")
    phase.expect(not update.run.timed_out and update.run.code != 0, "the update is refused (it exits non-zero)")
    phase.expect(dirty_refusal_is_plain(update.run.text),
                 "the refusal names managed_files_uncommitted and says nothing was changed")
    phase.reason("managed_files_uncommitted")
    changed = changed_anything(update.snapshot_before, update.snapshot_after)
    for name in changed:
        phase.reason("changed_" + name)
    phase.expect(not changed, "the refusal changed no file, commit, record or uncommitted listing")
    phase.expect(scene.edit_after == scene.edit_before, "the uncommitted edit itself is exactly as it was (never overwritten)")
    phase.expect(update.before.pids == update.after.pids and update.before.service_pids == update.after.service_pids
                 and runs_from(update.after.collector, scene.clone),
                 "the collector and the project's service are the same live processes, still on the earlier build")
    final = scene.final
    phase.expect(scene.dirty_after_discard == "", "once the edit is discarded the project is clean")
    phase.expect(final.run.code == 0 and "FAIL" not in states_of(final).values(),
                 "the same update then goes through (the refusal was about the uncommitted file)")
    phase.expect(final.after.version == current_version() and final.after.ready is True
                 and replaced(final.before.pids, final.after.pids, recovery.alive),
                 "and replaces the earlier collector by itself, ready on this build")


@check("L12.5", group=GROUP)
def l12_5_second_project_updated_after_the_first_works_and_the_first_still_works(phase: Phase) -> None:
    scene = main_scene()
    first, second = scene.u1, scene.u2
    phase.expect(first.run.code == 0 and not first.run.timed_out, "the first project's update exits 0")
    phase.expect(scene.start["second"].version == PREDECESSOR_VERSION
                 and runs_from(scene.start["second"].service, scene.clone),
                 "the second project was installed by the earlier build and served by its service")
    phase.expect(second.steps == ["setup_update"], "the second update was one command")
    phase.expect(not second.run.timed_out and second.run.code == 0, "the second project's update exits 0")
    phase.expect("FAIL" not in states_of(second).values() and "stopped working" not in " ".join(second.run.text.split()),
                 "no step fails and the update never says another project stopped working")
    phase.expect(second.after.ready is True and second.after.version == current_version()
                 and runs_from(second.after.service, ROOT)
                 and not set(second.after.service_pids) & set(second.before.service_pids),
                 "the second project's service was replaced and its collector reports this build, ready")
    phase.expect(len(second.after.pids) == 1 and runs_from(second.after.collector, ROOT),
                 "after the second update exactly one collector runs, from this build")
    phase.expect(scene.after_u2.ready is True and scene.after_u2.service_pids == first.after.service_pids
                 and scene.after_u2.version == current_version(),
                 "the first project's service is the same live process after the second update and still reports ready")
    works(phase, scene.first, scene.work["first"], "the first project")
    works(phase, scene.second, scene.work["second"], "the second project")


@check("L11.2", group=GROUP)
def l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds(phase: Phase) -> None:
    """Decision 260: the requirement names the installer's automatic rollback of a failed update. An update forced to fail
    part-way, after the Codex package's step was applied, is undone by that rollback: files and Git as before, the collector's
    records, the ledger and the project's own history store (recorded child history seeded through the real product before the
    failure) unchanged, and the next attempt succeeds with that history still readable."""
    scene = fail_scene()
    failed = scene.failed
    seed = scene.seed
    phase.expect(seed.run.open_code == 0 and seed.run.code == 0 and seed.outcome == "completed"
                 and scene.seed_closed[0] == 0 and scene.probe_closed[0] == 0,
                 "before the forced failure a child really ran through the installed CLI, was recorded and its orchestration closed")
    phase.expect(len(scene.history_before["rows"]) > 0 and scene.history_before["integrity"] == "ok"
                 and any(key.startswith("outcome:") for key in scene.history_before["rows"]),
                 "the project history store holds recorded child history (an outcome among its records), SQLite integrity ok")
    phase.expect(failed.steps == ["setup_update"], "the failing update was one command")
    phase.expect(not failed.run.timed_out and failed.run.code != 0, "the forced failure stops the update (it exits non-zero)")
    states = states_of(failed)
    phase.expect(states.get(4) == "FAIL" and states.get(1) == "OK" and states.get(2) == "OK" and states.get(3) == "OK",
                 "it stopped part-way, at the install step, after the checks, project and providers steps passed")
    phase.reason("update_failed_part_way")
    changed = changed_anything(failed.snapshot_before, failed.snapshot_after)
    for name in changed:
        phase.reason("changed_" + name)
    phase.expect("files" not in changed and "head" not in changed and "commits" not in changed and "dirty" not in changed,
                 "after the failure every file and the project's Git are exactly as before (the part applied was undone)")
    phase.expect(scene.touched_after != scene.touched_before,
                 "the Codex step really rewrote its file before the failure (it was written, then put back)")
    phase.expect("records" not in changed and all(failed.snapshot_after['ledger'].get(table) == count
                 for table, count in failed.snapshot_before['ledger'].items()
                 if table not in {'runtime_floor', 'runtime_lineage'}),
                 "the collector's records and original table row counts are unchanged; additive authority is journaled")
    for name in history_differences(scene.history_before, scene.history_failed):
        phase.reason("history_changed_" + name.replace(":", "_"))
    phase.expect(handover_history_kept(scene.history_before, scene.history_failed),
                 "every original project row is unchanged except the hand-over epoch; SQLite integrity ok")
    phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT)
                 and replaced(failed.before.pids, failed.after.pids, recovery.alive)
                 and not any(recovery.alive(pid) for pid in failed.before.service_pids),
                 "account_done names the completed hand-over; the new collector owns the records and the old service exited")
    phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text
                 and 'nothing was changed' not in failed.run.text,
                 "the failure reports the completed hand-over and names its resumable next step")
    nxt = scene.nxt
    phase.expect(nxt.run.code == 0 and "FAIL" not in states_of(nxt).values(),
                 "the next attempt (nothing forced) goes through")
    phase.expect(nxt.after.ready is True and nxt.after.version == current_version()
                 and nxt.before.pids == nxt.after.pids,
                 "and keeps the handed-over collector, ready on this build")
    phase.expect(nxt.snapshot_after["commits"] != nxt.snapshot_before["commits"] and nxt.snapshot_after["dirty"] == "",
                 "the next attempt installed and committed the update the failed one had undone")
    for name in history_differences(scene.history_before, scene.history_next, earlier_only=True):
        phase.reason("history_changed_after_next_" + name.replace(":", "_"))
    phase.expect(handover_history_kept(scene.history_before, scene.history_next),
                 "after the next successful update every earlier history record is still in the store, unchanged, integrity ok")
    phase.expect(scene.outcome_next == "completed" and scene.read_next.get("state") == "released",
                 "the seeded child's history is still readable after the next update (its outcome record and the account's per-run read)")


def update_problems(update, *, replaces_earlier: bool) -> list[str]:
    """What is wrong with one update of the slice that must succeed (empty: nothing). Fixed names only: exit 0 by itself (the
    harness stops nothing), the one command, no failing step, and the project ends with exactly one collector, from this
    build, ready and not stale (the earlier one alive right before and gone after, where one ran)."""
    found = []
    if update.steps != ["setup_update"]:
        found.append("not_the_only_command")
    if update.run.timed_out or update.run.code != 0:
        found.append("needed_a_hand_stop_or_failed")
    if "FAIL" in states_of(update).values():
        found.append("a_step_failed")
    if not (update.after.ready is True and update.after.version == current_version() and update.after.stale is False):
        found.append("collector_not_ready_on_this_build")
    if not (len(update.after.pids) == 1 and runs_from(update.after.collector, ROOT)):
        found.append("not_exactly_one_collector_of_this_build")
    if replaces_earlier and not (all(update.alive_before.values()) and update.alive_before
                                 and replaced(update.before.pids, update.after.pids, recovery.alive)):
        found.append("earlier_collector_not_replaced_by_the_update")
    return found


@check("G6", group=GROUP)
def g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build(phase: Phase) -> None:
    """Every update of the slice that is expected to succeed, judged alike: the two updates of the main scene (the first
    with an admitted, never launched run), the update with a running child, the update after the
    uncommitted file was discarded and the attempt after the forced failure. The refusals themselves are L12.4 and
    L11.2's; what G6 says is that no update, and no retry after a refusal, ever needs the collector stopped by hand. Each
    update's verdict is a reason code (`g6_<update>_ok` or `_failed`), so a report shows which of them it was."""
    main, child, dirty, fail = main_scene(), child_scene(), dirty_scene(), fail_scene()
    run = main.admitted
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted" and run.thread is None and run.code is None
                 and (main.admitted_ledger.get("host_run_bindings") or 0) >= 1,
                 "before the first update a run was opened and admitted, never dispatched, its binding in the ledger")
    start = main.start["first"]
    phase.expect(len(start.pids) == 1 and runs_from(start.collector, main.clone) and runs_from(start.service, main.clone)
                 and start.version == PREDECESSOR_VERSION and start.ready is True,
                 "before the first update the collector and the project's service run from the earlier build, ready")
    second_before = main.u2.alive_before
    second_extra = [] if (all(second_before.values()) and second_before and main.after_u2.ready is True
                          and main.after_u2.pids == main.u2.after.pids) else ["collector_not_the_one_started_before"]
    first_extra = [] if (sorted(states_of(main.u1)) == list(range(1, 10))
                         and (main.u1.after.earlier or {}).get("holding") == 0) else ["summary_or_earlier_runs_not_clean"]
    judged = (("first_update", update_problems(main.u1, replaces_earlier=True) + first_extra),
              ("second_update", update_problems(main.u2, replaces_earlier=False) + second_extra),
              ("running_child", update_problems(child.updated, replaces_earlier=True)),
              ("after_discarding_the_file", update_problems(dirty.final, replaces_earlier=True)),
              ("after_the_forced_failure", update_problems(fail.nxt, replaces_earlier=False)))
    for tag, problems in judged:
        phase.reason(f"g6_{tag}_{'failed' if problems else 'ok'}")
    bad = [tag for tag, problems in judged if problems]
    phase.expect(not bad, "every update of the slice goes through by itself and ends healthy on this build: failing " + ", ".join(bad))


# ---------------------------------------------------------------------------------------------
# Scene 2: a child really running when the update is run (L12.3)

def managed_snapshot(project):
    """Read complete managed-file digests independently of the installer."""
    result = {}
    for directory in ('.codex', '.agents', '.claude'):
        for path in (project / directory).rglob('*'):
            try:
                if path.is_file() and not path.is_symlink():
                    result[str(path.relative_to(project))] = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                pass  # an atomic replacement is observed on the following sample
    return result


def child_scene():
    if 'upd_child' in _SCENES:
        return _SCENES['upd_child']
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES['upd_child'] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    scene.clone = previous_clone(world)
    set_up_previous(world, scene.clone)
    scene.finished = run_a_child(world, 'finished-before-update')
    scene.finished_history_before = history_dump(world, scene.finished.project_id)
    scene.finished_admissions_before = recovery.count_by_type(world, scene.finished.project_id).get('host_result_admission', 0)
    ctl = scene.ctl = Ctl(world, 'c')
    ctl.host('provider-observe', '--provider', 'claude', '--billing-probe')
    gate = world.tools / 'update-gate'
    run = scene.run = ctl.child()
    arm_gated(ctl, run, gate)
    ctl.dispatch_in_background(run, refuses=True)  # the update ending this dispatch is the explicit stimulus
    scene.running = ctl.wait_running(run)
    scene.child_before = [pid for pid, _ in listing(world, 'fake_child.py')]
    scene.managed_before = managed_snapshot(world.project)
    scene.managed_samples = [scene.managed_before]
    done = threading.Event()
    def observe_files():
        while not done.wait(0.05):
            scene.managed_samples.append(managed_snapshot(world.project))
    observer = threading.Thread(target=observe_files, daemon=True)
    observer.start()
    try:
        scene.updated = do_update(world)
    finally:
        done.set()
        observer.join(5)
    scene.monitor_finished = not observer.is_alive()
    scene.managed_after = managed_snapshot(world.project)
    scene.managed_samples.append(scene.managed_after)
    scene.child_after = [(pid, recovery.alive(pid)) for pid in scene.child_before]
    ctl.finish(run)
    scene.dispatch_code = run.code
    scene.lost_status = ctl.status(run, refuses=True)
    scene.terminal = recovery.persisted_terminal(recovery.run_binding(world, run))
    scene.slots = settled(world)
    _, status, _ = world.cli('learning-status')
    scene.status_text = json.dumps(status)
    scene.recorded = record_after_refusal(scene.finished.ctl, scene.finished.run)
    scene.rechecked = record_after_refusal(scene.finished.ctl, scene.finished.run)
    scene.finished_history_after = history_dump(world, scene.finished.project_id)
    scene.finished_admissions_after = recovery.count_by_type(world, scene.finished.project_id).get('host_result_admission', 0)
    scene.final = do_update(world)
    return scene


# ---------------------------------------------------------------------------------------------
# Scene 3: a managed file with an uncommitted change (L12.4)

EDITED = ".agents/skills/plan-change/SKILL.md"


def dirty_scene():
    if "upd_dirty" in _SCENES:
        return _SCENES["upd_dirty"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["upd_dirty"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    scene.clone = previous_clone(world)
    set_up_previous(world, scene.clone)
    path = world.project / EDITED
    path.write_text(path.read_text() + "\nA local note the update must not overwrite.\n")
    scene.edit_before = hashlib.sha256(path.read_bytes()).hexdigest()
    scene.dirty_listing = world.git("status", "--porcelain").stdout
    scene.refused = do_update(world, refuses=True)
    scene.edit_after = hashlib.sha256(path.read_bytes()).hexdigest()
    world.git("checkout", "--", EDITED)
    scene.dirty_after_discard = world.git("status", "--porcelain").stdout
    scene.final = do_update(world)
    return scene


# ---------------------------------------------------------------------------------------------
# Scene 4: an update forced to fail part-way (L11.2)

def fail_scene():
    if "upd_fail" in _SCENES:
        return _SCENES["upd_fail"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["upd_fail"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    scene.clone = previous_clone(world)
    set_up_previous(world, scene.clone)
    # Real recorded child history before the forced failure: one child run to its end through the installed CLI of the
    # earlier build, recorded as the primary does, its orchestration closed; then the history store's own contents.
    seed = Ctl(world, "f0")
    seed.host("provider-observe", "--provider", "claude", "--billing-probe")
    # The probe's own orchestration is closed too: a hold the earlier build leaves open is legitimately released by the next
    # update (the product's own work, not a rollback), so it would move rows of the store the next comparison reads.
    scene.probe_closed = seed.host("learning-close-orchestration", "--orchestration-handle", seed.handle, "--status", "completed")
    scene.seed = run_a_child(world, "f1")
    scene.seed_closed = scene.seed.ctl.host("learning-close-orchestration", "--orchestration-handle", scene.seed.ctl.handle,
                                           "--status", "completed")
    quiet(world)  # the seeded child is settled before the part-way failure is measured against it
    scene.history_before = history_dump(world, scene.seed.project_id)
    scene.touched = world.project / ".codex" / "orchestration" / "codex_agents" / "__init__.py"  # the file the Codex step updates
    scene.touched_before = scene.touched.stat().st_mtime_ns
    write_patches(world, fault_patches(forced=True))
    scene.failed = do_update(world, refuses=True)
    scene.pending = json.loads((shared.state_dir(world) / 'update-takeover.json').read_bytes())
    scene.touched_after = scene.touched.stat().st_mtime_ns
    scene.history_failed = history_dump(world, scene.seed.project_id)
    write_patches(world, fault_patches())
    scene.nxt = do_update(world)
    scene.history_next = history_dump(world, scene.seed.project_id)
    scene.read_next = recovery.capacity_read(world, scene.seed.run)  # the product's own per-run read of the seeded run
    scene.outcome_next = recovery.persisted_outcome(world, scene.seed.run, scene.seed.project_id)
    return scene
