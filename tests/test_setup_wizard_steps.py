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


if __name__ == "__main__":
    unittest.main()
