"""An update stops with the installer's own reason; the Done screen never names an empty project.

No process, socket or sleep: the wizard's steps run against a stand-in terminal.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ORCHESTRATION))

from setup_wizard import flow, steps  # noqa: E402
from setup_wizard.terminal import Stop  # noqa: E402


class Screen:
    """Records what the wizard puts on the screen; everything else is a no-op."""

    names = [f"step {n}" for n in range(1, 10)]

    def __init__(self) -> None:
        self.stat: dict = {}
        self.step = 0
        self.step_log: list = []
        self.no_pauses = False
        self.lines: list[str] = []
        self.commands: list[str] = []

    def list_add(self, kind, text): self.lines.append(text)
    def list_sub(self, text): self.lines.append(text)
    def list_plain(self, text): self.lines.append(text)
    def list_heading(self, text): self.lines.append(text)
    def list_cmd(self, text): self.commands.append(text)
    def body(self, text): self.lines.append(text)
    def menu(self, *args): raise AssertionError("an update shows no menu")
    def __getattr__(self, name): return lambda *a, **k: None


def make_wizard(update: bool = True) -> flow.Wizard:
    return flow.Wizard(Screen(), start="", python="/usr/bin/python3", owner_path="/", bash_version="5",
                       update=update)


class RefusedPreviewTests(unittest.TestCase):
    def test_an_update_whose_preview_is_refused_stops_with_the_installers_reason(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = os.path.realpath(base)
            manifest = Path(project, ".codex", "orchestration", "install-manifest.json")
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            wizard = make_wizard()
            wizard.mode = "full"

            def refused(*args, **kwargs):
                wizard.last_reply = {"claude": {"error": "untrusted_manifest"}, "codex": {}, "ok": False}
                return 2

            with mock.patch.object(wizard, "managed_problem", return_value=""), \
                    mock.patch.object(wizard, "basic_problem", return_value=""), \
                    mock.patch.object(wizard, "remember_project"), \
                    mock.patch.object(wizard, "owner_key_notice"), \
                    mock.patch.object(wizard, "prepare_project_inputs"), \
                    mock.patch.object(wizard, "run_installer", side_effect=refused), \
                    self.assertRaises(Stop) as caught:
                wizard.step_project(project)
        self.assertNotIn("the project cannot be used", caught.exception.message)
        self.assertIn("Claude files", caught.exception.message)
        self.assertIn("untrusted_manifest".replace("_", " "), caught.exception.message.lower())


class DoneScreenTests(unittest.TestCase):
    def summary(self, project: str) -> Screen:
        wizard = make_wizard(update=False)
        wizard.project = project
        wizard.t.step = 2
        with mock.patch.object(effects_module(), "read_owned_file", return_value=None), \
                mock.patch.object(wizard, "codex_profile_path", return_value="/nonexistent"):
            wizard.summary_screen()
        return wizard.t

    def test_no_remove_command_with_an_empty_target(self) -> None:
        screen = self.summary("")
        self.assertEqual([], [c for c in screen.commands if "remove" in c])

    def test_the_remove_command_names_the_project_when_there_is_one(self) -> None:
        screen = self.summary("/some/project")
        (command,) = [c for c in screen.commands if "remove" in c]
        self.assertIn("--target /some/project --apply", command)


def effects_module():
    return flow.effects


if __name__ == "__main__":
    unittest.main()
