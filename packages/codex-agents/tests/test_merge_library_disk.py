"""ML-19/TF-18: volatile contents and namespaces, exercised through the real IOCall seam."""
import stat
import unittest
from pathlib import Path
from unittest import mock

from tests.merge_library_support import Fixture
from codex_agents import merge_library as ml


class DurableDiskTests(unittest.TestCase):
    __requirements__ = ('ML-19', 'TF-18')
    __why_not_scenario__ = 'Model power loss distinguishes persisted content and namespace edges that real SIGKILL cannot erase from the kernel cache.'

    def setUp(self):
        from tests.merge_library_disk import MemoryDisk
        self.disk_type = MemoryDisk
        self.disk = MemoryDisk.empty(Path('/model'))

    def test_unflushed_write_is_lost_but_flushed_write_survives(self):
        d = self.disk
        d.write('/model/a', b'base', 0o640)
        d.flush('/model/a')
        d.flush('/model')
        d.write('/model/a', b'volatile', 0o640)
        d.crash()
        self.assertEqual(b'base', d.read('/model/a'))
        d.write('/model/a', b'durable', 0o640)
        d.flush('/model/a')
        d.crash()
        self.assertEqual((stat.S_IFREG, 0o640, b'durable'), d.snapshot('/model')['a'])

    def test_file_flush_does_not_persist_create_and_parent_flush_does_not_persist_bytes(self):
        d = self.disk
        d.write('/model/only-file', b'content', 0o644)
        d.flush('/model/only-file')
        d.crash()
        self.assertNotIn('only-file', d.snapshot('/model'))
        d.write('/model/only-parent', b'content', 0o644)
        d.flush('/model')
        d.crash()
        self.assertNotEqual((stat.S_IFREG, 0o644, b'content'), d.snapshot('/model').get('only-parent'))
        d.write('/model/complete', b'content', 0o644)
        d.flush('/model/complete')
        d.flush('/model')
        d.crash()
        self.assertEqual(b'content', d.read('/model/complete'))

    def test_cross_directory_rename_requires_both_parent_flushes(self):
        d = self.disk
        for name in ('left', 'right'):
            d.mkdir('/model/' + name)
            d.flush('/model/' + name)
        d.flush('/model')
        d.write('/model/left/a', b'bytes', 0o644)
        d.flush('/model/left/a')
        d.flush('/model/left')
        d.rename('/model/left/a', '/model/right/a')
        d.flush('/model/right')
        d.crash()
        self.assertIn('left/a', d.snapshot('/model'))
        self.assertIn('right/a', d.snapshot('/model'))
        d.unlink('/model/left/a')
        d.flush('/model/left')
        d.crash()
        self.assertNotIn('left/a', d.snapshot('/model'))
        self.assertEqual(b'bytes', d.read('/model/right/a'))

    def test_unflushed_removal_is_lost_and_directory_removal_requires_parent_flush(self):
        d = self.disk
        d.mkdir('/model/folder')
        d.flush('/model/folder')
        d.flush('/model')
        d.rmdir('/model/folder')
        d.crash()
        self.assertIn('folder', d.snapshot('/model'))
        d.rmdir('/model/folder')
        d.flush('/model')
        d.crash()
        self.assertNotIn('folder', d.snapshot('/model'))

    def test_existing_store_io_runs_against_memory_without_creating_real_files(self):
        f = Fixture()
        self.addCleanup(f.close)
        disk = self.disk_type(f.root)
        store = ml._Store(f.project, f.private, disk)
        self.addCleanup(store.close)
        path = f.private / 'memory-only'
        store.write(path, ml.Content(b'virtual complete bytes', 0o640))
        store.flush(f.private)
        self.assertFalse(path.exists())
        self.assertEqual(ml.Content(b'virtual complete bytes', 0o640), store.read(path))
        disk.crash()
        self.assertEqual(b'virtual complete bytes', disk.read(path))

    def test_real_land_uses_memory_namespace_history_and_full_modes(self):
        f = Fixture()
        self.addCleanup(f.close)
        batch = f.batch({'a.txt': b'model child', 'new/file': b'new'})
        disk = self.disk_type(f.root)
        before = dict(f.before)
        report = f.land(batch, hook=disk)
        self.assertEqual('landed', report.state)
        self.assertEqual(f.expected(batch), disk.snapshot(f.project, control=False))
        self.assertEqual(before, __import__('tests.merge_library_support', fromlist=['tree']).tree(f.project))
        rows = ml.history(f.project, f.private, hook=disk)
        self.assertEqual('landed', rows[-1].state)
        disk.crash()
        self.assertEqual(f.expected(batch), disk.snapshot(f.project, control=False))

    def test_skipping_flush_before_A_is_caught_after_model_power_loss(self):
        from tests.merge_library_disk import ModelCrash
        def committed_staging_survives():
            f = Fixture()
            self.addCleanup(f.close)
            batch = f.batch({'a.txt': b'durable child'})
            disk = self.disk_type(f.root)
            def crash_after_A(call):
                answer = disk(call)
                if call.operation == 'sqlite_commit' and call.when == 'after' and call.detail.get('commit') == 'A':
                    disk.crash()
                    raise ModelCrash()
                return answer
            with self.assertRaises(ModelCrash):
                f.land(batch, hook=crash_after_A)
            staged = '.cocolo/staging/child-1/e00000'
            self.assertEqual((stat.S_IFREG, 0o644, b'durable child'), disk.snapshot(f.project).get(staged))
        committed_staging_survives()
        original = ml._Store.flush_all
        def missing_A_flush(store):
            if store.phase != 'history':
                return original(store)
        with mock.patch.object(ml._Store, 'flush_all', missing_A_flush):
            with self.assertRaises(AssertionError):
                committed_staging_survives()


if __name__ == '__main__':
    unittest.main()
