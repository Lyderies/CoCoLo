"""S5: the Python wizard's steps, tested as pure decisions (DESIGN-0.4.0-S5-wizard.md).

Each test ports today's table for its requirement, so the new wizard decides exactly as setup.sh does.
No process, socket or sleep: unit stage.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ORCHESTRATION))

from setup_wizard import steps  # noqa: E402


class HomeMarkerTests(unittest.TestCase):
    """W3.1: leading home markers are expanded; everything else is left alone."""

    def test_leading_home_markers_are_expanded(self) -> None:
        home = "/Users/<you>"
        cases = {
            "~": home,
            "~/Code/app": f"{home}/Code/app",
            "~/my app": f"{home}/my app",
            "$HOME": home,
            "$HOME/Code/app": f"{home}/Code/app",
            "${HOME}/Code/app": f"{home}/Code/app",
            "  ~/Code/app  ": f"{home}/Code/app",
            "'~/Code/app'": f"{home}/Code/app",
            '"$HOME/Code/app"': f"{home}/Code/app",
            "/Code/app": "/Code/app",
            "Code/app": "Code/app",
            "~other/Code": "~other/Code",
            "/a/~/b": "/a/~/b",
            "$HOMEWORK/x": "$HOMEWORK/x",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, steps.expand_home(value, home))


class RoutingChoiceTests(unittest.TestCase):
    """W4.1-W4.3: four choices make a routing the router's own validator accepts."""

    def test_four_choices_are_required(self) -> None:
        with self.assertRaises(steps.ChoiceError):
            steps.provider_routing("on", "on")

    def test_invalid_provider_choices_are_refused(self) -> None:
        for case in (
            ("off", "off", "-", "off"),
            ("on", "on", "-", "off"),
            ("on", "off", "-", "on"),
            ("on", "on", "bad model", "off"),
            ("maybe", "on", "claude-haiku-5-5", "off"),
        ):
            with self.subTest(case=case), self.assertRaises(steps.ChoiceError):
                steps.provider_routing(*case)

    def test_routing_matches_the_router_validator(self) -> None:
        from codex_agents.routing import load_policy, validate_provider_routing
        policy = load_policy(ORCHESTRATION / "config" / "routing-policy.json")
        models = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5"
        for case in (
            ("on", "on", models, "off"),
            ("on", "on", "claude-haiku-5-5", "on"),
            ("on", "off", "-", "off"),
            ("off", "on", "claude-sonnet-5-5,claude-opus-5-5", "off"),
        ):
            with self.subTest(case=case):
                raw = steps.provider_routing(*case)
                with tempfile.TemporaryDirectory() as directory:
                    validate_provider_routing(Path(directory), raw.encode(), policy)
                config = json.loads(raw)
                self.assertEqual(case[0] == "on", config["providers"]["codex"]["enabled"])
                self.assertEqual(case[1] == "on", config["providers"]["claude"]["enabled"])
                self.assertEqual(case[3] == "on", config["codex_primary_may_bridge_to_claude"])
                if case[1] == "on":
                    self.assertEqual(case[2].split(","), config["providers"]["claude"]["models"])
                else:
                    self.assertNotIn("models", config["providers"]["claude"])

    def test_routing_is_the_same_as_setup_sh_writes(self) -> None:
        """Byte for byte what today's `setup.sh --print-provider-routing` writes (captured 2026-11-19), so
        an installed routing file does not change."""
        today = {
            ("on", "on", "claude-haiku-5-5", "on"):
                '{\n  "version": 1,\n  "providers": {\n    "codex": {"enabled": true},\n'
                '    "claude": {"enabled": true, "models": ["claude-haiku-5-5"]}\n  },\n'
                '  "codex_primary_may_bridge_to_claude": true\n}\n',
            ("on", "off", "-", "off"):
                '{\n  "version": 1,\n  "providers": {\n    "codex": {"enabled": true},\n'
                '    "claude": {"enabled": false}\n  },\n  "codex_primary_may_bridge_to_claude": false\n}\n',
        }
        for case, expected in today.items():
            with self.subTest(case=case):
                self.assertEqual(expected, steps.provider_routing(*case))



class ProfileTests(unittest.TestCase):
    """W8.1 and W4.6: the orchestrator profile, decided from its bytes (the safe read is an effect)."""

    def test_profile_compares_effective_settings_not_bytes(self) -> None:
        header = "# written by hand SECRET\n\n"
        same = (header + 'sandbox_mode = "workspace-write"  # keep\n\n'
                "[sandbox_workspace_write]\nnetwork_access = true\n")
        self.assertEqual("same", steps.profile_difference(same.encode()))
        self.assertEqual("same", steps.profile_difference((same + 'writable_roots = ["x"]\n').encode()))
        self.assertEqual("network_access", steps.profile_difference(same.replace("= true", "= false").encode()))
        self.assertEqual("sandbox_mode", steps.profile_difference(same.replace("workspace-write", "read-only").encode()))
        self.assertEqual("network_access", steps.profile_difference(b'sandbox_mode = "workspace-write"\n'))
        self.assertEqual("unreadable", steps.profile_difference(b"this is [not toml"))
        self.assertEqual("unreadable", steps.profile_difference(None))

    def test_profile_larger_than_the_read_limit_is_unreadable(self) -> None:
        good = 'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n'
        padded = good + "# pad\n" * 12000 + "network_access = false\n"
        self.assertGreater(len(padded), 65536)
        self.assertEqual("unreadable", steps.profile_difference(padded.encode()))
        self.assertEqual("same", steps.profile_difference((good + "# pad\n" * 100).encode()))

    def test_network_follows_the_last_setting_in_its_own_table(self) -> None:
        secret = "# SECRET\nmodel = \"x\"\n"
        enabled = (secret + 'sandbox_mode = "workspace-write"\n\n[other]\nnetwork_access = false\n\n'
                   "[sandbox_workspace_write]  # allow network\nnetwork_access   =   true # yes\n")
        self.assertTrue(steps.profile_network_on(enabled.encode()))
        for name, profile in {
            "network off": secret + "[sandbox_workspace_write]\nnetwork_access = false\n",
            "setting in another table": secret + "[other]\nnetwork_access = true\n",
            "setting before any table": "network_access = true\n",
            "last assignment wins": "[sandbox_workspace_write]\nnetwork_access = true\nnetwork_access = false\n",
            "commented out": "[sandbox_workspace_write]\n# network_access = true\n",
        }.items():
            with self.subTest(profile=name):
                self.assertFalse(steps.profile_network_on(profile.encode()))
        self.assertFalse(steps.profile_network_on(None))

    def test_codex_hint(self) -> None:
        """W4.6: `codex -p orchestrator` only when the profile turns network on, or is ready; else why."""
        for wanted, ready, network in ((True, False, True), (False, False, True), (True, True, True), (True, True, False)):
            with self.subTest(wanted=wanted, ready=ready, network=network):
                self.assertEqual("codex -p orchestrator", steps.codex_hint(wanted, ready, network)[0])
        command, why = steps.codex_hint(True, False, False)
        self.assertEqual("codex", command)
        self.assertIn("network_access = true", why)
        self.assertIn("missing", why)
        command, why = steps.codex_hint(False, False, False)
        self.assertEqual("codex", command)
        self.assertIn("not chosen", why)


class CodexPrimaryTests(unittest.TestCase):
    """W4.5: whether a Codex session may orchestrate, from the routing file's bytes."""

    def test_codex_primary_follows_the_provider_configuration(self) -> None:
        self.assertTrue(steps.codex_primary(None))  # No file: only Codex is enabled.
        for choices, expected in (
            (("on", "off", "-", "off"), True),
            (("off", "on", "claude-opus-5-5", "on"), True),
            (("on", "on", "claude-opus-5-5", "off"), True),
            (("off", "on", "claude-opus-5-5", "off"), False),
        ):
            with self.subTest(choices=choices):
                self.assertEqual(expected, steps.codex_primary(steps.provider_routing(*choices).encode()))
        self.assertFalse(steps.codex_primary(b"{"))



class RegistrationOutcomeTests(unittest.TestCase):
    """W6.5 and W6.2: what the collector step says after learning-register, one fixed sentence each."""

    def lines(self, reply, moved=None):
        return " ".join(steps.registration_outcome(reply, moved)[1])

    def test_each_failure_says_how_far_it_got_and_never_the_misleading_sentence(self) -> None:
        for reason, said, never in (
            ("ledger_upgrade_failed", "Your records were not upgraded: they are as the earlier version left them.",
             "were upgraded, but"),
            ("registration_incomplete_after_upgrade", "Your records were upgraded, but registering this project did not",
             "as the earlier version left them"),
            ("collector_not_started_after_registration", "This project is registered, but the collector",
             "did not finish"),
            ("ledger_upgrade_unknown", "could not be read afterwards. Delete nothing", "as the earlier version left them"),
        ):
            with self.subTest(reason=reason):
                reply = {"ready": False, "reason_code": reason, "ledger_copy": "/kept/old ledger",
                         "state_moved": "/somewhere/old copy"}
                ok, lines = steps.registration_outcome(reply, None)
                text = " ".join(lines)
                self.assertFalse(ok)
                self.assertIn("Collector registration failed", text)
                self.assertIn(said, text)
                self.assertNotIn(never, text)
                self.assertIn("A copy of the records as they were before the upgrade is at /kept/old ledger", text)
                self.assertIn("stay there; the old copy is at /somewhere/old copy", text)

    def test_a_failed_upgrade_names_its_cause_without_control_characters(self) -> None:
        text = self.lines({"ready": False, "reason_code": "ledger_upgrade_failed", "cause": "disk\x1b[2J full"})
        self.assertIn("What stopped it: disk[2J full.", text)
        self.assertNotIn("\x1b", text)

    def test_success_names_the_kept_copy_and_the_move_without_control_characters(self) -> None:
        ok, lines = steps.registration_outcome({"ready": True, "ledger_copy": "/kept/old ledger"}, "/old\x7f copy")
        text = " ".join(lines)
        self.assertTrue(ok)
        self.assertIn("Upgraded your orchestrator records. A copy of them as they were is at /kept/old ledger", text)
        self.assertIn("Moved your orchestrator records once to a protected folder", text)
        self.assertIn("/old copy", text)
        self.assertNotIn("\x7f", text)

    def test_no_move_no_records_sentence(self) -> None:
        self.assertNotIn("Moved your orchestrator records", self.lines({"ready": True}))



class ProjectChoiceTests(unittest.TestCase):
    """W3.2 and W3.4: which projects are offered, how the list is kept, and what a bad path says."""

    def test_projects_set_up_before_are_offered_and_this_folder_once(self) -> None:
        installed = {"/p/first", "/p/second"}
        lines = ["/p/first", "/p/second", "/p/gone", "relative/is-skipped", "/p/bad\x1bname"]
        paths, items = steps.project_choices(lines, "/p/second", "/orchestrator",
                                             installed.__contains__, lambda path: True)
        self.assertEqual(["/p/first", "/p/second"], paths)
        self.assertEqual(["/p/first", "/p/second", "Another folder...", "Quit"], items)  # This folder listed already.
        paths, items = steps.project_choices(["/p/first"], "/p/other", "/orchestrator",
                                             installed.__contains__, lambda path: True)
        self.assertEqual(["/p/first", "/p/other"], paths)
        self.assertEqual("/p/other (this folder)", items[1])
        self.assertEqual(([], []), steps.project_choices([], "/p/other", "/orchestrator",
                                                         installed.__contains__, lambda path: True))

    def test_the_orchestrator_and_a_folder_outside_git_are_never_offered_as_this_folder(self) -> None:
        installed = {"/p/first"}
        for default, is_git in (("/orchestrator", True), ("/p/plain", False)):
            with self.subTest(default=default):
                paths, _ = steps.project_choices(["/p/first"], default, "/orchestrator",
                                                 installed.__contains__, lambda path, git=is_git: git)
                self.assertEqual(["/p/first"], paths)

    def test_a_chosen_project_goes_first_and_the_list_keeps_ten(self) -> None:
        lines = [f"/p/{n}" for n in range(12)]
        kept = steps.remembered(lines, "/p/5")
        self.assertEqual("/p/5", kept[0])
        self.assertEqual(10, len(kept))
        self.assertEqual(1, kept.count("/p/5"))
        self.assertIsNone(steps.remembered(lines, "relative"))
        self.assertIsNone(steps.remembered(lines, "/p/bad\x01"))

    def test_a_path_that_cannot_be_opened_is_reported_exactly(self) -> None:
        lines = steps.cannot_open("/Code/proj", "/Code/proj", "/Users/<you>/Code/proj")
        self.assertEqual("Cannot open: [/Code/proj]", lines[0])
        self.assertNotIn("You typed", " ".join(lines))
        self.assertIn("Type the full path, for example /Users/<you>/projects/app. Tab completes paths.", lines)
        self.assertIn("That path does not exist, but this one does: /Users/<you>/Code/proj", lines)
        lines = steps.cannot_open("/Users/<you>/Code/nowhere", "~/Code/nowhere", None)
        self.assertIn("You typed: [~/Code/nowhere]", lines)
        self.assertFalse(any("but this one does" in line for line in lines))



class FailureSummaryTests(unittest.TestCase):
    """W10.4 and W5.5: the summary and the installer's refusals hold fixed text only, never output."""

    def test_a_failure_line_holds_the_step_label_exit_code_and_hint(self) -> None:
        self.assertEqual("step 4 (Install): The install failed | exit code 2 | Press ? to see what ran",
                         steps.failure_line(4, "Install", "The install failed", 2, "Press ? to see what ran"))
        self.assertEqual("COCOLO setup: what failed\nstep 4 (Install): x | exit code 2 | y\n",
                         steps.summary_text(["step 4 (Install): x | exit code 2 | y"]))

    def test_a_known_installer_refusal_becomes_its_fixed_message_and_never_echoes(self) -> None:
        secret = "SECRET-TOKEN user@example.invalid"
        for raw, expected in (
            (f"{secret}: Managed paths have uncommitted changes", "Managed paths (.codex, .agents, .claude)"),
            (f"error {secret}: rebase in progress", "A Git operation (merge, rebase, cherry-pick or similar)"),
            (f"{secret} holds files the orchestrator does not install", "holds files the orchestrator does not install"),
            (f"{secret}: not a usable git checkout", "The path is not the top folder of a Git working tree."),
            (f"{secret}: target differs", "collides with a managed file"),
        ):
            with self.subTest(expected=expected):
                message, known = steps.installer_refusal(raw, "python3 install.py install --target /p")
                self.assertTrue(known)
                self.assertIn(expected, message)
                self.assertNotIn("SECRET", message)
                self.assertNotIn("user@example.invalid", message)

    def test_an_unknown_refusal_points_to_the_manual_command_and_never_echoes(self) -> None:
        message, known = steps.installer_refusal("SECRET-TOKEN something new", "python3 install.py install --target /p")
        self.assertFalse(known)
        self.assertEqual("The installer refused: run it manually to see why:", message)

    def test_a_summary_destination_with_a_control_character_is_refused(self) -> None:
        self.assertIsNotNone(steps.summary_destination_refusal("/x/sum\x01mary.txt"))
        self.assertIsNone(steps.summary_destination_refusal("/x/summary.txt"))


if __name__ == "__main__":
    unittest.main()
