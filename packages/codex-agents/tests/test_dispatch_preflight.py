from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import assignment, routing_fields
import cli
from codex_agents import bridge, git_plumbing
from codex_agents.contracts import ContractError
from codex_agents.routing import load_policy, route_task


class DispatchPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "source.txt").write_text("CANARY-COMMITTED-CONTENT\n")
        self.commit()
        self.task = {
            "id": "task-1", "objective": "Inspect source", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            "deterministic_validation": False, "independent_parts": 1, "parallel_safe": False,
            "failed_attempts": 0, "failure_events": [], **routing_fields(),
        }
        self.route = route_task(self.task, load_policy(ORCHESTRATION_ROOT / "config/routing-policy.json"),
                                primary_provider="codex")
        self.assigned = assignment()
        self.assigned.update(role=self.route["role"], model=self.route["model"], effort=self.route["effort"],
                             verification_required=self.route["verification_required"])
        self.grant = deepcopy(self.assigned["permissions"])

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root), *arguments],
                              check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def preview(self, **options):
        return bridge.dispatch_preflight(self.root, self.task, self.route, self.assigned, **options)

    def test_committed_metadata_matches_export_without_reading_or_returning_content(self) -> None:
        (self.root / "source.txt").write_text("CANARY-UNCOMMITTED\n")
        (self.root / "untracked.txt").write_text("not admitted\n")
        original = bridge._git
        with mock.patch.object(bridge, "_git", wraps=original) as calls, \
                mock.patch.object(bridge, "private_workspace", side_effect=AssertionError("workspace created")), \
                mock.patch.object(bridge, "run_child", side_effect=AssertionError("child launched")):
            report = self.preview()
        self.assertFalse(any("cat-file" in call.args for call in calls.call_args_list))
        self.assertEqual(["source.txt"], [item["path"] for item in report["files"]])
        self.assertNotIn("CANARY", json.dumps(report))
        exported = self.base / "export"
        exported.mkdir()
        actual = bridge.export_committed_snapshot(self.root, ["source.txt"], exported)
        self.assertEqual(report["export_commit"], actual["commit"])
        self.assertEqual(report["export_manifest_digest"], actual["manifest_digest"])
        self.assertEqual("CANARY-COMMITTED-CONTENT\n", (exported / "source.txt").read_text())
        self.assertFalse(report["admission_created"])

    def test_ref_change_between_requested_paths_cannot_mix_snapshots(self) -> None:
        (self.root / "second.txt").write_text("old\n")
        self.commit()
        old = self.git("rev-parse", "HEAD").stdout.decode().strip()
        original = bridge._git
        trees = []

        def move_head(root, *arguments, **options):
            result = original(root, *arguments, **options)
            if "ls-tree" in arguments:
                trees.append(arguments)
                if len(trees) == 1:
                    (self.root / "second.txt").write_text("new content\n")
                    self.commit()
            return result

        exported = self.base / "export"
        exported.mkdir()
        with mock.patch.object(bridge, "_git", side_effect=move_head):
            actual = bridge.export_committed_snapshot(self.root, ["source.txt", "second.txt"], exported)
        self.assertEqual(old, actual["commit"])
        self.assertEqual("old\n", (exported / "second.txt").read_text())
        self.assertTrue(all(old in arguments for arguments in trees))
        self.assertNotEqual(old, self.git("rev-parse", "HEAD").stdout.decode().strip())
        with self.assertRaisesRegex(bridge.BridgeRefused, "commit changed"):
            bridge.committed_export_manifest(self.root, ["source.txt"], expected_commit=old)

    def test_pages_are_bounded_and_cannot_silently_switch_commits(self) -> None:
        folder = self.root / "docs"
        folder.mkdir()
        for index in range(140):
            (folder / (f"{index:03d}-" + "n" * 190)).write_text("x\n")
        (folder / "CLAUDE.md").write_text("not exported\n")
        self.commit()
        self.assigned.update(context=["docs"], owned_paths=[])
        self.assigned["permissions"]["filesystem"] = ["docs:read"]
        first = self.preview(limit=100)
        self.assertLessEqual(len(json.dumps(first, indent=2, sort_keys=True).encode()) + 1, 16_384)
        self.assertLess(len(first["files"]), 100)
        self.assertEqual(140, first["repository_file_count"])
        with self.assertRaisesRegex(bridge.BridgeRefused, "expected export commit"):
            self.preview(offset=first["next_offset"])
        paths = [item["path"] for item in first["files"]]
        offset = first["next_offset"]
        while offset is not None:
            page = self.preview(offset=offset, limit=100, expected_commit=first["export_commit"])
            self.assertEqual(first["export_manifest_digest"], page["export_manifest_digest"])
            self.assertLessEqual(len(json.dumps(page, indent=2, sort_keys=True).encode()) + 1, 16_384)
            paths.extend(item["path"] for item in page["files"])
            offset = page["next_offset"]
        self.assertEqual(140, len(set(paths)))
        self.assertFalse(any("CLAUDE.md" in path for path in paths))

    def test_grants_and_page_shapes_are_not_widened(self) -> None:
        before = deepcopy(self.assigned)
        for options in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 101}):
            with self.subTest(options=options), mock.patch.object(bridge, "_git", side_effect=AssertionError("git called")), \
                    self.assertRaisesRegex(bridge.BridgeRefused, "page is invalid"):
                self.preview(**options)
        self.assertEqual(before, self.assigned)
        self.assigned["context"] = ["ungranted.txt"]
        with self.assertRaisesRegex(bridge.BridgeRefused, "export_path_not_granted"):
            self.preview()

    def test_invalid_exports_refuse_and_size_caps_precede_blob_reads(self) -> None:
        (self.root / "link").symlink_to("source.txt")
        self.commit()
        for paths, message in ((["link"], "symlinks"), (["missing"], "not committed"),
                               (["../source.txt"], "inside the repository"),
                               ([str(self.root / "source.txt")], "inside the repository")):
            with self.subTest(paths=paths), self.assertRaisesRegex(bridge.BridgeRefused, message):
                bridge.committed_export_manifest(self.root, paths)
        original = bridge._git

        def no_blob(root, *arguments, **options):
            self.assertNotIn("cat-file", arguments)
            return original(root, *arguments, **options)

        with mock.patch.object(bridge, "_git", side_effect=no_blob), \
                self.assertRaisesRegex(bridge.BridgeRefused, "too large"):
            bridge.export_committed_snapshot(self.root, ["source.txt"], self.base / "absent-export", max_bytes=3)
        self.assertFalse((self.base / "absent-export").exists())

    def test_overlapping_paths_do_not_double_count_or_change_the_manifest(self) -> None:
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "a").write_text("a\n")
        self.commit()
        one = bridge.committed_export_manifest(self.root, ["docs"])
        overlap = bridge.committed_export_manifest(self.root, ["docs", "docs/a", "docs"])
        self.assertEqual(one, overlap)

    def test_capabilities_report_actual_tools_and_units(self) -> None:
        self.assigned["bounds"].update(timeout_seconds=5, max_tool_calls=7)
        for provider in ("codex", "claude"):
            self.route["provider"] = provider
            capabilities = self.preview()["capabilities"]
            self.assertEqual(self.route["role"], capabilities["role"])
            self.assertEqual(self.route["model"], capabilities["model"])
            self.assertEqual(self.route["effort"], capabilities["effort"])
            self.assertEqual(5, capabilities["hard_timeout_seconds"])
            self.assertEqual(7, capabilities["nominal_max_tool_calls"])
            self.assertIsNone(capabilities["exact_tool_call_limit"])
            self.assertFalse(capabilities["per_path_read_isolation"])
            self.assertEqual(["shell"], capabilities["assignment_tools"])
            if provider == "claude":
                self.assertEqual(["Read", "Glob", "Grep"], capabilities["client_tool_allowlist"])
                self.assertEqual(7, capabilities["model_turn_limit"])
                self.assertEqual("client --max-turns", capabilities["model_turn_enforcement"])
            else:
                self.assertIsNone(capabilities["model_turn_limit"])
                self.assertEqual("unavailable", capabilities["model_turn_enforcement"])

        self.route["provider"] = "claude"
        self.assigned["bounds"]["max_tool_calls"] = 0
        capabilities = self.preview()["capabilities"]
        self.assertEqual(0, capabilities["nominal_max_tool_calls"])
        self.assertEqual(1, capabilities["model_turn_limit"])

    def test_unsupported_or_writable_preview_refuses_before_git(self) -> None:
        for route in ({**self.route, "dispatch": "native"}, {**self.route, "sandbox_mode": "workspace-write"}):
            with self.subTest(route=route["dispatch"]), \
                    mock.patch.object(bridge, "_git", side_effect=AssertionError("git called")), \
                    self.assertRaises(bridge.BridgeRefused):
                bridge.dispatch_preflight(self.root, self.task, route, self.assigned)

    def test_explicit_turns_report_actual_units_and_unsupported_limits_precede_git(self) -> None:
        self.route.update(provider="claude", dispatch="bridge")
        self.assigned["bounds"]["max_tool_calls"] = 7
        self.assigned["execution_limits"] = {"max_model_turns": 2}
        caps = self.preview()["capabilities"]
        self.assertEqual((7, 2, None), (caps["nominal_max_tool_calls"],
                                      caps["model_turn_limit"], caps["exact_tool_call_limit"]))
        self.assertEqual("explicit", caps["execution_limit_contract"])
        self.assertEqual({"max_model_turns": 2}, caps["requested_execution_limits"])
        self.assertEqual("execution_limits.max_model_turns", caps["model_turn_limit_source"])
        for provider, limits in (("codex", {"max_model_turns": 1}),
                                 ("codex", {"max_tool_calls": 0}),
                                 ("claude", {"max_tool_calls": 0}),
                                 ("claude", {"max_model_turns": 8})):
            self.route["provider"] = provider
            self.assigned["execution_limits"] = limits
            with self.subTest(provider=provider, limits=limits), \
                    mock.patch.object(bridge, "_git", side_effect=AssertionError("git called")), \
                    self.assertRaises(ContractError):
                self.preview()

    def test_reserved_worker_input_is_indicated_without_a_repository_path_or_body(self) -> None:
        self.task["verification_for_outcome"] = "f" * 64
        self.assigned["context"] = ["f" * 64]
        self.assigned["permissions"]["filesystem"] = []
        report = self.preview()
        self.assertTrue(report["reserved_worker_result"])
        self.assertEqual([], report["files"])
        self.assertNotIn("CANARY", json.dumps(report))

    def test_folded_git_and_claude_context_names_are_not_materialized(self) -> None:
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "safe").write_text("x\n")
        self.commit()
        original = bridge._git

        def listing_as(name):
            def changed(root, *arguments, **options):
                completed = original(root, *arguments, **options)
                if "ls-tree" in arguments:
                    completed.stdout = completed.stdout.replace(b"docs/safe", name.encode())
                return completed
            return changed

        for name in ("docs/.GiT/config", "docs/.G\u200biT/config"):
            with self.subTest(name=name), mock.patch.object(bridge, "_git", side_effect=listing_as(name)), \
                    self.assertRaisesRegex(bridge.BridgeRefused, "inside the repository"):
                bridge.committed_export_manifest(self.root, ["docs"])
        with mock.patch.object(bridge, "_git", side_effect=listing_as("docs/CLAU\u200bDE.md")):
            self.assertEqual(0, bridge.committed_export_manifest(self.root, ["docs"])["file_count"])

    def test_cli_refuses_explicit_limits_before_collector_maintenance(self) -> None:
        self.assigned["execution_limits"] = {"max_tool_calls": 0}
        for name, data in (("task", self.task), ("route", self.route),
                           ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        for command, binding in (("dispatch", ["--run-handle", "not-opened"]),
                                 ("learning-open-host-run", ["--orchestration-handle", "not-opened"])):
            argv = ["cli.py", command, "--project-root", str(self.root),
                    "--task", str(self.base / "task.json"),
                    "--route", str(self.base / "route.json"),
                    "--assignment", str(self.base / "assignment.json"),
                    "--parent-grant", str(self.base / "grant.json"), *binding]
            output = io.StringIO()
            with self.subTest(command=command), mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(output), \
                    mock.patch.object(cli, "_learning_ledger",
                                      side_effect=AssertionError("collector maintenance contacted")):
                self.assertEqual(2, cli.main())
            self.assertEqual("assignment.execution_limits.max_tool_calls",
                             json.loads(output.getvalue())["field"])

    def test_cli_refuses_malformed_route_limits_before_collector_maintenance(self) -> None:
        self.route.update(provider="claude", dispatch="bridge")
        self.assigned["execution_limits"] = {"max_model_turns": 1}
        for name, data in (("task", self.task), ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        for command, binding in (("dispatch", ["--run-handle", "not-opened"]),
                                 ("learning-open-host-run", ["--orchestration-handle", "not-opened"])):
            for ceiling in (None, "3"):
                self.route["bounds"]["max_tool_calls"] = ceiling
                (self.base / "route.json").write_text(json.dumps(self.route))
                argv = ["cli.py", command, "--project-root", str(self.root),
                        "--task", str(self.base / "task.json"),
                        "--route", str(self.base / "route.json"),
                        "--assignment", str(self.base / "assignment.json"),
                        "--parent-grant", str(self.base / "grant.json"), *binding]
                output = io.StringIO()
                with self.subTest(command=command, ceiling=ceiling):
                    with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                            mock.patch.object(cli, "_learning_ledger",
                                              side_effect=AssertionError("collector contacted")):
                        self.assertEqual(2, cli.main())
                    self.assertEqual("route.bounds.max_tool_calls",
                                     json.loads(output.getvalue())["field"])

    def test_cli_preflight_does_not_create_learning_state(self) -> None:
        for name, data in (("task", self.task), ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        state = self.base / "state-must-not-be-created"
        command = [sys.executable, "-I", "-B", "-S", str(ORCHESTRATION_ROOT / "cli.py"),
                   "dispatch-preflight", "--project-root", str(self.root), "--state-root", str(state),
                   "--task", str(self.base / "task.json"), "--assignment", str(self.base / "assignment.json"),
                   "--parent-grant", str(self.base / "grant.json")]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        # A route that needs nothing from the ledger is pure policy: the preview is
        # given without any state, and creates none.
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ready"])
        self.assertTrue(report["preflight_only"])
        self.assertFalse(report["admission_created"])
        self.assertFalse(state.exists())

    def test_cli_uses_the_trusted_route_including_released_model_and_effort_changes(self) -> None:
        learned = {**self.route, "model": "gpt-6-astra", "effort": "medium",
                   "learning_candidate_digests": ["d" * 64]}
        self.assigned.update(model=learned["model"], effort=learned["effort"])
        for name, data in (("task", self.task), ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        argv = ["cli.py", "dispatch-preflight", "--project-root", str(self.root),
                "--task", str(self.base / "task.json"), "--assignment", str(self.base / "assignment.json"),
                "--parent-grant", str(self.base / "grant.json")]
        ledger = object()
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                mock.patch.object(cli, "_learning_ledger", return_value=(ledger, "project-fixture")), \
                mock.patch.object(cli, "_close_ledger") as close, \
                mock.patch.object(cli, "_store_free_route", return_value=None), \
                mock.patch.object(cli, "route_task", side_effect=AssertionError("base route used")), \
                mock.patch.object(cli, "_trusted_host_route", return_value=(learned, {})) as resolve:
            self.assertEqual(0, cli.main())
        resolve.assert_called_once_with(ledger, "project-fixture", self.root, self.task)
        close.assert_called_once_with(ledger)
        report = json.loads(output.getvalue())
        self.assertEqual("gpt-6-astra", report["capabilities"]["model"])
        self.assertEqual("medium", report["capabilities"]["effort"])
        self.assertFalse(report["admission_created"])

        # The refusal matches dispatch, before state or task files are opened.
        with mock.patch.object(sys, "argv", [*argv, "--fallback-receipt", "receipt-fixture"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "_learning_ledger", side_effect=AssertionError("state opened")):
            self.assertEqual(2, cli.main())

    def test_cli_refusal_of_an_uncommitted_read_path_says_it_admitted_nothing(self) -> None:
        # batch2 decision 167 (L6.2): every dispatch-preflight refusal carries admission_created false.
        for name, data in (("task", self.task), ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        argv = ["cli.py", "dispatch-preflight", "--project-root", str(self.root),
                "--task", str(self.base / "task.json"), "--assignment", str(self.base / "assignment.json"),
                "--parent-grant", str(self.base / "grant.json")]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                mock.patch.object(cli, "_learning_ledger", return_value=(object(), "project-fixture")), \
                mock.patch.object(cli, "_close_ledger"), \
                mock.patch.object(cli, "_store_free_route", return_value=None), \
                mock.patch.object(cli, "_trusted_host_route", return_value=(self.route, {})), \
                mock.patch.object(cli, "dispatch_preflight", side_effect=bridge.BridgeRefused("assignment read path is not committed")):
            self.assertEqual(2, cli.main())
        report = json.loads(output.getvalue())
        self.assertIs(False, report["ready"])
        self.assertIn("not committed", report["reason"])
        self.assertIs(False, report["admission_created"])

    def test_cli_refuses_policy_override_before_state_or_inputs_are_opened(self) -> None:
        argv = ["cli.py", "dispatch-preflight", "--project-root", str(self.root),
                "--task", "missing-task.json", "--assignment", "missing-assignment.json",
                "--parent-grant", "missing-grant.json", "--policy", "missing-policy.json"]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                mock.patch.object(cli, "_learning_ledger", side_effect=AssertionError("state opened")), \
                mock.patch.object(cli, "_json_input", side_effect=AssertionError("input opened")):
            self.assertEqual(2, cli.main())
        self.assertFalse(json.loads(output.getvalue())["ready"])
        self.assertIn("--policy is refused", output.getvalue())

    def test_bounded_git_output_and_total_timeout_reap_the_fixture_process(self) -> None:
        executable = bridge.find_git(untrusted=[self.root])
        with self.assertRaises(git_plumbing.GitOutputLimitError) as caught:
            git_plumbing.run_git(executable, self.root, "cat-file", "blob", "HEAD:source.txt", max_output_bytes=3)
        self.assertNotIn("CANARY", str(caught.exception))
        script = self.base / "drip-git"
        script.write_text(f"#!{sys.executable}\nimport time\nwhile True:\n print('x', flush=True)\n time.sleep(0.03)\n")
        script.chmod(0o700)
        real_popen = subprocess.Popen
        spawned = []

        def launch(*args, **options):
            process = real_popen(*args, **options)
            spawned.append(process)
            return process

        started = time.monotonic()
        with mock.patch.object(git_plumbing.subprocess, "Popen", side_effect=launch), \
                self.assertRaises(subprocess.TimeoutExpired):
            git_plumbing.run_git(str(script), self.root, "unused", timeout=0.15, max_output_bytes=65536)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(spawned[0].returncode)

    def test_export_shares_one_absolute_budget_across_metadata_and_blob_reads(self) -> None:
        exported = self.base / "export"
        exported.mkdir()
        original = git_plumbing._bounded_git_output
        with mock.patch.object(git_plumbing, "_bounded_git_output", wraps=original) as calls:
            bridge.export_committed_snapshot(self.root, ["source.txt"], exported)
        deadlines = [call.kwargs["deadline"] for call in calls.call_args_list]
        self.assertGreaterEqual(len(deadlines), 5)
        self.assertTrue(all(value is not None and value == deadlines[0] for value in deadlines))

    def test_late_blob_read_cannot_restart_export_budget_or_publish_content(self) -> None:
        now = [time.monotonic()]
        original = bridge._git
        blobs = []

        def delayed(root, *arguments, **options):
            completed = original(root, *arguments, **options)
            if "cat-file" in arguments:
                blobs.append(1)
                now[0] += bridge.MAX_EXPORT_SECONDS + 1
            return completed

        exported = self.base / "export"
        exported.mkdir()
        with mock.patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), \
                mock.patch.object(bridge, "_git", side_effect=delayed), \
                self.assertRaisesRegex(bridge.BridgeRefused, "export deadline exceeded"):
            bridge.export_committed_snapshot(self.root, ["source.txt"], exported)
        self.assertEqual(1, len(blobs))
        self.assertEqual([], list(exported.iterdir()))

    def test_expired_absolute_git_deadline_never_starts_a_process(self) -> None:
        with mock.patch.object(git_plumbing.subprocess, "Popen", side_effect=AssertionError("process started")), \
                self.assertRaises(subprocess.TimeoutExpired):
            git_plumbing.run_git("fixture-git", self.root, "rev-parse", "HEAD", max_output_bytes=100,
                                 deadline=time.monotonic() - 1)


if __name__ == "__main__":
    unittest.main()
