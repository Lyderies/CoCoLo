"""Client path validation has no database ownership or bootstrap side effects."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import collector_paths
from codex_agents.collector_paths import CollectorPaths, StateError, UNMOVED_STATE


class CollectorPathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "client-state"
        self.root.mkdir(mode=0o700)
        self.key = self.root / "digest.key"
        self.key.write_bytes(b"f" * 32)
        self.key.chmod(0o600)

    def test_existing_paths_need_no_database_and_create_nothing(self):
        before = sorted(self.root.iterdir())
        paths = CollectorPaths(self.root)
        self.assertEqual(paths.root, self.root)
        self.assertEqual(paths.socket, self.root / "collector.sock")
        self.assertEqual(paths.runtime_lock, self.root / "collector-runtime.lock")
        self.assertEqual(paths.collector_authorization, self.root / "collector-authorization.json")
        self.assertEqual(paths.client_health, self.root / "collector-client-health.json")
        self.assertEqual(paths.client_health_lock, self.root / "collector-client-health.lock")
        self.assertFalse(hasattr(paths, "database"))
        self.assertEqual(paths.digest_key(), b"f" * 32)
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_missing_optional_files_are_not_read_eagerly(self):
        self.key.unlink()
        self.assertEqual(CollectorPaths(self.root).root, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_relative_or_non_directory_roots_refuse_without_creation(self):
        for root in (Path("relative"), self.base / "missing", self.key):
            with self.subTest(root=root), self.assertRaises((StateError, OSError)):
                CollectorPaths(root)
        self.assertFalse((self.base / "missing").exists())

    def test_state_root_mode_and_owner_are_checked(self):
        self.root.chmod(0o755)
        with self.assertRaises(StateError):
            CollectorPaths(self.root)
        self.root.chmod(0o700)
        with mock.patch.object(collector_paths.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(StateError):
                CollectorPaths(self.root)

    def test_symlink_and_git_boundaries_refuse(self):
        link = self.base / "linked-state"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(StateError):
            CollectorPaths(link)
        for kind in ("directory", "file"):
            marker = self.base / ".git"
            if kind == "directory":
                marker.mkdir()
            else:
                marker.write_text("gitdir: irrelevant\n")
            try:
                with self.assertRaisesRegex(StateError, "Git working tree"):
                    CollectorPaths(self.root)
            finally:
                marker.rmdir() if kind == "directory" else marker.unlink()

    def test_default_and_legacy_only_paths_preserve_existing_refusal(self):
        legacy = self.base / "legacy-state"
        legacy.mkdir()
        missing = self.base / "missing-new-state"
        with (
            mock.patch.object(collector_paths, "default_state_root", return_value=missing),
            mock.patch("codex_agents.host_paths.legacy_state_root", return_value=legacy),
        ):
            with self.assertRaises(StateError) as caught:
                CollectorPaths()
            self.assertEqual(str(caught.exception), UNMOVED_STATE)
            self.assertFalse(missing.exists())
        with (
            mock.patch.object(collector_paths, "default_state_root", return_value=self.root),
            mock.patch("codex_agents.host_paths.legacy_state_root", return_value=legacy),
        ):
            self.assertEqual(CollectorPaths().root, self.root)

    def test_wrong_key_lengths_refuse(self):
        paths = CollectorPaths(self.root)
        for size in (0, 31, 33, 1_048_576):
            with self.subTest(size=size):
                self.key.write_bytes(b"x" * size)
                with self.assertRaises(StateError):
                    paths.digest_key()

    def test_key_modes_symlinks_hardlinks_and_special_files_refuse(self):
        paths = CollectorPaths(self.root)
        self.key.chmod(0o644)
        with self.assertRaises(StateError):
            paths.digest_key()
        self.key.chmod(0o600)
        other = self.base / "second-key-link"
        os.link(self.key, other)
        with self.assertRaises(StateError):
            paths.digest_key()
        other.unlink()
        self.key.rename(other)
        self.key.symlink_to(other)
        with self.assertRaises(StateError):
            paths.digest_key()
        self.key.unlink()
        self.key.mkdir(mode=0o600)
        with self.assertRaises(StateError):
            paths.digest_key()
        self.key.rmdir()
        os.mkfifo(self.key, 0o600)
        with self.assertRaises(StateError):
            paths.digest_key()

    def test_key_substitution_between_check_and_open_refuses_and_closes(self):
        paths = CollectorPaths(self.root)
        replacement = self.base / "replacement"
        replacement.write_bytes(b"z" * 32)
        replacement.chmod(0o600)
        original_open = os.open
        descriptors = []
        def substitute(path, flags, *args, **kwargs):
            if Path(path) == self.key:
                replacement.replace(self.key)
            descriptor = original_open(path, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with mock.patch.object(collector_paths.os, "open", side_effect=substitute):
            with self.assertRaises(StateError):
                paths.digest_key()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_parent_replaced_by_symlink_after_construction_refuses(self):
        paths = CollectorPaths(self.root)
        saved = self.base / "original-state"
        self.root.rename(saved)
        self.root.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(StateError):
            paths.digest_key()

    def test_open_is_nonblocking_nofollow_and_descriptor_closes(self):
        paths = CollectorPaths(self.root)
        original_open = os.open
        calls = []
        def tracked(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            calls.append((descriptor, flags))
            return descriptor
        with mock.patch.object(collector_paths.os, "open", side_effect=tracked):
            self.assertEqual(paths.digest_key(), b"f" * 32)
        self.assertEqual(len(calls), 1)
        descriptor, flags = calls[0]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NONBLOCK)
        with self.assertRaises(OSError):
            os.fstat(descriptor)


class SharedPathCompatibilityTests(unittest.TestCase):
    def test_missing_owner_signer_still_reports_not_configured(self):
        from codex_agents.learning_store import LearningState
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state = LearningState(Path(directory) / "state")
            state.allowed_signers_path.unlink()
            self.assertEqual(state.owner_key_report()["state"], "not_configured")

    def test_existing_database_missing_authority_keeps_recovery_reason(self):
        from codex_agents.learning_store import LearningLedger, LearningState
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state = LearningState(Path(directory) / "state")
            ledger = LearningLedger(state)
            ledger.close()
            state.key_path.unlink()
            with self.assertRaisesRegex(StateError, "existing ledger authority file is unavailable"):
                LearningState(state.root, create=False)

    def test_missing_canonical_project_keeps_os_error(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            with self.assertRaises(FileNotFoundError):
                collector_paths._canonical_project_root(Path(directory) / "missing")


if __name__ == "__main__":
    unittest.main()
