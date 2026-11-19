"""S5: the Python wizard's file effects (DESIGN-0.4.0-S5-wizard.md W4.4, W4.5, W4.6, W8.1).

Ported from setup.sh's helper and its routing writer. Files only: no process, socket or sleep.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import effects  # noqa: E402


class SafeReadTests(unittest.TestCase):
    """The profile and routing are read without following a symlink or opening a FIFO."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(os.path.realpath(directory.name))

    def test_a_regular_owned_file_is_read_up_to_one_byte_past_the_limit(self) -> None:
        path = self.base / "orchestrator.config.toml"
        path.write_bytes(b"x" * 70000)
        self.assertEqual(effects.PROFILE_READ_LIMIT + 1, len(effects.read_owned_file(path)))
        path.write_bytes(b"network_access = true\n")
        self.assertEqual(b"network_access = true\n", effects.read_owned_file(path))

    def test_a_symlinked_file_or_folder_is_never_followed(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (real / "orchestrator.config.toml").write_text("[sandbox_workspace_write]\nnetwork_access = true\n")
        (self.base / "link.toml").symlink_to(real / "orchestrator.config.toml")
        self.assertIsNone(effects.read_owned_file(self.base / "link.toml"))
        (self.base / "home").symlink_to(real)
        self.assertIsNone(effects.read_owned_file(self.base / "home" / "orchestrator.config.toml"))
        (self.base / "outer").mkdir()
        (self.base / "outer" / "inner").symlink_to(real)
        self.assertIsNone(effects.read_owned_file(self.base / "outer" / "inner" / "orchestrator.config.toml"))
        self.assertIsNotNone(effects.read_owned_file(real / "orchestrator.config.toml"))

    def test_a_fifo_is_never_opened_and_never_hangs(self) -> None:
        fifo = self.base / "orchestrator.config.toml"
        os.mkfifo(fifo)
        started = time.monotonic()
        self.assertIsNone(effects.read_owned_file(fifo))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_missing_file_reads_as_none(self) -> None:
        self.assertIsNone(effects.read_owned_file(self.base / "absent.toml"))

    def test_the_routing_file_is_absent_present_or_refused(self) -> None:
        """W4.5: absent means only Codex is enabled; a symlink or FIFO is refused and never opened."""
        path = self.base / "provider-routing.json"
        self.assertEqual(("absent", None), effects.read_routing(path))
        path.write_text("{}\n")
        self.assertEqual(("present", b"{}\n"), effects.read_routing(path))
        path.unlink()
        path.symlink_to(self.base / "elsewhere.json")
        self.assertEqual(("refused", None), effects.read_routing(path))
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(("refused", None), effects.read_routing(path))


class RoutingWriteTests(unittest.TestCase):
    """W4.4: written atomically, never through a symlink, replacing an earlier regular file."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(os.path.realpath(directory.name))

    def test_routing_is_written_atomically_without_leftovers(self) -> None:
        target = self.base / ".codex" / "orchestration"
        target.mkdir(parents=True)
        effects.write_project_routing(self.base, '{"first": 1}\n')
        self.assertEqual(["provider-routing.json"], [p.name for p in target.iterdir()])
        self.assertEqual('{"first": 1}\n', (target / "provider-routing.json").read_text())
        self.assertEqual(0o644, (target / "provider-routing.json").stat().st_mode & 0o777)
        effects.write_project_routing(self.base, '{"second": 2}\n')
        self.assertEqual('{"second": 2}\n', (target / "provider-routing.json").read_text())
        self.assertEqual(["provider-routing.json"], [p.name for p in target.iterdir()])

    def test_symlinked_routing_destination_is_refused(self) -> None:
        project = self.base / "project"
        (project / ".codex" / "orchestration").mkdir(parents=True)
        victim = self.base / "victim"
        victim.write_text("precious\n")
        link = project / ".codex" / "orchestration" / "provider-routing.json"
        link.symlink_to(victim)
        with self.assertRaises(effects.EffectRefused):
            effects.write_project_routing(project, "{}\n")
        self.assertEqual("precious\n", victim.read_text())
        self.assertTrue(link.is_symlink())
        other = self.base / "other"
        (other / "orchestration").mkdir(parents=True)
        linked = self.base / "linked"
        linked.mkdir()
        (linked / ".codex").symlink_to(other)
        with self.assertRaises(effects.EffectRefused):
            effects.write_project_routing(linked, "{}\n")
        self.assertEqual([], list((other / "orchestration").iterdir()))



class OwnerToolTests(unittest.TestCase):
    """W1.6-W1.8: the owner checks come from this clone's attest.py, loaded by path."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(os.path.realpath(directory.name))
        attest = effects.attest()
        for patcher in (mock.patch.object(attest, "SCRATCH_BASE", str(self.base)),
                        mock.patch.object(attest, "temp_roots", lambda: [])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_attest_is_this_clones_own_file(self) -> None:
        self.assertEqual(ROOT / "scripts" / "attest.py", Path(effects.attest().__file__))

    def test_the_working_folder_is_private_and_removed_at_the_end(self) -> None:
        """W1.7."""
        folder = effects.make_scratch("wizard-")
        self.assertTrue(folder.is_dir())
        self.assertEqual(0o700, folder.stat().st_mode & 0o777)
        self.assertTrue(str(folder).startswith(str(self.base)))
        (folder / "work").write_text("x")
        self.assertTrue(effects.remove_scratch(folder))
        self.assertFalse(folder.exists())

    def test_a_working_folder_that_cannot_be_made_is_a_fixed_refusal(self) -> None:
        with mock.patch.object(effects.attest(), "SCRATCH_BASE", "relative"):
            with self.assertRaises(effects.EffectRefused):
                effects.make_scratch("wizard-")

    def test_a_project_without_this_release_installed_is_refused(self) -> None:
        """W1.6: owner commands run only once the installed copy is this release byte for byte."""
        project = self.base / "project"
        project.mkdir()
        problem = effects.runtime_problem(project)
        self.assertIn("not an exact copy of this setup's version", problem)



class SessionScanTests(unittest.TestCase):
    """W2.1: the runtime's Codex session scan, in-process from this clone, never Codex's files directly."""

    def test_an_empty_codex_home_has_no_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            for name in ("home", "home/.codex", "home/.local", "home/.local/state"):
                (base / name).mkdir(mode=0o700)
            environ = {"HOME": str(base / "home"), "CODEX_HOME": str(base / "home" / ".codex"),
                       "XDG_STATE_HOME": str(base / "home" / ".local" / "state")}
            with mock.patch.dict(os.environ, environ):
                self.assertEqual({"ready": True, "warnings": []}, effects.session_scan(ROOT))
            self.assertEqual([".codex", ".local"], sorted(p.name for p in (base / "home").iterdir()))

    def test_a_scan_that_fails_is_none(self) -> None:
        with mock.patch.object(effects, "_scan_modules", side_effect=RuntimeError("broken")):
            self.assertIsNone(effects.session_scan(ROOT))


if __name__ == "__main__":
    unittest.main()
