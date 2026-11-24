"""L10, L11.1, L13: learning, backup and the session guard (T7 slice S3).

L10.1 hook events through the installed learning_hook.py, L10.2 the project store and `learning-run-cycle`, L10.3
retention through the real `learning-expire`, L11.1 `learning-backup-project` and `learning-verify-backup`, L13.1 the
session guard's refusals and L13.2 the protected copy's own-project pin, both through the protected runtime copy the
owner's Codex command rule runs, with a Codex session record written by the World (World.codex_session).

Every value is read through a different path than the command that produced it: the collector's status for the
ledger's own rows (read-only sqlite), the stored file for the backup reply's digest, the registry and the disk for a
removed backup, the project store's files for the move. Nothing is written into the product's state: the scenes only
read it. The commands are the installed CLI and the protected copy; the only fault is chosen by the runner's explicit
`--fault NAME` argument."""

from __future__ import annotations

import hashlib
import json
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
from e2e_checks_run import trusted_interpreter_checks  # noqa: E402
from e2e_checks_run_control import add_project, project_view, set_up, write_patches  # noqa: E402
from e2e_core import _SCENES, Phase, check  # noqa: E402

S3_IDS = ("L10.1", "L10.2", "L10.3", "L11.1", "L13.1", "L13.2")

# The slice's fault (a test-only rewrite of the installed product, applied by the stand-in Python as the named module
# is imported). It is chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in the
# process that runs the scenes); no environment variable selects it and World environments carry no E2E_ variable.
FAULT: str | None = None
FAULTS = {
    "learning_guard_broken": [
        # L10.1: the hook's ingest call is answered and dropped: no event reaches the collector.
        {"module": "codex_agents.collector_client",
         "old": "    def ingest(self, binding_token: str, record: dict[str, Any]) -> dict[str, Any]:\n",
         "new": "    def ingest(self, binding_token: str, record: dict[str, Any]) -> dict[str, Any]:\n        return {}\n"},
        # L10.2: the cycle's full report comes back with its proposals unfinished (an answer, not a refusal).
        {"module": "codex_agents.client_runtime",
         "old": "        return parse_bounded_json(''.join(chunks).encode('ascii'), max_bytes=view['report_bytes'])",
         "new": "        return {**parse_bounded_json(''.join(chunks).encode('ascii'), max_bytes=view['report_bytes']), 'proposal_status': 'pending'}"},
        # L11.1: a backup's verification reports a digest the file does not have.
        {"module": "codex_agents.learning_store",
         "old": "return {\"verified\": True, \"backup_sha256\": result[\"backup_sha256\"],",
         "new": "return {\"verified\": True, \"backup_sha256\": \"0\" * 64,"},
        # L13.1: a session that runs without its sandbox is no longer recognised.
        {"module": "codex_agents.session_guard",
         "old": "UNSANDBOXED_TYPES = frozenset({\"danger-full-access\"})",
         "new": "UNSANDBOXED_TYPES = frozenset()"},
        # L13.2: the own-project refusal loses its reason.
        {"module": "codex_agents.host_commands",
         "old": "class OwnRootRefused(HostCommandError):\n",
         "new": "class OwnRootRefused(HostCommandError):\n    def __init__(self, *args):\n        super().__init__(\"a refusal\")\n"},
    ]}
FAULTS["guard_disabled_profile_refusal_removed"] = [
    # L13.1 only: the guard's own early refusal of a session that runs without its sandbox (Codex then omits the
    # filesystem entries) is removed; the later check on the sandbox kind remains, which an entries-less record never reaches.
    {"module": "codex_agents.session_guard",
     "old": "    if refuse_unsandboxed and sandbox_type in UNSANDBOXED_TYPES:\n",
     "new": "    if False and refuse_unsandboxed and sandbox_type in UNSANDBOXED_TYPES:\n"}]
FAULTS["guard_project_parent_refusal_removed"] = [
    # L13.1 only: a session opened on a folder that holds the project (and not the home folder) is no longer refused.
    {"module": "codex_agents.session_guard",
     "old": "            key = _project_problem(written, project)\n",
     "new": "            key = None\n"}]
FAULT_TARGETS = {"learning_guard_broken": S3_IDS, "guard_disabled_profile_refusal_removed": ("L13.1",),
                 "guard_project_parent_refusal_removed": ("L13.1",)}
FAULT_ALSO_RED: dict[str, tuple] = {name: () for name in FAULT_TARGETS}


RETAIN_WINDOW_MS = 8000


def retention_patch() -> dict:
    """Decision 193 (test only, same class as decision 128's silence limits): the raw-event retention window, scaled for
    the one scene that judges real expiry. Every raw event then lives RETAIN_WINDOW_MS instead of its 7 to 90 days; nothing
    else changes (the summary window and every other limit are untouched)."""
    return {"module": "codex_agents.learning_store", "old": "            keep[code] = period\n",
            "new": f"            keep[code] = {RETAIN_WINDOW_MS}\n"}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


def registry_ids(flows=None) -> set:
    """Every requirement ID of the registry that a run of `flows` judges (every-flow requirements that have a check; all of the registry when None)."""
    items = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]
    return {item["id"] for item in items if flows is None or item["flow"] in flows or (item["flow"] == "all" and item["checks"])}


def fault_isolated(normal: dict, fault: dict, name: str = "learning_guard_broken", expected_ids=None) -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID: the fault run must hold the same IDs,
    leave every S3 ID red and change no other ID (a newly red ID outside the declared targets is not isolation)."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    allowed = set(FAULT_TARGETS[name]) | set(FAULT_ALSO_RED.get(name, ()))
    outside = [i for i in changed if i not in allowed]
    not_red = sorted(i for i in FAULT_TARGETS[name] if (after.get(i) or {}).get("passed") is not False)
    wanted = set(registry_ids(normal.get("flows") or None) if expected_ids is None else expected_ids)
    # an ID missing from either report (also one missing from both) is not isolation: every registered ID must be judged
    missing = sorted(set(before) ^ set(after)) + sorted(i for i in wanted if i not in before or i not in after)
    return {"isolated": not outside and not not_red and not missing, "changed": changed,
            "changed_outside_targets": outside, "targets_not_red": not_red, "ids_differ": missing}


# Every `refuses=True` of this module is one expected refusal with the reason its check asserts (G1: any other step
# of the scenes that does not exit 0 fails the check that captured it and G1 with it).
EXPECTED_REFUSALS = {
    "retention_future_clock": "learning-expire with a clock in the future: `input validation failed`",
    "backup_unknown_file": "learning-verify-backup of a file id that was never made: `project backup is unavailable`",
    "backup_expired_file": "learning-verify-backup of a backup retention removed: `project backup is unavailable`",
    "guard_danger_full_access": "a session without its sandbox: `runs without its sandbox (danger-full-access)`",
    "guard_unknown_sandbox": "a session whose sandbox kind the guard does not know: `not in the format`",
    "guard_not_a_codex_session": "a command that did not come from a Codex session: `CODEX_THREAD_ID is not set`",
    "guard_no_session_record": "a Codex session with no record: `no record of this session was found`",
    "guard_opened_on_home": "a session opened on the home folder: `can write to your home folder`",
    "guard_opened_above_home": "a session opened above the home folder: `holds your home folder`",
    "guard_opened_above_project": "a session opened on a parent that holds the project but not the home folder: `contains this project`",
    "copy_other_project": "the copy of project A asked about project B: `--project-root is refused`",
    "copy_other_state": "the copy asked to use another state root: `--state-root is refused`",
    "copy_of_b_asked_about_a": "the copy of project B asked about project A: `--project-root is refused`",
}

STATE = (".local", "state", "codex-agents")
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
BACKUP_ID, SHORT_BACKUP_ID, LONG_BACKUP_ID = "e2eb1", "e2eshort", "e2elong"
SHORT_BACKUP_SECONDS = 60
# What the hook events carry that must never reach the state: planted in each event, searched for afterwards.
PLANTED = ("E2E-SECRET-PROMPT", "E2E-SECRET-COMMAND", "E2E-SECRET-OUTPUT", "E2E-SECRET-ANSWER")
HOOK_EVENTS = (("UserPromptSubmit", {"prompt": PLANTED[0]}),
               ("PreToolUse", {"tool_name": "exec_command", "tool_input": {"cmd": PLANTED[1]}, "tool_use_id": "u1"}),
               ("PostToolUse", {"tool_name": "exec_command", "tool_input": {"cmd": PLANTED[1]},
                                "tool_response": PLANTED[2], "tool_use_id": "u1"}),
               ("SessionStart", {"source": "startup"}),
               ("Stop", {"stop_hook_active": False, "last_assistant_message": PLANTED[3]}))
HOOK_CODES = {"turn_started", "tool_started", "tool_output_observed", "session_started", "turn_stop_observed"}
PATH_IN_TEXT = re.compile(r"(?<![\w.-])/(Users|home|private|var|tmp|opt)/")


# ---------------------------------------------------------------------------------------------
# Reads: the ledger and the project store, read-only, never through the command under judgment

def state_root(world) -> Path:
    return world.home.joinpath(*STATE)


def ledger(world) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{state_root(world) / 'learning.sqlite3'}?mode=ro", uri=True)


def _rows(world, sql: str, params=()) -> list:
    connection = ledger(world)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def ledger_event_codes(world, project_id: str | None = None) -> dict:
    found: dict = {}
    for (content,) in _rows(world, "select content from records where record_type = 'event' and source = 'hook'"
                            + (" and project_id = ?" if project_id else ""), (project_id,) if project_id else ()):
        code = json.loads(bytes(content)).get("event_code")
        found[code] = found.get(code, 0) + 1
    return found


def ledger_ingestion(world) -> dict:
    return {code: count for code, count in _rows(world, "select event_code, accepted_count from hook_ingestion")}


def ledger_scalar(world, sql: str, params=()):
    return _rows(world, sql, params)[0][0]


def ledger_table_counts(world, names=("records", "projects", "stored_files")) -> dict:
    return {name: ledger_scalar(world, f'select count(*) from "{name}"') for name in names}


def ledger_project_ids(world) -> list[str]:
    return sorted(row[0] for row in _rows(world, "select project_id from projects"))


def event_rows(world) -> list[dict]:
    return [{"observed_at_ms": o, "ingested_at_ms": i, "expires_at_ms": e} for o, i, e in _rows(
        world, "select observed_at_ms, ingested_at_ms, expires_at_ms from records where record_type = 'event' order by source_sequence")]


def summary_count(world) -> int:
    return ledger_scalar(world, "select count(*) from records where record_type = 'event_summary'")


def stored_backups(world, project_id: str) -> list[dict]:
    return [{"file_id": f, "kind": k, "content_digest": d, "expires_at_ms": e, "relative_path": p} for f, k, d, e, p in _rows(
        world, "select file_id, kind, content_digest, expires_at_ms, relative_path from stored_files "
               "where project_id = ? and kind = 'backup' order by file_id", (project_id,))]


def store_files(world, project_id: str) -> list[str]:
    folder = state_root(world) / "projects" / project_id
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def store_identity_ids(world, project_id: str) -> list[str] | None:
    """The project ids the project store's own identity rows hold (None: the store cannot be read)."""
    path = state_root(world) / "projects" / project_id / "history.sqlite3"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return sorted(row[0] for row in connection.execute("select project_id from history_identity"))
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def store_integrity(world, project_id: str) -> str | None:
    path = state_root(world) / "projects" / project_id / "history.sqlite3"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return connection.execute("pragma integrity_check").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def checkout_project_ids(world) -> list[str]:
    """The ledger's project ids registered for this World's checkout, by its root (never by a directory name)."""
    import os
    roots = {str(world.project), os.path.realpath(world.project)}
    return sorted(pid for pid, root in _rows(world, "select project_id, canonical_root from projects where status = 'active'")
                  if root in roots)


def persisted_ids(world, project_id: str, record_type: str) -> list[str] | None:
    """The record ids of one type persisted for the project, in the shared ledger and in the project's own store (None:
    a store could not be read)."""
    found = [row[0] for row in _rows(world, "select record_id from records where project_id = ? and record_type = ?",
                                     (project_id, record_type))]
    path = state_root(world) / "projects" / project_id / "history.sqlite3"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            found += [row[0] for row in connection.execute(
                "select record_id from records where project_id = ? and record_type = ?", (project_id, record_type))]
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return sorted(found)


def store_identity(world, project_id: str) -> int | None:
    path = state_root(world) / "projects" / project_id / "history.sqlite3"
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute("select count(*) from history_identity").fetchone()[0]
    finally:
        connection.close()


def store_project_ids(world) -> list[str]:
    """The project ids the stores on disk say they hold (their own identity rows, not their directory names)."""
    folder = state_root(world) / "projects"
    found: list[str] = []
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        found += store_identity_ids(world, path.name) or []
    return sorted(found)


READ_CHUNK = 1 << 20


def sha256_file(path: Path) -> str | None:
    """The digest of a file, read in chunks; None when it cannot be read completely (never a digest of a part)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(READ_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def backup_manifest_sha256(path: Path) -> str | None:
    """The digest of the manifest a backup file stores in itself (its own `cocolo_backup_manifest` row); None if absent."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute("select manifest from cocolo_backup_manifest").fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if len(rows) != 1 or not isinstance(rows[0][0], bytes):
        return None
    return hashlib.sha256(rows[0][0]).hexdigest()


def state_files_holding(world, tokens) -> list[str]:
    """The names of the state's files that hold any planted token (names only), read in chunks. A file that cannot be
    read completely is reported as `unreadable:NAME` (an incomplete scan is never a clean one)."""
    found = []
    wanted = [token.encode() for token in tokens]
    overlap = max(len(token) for token in wanted) - 1
    for path in sorted(state_root(world).rglob("*")):
        if path.is_file() and not path.is_symlink():
            hit, tail = False, b""
            try:
                with open(path, "rb") as handle:
                    while chunk := handle.read(READ_CHUNK):
                        window = tail + chunk
                        if any(token in window for token in wanted):
                            hit = True
                            break
                        tail = window[-overlap:] if overlap else b""
            except OSError:
                found.append("unreadable:" + path.name)
                continue
            if hit:
                found.append(path.name)
    return found


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def step(world, *args: str, refuses: bool = False, timeout: float = 90) -> tuple[int, dict | None, str]:
    """An owner command of the installed CLI, judged like any step; World.cli itself captures a timeout with its
    partial output (G1, G8) and answers exit -1."""
    return world.cli(*args, refuses=refuses, timeout=timeout)


def hook_reads(world, project_id: str) -> dict:
    """What the hook counts are, by three paths: the collector's status, the ledger's event rows (by event code) and
    its ingestion table. Any of them None means it could not be read."""
    code, status, _ = step(world, "learning-status")
    ingestion = (status or {}).get("ingestion") if code == 0 else None
    counts = (ingestion or {}).get("hook_event_counts")
    return {"status": counts if isinstance(counts, dict) else None,
            "accepted": (ingestion or {}).get("accepted_hook_event_count"),
            "freshness": ((ingestion or {}).get("freshness") or {}).get("state"),
            "ledger": ledger_event_codes(world, project_id), "ingestion": ledger_ingestion(world),
            "sessions": ledger_scalar(world, "select count(*) from sessions where project_id = ?", (project_id,)),
            "secrets": state_files_holding(world, PLANTED)}


def nonzero(counts) -> dict | None:
    if not isinstance(counts, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values()):
        return None
    return {code: count for code, count in counts.items() if count}


def refused_for(result, part: str) -> bool:
    """A refusal that names its expected reason: exit 2, ready false, an error that holds `part` and no path of the
    machine (a reply that is missing, a crash or another reason does not count)."""
    code, reply, _ = result
    error = (reply or {}).get("error")
    return (code == 2 and isinstance(reply, dict) and reply.get("ready") is False and isinstance(error, str)
            and part in error and not PATH_IN_TEXT.search(error))


def hook_event(name: str, cwd: Path, **extra) -> dict:
    return {"session_id": "e2e-session-1", "cwd": str(cwd), "permission_mode": "default", "hook_event_name": name,
            "turn_id": "e2e-turn-1", "model": "gpt-6-luna", "transcript_path": None, **extra}


# ---------------------------------------------------------------------------------------------
# Scene 1: one project's learning (L10.1, L10.2, L10.3, L11.1)

def learn_scene():
    """The learning World, set up once; its sections (below) are built on first use by the check that needs them, so
    a step that breaks G1 or G8 (any step, in any World) is charged to the check that ran it."""
    if "learn" in _SCENES:
        return _SCENES["learn"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["learn"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    set_up(world)
    scene.other = add_project(world, "unregistered")  # committed, never set up: no recording for it
    scene.elsewhere = world.base / "elsewhere"
    scene.elsewhere.mkdir()
    world.git("init", "-q", project=scene.elsewhere)
    scene.project_id = ledger_project_ids(world)[0]
    return scene


def section(name: str):
    """The learning scene with section `name` built (a scene that already holds its fields is used as it is)."""
    scene = learn_scene()
    if name not in SECTION_FIELDS or hasattr(scene, SECTION_FIELDS[name]):
        return scene
    for needed in SECTION_NEEDS.get(name, ()):
        section(needed)
    SECTIONS[name](scene)
    return scene


def build_hooks(scene) -> None:
    """L10.1: events the hook does not record come first (outside the checkout, for a project that is not registered);
    the registered events that follow then show the pipeline alive and the counts exactly theirs."""
    world, project_id = scene.world, scene.project_id
    scene.before = hook_reads(world, project_id)

    def deliver(event, **where):
        code, out, _ = world.run_hook(event, **where)
        return code, out

    code, out = deliver(hook_event("UserPromptSubmit", scene.elsewhere, prompt=PLANTED[0]))
    scene.outside = {"code": code, "stdout": out, "after": hook_reads(world, project_id)}
    code, out = deliver(hook_event("UserPromptSubmit", scene.other.project, prompt=PLANTED[0]), checkout=scene.other.project)
    scene.unregistered = {"code": code, "stdout": out, "after": hook_reads(world, project_id)}
    scene.hook_results = []
    for name, extra in HOOK_EVENTS:
        code, out = deliver(hook_event(name, world.project, **extra))
        scene.hook_results.append((name, code, out))
    scene.on = hook_reads(world, project_id)


def build_migrate(scene) -> None:
    """L10.2: the project's history lives in its own store; the cycle runs there and leaves the shared ledger alone."""
    world, project_id = scene.world, scene.project_id
    migrate = SimpleNamespace()
    migrate.status = step(world, "learning-migrate-project", "--status")
    code, status, _ = step(world, "learning-status")
    migrate.learning_status_state = (status or {}).get("learning_migration") if code == 0 else None
    migrate.failure = (status or {}).get("learning_migration_failure") if code == 0 else "status_unreadable"
    migrate.store_files = store_files(world, project_id)
    migrate.store_identity = store_identity(world, project_id)
    migrate.store_identity_ids = store_identity_ids(world, project_id)
    migrate.checkout_ids = checkout_project_ids(world)
    migrate.cases_before = persisted_ids(world, project_id, "evaluation_case")
    migrate.candidates_before = persisted_ids(world, project_id, "candidate")
    migrate.ledger_project_ids = ledger_project_ids(world)
    migrate.store_project_ids = store_project_ids(world)
    migrate.ledger_before = ledger_table_counts(world)
    started = now_ms()
    migrate.run = step(world, "learning-run-cycle")
    migrate.window = (started, now_ms())
    migrate.ledger_after = ledger_table_counts(world)
    migrate.cases_after = persisted_ids(world, project_id, "evaluation_case")
    migrate.candidates_after = persisted_ids(world, project_id, "candidate")
    migrate.store_integrity_after = store_integrity(world, project_id)
    migrate.store_identity_ids_after = store_identity_ids(world, project_id)
    migrate.again = step(world, "learning-migrate-project")
    scene.migrate = migrate


def build_retention(scene) -> None:
    """L10.3: retention at the real clock keeps what has not expired; a clock in the future is refused."""
    world = scene.world
    retention = SimpleNamespace()
    retention.events_before = len(event_rows(world))
    retention.summaries_before = summary_count(world)
    rows = event_rows(world)
    retention.expiries = [row["expires_at_ms"] for row in rows]
    retention.ingested = [row["ingested_at_ms"] for row in rows]
    retention.real_now = now_ms()
    retention.now = step(world, "learning-expire", "--now-ms", str(retention.real_now))
    retention.events_after_now = len(event_rows(world))
    retention.past = step(world, "learning-expire", "--now-ms", str(retention.real_now - 600_000))
    retention.events_after_past = len(event_rows(world))
    retention.future = step(world, "learning-expire", "--now-ms", str(retention.real_now + 3_600_000), refuses=True)
    retention.events_after_future = len(event_rows(world))
    retention.summaries_after = summary_count(world)
    scene.retention = retention


def build_backup(scene) -> None:
    """L11.1: a backup, verified by the command and by independent reads of the file and the ledger."""
    world, project_id = scene.world, scene.project_id
    backup = SimpleNamespace()
    backup.create = step(world, "learning-backup-project", "--file-id", BACKUP_ID)
    # The file is found through the registry's relative path under the state root, never through a path in a reply.
    backup.stored = stored_backups(world, project_id)
    rows = [row for row in backup.stored if row["file_id"] == BACKUP_ID]
    relative = Path(rows[0]["relative_path"]) if len(rows) == 1 else None
    backup.from_registry = relative is not None and not relative.is_absolute() and ".." not in relative.parts
    path = state_root(world) / relative if backup.from_registry else Path("/nonexistent")
    backup.file_exists = path.is_file()
    backup.file_sha = sha256_file(path) if backup.file_exists else None
    backup.file_manifest_sha = backup_manifest_sha256(path) if backup.file_exists else None
    backup.file_integrity, backup.file_records = None, None
    if backup.file_exists:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                backup.file_integrity = connection.execute("pragma integrity_check").fetchone()[0]
                backup.file_records = connection.execute("select count(*) from records").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            backup.file_integrity = "unreadable"
    backup.ledger_records = ledger_scalar(world, "select count(*) from records where project_id = ?", (project_id,))
    backup.verify = step(world, "learning-verify-backup", "--file-id", BACKUP_ID)
    backup.unknown = step(world, "learning-verify-backup", "--file-id", "e2enever", refuses=True)
    backup.real_now = now_ms()
    scene.backup = backup


def build_short(scene) -> None:
    """L10.3: a backup made to live 60 seconds is removed by retention once its time has passed (real clock)."""
    world, project_id = scene.world, scene.project_id
    short = SimpleNamespace(events_before=len(event_rows(world)))
    made = step(world, "learning-backup-project", "--file-id", SHORT_BACKUP_ID, "--expires-in-seconds", str(SHORT_BACKUP_SECONDS))
    rows = [row for row in stored_backups(world, project_id) if row["file_id"] == SHORT_BACKUP_ID]
    short.expires_at_ms = rows[0]["expires_at_ms"] if made[0] == 0 and len(rows) == 1 else None
    kept = step(world, "learning-backup-project", "--file-id", LONG_BACKUP_ID)  # a week: it must still be there
    short.long_made = kept[0] == 0
    short_path = state_root(world) / rows[0]["relative_path"] if rows else None
    deadline = time.monotonic() + SHORT_BACKUP_SECONDS + 30
    while short.expires_at_ms is not None and now_ms() <= short.expires_at_ms and time.monotonic() < deadline:
        time.sleep(1.0)
    short.now_ms = now_ms()
    short.expire = step(world, "learning-expire", "--now-ms", str(short.now_ms))
    short.row_after = [row for row in stored_backups(world, project_id) if row["file_id"] == SHORT_BACKUP_ID]
    short.file_after = short_path.exists() if short_path is not None else None
    short.verify_after = step(world, "learning-verify-backup", "--file-id", SHORT_BACKUP_ID, refuses=True)
    short.long_rows_after = len([row for row in stored_backups(world, project_id) if row["file_id"] == LONG_BACKUP_ID])
    short.events_after = len(event_rows(world))
    scene.short = short


# A section's first field (a scene holding it is used as it is) and what must be built before it.
SECTIONS = {"hooks": build_hooks, "migrate": build_migrate, "retention": build_retention, "backup": build_backup,
            "short": build_short}
SECTION_FIELDS = {"hooks": "hook_results", "migrate": "migrate", "retention": "retention", "backup": "backup", "short": "short"}
SECTION_NEEDS = {"migrate": ("hooks",), "retention": ("hooks",), "backup": ("hooks",), "short": ("hooks",)}


def unchanged(after, base) -> bool:
    """A read taken after an event that must not be recorded: every count as before (zero), and every read present."""
    return (isinstance(after, dict) and nonzero(after.get("status")) == nonzero(base.get("status")) == {}
            and nonzero(after.get("ledger")) == {} and nonzero(after.get("ingestion")) == {}
            and after.get("accepted") == 0 and after.get("sessions") == 0 and after.get("secrets") == [])


@check("L10.1", group="learn")
def l10_1_hook_events_are_ingested_for_the_registered_checkout(phase: Phase) -> None:
    scene = section("hooks")
    before, on = scene.before, scene.on
    phase.expect(nonzero(before.get("status")) == {} and nonzero(before.get("ledger")) == {}
                 and nonzero(before.get("ingestion")) == {} and before.get("accepted") == 0 and before.get("sessions") == 0,
                 "no hook event was recorded before the events were delivered, by any of the three reads")
    names = [name for name, _extra in HOOK_EVENTS]
    phase.expect([name for name, _code, _out in scene.hook_results] == names, "the five Codex hook events were delivered")
    phase.expect(all(code == 0 for _name, code, _out in scene.hook_results), "every hook exits 0")
    phase.expect(all(out == ("{}\n" if name == "Stop" else "") for name, _code, out in scene.hook_results),
                 "a hook is quiet: only Stop answers, with an empty object")
    expected = {code: 1 for code in HOOK_CODES}
    phase.expect(nonzero(on.get("status")) == expected, "the collector's status counts each event once")
    phase.expect(nonzero(on.get("ledger")) == expected, "the ledger holds one raw event row of each kind")
    phase.expect(nonzero(on.get("ingestion")) == expected, "the ledger's ingestion table counts each event once")
    phase.expect(on.get("accepted") == 5, "the collector accepted exactly five events")
    phase.expect(on.get("freshness") == "fresh", "the collector reports the hooks as delivering")
    phase.expect(isinstance(on.get("sessions"), int) and on["sessions"] >= 1, "the ledger bound the hook's session")
    phase.expect(on.get("secrets") == [], "no prompt, command, tool output or answer reached the state")
    phase.reason("hook_events_ingested")


@check("L10.1", group="learn")
def l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded(phase: Phase) -> None:
    scene = section("hooks")
    # There is no recording switch (the owner dropped it, 2026-11-20); recording is "off" where the product does not
    # record: an event from a folder outside the checkout, and an event for a project that was never registered.
    for what, case in (("outside the checkout", scene.outside), ("for an unregistered project", scene.unregistered)):
        phase.expect(case.get("code") == 0 and case.get("stdout") == "", f"the hook exits 0 and says nothing for an event {what}")
        phase.expect(unchanged(case.get("after"), scene.before),
                     f"an event {what} is recorded by none of the three reads")
    phase.expect(nonzero(scene.on.get("ledger")) == {code: 1 for code in HOOK_CODES},
                 "the registered events delivered after them are the only ones counted")
    phase.reason("hook_events_not_recorded")


# ---------------------------------------------------------------------------------------------
# L10.2

@check("L10.2", group="learn")
def l10_2_history_lives_in_the_project_store_and_the_cycle_runs(phase: Phase) -> None:
    scene = section("migrate")
    migrate = scene.migrate
    code, reply, _ = migrate.status
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True and reply.get("state") == "complete",
                 "the migration status says the project's history has moved")
    phase.expect(migrate.learning_status_state == "complete", "the collector's own status says the move is complete")
    phase.expect(migrate.failure is None, "no migration failure is recorded")
    phase.expect("history.sqlite3" in (migrate.store_files or []) and migrate.store_identity == 1,
                 "the project's own store exists on disk and holds its identity")
    phase.expect(len(migrate.ledger_project_ids or []) == 1 and migrate.ledger_project_ids == migrate.store_project_ids
                 and migrate.ledger_project_ids == [scene.project_id],
                 "the one project of the shared ledger is the one with its own store")
    phase.expect(isinstance(migrate.store_identity_ids, list) and migrate.store_identity_ids == [scene.project_id]
                 and migrate.checkout_ids == [scene.project_id],
                 "the store's own identity row names the project the ledger registered for this checkout")
    code, reply, _ = migrate.run
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True, "learning-run-cycle runs")
    analysis = (reply or {}).get("analysis")
    as_of = (analysis or {}).get("as_of_ms")
    phase.expect(isinstance(analysis, dict) and analysis.get("schema_version") == 1 and isinstance(analysis.get("slices"), dict),
                 "the cycle's analysis has its closed shape")
    phase.expect(isinstance(as_of, int) and migrate.window[0] <= as_of <= migrate.window[1],
                 "the analysis is as of the moment the command ran")
    phase.expect(reply.get("proposal_status") == "complete" and isinstance(reply.get("mined_case_ids"), list)
                 and isinstance(reply.get("proposed_candidate_digests"), list) and reply.get("expired_records") == 0,
                 "the cycle finished its proposals and expired nothing from the project store")
    phase.expect(isinstance(migrate.ledger_before, dict) and migrate.ledger_before == migrate.ledger_after,
                 "the cycle did not touch the shared ledger")
    # What the cycle persisted is read from the stores, not from its reply: the cases and candidates it reports are
    # exactly the ones that appeared in the ledger or the project store, and the store is still sound.
    cases, candidates = (migrate.cases_before, migrate.cases_after), (migrate.candidates_before, migrate.candidates_after)
    phase.expect(all(isinstance(x, list) for x in (*cases, *candidates)), "the persisted cases and candidates were read")
    mined, proposed = (reply or {}).get("mined_case_ids"), (reply or {}).get("proposed_candidate_digests")
    phase.expect(all(isinstance(x, list) for x in (*cases, *candidates)) and isinstance(mined, list)
                 and sorted(set(cases[1]) - set(cases[0])) == sorted(mined),
                 "the cases the cycle reports mined are the cases that were persisted by it")
    phase.expect(all(isinstance(x, list) for x in (*cases, *candidates)) and isinstance(proposed, list)
                 and len(candidates[1]) - len(candidates[0]) == len(proposed),
                 "the candidates the cycle reports proposed are the candidates that were persisted by it")
    phase.expect(migrate.store_integrity_after == "ok" and migrate.store_identity_ids_after == [scene.project_id],
                 "after the cycle the project store is sound and still holds the project's identity")
    code, reply, _ = migrate.again
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True and reply.get("state") == "complete"
                 and reply.get("imported_records") == 0 and reply.get("ledger_retired") is True,
                 "moving again imports nothing and the shared ledger stays retired")
    phase.reason("history_in_project_store")


# ---------------------------------------------------------------------------------------------
# L10.3

@check("L10.3", group="learn")
def l10_3_unexpired_events_are_kept_and_a_future_clock_is_refused(phase: Phase) -> None:
    r = section("retention").retention
    phase.expect(isinstance(r.events_before, int) and r.events_before > 0, "raw events exist to be kept")
    stamps = list(zip(r.expiries or [], r.ingested or []))
    phase.expect(len(stamps) == r.events_before and all(isinstance(e, int) and isinstance(i, int) for e, i in stamps),
                 "every raw event carries an expiry")
    phase.expect(all(e - i >= SEVEN_DAYS_MS for e, i in stamps) and all(e > r.real_now for e, _ in stamps),
                 "a raw event lives at least seven days from its ingestion")
    for what, result in (("the real clock", r.now), ("a clock ten minutes back", r.past)):
        code, reply, _ = result
        phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True and reply.get("expired_records") == 0,
                     f"expiry at {what} expires nothing")
    phase.expect(r.events_after_now == r.events_before and r.events_after_past == r.events_before,
                 "every raw event is still in the ledger after expiry at the real clock and before it")
    phase.expect(refused_for(r.future, "input validation failed"), "a clock in the future is refused")
    phase.expect(r.events_after_future == r.events_before, "the refused expiry removed nothing")
    phase.expect(isinstance(r.summaries_after, int) and r.summaries_after == r.summaries_before,
                 "no summary was made for events that are kept")
    phase.reason("retention_keeps_unexpired")


@check("L10.3", group="learn")
def l10_3_an_expired_backup_is_removed_by_retention(phase: Phase) -> None:
    s = section("short").short
    phase.expect(isinstance(s.expires_at_ms, int) and isinstance(s.now_ms, int) and s.now_ms >= s.expires_at_ms,
                 "the clock of the expiry is at or past the backup's own expiry")
    code, reply, _ = s.expire
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True, "learning-expire runs")
    phase.expect(s.row_after == [], "retention removed the backup from the registry")
    phase.expect(s.file_after is False, "retention removed the backup file from the disk")
    phase.expect(refused_for(s.verify_after, "project backup is unavailable"), "the removed backup can no longer be verified")
    phase.expect(s.long_made is True and s.long_rows_after == 1, "the backup that has not expired is kept")
    phase.expect(isinstance(s.events_after, int) and s.events_after == s.events_before and s.events_before > 0,
                 "no raw event went with it")
    phase.reason("retention_removes_expired_backup")


def retain_scene():
    """Decision 193: a World whose raw-event window is scaled (retention_patch); real expiry through `learning-expire`."""
    if "retain" in _SCENES:
        return _SCENES["retain"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["retain"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, [retention_patch(), *fault_patches()])
    set_up(world)
    scene.project_id = ledger_project_ids(world)[0]
    for name, extra in HOOK_EVENTS:
        world.run_hook(hook_event(name, world.project, **extra))
    rows = event_rows(world)
    scene.expiries = [row["expires_at_ms"] for row in rows]
    scene.ingested = [row["ingested_at_ms"] for row in rows]
    deadline = time.monotonic() + 60
    while scene.expiries and all(isinstance(e, int) for e in scene.expiries) and now_ms() <= max(scene.expiries) \
            and time.monotonic() < deadline:
        time.sleep(0.5)
    scene.events_before = len(event_rows(world))
    scene.codes_before = ledger_event_codes(world, scene.project_id)
    scene.summaries_before = summary_count(world)
    scene.real_now = now_ms()
    scene.expire = step(world, "learning-expire", "--now-ms", str(scene.real_now))
    scene.events_after = len(event_rows(world))
    scene.summaries_after = summary_count(world)
    scene.summary_events, scene.summary_codes = 0, {}
    for (content,) in _rows(world, "select content from records where record_type = 'event_summary'"):
        record = json.loads(bytes(content))
        scene.summary_events += record.get("event_count") or 0
        for code, count in (record.get("event_counts") or {}).items():
            scene.summary_codes[code] = scene.summary_codes.get(code, 0) + count
    return scene


@check("L10.3", group="retain")
def l10_3_expired_raw_events_are_removed_and_their_summary_kept(phase: Phase) -> None:
    r = retain_scene()
    stamps = list(zip(r.expiries or [], r.ingested or []))
    phase.expect(len(stamps) > 0 and all(isinstance(e, int) and isinstance(i, int) for e, i in stamps),
                 "raw events exist and carry an expiry")
    phase.expect(all(0 < e - i < 60_000 for e, i in stamps), "the raw-event window is the test-scaled one (decision 193)")
    phase.expect(all(e <= r.real_now for e, _ in stamps), "every raw event is expired at the real clock")
    phase.expect(isinstance(r.events_before, int) and r.events_before == len(stamps) and r.events_before > 0
                 and isinstance(r.codes_before, dict) and sum(r.codes_before.values()) == r.events_before,
                 "the raw events are in the ledger just before the command")
    code, reply, _ = r.expire
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True
                 and isinstance(reply.get("expired_records"), int) and reply["expired_records"] >= r.events_before,
                 "learning-expire reports the expired raw events removed")
    phase.expect(r.events_after == 0, "the expired raw events are gone from the ledger")
    phase.expect(isinstance(r.summaries_after, int) and r.summaries_after > r.summaries_before,
                 "an event summary was kept")
    phase.expect(r.summary_events == r.events_before and r.summary_codes == r.codes_before,
                 "the kept summaries account for every removed event, by kind")
    phase.reason("raw_events_removed_summary_kept")


# ---------------------------------------------------------------------------------------------
# L11.1

@check("L11.1", group="learn")
def l11_1_backup_verifies_and_matches_the_ledger(phase: Phase) -> None:
    b = section("backup").backup
    code, reply, _ = b.create
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True and reply.get("verified") is True,
                 "learning-backup-project makes a backup and verifies it")
    phase.expect(b.from_registry is True and b.file_exists is True,
                 "the backup file registered under its relative path is on the disk (found through the registry, not the reply)")
    phase.expect(isinstance(b.file_sha, str) and reply.get("backup_sha256") == b.file_sha,
                 "the reply's digest is the digest of the file on the disk")
    phase.expect(isinstance(b.file_manifest_sha, str) and reply.get("manifest_sha256") == b.file_manifest_sha,
                 "the reply's manifest digest is the digest of the manifest stored in the file")
    phase.expect(reply.get("isolated_restore_integrity") == "ok" and b.file_integrity == "ok",
                 "the isolated restore and the file itself are sound")
    counts = reply.get("counts")
    phase.expect(isinstance(counts, dict) and isinstance(counts.get("records"), int) and counts["records"] > 0
                 and counts["records"] == b.file_records == b.ledger_records,
                 "the reply, the file and the ledger hold the same number of records")
    rows = [row for row in (b.stored or []) if row.get("file_id") == BACKUP_ID and row.get("kind") == "backup"]
    phase.expect(len(rows) == 1 and rows[0].get("content_digest") == b.file_sha,
                 "the ledger's registry holds the backup under its digest")
    phase.expect(len(rows) == 1 and isinstance(rows[0].get("expires_at_ms"), int) and rows[0]["expires_at_ms"] > b.real_now,
                 "the registered backup has not expired")
    code, again, _ = b.verify
    phase.expect(code == 0 and isinstance(again, dict) and again.get("ready") is True and again.get("verified") is True,
                 "learning-verify-backup succeeds")
    phase.expect(again.get("backup_sha256") == b.file_sha and isinstance(b.file_manifest_sha, str)
                 and again.get("manifest_sha256") == b.file_manifest_sha and again.get("counts") == counts, "verifying again agrees with the file and the backup's own manifest")
    phase.expect(refused_for(b.unknown, "project backup is unavailable"), "a backup that was never made is refused")
    phase.reason("backup_verified")


# ---------------------------------------------------------------------------------------------
# Scene 2: the session guard and the protected copy (L13.1, L13.2)

THREADS = {name: f"019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a{number:02x}" for number, name in enumerate(
    ("control_a", "control_b", "danger", "unknown_sandbox", "no_record", "home", "above_home", "project_parent"))}
REASON_TEXT = {
    "danger": "runs without its sandbox (danger-full-access)",
    "unknown_sandbox": "not in the format the orchestrator knows",
    "no_thread": "CODEX_THREAD_ID is not set",
    "no_record": "no record of this session was found",
    "home": "can write to your home folder",
    "above_home": "holds your home folder",
    "project_parent": "opened on a folder that contains this project",
}


def guard_scene():
    if "guard" in _SCENES:
        return _SCENES["guard"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["guard"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    a = project_view(world)
    b = add_project(world, "project-b")
    nest = world.base / "nest"  # a parent folder that holds a checkout and NOT the home folder (project_parent)
    nest.mkdir()
    c = add_project(world, "nest/project-c")
    for view in (a, b, c):  # every setup first: a later setup restarts the shared collector
        set_up(view)
    copies = world.protected_copies()
    import os
    real_a, real_b = os.path.realpath(a.project), os.path.realpath(b.project)
    scene.copies = {"a": str(copies.get(real_a)), "b": str(copies.get(real_b))}
    scene.bound = {"a": real_a if real_a in copies else None, "b": real_b if real_b in copies else None}
    world.codex_session(a.project, thread=THREADS["control_a"])
    world.codex_session(b.project, thread=THREADS["control_b"])
    status_a = ("learning-status", "--project-root", str(a.project))
    status_b = ("learning-status", "--project-root", str(b.project))
    # The two copies serve their own projects (controls), before the ledger is counted.
    scene.other = {"own": world.copy_cli(a.project, *status_a, thread=THREADS["control_a"]),
                   "b_own": world.copy_cli(b.project, *status_b, thread=THREADS["control_b"])}
    scene.cases = {"control": scene.other["own"]}
    scene.ledger_before = ledger_table_counts(world, ("projects", "records"))
    world.codex_session(a.project, thread=THREADS["danger"], sandbox="danger-full-access", disabled_profile=True)
    world.codex_session(a.project, thread=THREADS["unknown_sandbox"], sandbox="external-sandbox")
    world.codex_session(world.home, thread=THREADS["home"])
    world.codex_session(world.base, thread=THREADS["above_home"])
    world.codex_session(nest, thread=THREADS["project_parent"])
    scene.cases["danger"] = world.copy_cli(a.project, *status_a, thread=THREADS["danger"], refuses=True)
    scene.cases["unknown_sandbox"] = world.copy_cli(a.project, *status_a, thread=THREADS["unknown_sandbox"], refuses=True)
    scene.cases["no_thread"] = world.copy_cli(a.project, *status_a, thread=None, refuses=True)
    scene.cases["no_record"] = world.copy_cli(a.project, *status_a, thread=THREADS["no_record"], refuses=True)
    scene.cases["home"] = world.copy_cli(a.project, *status_a, thread=THREADS["home"], refuses=True)
    scene.cases["above_home"] = world.copy_cli(a.project, *status_a, thread=THREADS["above_home"], refuses=True)
    scene.cases["project_parent"] = world.copy_cli(c.project, "learning-status", "--project-root", str(c.project),
                                                   thread=THREADS["project_parent"], refuses=True)
    scene.other["project"] = world.copy_cli(a.project, *status_b, thread=THREADS["control_a"], refuses=True)
    scene.other["state"] = world.copy_cli(a.project, *status_a, "--state-root", str(world.base / "other-state"),
                                          thread=THREADS["control_a"], refuses=True)
    scene.other["b_for_a"] = world.copy_cli(b.project, *status_a, thread=THREADS["control_b"], refuses=True)
    scene.ledger_after = ledger_table_counts(world, ("projects", "records"))
    return scene


@check("L13.1", group="guard")
def l13_1_session_guard_refuses_each_unsafe_session_with_its_reason(phase: Phase) -> None:
    scene = guard_scene()
    code, reply, _ = scene.cases["control"]
    phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True,
                 "a sandboxed session on the project itself is served")
    for name, text in REASON_TEXT.items():
        phase.expect(refused_for(scene.cases.get(name), text), f"the {name.replace('_', ' ')} session is refused with its reason")
        phase.reason("guard_refused_" + name)
    phase.expect(isinstance(scene.ledger_before, dict) and scene.ledger_before == scene.ledger_after,
                 "the refusals changed nothing in the ledger")


@check("L13.2", group="guard")
def l13_2_protected_copy_refuses_a_project_it_is_not_bound_to(phase: Phase) -> None:
    scene = guard_scene()
    phase.expect(scene.bound.get("a") is not None and scene.bound.get("b") is not None and scene.bound["a"] != scene.bound["b"]
                 and scene.copies.get("a") != scene.copies.get("b"),
                 "there are two protected copies, each bound to its own project (read from the binding files)")
    for name in ("own", "b_own"):
        code, reply, _ = scene.other[name]
        phase.expect(code == 0 and isinstance(reply, dict) and reply.get("ready") is True,
                     "each copy serves the project it is bound to")
    phase.expect(refused_for(scene.other.get("project"), "--project-root is refused"),
                 "the copy of one project refuses to serve the other")
    phase.expect(refused_for(scene.other.get("b_for_a"), "--project-root is refused"),
                 "the other copy refuses the first project too")
    phase.expect(refused_for(scene.other.get("state"), "--state-root is refused"),
                 "the copy refuses another state root")
    phase.expect(isinstance(scene.ledger_before, dict) and scene.ledger_before == scene.ledger_after,
                 "the refusals changed nothing in the ledger")
    phase.reason("copy_bound_to_own_project")
