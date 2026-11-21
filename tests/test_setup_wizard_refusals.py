"""An update stops with the installer's own reason; the Done screen never names an empty project.

No process, socket or sleep: the wizard's steps run against a stand-in terminal.
"""

import ast
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
sys.path.insert(2, str(ROOT / "packages" / "claude-agents" / "src"))

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


class RefusalWordingTests(unittest.TestCase):
    """Each Claude refusal says plainly what is known, that nothing changed, and what to do next."""

    MANUAL = "python3 install.py install --target /p"

    def message(self, code: str) -> str:
        text, known = steps.installer_refusal(f'{{"claude": {{"error": "{code}"}}}}', self.MANUAL)
        self.assertTrue(known, code)
        return text

    def test_a_catalogue_refusal_blames_this_copy_of_cocolo_not_the_project(self) -> None:
        for code in ("previous_release_not_pinned", "previous_release_path_refused", "invalid_previous_release"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertIn("This copy of CoCoLo", text)
                self.assertIn("Nothing was changed", text)
                self.assertIn("Get a fresh copy of CoCoLo", text)
                self.assertIn("run setup again", text)
                self.assertNotIn("Restore them with git", text)

    def test_a_missing_or_changed_file_is_not_called_hand_edited(self) -> None:
        text = self.message("managed_file_modified")
        self.assertNotIn("by hand", text)
        self.assertIn("changed or missing", text)
        self.assertIn("Nothing was changed", text)
        self.assertIn("git diff -- .claude", text)
        self.assertIn("run setup again", text)

    def test_an_untrusted_manifest_names_what_to_do_without_asserting_a_cause(self) -> None:
        text = self.message("untrusted_manifest")
        self.assertNotIn("older", text)
        self.assertIn("Nothing was changed", text)
        self.assertIn("git diff -- .claude", text)
        self.assertIn("run setup again", text)

    def test_the_advice_never_restores_all_of_claude_and_keeps_a_way_out(self) -> None:
        for code in ("untrusted_manifest", "managed_file_modified"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertNotIn("git restore .claude", text)
                self.assertNotIn("git restore -- .claude ", text)
                self.assertIn("Save any change you want to keep", text)
                self.assertIn("remove the package files", text)
                self.assertIn(".claude/claude-agents", text)

    def test_nothing_was_changed_is_not_said_after_a_rollback_that_failed(self) -> None:
        for code in ("untrusted_manifest", "managed_file_modified", "previous_release_not_pinned"):
            with self.subTest(code=code):
                text, known = steps.installer_refusal(f'{{"claude": {{"error": "{code}"}}}}', self.MANUAL,
                                                      rollback_incomplete=True)
                self.assertTrue(known)
                self.assertNotIn("Nothing was changed", text)
                self.assertIn("could not undo everything", text)
                self.assertIn("git status", text)

    def test_the_wizard_notices_an_incomplete_rollback_in_the_installer_reply(self) -> None:
        wizard = make_wizard()
        wizard.last_reply = {"ok": False, "claude": {"error": "managed_file_modified"},
                             "rollback": [{"package": "claude", "action": None, "ok": False, "note": "x"}]}
        wizard.project = "/p"
        message = wizard.explain_installer("The install failed", self.MANUAL)
        self.assertNotIn("Nothing was changed", message)
        wizard.last_reply = {"ok": False, "claude": {"error": "managed_file_modified"},
                             "rollback": [{"package": "claude", "action": "restore", "ok": True}]}
        self.assertIn("Nothing was changed", wizard.explain_installer("The install failed", self.MANUAL))

    def test_the_removal_advice_names_exact_package_files_never_a_glob_or_a_whole_folder(self) -> None:
        for code in ("untrusted_manifest", "managed_file_modified"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertNotIn("claude_agents_*", text)
                self.assertNotIn(" and .claude/claude-agents,", text)
                for exact in steps.CLAUDE_PACKAGE_FILES:
                    self.assertIn(exact, text)
                self.assertIn("only if it is then empty", text)

    def test_every_refusal_code_of_the_claude_package_has_a_wizard_message(self) -> None:
        source = (ROOT / "packages" / "claude-agents" / "src" / "claude_agents" / "packaging.py").read_text()
        codes = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_fail", "PackageError"):
                codes.update(const.value for arg in node.args for const in ast.walk(arg)
                             if isinstance(const, ast.Constant) and isinstance(const.value, str))
        self.assertGreaterEqual(len(codes), 20, codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                text, known = steps.installer_refusal(f'{{"claude": {{"error": "{code}"}}}}', self.MANUAL)
                self.assertTrue(known, code)
                self.assertNotIn("{manual}", text)

    def test_the_advice_lists_exactly_the_files_the_package_installs(self) -> None:
        from claude_agents import packaging
        self.assertEqual(set(packaging.bundle_paths()) | {packaging.MANIFEST}, set(steps.CLAUDE_PACKAGE_FILES))

    def test_an_installation_pending_refusal_names_the_marker_and_the_lock(self) -> None:
        for code in ("installation_pending", "rollback_pending"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertIn(".claude/claude-agents/install-pending.json", text)
                self.assertIn(".claude/claude-agents/install.lock", text)
                self.assertIn("run setup again", text)

    def test_an_unrecognised_refusal_still_warns_after_an_incomplete_rollback(self) -> None:
        wizard = make_wizard()
        wizard.last_reply = {"ok": False, "claude": {"error": "something nobody mapped"},
                             "rollback": [{"package": "claude", "action": None, "ok": False}]}
        message = wizard.explain_installer("The install failed", self.MANUAL)
        self.assertIn("could not undo everything", message)

    def test_the_installers_own_safety_refusals_are_recognised(self) -> None:
        for text in ("claude_agents_x.md is not a regular file", ".claude is a symlink or not a folder",
                     ".claude/agents/x.md cannot be used safely (unsafe_or_unavailable_path)",
                     "the claude files could not be saved before the update: .claude is a symlink or not a folder"):
            with self.subTest(text=text):
                self.assertTrue(steps.installer_refusal(text.lower(), self.MANUAL)[1])

    def test_a_wrong_call_to_the_package_is_not_blamed_on_a_damaged_copy(self) -> None:
        """Owner fix 5 (r4 LOW: invalid_operation and invalid_apply got the damaged-copy remedy, steps.py:337-340).
        These codes mean setup itself called the package wrongly. Fails on 1d359e3b: 'incomplete or damaged'."""
        for code in ("invalid_operation", "invalid_apply"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertNotIn("incomplete or damaged", text)
                self.assertIn("fault in setup itself", text)
                self.assertIn("Nothing was changed", text)
                self.assertIn("report it", text)

    def test_an_oversized_file_is_not_called_a_symlink(self) -> None:
        """Owner fix 5 (r4 LOW: oversized_file got 'Replace it with a real file or folder', steps.py:345-347). Fails
        on 1d359e3b: it is grouped with the symlink refusals."""
        text = self.message("oversized_file")
        self.assertNotIn("Replace it with a real file or folder", text)
        self.assertIn("larger than", text)
        self.assertIn("git diff -- .claude", text)
        self.assertIn("run setup again", text)

    def test_a_write_failure_does_not_say_it_went_back_only_where_it_could(self) -> None:
        """Owner fix 5 (r4 verifier: write_failed/operation_failed said both 'went back ... where it could' and
        'Nothing was changed'). operation_failed means the package undid all of its own writes. Fails on
        1d359e3b: both claims are in the one message."""
        for code in ("operation_failed", "write_failed"):
            with self.subTest(code=code):
                text = self.message(code)
                self.assertNotIn("where it could", text)
                self.assertIn("put back", text)

    def test_nothing_was_changed_is_never_said_when_a_rollback_note_exists(self) -> None:
        """Owner fix 5 ('never say Nothing was changed when a restore note exists'). A rollback entry with a note
        means something was left or not undone. Fails on 1d359e3b: a noted entry marked ok still gives
        'Nothing was changed'."""
        reply = {"ok": False, "claude": {"error": "managed_file_modified"},
                 "rollback": [{"package": "claude", "action": "restore", "ok": True, "note": "left alone: x"}]}
        self.assertTrue(steps.rollback_incomplete(reply))
        wizard = make_wizard()
        wizard.last_reply = reply
        wizard.project = "/p"
        self.assertNotIn("Nothing was changed", wizard.explain_installer("The install failed", self.MANUAL))

    def test_a_step_without_a_write_report_is_explained_without_claiming_nothing_changed(self) -> None:
        """Owner fix 1 and 5: the installer stops with no_write_report when a package gives no account of its
        writes; the wizard recognises it and says the files were left as they are. Fails on 1d359e3b: the code
        is unknown to the wizard."""
        text = self.message("no_write_report")
        self.assertNotIn("Nothing was changed", text)
        self.assertIn("left", text)
        self.assertIn("git status", text)

    def test_update_required_does_not_claim_the_install_is_older(self) -> None:
        text = self.message("update_required")
        self.assertNotIn("is an older release", text)
        self.assertIn("Update the install", text)


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
