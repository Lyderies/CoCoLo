"""L12.1, L12.6 and G7: updates from earlier releases with history built on them (T7 slice S4).

An earlier release is installed the way its own installer installs it (`git archive <tag>`, that tag's scripts/install.py
and scripts/attest.py, its installed CLI): the v0.3.x setup.sh has no --answers or --update, and its wizard is a menu an
owner answers by hand (owner check, ANALYSIS-0.4.0-t7-lifecycle-e2e.md R6), so this proves the DATA upgrade from that
release and not the old wizard. History is built on the earlier release by its own CLI (the run lifecycle of L5, the
provider observations, the owner key and billing attestations). The update is HEAD's own `./setup.sh --answers` with the
owner's records consent (`upgrade_records: true`; `--update` alone refuses until the owner chooses that, which
tests/test_e2e_update_history.py's scene shows as `records_need_owner_choice`).

Every value is read through a different path than the stimulus: the account ledger and the project's history as
read-only sqlite, the installed CLI's status, the process table and the git objects of the tag. The only direct write
into an old ledger is the L12.6 bulk fixture (rows copied from the release's own records, see `grow_old_history`),
made while no process of the release is running."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_run import (drive_lifecycle, lifecycle_preflights, lifecycle_verified_and_closed,  # noqa: E402
                            lifecycle_worker_recorded, trusted_interpreter_checks)
from e2e_checks_shared import write_patches  # noqa: E402
from e2e_core import CLAUDE_MODEL, PROBE, _SCENES, CheckFailed, Phase, check  # noqa: E402

TAGS = ("v0.3.1", "v0.3.2", "v0.3.3", "v0.3.4")
VERSION_PATH = "packages/codex-agents/native-package/VERSION"
# The fault of this slice, chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT).
FAULT: str | None = None
FAULTS: dict[str, list] = {}
HEAD_VERSION = (ROOT / VERSION_PATH).read_text().strip()


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


# The earlier release -----------------------------------------------------------------------------------------

def trusted_git() -> str:
    found = subprocess.run(["/bin/bash", str(e2e_support.SETUP), "--print-git"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=e2e_support.child_environment(os.environ)).stdout.strip()
    if not found.startswith("/"):
        raise e2e_support.HarnessError("no trusted git")
    return found


def tag_version(tag: str) -> str:
    """The orchestrator version a release tag carries, read from the repository's own objects."""
    done = subprocess.run([trusted_git(), "-C", str(ROOT), "show", f"{tag}:{VERSION_PATH}"], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, env=e2e_support.child_environment(os.environ))
    if done.returncode != 0 or not re.fullmatch(r"\d+\.\d+\.\d+", done.stdout.strip()):
        raise e2e_support.HarnessError("the release tag is not in this repository")
    return done.stdout.strip()


def archive_tag(world, tag: str) -> Path:
    """`git archive <tag>` extracted next to the world: the release as it was shipped."""
    folder = world.base / ("rel-" + tag)
    if not folder.exists():
        folder.mkdir()
        packed = subprocess.run([trusted_git(), "-C", str(ROOT), "archive", tag], capture_output=True,
                                stdin=subprocess.DEVNULL, env=e2e_support.child_environment(os.environ))
        if packed.returncode != 0:
            raise e2e_support.HarnessError("the release tag cannot be archived")
        unpacked = subprocess.run(["/usr/bin/tar", "-x", "-C", str(folder)], input=packed.stdout, capture_output=True,
                                  env=e2e_support.child_environment(os.environ))
        if unpacked.returncode != 0:
            raise e2e_support.HarnessError("the release archive cannot be unpacked")
    return folder


def run_release_tool(world, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=world.env, stdin=subprocess.DEVNULL,
                          cwd=str(world.base), timeout=300)


def python_tool(world, release: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return run_release_tool(world, [str(world.shim), "-I", "-B", "-S", str(release / "scripts" / script), *args])


def install_release(world, tag: str, *, first: bool) -> Path:
    """The release's own installer, applied to the project, then the provider routing its setup writes, committed.

    The release's setup.sh wizard is a menu; these are the commands it runs (its step_install)."""
    release = archive_tag(world, tag)
    options = ["install", "--target", str(world.project), "--apply"]
    if first:
        routing = release / "project-agent-routing.json"
        routing.write_text('{"version": 1, "roles": {}, "verifiers": {}}\n')
        options += ["--operational-checks", str(world.checks), "--project-agent-routing", str(routing)]
    done = python_tool(world, release, "install.py", *options)
    if done.returncode != 0:
        raise e2e_support.HarnessError(f"the {tag} installer failed")
    if first:
        written = run_release_tool(world, ["/bin/bash", str(release / "setup.sh"), "--write-provider-routing",
                                           str(world.project), "on", "on", CLAUDE_MODEL, "off"])
        if written.returncode != 0:
            raise e2e_support.HarnessError(f"the {tag} provider routing was not written")
    world.git("add", ".")
    world.git("commit", "-qm", "install " + tag)
    return release


def attest(world, release: Path):
    """The release's own attest.py on a terminal: the owner key (made at the first run) and both billing statements,
    the passphrase typed and each statement signed, as the owner does."""
    return world.terminal([str(world.shim), "-I", "-B", "-S", str(release / "scripts" / "attest.py"),
                           "--target", str(world.project)], label="old_attest", cwd=world.project, timeout=120)


def set_up_release(world, tag: str) -> Path:
    """A project on the earlier release `tag`: installed, owner key made, collector registered, both providers
    attested and observed. Returns the release folder."""
    trusted_interpreter_checks(world)
    release = install_release(world, tag, first=True)
    code, reply, _ = world.cli("learning-register")
    if code != 0 or not (reply or {}).get("ready"):
        raise e2e_support.HarnessError(f"the {tag} collector did not register")
    world.claude_frames(e2e_support.claude_probe_frames())
    done = attest(world, release)
    if done.code != 0 or "Attested: codex, claude" not in done.text:
        raise e2e_support.HarnessError(f"the {tag} billing attestations were not signed")
    world.cli("provider-observe", "--provider", "codex")
    world.cli("provider-observe", "--provider", "claude")
    world.host_cli("claude", *PROBE)
    return release


# Reads that do not go through the update ---------------------------------------------------------------------

def ledger_path(world) -> Path:
    return world.home / ".local" / "state" / "codex-agents" / "learning.sqlite3"


def row_keys(connection, table: str) -> frozenset:
    """The identity of each row of a table, never its content: the declared primary key, or a digest of the whole
    row where the table declares none."""
    pk = [row[1] for row in sorted(connection.execute(f'pragma table_info("{table}")'), key=lambda r: r[5]) if row[5]]
    if pk:
        query = "select " + ",".join(f'"{name}"' for name in pk) + f' from "{table}"'
        return frozenset(hashlib.sha256(repr(row).encode()).hexdigest() for row in connection.execute(query))
    return frozenset(hashlib.sha256(repr(row).encode()).hexdigest() for row in connection.execute(f'select * from "{table}"'))


def counts_by_kind(world) -> dict:
    """Counts by kind and the identity of every row, never content: the account ledger's rows by table (`tables`, and
    `keys` by table) and its records by record type (`types`, and `record_keys` by type, the record ids)."""
    connection = sqlite3.connect(f"file:{ledger_path(world)}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
        keys = {name: row_keys(connection, name) for name in tables if name != "records"}
        record_keys: dict = {}
        for kind, record_id in connection.execute("select record_type, record_id from records"):
            record_keys.setdefault(kind, set()).add(record_id)
        counts = {name: connection.execute(f'select count(*) from "{name}"').fetchone()[0] for name in tables}
    finally:
        connection.close()
    return {"tables": counts, "types": {kind: len(ids) for kind, ids in record_keys.items()},
            "keys": keys, "record_keys": {kind: frozenset(ids) for kind, ids in record_keys.items()}}


def status(world, *, earlier_release: bool = False) -> dict:
    """The installed CLI's learning-status. An earlier release's own status prints the owner key's fingerprint, which
    a saved output may not carry (G8), so it is read here without being kept as an output of the run; nothing of it
    reaches the report."""
    if not earlier_release:
        code, reply, _ = world.cli("learning-status")
        return reply or {}
    done = subprocess.run([str(world.shim), "-I", "-B", "-S", str(world.installed_cli), "learning-status",
                           "--project-root", str(world.project)], capture_output=True, text=True, env=world.env,
                          stdin=subprocess.DEVNULL, cwd=str(world.base), timeout=60)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    return reply if isinstance(reply, dict) else {}


def service_commands(world) -> list[str]:
    """The command lines of the collector and the project services running inside this world (process table)."""
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             env=e2e_support.child_environment(os.environ)).stdout
    return [line.partition(" ")[2] for line in listing.splitlines()
            if str(world.base) in line and ("collector_host.py" in line or "project_host.py" in line)]


# The update ----------------------------------------------------------------------------------------------------

def update_from(world, scene) -> None:
    """HEAD's own scripted setup with the owner's records consent, on the project the earlier release installed."""
    scene.before = counts_by_kind(world)
    scene.before_status = status(world, earlier_release=True)
    if FAULT:
        write_patches(world, fault_patches())
    scene.update = world.setup(world.answers(upgrade_records=True, providers={
        "codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL, "bridge": "off"}))
    scene.after = counts_by_kind(world)
    scene.after_status = status(world)


def recent_admission(world) -> int:
    """Admission records (headroom and billing readings) observed in the last day, as the admission read counts them."""
    connection = sqlite3.connect(f"file:{ledger_path(world)}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in ADMISSION_KINDS)
        return connection.execute(f"select count(*) from records where record_type in ({marks}) and observed_at_ms > ?",
                                  (*ADMISSION_KINDS, int(time.time() * 1000) - 86_400_000)).fetchone()[0]
    finally:
        connection.close()


def collector_processes(world) -> list[str]:
    """pid and age of each collector host of this world (never a path)."""
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True,
                             env=e2e_support.child_environment(os.environ)).stdout
    return [" ".join(line.split()[:2]) for line in listing.splitlines()
            if str(world.base) in line and "collector_host.py" in line]


def probe_diagnosis(world, scene) -> dict:
    """What was true when the first probe was refused, and what changed until it was not: the collector's own status,
    its processes (pid, age) and a further probe, every 3 seconds for at most 60 s. Reasons and numbers only. This
    only RECORDS (the first attempt already failed the check); it never makes anything green."""
    started = time.monotonic()
    trail = []
    while True:
        reply = status(world)
        trail.append({"s": round(time.monotonic() - started), "ready": reply.get("ready"),
                      "stale": reply.get("collector_code_stale"), "version": reply.get("collector_runtime_version"),
                      "processes": collector_processes(world)})
        if scene.probe_refused is None or time.monotonic() - started > 60:  # a refusal other than stale: one snapshot
            break
        code, _reply, text = world.host_cli("claude", *PROBE, refuses="collector_code_stale")
        trail[-1]["probe"] = code if code == 0 else ("stale" if "collector_code_stale" in text else code)
        if code == 0:
            break
        time.sleep(3)
    return {"head_version": HEAD_VERSION, "trail": trail}


def build_scene(tag: str, name: str, *, grow=None, chain=None):
    """One world: the earlier release `tag` with its history, then the update to this build, then the L5 run again."""
    if name in _SCENES:
        return _SCENES[name]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world, tag=tag, primary="claude", steps={}, files={}, digests={}, failed=None)
    _SCENES[name] = scene
    scene.release = chain(world, scene) if chain else set_up_release(world, tag)
    scene.old_version = status(world, earlier_release=True).get("collector_runtime_version")
    old_run = SimpleNamespace(world=world, steps={}, files={}, digests={}, failed=None)
    drive_lifecycle(old_run, "claude", earlier_release=True)
    scene.old_run = old_run
    if grow is not None:
        grow(scene)
    update_from(world, scene)
    scene.setup = scene.update  # the L5 judges read `scene.setup` as the run that made the project ready
    world.claude_frames(e2e_support.claude_probe_frames())
    world.cli("provider-observe", "--provider", "codex")
    world.cli("provider-observe", "--provider", "claude")
    # The billing probe right after the update is judged on its FIRST attempt: setup said ready, so an owner's next
    # command must work. No retry: a refusal (`collector_code_stale` was seen) is recorded with what was true at that
    # moment and turns the checks that read `scene.probe` red.
    code, scene.probe, text = world.host_cli("claude", *PROBE)
    scene.probe_code = code
    scene.probe_refused = "collector_code_stale" if "collector_code_stale" in text else None
    scene.probe_diagnosis = probe_diagnosis(world, scene) if code != 0 else None
    if scene.probe_diagnosis is not None:  # reasons and numbers only: the run's own record of what the refusal was
        print("first_probe_refused", scene.probe_refused, json.dumps(scene.probe_diagnosis), file=sys.stderr)
    drive_lifecycle(scene, "claude")
    for step, output in zip(world.steps, world.outputs):  # what refused, as reason codes only (the run's own record)
        if step.get("code") not in (0, None) and not step.get("refuses"):
            codes = sorted(set(re.findall(r'"(?:reason_code|code)": ?"([a-z_0-9]+)"', output)))
            print("step_refused", step.get("step"), step.get("code"), codes, repr(output[:700]), file=sys.stderr)
    scene.final = counts_by_kind(world)  # after the project opened and dispatched on the updated build
    scene.final_recent_admission = recent_admission(world)
    return scene


# The judges ------------------------------------------------------------------------------------------------------

# What a setup run adds to the account ledger by itself: the observations and admission evidence of its own two provider
# checks (decision on L1.2's OWN_CHECK_TABLES: a second setup may add exactly these) and one stored file. The additions
# are IDENTIFIED, not allowed for in a count: a row (a record id, a primary key) the earlier ledger held must still be
# there after the update, and a row that is new must be of a kind the setup's own checks make. So two old provider
# identities deleted while the setup adds two new ones is a loss, not "the same number".
# Exactly what the update's setup adds, as measured on a real update from each of the four releases (S4 runs): the two
# provider checks make two observations (an origin and a request each), two provider identities, one billing and one
# headroom reading, and the setup stores one file. A table the earlier release did not have yet (a migration made it:
# run_holds, project_wake, admission_evidence for v0.3.1) is not the earlier release's and is not counted.
SETUP_ADDS_TABLES = {"account_observation_origins": 2, "account_observation_requests": 2, "admission_evidence": 2,
                     "stored_files": 1}
SETUP_ADDS_KINDS = {"provider_identity": 2, "provider_billing": 1, "provider_headroom": 1}


def setup_additions(before: dict, after: dict) -> dict:
    """The rows the update's setup added, by table (`tables`) and by record kind (`types`): the rows of `after` that
    `before` did not hold. The rows `before` held that `after` does not are `lost`, by the same two keys."""
    added = {"tables": {}, "types": {}}
    lost = {"tables": {}, "types": {}}
    for table in set(before["keys"]) | set(after["keys"]):
        gained = len(after["keys"].get(table, frozenset()) - before["keys"].get(table, frozenset()))
        missing = len(before["keys"].get(table, frozenset()) - after["keys"].get(table, frozenset()))
        added["tables"][table], lost["tables"][table] = gained, missing
    for kind in set(before["record_keys"]) | set(after["record_keys"]):
        gained = len(after["record_keys"].get(kind, frozenset()) - before["record_keys"].get(kind, frozenset()))
        missing = len(before["record_keys"].get(kind, frozenset()) - after["record_keys"].get(kind, frozenset()))
        added["types"][kind], lost["types"][kind] = gained, missing
    added["tables"]["records"], lost["tables"]["records"] = sum(added["types"].values()), sum(lost["types"].values())
    return {"added": added, "lost": lost}


def history_kept(phase: Phase, scene) -> None:
    """G7: the earlier ledger's rows before the update and after it, by table and by record kind. Nothing is read from
    a record, only identified (record id, primary key) and counted."""
    before, after = scene.before, scene.after
    phase.expect(sum(before["tables"].values()) > 0 and before["types"], "the earlier release's ledger held history")
    changed = setup_additions(before, after)
    added, lost = changed["added"], changed["lost"]
    scene.setup_added = {"tables": {k: v for k, v in added["tables"].items() if v},
                         "types": {k: v for k, v in added["types"].items() if v}}  # identified, kept for the report
    if getattr(scene, "world", None) is not None:
        print("setup_added", json.dumps(scene.setup_added, sort_keys=True), file=sys.stderr)
    phase.expect(not any(lost["tables"].values()), "no table of the earlier release lost a row")
    phase.expect(not any(lost["types"].values()), "no record kind of the earlier release lost a record")
    for table, gained in added["tables"].items():
        if table in before["tables"] and table != "records":  # (a table a migration made new is not the earlier release's)
            phase.expect(gained == SETUP_ADDS_TABLES.get(table, 0), "a table gained exactly the rows the setup's own checks add")
    for kind, gained in added["types"].items():
        phase.expect(gained == SETUP_ADDS_KINDS.get(kind, 0), "a record kind gained exactly the records the setup's own checks make")
    for table, count in before["tables"].items():  # the counts agree with the identities
        phase.expect(after["tables"].get(table, -1) == count + added["tables"][table] - lost["tables"][table],
                     "a table's count is its earlier rows plus the setup's additions")


def summary(text: str) -> dict:
    from e2e_checks_setup import summary_states
    return summary_states(text)


FIRST_PROBE_RAN = "the billing probe right after the update, on its first attempt, ran on the long history"
FIRST_PROBE_READY = "the billing probe right after the update succeeded on its first attempt and the project was ready"


def first_probe_succeeded(phase: Phase, scene, what: str) -> None:
    """The first billing probe after the update (never a later or diagnostic one), judged here by itself so no check
    depends on the order the runner takes the checks of one scene in."""
    probe = getattr(scene, "probe", None)
    probe = probe if isinstance(probe, dict) else {}
    phase.expect(getattr(scene, "probe_code", None) == 0 and probe.get("ready") is True
                 and probe.get("probe") in ("launched", "reused"), what)


def updated_and_full_run_passes(phase: Phase, scene) -> None:
    """L12.1: the project ran the earlier release with history, the update to this build succeeded, the first billing
    probe after it succeeded (judged here, independent of check order), and the L5 run (both judges of L5.1, L5.2 and
    L5.3) passes on the updated project."""
    first_probe_succeeded(phase, scene, FIRST_PROBE_READY)
    world = scene.world
    wanted = tag_version(scene.tag)
    phase.expect(wanted != HEAD_VERSION, "the earlier release is not this build")
    phase.expect(scene.old_version == wanted, "the project's collector ran the earlier release before the update")
    phase.expect(scene.before["tables"].get("host_run_bindings", 0) >= 2
                 and scene.before["tables"].get("run_provenance", 0) >= 2,
                 "the earlier release had recorded runs (a worker and a verifier) before the update")
    phase.expect(scene.old_run.failed is None, "every step of the earlier release's own run was answered")
    run = scene.update
    phase.expect(not run.timed_out and run.code == 0, "the update exits 0")
    states = summary(run.text)
    phase.expect(sorted(states) == list(range(1, 10)) and not [n for n, s in states.items() if s[0] == "FAIL"],
                 "the update's nine steps are all named and none failed")
    phase.expect(states[5][0] == "OK" and "ready" in states[5][2], "the update registered the collector ready")
    after = scene.after_status
    phase.expect(after.get("ready") is True and after.get("collector_runtime_version") == HEAD_VERSION
                 and after.get("collector_code_stale") is False,
                 "the collector runs this build, ready and not stale")
    commands = service_commands(world)
    phase.expect(sum("collector_host.py" in command for command in commands) == 1
                 and not any(("rel-" + scene.tag) in command for command in commands),
                 "one collector runs, and none from the earlier release")
    lifecycle_preflights(phase, scene)
    lifecycle_worker_recorded(phase, scene)
    lifecycle_verified_and_closed(phase, scene)


def scene_for(tag: str):
    return build_scene(tag, "upd_" + tag.replace(".", "_"))


# L12.1 and G7: one scene for each earlier release ------------------------------------------------------------------

@check("L12.1", group="upd_v0_3_1")
def l12_1_update_from_v0_3_1_succeeds_and_the_full_run_passes_again(phase: Phase) -> None:
    updated_and_full_run_passes(phase, scene_for("v0.3.1"))


@check("L12.1", group="upd_v0_3_2")
def l12_1_update_from_v0_3_2_succeeds_and_the_full_run_passes_again(phase: Phase) -> None:
    updated_and_full_run_passes(phase, scene_for("v0.3.2"))


@check("L12.1", group="upd_v0_3_3")
def l12_1_update_from_v0_3_3_succeeds_and_the_full_run_passes_again(phase: Phase) -> None:
    updated_and_full_run_passes(phase, scene_for("v0.3.3"))


@check("L12.1", group="upd_v0_3_4")
def l12_1_update_from_v0_3_4_succeeds_and_the_full_run_passes_again(phase: Phase) -> None:
    updated_and_full_run_passes(phase, scene_for("v0.3.4"))


@check("G7", group="upd_v0_3_1")
def g7_update_from_v0_3_1_keeps_counts_by_kind(phase: Phase) -> None:
    history_kept(phase, scene_for("v0.3.1"))


@check("G7", group="upd_v0_3_2")
def g7_update_from_v0_3_2_keeps_counts_by_kind(phase: Phase) -> None:
    history_kept(phase, scene_for("v0.3.2"))


@check("G7", group="upd_v0_3_3")
def g7_update_from_v0_3_3_keeps_counts_by_kind(phase: Phase) -> None:
    history_kept(phase, scene_for("v0.3.3"))


@check("G7", group="upd_v0_3_4")
def g7_update_from_v0_3_4_keeps_counts_by_kind(phase: Phase) -> None:
    history_kept(phase, scene_for("v0.3.4"))


# Faults (test-only, chosen only by --fault) --------------------------------------------------------------------------

# An update that loses history: the hold register's migration, which runs on every earlier ledger (schema 13 and 14),
# also empties the recorded runs' provenance. Nothing else changes, so the update still succeeds and the run still
# passes; only the counts by kind (G7) say history was lost.
FAULTS["update_loses_history"] = [
    {"module": "codex_agents.learning_store",
     "old": "        from .active_run_capacity import LOST_RUN_REASON\n\n        try:\n            install_run_holds_schema(self._connection)\n",
     "new": "        from .active_run_capacity import LOST_RUN_REASON\n\n        self._connection.execute('DELETE FROM run_provenance')\n"
            "        try:\n            install_run_holds_schema(self._connection)\n"},
]

# An update that loses the ADMISSION history (L12.6a): the same migration also deletes the admission records (headroom and
# billing readings). The update, the run and the probe still work, since the setup's own provider checks write new
# readings; only the preserved records (G7, L12.6a) say the earlier history is gone.
FAULTS["admission_history_dropped"] = [
    {"module": "codex_agents.learning_store",
     "old": "        from .active_run_capacity import LOST_RUN_REASON\n\n        try:\n            install_run_holds_schema(self._connection)\n",
     "new": "        from .active_run_capacity import LOST_RUN_REASON\n\n        self._connection.execute(\"DELETE FROM records WHERE record_type IN ('provider_headroom','provider_billing')\")\n"
            "        try:\n            install_run_holds_schema(self._connection)\n"},
]


# L12.6: history built on an earlier release that is long (L12.6b is deferred: see S4-STATUS.md) --------------------------------

BULK_RECORD_COPIES = 300     # copies of each admission record kind: over 512 together, all observed in the last day
BULK_EVENTS = 200_000        # copies of one real hook event: the project's records pass 128 MiB
BULK_BYTES = 128 * 1024 * 1024
ADMISSION_KINDS = ("provider_headroom", "provider_billing")


def grow_old_history(scene) -> None:
    """Make the earlier release's records long: more than 512 admission records observed in the last day and a
    project over 128 MiB. The rows are copies of the release's own records (a real hook event delivered by its
    installed hook, its own latest headroom and billing readings), written while none of its processes runs, with
    the columns the release's own `records` table has. This is the one direct write into an old ledger: the history
    is the stimulus, and what the update does with it is read through the product."""
    world = scene.world
    code, _out, _text = world.run_hook({"session_id": "e2e-session-1", "cwd": str(world.project),
                                        "permission_mode": "default", "hook_event_name": "UserPromptSubmit",
                                        "turn_id": "e2e-turn-1", "model": "gpt-6-luna", "transcript_path": None,
                                        "prompt": "x"})
    world.stop_processes()
    connection = sqlite3.connect(ledger_path(world))
    connection.row_factory = sqlite3.Row
    try:
        columns = [row["name"] for row in connection.execute("pragma table_info(records)")]
        sequence = connection.execute("select max(source_sequence) from records").fetchone()[0]
        now = int(time.time() * 1000)
        added = {}
        marks = ",".join("?" for _ in columns)

        def add(template, kind: str, count: int, *, age_ms: int, pad: str = "") -> None:
            nonlocal sequence
            rows = []
            for index in range(count):
                record = json.loads(bytes(template["content"]))
                observed = now - age_ms - index
                record["record_id"] = f"{kind}:{hashlib.sha256(f'e2e-{kind}-{age_ms}-{index}'.encode()).hexdigest()}"
                record["observed_at_ms"] = observed
                if pad:
                    record["model"] = f"model-{index:06d}-{pad}"
                content = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                sequence += 1
                row = dict(template)
                row.update(record_id=record["record_id"], source_sequence=sequence, observed_at_ms=observed,
                           ingested_at_ms=observed, content=content,
                           content_digest=hashlib.sha256(content).hexdigest())
                rows.append([row[name] for name in columns])
            connection.executemany(f"insert into records({','.join(columns)}) values({marks})", rows)
            added[kind] = added.get(kind, 0) + count

        for kind in ADMISSION_KINDS:
            template = connection.execute("select * from records where record_type=? order by observed_at_ms desc "
                                          "limit 1", (kind,)).fetchone()
            add(template, kind, BULK_RECORD_COPIES, age_ms=120_000)
        event = connection.execute("select * from records where record_type='event' order by observed_at_ms desc "
                                   "limit 1").fetchone()
        scene.event_copies = 0
        if event is not None:
            add(event, "event", BULK_EVENTS, age_ms=3_600_000, pad="m" * 100)
            scene.event_copies = BULK_EVENTS
        connection.commit()
        scene.grown = added
        scene.ledger_bytes = ledger_path(world).stat().st_size
    finally:
        connection.close()


# The admission read of the updated build is cut to one row: any project with history is refused at its open and its
# billing probe with `admission_capture_limit`, the read the 0.3.2 fix bounded (L12.1's full run and L12.6 both fail).
FAULTS["admission_capture_tiny"] = [
    {"module": "codex_agents.account_original_open",
     "old": "AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer, snapshot.anchor_ms))",
     "new": "AdmissionCaptureLimits(1, limits.bytes_per_job), signer, snapshot.anchor_ms))"},
    {"module": "codex_agents.account_billing_admission",
     "old": "AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer, anchor_ms))",
     "new": "AdmissionCaptureLimits(1, limits.bytes_per_job), signer, anchor_ms))"},
]


def big_scene():
    """L12.6, the long history: an earlier release (the first of the line this slice covers) with its own run, made long."""
    return build_scene("v0.3.1", "upd_big", grow=grow_old_history)


def backups_over(world, size: int) -> int:
    """How many project backup files the update wrote that are larger than `size` bytes (a path of its own)."""
    found = (world.home / ".local" / "state" / "codex-agents").rglob("*.sqlite3")
    return sum(1 for path in found if "backup" in str(path) and path.stat().st_size > size)


def admission_preserved(phase: Phase, scene) -> None:
    """L12.6a, the admission history itself and the first probe: every admission record the earlier release held is
    still there (by record id) after the update and after the project opened and dispatched, more than 512 are in the
    admission window then, and the billing probe right after the update worked on its first attempt."""
    before = scene.before
    # The admission records themselves: every one the earlier release held is still there (by record id) after the
    # update, and the more than 512 observed in the last day are still there after the project opened and dispatched.
    for kind in ADMISSION_KINDS:
        phase.expect(before["record_keys"].get(kind, frozenset()) <= scene.after["record_keys"].get(kind, frozenset()),
                     "the update kept every admission record of the earlier release")
        phase.expect(before["record_keys"].get(kind, frozenset()) <= scene.final["record_keys"].get(kind, frozenset()),
                     "every admission record is still there after the project opened and dispatched")
    phase.expect(scene.final_recent_admission > 512, "more than 512 admission records were in the admission window when the project opened and dispatched")
    first_probe_succeeded(phase, scene, FIRST_PROBE_RAN)


@check("L12.6a", group="upd_big")
def l12_6a_over_512_admission_records_and_128_mib_still_open_dispatch_and_upgrade(phase: Phase) -> None:
    """L12.6a: unbounded admission history and a ledger over 128 MiB. The history is a SYNTHETIC VOLUME fixture: its
    admission and event rows are copies of the release's own records (one real hook event, its own latest headroom and
    billing readings), so it proves the thresholds (rows over 512, bytes over 128 MiB) and not the diversity of a
    long real history, nor the growth of tables that relate to historical runs (200,000 events keep one template's
    relationships): an upgrade cost proportional to historical runs or links is not shown (docs/requirements/t7/S4-STATUS.md).
    L12.6b (0.2.x leftovers, a masked busy probe, the protected copy) is a separate, unchecked requirement."""
    scene = big_scene()
    world = scene.world
    before = scene.before
    admission = sum(before["types"].get(kind, 0) for kind in ADMISSION_KINDS)
    phase.expect(admission > 512, "the earlier release's ledger held more than 512 admission records")
    phase.expect(scene.event_copies > 0 and before["types"].get("event", 0) >= scene.event_copies,
                 "the earlier release's ledger held the project's long event history")
    phase.expect(scene.ledger_bytes > BULK_BYTES, "the earlier release's records came to more than 128 MiB")
    run = scene.update
    phase.expect(not run.timed_out and run.code == 0, "the update exits 0 with all of that in the records")
    states = summary(run.text)
    phase.expect(sorted(states) == list(range(1, 10)) and not [n for n, s in states.items() if s[0] == "FAIL"],
                 "none of the update's nine steps failed")
    after = scene.after_status
    phase.expect(after.get("ready") is True and after.get("collector_runtime_version") == HEAD_VERSION,
                 "the collector runs this build and is ready")
    phase.expect(scene.after["types"].get("event") == before["types"].get("event"),
                 "the update kept every event record of the long project")
    admission_preserved(phase, scene)
    phase.expect(backups_over(world, BULK_BYTES) >= 1, "the update backed up the project's records, over 128 MiB")
    lifecycle_preflights(phase, scene)
    lifecycle_worker_recorded(phase, scene)
    lifecycle_verified_and_closed(phase, scene)
