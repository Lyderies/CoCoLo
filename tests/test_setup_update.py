"""S1: one-step update (PLAN S1-U1, S1-U2, S1-U6; DESIGN-0.4.0-S1-update.md).

`./setup.sh --update PROJECT` updates an installed project with no menu: every earlier answer is reused
and only attest.py asks, for the owner-key passphrase. It stops plainly, changing nothing, on a real
problem. Driven in-process like test_setup_wizard_direct, with test_setup_sh's fixtures; this module
runs only the tests defined here.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_setup_sh import WizardTerminalTests  # noqa: E402
from wizard_direct_driver import DirectDriverMixin  # noqa: E402

REGISTERED = {"FAKE_REGISTER": '{"ready": true, "state_moved": null}'}


class SetupUpdateTests(DirectDriverMixin, WizardTerminalTests):
    def installed(self) -> Path:
        """A project installed and committed by an ordinary setup run, as the update starts from."""
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks)
        self.send("\r")
        self.finish_after_install()
        self._reap()
        return project

    def update(self, project: Path, extra_env: dict | None = None) -> tuple[int, str]:
        """`--update PROJECT` with no key pressed at all; its exit status and whole screen."""
        self.attest_count = self.base / "attest.count"
        env = {**REGISTERED, "FAKE_ATTEST": str(self.attest_count), **(extra_env or {})}
        self.start(project, env, argv=["--update", str(project)])
        code = self.finish(timeout=60)
        return code, self.plain(self.transcript)

    @staticmethod
    def plain(text: str) -> str:
        import re
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", text).replace("\r", "")

    def attested(self) -> int:
        return int(self.attest_count.read_text()) if self.attest_count.exists() else 0

    def test_U1_an_installed_project_is_updated_with_no_menu_and_only_the_attestation_asks(self):
        project = self.installed()
        commits = self.commit_count(project)
        code, screen = self.update(project)
        self.assertEqual(0, code, screen[-1500:])
        self.assertNotIn("Enter select", screen)  # no menu was shown
        self.assertNotIn("Press Enter to continue", screen)
        self.assertEqual(1, self.attested())  # attest.py ran once: the only prompt is its own
        for step in ("Checks", "Project", "Providers", "Install", "Collector", "Billing"):
            self.assertRegex(screen, rf"OK\s+\d\. {step}:", step)
        self.assertEqual(commits, self.commit_count(project))  # nothing changed, so nothing committed

    def test_U1_the_update_reuses_the_projects_provider_choices(self):
        project = self.installed()
        routing = project / ".codex" / "orchestration" / "provider-routing.json"
        before = routing.read_bytes()
        code, screen = self.update(project)
        self.assertEqual(0, code, screen[-1500:])
        self.assertEqual(before, routing.read_bytes())
        self.assertIn("Keeping the existing provider configuration", screen)

    def test_U1_a_project_without_an_install_is_refused_before_anything_changes(self):
        project, _ = self.make_project()
        code, screen = self.update(project)
        self.assertEqual(1, code)
        self.assertIn("has no COCOLO install to update", screen)
        self.assertFalse((project / ".codex" / "orchestration" / "install-manifest.json").exists())
        self.assertEqual(0, self.attested())

    def test_U6_uncommitted_managed_files_stop_the_update_and_change_nothing(self):
        project = self.installed()
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        changed = project / ".codex" / "orchestration" / "operational-checks.json"
        changed.write_text(changed.read_text() + "\n")
        before = manifest.read_bytes()
        code, screen = self.update(project)
        self.assertEqual(1, code)
        self.assertIn("managed_files_uncommitted", screen)
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(0, self.attested())

    def test_U6_a_client_that_is_not_signed_in_stops_the_update_and_changes_nothing(self):
        project = self.installed()
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        before = manifest.read_bytes()
        (self.fakes / "claude").write_text(
            '#!/bin/sh\necho \'{"loggedIn": false, "subscriptionType": "max", "authMethod": "x"}\'\n')
        code, screen = self.update(project)
        self.assertEqual(1, code)
        self.assertIn("claude: installed but not signed in", screen)
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(0, self.attested())

    def test_U6_records_that_need_the_owners_choice_stop_the_update(self):
        project = self.installed()
        code, screen = self.update(project, {"FAKE_REGISTER": "", "FAKE_UPGRADE": '{"ready": true}'})
        self.assertEqual(1, code)
        self.assertIn("records_need_owner_choice", screen)
        self.assertEqual(0, self.attested())

    def test_U2_the_menu_entry_update_this_project_does_the_same(self):
        project = self.installed()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count)})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project")
        self.send("\r")  # the first entry
        code = self.finish(timeout=60)
        screen = self.plain(self.transcript)
        self.assertEqual(0, code, screen[-1500:])
        self.assertEqual(1, self.attested())
        self.assertNotIn("Keep the existing configuration", screen)  # no further question


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the inherited terminal tests run in their own modules."""
    names = sorted(name for name in vars(SetupUpdateTests) if name.startswith("test_"))
    return unittest.TestSuite(SetupUpdateTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
