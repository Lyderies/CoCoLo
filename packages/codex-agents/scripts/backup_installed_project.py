#!/usr/bin/env python3
"""Create and verify a project-partition backup with the installed runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path


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
    value["quiesced_for_upgrade"] = True
    temporary = path.parent / f".collector-authorization.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        root_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


_BYTES_KEY = "$codex_agents_bytes_base64"
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
        "ingested_at_ms", "expires_at_ms", "content", "content_digest", "late",
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
    "capacity_executions": ("handle_digest", "run_id", "bridge_run_id", "owner_digest"),
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
    if schema_version not in {5, 6, 7, 8, 9, 10}:
        raise RuntimeError("installed ledger schema does not support this backup")
    return tuple(table for table in _TABLE_COLUMNS
                 if (schema_version >= 7 or table != "launch_claims")
                 and (schema_version >= 8 or table not in {
                     "launch_inputs", "launch_observations", "launch_receipts"})
                 and (schema_version >= 9 or table not in {"active_run_capacity", "capacity_completions", "capacity_executions"})
                 and (schema_version >= 10 or table not in {'account_open_receipts', 'account_open_requests'}))


def _encode(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(value: object) -> object:
    if isinstance(value, dict) and set(value) == {_BYTES_KEY}:
        encoded = value[_BYTES_KEY]
        if not isinstance(encoded, str):
            raise RuntimeError("project backup byte encoding is invalid")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("project backup byte encoding is invalid") from exc
    return value


def _verify_relational_restore(
    source: object,
    body: dict[str, object],
    *,
    validate_learning_record: object,
    content_digest: object,
) -> None:
    import sqlite3

    source_connection = source
    restored = sqlite3.connect(":memory:")
    restored.row_factory = sqlite3.Row
    restored.execute("PRAGMA foreign_keys = ON")
    try:
        for table in ("projects", *_tables(body["ledger_schema_version"])):
            schema = source_connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if schema is not None:
                restored.execute(str(schema[0]))
        if body["ledger_schema_version"] >= 8:
            parent_index = source_connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='launch_receipt_parent'"
            ).fetchone()
            if parent_index is None:
                raise RuntimeError("project backup launch receipt parent is unavailable")
            restored.execute(str(parent_index[0]))
        if body["ledger_schema_version"] >= 9:
            parent_index = source_connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='active_capacity_parent'"
            ).fetchone()
            if parent_index is None:
                raise RuntimeError("project backup active capacity parent is unavailable")
            restored.execute(str(parent_index[0]))
        project = body["project"]
        restored.execute(
            f"INSERT INTO projects({','.join(_PROJECT_COLUMNS)}) VALUES(?,?,?,?,?,?)",
            [_decode(project[name]) for name in _PROJECT_COLUMNS],
        )
        project_id = project["project_id"]
        for table in _tables(body["ledger_schema_version"]):
            columns = body["columns"][table]
            rows = body["tables"][table]
            if body["counts"][table] != len(rows):
                raise RuntimeError("project backup count manifest is invalid")
            if not columns:
                if rows:
                    raise RuntimeError("project backup row shape is invalid")
                continue
            placeholders = ",".join("?" for _ in range(len(columns) + 1))
            for row in rows:
                if set(row) != set(columns):
                    raise RuntimeError("project backup row shape is invalid")
                values = [_decode(row[name]) for name in columns]
                if table == "records":
                    record = validate_learning_record(json.loads(bytes(
                        values[columns.index("content")]
                    )))
                    if content_digest(record) != values[columns.index("content_digest")]:
                        raise RuntimeError("project backup record verification failed")
                restored.execute(
                    f"INSERT INTO {table}(project_id,{','.join(columns)}) "
                    f"VALUES({placeholders})",
                    [project_id, *values],
                )
        if restored.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("project backup foreign-key verification failed")
        if body["ledger_schema_version"] >= 9 and restored.execute(
            "SELECT 1 FROM launch_claims c LEFT JOIN active_run_capacity a "
            "ON a.project_id=c.project_id AND a.handle_digest=c.handle_digest "
            "WHERE a.run_id IS NULL OR a.run_id<>c.run_id OR a.bridge_run_id IS NOT c.bridge_run_id LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError("project backup capacity coverage is incomplete")
        if body["ledger_schema_version"] >= 9 and restored.execute(
            "SELECT 1 FROM active_run_capacity a LEFT JOIN capacity_completions c "
            "ON c.project_id=a.project_id AND c.handle_digest=a.handle_digest "
            "AND c.run_id=a.run_id AND c.bridge_run_id=a.bridge_run_id "
            "LEFT JOIN capacity_executions e ON e.project_id=a.project_id AND e.handle_digest=a.handle_digest "
            "AND e.run_id=a.run_id AND e.bridge_run_id=a.bridge_run_id "
            "WHERE a.released_at_ms IS NOT c.released_at_ms OR a.release_digest IS NOT c.receipt_digest "
            "OR (a.released_at_ms IS NOT NULL AND e.owner_digest IS NULL) LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError("project backup completion coverage is incomplete")
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("project backup integrity verification failed")
        for table in _tables(body["ledger_schema_version"]):
            columns = body["columns"][table]
            if not columns:
                continue
            actual = [
                {name: _encode(row[name]) for name in columns}
                for row in restored.execute(
                    f"SELECT {','.join(columns)} FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            ]
            if sorted(_canonical_json(row) for row in actual) != sorted(
                _canonical_json(row) for row in body["tables"][table]
            ):
                raise RuntimeError("project backup relational roundtrip changed rows")
    finally:
        restored.close()


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

    from codex_agents.learning_contracts import (  # noqa: PLC0415
        content_digest,
        validate_learning_record,
    )
    from codex_agents.learning_store import (  # noqa: PLC0415
        CollectorMaintenance,
        LearningLedger,
        LearningState,
    )

    state = LearningState(args.state_root, create=False)
    maintenance = CollectorMaintenance.acquire(state, cli_path, target)
    ledger = None
    operation_completed = False
    try:
        ledger = LearningLedger(state)
        project_id = ledger.project_for_root(target)
        connection = ledger._connection
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version not in {5, 6, 7, 8, 9, 10}:
            raise RuntimeError("installed ledger schema does not support this backup")
        project_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(projects)").fetchall()
        }
        if project_columns != set(_PROJECT_COLUMNS):
            raise RuntimeError("project backup cannot represent projects exactly")
        project = connection.execute(
            f"SELECT {','.join(_PROJECT_COLUMNS)} "
            "FROM projects WHERE project_id=? AND status='active'",
            (project_id,),
        ).fetchone()
        if project is None:
            raise RuntimeError("project binding is not active")
        tables: dict[str, list[dict[str, object]]] = {}
        columns_manifest: dict[str, list[str]] = {}
        for table in _tables(schema_version):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            available = (
                {str(row[1]) for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()}
                if exists is not None else set()
            )
            columns = list(_expected_columns(schema_version, table))
            expected_available = {"project_id", *columns} if columns else set()
            if available != expected_available:
                raise RuntimeError(
                    f"project backup cannot represent {table} exactly"
                )
            columns_manifest[table] = columns
            if not columns:
                tables[table] = []
                continue
            ordering = ",".join(name for name in _TABLE_ORDER[table] if name in available)
            query = (
                f"SELECT {','.join(columns)} FROM {table} WHERE project_id=?"
                + (f" ORDER BY {ordering}" if ordering else "")
            )
            values = []
            for row in connection.execute(query, (project_id,)).fetchall():
                value = {column: _encode(row[column]) for column in columns}
                values.append(value)
            tables[table] = values

        body = {
            "schema_version": 2,
            "ledger_schema_version": schema_version,
            "project": {column: _encode(project[column]) for column in _PROJECT_COLUMNS},
            "columns": columns_manifest,
            "tables": tables,
            "counts": {name: len(rows) for name, rows in tables.items()},
        }
        body_bytes = _canonical_json(body)
        payload = _canonical_json(
            {
                "backup_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "body": body,
            }
        )
        if len(payload) > 128 * 1024 * 1024:
            raise RuntimeError("project backup is oversized")
        expires_at_ms = (
            time.time_ns() // 1_000_000 + args.expires_in_seconds * 1000
        )
        path = ledger._write_owner_file("backup", project_id, args.file_id, payload)
        try:
            ledger._register_stored_file(
                project_id,
                file_id=args.file_id,
                kind="backup",
                path=path,
                digest=hashlib.sha256(payload).hexdigest(),
                expires_at_ms=expires_at_ms,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise

        decoded = json.loads(path.read_bytes())
        if (
            hashlib.sha256(_canonical_json(decoded["body"])).hexdigest()
            != decoded["backup_sha256"]
        ):
            raise RuntimeError("project backup verification failed")
        _verify_relational_restore(
            connection,
            decoded["body"],
            validate_learning_record=validate_learning_record,
            content_digest=content_digest,
        )
        if args.leave_quiesced:
            _quiesce_collector_authorization(state.collector_authorization)
        operation_completed = True
        print(json.dumps({
            "ready": True,
            "project_id": project_id,
            "file_id": args.file_id,
            "path": str(path),
            "ledger_schema_version": schema_version,
            "backup_sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_sha256": decoded["backup_sha256"],
            "counts": body["counts"],
            "isolated_restore_integrity": "ok",
            "isolated_restore_foreign_keys": "ok",
            "collector_quiesced": args.leave_quiesced,
        }, indent=2, sort_keys=True))
        return 0
    finally:
        if ledger is not None:
            ledger.close()
        if not args.leave_quiesced or not operation_completed:
            try:
                maintenance.resume()
            except Exception:
                if operation_completed:
                    raise RuntimeError(
                        "backup completed but the installed collector did not restart"
                    )


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, indent=2))
        raise SystemExit(2)
