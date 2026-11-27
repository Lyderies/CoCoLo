"""S15 fault rows at the real B4s writer; no sockets or synthetic SQL writes.

Each row creates open-binding, context and lease custody with product writers.
Bound rows also exercise the lease -> reservation transfer. Transaction cuts
raise BaseException after the real effect; reopening never invents a release.
"""
from contextlib import contextmanager
from unittest import mock

from test_framework import requirements
from tests.test_account_settlement import SettlementFixture
from tests.test_publication_replay import claim_binding
from codex_agents import project_holds_schema as holds

WHY = 'Real reserve/arm/bind/arbitrate/settle writers and store reopening; cuts surround durable effects without service sockets.'


class Stop(BaseException):
    pass


class SettlementMatrixTests(SettlementFixture):
    def setup_row(self, bound):
        lease = self.lease()
        self.store.record_open_binding(lease.run_handle, lease.attempt, lease.run_id, 'e' * 64, 'codex',
                                       instance='earlier', now_ms=self.now)
        if bound:
            self.bound()
        else:
            self.armed()
        self.open()
        return self.closure()

    def oracle(self, bound, raw, first):
        identity = holds.attempt_identity(*self.attempt())
        primary = ('reservation', claim_binding().claim_id, 'lost') if bound else ('lease', identity, 'lost_unbound')
        self.assertEqual(('released', primary[2]), self.hold(*primary[:2])[:2], primary)
        self.assertEqual(('released', 'claimed'), self.hold('open_binding', identity)[:2])
        self.assertEqual(('released', 'lost_with_reservation'), self.custody())
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_account_settlements').fetchone()[0])
        self.assertEqual((0, 0, 0, 0), self.counts())
        self.consistent()
        before = self.snapshot()
        self.successor()
        again = self.record_settlement(raw=raw)
        self.assertEqual((first['receipt_digest'], True), (again['receipt_digest'], again['known']))
        self.assertEqual(before, self.snapshot())
        self.store.expire_results()
        self.assertEqual((0, 0, 0), self.store._result_counts())

    def row(self, bound, cut):
        raw = self.setup_row(bound)
        before = self.snapshot()
        if cut in ('before_release', 'after_release'):
            release = holds.release_hold
            target_kind = 'reservation' if bound else 'lease'
            hit = []

            def stop(connection, project, kind, *args, **kwargs):
                if kind == target_kind:
                    hit.append(kind)
                    if cut == 'before_release':
                        raise Stop()
                value = release(connection, project, kind, *args, **kwargs)
                if kind == target_kind:
                    raise Stop()
                return value

            with mock.patch.object(holds, 'release_hold', stop), self.assertRaises(Stop):
                self.record_settlement(raw=raw)
            self.assertEqual([target_kind], hit)
            self.assertEqual(before, self.snapshot(), 'a cut inside the writer must roll back every companion')
        elif cut == 'after_commit':
            transaction = self.store._transaction

            @contextmanager
            def stopped_reply():
                with transaction():
                    yield
                raise Stop()

            with mock.patch.object(self.store, '_transaction', stopped_reply), self.assertRaises(Stop):
                self.record_settlement(raw=raw)
        elif cut != 'before_settlement':
            raise ValueError(cut)
        self.successor()
        first = self.record_settlement(raw=raw)
        self.assertEqual(cut == 'after_commit', first['known'])
        self.oracle(bound, raw, first)

    @requirements('S15-RS8-SETTLEMENT', why_not_scenario=WHY)
    def test_lease_and_reservation_cross_cuts_before_and_after_release_and_lost_reply(self):
        # Fresh real store per cell: one failed row cannot contaminate its neighbour.
        for bound in (False, True):
            for cut in ('before_settlement', 'before_release', 'after_release', 'after_commit'):
                with self.subTest(kind='reservation' if bound else 'lease', exit=cut):
                    case = SettlementMatrixTests('runTest')
                    case.setUp()
                    try:
                        case.row(bound, cut)
                    finally:
                        case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_settlement_release_disabled_once_fails_its_own_row(self):
        # Patch the release the product calls, while all other releases still run.
        for bound, disabled in ((False, 'lease'), (False, 'open_binding'),
                                (False, 'result_custody'), (True, 'reservation')):
            with self.subTest(bound=bound, disabled=disabled):
                case = SettlementMatrixTests('runTest')
                case.setUp()
                try:
                    raw = case.setup_row(bound)
                    original = holds.release_hold
                    hits = []

                    def off(connection, project, kind, *args, **kwargs):
                        if kind == disabled:
                            hits.append(kind)
                            return None
                        return original(connection, project, kind, *args, **kwargs)

                    with mock.patch.object(holds, 'release_hold', off):
                        first = case.record_settlement(raw=raw)
                    self.assertEqual([disabled], hits)
                    with self.assertRaises(AssertionError):
                        case.oracle(bound, raw, first)
                finally:
                    case.doCleanups()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(SettlementMatrixTests)
