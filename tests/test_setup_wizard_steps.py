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


if __name__ == "__main__":
    unittest.main()
