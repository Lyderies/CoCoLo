"""S44a: the usage report works on a real install, with the root the owner types, and says what it holds.

The owner's installed 0.8.67 refused `learning-usage-report --project-root . --since .. --until ..` with
`usage_state_unavailable` while `learning-status` worked. The repository's other usage tests run the source
tree with `--state-root` and an absolute project root, so they never took the owner's path. Here the CLI is the
installed copy of this package (its files and manifest as the installer writes them), run from inside the
project with `.` as the root and no `--state-root`, in a HOME and XDG_STATE_HOME of the test's own: the default
state root is the one the real `default_state_root()` gives. Nothing on the state, key, store or report path is
patched.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_bridge
from tests.support import SHORT_TMP
from tests.test_host_commands import install_package
from test_framework import requirements
from tests.test_usage_report import CODEX, CLAUDE, HOUR, T0, UsageReportCase, iso
from codex_agents.collector_paths import default_state_root
from codex_agents.learning_store import LearningLedger, LearningState
from codex_agents.project_history import ProjectHistory
from codex_agents.project_usage_report import usage_report


class InstalledUsageCase(UsageReportCase):
    """A project with the package installed, a default state root under a test HOME, and finished runs in its store."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP, prefix="su")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        home = base / "h"
        state_home = home / ".s" / "x"
        state_home.mkdir(parents=True, mode=0o700)
        for folder in (home, home / ".s", state_home):
            folder.chmod(0o700)
        self.environment = {"HOME": str(home), "XDG_STATE_HOME": str(state_home), "PATH": os.environ.get("PATH", "")}
        patch = mock.patch.dict(os.environ, self.environment)
        patch.start()
        self.addCleanup(patch.stop)
        self.state_root = state_home / "codex-agents"
        self.assertEqual(self.state_root, default_state_root())  # the real resolver
        self.project = base / "project"
        self.project.mkdir()
        install_package(self.project)
        self.cli = self.project / ".codex" / "orchestration" / "cli.py"
        state = LearningState(self.state_root)
        ledger = LearningLedger(state)
        self.project_id = ledger.register_project(self.project.resolve(), now_ms=1)
        ledger.close()
        directory = state.root / "projects" / self.project_id
        directory.mkdir(parents=True, mode=0o700)
        self.store = ProjectHistory(directory, self.project_id)
        self.counter = 0

    def run_installed(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", str(self.cli),
             "learning-usage-report", *arguments],
            capture_output=True, text=True, timeout=60, check=False, cwd=cwd or self.project, env=self.environment)

    def report(self, since: int = T0, until: int = T0 + 24 * HOUR, *extra: str, expected: int = 0,
               root: str = ".") -> dict:
        done = self.run_installed("--project-root", root, "--since", iso(since), "--until", iso(until), *extra)
        self.assertEqual(expected, done.returncode, done.stdout + done.stderr)
        return json.loads(done.stdout)


class InstalledUsageReportTests(InstalledUsageCase):
    __why_not_scenario__ = ('The installed copy of the package runs as a real subprocess against a real state root and '
                            'stores; there is no owner, provider or live service in a test, so no live scenario.')
    @requirements('S44A-1')
    def test_s44a_1_the_dot_root_the_owner_types_opens_the_default_state(self) -> None:
        # The owner's command: run inside the project, `.` as the root, no --state-root.
        self.finished(T0 + HOUR, usage={"input_tokens": 1000, "cached": 800, "output_tokens": 50})
        report = self.report()
        self.assertTrue(report["ready"], report)
        self.assertEqual(1, len(report["runs"]))
        self.assertEqual((1000, 50, 800), (report["runs"][0]["input_tokens"], report["runs"][0]["output_tokens"],
                                           report["runs"][0]["cache_read_input_tokens"]))

    @requirements('S44A-1')
    def test_s44a_1_an_absolute_root_and_a_dot_root_report_the_same_runs(self) -> None:
        self.finished(T0 + HOUR, usage={"input_tokens": 7, "cached": 1, "output_tokens": 2})
        self.assertEqual(self.report(root=str(self.project.resolve())), self.report(root="."))

    @requirements('S44A-1')
    def test_s44a_1_a_root_below_the_project_is_not_the_project(self) -> None:
        (self.project / "sub").mkdir()
        self.finished(T0 + HOUR, usage={"input_tokens": 7, "cached": 1, "output_tokens": 2})
        report = self.report(expected=2, root="sub")
        self.assertEqual((False, "usage_project_not_registered"), (report["ready"], report["reason_code"]))

    @requirements('S44A-1')
    def test_s44a_1_a_root_that_is_not_a_folder_is_named_not_called_a_state_problem(self) -> None:
        report = self.report(expected=2, root="no-such-folder")
        self.assertEqual("usage_project_root_invalid", report["reason_code"])
        self.assertIn("project", report["next_step"].lower())

    @requirements('S44A-1')
    def test_s44a_1_a_state_root_the_report_cannot_trust_is_still_refused_as_state(self) -> None:
        # No check is weakened: a key file other users can read stays refused, with the state's own reason.
        key = self.state_root / "digest.key"
        key.chmod(0o644)
        self.assertNotEqual(0o600, stat.S_IMODE(key.stat().st_mode))
        report = self.report(expected=2)
        self.assertEqual("usage_state_unavailable", report["reason_code"])
        key.chmod(0o600)
        self.assertTrue(self.report()["ready"])

    @requirements('S44A-1')
    def test_s44a_1_the_report_opens_the_state_learning_status_opens(self) -> None:
        # Both go through CollectorPaths.digest_key() for the same default root: one key, one set of checks.
        from codex_agents.collector_paths import CollectorPaths
        self.assertEqual(CollectorPaths().digest_key(), LearningState(self.state_root, create=False).digest_key())
        self.assertEqual(self.state_root.resolve(), CollectorPaths().root)

    @requirements('S44A-3')
    def test_s44a_3_a_window_between_recorded_runs_says_it_has_no_usage_data(self) -> None:
        self.finished(T0 + 2 * HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 1})
        self.finished(T0 + 50 * HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 1})
        report = self.report(T0 + 10 * HOUR, T0 + 20 * HOUR)
        self.assertTrue(report["ready"])
        self.assertEqual([], report["runs"])
        self.assertEqual("no_runs_in_window", report["window_data"]["state"])
        self.assertIn("no usage data", report["window_data"]["note"].lower())
        self.assertEqual(iso(T0 + 2 * HOUR), report["window_data"]["first_run_recorded"])
        self.assertEqual(iso(T0 + 50 * HOUR), report["window_data"]["last_run_recorded"])

    @requirements('S44A-3')
    def test_s44a_3_a_window_before_usage_was_recorded_names_the_first_record(self) -> None:
        self.finished(T0 + 5 * HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 1})
        report = self.report(T0, T0 + HOUR)
        self.assertEqual("before_usage_recorded", report["window_data"]["state"])
        self.assertIn(iso(T0 + 5 * HOUR), report["window_data"]["note"])

    @requirements('S44A-3')
    def test_s44a_3_a_store_with_no_usage_at_all_says_nothing_was_recorded(self) -> None:
        report = self.report()
        self.assertEqual("nothing_recorded", report["window_data"]["state"])
        self.assertIsNone(report["window_data"]["first_run_recorded"])

    @requirements('S44A-3')
    def test_s44a_3_a_window_with_runs_says_it_has_data(self) -> None:
        self.finished(T0 + HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 1})
        self.assertEqual("has_runs", self.report()["window_data"]["state"])
        self.finished(T0 + 2 * HOUR, usage=None, status="rejected")
        report = self.report()
        self.assertEqual(1, report["window_data"]["runs_without_usage"])


class RecordedChildRunsReachTheReportTests(test_bridge.BridgeTestCase):
    """S44a-2: a child's own output frames, dispatched through the real bridge, become tokens in the report.

    Each provider's child ends with the usage frame its client really writes (a Claude `result` frame, a Codex
    `turn.completed` frame; the fake clients replay recorded frames). The bridge records the usage, the host result
    records the outcome, and the report, which opens the same state, lists the run with its tokens.
    """

    __why_not_scenario__ = ('The real bridge and ledger run; the provider clients are the harness fakes that replay '
                            'recorded frames, so no model is launched.')

    def report(self) -> dict:
        now = int(time.time() * 1000)
        return usage_report(self.state, self.project.resolve(), now - HOUR, now + HOUR)

    def commit(self, route, assigned, handle, output) -> None:
        self.ledger.commit_host_result(
            self.project_id, assignment=assigned, task=self.task(), route=route, result=output["result"],
            observation={"run_handle": handle, "result_attempts": test_bridge.ACCEPTED},
            execution_configuration=route["execution_configuration"])

    @requirements('S44A-2')
    def test_s44a_2_a_claude_child_run_shows_its_tokens(self) -> None:
        route, assigned, handle = self.claude_run()
        frame = test_bridge.result_frame()
        frame["usage"] = {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 400,
                          "cache_creation_input_tokens": 80}
        self.frames("claude", [test_bridge.init_frame(), test_bridge.RATE_LIMIT, frame])
        output = self.dispatch(route, assigned, handle)
        self.commit(route, assigned, handle, output)
        report = self.report()
        (row,) = [run for run in report["runs"] if run["provider"] == "claude"]
        self.assertEqual((120, 30, 80, 400, True), (row["input_tokens"], row["output_tokens"],
                         row["cache_creation_input_tokens"], row["cache_read_input_tokens"], row["usage_complete"]))
        self.assertEqual("reported", report["usage_coverage"]["child_runs"]["claude"]["state"])

    @requirements('S44A-2')
    def test_s44a_2_a_codex_child_run_shows_its_tokens(self) -> None:
        self.admit("codex")
        route = self.bridged_route("codex")
        assigned = self.assignment_for(route)
        handle = self.open(route, assigned)
        self.frames("codex", [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 5,
                                                 "output_tokens": 9, "reasoning_output_tokens": 3}}])
        (self.tools / "codex-last-message.json").write_text(json.dumps(test_bridge.null_filled(test_bridge.result())))
        output = self.dispatch(route, assigned, handle)
        self.commit(route, assigned, handle, output)
        report = self.report()
        (row,) = [run for run in report["runs"] if run["provider"] == "codex"]
        self.assertEqual((50, 9, 5, True), (row["input_tokens"], row["output_tokens"],
                                            row["cache_read_input_tokens"], row["usage_complete"]))
        self.assertEqual("reported", report["usage_coverage"]["child_runs"]["codex"]["state"])
        self.assertEqual("no_runs", report["usage_coverage"]["child_runs"]["claude"]["state"])

    @requirements('S44A-2')
    def test_s44a_2_a_child_that_reported_no_usage_is_not_reported_never_zero(self) -> None:
        route, assigned, handle = self.claude_run()
        frame = test_bridge.result_frame()
        frame["usage"] = None
        self.frames("claude", [test_bridge.init_frame(), test_bridge.RATE_LIMIT, frame])
        self.commit(route, assigned, handle, self.dispatch(route, assigned, handle))
        report = self.report()
        (row,) = [run for run in report["runs"] if run["provider"] == "claude"]
        self.assertEqual((False, None, None), (row["usage_complete"], row["input_tokens"], row["output_tokens"]))
        self.assertEqual("not_reported", report["usage_coverage"]["child_runs"]["claude"]["state"])

    @requirements('S44A-2')
    def test_s44a_2_the_primary_session_is_plainly_not_reported(self) -> None:
        report = self.report()
        self.assertEqual("not_reported", report["usage_coverage"]["primary_session"]["state"])
        self.assertIn("hook", report["usage_coverage"]["primary_session"]["note"].lower())


class StatusRunUsageTests(InstalledUsageCase):
    """S44a-2: learning-status says where per-run usage is, instead of leaving `root_usage: false` to be misread."""

    __why_not_scenario__ = ('The real status helper reads a real default state root and stores; learning-status itself '
                            'needs a running collector, which a test does not start.')

    @requirements('S44A-2')
    def test_s44a_2_status_says_where_run_usage_is_without_opening_any_database(self) -> None:
        # learning-status is a client: it never opens a database (test_cli_status_client_boundary), so it names the
        # report that reads the runs instead of reading them.
        from codex_agents.project_usage_report import status_run_usage
        with mock.patch("sqlite3.connect", side_effect=AssertionError("status opened a database")):
            shown = status_run_usage()
        self.assertEqual("see_report", shown["state"])
        self.assertIn("learning-usage-report", shown["report_command"])
        self.assertIn("learning-usage-report", shown["note"])
        self.assertIn("hook", shown["note"].lower())

    @requirements('S44A-2')
    def test_s44a_2_learning_status_carries_the_run_usage_and_stays_read_only(self) -> None:
        source = (self.cli).read_text()
        start = source.index('elif args.command == "learning-status"')
        branch = source[start:source.index("elif args.command", start + 10)]
        self.assertIn("status_run_usage", branch)
        self.assertIn("run_usage", branch)

    @requirements('S44A-2')
    def test_s44a_2_every_json_output_here_is_strict_json_with_the_new_fields(self) -> None:
        # Plain-language lines belong to human output only: each JSON document parses with no extra text and no
        # NaN or Infinity, and carries run_usage / usage_coverage / window_data.
        from codex_agents.project_usage_report import status_run_usage

        def strict(text):
            return json.loads(text, parse_constant=lambda name: self.fail(f"non-JSON constant {name}"))

        self.finished(T0 + HOUR, usage={"input_tokens": 5, "cached": 1, "output_tokens": 1})
        done = self.run_installed("--project-root", ".", "--since", iso(T0), "--until", iso(T0 + 24 * HOUR))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        document = strict(done.stdout)
        self.assertIn("usage_coverage", document)
        self.assertIn("window_data", document)
        run_usage = strict(json.dumps(status_run_usage(), allow_nan=False))
        self.assertEqual({"state", "report_command", "window_days", "note"}, set(run_usage))



if __name__ == "__main__":
    unittest.main()
