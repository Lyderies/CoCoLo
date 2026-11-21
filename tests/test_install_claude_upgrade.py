"""The umbrella installer upgrades a project whose Claude package is the previous release (0.8.40)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "scripts"))

from test_install import PROJECT_CONFIG, git, installer, inventory, isolate_home, make_target  # noqa: E402

from setup_wizard import steps  # noqa: E402

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
        lock = self.target / ".claude/claude-agents/install.lock"
        edited = self.target / ".claude/agents/claude_agents_planner.md"

        def leaves_a_pending_marker(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                pending.write_text("{}\n")
                lock.write_text("token\n")
                edited.write_text("an unknown edit the package kept\n")
                return {"error": "operation_failed", "rollback_state": "pending", "exit_code": 2}
            return report

        self.patch("_run", leaves_a_pending_marker)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("{}\n", pending.read_text())
        self.assertEqual("token\n", lock.read_text())
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

    # --- one boundary: only what this run wrote is ever undone --------------------------------

    def test_a_package_file_the_user_edits_after_the_update_is_left_alone(self) -> None:
        planner = self.target / ".claude/agents/claude_agents_planner.md"
        skill = self.target / ".claude/skills/claude-agents/SKILL.md"

        def user_edits_then_settings_fail(target, plan):
            planner.write_text("the user's own edit after the update\n")
            raise installer.InstallRefused("simulated settings failure")

        self.patch("apply_settings", user_edits_then_settings_fail)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("the user's own edit after the update\n", planner.read_text())
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        self.assertFalse(claude["ok"], claude)
        self.assertEqual([".claude/agents/claude_agents_planner.md"], claude["left_alone"])
        release = json.loads((PACKAGE / "previous-releases.json").read_text())["releases"][0]
        self.assertEqual(release["files"][".claude/skills/claude-agents/SKILL.md"], skill.read_text())

    def test_a_failure_without_the_structured_field_is_undone_even_when_the_text_says_pending(self) -> None:
        before = inventory(self.target)
        real_run = installer._run

        def says_pending_in_words(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                return {"error": "rollback_pending", "exit_code": 2}
            return report

        self.patch("_run", says_pending_in_words)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assert_stays_on_the_previous_install(summary, before)

    def test_the_structured_field_alone_keeps_a_package_whatever_the_error_text(self) -> None:
        real_run = installer._run
        pending = self.target / ".claude/claude-agents/install-pending.json"

        def pending_by_field(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                pending.write_text("{}\n")
                return {"error": "something else entirely", "rollback_state": "pending", "exit_code": 2}
            return report

        self.patch("_run", pending_by_field)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(pending.is_file())
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        self.assertFalse(claude["ok"], claude)

    def test_after_a_kept_rollback_the_advised_steps_make_setup_work_again(self) -> None:
        real_run = installer._run
        pending = self.target / ".claude/claude-agents/install-pending.json"
        lock = self.target / ".claude/claude-agents/install.lock"
        state = {"fail": True}

        def kept_once(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply and state["fail"]:
                pending.write_text("{}\n")
                lock.write_text("token\n")
                (self.target / ".claude/agents/claude_agents_planner.md").write_text("half written\n")
                return {"error": "operation_failed", "rollback_state": "pending", "exit_code": 2}
            return report

        self.patch("_run", kept_once)
        first = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(first["ok"], first)
        state["fail"] = False
        # Run again without following any advice: refused, naming the marker, nothing changed.
        stuck = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(stuck["ok"], stuck)
        self.assertIn("installation_pending", stuck["claude"]["error"])
        message, known = steps.installer_refusal(steps.raw(stuck), "manual")
        self.assertTrue(known)
        for advised in (".claude/claude-agents/install-pending.json", ".claude/claude-agents/install.lock"):
            self.assertIn(advised, message)
        # The advised steps: put the package files back from Git and remove the two control files.
        git(self.target, "checkout", "--", ".claude")
        pending.unlink()
        lock.unlink()
        again = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(again["ok"] and again["applied"], again)

    def test_a_special_file_at_an_owned_path_is_refused_without_being_opened(self) -> None:
        (self.target / ".claude/agents").mkdir(exist_ok=True)
        fifo = self.target / ".claude/agents/claude_agents_extra.md"
        os.mkfifo(fifo)

        def alarm(signum, frame):
            raise AssertionError("the snapshot blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(10)
        self.addCleanup(signal.alarm, 0)
        with self.assertRaises(installer.InstallRefused) as caught:
            installer._snapshot(self.target, "claude", {"changed_paths": [".claude/agents/claude_agents_extra.md"]})
        self.assertIn("not a regular file", str(caught.exception))
        message, known = steps.installer_refusal(str(caught.exception).lower(), "manual")
        self.assertTrue(known, message)

    def test_an_unrelated_special_file_does_not_block_the_upgrade(self) -> None:
        os.mkfifo(self.target / ".claude/unrelated.fifo")
        applied = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(applied["ok"] and applied["applied"], applied)

    def test_the_obsolete_files_of_the_installed_claude_manifest_are_owned_too(self) -> None:
        manifest = json.loads((self.target / MANIFEST).read_text())
        manifest["files"][".claude/agents/claude_agents_retired.md"] = "0" * 64
        (self.target / MANIFEST).write_bytes(canonical(manifest))
        owned = installer._owned_paths(self.target, "claude", {"changed_paths": []})
        self.assertIn(".claude/agents/claude_agents_retired.md", owned)

    def test_a_manifest_that_is_a_symlink_is_not_followed(self) -> None:
        outside = self.base / "outside-manifest.json"
        outside.write_text(json.dumps({"files": {".codex/from-outside.txt": "0" * 64}}))
        manifest = self.target / ".codex/orchestration/install-manifest.json"
        manifest.unlink()
        manifest.symlink_to(outside)
        for name, relative in (("codex", ".codex/from-outside.txt"),):
            self.assertNotIn(relative, installer._owned_paths(self.target, name, {}))

    def test_a_project_folder_replaced_by_a_symlink_after_the_snapshot_is_refused(self) -> None:
        preview = {"changed_paths": [".claude/agents/claude_agents_planner.md"]}
        snapshot = installer._snapshot(self.target, "claude", preview)
        moved = self.base / "target-moved"
        decoy = self.base / "decoy"
        (decoy / ".claude/agents").mkdir(parents=True)
        victim = decoy / ".claude/agents/claude_agents_planner.md"
        victim.write_text("decoy\n")
        self.target.rename(moved)
        self.target.symlink_to(decoy, target_is_directory=True)
        self.addCleanup(lambda: (self.target.unlink(), moved.rename(self.target)))
        journal = {"entries": {".claude/agents/claude_agents_planner.md": (None, (b"x", 0o644))}, "created": set()}
        ok, _ = installer._undo(self.target, "claude", snapshot, journal)
        self.assertFalse(ok)
        self.assertEqual("decoy\n", victim.read_text())

    def test_a_folder_this_run_did_not_create_is_never_removed(self) -> None:
        keep = self.target / ".claude/preexisting"
        keep.mkdir()
        wanted = ".claude/preexisting/made/by/step.txt"
        snapshot = installer._snapshot(self.target, "claude", {"changed_paths": [wanted]})
        (self.target / ".claude/preexisting/made/by").mkdir(parents=True)
        (self.target / wanted).write_text("written by the step\n")
        post = installer._observe(self.target, "claude", snapshot)
        journal = installer._journal(snapshot, post)
        ok, left = installer._undo(self.target, "claude", snapshot, journal)
        self.assertTrue(ok and not left, (ok, left))
        self.assertTrue(keep.is_dir())
        self.assertFalse((self.target / ".claude/preexisting/made").exists())

    @unittest.skipIf(os.geteuid() == 0, "a read-only folder does not stop root, so the write would not fail")
    def test_an_update_that_fails_partway_leaves_no_marker_or_lock_and_can_be_retried(self) -> None:
        self.make_read_only_for_the_last_change()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        for control in (".claude/claude-agents/install-pending.json", ".claude/claude-agents/install.lock"):
            self.assertFalse((self.target / control).exists(), control)
        self.assertEqual("operation_failed", summary["claude"]["error"])
        # this test is a guard: the package undoes its own partial write, so it also passes on 3d86ee76


class FreshInstallFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
        self.target, self.codex_options = make_target(self.base)

    def patch_run(self, reply: dict, damage=None) -> None:
        real_run = installer._run

        def run(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                if damage:
                    damage()
                return reply
            return report

        original = installer._run
        installer._run = run
        self.addCleanup(setattr, installer, "_run", original)

    def test_a_failed_fresh_install_is_undone_and_says_so(self) -> None:
        before = {p: d for p, d in inventory(self.target).items() if p not in PROJECT_CONFIG}
        self.patch_run({"error": "operation_failed", "exit_code": 2})
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        self.assertTrue(claude["ok"], claude)
        self.assertEqual(before, {p: d for p, d in inventory(self.target).items() if p not in PROJECT_CONFIG})

    def test_a_failed_fresh_install_that_cannot_be_undone_is_reported_incomplete(self) -> None:
        planner = self.target / ".claude/agents/claude_agents_planner.md"
        self.patch_run({"error": "operation_failed", "rollback_state": "pending", "exit_code": 2},
                       damage=lambda: (self.target / ".claude/claude-agents/install-pending.json").write_text("{}\n"))
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertTrue(steps.rollback_incomplete(summary))
        message, known = steps.installer_refusal(steps.raw(summary), "manual", steps.rollback_incomplete(summary))
        self.assertNotIn("Nothing was changed", message)
        self.assertTrue(planner.exists())  # kept for the owner to inspect, never silently deleted


if __name__ == "__main__":
    unittest.main()
