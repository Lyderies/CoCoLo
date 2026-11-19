"""S5: the Python wizard's Git effects (DESIGN-0.4.0-S5-wizard.md W1.4, W1.8, W5.1-W5.5).

Every Git call goes through this clone's installer in-process (install.project_changes,
install.repository_helpers, install.commit_paths): plumbing only, no project hook. Runs real Git
in temporary repositories, so it is an integration test.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import effects  # noqa: E402

IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


class GitEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(os.path.realpath(directory.name)) / "project"
        self.project.mkdir()
        for name, value in IDENTITY.items():
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, v=previous: os.environ.pop(n) if v is None else os.environ.__setitem__(n, v))
        self.git("init", "-q", "-b", "main")
        (self.project / ".codex").mkdir()
        (self.project / ".codex" / "kept").write_text("kept\n")
        (self.project / ".codex" / "old").write_text("old\n")
        (self.project / "README").write_text("readme\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "start")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.project), *args], check=True, capture_output=True,
                              text=True).stdout

    def test_changes_name_only_changed_candidates_and_both_names_of_a_rename(self) -> None:
        self.git("mv", ".codex/old", ".codex/new")
        (self.project / ".codex" / "added").write_text("added\n")
        (self.project / "README").write_text("changed\n")
        found = effects.git_changes(self.project, [".codex"])
        self.assertEqual([".codex/added", ".codex/new", ".codex/old"], found)
        self.assertEqual([], effects.git_changes(self.project, [".claude"]))

    def test_paths_are_literal(self) -> None:
        """W5.2: `.codex/k*` names that file only, never a wildcard matching `.codex/kept`."""
        (self.project / ".codex" / "kept").write_text("changed\n")
        self.assertEqual([], effects.git_changes(self.project, [".codex/k*"]))

    def test_a_helper_in_the_project_settings_stops_git(self) -> None:
        """W1.4: with a helper named, the wizard runs no Git that could start it."""
        self.assertEqual([], effects.git_helpers(self.project))
        self.git("config", "core.pager", "touch planted")
        self.assertEqual(["core.pager"], effects.git_helpers(self.project))
        (self.project / ".codex" / "kept").write_text("changed\n")
        self.assertIsNone(effects.git_changes(self.project, [".codex"]))
        state, reason = effects.git_commit(self.project, "Set up", "detail", [".codex/kept"])
        self.assertEqual("failed", state)
        self.assertIn("name programs Git would run", reason)
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").strip())

    def test_commit_takes_exactly_the_paths_given_and_runs_no_hook(self) -> None:
        """W5.3: plumbing only: a pre-commit hook is never started."""
        marker = self.project.parent / "hook-ran"
        hook = self.project / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        (self.project / ".codex" / "kept").write_text("changed\n")
        (self.project / ".codex" / "added").write_text("added\n")
        self.assertEqual(("committed", ""), effects.git_commit(self.project, "Set up", "detail", [".codex/kept"]))
        self.assertFalse(marker.exists())
        self.assertEqual(".codex/kept\n", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(["?? .codex/added"], self.git("status", "--porcelain").splitlines())

    def test_nothing_to_commit_commits_nothing(self) -> None:
        """W5.4."""
        # W5.4: nothing changed is not a failure: the state is "nothing" ("nothing to commit").
        self.assertEqual(("nothing", ""), effects.git_commit(self.project, "Set up", "detail", [".codex/kept"]))
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").strip())

    def test_the_installer_is_this_clones_own_file(self) -> None:
        """W1.8: loaded by path from this clone, never found on sys.path."""
        self.assertEqual(ROOT / "scripts" / "install.py", Path(effects.installer().__file__))



class GitQueryTests(GitEffectTests):
    """W5.1 and W5.7: which installed files .gitignore hides, and which candidates may be committed."""

    test_changes_name_only_changed_candidates_and_both_names_of_a_rename = None
    test_paths_are_literal = None
    test_a_helper_in_the_project_settings_stops_git = None
    test_commit_takes_exactly_the_paths_given_and_runs_no_hook = None
    test_nothing_to_commit_commits_nothing = None
    test_the_installer_is_this_clones_own_file = None

    def test_ignored_installed_files_are_named(self) -> None:
        (self.project / ".gitignore").write_text(".codex/hidden\n")
        (self.project / ".codex" / "hidden").write_text("x\n")
        (self.project / ".codex" / "shown").write_text("x\n")
        self.assertEqual([".codex/hidden"],
                         effects.git_ignored(self.project, [".codex/hidden", ".codex/shown", ".codex/kept"]))
        self.assertEqual([], effects.git_ignored(self.project, []))

    def test_commit_candidates_are_what_exists_or_is_tracked_from_the_reply(self) -> None:
        """setup.sh's `pj paths`: the plan's paths, configuration written, Claude's changed paths, the
        manifest and the routing, kept when present or tracked (a removed tracked file still counts)."""
        (self.project / ".codex" / "kept").unlink()  # Tracked, now removed: still a candidate.
        reply = {"codex": {"project_configuration": {".codex/config-new": "create", ".codex/config-old": "preserve"}},
                 "claude": {"changed_paths": [".claude/agents/a.md"]},
                 "claude_settings": {"changed_paths": [".claude/settings.json"]}}
        (self.project / ".codex" / "config-new").write_text("x\n")
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.json").write_text("{}\n")
        found = effects.commit_candidates(self.project, reply, [".codex/kept", ".codex/never-existed"])
        self.assertEqual([".claude/settings.json", ".codex/config-new", ".codex/kept"], found)

    def test_an_unsafe_candidate_name_refuses_the_whole_list(self) -> None:
        with self.assertRaises(effects.EffectRefused):
            effects.commit_candidates(self.project, {"claude": {"changed_paths": ["../outside"]}}, [])
        with self.assertRaises(effects.EffectRefused):
            effects.commit_candidates(self.project, {}, [".codex/bad\nname"])



class TemporaryFolderTests(unittest.TestCase):
    """Step 8: the runtime's temporary roots start getconf on macOS, so this runs as integration."""

    def test_a_project_in_a_temporary_folder_is_found_with_the_runtimes_own_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(effects.project_in_temp(directory))
        self.assertFalse(effects.project_in_temp(str(ROOT)) and not str(ROOT).startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
