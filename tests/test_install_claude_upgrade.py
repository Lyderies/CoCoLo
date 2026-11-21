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


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def failed_with_writes(report: dict, **fields) -> dict:
    """A failure reply that still carries the real step's own write report (the package protocol since the
    write-report fix): the step really wrote what it reports, then failed."""
    carried = {key: report[key] for key in ("writes", "folders_created", "folders_removed") if key in report}
    return {**carried, "exit_code": 2, **fields}


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
                return failed_with_writes(report, error="operation_failed")
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
                return failed_with_writes(report, error="operation_failed")
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
        return folder

    @unittest.skipIf(os.geteuid() == 0, "a read-only folder does not stop root, so the package's write would not fail")
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
                return failed_with_writes(report, error="operation_failed")
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
                return failed_with_writes(report, error="operation_failed")
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
                return failed_with_writes(report, error="rollback_pending")
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
        # The kept note says what to do; the wizard shows it.
        (claude,) = [r for r in first["rollback"] if r["package"] == "claude"]
        for advised in (".claude/claude-agents/install-pending.json", ".claude/claude-agents/install.lock",
                        "run setup again"):
            self.assertIn(advised, claude["note"])
        self.assertIn(claude["note"], steps.rollback_notes(first))
        # Run again without following the advice: refused before anything is written, for exactly this reason
        # (the kept files are uncommitted changes under .claude), never for some other known one.
        with self.assertRaises(installer.InstallRefused) as stuck:
            installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertEqual("managed paths (.codex, .agents, .claude) have uncommitted changes", str(stuck.exception))
        message, known = steps.installer_refusal(str(stuck.exception).lower(), "manual")
        self.assertTrue(known)
        self.assertTrue(message.startswith("Managed paths (.codex, .agents, .claude) have uncommitted changes"), message)
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

    def test_a_project_folder_replaced_after_the_snapshot_is_refused(self) -> None:
        """GUARD (green on 1d359e3b, whose journal shape never matches a digest; r4 test finding: the decoy did not
        match the journal, so content alone refused the undo). The
        decoy now holds exactly the journal's after-state, so only the project-folder identity check (and, for
        a symlink, the no-follow open) stops the undo from deleting it."""
        preview = {"changed_paths": [".claude/agents/claude_agents_planner.md"]}
        for swap in ("symlink", "folder"):
            with self.subTest(swap=swap):
                snapshot = installer._snapshot(self.target, "claude", preview)
                moved = self.base / f"target-moved-{swap}"
                decoy = self.base / f"decoy-{swap}"
                (decoy / ".claude/agents").mkdir(parents=True)
                victim = decoy / ".claude/agents/claude_agents_planner.md"
                victim.write_text("decoy\n")
                self.target.rename(moved)
                if swap == "symlink":
                    self.target.symlink_to(decoy, target_is_directory=True)
                else:
                    decoy.rename(self.target)
                    victim = self.target / ".claude/agents/claude_agents_planner.md"
                journal = {"entries": {".claude/agents/claude_agents_planner.md": (None, sha(b"decoy\n"))},
                           "created": [], "left": []}
                try:
                    ok, _ = installer._undo(self.target, "claude", snapshot, journal)
                    self.assertFalse(ok)
                    self.assertEqual("decoy\n", victim.read_text())
                finally:
                    if self.target.is_symlink():
                        self.target.unlink()
                    else:
                        self.target.rename(decoy)
                    moved.rename(self.target)

    @unittest.skipIf(os.geteuid() == 0, "a read-only folder does not stop root, so the write would not fail")
    def test_an_update_that_fails_partway_leaves_no_marker_or_lock_and_can_be_retried(self) -> None:
        """GUARD (passes on 1d359e3b too: the package undoes its own partial write). A real failure inside the
        package; afterwards no control file is left, and once the folder is writable again setup really retries
        and upgrades."""
        folder = self.make_read_only_for_the_last_change()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        for control in (".claude/claude-agents/install-pending.json", ".claude/claude-agents/install.lock"):
            self.assertFalse((self.target / control).exists(), control)
        self.assertEqual("operation_failed", summary["claude"]["error"])
        folder.chmod(0o755)
        again = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(again["ok"] and again["applied"], again)


    # --- the package's own write report is the journal (owner fix 1-4) ----------------------------------

    PLANNER = ".claude/agents/claude_agents_planner.md"
    SKILL = ".claude/skills/claude-agents/SKILL.md"

    def release_file(self, path: str) -> str:
        return json.loads((PACKAGE / "previous-releases.json").read_text())["releases"][0]["files"][path]

    def during_the_claude_step(self, action, reply=None) -> None:
        """Run the real package step, then `action` (another writer, while setup is still running), then hand
        back the real reply, or `reply(real_reply)`."""
        real_run = installer._run

        def run(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                action()
                return reply(report) if reply else report
            return report

        self.patch("_run", run)

    def settings_fail(self) -> list:
        calls = []

        def failing_settings(target, plan):
            calls.append(target)
            raise installer.InstallRefused("simulated settings failure")

        self.patch("apply_settings", failing_settings)
        return calls

    def claude_entry(self, summary: dict) -> dict:
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        return claude

    def test_a_users_edit_to_a_package_file_while_the_step_runs_is_left_alone_and_named(self) -> None:
        """Owner fix 1 (r4 HIGH 'observation is not write authority'): an edit made after the package wrote a file,
        before setup looked, is the user's: a later failure leaves it and names it. Fails on 1d359e3b: the
        read-back records the edit as the run's own write, and the undo overwrites it with 0.8.40."""
        planner = self.target / self.PLANNER
        self.during_the_claude_step(lambda: planner.write_text("the user's edit while the update ran\n"))
        self.settings_fail()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("the user's edit while the update ran\n", planner.read_text())
        claude = self.claude_entry(summary)
        self.assertFalse(claude["ok"], claude)
        self.assertIn(self.PLANNER, claude["left_alone"])
        self.assertEqual(self.release_file(self.SKILL), (self.target / self.SKILL).read_text())

    def test_an_update_that_wrote_nothing_never_undoes_a_change_made_while_it_ran(self) -> None:
        """Owner fix 1 (r4: 'an already-current update ... creates a false journal entry'): the package reports no
        write, so nothing of its is undone. Fails on 1d359e3b: the read-back sees the user's edit and undoes it."""
        done = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(done["ok"], done)
        self.commit("current release")
        planner = self.target / self.PLANNER
        self.during_the_claude_step(lambda: planner.write_text("the user's edit\n"))
        calls = self.settings_fail()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("the user's edit\n", planner.read_text())
        # r5 item 1: a changed owned path the report leaves out stops setup at once and is named, never ok.
        self.assertEqual([], calls)
        claude = self.claude_entry(summary)
        self.assertFalse(claude["ok"], claude)
        self.assertIn(self.PLANNER, claude["left_alone"])

    def test_a_package_reply_without_its_write_report_stops_setup_and_undoes_nothing_of_that_step(self) -> None:
        """Owner fix 1 ('no reply, or a malformed reply, means stop and undo conservatively, never ok'). Fails on
        1d359e3b: the reply is read as success, the later steps run and setup ends ok."""
        calls = []
        real_settings = installer.apply_settings
        self.patch("apply_settings", lambda target, plan: (calls.append(target), real_settings(target, plan))[1])
        self.during_the_claude_step(lambda: None, reply=lambda report: {"result": "applied", "exit_code": 0})
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(summary["applied"], summary)
        self.assertEqual([], calls)  # stopped at once: the settings step never ran
        self.assertIn("no_write_report", summary["claude"]["error"])
        claude = self.claude_entry(summary)
        self.assertFalse(claude["ok"], claude)
        self.assertIn(self.PLANNER, claude["left_alone"])  # what the step may have written is left, and named
        self.assertNotEqual(self.release_file(self.PLANNER), (self.target / self.PLANNER).read_text())

    def test_an_unknown_rollback_state_is_never_read_as_none(self) -> None:
        """Owner fix 4 (rollback_state is a structured field): a value that is neither 'none' nor 'pending' makes the
        reply unusable, so nothing of that package is undone. Fails on 1d359e3b: anything but 'pending' counts as
        'none' and the package's files are rolled back."""
        self.during_the_claude_step(lambda: None, reply=lambda report: failed_with_writes(
            report, error="operation_failed", rollback_state="PENDING"))
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(self.claude_entry(summary)["ok"])
        self.assertNotEqual(self.release_file(self.PLANNER), (self.target / self.PLANNER).read_text())

    def test_a_project_folder_that_changes_identity_during_a_step_stops_setup_at_once(self) -> None:
        """Owner fix 2 (r4 MEDIUM: a failed read-back was swallowed and later steps ran to ok=True, install.py:1449,
        :1466). Fails on 1d359e3b: the settings step runs on the replacement folder and setup ends ok."""
        moved = self.base / "target-before-the-swap"

        def replace_the_project_folder() -> None:
            self.target.rename(moved)
            shutil.copytree(moved, self.target, symlinks=True)

        calls = []
        real_settings = installer.apply_settings
        self.patch("apply_settings", lambda target, plan: (calls.append(target), real_settings(target, plan))[1])
        self.during_the_claude_step(replace_the_project_folder)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(summary["applied"], summary)
        self.assertEqual([], calls)
        self.assertIn("unsafe_or_unavailable_path", summary["claude"]["error"])
        self.assertIn("project_folder_changed", summary["claude"]["error"])  # r5 item 5: its own remedy
        self.assertFalse(self.claude_entry(summary)["ok"])
        self.assertTrue(steps.rollback_incomplete(summary))

    def swap_once_when_undoing(self, relative: str, content: str) -> None:
        """Another writer saves `relative` as a new file (new inode) right after the undo has read it, before the
        undo acts on it: the window the owner's fix 3 closes with a descriptor identity check."""
        real_read, real_undo = installer._read_file, installer._undo
        state = {"armed": False, "done": False}

        def undo(*args, **kwargs):
            state["armed"] = True
            return real_undo(*args, **kwargs)

        def read(folder, name, *args, **kwargs):
            result = real_read(folder, name, *args, **kwargs)
            if state["armed"] and not state["done"] and name == relative.rsplit("/", 1)[-1]:
                state["done"] = True
                path = self.target / relative
                fresh = path.with_name(".editor-save")
                fresh.write_text(content)
                os.replace(fresh, path)
            return result

        self.patch("_undo", undo)
        self.patch("_read_file", read)

    def test_an_undo_never_replaces_a_file_swapped_in_after_it_was_checked(self) -> None:
        """Owner fix 3 (r4 HIGH 'undo comparison and mutation are separate operations', install.py:1278, :1293): the
        rename happens only if the entry is still the one whose content was checked. Fails on 1d359e3b: the undo
        renames 0.8.40 over the user's save."""
        self.swap_once_when_undoing(self.PLANNER, "the user's save\n")
        self.settings_fail()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("the user's save\n", (self.target / self.PLANNER).read_text())
        self.assertIn(self.PLANNER, self.claude_entry(summary)["left_alone"])

    def test_a_refused_undo_never_recreates_a_folder_the_user_deleted(self) -> None:
        """Owner fix 3 (r4 MEDIUM, install.py:1272). Fails on 1d359e3b: the undo makes the folder again before it
        finds the file gone, and leaves it behind empty. The folder holds files the update wrote, and it is deleted
        after setup checked the write report (in the failing settings step), so only the undo-time guard can keep
        it from being made again (r5: the earlier version was pruned by the check and never reached the undo)."""
        agents = (self.target / self.PLANNER).parent

        def deletes_then_fails(target, plan):
            shutil.rmtree(agents)
            raise installer.InstallRefused("simulated settings failure")

        self.patch("apply_settings", deletes_then_fails)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(agents.exists())
        self.assertIn(self.PLANNER, self.claude_entry(summary)["left_alone"])

    def drop_writes(self, keep) -> None:
        """The real Claude step runs; its reply keeps only the write entries `keep` accepts (a truncated report)."""
        def reply(report):
            return {**report, "writes": [entry for entry in report["writes"] if keep(entry)]}
        self.during_the_claude_step(lambda: None, reply=reply)

    def test_a_write_report_that_leaves_out_a_changed_file_stops_setup_and_names_it(self) -> None:
        """r5 item 1 (a truncated report, or writes=[] when the step changed owned files, was accepted as ok). The
        snapshot is compared with the disk: an owned path that changed and is missing from the report stops setup
        at once, is named, and is never undone. Fails on d946a683: the report is accepted and setup ends ok."""
        for case, keep in (("one entry left out", lambda entry: entry["path"] != self.PLANNER),
                           ("writes=[]", lambda entry: False)):
            with self.subTest(case=case):
                self.drop_writes(keep)
                calls = []
                real_settings = installer.apply_settings
                self.patch("apply_settings", lambda target, plan: (calls.append(target), real_settings(target, plan))[1])
                summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
                self.assertFalse(summary["ok"], summary)
                self.assertFalse(summary["applied"], summary)
                self.assertEqual([], calls)
                self.assertIn("no_write_report", summary["claude"]["error"])
                claude = self.claude_entry(summary)
                self.assertFalse(claude["ok"], claude)
                self.assertIn(self.PLANNER, claude["left_alone"])
                self.assertNotEqual(self.release_file(self.PLANNER), (self.target / self.PLANNER).read_text())
                git(self.target, "checkout", "--", ".")

    def test_a_reported_new_file_that_was_never_saved_is_left_alone_not_deleted(self) -> None:
        """r5 item 1 (install.py:231-232, :386-389: a before=null path that was not snapshotted was accepted and could
        be deleted by the undo). Fails on d946a683: the user's own agent file is unlinked."""
        mine = self.target / ".claude/agents/my_agent.md"
        mine.write_text("the user's own agent\n")
        self.commit("user agent")
        entry = {"path": ".claude/agents/my_agent.md", "before": None, "after": sha(mine.read_bytes()), "mode": 0o644}
        self.during_the_claude_step(lambda: None, reply=lambda report: {**report, "writes": [*report["writes"], entry]})
        self.settings_fail()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual("the user's own agent\n", mine.read_text())
        self.assertIn(".claude/agents/my_agent.md", self.claude_entry(summary)["left_alone"])

    def test_a_path_with_a_nul_or_that_does_not_encode_is_no_write_report(self) -> None:
        """r5 item 3 (install.py:41, :136, :613-619: such a path raised ValueError past every handler). It stops setup
        as no_write_report and setup still returns its summary. Fails on d946a683: ValueError escapes, or the
        reply is accepted and setup ends ok."""
        bad = (".claude/agents/x\x00y.md", ".claude/agents/x\udc80y.md")
        for field, path in [("writes", p) for p in bad] + [("folders_created", p) for p in bad]:
            with self.subTest(field=field, path=repr(path)):
                if field == "writes":
                    extra = {"path": path, "before": None, "after": "0" * 64, "mode": 0o644}
                else:
                    extra = {"path": path, "device": 1, "inode": 1}
                self.during_the_claude_step(
                    lambda: None, reply=lambda report, field=field, extra=extra: {**report, field: [*report[field], extra]})
                summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
                self.assertFalse(summary["ok"], summary)
                self.assertIn("no_write_report", summary["claude"]["error"])
                git(self.target, "checkout", "--", ".")

    def test_a_mode_change_made_while_the_step_ran_is_left_alone_and_named(self) -> None:
        """r5 item 4 (install.py:167-168, :384: the after-check compared bytes only, so the undo replaced a file whose
        mode someone changed). Fails on d946a683: the file is undone and the user's mode is lost."""
        planner = self.target / self.PLANNER
        self.during_the_claude_step(lambda: planner.chmod(0o600))
        self.settings_fail()
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual(0o600, stat.S_IMODE(planner.stat().st_mode))
        self.assertNotEqual(self.release_file(self.PLANNER), planner.read_text())
        self.assertIn(self.PLANNER, self.claude_entry(summary)["left_alone"])

    def test_the_pending_marker_is_never_undone_while_a_lock_is_held(self) -> None:
        """GUARD of owner fix 4 (r4 MEDIUM 'control cleanup can erase the pending marker before discovering an
        unsafe lock', install.py:1249). The package never reports its marker as a write to undo, so no operate
        flow reaches this. It passes on 1d359e3b only because that journal format never matches a digest."""
        pending, lock = installer.CLAUDE_CONTROL
        for case in ("lock changed by another run", "lock held by another run"):
            with self.subTest(case=case):
                snapshot = installer._snapshot(self.target, "claude", {"changed_paths": []})
                marker, token = b'{"marker": 1}\n', b"our token\n"
                (self.target / pending).write_bytes(marker)
                (self.target / lock).write_bytes(b"another run's token\n")
                entries = {pending: (None, sha(marker))}
                if case == "lock changed by another run":
                    entries[lock] = (None, sha(token))
                ok, left = installer._undo(self.target, "claude", snapshot,
                                           {"entries": entries, "created": [], "left": []})
                self.assertFalse(ok)
                self.assertEqual(marker, (self.target / pending).read_bytes())
                self.assertIn(pending, left)
                (self.target / pending).unlink()
                (self.target / lock).unlink()


class FreshInstallFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
        self.target, self.codex_options = make_target(self.base)

    def patch_run(self, reply: dict, damage=None, carry_writes: bool = False) -> None:
        real_run = installer._run

        def run(script, operation, target, apply, extra):
            report = real_run(script, operation, target, apply, extra)
            if script == installer.CLAUDE_INSTALLER and apply:
                if damage:
                    damage()
                return failed_with_writes(report, **reply) if carry_writes else reply
            return report

        original = installer._run
        installer._run = run
        self.addCleanup(setattr, installer, "_run", original)

    def test_a_failed_fresh_install_is_undone_and_says_so(self) -> None:
        before = {p: d for p, d in inventory(self.target).items() if p not in PROJECT_CONFIG}
        self.patch_run({"error": "operation_failed", "exit_code": 2}, carry_writes=True)
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


    def claude_entry(self, summary: dict) -> dict:
        (claude,) = [r for r in summary["rollback"] if r["package"] == "claude"]
        return claude

    def test_a_folder_the_run_created_that_now_holds_a_users_file_is_kept_and_named(self) -> None:
        """Owner fix 3 (r4: folder cleanup failures were swallowed, install.py:1256, :1260). Fails on 1d359e3b: the
        rmdir failure is ignored and the rollback says ok."""
        notes = self.target / ".claude/skills/claude-agents/notes.md"
        self.patch_run({"error": "operation_failed"}, damage=lambda: notes.write_text("mine\n"), carry_writes=True)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertEqual("mine\n", notes.read_text())
        claude = self.claude_entry(summary)
        self.assertFalse(claude["ok"], claude)
        self.assertIn(".claude/skills/claude-agents", claude["left_alone"])

    def test_a_folder_the_run_created_and_the_user_replaced_is_not_removed(self) -> None:
        """Owner fix 3 (folder cleanup by identity, r4 MEDIUM install.py:1224, :1259). Fails on 1d359e3b: the empty
        folder the user put in its place is removed by name."""
        folder = self.target / ".claude/skills/claude-agents"

        def replace_folder() -> None:
            folder.rename(self.base / "moved-skill")
            folder.mkdir()

        self.patch_run({"error": "operation_failed"}, damage=replace_folder, carry_writes=True)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(folder.is_dir())
        self.assertIn(".claude/skills/claude-agents", self.claude_entry(summary)["left_alone"])

    def test_an_undo_never_removes_a_file_swapped_in_after_it_was_checked(self) -> None:
        """Owner fix 3 (unlink by descriptor identity, install.py:1284). Fails on 1d359e3b: the undo unlinks the
        user's new file by name."""
        planner = ".claude/agents/claude_agents_planner.md"
        real_read, real_undo = installer._read_file, installer._undo
        state = {"armed": False, "done": False}

        def undo(*args, **kwargs):
            state["armed"] = True
            return real_undo(*args, **kwargs)

        def read(folder, name, *args, **kwargs):
            result = real_read(folder, name, *args, **kwargs)
            if state["armed"] and not state["done"] and name == "claude_agents_planner.md":
                state["done"] = True
                fresh = self.target / ".claude/agents/.editor-save"
                fresh.write_text("the user's save\n")
                os.replace(fresh, self.target / planner)
            return result

        for name, value in (("_undo", undo), ("_read_file", read)):
            self.addCleanup(setattr, installer, name, getattr(installer, name))
            setattr(installer, name, value)
        self.patch_run({"error": "operation_failed"}, carry_writes=True)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertEqual("the user's save\n", (self.target / planner).read_text())
        self.assertIn(planner, self.claude_entry(summary)["left_alone"])

    def test_a_folder_this_run_did_not_create_is_never_removed(self) -> None:
        """Owner fix 3, on the real Claude producer report (r5: the earlier version supplied folders_created itself,
        so the folder survived by construction): .claude and .claude/skills exist (empty) before a fresh install
        that fails; the undo removes the folders the package made and keeps those. GUARD: green on 1d359e3b
        too, which inferred folders by reading back."""
        keep = self.target / ".claude/skills"
        keep.mkdir(parents=True)
        self.patch_run({"error": "operation_failed"}, carry_writes=True)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        claude = self.claude_entry(summary)
        self.assertTrue(claude["ok"], claude)
        self.assertTrue(keep.is_dir())
        self.assertFalse((self.target / ".claude/skills/claude-agents").exists())
        self.assertFalse((self.target / ".claude/agents").exists())

    @unittest.skipIf(os.geteuid() == 0, "root is not refused a folder search, so the stat would not fail")
    def test_a_folder_that_cannot_be_examined_is_named_and_the_rollback_goes_on(self) -> None:
        """r5 item 2 (install.py:439: a stat error other than FileNotFoundError escaped and aborted the rollback). A
        folder the run created sits in a folder that can be read but not searched. Fails on d946a683:
        PermissionError escapes operate; on 1d359e3b the folder is not named."""
        skills = self.target / ".claude/skills"

        def unsearchable() -> None:
            skills.chmod(0o444)

        self.addCleanup(lambda: skills.exists() and skills.chmod(0o755))
        self.patch_run({"error": "operation_failed"}, damage=unsearchable, carry_writes=True)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        claude = self.claude_entry(summary)
        self.assertFalse(claude["ok"], claude)
        self.assertIn(".claude/skills/claude-agents", claude["left_alone"])
        self.assertFalse((self.target / ".claude/agents/claude_agents_planner.md").exists())  # the rest was undone
        self.assertEqual(["claude", "codex"], [entry["package"] for entry in summary["rollback"]])

    @unittest.skipIf(os.geteuid() == 0, "a read-only folder does not stop root, so the package's write would not fail")
    def test_a_real_codex_failure_partway_is_undone_from_the_packages_own_report(self) -> None:
        """Owner fix 1 and 6, a real mid-step failure through the package (no faked reply): the Codex package writes
        its .agents files, then cannot create .codex/agents; setup undoes exactly what it reports. Fails on
        1d359e3b only at the write report (the reply has none); the undo itself worked there too, because
        nothing else writes while the step runs."""
        before = inventory(self.target)
        codex = self.target / ".codex"
        codex.mkdir()
        codex.chmod(0o555)
        self.addCleanup(codex.chmod, 0o755)
        summary = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertFalse(summary["ok"], summary)
        self.assertIn("error", summary["codex"])
        self.assertTrue(summary["codex"].get("writes"), summary["codex"])  # it really wrote before failing
        self.assertEqual([{"package": "codex", "action": "restore", "ok": True}], summary["rollback"])
        self.assertEqual(before, inventory(self.target))
        self.assertFalse((self.target / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
