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

from wizard_flow_harness import ROOT, Harness  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
