"""Q5/Q6, D7 and terminal-direction witnesses from the reviewed end-state table."""
import errno
import unittest

from codex_agents import merge_library as ml
from tests.merge_library_support import Fixture, put, tree
from tests.merge_library_disk import MemoryDisk, ModelCrash


class RecoveryPolicies(unittest.TestCase):
    __requirements__ = ('ML-11', 'ML-17', 'ML-19', 'ML-23', 'TF-19')
    __why_not_scenario__ = 'Power loss and persistence identity checks need exact commit boundaries and an independent disk view.'

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def test_Q5_all_emptied_ancestors_are_recorded_and_unlisted_empty_folder_survives(self):
        f = self.f
        put(f.project / 'outer/inner/leaf', b'old')
        (f.project / 'owner-empty').mkdir()
        f.before = f.operation_before = tree(f.project)
        identities = {name: ((f.project / name).stat().st_dev, (f.project / name).stat().st_ino)
                      for name in ('outer', 'outer/inner')}
        batch = f.batch({'outer/inner/leaf': None})
        report = f.land(batch)
        self.assertEqual('landed', report.state)
        self.assertEqual(identities, ml.history(f.project, f.private)[-1].emptied_folders)
        self.assertEqual(f.expected(batch), tree(f.project))
        self.assertTrue((f.project / 'owner-empty').is_dir())

    def test_Q6_cancel_applying_attempts_putback_and_records_the_outcome(self):
        f = self.f
        batch = f.batch({'a.txt': b'cancelled child'})
        disk = MemoryDisk(f.root)
        def crash(call):
            answer = disk(call)
            if call.operation == 'exchange' and call.phase == 'forward' and call.when == 'after':
                disk.crash()
                raise ModelCrash()
            return answer
        with self.assertRaises(ModelCrash):
            f.land(batch, hook=crash)
        ticket = ml.queue(f.project, f.private, hook=disk)[0].ticket
        report = ml.cancel(f.project, f.private, ticket, hook=disk)
        self.assertEqual('reverted', report.state)
        self.assertEqual('cancelled', report.reason)
        self.assertEqual('reverted', ml.history(f.project, f.private, hook=disk)[-1].state)
        self.assertEqual(f.before, disk.snapshot(f.project, control=False))

    def test_D7_unreadable_history_reports_unknown_and_touches_nothing(self):
        f = self.f
        batch = f.batch({'a.txt': b'child'})
        f.land(batch)
        before = tree(f.project, metadata=True, control=True)
        def unreadable(call):
            if call.operation == 'sqlite_read' and call.when == 'before':
                raise OSError(errno.EIO, 'unreadable history')
        reports = ml.recover(f.project, f.private, hook=unreadable)
        self.assertEqual(1, len(reports))
        self.assertEqual(('unknown', 'io', False), (reports[0].state, reports[0].reason, reports[0].recorded))
        self.assertEqual(before, tree(f.project, metadata=True, control=True))

    def test_D6_retry_and_cancel_cannot_reverse_durable_landed(self):
        f = self.f
        batch = f.batch({'a.txt': b'child'})
        landed = f.land(batch)
        before = tree(f.project)
        report = ml.retry(f.project, f.private, landed.batch_no, policy=f.policy)
        self.assertEqual(('refused', 'terminal'), (report.state, report.reason))
        ml.cancel(f.project, f.private, ml.queue(f.project, f.private)[0].ticket)
        self.assertEqual(before, tree(f.project))
        self.assertEqual('landed', ml.history(f.project, f.private)[-1].state)


if __name__ == '__main__':
    unittest.main()
