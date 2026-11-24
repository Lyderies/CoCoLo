"""s2fix round 3 (decision 185.3): removing a deleted project's history store never follows a link and never lets a
second writer take a replacement lock."""
import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import TEMP_ROOT

from codex_agents.collector_paths import CollectorPaths
from codex_agents.project_service import history_directory, remove_project_history

PROJECT = 'prj_' + 'a' * 32


class StoreRemovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'state'
        self.root.mkdir(mode=0o700)
        self.state = CollectorPaths.__new__(CollectorPaths)
        self.state.root = self.root

    def store(self):
        directory = history_directory(self.state, PROJECT)
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'history.sqlite3').write_bytes(b'x')
        (directory / 'history.lock').write_bytes(b'')
        return directory

    def test_a_symlinked_projects_directory_is_refused_and_nothing_behind_it_is_touched(self):
        elsewhere = self.root.parent / 'elsewhere'
        (elsewhere / PROJECT).mkdir(parents=True)
        (elsewhere / PROJECT / 'history.sqlite3').write_bytes(b'keep')
        os.symlink(elsewhere, self.root / 'projects')
        self.assertFalse(remove_project_history(self.state, PROJECT))
        self.assertTrue((elsewhere / PROJECT / 'history.sqlite3').exists())

    def test_a_symlinked_project_directory_is_refused(self):
        elsewhere = self.root.parent / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'history.sqlite3').write_bytes(b'keep')
        (self.root / 'projects').mkdir(mode=0o700)
        os.symlink(elsewhere, self.root / 'projects' / PROJECT)
        self.assertFalse(remove_project_history(self.state, PROJECT))
        self.assertTrue((elsewhere / 'history.sqlite3').exists())

    def test_a_directory_of_another_owner_is_refused(self):
        from unittest import mock
        self.store()
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            self.assertFalse(remove_project_history(self.state, PROJECT))
        self.assertTrue((history_directory(self.state, PROJECT) / 'history.sqlite3').exists())

    def test_the_lock_file_is_never_unlinked_so_no_replacement_lock_can_be_taken(self):
        directory = self.store()
        self.assertTrue(remove_project_history(self.state, PROJECT))
        self.assertFalse((directory / 'history.sqlite3').exists())
        self.assertTrue((directory / 'history.lock').exists())
        # A writer arriving later and one holding the original lock exclude each other: it is the same file.
        first = os.open(directory / 'history.lock', os.O_RDWR)
        second = os.open(directory / 'history.lock', os.O_RDWR)
        try:
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(first)
            os.close(second)


if __name__ == '__main__':
    unittest.main()
