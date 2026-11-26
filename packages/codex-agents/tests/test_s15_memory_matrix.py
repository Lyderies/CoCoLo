"""S15 memory ownership/recovery rows, real writer, instance lock and owner.

A held lock is the live control; a retained unlocked inode is stopping proof.
A rollback or lost answer never serves as stopping evidence by itself.
"""
from contextlib import contextmanager
from unittest import mock

from test_framework import requirements
from tests.test_memory_holds import MemoryFixture
from tests import test_b6_unattended_close as b6
from codex_agents import memory_holds as memory

WHY = 'Actual registration/preparation/release transactions, retained journal locks, successor recovery and unattended orchestration close; no sockets.'


class Stop(BaseException):
    pass


class MemoryMatrixTests(MemoryFixture):
    def oracle(self, preparation):
        kinds = ('driver_entry', 'preparation') if preparation else ('driver_entry',)
        for kind in kinds:
            row = self.row(kind)
            self.assertEqual(('released', 'service_stopped'), (row['state'], row['release_kind']), kind)
        before = [self.row(kind) for kind in kinds]
        self.reopen()
        memory.recover(self.store, self.journal.directory, 'successor')
        self.assertEqual(before, [self.row(kind) for kind in kinds])
        self.now = b6.hold(self.store, self.handle)[3] + 1
        self.assertEqual(1, self.owner.end_idle())
        self.assertEqual('interrupted', self.status(self.handle))
        self.assertEqual(('released', 'interrupted_unattended'), b6.hold(self.store, self.handle)[:2])
        self.assertEqual(0, self.owner.end_idle())
        b6.consistent(self, self.store)

    def row_at_cut(self, preparation, cut):
        if cut == 'before_registration':
            self.assertIsNone(self.row('driver_entry'))
        if cut == 'registration_lost_reply':
            transaction = self.store._transaction

            @contextmanager
            def lost():
                with transaction():
                    yield
                raise Stop()

            with mock.patch.object(self.store, '_transaction', lost), self.assertRaises(Stop):
                self.register()
        else:
            self.register()
        if preparation:
            self.prep()
        # All live rows survive an ordinary startup pass and a wall rollback.
        self.now = 0
        memory.recover(self.store, self.journal.directory, 'successor')
        self.assertEqual('held', self.row('driver_entry')['state'])
        if preparation:
            self.assertEqual('held', self.row('preparation')['state'])
        self.journal.keep_lock = True
        self.journal.release()
        if cut in ('before_release', 'after_release'):
            release = memory.release_hold
            hits = []

            def stopped(*args, **kwargs):
                hits.append(args[2])
                if cut == 'before_release':
                    raise Stop()
                release(*args, **kwargs)
                raise Stop()

            before = [self.row(k) for k in ('driver_entry', 'preparation')]
            with mock.patch.object(memory, 'release_hold', stopped), self.assertRaises(Stop):
                memory.recover(self.store, self.journal.directory, 'successor')
            self.assertEqual(1, len(hits))
            self.assertEqual(before, [self.row(k) for k in ('driver_entry', 'preparation')])
        self.reopen()
        memory.recover(self.store, self.journal.directory, 'successor')
        self.oracle(preparation)

    @requirements('S15-RS8-MEMORY', why_not_scenario=WHY)
    def test_both_memory_kinds_cross_crashes_and_lost_registration_reply(self):
        for preparation in (False, True):
            for cut in ('before_registration', 'registration_lost_reply', 'before_release', 'after_release'):
                with self.subTest(preparation=preparation, cut=cut):
                    case = MemoryMatrixTests('runTest')
                    case.setUp()
                    try:
                        case.row_at_cut(preparation, cut)
                    finally:
                        case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_memory_recovery_release_disabled_once_fails_its_row(self):
        for disabled in ('driver_entry', 'preparation'):
            with self.subTest(disabled=disabled):
                case = MemoryMatrixTests('runTest')
                case.setUp()
                try:
                    case.register()
                    case.prep()
                    case.journal.keep_lock = True
                    case.journal.release()
                    original = memory.release_hold
                    hits = []

                    def off(connection, project, kind, *args, **kwargs):
                        if kind == disabled:
                            hits.append(kind)
                            return
                        return original(connection, project, kind, *args, **kwargs)

                    with mock.patch.object(memory, 'release_hold', off):
                        memory.recover(case.store, case.journal.directory, 'successor')
                    self.assertEqual([disabled], hits)
                    with self.assertRaises(AssertionError):
                        case.oracle(True)
                finally:
                    case.doCleanups()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(MemoryMatrixTests)
