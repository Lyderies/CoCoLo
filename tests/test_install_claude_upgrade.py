"""The umbrella installer upgrades a project whose Claude package is the previous release (0.8.40)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_install import git, installer, inventory, isolate_home, make_target  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "claude-agents"
MANIFEST = ".claude/claude-agents/install-manifest.json"


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ClaudeUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
        self.target, self.codex_options = make_target(self.base)
        done = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(done["ok"], done)
        # Put the previous release's Claude files and manifest in place of the new ones.
        release = json.loads((PACKAGE / "previous-releases.json").read_text())["releases"][0]
        files = dict(release["files"])
        manifest = {"schema_version": 1, "namespace": "claude-agents",
                    "package_version": release["package_version"], "provider_execution_enabled": False,
                    "native_discovery_verified": False, "model_isolation": "not_provided",
                    "files": {p: hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(files.items())}}
        manifest["bundle_sha256"] = hashlib.sha256(canonical(manifest)).hexdigest()
        files[MANIFEST] = canonical(manifest).decode()
        for path, content in files.items():
            (self.target / path).write_text(content)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "claude package 0.8.40")

    def test_preview_and_apply_upgrade_the_previous_claude_release(self) -> None:
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertTrue(preview["ok"], preview)
        self.assertEqual("preview", preview["claude"]["result"])
        self.assertEqual("update", preview["claude"]["operation"])
        applied = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(applied["ok"] and applied["applied"], applied)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "upgrade")
        verified = installer.operate("verify", self.target)
        self.assertEqual("verified", verified["claude"]["result"], verified)

    def test_a_tampered_previous_file_is_refused_at_preview(self) -> None:
        path = self.target / ".claude/agents/claude_agents_planner.md"
        path.write_text(path.read_text() + "\nextra\n")
        git(self.target, "commit", "-qam", "tamper")
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertFalse(preview["ok"], preview)
        self.assertEqual("managed_file_modified", preview["claude"]["error"])

    def test_an_unknown_claude_manifest_is_refused_at_preview(self) -> None:
        path = self.target / MANIFEST
        manifest = json.loads(path.read_text())
        manifest["bundle_sha256"] = "0" * 64
        path.write_bytes(canonical(manifest))
        git(self.target, "commit", "-qam", "unknown")
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertFalse(preview["ok"], preview)
        self.assertEqual("untrusted_manifest", preview["claude"]["error"])

    def patch(self, name: str, value) -> None:
        original = getattr(installer, name)
        setattr(installer, name, value)
        self.addCleanup(setattr, installer, name, original)

    def assert_stays_on_the_previous_install(self, summary: dict, before: dict) -> None:
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(summary["applied"], summary)
        self.assertEqual(before, inventory(self.target))

    def test_a_failed_settings_step_after_the_claude_update_restores_the_previous_install(self) -> None:
        before = inventory(self.target)

        def failing_settings(target, plan):
            raise installer.InstallRefused("simulated settings failure")

        self.patch("apply_settings", failing_settings)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertEqual(["claude", "codex"], [r["package"] for r in summary["rollback"]])
        self.assertTrue(all(r["ok"] for r in summary["rollback"]), summary["rollback"])
        self.assert_stays_on_the_previous_install(summary, before)

    def test_a_claude_update_that_applied_and_then_reported_failure_is_restored(self) -> None:
        before = inventory(self.target)
        real_run = installer._run

        def applies_then_fails(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                return {"error": "operation_failed", "exit_code": 2}
            return report

        self.patch("_run", applies_then_fails)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assert_stays_on_the_previous_install(summary, before)

    def test_a_failed_update_leaves_the_project_able_to_upgrade_again(self) -> None:
        real_run = installer._run
        failures = []

        def applies_then_fails_once(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply and not failures:
                failures.append(1)
                return {"error": "operation_failed", "exit_code": 2}
            return report

        self.patch("_run", applies_then_fails_once)
        self.assertFalse(installer.operate("install", self.target, apply=True,
                                           codex_options=self.codex_options)["ok"])
        again = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(again["ok"] and again["applied"], again)

    def commit(self, message: str) -> None:
        git(self.target, "add", "-A")
        git(self.target, "commit", "-qm", message)

    def make_read_only_for_the_last_change(self) -> None:
        """Make the package's own write fail partway: the last file it would change cannot be written."""
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        changed = [p for p in preview["claude"]["changed_paths"] if p != MANIFEST]
        self.assertGreaterEqual(len(changed), 2, changed)
        folder = (self.target / sorted(changed)[-1]).parent
        folder.chmod(0o555)
        self.addCleanup(folder.chmod, 0o755)

    def test_an_update_that_fails_partway_leaves_the_previous_install_and_no_markers(self) -> None:
        before = inventory(self.target)
        self.make_read_only_for_the_last_change()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assert_stays_on_the_previous_install(summary, before)
        self.assertIn("error", summary["claude"], summary)
        self.assertTrue(all(r["ok"] for r in summary["rollback"]), summary["rollback"])

    def test_files_the_user_changes_while_an_update_fails_survive_the_rollback(self) -> None:
        other = self.target / ".claude" / "skills" / "mine" / "SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("mine v1\n")
        notes = self.target / ".agents" / "notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes v1\n")
        self.commit("user files")
        real_run = installer._run

        def user_edits_then_fails(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                other.write_text("mine v2\n")
                notes.write_text("notes v2\n")
                (self.target / ".claude" / "agents" / "my_agent.md").write_text("a new user agent\n")
                return {"error": "operation_failed", "exit_code": 2}
            return report

        self.patch("_run", user_edits_then_fails)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("mine v2\n", other.read_text())
        self.assertEqual("notes v2\n", notes.read_text())
        self.assertEqual("a new user agent\n", (self.target / ".claude/agents/my_agent.md").read_text())
        planner = self.target / ".claude/agents/claude_agents_planner.md"
        self.assertNotIn("model: sonnet", planner.read_text())  # the package's own file is back to 0.8.40

    def test_a_package_that_keeps_unfinished_changes_is_left_alone(self) -> None:
        real_run = installer._run
        pending = self.target / ".claude/claude-agents/install-pending.json"
        edited = self.target / ".claude/agents/claude_agents_planner.md"

        def leaves_a_pending_marker(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                pending.write_text("{}\n")
                edited.write_text("an unknown edit the package kept\n")
                return {"error": "rollback_pending", "exit_code": 2}
            return report

        self.patch("_run", leaves_a_pending_marker)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("{}\n", pending.read_text())
        self.assertEqual("an unknown edit the package kept\n", edited.read_text())
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        self.assertFalse(claude["ok"], claude)
        self.assertIn("rollback pending", claude["note"])

    def test_a_user_symlink_under_claude_does_not_block_the_upgrade(self) -> None:
        elsewhere = self.base / "shared-skills"
        elsewhere.mkdir()
        (elsewhere / "SKILL.md").write_text("shared\n")
        (self.target / ".claude" / "skills" / "shared").symlink_to(elsewhere, target_is_directory=True)
        (self.target / ".agents" / "linked.md").symlink_to(elsewhere / "SKILL.md")
        self.commit("user symlinks")
        applied = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(applied["ok"] and applied["applied"], applied)
        self.assertEqual("shared\n", (elsewhere / "SKILL.md").read_text())

    def test_an_owned_folder_swapped_for_a_symlink_is_refused_and_nothing_outside_is_touched(self) -> None:
        outside = self.base / "outside"
        (outside / "agents").mkdir(parents=True)
        sentinel = outside / "agents" / "claude_agents_planner.md"
        sentinel.write_text("outside content\n")
        stray = outside / "stray.txt"
        stray.write_text("stray\n")
        real_run = installer._run

        def swaps_the_folder_then_fails(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                (self.target / ".claude").rename(self.base / "claude-moved")
                (self.target / ".claude").symlink_to(outside, target_is_directory=True)
                return {"error": "operation_failed", "exit_code": 2}
            return report

        self.patch("_run", swaps_the_folder_then_fails)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("outside content\n", sentinel.read_text())
        self.assertEqual("stray\n", stray.read_text())
        self.assertEqual({"agents/claude_agents_planner.md", "stray.txt"},
                         {p.relative_to(outside).as_posix() for p in outside.rglob("*") if p.is_file()})
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        self.assertFalse(claude["ok"], claude)

    def test_a_symlinked_claude_folder_is_refused_before_anything_changes(self) -> None:
        moved = self.base / "claude-real"
        (self.target / ".claude").rename(moved)
        (self.target / ".claude").symlink_to(moved, target_is_directory=True)
        git(self.target, "add", "-A")
        git(self.target, "commit", "-qm", "symlinked .claude")
        before = inventory(self.target)
        outside_before = {p.relative_to(moved).as_posix(): p.read_bytes() for p in moved.rglob("*") if p.is_file()}
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(summary["applied"], summary)
        self.assertEqual(before, inventory(self.target))
        self.assertEqual(outside_before, {p.relative_to(moved).as_posix(): p.read_bytes()
                                          for p in moved.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
