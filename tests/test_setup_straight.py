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

    # S4-3: an update skips the attestation when every enabled provider's current account is attested.

    ATTESTED = '{"ready": true, "collector_runtime_version": "0.0.0", "provider_attestations": ' \
               '{"codex": {"state": "missing"}, "claude": {"state": "present"}}}'
    MISSING = ATTESTED.replace('"claude": {"state": "present"}', '"claude": {"state": "missing"}')
    OBSERVED = '{"ready": true, "provider": "claude"}'

    def test_S4_3_update_skips_billing_when_attested_and_runs_it_otherwise(self):
        project = self.installed()
        identity = self.ATTESTED.replace('"claude": {"state": "present"}', '"claude": {"state": "identity_missing"}')
        malformed = '{"ready": true, "collector_runtime_version": "0.0.0", "provider_attestations": []}'
        not_ready = self.ATTESTED.replace('{"ready": true', '{"ready": false', 1)
        cases = [("attested", {"FAKE_STATUS_REPLY": self.ATTESTED, "FAKE_OBSERVE": self.OBSERVED}, 0),
                 ("missing", {"FAKE_STATUS_REPLY": self.MISSING, "FAKE_OBSERVE": self.OBSERVED}, 1),
                 ("identity missing", {"FAKE_STATUS_REPLY": identity, "FAKE_OBSERVE": self.OBSERVED}, 1),
                 ("status malformed", {"FAKE_STATUS_REPLY": malformed, "FAKE_OBSERVE": self.OBSERVED}, 1),
                 ("status not ready", {"FAKE_STATUS_REPLY": not_ready, "FAKE_STATUS_REPLY_FROM": "2",
                                       "FAKE_OBSERVE": self.OBSERVED}, 1),
                 ("observe refused", {"FAKE_STATUS_REPLY": self.ATTESTED}, 1)]
        for name, env, runs in cases:
            with self.subTest(name):
                self.attest_count = self.base / "attest.count"
                self.attest_count.unlink(missing_ok=True)
                (self.base / "status.count").unlink(missing_ok=True)
                code, screen = self.update(project, env)
                self.assertEqual(0, code, screen[-1500:])
                self.assertEqual(runs, self.attested())
                self.assertEqual(runs == 0, "already attested" in screen)
                self._reap()
        # Only the enabled provider (Claude; Codex is off) is observed, for this project, by this clone's CLI.
        observes = [line for line in (self.base / "register.log").read_text().splitlines() if "provider-observe" in line]
        self.assertTrue(observes)
        for line in observes:
            self.assertIn("--provider claude", line)
            self.assertIn(f"--project-root {project}", line)

    def test_S4_3_the_menus_update_also_skips_billing_when_attested(self):
        project = self.installed()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count),
                             "FAKE_STATUS_REPLY": self.ATTESTED, "FAKE_OBSERVE": self.OBSERVED})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\r")
        self.assertEqual(0, self.finish(timeout=60))
        self.assertEqual(0, self.attested())

    def test_S4_3_the_menus_update_runs_billing_when_one_is_missing(self):
        project = self.installed()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count),
                             "FAKE_STATUS_REPLY": self.MISSING, "FAKE_OBSERVE": self.OBSERVED})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\r")
        self.assertEqual(0, self.finish(timeout=60))
        self.assertEqual(1, self.attested())

    def test_S4_3_configure_billing_only_always_runs_the_attestation(self):
        project = self.installed()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count),
                             "FAKE_STATUS_REPLY": self.ATTESTED, "FAKE_OBSERVE": self.OBSERVED})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\x1b[B\r")  # Configure
        self.expect("Re-run the billing attestation only")
        self.send("\x1b[B\x1b[B\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("attest stub run 1")

    def test_S4_3_an_answers_file_always_runs_the_attestation(self):
        project, checks = self.make_project()
        answers = self.answers_for(project, checks)
        code, screen = self.scripted(answers, {"FAKE_STATUS_REPLY": self.ATTESTED, "FAKE_OBSERVE": self.OBSERVED})
        self.assertEqual(0, code, screen[-1500:])
        self.assertEqual(1, self.attested())

    # Review 040-s4-code: Quit changes nothing; the owner's profile, rule and records choices stay asked.

    def test_S4_1_quit_on_an_installed_project_changes_nothing_in_it(self):
        # Not the remembered list: a project chosen moves to the top of it (test_setup_sh's
        # test_projects_set_up_before_are_offered_in_a_list), an earlier requirement kept.
        import subprocess
        project = self.installed()
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        before = manifest.read_bytes()
        self.start(project, REGISTERED)
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\x1b[B\x1b[B\r")  # Quit
        self.assertEqual(0, self.finish(timeout=30))
        self.assertEqual(before, manifest.read_bytes())
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual("", status)

    def test_S4_2_set_up_asks_the_profile_and_the_commands_rule_and_declining_writes_neither(self):
        project, checks = self.make_project(base=self.non_temporary_base())
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.base / "attest.count"), "FAKE_RULE": "missing"})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        self.expect("Set up (recommended)")
        self.send("\r")
        self.expect("Codex children")
        self.send("\x1b[B" * 5 + " \r")  # bridging on, Codex and Claude children kept on
        self.expect("Looks right")
        self.send("\r")
        self.expect("Commit now")
        self.send("\r")
        self.expect("Create the profile")  # asked
        self.send("\x1b[B\r")  # Skip
        self.expect("No, not now")  # the commands rule, asked, refusal first
        self.send("\r")
        self.finish(timeout=90)
        self.assertFalse((self.home / ".codex" / "orchestrator.config.toml").exists())
        self.assertNotIn("install-codex-rule", self.rule_log.read_text() if self.rule_log.exists() else "")

    @unittest.skipUnless(sys.platform == "darwin", "the old records folder was macOS's")
    def test_S4_2_set_up_asks_before_moving_earlier_records(self):
        self.plant_legacy_records()
        moved = '{"ready": true, "state_moved": "/somewhere/old copy", "state_root": "/x"}'
        project, checks = self.make_project()
        self.start(project, {"FAKE_REGISTER": moved, "FAKE_ATTEST": str(self.base / "attest.count")})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        self.expect("Set up (recommended)")
        self.send("\r")
        self.expect("Codex children")
        self.send(" \r")
        self.expect("Looks right")
        self.send("\r")
        self.expect("Commit now")
        self.send("\r")
        self.expect("Move these records to the protected folder?")  # asked, not implied
        self.send("\x1b[B\r")  # No
        self.finish(timeout=90)
        self.assertIn("old records not moved", self.plain(self.transcript))  # the summary keeps it
        self.assertFalse((self.base / "register.log").exists())


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited update tests run in test_setup_update."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(SetupStraightTests):
        if name in SetupStraightTests.__dict__:
            suite.addTest(SetupStraightTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
