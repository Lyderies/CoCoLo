"""RT-8: every unit's usage is recorded and one read-only command reports it.

`learning-usage-report` answers, per provider and model and per run, what a window of children used:
runs, runs whose usage is complete, input, output, cache-creation and cache-read tokens, with each run's
time, model, effort, role and outcome. It reads the project's store (and the ledger an earlier version
kept) read-only: it starts no service and writes nothing, and it never prints task text, paths or the
store's own run identifiers.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import verification_outcome

from codex_agents.learning_adapters import empty_usage, token_usage_known
from codex_agents.learning_contracts import canonical_json, content_digest, validate_learning_record
from codex_agents.learning_store import LearningLedger, LearningState
from codex_agents.project_history import ProjectHistory

CLI = ORCHESTRATION_ROOT / "cli.py"
SECRET_SLICE = "PRIVATE-task-slice"
T0 = 1_800_000_000_000  # the window starts here
HOUR = 3_600_000
CODEX = "gpt-5.6-sol"
CLAUDE = "claude-sonnet-5-5"
CONFIGURATION = ("model", "effort", "role", "topology", "max_parallel", "verification_required", "verifier_digest",
                 "bounds_digest", "package_digest", "policy_digest", "agent_digest", "skill_digests",
                 "prompt_digest", "client_digest")


def iso(milliseconds: int) -> str:
    return datetime.datetime.fromtimestamp(milliseconds / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsageRecordingTests(unittest.TestCase):
    def test_usage_is_complete_when_the_provider_reported_every_token_count_it_has(self) -> None:
        # The eight outcome usage fields include model calls, tool calls, latency and cost, which a bridged
        # child never reports; completeness is about the token counts the provider does report (RT-8).
        tokens = {**empty_usage(), "input_tokens": 600, "cached_tokens": 400, "output_tokens": 30}
        self.assertTrue(token_usage_known(tokens))
        for missing in ("input_tokens", "cached_tokens", "output_tokens"):
            self.assertFalse(token_usage_known({**tokens, missing: None}), missing)
        self.assertFalse(token_usage_known(empty_usage()))


class UsageReportCase(unittest.TestCase):
    """A state root with a registered project whose own store holds a few finished runs."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        self.project = base / "project-with-a-private-name"
        self.project.mkdir()
        state = LearningState(self.state_root)
        ledger = LearningLedger(state)
        self.project_id = ledger.register_project(self.project, now_ms=1)
        ledger.close()
        directory = state.root / "projects" / self.project_id
        directory.mkdir(parents=True, mode=0o700)
        self.store = ProjectHistory(directory, self.project_id)
        self.counter = 0

    def append(self, record: dict, source: str, ingested_at_ms: int) -> None:
        checked = validate_learning_record(record)
        with self.store._transaction():
            self.store._append_locked(checked, canonical_json(checked), source, ingested_at_ms)

    def finished(self, at_ms: int, *, model: str = CODEX, usage: dict | None = None, effort: str = "high",
            role: str = "codex_agents_verification_worker", status: str = "accepted", outcome: bool = True,
            ) -> str:
        """One finished run: its usage record (or the note that there was none) and its host outcome."""
        self.counter += 1
        run_id = f"run-private-{self.counter}"
        provider = "claude" if model.startswith("claude-") else "codex"
        base = {"schema_version": 1, "provider": provider, "run_id": run_id, "phase": "after",
                "observed_at_ms": at_ms}
        if usage is None:
            self.append({**base, "record_type": "observation_unavailable",
                         "record_id": f"observation_unavailable:{self.counter}",
                         "reason_code": "usage_unavailable"}, "provider", at_ms)
        else:
            self.append({**base, "record_type": "provider_usage", "record_id": f"provider_usage:{self.counter}",
                         "requested_model": model, "models": [], "helper_models": [],
                         "input_tokens": usage.get("input_tokens"), "cached_input_tokens": usage.get("cached"),
                         "cache_creation_input_tokens": usage.get("creation"),
                         "output_tokens": usage.get("output_tokens"), "reasoning_output_tokens": None},
                        "provider", at_ms)
        if outcome:
            value = verification_outcome(f"outcome-{self.counter}")
            value.update(run_id=run_id, model=model, effort=effort, role=role, status=status, accepted=status == "accepted", admission_state=status,
                         task_slice=SECRET_SLICE, observed_at_ms=at_ms + 5_000)
            value["configuration_digest"] = content_digest({name: value[name] for name in CONFIGURATION})
            value["usage"] = empty_usage()
            value["usage_sources"] = {field: {"source": "unavailable", "coverage": "unknown"} for field in value["usage"]}
            self.append(value, "host", at_ms + 5_000)
        return run_id

    def report(self, since: int = T0, until: int = T0 + 24 * HOUR, *extra: str, expected: int = 0) -> dict:
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(CLI), "learning-usage-report", "--state-root", str(self.state_root),
             "--project-root", str(self.project), "--since", iso(since), "--until", iso(until), *extra],
            capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(expected, done.returncode, done.stdout + done.stderr)
        return json.loads(done.stdout)


class UsageReportTests(UsageReportCase):
    def populate(self) -> dict:
        ids = {}
        ids["codex"] = self.finished(T0 + HOUR, usage={"input_tokens": 1000, "cached": 800, "output_tokens": 50})
        ids["claude"] = self.finished(T0 + 2 * HOUR, model=CLAUDE, role="claude_agents_researcher", effort="medium",
                                 usage={"input_tokens": 120, "cached": 400, "creation": 80, "output_tokens": 30})
        ids["claude2"] = self.finished(T0 + 3 * HOUR, model=CLAUDE, role="claude_agents_researcher", effort="medium",
                                  usage={"input_tokens": 10, "cached": 20, "creation": 30, "output_tokens": 5})
        ids["unknown"] = self.finished(T0 + 4 * HOUR, usage=None, status="rejected")
        ids["early"] = self.finished(T0 - HOUR, usage={"input_tokens": 7, "cached": 1, "output_tokens": 1})
        ids["late"] = self.finished(T0 + 30 * HOUR, usage={"input_tokens": 9, "cached": 1, "output_tokens": 1})
        return ids

    def test_per_provider_and_model_totals_and_per_run_rows(self) -> None:
        self.populate()
        report = self.report()
        self.assertTrue(report["ready"])
        models = {(row["provider"], row["model"]): row for row in report["models"]}
        self.assertEqual({("codex", CODEX), ("claude", CLAUDE)}, set(models))
        codex, claude = models[("codex", CODEX)], models[("claude", CLAUDE)]
        # The unknown run has an outcome, so it counts under its model with its usage unknown.
        self.assertEqual((2, 1, 1000, 50, 800), (codex["runs"], codex["runs_with_complete_usage"],
                                                 codex["input_tokens"], codex["output_tokens"],
                                                 codex["cache_read_input_tokens"]))
        self.assertEqual((2, 2, 130, 35, 110, 420), (
            claude["runs"], claude["runs_with_complete_usage"], claude["input_tokens"], claude["output_tokens"],
            claude["cache_creation_input_tokens"], claude["cache_read_input_tokens"]))
        self.assertFalse(report["truncated"])
        rows = report["runs"]
        self.assertEqual(4, len(rows))
        self.assertEqual([T0 + HOUR, T0 + 2 * HOUR, T0 + 3 * HOUR, T0 + 4 * HOUR], [row["time_ms"] for row in rows])
        first = rows[0]
        self.assertEqual((iso(T0 + HOUR), CODEX, "high", "codex_agents_verification_worker", "accepted", True),
                         (first["time"], first["model"], first["effort"], first["role"], first["outcome"],
                          first["usage_complete"]))
        self.assertEqual((1000, 50, None, 800), (first["input_tokens"], first["output_tokens"],
                                                 first["cache_creation_input_tokens"],
                                                 first["cache_read_input_tokens"]))
        self.assertEqual((120, 30, 80, 400), tuple(rows[1][key] for key in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
        last = rows[3]
        self.assertEqual((False, None, "rejected"), (last["usage_complete"], last["input_tokens"], last["outcome"]))

    def test_the_window_is_since_inclusive_until_exclusive(self) -> None:
        self.finished(T0, usage={"input_tokens": 1, "cached": 0, "output_tokens": 1})
        self.finished(T0 + HOUR, usage={"input_tokens": 2, "cached": 0, "output_tokens": 1})
        report = self.report(T0, T0 + HOUR)
        self.assertEqual([T0], [row["time_ms"] for row in report["runs"]])

    def test_the_report_names_no_task_text_path_or_store_identifier(self) -> None:
        ids = self.populate()
        report = self.report()
        text = json.dumps(report)
        for secret in (SECRET_SLICE, str(self.project), self.project.name, self.project_id, "run-private",
                       str(self.state_root), *ids.values()):
            self.assertNotIn(secret, text)
        identifiers = [row["run_id"] for row in report["runs"]]
        self.assertEqual(len(set(identifiers)), len(identifiers))
        self.assertTrue(all(value.startswith("run:") and len(value) == 68 for value in identifiers), identifiers)

    def test_a_window_past_the_row_limit_is_truncated_not_refused(self) -> None:
        for number in range(5):
            self.finished(T0 + (number + 1) * 1000, usage={"input_tokens": 10, "cached": 0, "output_tokens": 1})
        report = self.report(T0, T0 + HOUR, "--limit", "3")
        self.assertTrue(report["truncated"])
        self.assertEqual(3, len(report["runs"]))
        self.assertEqual(3, sum(row["runs"] for row in report["models"]))
        self.assertEqual(3, report["row_limit"])
        self.assertEqual(sorted(row["time_ms"] for row in report["runs"]), [row["time_ms"] for row in report["runs"]])

    def test_a_bad_window_or_limit_is_refused(self) -> None:
        for arguments in (("--limit", "0"), ("--limit", "100000")):
            self.assertFalse(self.report(T0, T0 + HOUR, *arguments, expected=2)["ready"], arguments)
        self.assertFalse(self.report(T0 + HOUR, T0, expected=2)["ready"])
        self.assertFalse(self.report(T0, T0 + 400 * 24 * HOUR, expected=2)["ready"])

    def test_it_reads_only_a_project_the_state_root_knows(self) -> None:
        self.store.close()
        stranger = Path(self.temporary.name) / "stranger"
        stranger.mkdir()
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(CLI), "learning-usage-report", "--state-root", str(self.state_root),
             "--project-root", str(stranger), "--since", iso(T0), "--until", iso(T0 + HOUR)],
            capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(2, done.returncode)
        self.assertFalse(json.loads(done.stdout)["ready"])

    def test_a_run_older_than_its_usage_record_is_found_by_the_time_of_its_usage(self) -> None:
        # The outcome is recorded after the usage record; one that falls outside the window does not move the run.
        run = self.finished(T0 + 24 * HOUR - 1000, usage={"input_tokens": 3, "cached": 0, "output_tokens": 1})
        self.assertIsNotNone(run)
        report = self.report()
        self.assertEqual(1, len(report["runs"]))
        self.assertEqual("accepted", report["runs"][0]["outcome"])
        later = self.report(T0 + 24 * HOUR, T0 + 48 * HOUR)
        self.assertEqual([], later["runs"])


def tree(root: Path) -> dict:
    """Every file under root with its size, modification time and content digest."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_socket():
            stat = path.stat()
            found[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns,
                                                  hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            found[str(path.relative_to(root))] = None
    return found


class UsageReportReadOnlyTests(UsageReportCase):
    def test_it_starts_no_service_and_writes_nothing(self) -> None:
        self.finished(T0 + HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 2})
        self.store.close()
        before = tree(Path(self.temporary.name))
        report = self.report()
        self.assertEqual(1, len(report["runs"]))
        self.assertEqual(before, tree(Path(self.temporary.name)))
        self.assertFalse((self.state_root / "collector.sock").exists())
        self.assertFalse((self.state_root / "client-runtimes").exists())

    def test_it_never_starts_the_collector_or_the_project_service(self) -> None:
        source = (ORCHESTRATION_ROOT / "cli.py").read_text()
        start = source.index('args.command == "learning-usage-report"')
        branch = source[start:source.index("elif args.command", start + 10)]
        for forbidden in ("_start_project_host", "_start_collector", "_learning_ledger", "CollectorMaintenance"):
            self.assertNotIn(forbidden, branch)

    def test_the_report_runs_while_the_project_store_is_open_in_a_running_service(self) -> None:
        self.finished(T0 + HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 2})
        # The store stays open here, as it is in a running project service.
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(CLI), "learning-usage-report", "--state-root", str(self.state_root),
             "--project-root", str(self.project), "--since", iso(T0), "--until", iso(T0 + 24 * HOUR)],
            capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(1, len(json.loads(done.stdout)["runs"]))
        self.assertTrue(os.path.exists(self.state_root))


if __name__ == "__main__":
    unittest.main()
