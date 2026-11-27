#!/usr/bin/env python3
"""Create and verify a project-partition backup with the installed runtime.

The backup is the streamed format-3 file (one private SQLite copy of the
partition) that the ledger itself writes before an upgrade (PLAN F3). It is
written and checked by this clone's own project_backup in a separate isolated
Python, so a runtime without a streamed writer (0.2.x, 0.3.0) can be backed up
at any size; the installed runtime holds the collector's maintenance lock
throughout and registers the finished file in its own ledger.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

SCRIPT = Path(__file__).resolve()
# This clone's runtime package: its project_backup writes and checks the copy.
CLONE_PACKAGE = SCRIPT.parents[1] / "native-package" / ".codex" / "orchestration"
# The ledger's own limit for one project's backup (learning_store.MAX_PROJECT_BACKUP_BYTES).
BACKUP_LIMIT_BYTES = 16 * 1024 ** 3
# How long the copy and its checks may take; a hung child must not hold the collector stopped (F3-9).
CHILD_SECONDS = 4 * 3600
_FILE_ID = re.compile(r"[A-Za-z0-9_]{1,160}")


_FIXED_REASONS = frozenset({
    "collector runtime authorization is invalid",
    "the earlier authorization could not be put back",
    "the earlier authorization could not be put back; a temporary copy remains",
    "installed ledger schema does not support this backup",
    "a folder on the way to the backup is a link",
    "a folder on the way to the backup is not a private folder of this account",
    "the backup copy's check did not report everything it must",
    "backup file id must be 1 to 160 letters, digits or underscores",
    "the backup folder is not inside the records folder",
    "a backup with this file id already exists",
    "the backup copy's columns differ from the installed schema's columns",
    "the backup copy is not the file that was checked",
    "the backup copy changed after it was checked",
    "a backup with this file id appeared while it was written",
    "the backup was stopped by a signal; nothing was kept",
    "stopped by a signal",
    "the backup copy is not the file the script created",
    "the backup copy is not the private file the script created",
    "the project's backup is over the ledger's backup limit",
    "the backup copy changed while it was checked",
    "backup expiry must be at least 60 seconds",
    "installed orchestration runtime is unavailable",
    "project backup cannot represent projects exactly",
    "project backup cannot represent the ledger's tables exactly",
    "project binding is not active",
})
_FIXED_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"the backup copy was stopped at its time limit \(\d+ s\)",
    r"the backup copy could not be written or checked \(exit -?\d+\)",
    r"the backup could not be completed \(\w+\)",
    r"the project's backup is planned at \d+ bytes, over the limit of \d+",
    r"the project's backup is planned at \d+ bytes and needs \d+ bytes free; not enough disk space",
    r"project backup cannot represent [a-z_]+ exactly",
))
_CLEANUP = re.compile(r"(.*); \d+ partial file\(s\) in the backup folder could not be removed", re.S)


def _fixed(text: str) -> bool:
    if text in _FIXED_REASONS or any(pattern.fullmatch(text) for pattern in _FIXED_PATTERNS):
        return True
    cleanup = _CLEANUP.fullmatch(text)
    return cleanup is not None and _fixed(cleanup.group(1))


def _reason(error: BaseException) -> str:
    """A fixed reason for a reply (decisions 36, 192): only an allowlist of this script's own fixed reason
    strings passes through; any other text (filesystem, ledger, child, parser, an unlisted RuntimeError) can
    hold a path or a private value, so it becomes one fixed generic sentence."""
    if isinstance(error, RuntimeError) and _fixed(str(error)):
        return str(error)
    return f"the backup could not be completed ({type(error).__name__})"


class QuiesceLeftStopped(RuntimeError):
    """Quiescing failed after the authorization file was replaced, and the earlier one could not be put
    back: the collector stays stopped (F3-12)."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _quiesce_collector_authorization(path: Path) -> None:
    """Make both old and new runtimes fail closed until owner registration."""

    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError("collector runtime authorization is invalid")
    quiesced = value.get("quiesced_for_upgrade") is True
    expected = {
        "schema_version", "runtime_version", "runtime_digest",
        *(('quiesced_for_upgrade',) if quiesced else ()),
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or not isinstance(value.get("runtime_version"), str)
        or not isinstance(value.get("runtime_digest"), str)
        or len(value["runtime_digest"]) != 64
        or any(character not in "0123456789abcdef" for character in value["runtime_digest"])
    ):
        raise RuntimeError("collector runtime authorization is invalid")
    if quiesced:  # Already as asked: nothing to change (F3-17).
        return
    value["quiesced_for_upgrade"] = True
    _write_authorization(path, _canonical_json(value) + b"\n")


def _write_authorization(path: Path, content: bytes) -> None:
    """Replace the authorization file with `content`; a failure after the replacement puts the previous
    file back (F3-12), and if that fails too, says the collector is left stopped."""
    previous = path.read_bytes()
    temporary = path.parent / f".collector-authorization.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
            root_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(root_descriptor)
            finally:
                os.close(root_descriptor)
        except BaseException as error:
            restore = path.parent / f".collector-authorization.{secrets.token_hex(8)}.tmp"
            try:
                restore.write_bytes(previous)
                restore.chmod(0o600)
                os.replace(restore, path)
            except BaseException:
                outcome = QuiesceLeftStopped("the earlier authorization could not be put back")
                try:
                    restore.unlink(missing_ok=True)
                except OSError:
                    outcome = QuiesceLeftStopped("the earlier authorization could not be put back; a temporary copy remains")
                raise outcome from error
            try:
                restore.unlink(missing_ok=True)  # F3-17: no temporary copy left behind
            except OSError:
                pass  # The rollback itself succeeded; a stray copy is harmless
            raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Never replaces the outcome above; a stray copy changes nothing the collector reads.


# E2a-1: the machine-local launch record columns (an installed 0.8.60+ ledger has both; older ones neither).
LOCAL_PROJECT_COLUMNS = frozenset({"launch_copy", "launch_digest"})
# 0.8.60 fix round (decision 247): the machine-local tables, by exact columns (the product's own
# learning_store.LOCAL_TABLE_COLUMNS), and the other tables a ledger legitimately holds outside the backup.
LOCAL_TABLE_COLUMNS = {
    "project_wake": ("project_id", "allocated_epoch", "epoch", "seq", "state", "next_due_at_ms", "body_digest",
                     "due_at_ms", "refused", "updated_at_ms"),
    "project_wake_instances": ("project_id", "instance", "epoch"),
    "project_starts": ("project_id", "attempt", "claimed_at_ms", "child_pid", "failures", "streak_first_ms",
                       "next_start_at_ms", "parked_reason", "parked_digest", "outcome", "reason", "outcome_at_ms",
                       "started_at_ms", "probation_attempt", "probation_pid", "probation_until_ms", "updated_at_ms"),
}
# Decision 249: those other tables by exact columns too (the runtime derives them from its own CREATE statements as
# learning_store.OTHER_TABLE_COLUMNS; a test pins this copy equal to it).
OTHER_TABLE_COLUMNS = {
    'runtime_floor': ('singleton', 'floor_version'),
    'runtime_lineage': ('predecessor_digest', 'predecessor_version', 'successor_digest',
                        'successor_version', 'epoch', 'takeover_id', 'recorded_at_ms'),
    "runtime_takeover": ("singleton", "epoch", "old_digest", "new_digest"),
    "additive_migrations": ("feature_key",),
    "owner_key_history": ("sequence", "signer_line", "reason", "recorded_at_ms"),
    "admission_evidence": ("project_id", "record_id", "record_type", "source", "observed_at_ms", "provider",
                           "account_fingerprint", "live_until_ms", "claude_pass"),
}
_OTHER_TABLES = frozenset(OTHER_TABLE_COLUMNS)
_PROJECT_COLUMNS = (
    "project_id", "canonical_root", "root_digest", "status", "created_at_ms",
    "deleted_at_ms",
)
_TABLE_COLUMNS = {
    "sessions": ("session_digest", "producer", "created_at_ms"),
    "run_provenance": (
        "run_id", "producer", "client_version", "session_digest", "created_at_ms",
    ),
    "records": (
        "record_id", "record_type", "source", "source_sequence", "observed_at_ms",
        "ingested_at_ms", "expires_at_ms", "content", "content_digest", "late", "probe_id",
    ),
    "record_links": ("from_record_id", "to_record_id", "relation"),
    "stored_files": (
        "file_id", "kind", "relative_path", "content_digest", "source_record_id",
        "expires_at_ms",
    ),
    "collector_health": ("code", "count", "updated_at_ms"),
    "consumed_grants": (
        "grant_id", "promotion_record_id", "candidate_digest", "action",
        "consumed_at_ms",
    ),
    "owner_attestations": ("subject_record_id", "signature", "attested_at_ms"),
    "repair_receipts": (
        "receipt_id", "assignment_digest", "reason_code", "issued_at_ms",
        "expires_at_ms", "consumed_at_ms", "run_id",
    ),
    "launch_claims": ("handle_digest", "run_id", "kind", "claimed_at_ms", "bridge_run_id"),
    "launch_inputs": ("handle_digest", "run_id", "runtime_digest", "provider", "model", "effort", "hard_timeout_seconds"),
    "launch_observations": ("handle_digest", "identity_digest", "observed_at_ms"),
    "launch_receipts": ("scope_digest", "request_digest", "parameters_digest", "handle_digest", "receipt_digest",
                        "run_id", "state", "reason_code", "bridge_run_id", "deadline_at_ms", "recorded_at_ms"),
    "active_run_capacity": ("handle_digest", "run_id", "bridge_run_id", "kind", "provider", "account_fingerprint",
                            "runtime_digest", "policy_digest", "claimed_at_ms", "deadline_at_ms",
                            "clock_reference_ns", "execution_deadline_ns", "clock_owner_digest", "released_at_ms", "release_digest"),
    "capacity_completions": ("handle_digest", "run_id", "bridge_run_id", "terminal_record_id", "terminal_content_digest",
                             "released_at_ms", "receipt_digest"),
    "capacity_executions": ("handle_digest", "run_id", "bridge_run_id", "owner_digest", "cleanup_reservation_digest"),
    "account_cleanup_receipts": ('handle_digest', 'run_id', 'bridge_run_id', 'registration_digest', 'lease_digest',
        'binding_digest', 'reservation_digest', 'terminal_record_id', 'terminal_content_digest', 'result_digest',
        'terminal_receipt_digest', 'terminal_recorded_at_ms', 'cleanup_digest', 'released_at_ms', 'receipt_digest', 'state_seal'),
    "account_observation_origins": ('request_key', 'parameters_digest', 'observed_at_ms', 'origin_seal'),
    "account_observation_requests": ('request_key', 'parameters_digest', 'runtime_digest', 'caller_scope', 'provider',
        'handle_digest', 'run_id', 'phase', 'observed_at_ms', 'deadline_at_ms', 'state', 'references_json', 'state_seal'),
    "account_billing_probe_origins": ('request_key', 'parameters_digest', 'created_at_ms', 'origin_seal'),
    "account_billing_probe_requests": ('request_key', 'parameters_digest', 'runtime_digest', 'caller_scope',
        'created_at_ms', 'deadline_at_ms', 'state', 'account_fingerprint', 'admission_record_id',
        'admission_digest', 'references_json', 'state_seal'),
    "run_holds": ("kind", "identity", "state", "generation", "taken_at_ms", "released_at_ms", "release_kind",
                  "receipt", "evidence", "next_check_ms"),
    "host_run_bindings": (
        "handle_digest", "run_id", "assignment_digest", "task_digest",
        "task_id_digest", "route_digest", "configuration_digest", "client_version",
        "created_at_ms", "expires_at_ms", "consumed_at_ms", "orchestration_id",
        "native_assignment_id", "agent_type", "usage_complete", "binding_state",
        "abandoned_reason", "observation_started_at_ms",
        "observation_deadline_at_ms", "last_progress_at_ms", "deadline_observed",
        "history_owner",
    ),
    "hook_ingestion": ("event_code", "accepted_count", "last_accepted_at_ms"),
    "orchestrations": (
        "orchestration_id", "handle_digest", "client_version", "status",
        "started_at_ms", "ended_at_ms", "goal_tokens_used",
        "goal_time_used_seconds", "expires_at_ms",
    ),
    "orchestration_checkpoints": (
        "orchestration_id", "sequence", "status", "goal_tokens_used",
        "goal_time_used_seconds", "observed_at_ms",
    ),
    "operational_qa": (
        "qa_id", "qa_digest", "host_outcome_digest",
        "verifier_host_outcome_digest", "accepted", "observed_at_ms", "content",
    ),
    "operational_check_receipts": (
        "receipt_id", "report_digest", "host_outcome_digest",
        "verifier_host_outcome_digest", "orchestration_id", "issued_at_ms",
        "expires_at_ms", "consumed_at_ms", "report",
    ),
    "tombstones": ("deletion_id", "deleted_at_ms"),
    "account_open_receipts": ("handle_digest", "run_id", "request_key", "scope_digest", "parameters_digest", "content", "content_seal"),
    "account_open_requests": ("request_key", "original_key", "parameters_digest", "handle_digest", "run_id", "state", "opened_at_ms",
                              "observation_deadline_at_ms", "receipt_digest", "request_seal"),
}
_TABLE_ORDER = {
    "sessions": ("session_digest", "producer"),
    "run_provenance": ("run_id", "producer"),
    "records": ("source_sequence",),
    "record_links": ("from_record_id", "to_record_id", "relation"),
    "stored_files": ("file_id",),
    "collector_health": ("code",),
    "consumed_grants": ("grant_id",),
    "owner_attestations": ("subject_record_id",),
    "repair_receipts": ("receipt_id",),
    "host_run_bindings": ("created_at_ms", "run_id"),
    "launch_claims": ("handle_digest",),
    "launch_inputs": ("handle_digest",),
    "launch_observations": ("handle_digest",),
    "launch_receipts": ("scope_digest", "request_digest"),
    "active_run_capacity": ("handle_digest",),
    "capacity_completions": ("handle_digest",),
    "capacity_executions": ("handle_digest",),
    "account_cleanup_receipts": ("handle_digest",),
    "account_observation_origins": ("request_key",),
    "account_observation_requests": ("request_key",),
    "account_billing_probe_origins": ("request_key",),
    "account_billing_probe_requests": ("request_key",),
    "run_holds": ("kind", "identity"),
    "hook_ingestion": ("event_code",),
    "orchestrations": ("started_at_ms", "orchestration_id"),
    "orchestration_checkpoints": ("orchestration_id", "sequence"),
    "operational_qa": ("observed_at_ms", "qa_id"),
    "operational_check_receipts": ("issued_at_ms", "receipt_id"),
    "tombstones": ("deletion_id",),
    "account_open_receipts": ("handle_digest",),
    "account_open_requests": ("request_key",),
}


def _expected_columns(schema_version: int, table: str) -> tuple[str, ...]:
    if schema_version < 16 and table == "records":
        return _TABLE_COLUMNS[table][:-1]  # PC-4: probe_id arrived with schema 16
    if schema_version < 15 and table == "run_holds":
        return ()
    if schema_version < 13 and table in {'account_billing_probe_origins', 'account_billing_probe_requests'}:
        return ()
    if schema_version < 12 and table in {'account_observation_origins', 'account_observation_requests'}:
        return ()
    if schema_version < 11 and table == 'capacity_executions':
        return _TABLE_COLUMNS[table][:-1] if schema_version >= 9 else ()
    if schema_version < 9 and table in {"active_run_capacity", "capacity_completions", "capacity_executions"}:
        return ()
    if schema_version < 8 and table in {"launch_inputs", "launch_observations", "launch_receipts"}:
        return ()
    if schema_version < 7 and table == "launch_claims":
        return ()
    if schema_version == 5:
        if table == "host_run_bindings":
            return tuple(column for column in _TABLE_COLUMNS[table] if column not in {
                "binding_state", "abandoned_reason", "observation_started_at_ms",
                "observation_deadline_at_ms", "last_progress_at_ms",
                "deadline_observed", "history_owner",
            })
        if table == "orchestration_checkpoints":
            return ()
    if schema_version < 10 and table == 'host_run_bindings':
        return tuple(n for n in _TABLE_COLUMNS[table] if n != 'history_owner')
    return _TABLE_COLUMNS[table]


def _tables(schema_version: int) -> tuple[str, ...]:
    if schema_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18}:
        raise RuntimeError("installed ledger schema does not support this backup")
    return tuple(table for table in _TABLE_COLUMNS
                 if (schema_version >= 7 or table != "launch_claims")
                 and (schema_version >= 8 or table not in {
                     "launch_inputs", "launch_observations", "launch_receipts"})
                 and (schema_version >= 9 or table not in {"active_run_capacity", "capacity_completions", "capacity_executions"})
                 and (schema_version >= 10 or table not in {'account_open_receipts', 'account_open_requests'})
                 and (schema_version >= 11 or table != 'account_cleanup_receipts')
                 and (schema_version >= 12 or table not in {'account_observation_origins', 'account_observation_requests'})
                 and (schema_version >= 13 or table not in {'account_billing_probe_origins', 'account_billing_probe_requests'})
                 and (schema_version >= 15 or table != 'run_holds'))


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_DIGEST = re.compile(r"[0-9a-f]{64}")


class _Stopped(BaseException):
    """A termination signal arrived during the backup."""


def _private_folder(path: Path, *, owner_only: bool) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError("a folder on the way to the backup is a link")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (owner_only and info.st_mode & 0o077):
        raise RuntimeError("a folder on the way to the backup is not a private folder of this account")


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def _checked_report(child: object) -> dict[str, object]:
    """Every field the child must report, checked before anything is published (F3-3c)."""
    if (not isinstance(child, dict)
            or not isinstance(child.get("backup_sha256"), str) or not _DIGEST.fullmatch(child["backup_sha256"])
            or not isinstance(child.get("manifest_sha256"), str) or not _DIGEST.fullmatch(child["manifest_sha256"])
            or not isinstance(child.get("counts"), dict)
            or not all(isinstance(k, str) and type(v) is int for k, v in child["counts"].items())
            or not isinstance(child.get("identity"), list) or len(child["identity"]) != 2
            or not all(type(part) is int for part in child["identity"])
            or not isinstance(child.get("columns"), dict)
            or not all(isinstance(k, str) and isinstance(v, list) for k, v in child["columns"].items())):
        raise RuntimeError("the backup copy's check did not report everything it must")
    return child


def check_inventory(connection, schema_version: int, *, exact_columns: bool = True) -> None:
    """Refuse a ledger the backup cannot represent exactly: every table it holds is a backed-up table, the
    projects table, a known machine-local table or another known non-partition table (sqlite_ internal
    tables aside), and each one that is backed up or local has exactly its columns, generated and hidden
    columns included (table_xinfo). Nothing is written before this passes (0.8.60 fix round, decision 247)."""
    def named(table):
        return [(str(row[1]), int(row[6])) for row in sorted(
            connection.execute(f"PRAGMA table_xinfo({table})").fetchall(), key=lambda row: row[0])]
    backed = set(_TABLE_COLUMNS)  # any version's backed-up table is known; the exact ones are checked below
    found = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
             if not str(row[0]).startswith("sqlite_")}
    if found - backed - {"projects"} - set(LOCAL_TABLE_COLUMNS) - _OTHER_TABLES:
        raise RuntimeError("project backup cannot represent the ledger's tables exactly")
    exact = {**LOCAL_TABLE_COLUMNS, **OTHER_TABLE_COLUMNS}
    for table in sorted(found & set(exact)):
        columns = named(table)
        if tuple(name for name, _ in columns) != exact[table] or any(hidden for _, hidden in columns):
            raise RuntimeError(f"project backup cannot represent {table} exactly")
    projects = named("projects")
    names = {name for name, _ in projects}
    if LOCAL_PROJECT_COLUMNS <= names:
        names -= LOCAL_PROJECT_COLUMNS
    if names != set(_PROJECT_COLUMNS) or any(hidden for _, hidden in projects):
        raise RuntimeError("project backup cannot represent projects exactly")
    for table in sorted(found & backed):
        if any(hidden for _, hidden in named(table)):
            raise RuntimeError(f"project backup cannot represent {table} exactly")
    if not exact_columns:
        return
    for table in _tables(schema_version):
        columns = _expected_columns(schema_version, table)
        available = named(table) if table in found else []
        if ({name for name, _ in available} != ({"project_id", *columns} if columns else set())
                or any(hidden for _, hidden in available)):
            raise RuntimeError(f"project backup cannot represent {table} exactly")


def backup_partition(ledger, project_id: str, file_id: str, *, schema_version: int,
                     expires_at_ms: int) -> dict[str, object]:
    """Write, check and register one partition's format-3 backup (PLAN F3-1 to F3-3).

    `ledger` is the installed runtime's open ledger, whose collector the caller
    holds stopped. The copy is made in a separate isolated Python with this
    clone's project_backup, which checks it through one descriptor and equal to
    the live partition and reports the file's identity and digest. Only that
    file, reopened without following links, is published (never replacing
    anything) and registered. Whatever fails, a termination signal included,
    the child is stopped and reaped and nothing this run created is left.
    """
    if not _FILE_ID.fullmatch(file_id):
        raise RuntimeError("backup file id must be 1 to 160 letters, digits or underscores")
    check_inventory(ledger._connection, schema_version, exact_columns=False)  # before anything is created
    root = ledger.state.root
    backups = ledger.state.backups
    partition = backups / project_id
    if backups.parent != root or partition.parent != backups:
        raise RuntimeError("the backup folder is not inside the records folder")
    _private_folder(root, owner_only=False)
    _private_folder(backups, owner_only=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(partition, 0o700)
    _private_folder(partition, owner_only=True)
    folder = os.open(partition, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    final_name, partial_name = f"{file_id}.sqlite3", f".{file_id}.partial"
    created = published = None
    child = None
    # F3-15: stop signals are held throughout and released only while the child runs. A stop held
    # after registration is taken off the queue and noted instead.
    found_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    committed, late_stop, previous = [], [], {}
    try:
        if threading.current_thread() is threading.main_thread():
            def stop(signum, frame):
                if committed:
                    late_stop.append(signum)
                    return
                raise _Stopped()
            for number in _STOP_SIGNALS:
                previous[number] = signal.signal(number, stop)
        if os.path.lexists(partition / final_name):
            raise RuntimeError("a backup with this file id already exists")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(partial_name, dir_fd=folder)  # Left by a backup that was stopped; never registered.
        descriptor = os.open(partial_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=folder)
        try:
            created = _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        argv = [sys.executable, "-I", "-B", "-S", str(SCRIPT), "--stream-child", str(ledger.state.database),
                str(partition / partial_name), project_id, str(schema_version), str(BACKUP_LIMIT_BYTES),
                f"{created[0]}:{created[1]}"]
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)  # The one window a stop acts in.
            out, err = child.communicate(timeout=CHILD_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"the backup copy was stopped at its time limit ({CHILD_SECONDS} s)") from None
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
        try:
            report = json.loads(out)
        except (ValueError, RecursionError):
            report = None
        if child.returncode != 0:
            if isinstance(report, dict) and report.get("error"):
                error_text = report["error"]
                raise RuntimeError(error_text if isinstance(error_text, str) and _fixed(error_text)
                                   else "the backup could not be completed (ChildError)")  # allowlist only
            raise RuntimeError(f"the backup copy could not be written or checked (exit {child.returncode})")
        report = _checked_report(report)
        # The copy has exactly the columns this script checked against the installed schema (F3-6).
        expected = {table: list(_expected_columns(schema_version, table)) for table in _tables(schema_version)}
        if report["columns"] != expected:
            raise RuntimeError("the backup copy's columns differ from the installed schema's columns")
        # The file published is the file the child checked: same identity, same bytes.
        descriptor = os.open(partial_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=folder)
        try:
            info = os.fstat(descriptor)
            if (_identity(info) != created or report["identity"] != created or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise RuntimeError("the backup copy is not the file that was checked")
            hasher = hashlib.sha256()
            while chunk := os.read(descriptor, 1_048_576):
                hasher.update(chunk)
            if hasher.hexdigest() != report["backup_sha256"]:
                raise RuntimeError("the backup copy changed after it was checked")
        finally:
            os.close(descriptor)
        # Published by a new name for the same file: never replaces anything that appeared meanwhile.
        try:
            os.link(partial_name, final_name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
            published = created
        except FileExistsError:
            raise RuntimeError("a backup with this file id appeared while it was written") from None
        os.unlink(partial_name, dir_fd=folder)
        os.fsync(folder)
        path = partition / final_name
        ledger._register_stored_file(project_id, file_id=file_id, kind="backup", path=path,
                                     digest=report["backup_sha256"], expires_at_ms=expires_at_ms)
        committed.append(True)
        if previous:  # A stop held since the child ended is taken now, and only noted.
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)
            signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
        return {**({"stopped_after_registration": True} if late_stop else {}),
            "ready": True,
            "project_id": project_id,
            "file_id": file_id,
            "backup_name": path.name,
            "format": 3,
            "ledger_schema_version": schema_version,
            "backup_sha256": report["backup_sha256"],
            "manifest_sha256": report["manifest_sha256"],
            "counts": report["counts"],
            "isolated_restore_integrity": "ok",
            "isolated_restore_foreign_keys": "ok",
        }
    except BaseException as error:
        signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)  # A second stop cannot cut cleanup short.
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        # Remove only what this run created: a name whose file is still ours.
        remaining = []
        for name, ours in ((final_name, published), (partial_name, created)):
            if ours is None:
                continue
            try:
                if _identity(os.stat(name, dir_fd=folder, follow_symlinks=False)) == ours:
                    os.unlink(name, dir_fd=folder)
            except FileNotFoundError:
                pass
            except OSError:
                remaining.append(name)
        if remaining:  # F3-14: say what is left rather than claim nothing was kept.
            raise RuntimeError(f"{'stopped by a signal' if isinstance(error, _Stopped) else _reason(error)}; "
                               f"{len(remaining)} partial file(s) in the backup folder could not be removed") from None
        if isinstance(error, _Stopped):
            raise RuntimeError("the backup was stopped by a signal; nothing was kept") from None
        raise
    finally:
        try:
            os.close(folder)
        except OSError:
            pass  # A folder descriptor that will not close changes nothing on disk.
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, found_mask)


def finish_backup(report: dict[str, object], *, leave_quiesced: bool, quiesce) -> tuple[dict[str, object], int]:
    """The final report and exit status once the backup is registered (F3-3e).

    With `leave_quiesced`, the collector is quiesced only now; if that fails the
    backup stays made and registered, the report says the collector was not
    left stopped, and the status is 3.
    """
    if not leave_quiesced:
        return {**report, "collector_quiesced": False}, 0
    try:
        quiesce()
    except QuiesceLeftStopped as exc:  # F3-12: replaced and not put back, so it is stopped
        return {**report, "collector_quiesced": True, "quiesce_error": _reason(exc)}, 3
    except Exception as exc:
        return {**report, "collector_quiesced": False, "quiesce_error": _reason(exc)}, 3
    return {**report, "collector_quiesced": True}, 0


def conclude(*, report: dict[str, object] | None, code: int, error: BaseException | None, close,
             resume) -> tuple[dict[str, object], int]:
    """F3-16: the one ending. The ledger is closed and the collector restarted (unless it stays stopped as
    asked) whatever failed before; every later error goes into the one report. Exit 0 only when all
    succeeded; 3 when the backup was made but something after it failed; 2 when it was not made."""
    made = report is not None
    final = dict(report) if made else {"ready": False, "error": _reason(error)}
    if made and error is not None:
        final["error"], code = _reason(error), 3
    elif not made:
        code = 2
    try:
        close()
    except BaseException as exc:  # An interrupt here still lets the collector restart (F3-16).
        final["close_error"] = _reason(exc)
        code = code or 3
    if not final.get("collector_quiesced"):
        try:
            resume()
        except BaseException as exc:
            final["restart_error"] = f"the installed collector did not restart: {_reason(exc)}"
            code = code or 3
    return final, code


def settle_maintenance(final: dict[str, object], code: int, *, resume) -> tuple[dict[str, object], int]:
    """F3-13: restart the collector unless it stays stopped as asked, before the one report is printed;
    a restart that fails is in that report (status 3), the backup still made and registered."""
    if final.get("collector_quiesced"):
        return final, code
    try:
        resume()
    except Exception as exc:
        return {**final, "restart_error": f"the installed collector did not restart: {_reason(exc)}"}, 3
    return final, code


def _peak_memory_bytes(resource) -> int:
    """This process's own peak memory. Linux keeps ru_maxrss across fork and exec, so it would report
    the parent's peak; VmHWM belongs to this address space alone."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def _stream_child(argv: list[str]) -> int:
    """`--stream-child DATABASE PARTIAL PROJECT SCHEMA LIMIT DEV:INO`: copy and check with this clone's package.

    Runs as `python -I -B -S`; reads the ledger read-only while the installed
    collector is stopped, writes the copy into the empty PARTIAL file the script
    created (DEV:INO), checks it through one descriptor completely and against
    the live partition, confirms the path is still that file, and prints its
    identity, digest, manifest digest, counts and this process's peak memory.
    """
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)  # Held by the script; nothing else changed.
    database, partial, project_id, schema, limit, expected = argv
    expected_identity = [int(part) for part in expected.split(":")]
    sys.path.insert(0, str(CLONE_PACKAGE))
    import resource
    import sqlite3

    from codex_agents import project_backup  # noqa: PLC0415

    path = Path(partial)
    if _identity(os.lstat(path)) != expected_identity or not stat.S_ISREG(os.lstat(path).st_mode):
        raise RuntimeError("the backup copy is not the file the script created")
    from codex_agents import learning_store  # noqa: PLC0415

    connection = sqlite3.connect("file:" + urllib.parse.quote(database) + "?mode=ro", uri=True,
                                 isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")  # One snapshot for the plan, the copy and the comparison (F3-8).
        schema_version = int(schema)
        check_inventory(connection, schema_version, exact_columns=False)  # the same refusal, inside the one snapshot
        # Planned before anything is written, as the ledger plans its own backups (F3-7).
        planned = project_backup.estimate_bytes(connection, project_id, schema_version)
        if planned > int(limit):
            raise RuntimeError(f"the project's backup is planned at {planned} bytes, over the limit of {limit}")
        free = os.statvfs(path.parent)
        needed = planned * learning_store.BACKUP_SPACE_FACTOR + learning_store.BACKUP_SPACE_MARGIN_BYTES
        if needed > free.f_bavail * free.f_frsize:
            raise RuntimeError(f"the project's backup is planned at {planned} bytes and needs {needed} bytes free; "
                               "not enough disk space")
        manifest = project_backup.write(connection, path, project_id, schema_version, limit_bytes=int(limit))
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            if (_identity(before) != expected_identity or not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
                raise RuntimeError("the backup copy is not the private file the script created")
            if before.st_size > int(limit):  # The plan is an estimate; the file is the fact.
                raise RuntimeError("the project's backup is over the ledger's backup limit")
            os.fsync(descriptor)
            digest = project_backup.file_digest(descriptor)
            result = project_backup.verify(path, project_id=project_id, schema_version=schema_version,
                                           compare=connection)
            # The path was read again by name: it must still be the file that was hashed.
            if (_identity(os.lstat(path)) != expected_identity
                    or _identity(os.fstat(descriptor)) != expected_identity
                    or project_backup.file_digest(descriptor) != digest):
                raise RuntimeError("the backup copy changed while it was checked")
        finally:
            os.close(descriptor)
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
    peak = _peak_memory_bytes(resource)
    print(json.dumps({"backup_sha256": digest, "manifest_sha256": result["manifest_sha256"],
                      "counts": result["counts"], "identity": expected_identity, "peak_rss_bytes": peak,
                      "columns": manifest["columns"]},
                     sort_keys=True))
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--expires-in-seconds", type=int, default=2_592_000)
    parser.add_argument(
        "--leave-quiesced",
        action="store_true",
        help=(
            "leave the collector stopped so an update and guarded schema migration "
            "cannot make the exact backup stale"
        ),
    )
    args = parser.parse_args()
    if args.expires_in_seconds < 60:
        raise RuntimeError("backup expiry must be at least 60 seconds")
    target = args.target.resolve(strict=True)
    orchestration_root = target / ".codex" / "orchestration"
    cli_path = orchestration_root / "cli.py"
    if not cli_path.is_file():
        raise RuntimeError("installed orchestration runtime is unavailable")
    sys.path.insert(0, str(orchestration_root))

    from codex_agents.learning_store import (  # noqa: PLC0415
        CollectorMaintenance,
        LearningLedger,
        LearningState,
    )

    state = LearningState(args.state_root, create=False)
    found_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)  # F3-15: held to the end
    try:
        maintenance = CollectorMaintenance.acquire(state, cli_path, target)
        ledger = report = error = None
        code = 0
        try:
            ledger = LearningLedger(state)
            project_id = ledger.project_for_root(target)
            connection = ledger._connection
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18}:
                raise RuntimeError("installed ledger schema does not support this backup")
            check_inventory(connection, schema_version)
            project = connection.execute(
                f"SELECT {','.join(_PROJECT_COLUMNS)} "
                "FROM projects WHERE project_id=? AND status='active'",
                (project_id,),
            ).fetchone()
            if project is None:
                raise RuntimeError("project binding is not active")
            expires_at_ms = (
                time.time_ns() // 1_000_000 + args.expires_in_seconds * 1000
            )
            report = backup_partition(ledger, project_id, args.file_id, schema_version=schema_version,
                                      expires_at_ms=expires_at_ms)
            report, code = finish_backup(
                report, leave_quiesced=args.leave_quiesced,
                quiesce=lambda: _quiesce_collector_authorization(state.collector_authorization))
        except BaseException as exc:  # Every exit after the preflight goes through the one ending.
            error = exc
        # F3-16: one ending, then one report.
        final, code = conclude(report=report, code=code, error=error,
                               close=ledger.close if ledger is not None else (lambda: None),
                               resume=maintenance.resume)
        if set(signal.sigpending()) & set(_STOP_SIGNALS):  # It acts once the report is out.
            final["stopped_after_registration" if report is not None else "stopped"] = True
        print(json.dumps(final, indent=2, sort_keys=True))
        return code
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, found_mask)

if __name__ == "__main__":
    if sys.argv[1:2] == ["--stream-child"]:
        try:
            raise SystemExit(_stream_child(sys.argv[2:]))
        except Exception as exc:
            print(json.dumps({"error": _reason(exc)}))
            raise SystemExit(2)
    try:
        raise SystemExit(_main())
    except Exception as exc:
        print(json.dumps({"ready": False, "error": _reason(exc)}, indent=2))
        raise SystemExit(2)
