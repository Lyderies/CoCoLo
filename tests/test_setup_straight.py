"""S4: setup goes straight through (PLAN S4-1 to S4-4; DESIGN-0.4.0-S4-straight.md).

Integration stage: the in-process wizard driver (tests/wizard_direct_driver.py), with fake clients.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_setup_update import REGISTERED, SetupUpdateTests  # noqa: E402


class SetupStraightTests(SetupUpdateTests):
    def to_fresh_choice(self, **env):
        project, checks = self.make_project()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count), **env})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Path to operational-checks.json")  # S4 / verifier: a needed input stays asked
        self.send(f"{checks}\r")
        self.expect("Set up (recommended)")
        return project

    def test_S4_1_a_fresh_project_offers_set_up_configure_and_quit_and_quit_changes_nothing(self):
        project = self.to_fresh_choice()
        screen = self.plain(self.transcript)
        order = [screen.rindex(label) for label in ("Set up (recommended)", "Configure step by step", "Quit")]
        self.assertEqual(sorted(order), order)
        self.send("\x1b[B\x1b[B\r")  # Quit
        self.finish(timeout=30)
        self.assertFalse((project / ".codex" / "orchestration").exists())

    def test_S4_2_set_up_asks_only_the_owners_choices_and_installs_everything(self):
        project = self.to_fresh_choice()
        self.send("\r")  # Set up
        self.expect("Codex children")
        self.send(" \r")  # Codex children off (a provider choice stays asked)
        self.expect("Looks right")
        self.send("\r")
        self.expect("Commit now")  # commit stays asked
        self.send("\r")
        code = self.finish(timeout=90)
        screen = self.plain(self.transcript)
        self.assertEqual(0, code, screen[-1500:])
        for implied in ("Install now", "Register now", "Start the attestation", "Back to the main menu"):
            self.assertNotIn(implied, screen)  # not offered: done as Set up says
        self.assertTrue((project / ".codex" / "orchestration" / "cli.py").is_file())
        self.assertEqual(1, self.attested())  # attest.py ran (it asks the passphrase itself)

    def test_S4_1_an_installed_project_offers_update_configure_and_quit_and_configure_keeps_the_menu(self):
        project = self.installed()
        self.start(project, REGISTERED)
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        screen = self.plain(self.transcript)
        order = [screen.rindex(label) for label in ("Update this project (recommended)", "Configure", "Quit")]
        self.assertEqual(sorted(order), order)
        self.send("\x1b[B\r")  # Configure
        self.expect("Verify the install only")  # today's eight-entry menu, unchanged
        self.expect("Replace a lost owner key")

    def set_up_to_commit(self, **env):
        project = self.to_fresh_choice(**env)
        self.send("\r")  # Set up
        self.expect("Codex children")
        self.send(" \r")
        self.expect("Looks right")
        self.send("\r")
        self.expect("Commit now")
        return project

    def test_S4_2_declining_the_commit_in_set_up_leaves_the_install_uncommitted(self):
        import subprocess
        project = self.set_up_to_commit()
        self.send("\x1b[B\r")  # I will commit myself
        # Not the exit status: an uncommitted install is not verified, which setup already counts as a
        # failure on every path (a leftover recorded in PLAN S4, not an S4 change).
        self.finish(timeout=90)
        tracked = subprocess.run(["git", "-C", str(project), "ls-files", ".codex/orchestration/cli.py"],
                                 capture_output=True, text=True, env=self.env).stdout
        self.assertEqual("", tracked)  # the owner's choice was kept: nothing committed
        self.assertTrue((project / ".codex" / "orchestration" / "cli.py").is_file())

    def test_S4_2_a_failed_step_in_set_up_offers_the_retry_menu_and_going_back_leaves_set_up(self):
        self.set_up_to_commit(FAKE_ATTEST_FAILS="1")
        self.send("\r")  # commit
        self.expect("attest stub run 1")
        self.expect("Try again")  # the retry menu, not update mode's stop
        self.assertIn("Back to the main menu", self.screen)
        self.send("\x1b[B\x1b[B\r")  # Back to the main menu
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\x1b[B\r")  # Configure
        self.expect("Update the install and walk through every step")
        self.send("\x1b[B\r")
        for wait in ("Continue", "Keep the existing configuration"):
            self.expect(wait)
            self.send("\r")
        self.expect("Install now")  # asked again: Set up ended with the attempt


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited update tests run in test_setup_update."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(SetupStraightTests):
        if name in SetupStraightTests.__dict__:
            suite.addTest(SetupStraightTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
