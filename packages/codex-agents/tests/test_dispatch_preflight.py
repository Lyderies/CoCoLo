from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import assignment, routing_fields
from codex_agents import bridge, git_plumbing
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
            if provider == "claude":
                self.assertEqual(["Read", "Glob", "Grep"], capabilities["client_tool_allowlist"])
                self.assertEqual(7, capabilities["model_turn_limit"])
            else:
                self.assertIsNone(capabilities["model_turn_limit"])

    def test_cli_preflight_does_not_create_learning_state(self) -> None:
        for name, data in (("task", self.task), ("assignment", self.assigned), ("grant", self.grant)):
            (self.base / f"{name}.json").write_text(json.dumps(data))
        state = self.base / "state-must-not-be-created"
        command = [sys.executable, "-I", "-B", "-S", str(ORCHESTRATION_ROOT / "cli.py"),
                   "dispatch-preflight", "--project-root", str(self.root), "--state-root", str(state),
                   "--task", str(self.base / "task.json"), "--assignment", str(self.base / "assignment.json"),
                   "--parent-grant", str(self.base / "grant.json")]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        self.assertTrue(json.loads(completed.stdout)["preflight_only"])
        self.assertFalse(state.exists())

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


if __name__ == "__main__":
    unittest.main()
