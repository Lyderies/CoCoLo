"""S1: one-step update and scripted setup (PLAN S1-U1 to S1-U3, S1-U6; DESIGN-0.4.0-S1-update.md).

`./setup.sh --update PROJECT` updates an installed project with no menu: every earlier answer is reused
and only attest.py asks, for the owner-key passphrase. It stops plainly, changing nothing, on a real
problem. Driven in-process like test_setup_wizard_direct, with test_setup_sh's fixtures; this module
runs only the tests defined here.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_setup_sh import WizardTerminalTests  # noqa: E402
from wizard_direct_driver import DirectDriverMixin  # noqa: E402

REGISTERED = {"FAKE_REGISTER": '{"ready": true, "state_moved": null}'}


class SetupUpdateTests(DirectDriverMixin, WizardTerminalTests):
    def installed(self) -> Path:
        """A project installed and committed by an ordinary setup run, as the update starts from. Codex
        children are off, so the commands step is skipped: with Codex on it fails here (the test's HOME is
        not the account's home, so no rule can be installed), and an update reports a failed step (exit 1)."""
        project, checks = self.make_project()
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        self.expect("fresh install is possible")
        self.send("\x1b[B\r\r")  # S4: Configure step by step, then Continue
        self.expect("Codex children")
        self.send(" \r")  # Codex children off
        for wait in ("Looks right", "Install now", "Commit now", "Continue"):
            self.expect(wait)
            self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\x1b[B\r")
        self.expect("Not chosen")
        self.send("\r")
        self.expect("remove --target")
        self.send("\r")
        self.assertEqual(0, self.finish())
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
        self.assertIn("Update the install and walk through every step", screen)
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


    # S1-U3: ./setup.sh --answers FILE -----------------------------------------------------------

    def answers_for(self, project: Path, checks: Path, **changes) -> Path:
        answers = {"project": str(project), "operational_checks": str(checks),
                   "providers": {"codex": "off", "claude": "on", "claude_models": "claude-sonnet-5-5",
                                 "bridge": "off"},
                   "commit": True, "commands_rule": False, "move_records": False, "upgrade_records": False}
        for key, value in changes.items():
            if value is None:
                answers.pop(key, None)
            else:
                answers[key] = value
        path = self.base / "answers.json"
        path.write_text(json.dumps(answers))
        return path

    def scripted(self, answers: Path, extra_env: dict | None = None) -> tuple[int, str]:
        self.attest_count = self.base / "attest.count"
        env = {**REGISTERED, "FAKE_ATTEST": str(self.attest_count), **(extra_env or {})}
        self.start(self.base, env, argv=["--answers", str(answers)])
        code = self.finish(timeout=60)
        return code, self.plain(self.transcript)

    def test_U3_a_fresh_project_is_installed_from_an_answers_file_with_no_menu(self):
        project, checks = self.make_project()
        code, screen = self.scripted(self.answers_for(project, checks))
        self.assertEqual(0, code, screen[-1500:])
        self.assertNotIn("Enter select", screen)
        self.assertTrue((project / ".codex" / "orchestration" / "install-manifest.json").is_file())
        routing = json.loads((project / ".codex" / "orchestration" / "provider-routing.json").read_text())
        self.assertIn("claude-sonnet-5-5", json.dumps(routing))
        self.assertEqual(1, self.attested())
        self.assertEqual(2, self.commit_count(project))  # the project's own commit, then the install

    def test_U3_commit_false_leaves_the_install_uncommitted(self):
        project, checks = self.make_project()
        code, screen = self.scripted(self.answers_for(project, checks, commit=False))
        # Uncommitted files cannot be verified, so the run ends 1 (every step ok is 0), saying why.
        self.assertEqual(1, code, screen[-1500:])
        self.assertIn("not committed (you chose to commit yourself)", screen)
        self.assertIn("must be committed and clean", screen)
        self.assertEqual(1, self.commit_count(project))

    def test_U3_a_missing_unknown_or_invalid_answer_is_refused_naming_it_before_anything_changes(self):
        project, checks = self.make_project()
        cases = {"commit": {"commit": None}, "colour": {"colour": "blue"}, "mode": {"mode": "install"},
                 "move_records": {"move_records": None}, "upgrade_records": {"upgrade_records": None},
                 "operational_checks": {"operational_checks": ""},
                 "providers.codex": {"providers": {"codex": "maybe", "claude": "on",
                                                   "claude_models": "claude-sonnet-5-5", "bridge": "off"}},
                 "commands_rule": {"commands_rule": "yes"},
                 "billing_sign_again": {"billing_sign_again": "yes"}}
        for named, changes in cases.items():
            with self.subTest(named=named):
                code, screen = self.scripted(self.answers_for(project, checks, **changes))
                self.assertEqual(2, code)
                self.assertIn(f"answers file: {named}", screen)
                self.assertFalse((project / ".codex").exists())
                self.assertEqual(0, self.attested())

    def test_U3_records_that_need_a_choice_follow_the_answer(self):
        upgrade = {"FAKE_REGISTER": "", "FAKE_UPGRADE": '{"ready": true, "state_moved": null}'}
        project, checks = self.make_project()
        code, screen = self.scripted(self.answers_for(project, checks), upgrade)  # upgrade_records false
        # Declined as answered: skipped as chosen (exit 0), never upgraded, and the summary says so.
        self.assertEqual(0, code, screen[-1500:])
        self.assertNotIn("--upgrade-ledger", (self.base / "register.log").read_text())
        self.assertIn("The records were not upgraded", screen)
        self.assertRegex(screen, r"SKIP\s+5\. Collector: records not upgraded")
        self._reap()
        project2, checks2 = self.make_project("second")
        code, screen = self.scripted(self.answers_for(project2, checks2, upgrade_records=True), upgrade)
        self.assertEqual(0, code, screen[-1500:])
        self.assertIn("--upgrade-ledger", (self.base / "register.log").read_text())


    # Review 040-s1-update ---------------------------------------------------------------------------

    def test_U6_the_update_stops_at_the_first_failed_step_and_runs_nothing_after_it(self):
        project = self.installed()
        self.make_newer(project)
        commits = self.commit_count(project)
        code, screen = self.update(project, {"FAKE_FAIL": "1"})  # the install's apply fails
        self.assertEqual(1, code)
        self.assertIn("The update stopped", screen)
        self.assertNotIn('Update the install and walk through every step', screen)
        self.assertEqual(0, self.attested())  # no billing, profile or commands after the failure
        self.assertNotRegex(screen, r"OK\s+\d\. Collector:")
        self.assertEqual(commits, self.commit_count(project))

    def make_newer(self, project: Path) -> None:
        """Leave the project needing an update: one managed file differs, committed."""
        import subprocess
        changed = project / ".agents" / "skills" / "orchestrate-agents" / "SKILL.md"
        changed.write_text(changed.read_text() + "\nOlder copy.\n")
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "-C", str(project), "commit", "-qam", "older"], check=True, env=env)

    def test_U6_a_step_marked_failed_ends_the_update_with_exit_1(self):
        project, checks = self.make_project()  # in a temporary folder: the commands step fails
        code, screen = self.scripted(self.answers_for(project, checks, providers={
            "codex": "on", "claude": "on", "claude_models": "claude-sonnet-5-5", "bridge": "off"}))
        self.assertEqual(1, code)
        self.assertRegex(screen, r"FAIL\s+\d\. Commands: project in a temporary folder")

    def test_U2_update_from_the_menu_after_continuing_past_a_failed_check_stops(self):
        project = self.installed()
        (self.fakes / "claude").write_text(
            '#!/bin/sh\necho \'{"loggedIn": false, "subscriptionType": "max", "authMethod": "x"}\'\n')
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        before = manifest.read_bytes()
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count)})
        self.expect("Continue anyway")
        self.send("\x1b[B\r")  # Continue anyway
        self.choose_project()
        self.expect("Update this project")
        self.send("\r")
        code = self.finish(timeout=60)
        screen = self.plain(self.transcript)
        self.assertEqual(1, code)
        self.assertIn("every check passed", screen)
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(0, self.attested())

    def test_U6_a_refused_project_is_not_remembered(self):
        project = self.installed()
        listed = self.home / ".local" / "state" / "codex-agents" / "installer" / "setup-projects"
        listed.write_text("")
        changed = project / ".codex" / "orchestration" / "operational-checks.json"
        changed.write_text(changed.read_text() + "\n")
        code, _ = self.update(project)
        self.assertEqual(1, code)
        self.assertEqual("", listed.read_text())


    # S1-U8 ------------------------------------------------------------------------------------------

    def test_U8_the_update_starts_the_projects_new_service_once_the_collector_is_ready(self):
        project = self.installed()
        code, screen = self.update(project)
        self.assertEqual(0, code, screen[-1500:])
        calls = (self.base / "register.log").read_text().splitlines()
        started = [i for i, line in enumerate(calls) if "learning-start-project-service" in line]
        registered = [i for i, line in enumerate(calls) if "learning-register" in line]
        self.assertEqual(1, len(started))
        self.assertLess(registered[-1], started[0])  # after the collector is registered and ready
        self.assertIn(str(project), calls[started[0]])

    def test_U8_a_service_that_cannot_start_stops_the_update_with_its_reason(self):
        project = self.installed()
        code, screen = self.update(project, {"FAKE_START": '{"ready": false, "reason_code": "project_service_other_version"}'})
        self.assertEqual(1, code)
        self.assertIn("project_service_other_version", screen)
        self.assertEqual(0, self.attested())  # stopped at the collector step

    # UPDFIX (L12.3) ----------------------------------------------------------------------------------

    REFUSED = '{"ready": true, "children_running": true, "would_replace": true, "refused": true, "reason": "predecessor_unverified", "message": "the running version could not be confirmed; nothing was changed"}'

    def snapshot(self, project: Path) -> tuple:
        """The manifest's bytes, Git's HEAD and the commit count: what a refused update must leave alone."""
        import subprocess
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True,
                              env=self.env).stdout.strip()
        return manifest.read_bytes(), head, self.commit_count(project)

    def test_B4t_unverified_predecessor_is_refused_before_anything_is_installed(self):
        project = self.installed()
        before = self.snapshot(project)
        code, screen = self.update(project, {**REGISTERED, "FAKE_CHILDREN": self.REFUSED})
        flat = " ".join(screen.split())
        self.assertEqual(1, code, screen[-1500:])
        self.assertIn("predecessor_unverified", flat)
        for words in ("the running version could not be confirmed", "nothing was changed"):
            self.assertIn(words, flat)
        self.assertEqual(before, self.snapshot(project))  # no file written, nothing committed
        self.assertNotIn("Installing", screen)  # the installer never ran
        self.assertEqual(0, self.attested())
        calls = (self.base / "register.log").read_text().splitlines()
        self.assertEqual(1, len(calls))  # only the read-only check: nothing was registered or started
        self.assertIn("learning-update-check", calls[0])
        self.assertIn(str(project), calls[0])

    def test_UPDFIX_3_a_check_that_does_not_refuse_leaves_the_update_as_it_was(self):
        project = self.installed()
        for reply in ('{"ready": true, "children_running": true, "would_replace": false, "refused": false}',
                      '{"ready": true, "children_running": false, "would_replace": true, "refused": false}'):
            with self.subTest(reply=reply):
                code, screen = self.update(project, {**REGISTERED, "FAKE_CHILDREN": reply})
                self.assertEqual(0, code, screen[-1500:])
                self.assertNotIn("child_run_active", screen)

    def test_B4t_the_busy_takeover_finishes_before_install_and_completes_after_service_start(self):
        project = self.installed()
        plan = json.dumps(dict(ready=True, children_running=True, would_replace=True, refused=False,
            takeover=dict(takeover='planned', children=1, other_children=0, service_version='0.8.66',
                          records_upgrade=False)))
        reply = json.dumps(dict(ready=True, takeover=True, phase='account_done', predecessor_version='0.8.66',
                                successor_version='0.8.67', runs=1, lost=1, finished_kept=2, killed=False))
        code, screen = self.update(project, {**REGISTERED, 'FAKE_CHILDREN': plan, 'FAKE_TAKEOVER': reply})
        self.assertEqual(0, code, screen[-1500:])
        flat = ' '.join(screen.split())
        self.assertIn('that run ends as lost', flat)
        self.assertIn('Finished results are kept and can still be recorded', flat)
        self.assertIn('1 child run ended (lost, ended by the update). 2 finished results kept.', flat)
        self.assertNotIn('try again when none is running', flat)
        calls = (self.base / 'register.log').read_text().splitlines()
        stops = [i for i, call in enumerate(calls) if 'learning-update-takeover' in call]
        self.assertEqual(3, len(stops), calls)
        self.assertIn('--action run', calls[stops[0]])
        self.assertIn('--action installed', calls[stops[1]])
        self.assertIn('--action complete', calls[stops[2]])
        self.assertLess(stops[0], next(i for i, call in enumerate(calls) if 'learning-register' in call))
        self.assertLess(next(i for i, call in enumerate(calls) if 'learning-start-project-service' in call), stops[2])

    def test_UPDFIX_3_a_refused_project_is_not_remembered(self):
        project = self.installed()
        listed = self.home / ".local" / "state" / "codex-agents" / "installer" / "setup-projects"
        listed.write_text("")
        code, _ = self.update(project, {**REGISTERED, "FAKE_CHILDREN": self.REFUSED})
        self.assertEqual(1, code)
        self.assertEqual("", listed.read_text())

    NOT_REFUSED = '{"ready": true, "children_running": false, "would_replace": false, "refused": false}'
    UNREADABLE = ('{"ready": true, "children_running": false, "would_replace": false, "unreadable": true, '
                  '"refused": true, "reason": "ledger_unreadable"}')

    def test_UPDFIX_3_a_check_that_cannot_tell_refuses_before_anything_is_installed(self):
        # Decision 265: fail closed. A reply that is not a readable answer, or says the ledger is unreadable, refuses.
        project = self.installed()
        for reply in (self.UNREADABLE, "not json at all", "[]", '{"ready": true}'):
            with self.subTest(reply=reply):
                before = self.snapshot(project)
                code, screen = self.update(project, {**REGISTERED, "FAKE_CHILDREN": reply})
                flat = " ".join(screen.split())
                self.assertEqual(1, code, screen[-1500:])
                self.assertIn("could not tell whether a child run is active", flat)
                self.assertIn("nothing was changed", flat)
                self.assertEqual(before, self.snapshot(project))
                self.assertNotIn("Installing", screen)
                self.assertEqual(0, self.attested())

    def test_B4t_a_predecessor_that_becomes_unverified_is_refused_before_any_write(self):
        # Review r1 MED: the check is repeated right before the installer's --apply (step 4's first write).
        project = self.installed()
        before = self.snapshot(project)
        applies = self.py_full.read_text().count("--apply")
        code, screen = self.update(project, {**REGISTERED, "FAKE_CHILDREN": self.NOT_REFUSED,
                                             "FAKE_CHILDREN_LATER": self.REFUSED})
        flat = " ".join(screen.split())
        self.assertEqual(1, code, screen[-1500:])
        self.assertIn("predecessor_unverified", flat)
        self.assertIn("the running version could not be confirmed", flat)
        self.assertEqual(before, self.snapshot(project))
        self.assertEqual(applies, self.py_full.read_text().count("--apply"))  # the installer never wrote
        calls = (self.base / "register.log").read_text().splitlines()
        self.assertEqual(2, len(calls))  # step 2's check and the one before --apply; nothing registered
        self.assertTrue(all("learning-update-check" in call for call in calls))
        self.assertEqual(0, self.attested())

    def test_UPDFIX_3_an_interactive_refused_update_leaves_the_remembered_projects_alone(self):
        # Review r1 MED: opening an installed project interactively remembered it before the child check.
        project = self.installed()
        listed = self.home / ".local" / "state" / "codex-agents" / "installer" / "setup-projects"
        listed.write_text("")
        self.attest_count = self.base / "attest.count"
        self.start(project, {**REGISTERED, "FAKE_ATTEST": str(self.attest_count), "FAKE_CHILDREN": self.REFUSED})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\r")
        self.assertEqual(1, self.finish(timeout=60))
        self.assertIn("the running version could not be confirmed", " ".join(self.plain(self.transcript).split()))
        self.assertEqual("", listed.read_text())

    def test_UPDFIX_3_a_refusal_at_the_check_before_the_install_leaves_the_remembered_projects_alone(self):
        # Review r2c: the project was remembered between step 2's check and the one right before --apply, so a refusal
        # at the second changed setup-projects. Both entry paths (the update and the interactive open).
        project = self.installed()
        listed = self.home / ".local" / "state" / "codex-agents" / "installer" / "setup-projects"
        listed.write_text("")
        env = {**REGISTERED, "FAKE_CHILDREN": self.NOT_REFUSED, "FAKE_CHILDREN_LATER": self.REFUSED}
        code, screen = self.update(project, env)
        self.assertEqual(1, code, screen[-1500:])
        self.assertIn("the running version could not be confirmed", " ".join(screen.split()))
        self.assertEqual("", listed.read_text(), "the update path")
        self.attest_count = self.base / "attest.count"
        self.start(project, {**env, "FAKE_ATTEST": str(self.attest_count)})
        self.expect("Everything looks good")
        self.send("\r")
        self.choose_project()
        self.expect("Update this project (recommended)")
        self.send("\r")
        self.assertEqual(1, self.finish(timeout=60))
        self.assertIn("the running version could not be confirmed", " ".join(self.plain(self.transcript).split()))
        self.assertEqual("", listed.read_text(), "the interactive path")

    def test_B4t_a_failed_service_stop_is_resumable_before_the_installer_writes(self):
        project = self.installed()
        before = self.snapshot(project)
        plan = json.dumps(dict(ready=True, refused=False, children_running=True, would_replace=True,
                               takeover=dict(takeover='planned', records_upgrade=False)))
        reply = json.dumps(dict(ready=False, takeover=True, phase='hint_withdrawn', reason_code='service_stop_failed',
            error="The update stopped partway (service_stop_failed): this project's old service was told to stop, and 1 child run ended as lost. Run ./setup.sh --update to finish."))
        code, screen = self.update(project, {'FAKE_CHILDREN': plan, 'FAKE_TAKEOVER': reply})
        flat = " ".join(screen.split())
        self.assertEqual(1, code, screen[-1500:])
        self.assertIn('service_stop_failed', flat)
        self.assertIn('old service was told to stop, and 1 child run ended as lost', flat)
        self.assertIn('./setup.sh --update', flat)
        self.assertNotIn('nothing was changed', flat)
        self.assertNotIn('Nothing was changed', flat)
        self.assertEqual(before, self.snapshot(project))


def load_tests(loader, tests, pattern):
    """Only the tests defined here; the inherited terminal tests run in their own modules."""
    names = sorted(name for name in vars(SetupUpdateTests) if name.startswith("test_"))
    return unittest.TestSuite(SetupUpdateTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
