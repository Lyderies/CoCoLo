"""L11.2, L12.2 to L12.5 and G6: update safety and rollback (T7 slice S5).

The update is `./setup.sh --update PROJECT` of this checkout, run on a project installed by an EARLIER build. The earlier
build is a copy of this source tree (its tracked files, so a shallow clone of the repository is enough) whose VERSION is
the release before the current one: a real other version for the collector, the project services and the install record,
installed through its own `setup.sh --answers`. It is NOT older code (S4 builds history on real earlier tags).

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
import subprocess
import sys
import time
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
CHECK_NAMES = (
    "l12_2_one_update_with_an_admitted_never_launched_run_needs_no_manual_stop",
    "l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on",
    "l12_3_refused_update_with_a_running_child_changes_nothing",
    "l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended",
    "l12_4_update_with_uncommitted_managed_files_is_refused_and_changes_nothing",
    "l12_5_second_project_updated_after_the_first_works_and_the_first_still_works",
    "l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds",
    "g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build",
)

# The fault of this slice, chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in the
# process that runs the scenes); no environment variable selects it and World environments carry no E2E_ variable.
FAULT: str | None = None
FAULTS = {
    # The earlier collector refuses every handover, as if a child were always running: the update can only finish if the
    # owner stops the collector by hand.
    "update_handover_broken": [
        {"module": "codex_agents.learning_store",
         "old": "                # earlier version left never ran under a collector, so they do not count.\n"
                "                if not self.ledger.drained(children_only=True):\n",
         "new": "                # earlier version left never ran under a collector, so they do not count.\n"
                "                if True:\n"}]}
# Every S5 ID the fault must turn red; nothing else may change except G1 (every update that is not an expected refusal
# must exit 0, and the broken handover makes those exit 2).
FAULT_TARGETS = {"update_handover_broken": S5_IDS}
FAULT_ALSO_RED = {"update_handover_broken": ("G1",)}
# A forced failure part-way through an update (L11.2): not a fault of the product but the stimulus of that requirement. The
# Claude package's update step, which runs after the Codex package's has been applied, fails once.
FORCED_FAILURE = [
    {"module": "claude_agents.packaging",
     "old": "        report = _operate(source_root, target, operation, apply, previous_source_root)\n",
     "new": "        if apply and operation == \"update\":\n            _fail(\"e2e_forced_failure\")\n"
            "        report = _operate(source_root, target, operation, apply, previous_source_root)\n"}]


def fault_patches(*, forced: bool = False) -> list:
    return [*(FAULTS[FAULT] if FAULT else []), *(FORCED_FAILURE if forced else [])]


def registry_ids(flows=None) -> set:
    """Every requirement ID of the registry that a run of `flows` judges (every-flow requirements that have a check)."""
    items = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]
    return {item["id"] for item in items if flows is None or item["flow"] in flows or (item["flow"] == "all" and item["checks"])}


def fault_isolated(normal: dict, fault: dict, name: str = "update_handover_broken", expected_ids=None) -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID: the fault run must hold the same IDs, leave
    every S5 ID red and change no other ID (G1 may change: the broken handover makes the updates that must succeed exit 2)."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    allowed = set(FAULT_TARGETS[name]) | set(FAULT_ALSO_RED.get(name, ()))
    outside = [i for i in changed if i not in allowed]
    not_red = sorted(i for i in FAULT_TARGETS[name] if (after.get(i) or {}).get("passed") is not False)
    wanted = set(registry_ids(normal.get("flows") or None) if expected_ids is None else expected_ids)
    missing = sorted(set(before) ^ set(after)) + sorted(i for i in wanted if i not in before or i not in after)
    return {"isolated": not outside and not not_red and not missing, "changed": changed,
            "changed_outside_targets": outside, "targets_not_red": not_red, "ids_differ": missing}


# Every `refuses=True` of this module is one expected refusal with the reason its check asserts (G1: any other step that
# does not exit 0 fails the check that captured it, and G1 with it).
EXPECTED_REFUSALS = {
    "update_with_a_running_child": "the update's collector step: `a child run is active; nothing was changed, try again when none is running`",
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


def previous_clone(world) -> Path:
    """The earlier build: a copy of this checkout's tracked files with the release before this one as its VERSION."""
    git = subprocess.run(["/bin/bash", str(e2e_support.SETUP), "--print-git"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, env=e2e_support.child_environment(os.environ)).stdout.strip()
    if not git.startswith("/"):
        raise e2e_support.HarnessError("no trusted git")
    listed = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, stdin=subprocess.DEVNULL,
                            env=e2e_support.child_environment(os.environ))
    if listed.returncode != 0 or not listed.stdout:
        raise e2e_support.HarnessError("the tracked files of this checkout could not be listed")
    clone = world.base / "previous-build"
    clone.mkdir()
    for name in listed.stdout.decode().split("\0"):
        source = ROOT / name
        if not name or not os.path.lexists(source):
            continue
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    package = clone / "packages" / "codex-agents" / "native-package"
    (package / "VERSION").write_text(previous_version(current_version()) + "\n")
    # One comment line in a runtime file: the earlier build is another runtime (its own digest), as every real release is.
    with open(package / ".codex" / "orchestration" / "codex_agents" / "__init__.py", "a", encoding="utf-8") as handle:
        handle.write(PREVIOUS_BUILD_MARK)
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


@check("L12.2", group="upd_main")
def l12_2_one_update_with_an_admitted_never_launched_run_needs_no_manual_stop(phase: Phase) -> None:
    scene = main_scene()
    update = scene.u1
    run = scene.admitted
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted",
                 "before the update a run was opened and admitted")
    phase.expect(run.thread is None and run.code is None, "that run was never dispatched, so it never launched")
    phase.expect((scene.admitted_ledger.get("host_run_bindings") or 0) >= 1,
                 "the account ledger holds the opened run's binding before the update")
    start = scene.start["first"]
    phase.expect(len(start.pids) == 1 and runs_from(start.collector, scene.clone) and runs_from(start.service, scene.clone),
                 "before the update the collector and the project's service run from the earlier build")
    phase.expect(start.version == previous_version(current_version()) and start.ready is True,
                 "the collector reports the earlier version, ready")
    phase.expect(update.steps == ["setup_update"], "the update was one command (the harness ran nothing else around it)")
    phase.expect(not update.run.timed_out and update.run.code == 0, "the one update command exits 0")
    states = states_of(update)
    phase.expect(sorted(states) == list(range(1, 10)) and "FAIL" not in states.values(),
                 "the update's summary names every step and none failed")
    phase.expect(all(update.alive_before.values()) and update.alive_before,
                 "the earlier collector was running right before the update")
    phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive),
                 "the earlier collector is gone and exactly one new collector runs (replaced by the update itself)")
    phase.expect(runs_from(update.after.collector, ROOT) and runs_from(update.after.service, ROOT),
                 "the collector and the project's service now run from this build")
    phase.expect(update.after.service_pids and not set(update.after.service_pids) & set(update.before.service_pids),
                 "the project's service was replaced, not kept")
    phase.expect(update.after.ready is True and update.after.version == current_version() and update.after.stale is False,
                 "the collector reports this build, ready, its code not stale")
    phase.expect((update.after.earlier or {}).get("holding") == 0,
                 "the collector reports no earlier-version run holding a slot")
    phase.reason("update_without_manual_stop")


@check("L12.3", group="upd_child")
def l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on(phase: Phase) -> None:
    scene = child_scene()
    update, run = scene.refused, scene.run
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    phase.expect((scene.running.get("liveness") or {}).get("last_activity_category") == "startup"
                 and scene.running.get("stop_requested") is False,
                 "the project's service reports the child really running before the update")
    phase.expect(update.steps == ["setup_update"], "the update was one command")
    phase.expect(not update.run.timed_out and update.run.code != 0, "the update is refused (it exits non-zero)")
    phase.expect(child_refusal_is_plain(update.run.text),
                 "the refusal says a child run is active, that nothing was changed and to try again when none is running")
    phase.reason("child_run_active")
    # UPDFIX-3: the refusal is decided in step 2, before the installer runs (it was the collector step, after the install).
    states = states_of(update)
    phase.expect(not any(state == "OK" for number, state in states.items() if 3 <= number <= 8),
                 "the refusal comes before the install step: steps 3 to 8 (providers to commands) ran none of their work")
    # Review r1: the line above alone also passes an empty summary; the refusal itself must be reported, at step 2.
    phase.expect("The update stopped (child_run_active)" in update.run.text and states.get(2) != "OK",
                 "the refusal is reported as the update stopping at step 2 (Project) with the reason child_run_active")
    phase.expect(update.before.pids == update.after.pids and all(update.alive_after.values()) and update.alive_after
                 and update.before.service_pids == update.after.service_pids,
                 "the earlier collector and the project's service are the same live processes right after the refusal")
    phase.expect(runs_from(update.after.collector, scene.clone) and runs_from(update.after.service, scene.clone),
                 "they still run from the earlier build")
    phase.expect(len(scene.child_before) == 1 and scene.child_after == [(scene.child_before[0], True)],
                 "the child's own process is the same one and still running right after the refusal")
    phase.expect(scene.dispatch_code == 0, "the child's dispatch finishes normally once the child ends")
    phase.expect(scene.slots and all(row["released_at_ms"] is not None for row in scene.slots),
                 "the child's slot is released in the ledger once it ends")


@check("L12.3", group="upd_child")
def l12_3_refused_update_with_a_running_child_changes_nothing(phase: Phase) -> None:
    """The requirement is `changes nothing`: no file, no commit, no record, and the project still works for the primary
    that started the child (it can record the child's result and close its orchestration)."""
    scene = child_scene()
    update = scene.refused
    changed = changed_anything(update.snapshot_before, update.snapshot_after)
    for name in changed:
        phase.reason("changed_" + name)
    phase.expect(update.run.code != 0, "the update was refused")
    phase.expect("files" not in changed, "the refusal left every file of the project and the home as it was")
    phase.expect("head" not in changed and "commits" not in changed, "the refusal made no commit in the project")
    phase.expect("dirty" not in changed, "the refusal left the project's uncommitted listing as it was")
    phase.expect("tables" not in changed, "the refusal changed no ledger table's row count")
    phase.expect(not changed, "the refusal changed nothing at all")
    phase.expect(scene.recorded[0] == 0, "the primary can still record the child's result after the refusal")
    phase.expect(scene.closed[0] == 0, "the primary can still close its orchestration after the refusal")


@check("L12.3", group="upd_child")
def l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended(phase: Phase) -> None:
    """`Try again when none is running` (the refusal's own words) has to work without a hand stop."""
    scene = child_scene()
    final = scene.final
    phase.expect(scene.dispatch_code == 0 and scene.slots and all(row["released_at_ms"] is not None for row in scene.slots),
                 "the child has ended and its slot is released")
    phase.expect(final.steps == ["setup_update"], "the second update was one command")
    states = states_of(final)
    for number, state in states.items():
        if state == "FAIL":
            phase.reason(f"step_{number}_failed")
    phase.expect(not final.run.timed_out and final.run.code == 0 and "FAIL" not in states.values(),
                 "the same update, with no child running, goes through")
    phase.expect(replaced(final.before.pids, final.after.pids, recovery.alive) and final.after.version == current_version()
                 and final.after.ready is True and runs_from(final.after.service, ROOT),
                 "and replaces the earlier collector and the project's service, ready on this build")


@check("L12.4", group="upd_dirty")
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


@check("L12.5", group="upd_main")
def l12_5_second_project_updated_after_the_first_works_and_the_first_still_works(phase: Phase) -> None:
    scene = main_scene()
    first, second = scene.u1, scene.u2
    phase.expect(first.run.code == 0 and not first.run.timed_out, "the first project's update exits 0")
    phase.expect(scene.start["second"].version == previous_version(current_version())
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


@check("L11.2", group="upd_fail")
def l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds(phase: Phase) -> None:
    """The requirement names `learning-rollback`; that command (cli.py) records a candidate's failback and has nothing to do
    with an upgrade, so this check judges what the requirement describes: a failed upgrade, forced part-way, undone by the
    installer's own rollback, the records unchanged, the next attempt succeeding (S5-STATUS.md, finding)."""
    scene = fail_scene()
    failed = scene.failed
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
    phase.expect("records" not in changed and "tables" not in changed,
                 "the collector's records by kind and the ledger's table row counts are unchanged")
    phase.expect(failed.before.pids == failed.after.pids and failed.before.service_pids == failed.after.service_pids
                 and runs_from(failed.after.collector, scene.clone),
                 "the collector and the project's service are the same live processes, still on the earlier build")
    nxt = scene.nxt
    phase.expect(nxt.run.code == 0 and "FAIL" not in states_of(nxt).values(),
                 "the next attempt (nothing forced) goes through")
    phase.expect(nxt.after.ready is True and nxt.after.version == current_version()
                 and replaced(nxt.before.pids, nxt.after.pids, recovery.alive),
                 "and replaces the earlier collector by itself, ready on this build")
    phase.expect(nxt.snapshot_after["commits"] != nxt.snapshot_before["commits"] and nxt.snapshot_after["dirty"] == "",
                 "the next attempt installed and committed the update the failed one had undone")


@check("G6", group="upd_main")
def g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build(phase: Phase) -> None:
    scene = main_scene()
    first, second = scene.u1, scene.u2
    for name, update in (("the first", first), ("the second", second)):
        phase.expect(update.run.code == 0 and not update.run.timed_out, f"{name} update exits 0")
        phase.expect(update.steps == ["setup_update"], f"{name} update was the only command run around it")
        phase.expect(update.after.ready is True and update.after.version == current_version()
                     and update.after.stale is False, f"{name} update ends with the collector ready on this build, not stale")
        phase.expect(len(update.after.pids) == 1 and runs_from(update.after.collector, ROOT),
                     f"{name} update ends with exactly one collector, running this build")
    phase.expect(all(first.alive_before.values()) and first.alive_before
                 and replaced(first.before.pids, first.after.pids, recovery.alive),
                 "the earlier collector was alive right before the first update and gone after it, with no stop in between")
    phase.expect(all(second.alive_before.values()) and second.alive_before,
                 "the collector the first update started was alive right before the second update")
    phase.expect(scene.after_u2.ready is True and scene.after_u2.pids == second.after.pids,
                 "the collector is healthy and the same process after both updates")
    phase.reason("collector_replaced_by_update")


# ---------------------------------------------------------------------------------------------
# Scene 2: a child really running when the update is run (L12.3)

def child_scene():
    if "upd_child" in _SCENES:
        return _SCENES["upd_child"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["upd_child"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    scene.clone = previous_clone(world)
    set_up_previous(world, scene.clone)
    ctl = scene.ctl = Ctl(world, "c")
    ctl.host("provider-observe", "--provider", "claude", "--billing-probe")
    gate = world.tools / "update-gate"
    run = scene.run = ctl.child()
    arm_gated(ctl, run, gate)
    ctl.dispatch_in_background(run)
    scene.running = ctl.wait_running(run)
    scene.child_before = [pid for pid, _ in listing(world, "fake_child.py")]
    scene.refused = do_update(world, refuses=True)
    scene.child_after = [(pid, recovery.alive(pid)) for pid in scene.child_before]  # the child process itself, right after
    gate.write_text("open")
    ctl.finish(run)
    scene.dispatch_code = run.code
    scene.recorded = record_after_refusal(ctl, run)
    scene.slots = settled(world)
    scene.closed = ctl.host("learning-close-orchestration", "--orchestration-handle", ctl.handle, "--status", "completed",
                            refuses=True)  # the primary closes what it opened
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
    scene.touched = world.project / ".codex" / "orchestration" / "codex_agents" / "__init__.py"  # the file the Codex step updates
    scene.touched_before = scene.touched.stat().st_mtime_ns
    write_patches(world, fault_patches(forced=True))
    scene.failed = do_update(world, refuses=True)
    scene.touched_after = scene.touched.stat().st_mtime_ns
    write_patches(world, fault_patches())
    scene.nxt = do_update(world)
    return scene
