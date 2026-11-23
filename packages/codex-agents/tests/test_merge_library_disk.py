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
        self.assertNotIn('only-parent', d.snapshot('/model'))
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
        suppressed = []
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
            event = batch.events[0]
            staged = f'.cocolo/staging/{batch.child_id}/e{batch.events.index(event):05d}'
            self.assertEqual((stat.S_IFREG, event.new.mode_bits, event.new.data), disk.snapshot(f.project).get(staged),
                             'commit A lost staged contents: ' + staged)
        committed_staging_survives()
        original = ml._Store.flush_all
        def missing_A_flush(store):
            if store.phase != 'history':
                return original(store)
            suppressed.append(store.phase)
        with mock.patch.object(ml._Store, 'flush_all', missing_A_flush):
            with self.assertRaisesRegex(AssertionError, 'commit A lost staged contents: .*e00000'):
                committed_staging_survives()
        self.assertEqual(['history'], suppressed, 'missing-flush mutation never activated')

    def call(self, operation, path, **detail):
        return self.disk(ml.IOCall(operation, 'before', 'test', str(path), detail=detail)).value

    def test_review_B1_flush_open_inode_after_rename(self):
        import os
        d = self.disk
        d.write('/model/a', b'old'); d.flush('/model/a'); d.flush('/model')
        fd = self.call('open', '/model/a', flags=os.O_RDWR)
        self.call('write', '/model/a', fd=fd, data=b'new')
        d.rename('/model/a', '/model/b'); d.flush('/model')
        self.call('fsync', '/model/a', fd=fd)
        d.crash()
        self.assertEqual(b'new', d.read('/model/b'))

    def test_review_B2_missing_read_is_ENOENT(self):
        with self.assertRaises(FileNotFoundError):
            self.call('read', '/model/missing')

    def test_review_B5_database_identity_survives_rename_not_unlink(self):
        from tests.merge_library_disk import _Connection
        d = self.disk
        db = _Connection(d, '/model/db')
        db.execute('INSERT OR REPLACE INTO rows VALUES (?,?)', (1, b'row'))
        db.commit(); d.flush('/model')
        d.rename('/model/db', '/model/moved'); d.flush('/model'); d.crash()
        moved = _Connection(d, '/model/moved')
        self.assertEqual([(b'row',)], moved.execute('SELECT value FROM rows ORDER BY id').fetchall())
        d.unlink('/model/moved'); d.flush('/model'); d.crash()
        new = _Connection(d, '/model/moved')
        self.assertEqual([], new.execute('SELECT value FROM rows ORDER BY id').fetchall())

    def test_review_B7_open_flags_and_B8_directory_errors(self):
        import os
        d = self.disk
        d.write('/model/file', b'data'); d.mkdir('/model/dir')
        with self.assertRaises(NotADirectoryError):
            self.call('open', '/model/file', flags=os.O_RDONLY | os.O_DIRECTORY)
        with self.assertRaises(IsADirectoryError):
            d.unlink('/model/dir')
        with self.assertRaises(NotADirectoryError):
            d.rmdir('/model/file')
        self.call('open', '/model/file', flags=os.O_WRONLY | os.O_TRUNC)
        self.assertEqual(b'', d.read('/model/file'))
        parent, name = d._parent('/model/link')
        d.links[parent][name] = d._new(stat.S_IFLNK, 0o777, 'file')
        with self.assertRaises(OSError) as error:
            self.call('open', '/model/link', flags=os.O_RDONLY | os.O_NOFOLLOW)
        self.assertEqual(__import__('errno').ELOOP, error.exception.errno)

    def test_review_B11_left_parent_exchange_and_unflushed_unlink(self):
        d = self.disk
        for name in ('left', 'right'):
            d.mkdir('/model/' + name); d.flush('/model/' + name)
        d.flush('/model')
        for name, data in (('left/a', b'a'), ('right/b', b'b')):
            d.write('/model/' + name, data); d.flush('/model/' + name)
        d.flush('/model/left'); d.flush('/model/right')
        d.rename('/model/left/a', '/model/right/a'); d.flush('/model/left'); d.crash()
        self.assertFalse(d.exists('/model/left/a'))
        self.assertFalse(d.exists('/model/right/a'))
        d.write('/model/left/a', b'a'); d.flush('/model/left/a'); d.flush('/model/left')
        d.rename('/model/left/a', '/model/right/b', exchange=True)
        d.flush('/model/left'); d.flush('/model/right'); d.crash()
        self.assertEqual(b'b', d.read('/model/left/a'))
        self.assertEqual(b'a', d.read('/model/right/b'))
        d.unlink('/model/left/a'); d.crash()
        self.assertEqual(b'b', d.read('/model/left/a'))

    def test_review_B6_power_loss_latches_finally_IO_until_recovery(self):
        from tests.merge_library_disk import ModelCrash
        f = Fixture()
        self.addCleanup(f.close)
        batch = f.batch({'a.txt': b'child'})
        disk = self.disk_type(f.root)
        unwinding = []
        def hook(call):
            if getattr(disk, 'powered_off', False):
                unwinding.append(call.operation)
            answer = disk(call)
            if call.operation == 'write' and call.when == 'after':
                disk.crash(power_loss=True)
                raise ModelCrash()
            return answer
        with self.assertRaises(ModelCrash):
            f.land(batch, hook=hook)
        self.assertIn('close', unwinding, 'production finally I/O evidence')
        with self.assertRaises(ModelCrash):
            disk(ml.IOCall('mkdir', 'before', 'test', str(f.project / 'after-power-loss')))
        self.assertFalse(disk.exists(f.project / 'after-power-loss'))
        disk.resume()
        ml.recover(f.project, f.private, hook=disk)


if __name__ == '__main__':
    unittest.main()
