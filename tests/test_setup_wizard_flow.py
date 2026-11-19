"""T8h: the wizard's steps tested directly through the in-process flow harness (tests/wizard_flow_harness.py).

Each test names the WizardTerminalTests test in tests/test_setup_sh.py whose requirement it proves, with
the same screens, files and outcomes, without a pty or a Python start per command. Runs Git, so it is an
integration test.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wizard_flow_harness import FINISH, ROOT, Harness, to_commit  # noqa: E402

ENTER, DOWN = "\r", "\x1b[B"


class ProjectChoiceTests(unittest.TestCase):
    """W3: choosing the project (setup.sh step 2)."""

    def setUp(self) -> None:
        self.h = Harness(self)

    def test_home_markers_in_the_project_path_are_expanded(self) -> None:
        """Replaces WizardTerminalTests.test_home_markers_in_the_project_path_are_expanded (W3.1)."""
        for typed in ("~/Code/proj", "$HOME/Code/proj", "${HOME}/Code/proj", "'~/Code/proj'"):
            with self.subTest(typed=typed):
                self.h = Harness(self)
                project, _ = self.h.project_under_home()
                _, screen = self.h.run(ENTER, f"{typed}\r", start=project)
                self.assertIn("Path to operational-checks.json", screen)
                self.assertNotIn("Cannot open", screen)

    def test_projects_set_up_before_are_offered_in_a_list(self) -> None:
        """Replaces WizardTerminalTests.test_projects_set_up_before_are_offered_in_a_list (W3.2)."""
        first, _ = self.h.make_project("first")
        second, _ = self.h.make_project("second")
        gone = self.h.base / "gone"
        self.h.remembered(first, second)
        listed = self.h.home / ".local" / "state" / "codex-agents" / "installer" / "setup-projects"
        listed.write_text(listed.read_text() + f"{gone}\n")
        _, screen = self.h.run(ENTER, DOWN + ENTER, start=second)
        menu = screen[screen.index("Another folder...") - 600:screen.index("Another folder...")]
        self.assertIn(str(first), menu)
        self.assertIn(str(second), menu)
        self.assertNotIn("(this folder)", screen)
        self.assertNotIn(str(gone), screen)
        self.assertNotIn("is-skipped", screen)
        self.assertNotIn("Project path", screen)
        self.assertIn("An existing install was found", screen)
        self.assertIn(f"Project: {second}", screen)
        self.assertEqual([str(second), str(first)], listed.read_text().splitlines()[:2])

    def test_another_folder_asks_for_a_path(self) -> None:
        """Replaces WizardTerminalTests.test_another_folder_asks_for_a_path (W3.2)."""
        first, _ = self.h.make_project("first")
        self.h.remembered(first)
        other, _ = self.h.make_project("other")
        _, screen = self.h.run(ENTER, DOWN + DOWN + ENTER, start=other)
        self.assertIn(f"{other} (this folder)", screen)
        self.assertIn("Project path", screen.split("Another folder...", 1)[1])

    def test_a_lost_tilde_is_reported_exactly_and_offered_back(self) -> None:
        """Replaces WizardTerminalTests.test_a_lost_tilde_is_reported_exactly_and_offered_back (W3.4)."""
        project, _ = self.h.project_under_home()
        _, screen = self.h.run(ENTER, "/Code/proj\r", ENTER, start=project)
        self.assertIn("Cannot open: [/Code/proj]", screen)
        self.assertIn("Type the full path, for example /Users/<you>/projects/app. Tab completes paths.", screen)
        self.assertIn(f"that path does not exist, but this one does: {project}".lower(), screen.lower())
        self.assertIn("Use the path shown above", screen)
        self.assertIn("Path to operational-checks.json", screen.split("Use the path shown above", 1)[1])

    def test_cannot_open_without_a_home_match_offers_no_guess(self) -> None:
        """Replaces WizardTerminalTests.test_cannot_open_without_a_home_match_offers_no_guess (W3.4)."""
        project, _ = self.h.project_under_home()
        _, screen = self.h.run(ENTER, "~/Code/nowhere\r", start=project)
        self.assertIn(f"Cannot open: [{self.h.home}/Code/nowhere]", screen)
        self.assertIn("You typed: [~/Code/nowhere]", screen)
        self.assertIn("Tab completes paths", screen)
        self.assertIn("Choose another path", screen)
        self.assertNotIn("but this one does", screen)
        self.assertNotIn("Use the path shown above", screen)

    def test_path_starting_with_a_dash_is_resolved(self) -> None:
        """Replaces WizardTerminalTests.test_path_starting_with_a_dash_is_resolved (W3.3)."""
        (self.h.base / "-odd").mkdir()
        _, screen = self.h.run(ENTER, "-odd\r", start=self.h.base)
        self.assertIn("not inside a Git working tree", screen)
        self.assertNotIn("Cannot open", screen)

    def test_started_from_the_orchestrator_it_suggests_no_project_and_refuses_itself(self) -> None:
        """Replaces WizardTerminalTests.test_started_from_the_orchestrator_it_suggests_no_project_and_refuses_itself
        (W3.3, W3.4)."""
        _, screen = self.h.run(ENTER, ENTER, f"{ROOT}\r", start=ROOT)
        self.assertEqual(2, screen.count("(type or paste the full path)"))  # nothing typed: asked again
        self.assertIn("this orchestrator's own folder", screen)
        self.assertFalse(any(str(Path("scripts") / "install.py") in " ".join(argv) for argv in self.h.launched))



class ChecksTests(unittest.TestCase):
    """W2, W9.2: the checks step (setup.sh step 1)."""

    def setUp(self) -> None:
        self.h = Harness(self)

    @staticmethod
    def flat(screen: str) -> str:
        return re.sub(r"\s+", " ", screen)

    def test_a_codex_session_able_to_write_this_clone_is_warned_about(self) -> None:
        """Replaces WizardTerminalTests.test_a_codex_session_able_to_write_this_clone_is_warned_about (W2.1-W2.3, W2.5)."""
        self.assertFalse(self.h.home.is_relative_to(ROOT))
        self.h.write_codex_session(ROOT, "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b")
        self.h.scan = "real"
        code, screen = self.h.run(ENTER, "q\r", "y", start=self.h.base)
        flat = self.flat(screen)
        self.assertIn(f"A Codex session on 2026-11-01 could write to {ROOT}. Codex was opened on "
                      "a folder that holds this setup folder; open Codex on a project folder only.", flat)
        self.assertIn("Open Codex on the project itself: never on a folder that contains a set-up project, "
                      "this setup folder, your home folder, a folder above it or /.", flat)
        self.assertIn("Read the warning above", flat)
        self.assertIn("Project path", screen.split("Read the warning above", 1)[1])  # Continue went on
        self.assertEqual(130, code)
        self.assertEqual([self.h.home / ".codex" / "sessions"], list((self.h.home / ".codex").iterdir()))

    def test_sessions_able_to_write_the_records_or_a_set_up_project_are_warned_about(self) -> None:
        """Replaces WizardTerminalTests.test_sessions_able_to_write_the_records_or_a_set_up_project_are_warned_about (W2.2)."""
        self.h.scan = {"ready": True, "warnings": [
            {"date": "2026-11-01", "holds": "state", "folder": "/x/state-parent"},
            {"date": "2026-11-02", "holds": "project", "folder": "/x/projects",
             "message": "Codex was opened on a folder that contains a set-up project (demo\x1b[2J); "
                        "open Codex on the project itself"}]}
        code, screen = self.h.run(DOWN + ENTER, start=self.h.base)
        flat = self.flat(screen)
        self.assertIn("A Codex session on 2026-11-01 could write to /x/state-parent. Codex was opened on a "
                      "folder that holds the orchestrator's learning state.", flat)
        self.assertIn("A Codex session on 2026-11-02 could write to /x/projects. Codex was opened on a "
                      "folder that contains a set-up project (demo[2J); open Codex on the project itself.", flat)
        self.assertNotIn("\x1b", screen)
        self.assertIn("never on a folder that contains a set-up project", flat)
        self.assertEqual(0, code)

    def test_a_project_codex_session_is_not_warned_about(self) -> None:
        """Replaces WizardTerminalTests.test_a_project_codex_session_is_not_warned_about (W2.4)."""
        project = self.h.base / "some-project"
        project.mkdir()
        self.h.write_codex_session(project, "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5c")
        self.h.scan = "real"
        code, screen = self.h.run("q", "y", start=self.h.base)
        self.assertIn("Everything looks good", screen)
        self.assertNotIn("A Codex session on", screen)
        self.assertEqual(130, code)

    @unittest.skipUnless(sys.platform.startswith("linux"), "the warning is Linux's")
    def test_linux_warns_that_operational_checks_need_macos(self) -> None:
        """Replaces WizardTerminalTests.test_linux_warns_that_operational_checks_need_macos (W3.5)."""
        code, screen = self.h.run("q", "y", start=self.h.base)
        self.assertIn("Operational checks need macOS today", screen)
        self.assertIn("known limitation", screen)
        self.assertNotIn("FAIL", screen)
        self.assertEqual(130, code)


class CollectorTests(unittest.TestCase):
    """W6: the collector step (setup.sh step 5), after a real install and commit."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.project, self.checks = self.h.make_project()

    def to_register(self, *after: str) -> tuple[int, str]:
        """A fresh install, committed, then Register now, then `after`."""
        return self.h.run(*to_commit(self.checks), ENTER, ENTER, ENTER, *after, start=self.project)

    @staticmethod
    def flat(screen: str) -> str:
        return re.sub(r"\s+", " ", screen)

    def test_the_one_time_move_of_the_records_is_explained(self) -> None:
        """Replaces WizardTerminalTests.test_the_one_time_move_of_the_records_is_explained (W6.2)."""
        self.h.fake_register('{"ready": true, "state_moved": "/somewhere/old\x7f copy", '
                             '"state_root": "/elsewhere/codex-agents"}')
        _, screen = self.to_register()
        flat = self.flat(screen)
        self.assertIn("Collector status: ready", flat)
        self.assertIn("Moved your orchestrator records once to a protected folder, ~/.local/state/codex-agents. "
                      "The old copy at /somewhere/old copy can be deleted once you're happy.", flat)
        self.assertNotIn("\x7f", screen)
        self.assertNotIn("/elsewhere", flat)

    def test_no_move_no_records_sentence(self) -> None:
        """Replaces WizardTerminalTests.test_no_move_no_records_sentence (W6.2)."""
        self.h.fake_register('{"ready": true, "state_moved": null}')
        _, screen = self.to_register()
        self.assertIn("Collector status: ready", screen)
        self.assertNotIn("Moved your orchestrator records", screen)

    @unittest.skipUnless(sys.platform == "darwin", "the old records folder was macOS's")
    def test_old_records_are_shown_and_moved_only_when_approved(self) -> None:
        """Replaces WizardTerminalTests.test_old_records_are_shown_and_moved_only_when_approved (W6.2)."""
        digest = self.h.plant_legacy_records()
        self.h.fake_register('{"ready": true, "state_moved": "/somewhere/old copy", "state_root": "/x"}')
        _, screen = self.to_register(ENTER)  # Yes, move them
        # The screen draws the question (its body), then the details (its list), then the menu.
        question = self.flat(screen.split("Move these records to the protected folder?", 1)[1].split("Yes, move them", 1)[0])
        self.assertIn("still in the old folder ~/Library/Application Support/codex-agents (created 2", question)
        self.assertRegex(question, r"Owner key in those records: ssh-ed25519 SHA256:[A-Za-z0-9+/]+")
        self.assertIn("Your owner key file ~/.ssh/codex-agents-owner.pub:", question)
        self.assertTrue(any(line in question for line in ("They match", "They do not match", "cannot compare")))
        self.assertIn("Collector status: ready", screen)
        self.assertIn(f"--move-legacy-state {digest}", " ".join(" ".join(args) for args in self.h.register_log))

    @unittest.skipUnless(sys.platform == "darwin", "the old records folder was macOS's")
    def test_declining_the_move_registers_nothing(self) -> None:
        """Replaces WizardTerminalTests.test_declining_the_move_registers_nothing (W6.2)."""
        self.h.plant_legacy_records()
        self.h.fake_register('{"ready": true, "state_moved": "/somewhere/old copy", "state_root": "/x"}')
        _, screen = self.to_register(DOWN + ENTER)  # No
        self.assertIn("The old records were not moved", screen)
        self.assertEqual([], self.h.register_log)

    def test_an_earlier_versions_records_are_upgraded_only_when_approved(self) -> None:
        """Replaces WizardTerminalTests.test_an_earlier_versions_records_are_upgraded_only_when_approved (W6.3)."""
        self.h.fake_upgrade('{"ready": true, "state_moved": null, "ledger_upgraded_from": 6, '
                            '"ledger_copy": "/kept/old ledger"}')
        _, screen = self.to_register(ENTER)  # Yes, upgrade them
        question = self.flat(screen.split("Upgrade the records now?", 1)[1].split("Yes, upgrade them", 1)[0])
        self.assertIn("written by an earlier version of the orchestrator", question)
        self.assertIn("keeps a checked copy of everything as it is", question)
        self.assertIn("until you run this setup for each of them", question)
        self.assertIn("Upgraded your orchestrator records. A copy of them as they were is at /kept/old ledger",
                      self.flat(screen))
        self.assertIn("Collector status: ready", screen)
        self.assertEqual(2, len(self.h.register_log))
        self.assertNotIn("--upgrade-ledger", self.h.register_log[0])
        self.assertIn("--upgrade-ledger", self.h.register_log[1])

    def failed_upgrade(self, reason: str, said: str, never: str) -> None:
        self.h.fake_upgrade('{"ready": false, "reason_code": "%s", "ledger_copy": "/kept/old ledger",'
                            ' "state_moved": "/somewhere/old copy"}' % reason)
        _, screen = self.to_register(ENTER)  # Yes, upgrade them
        last = self.flat(screen[screen.rindex("Collector registration failed"):])
        self.assertIn(said, last)
        self.assertIn("A copy of the records as they were before the upgrade is at /kept/old ledger", last)
        self.assertIn("stay there; the old copy is at /somewhere/old copy", last)
        self.assertNotIn(never, last)

    def test_an_upgrade_that_failed_before_changing_anything_says_so(self) -> None:
        """Replaces WizardTerminalTests.test_an_upgrade_that_failed_before_changing_anything_says_so (W6.5)."""
        self.failed_upgrade("ledger_upgrade_failed",
                            "Your records were not upgraded: they are as the earlier version left them.",
                            "were upgraded, but")

    def test_a_failure_after_the_upgrade_is_never_described_as_records_left_unchanged(self) -> None:
        """Replaces WizardTerminalTests.test_a_failure_after_the_upgrade_is_never_described_as_records_left_unchanged
        (W6.5)."""
        self.failed_upgrade("registration_incomplete_after_upgrade",
                            "Your records were upgraded, but registering this project did not",
                            "as the earlier version left them")

    def test_a_collector_that_did_not_start_after_registration_is_not_called_an_unfinished_registration(self) -> None:
        """Replaces WizardTerminalTests.test_a_collector_that_did_not_start_after_registration_is_not_called_an_
        unfinished_registration (W6.5)."""
        self.failed_upgrade("collector_not_started_after_registration",
                            "This project is registered, but the collector", "did not finish")

    def test_an_upgrade_whose_outcome_cannot_be_read_tells_the_owner_to_keep_everything(self) -> None:
        """Replaces WizardTerminalTests.test_an_upgrade_whose_outcome_cannot_be_read_tells_the_owner_to_keep_everything
        (W6.5)."""
        self.failed_upgrade("ledger_upgrade_unknown", "could not be read afterwards. Delete nothing",
                            "as the earlier version left them")

    def test_declining_the_upgrade_changes_nothing(self) -> None:
        """Replaces WizardTerminalTests.test_declining_the_upgrade_changes_nothing (W6.3)."""
        self.h.fake_upgrade('{"ready": true}')
        _, screen = self.to_register(DOWN + ENTER)  # No
        self.assertIn("The records were not upgraded", screen)
        self.assertIn("install the earlier version here again", self.flat(screen))
        self.assertEqual(1, len(self.h.register_log))
        self.assertNotIn("--upgrade-ledger", self.h.register_log[0])

    @unittest.skipUnless(sys.platform == "darwin", "the old records folder was macOS's")
    def test_old_records_that_also_need_the_upgrade_get_both_questions_before_anything_moves(self) -> None:
        """Replaces WizardTerminalTests.test_old_records_that_also_need_the_upgrade_get_both_questions_before_anything_
        moves (W6.4)."""
        digest = self.h.plant_legacy_records()
        self.h.fake_upgrade('{"ready": true, "state_moved": "/somewhere/old copy", "ledger_upgraded_from": 6,'
                            ' "ledger_copy": "/kept/old ledger"}')
        _, screen = self.to_register(ENTER, ENTER)  # Yes, move them; Yes, upgrade them
        self.assertLess(screen.index("Move these records to the protected folder?"),
                        screen.index("Upgrade the records now?"))
        flat = self.flat(screen)
        self.assertIn("Upgraded your orchestrator records. A copy of them as they were is at /kept/old ledger", flat)
        self.assertIn("Moved your orchestrator records once to a protected folder", flat)
        self.assertIn("Collector status: ready", flat)
        self.assertEqual(2, len(self.h.register_log))
        self.assertNotIn("--upgrade-ledger", self.h.register_log[0])  # refused before moving
        self.assertIn("--upgrade-ledger", self.h.register_log[1])
        self.assertIn(f"--move-legacy-state {digest}", " ".join(self.h.register_log[1]))

    def test_C6_setup_waits_for_a_collector_that_is_still_starting(self) -> None:
        """Replaces WizardTerminalTests.test_C6_setup_waits_for_a_collector_that_is_still_starting (W6.6)."""
        self.h.fake_register('{"ready": true, "state_moved": null}')
        self.h.fake_status(starting=3)
        _, screen = self.to_register()
        self.assertIn("Collector status: ready", screen)
        self.assertEqual(4, self.h.status_count)
        self.assertNotIn("not ready", screen)

    def test_C6_any_other_reason_still_fails_at_once(self) -> None:
        """Replaces WizardTerminalTests.test_C6_any_other_reason_still_fails_at_once (W6.6)."""
        self.h.fake_register('{"ready": true, "state_moved": null}')
        self.h.fake_status(starting=3, reason="status_owner_key_missing")
        _, screen = self.to_register()
        self.assertIn("not ready", screen)
        self.assertEqual(1, self.h.status_count)


class InstallCommitTests(unittest.TestCase):
    """W5: the install and commit step (setup.sh step 4), with a real install."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.project, self.checks = self.h.make_project()

    def git(self, *args: str) -> str:
        return self.h.git(self.project, *args)

    def test_an_update_that_changes_nothing_offers_no_commit(self) -> None:
        """Replaces WizardTerminalTests.test_an_update_that_changes_nothing_offers_no_commit (W5.4)."""
        self.h.run(*to_commit(self.checks), ENTER, *FINISH, start=self.project)
        before = self.h.commit_count(self.project)
        self.assertEqual(2, before)
        # The project is listed now: pick it, update, keep the routing, install.
        _, screen = self.h.run(ENTER, ENTER, ENTER, ENTER, ENTER, ENTER, *FINISH, start=self.project)
        self.assertIn("Nothing to commit: the project already has these files committed", screen)
        self.assertNotIn("Commit now", screen)
        self.assertNotIn("Next: commit the installed files", screen)
        final = self.h.final(screen)
        self.assertIn("nothing to commit", final)
        self.assertNotIn("commit failed", final)
        self.assertNotIn("The commit", final)
        self.assertEqual(before, self.h.commit_count(self.project))

    def test_only_paths_with_changes_are_committed(self) -> None:
        """Replaces WizardTerminalTests.test_only_paths_with_changes_are_committed (W5.1, W5.3)."""
        import json
        import subprocess
        routing_in = self.h.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run([sys.executable, str(ROOT / "scripts" / "install.py"), "install", "--target", str(self.project),
                        "--project-agent-routing", str(routing_in), "--operational-checks", str(self.checks),
                        "--apply"], check=True, capture_output=True)
        self.git("add", "-A")
        self.git("commit", "-qm", "adopt")
        before = self.h.commit_count(self.project)
        # Update the install; choose providers (none kept); looks right; install; commit now.
        _, screen = self.h.run(ENTER, ENTER, ENTER, ENTER, ENTER, ENTER, ENTER, ENTER, *FINISH, start=self.project)
        commit_screen = screen[screen.index("Next: commit the installed files"):screen.index("Commit now")]
        self.assertIn("Only these 1 paths", commit_screen)
        self.assertIn(".codex/orchestration/provider-routing.json", commit_screen)
        self.assertNotIn("install-manifest.json", commit_screen)
        self.assertIn("; committed", self.h.final(screen))
        self.assertEqual(before + 1, self.h.commit_count(self.project))
        self.assertEqual([".codex/orchestration/provider-routing.json"],
                         self.git("show", "--name-only", "--format=", "HEAD").split())

    def test_summary_says_not_committed_and_lists_what_is_left(self) -> None:
        """Replaces WizardTerminalTests.test_summary_says_not_committed_and_lists_what_is_left (W5.7, W10.5)."""
        _, screen = self.h.run(*to_commit(self.checks), DOWN + ENTER, *FINISH, start=self.project)
        final = self.h.final(screen)
        self.assertIn("not committed (you chose to commit yourself)", final)
        self.assertIn("Left uncommitted in the project:", final)
        self.assertIn(".agents/skills/", final)
        self.assertRegex(final, r"\.\.\. and \d+ more")
        self.assertNotIn("commit failed", final)
        self.assertNotEqual("", self.git("status", "--porcelain"))

    def test_the_hooks_are_offered_for_trust_only_after_the_runtime_check(self) -> None:
        """Replaces WizardTerminalTests.test_the_hooks_are_offered_for_trust_only_after_the_runtime_check (W8.3)."""
        _, screen = self.h.run(*to_commit(self.checks), DOWN + ENTER, *FINISH, start=self.project)
        self.assertIn("Close every Codex session in this project first.", screen)
        commit_screen = re.sub(r"\s+", " ", screen[:screen.index("Commit now")])
        self.assertIn("In Codex, open /hooks and trust the orchestrator's learning hooks (main checkout only)",
                      commit_screen)
        self.assertNotIn("Do not trust", commit_screen)

    def test_a_file_planted_in_the_orchestration_folder_stops_the_install(self) -> None:
        """Replaces WizardTerminalTests.test_a_file_planted_in_the_orchestration_folder_stops_the_install (W1.6)."""
        planted = self.project / ".codex" / "orchestration"
        planted.mkdir(parents=True)
        (planted / "socket.py").write_text("raise SystemExit(1)\n")
        self.git("add", "-f", ".codex")
        self.git("commit", "-qm", "planted")
        _, screen = self.h.run(ENTER, ENTER, f"{self.checks}\r", start=self.project)
        self.assertIn("holds files the orchestrator does not install", re.sub(r"\s+", " ", screen))
        self.assertNotIn("open /hooks", re.sub(r"\s+", " ", screen))
        self.assertFalse((self.project / ".codex" / "hooks.json").exists())
        self.assertFalse((self.project / ".codex" / "orchestration" / "install-manifest.json").exists())

    def test_a_failed_path_helper_stages_and_commits_nothing(self) -> None:
        """Replaces WizardTerminalTests.test_a_failed_path_helper_stages_and_commits_nothing (W5.5)."""
        self.h.helper_fails = True
        _, screen = self.h.run(*to_commit(self.checks), *FINISH, start=self.project)
        self.assertIn("commit failed: no paths to commit", screen)
        self.assertNotIn("Commit now", screen)
        self.assertIn("commit failed", self.h.final(screen))
        self.assertEqual(1, self.h.commit_count(self.project))
        self.assertEqual("", self.git("diff", "--cached", "--name-only"))


if __name__ == "__main__":
    unittest.main()
