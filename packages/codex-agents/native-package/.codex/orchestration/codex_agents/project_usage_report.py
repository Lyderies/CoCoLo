"""Read-only usage of the children a project ran in a window (RT-8).

`learning-usage-report` answers, per provider and model, how many runs finished, how many of them have
complete usage, and their input, output, cache-creation and cache-read tokens, with one row per run: an
opaque run identifier, its time, model, effort, role, outcome and tokens. It is content-free: no task text,
path or store identifier.

It reads the project's own store, and the ledger an earlier version kept, with read-only SQLite
connections. It starts no service, takes no enrollment proof, and writes nothing, not even the
write-ahead files of a database nobody has open. A run is counted at the time its usage was recorded (the
terminal time of its attempt), once, in whichever store holds it.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import urllib.parse
from pathlib import Path

from .collector_paths import StateError, _assert_no_symlink_components, _canonical_project_root
from .contracts import ContractError
from .learning_adapters import TOKEN_USAGE_FIELDS, token_usage_known
from .learning_contracts import MAX_RECORD_BYTES, external_id, keyed_digest, parse_bounded_json, provider_for_model

DEFAULT_ROWS = 500
MAX_ROWS = 2_000  # Runs listed at most; a larger window is cut at this and says so.
MAX_WINDOW_MS = 366 * 24 * 3_600_000
_CHUNK = 200  # run identifiers looked up per statement
_USAGE_TYPES = ("provider_usage", "observation_unavailable")
_TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class UsageReportError(Exception):
    """A refusal with a fixed reason code; never a path or value."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _readonly(path: Path):
    """The database at path opened for reading only, or None when it does not exist.

    A database with write-ahead files has a live or interrupted writer: read it alongside that writer. One
    with none is not open anywhere, and is read without locks or write-ahead files so that nothing is
    created next to it.
    """
    try:
        _assert_no_symlink_components(path)
        info = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, StateError):
        raise UsageReportError("usage_store_unavailable") from None
    if not stat.S_ISREG(info.st_mode):
        raise UsageReportError("usage_store_unavailable")
    shared = any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm"))
    uri = "file:" + urllib.parse.quote(str(path)) + ("?mode=ro" if shared else "?mode=ro&immutable=1")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error:
        raise UsageReportError("usage_store_unavailable") from None
    return connection


def _project_id(state, root: Path, key: bytes) -> str:
    connection = _readonly(state.root / "learning.sqlite3")
    if connection is None:
        raise UsageReportError("usage_project_not_registered")
    try:
        row = connection.execute(
            "SELECT project_id FROM projects WHERE root_digest=? AND status='active'",
            (keyed_digest(key, str(root)),),
        ).fetchone()
    except sqlite3.Error:
        raise UsageReportError("usage_store_unavailable") from None
    finally:
        connection.close()
    if row is None or type(row[0]) is not str:
        raise UsageReportError("usage_project_not_registered")
    return row[0]


def _record(blob):
    if type(blob) is not bytes or not 0 < len(blob) <= MAX_RECORD_BYTES:
        return None
    try:
        value = parse_bounded_json(blob)
    except ContractError:
        return None
    return value if type(value) is dict and type(value.get("run_id")) is str else None


def _by_run(connection, project_id, record_types, source, run_ids):
    """Each named run's first stored record of these types by this source, in sequence order."""
    found = {}
    names = sorted(run_ids)
    marks = ",".join("?" for _ in record_types)
    for start in range(0, len(names), _CHUNK):
        chunk = names[start:start + _CHUNK]
        rows = connection.execute(
            f"SELECT content,observed_at_ms FROM records WHERE project_id=? AND record_type IN ({marks}) "
            f"AND source=? AND json_extract(CAST(content AS TEXT),'$.run_id') IN ({','.join('?' for _ in chunk)}) "
            "ORDER BY source_sequence LIMIT ?",
            (project_id, *record_types, source, *chunk, 4 * len(chunk) + 1),
        ).fetchall()
        for blob, observed in rows:
            value = _record(blob)
            if value is not None and type(observed) is int:
                found.setdefault(value["run_id"], (value, observed))
    return found


def _usage_tokens(record):
    """(complete, input, output, cache creation, cache read) of one usage record."""
    if record["record_type"] != "provider_usage":
        return False, None, None, None, None
    inputs, cached = record.get("input_tokens"), record.get("cached_input_tokens")
    outputs, creation = record.get("output_tokens"), record.get("cache_creation_input_tokens")
    complete = None not in (inputs, cached, outputs) and (record.get("provider") != "claude" or creation is not None)
    return complete, inputs, outputs, creation, cached


def _outcome_tokens(outcome):
    usage = outcome.get("usage")
    if type(usage) is not dict or not all(field in usage for field in TOKEN_USAGE_FIELDS):
        return False, None, None, None, None
    return token_usage_known(usage), usage["input_tokens"], usage["output_tokens"], None, usage["cached_tokens"]


def _integer_or_none(value):
    return value if type(value) is int and value >= 0 else None


def _text(value):
    return value if type(value) is str and 0 < len(value) <= 128 else None


def _rows(connection, project_id, since_ms, until_ms, limit):
    """This store's runs of the window as (time, run, provider, model, effort, role, outcome, tokens...)."""
    usage_rows = connection.execute(
        "SELECT content,observed_at_ms FROM records WHERE project_id=? AND record_type IN (?,?) AND source='provider' "
        "AND observed_at_ms>=? AND observed_at_ms<? ORDER BY observed_at_ms,source_sequence LIMIT ?",
        (project_id, *_USAGE_TYPES, since_ms, until_ms, limit + 1),
    ).fetchall()
    usage = {}
    for blob, observed in usage_rows:
        value = _record(blob)
        if value is not None and type(observed) is int:
            usage.setdefault(value["run_id"], (value, observed))
    # Runs the window shows only by their outcome: one with no usage record at all (an earlier version's
    # native child) is counted at its outcome's time, with its usage as that outcome recorded it.
    outcome_rows = connection.execute(
        "SELECT content,observed_at_ms FROM records WHERE project_id=? AND record_type='outcome' AND source='host' "
        "AND observed_at_ms>=? AND observed_at_ms<? ORDER BY observed_at_ms,source_sequence LIMIT ?",
        (project_id, since_ms, until_ms, limit + 1),
    ).fetchall()
    windowed = {}
    for blob, observed in outcome_rows:
        value = _record(blob)
        if value is not None and type(observed) is int and value["run_id"] not in usage:
            windowed.setdefault(value["run_id"], (value, observed))
    stored = _by_run(connection, project_id, _USAGE_TYPES, "provider", windowed) if windowed else {}
    outcomes = _by_run(connection, project_id, ("outcome",), "host", usage) if usage else {}
    rows = []
    for run_id, (record, observed) in usage.items():
        outcome = outcomes.get(run_id, (None, None))[0]
        rows.append((observed, run_id, record.get("provider"), _text(record.get("requested_model")), outcome,
                     _usage_tokens(record)))
    for run_id, (outcome, observed) in windowed.items():
        if run_id not in stored:
            rows.append((observed, run_id, None, None, outcome, _outcome_tokens(outcome)))
    return rows, len(usage_rows) > limit or len(outcome_rows) > limit


def usage_report(state, project_root, since_ms: int, until_ms: int, limit: int = DEFAULT_ROWS) -> dict:
    """The window's runs of one project, per provider and model and one by one. Reads only."""
    if (type(since_ms) is not int or type(until_ms) is not int or not 0 <= since_ms < until_ms
            or until_ms - since_ms > MAX_WINDOW_MS or type(limit) is not int or not 1 <= limit <= MAX_ROWS):
        raise UsageReportError("usage_window_invalid")
    # S44a: the root the owner types is `.`, which the report took for "not absolute" and called a state problem
    # (learning-status resolves it first). A relative root is read from the folder the command runs in; every
    # check on the path stays, and a root that fails one is named as the project root, not as the state.
    try:
        path = Path(project_root)
        root = _canonical_project_root(path if path.is_absolute() else Path(os.getcwd()) / path)
    except (StateError, OSError):
        raise UsageReportError("usage_project_root_invalid") from None
    try:
        key = state.digest_key()
    except (StateError, OSError):
        raise UsageReportError("usage_state_unavailable") from None
    project_id = _project_id(state, root, key)
    sources = {}
    gathered = {}
    cut = False
    recorded = []  # each store's (first, last) time of any run it holds for the project
    for name, path in (("project_store", state.root / "projects" / project_id / "history.sqlite3"),
                       ("ledger", state.root / "learning.sqlite3")):
        connection = _readonly(path)
        sources[name] = connection is not None
        if connection is None:
            continue
        try:
            rows, more = _rows(connection, project_id, since_ms, until_ms, limit)
            recorded.append(_recorded_span(connection, project_id))
        except sqlite3.Error:
            raise UsageReportError("usage_store_unavailable") from None
        finally:
            connection.close()
        cut = cut or more
        for row in rows:
            gathered.setdefault(row[1], row)  # the project's own store first: a run is counted once
    ordered = sorted(gathered.values(), key=lambda row: (row[0], row[1]))
    if len(ordered) > limit:
        ordered, cut = ordered[:limit], True
    runs = []
    models = {}
    for observed, run_id, provider, model, outcome, (complete, inputs, outputs, creation, cached) in ordered:
        name = _text(outcome.get("model")) if outcome else None
        model = name or model
        if provider is None:
            try:
                provider = provider_for_model(model)
            except ContractError:
                provider = "unknown"
        row = {
            "run_id": external_id(key, "run", run_id), "time_ms": observed, "time": _iso(observed),
            "provider": provider, "model": model or "unknown",
            "effort": _text(outcome.get("effort")) if outcome else None,
            "role": _text(outcome.get("role")) if outcome else None,
            "outcome": _text(outcome.get("status")) if outcome else None,
            "usage_complete": bool(complete),
            "input_tokens": _integer_or_none(inputs), "output_tokens": _integer_or_none(outputs),
            "cache_creation_input_tokens": _integer_or_none(creation),
            "cache_read_input_tokens": _integer_or_none(cached),
        }
        runs.append(row)
        total = models.setdefault((row["provider"], row["model"]), {
            "provider": row["provider"], "model": row["model"], "runs": 0, "runs_with_complete_usage": 0,
            # Codex counts cached tokens inside its input; Claude counts cache reads and creation apart from it.
            "input_includes_cache_reads": row["provider"] == "codex",
            **{field: 0 for field in _TOKENS}})
        total["runs"] += 1
        total["runs_with_complete_usage"] += int(row["usage_complete"])
        for field in _TOKENS:
            if row[field] is not None:
                total[field] += row[field]
    return {
        "window": {"since_ms": since_ms, "until_ms": until_ms, "since": _iso(since_ms), "until": _iso(until_ms)},
        "row_limit": limit, "truncated": cut, "sources": sources,
        "window_data": _window_data(runs, recorded, since_ms, until_ms),
        "usage_coverage": _coverage(runs),
        "models": [models[key_] for key_ in sorted(models)], "runs": runs,
    }


def _recorded_span(connection, project_id):
    """(first, last) time any run of the project was recorded in this store, or (None, None)."""
    # A run is counted at its usage record's time; only a project with none (an earlier version's runs) is read by
    # its outcomes' times.
    for kind, source in (("record_type IN (?,?)", "provider"), ("record_type=?", "host")):
        names = _USAGE_TYPES if source == "provider" else ("outcome",)
        first, last = connection.execute(
            f"SELECT MIN(observed_at_ms),MAX(observed_at_ms) FROM records WHERE project_id=? AND {kind} AND source=?",
            (project_id, *names, source)).fetchone()
        if type(first) is int and type(last) is int:
            return first, last
    return None, None


def _window_data(runs, recorded, since_ms, until_ms):
    """Whether the window holds usage data, and if not why, in plain words (S44a-3)."""
    firsts = [first for first, _ in recorded if first is not None]
    lasts = [last for _, last in recorded if last is not None]
    first, last = (min(firsts), max(lasts)) if firsts else (None, None)
    data = {"first_run_recorded": None if first is None else _iso(first),
            "last_run_recorded": None if last is None else _iso(last), "runs_without_usage": 0}
    if runs:
        data.update(state="has_runs", runs_without_usage=sum(1 for run in runs if not run["usage_complete"]))
        data["note"] = ("Runs without complete usage are listed with their tokens left empty, never as zero."
                        if data["runs_without_usage"] else "Every run in this window has complete usage.")
    elif first is None:
        data.update(state="nothing_recorded", note="No run has any usage or outcome recorded for this project, so "
                    "there is no usage data for any window.")
    elif until_ms <= first:
        data.update(state="before_usage_recorded", note=f"This window ends before the first run was recorded "
                    f"({_iso(first)}); usage was not recorded then, so there is no usage data for it.")
    else:
        data.update(state="no_runs_in_window", note=f"No run was recorded in this window; there is no usage data "
                    f"for it. Runs were recorded from {_iso(first)} to {_iso(last)}.")
    return data


PRIMARY_SESSION_NOTE = (
    "The primary session's own tokens are not given to CoCoLo: the Codex hooks carry no token counts, which is why "
    "learning-status shows ingestion.coverage root_usage and child_usage as false (that describes hook events, not "
    "the runs). Only each child's own tokens are recorded.")


def _coverage(runs):
    """Per provider, whether the window's child runs reported their usage: reported, partial, not_reported, no_runs."""
    children = {}
    for provider in ("codex", "claude"):
        mine = [run for run in runs if run["provider"] == provider]
        complete = sum(1 for run in mine if run["usage_complete"])
        state = ("no_runs" if not mine else "reported" if complete == len(mine)
                 else "not_reported" if complete == 0 else "partial")
        children[provider] = {"state": state, "runs": len(mine), "runs_with_complete_usage": complete}
    return {"child_runs": children,
            "primary_session": {"state": "not_reported", "note": PRIMARY_SESSION_NOTE}}


STATUS_USAGE_DAYS = 7


def status_run_usage(state, project_root):
    """What learning-status shows beside the hook coverage flags: the last days' child runs, per provider.

    None when the project's runs cannot be read (not registered, state unreadable): unknown is never shown as zero.
    """
    now = int(time.time() * 1000)
    try:
        report = usage_report(state, project_root, now - STATUS_USAGE_DAYS * 24 * 3_600_000, now + 1, MAX_ROWS)
    except UsageReportError:
        return None
    return {"window_days": STATUS_USAGE_DAYS, "truncated": report["truncated"], **report["usage_coverage"],
            "note": f"Each child run's tokens are recorded from the child's own output and listed by "
                    f"learning-usage-report. {PRIMARY_SESSION_NOTE}"}


def _iso(milliseconds: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(milliseconds / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
