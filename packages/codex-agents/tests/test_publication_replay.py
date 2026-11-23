"""Slice D4 (RS-D P1, P2', P3', R1-R6, R3', X1-X3): publication material and replay, at the store.

DESIGN-0.4.0-RS-D.md draft 3 s1-s3, s6, draft 4 s2-s4, shared rules S-2 and S-3 (DESIGN-0.4.0-RS-BD-shared.md SH-4).
The prepare transaction stores the exact prepared material in `project_result_publications`; a failed publication is
retried from the kept packet until it commits; a still-eligible publication (reservation `reserved`, obligation row
`held`, context `bound`) whose final body verifies is committed from the store by a successor or by settlement
arbitration, and an ineligible one only finishes cleanup. Settlement is arbitrated at the writer before the account is
asked: `recovered`, `unrecoverable` (the obligation row goes `releasing`, so nothing commits afterwards) or `settling`.
Integration stage: real stores in temporary folders, the fixtures of the D2 tests; faults are injected by patching the
file calls the store makes, never the real home. A process stop is a BaseException nothing in the store catches.
RS-B's `record_account_settlement` is B4's: the tests plant its two writes (the settlement row and the released
obligation) exactly as D2's X9 does.
"""
import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import threading
import types
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_deadlines_retention as dr
from tests import test_record_context as rc
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_project_history import binding

from codex_agents import project_history, project_holds_schema, project_result_contexts, project_driver, result_persistence
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.result_contracts import CodexUsageObservation

KEY = leases.fixtures.KEY
WHY = ('D4 checks real project stores in temporary folders (leases, contexts, holds, files, prepared material); injected '
       'file failures and process stops expose each step of publication, replay and arbitration.')
POINTS = ('create', 'write', 'fsync', 'link', 'unlink_partial', 'fsync_directory')


def claim_binding(number=1):
    """The fixtures' binding, with a claim id of the product's shape (`brg_` and 32 hex digits): the terminal record
    names its run's claim as its bridge run, and a client usage record is built only for the claim's own terminal."""
    base = binding(number)
    claim = 'brg_' + f'{number:032x}'
    return dataclasses.replace(base, claim_id=claim, claim_digest=hashlib.sha256(claim.encode()).hexdigest())


class Stop(BaseException):
    """A process stop: nothing in the store catches it, so what was written stays."""


class ReplayFixture(dr.LeaseBackedFixture):
    def usage(self):
        return CodexUsageObservation(assignment()['model'], None)

    def commit(self, number=1, usage=True):
        body, content = self.packet(number)
        return self.store.commit_result(content, claim_binding(number), assignment(), body,
                                        **({'client_usage': self.usage()} if usage else {}))

    def packet(self, number=1):
        return self.fixture.payload(number)

    def fail_at(self, point, error):
        """Fail the first file call of one publication step with `error` (an OSError, or a Stop)."""
        original = {name: getattr(os, name) for name in ('open', 'write', 'fsync', 'link', 'unlink')}
        done = []
        directory = self.store._directory_fd

        def once(match):
            if done or not match:
                return False
            done.append(1)
            return True

        def open_(name, flags, *args, **kwargs):
            if once(point == 'create' and str(name).endswith('.partial') and flags & os.O_CREAT):
                raise error
            return original['open'](name, flags, *args, **kwargs)

        def write(descriptor, data):
            if once(point == 'write'):
                raise error
            return original['write'](descriptor, data)

        def fsync(descriptor):
            if once((point == 'fsync_directory') == (descriptor == directory) and point in ('fsync', 'fsync_directory')):
                raise error
            return original['fsync'](descriptor)

        def link(*args, **kwargs):
            if once(point == 'link'):
                raise error
            return original['link'](*args, **kwargs)

        def unlink(name, *args, **kwargs):
            if once(point == 'unlink_partial' and str(name).endswith('.partial')):
                raise error
            return original['unlink'](name, *args, **kwargs)
        patches = [mock.patch.object(project_history.os, name, side_effect=function) for name, function in
                   (('open', open_), ('write', write), ('fsync', fsync), ('link', link), ('unlink', unlink))]
        return _Patches(patches)

    def strand(self, number=1, *, usage=True, stop_at=None):
        """Bind an attempt, then stop the publication: at the commit transaction (final body on disk) or at one
        file step. What is left is what a stopped service leaves."""
        self.bound(number)
        if stop_at is None:
            with mock.patch.object(self.store, '_commit_terminal_locked', side_effect=Stop()):
                with self.assertRaises(Stop):
                    self.commit(number, usage=usage)
        else:
            with self.fail_at(stop_at, Stop()):
                with self.assertRaises(Stop):
                    self.commit(number, usage=usage)

    def bound(self, number=1, **context):
        lease = leases.lease_binding(number)
        self.store.reserve_result_lease(lease, assignment(), record_context=rc.simple_context(number, **context))
        self.store.arm_result_lease(lease)
        return self.store.bind_result_lease(lease, claim_binding(number), assignment(), hard_deadline_at_ms=2000)

    def reservation(self, number=1):
        return self.store.result_reservation(claim_binding(number))

    def settle(self, kind, number=1):
        """What B4's `record_account_settlement` will write: the settlement row and the obligation released with it."""
        target = claim_binding(number)
        self.store._connection.execute(
            "INSERT INTO project_account_settlements VALUES(?,?,?,?,?,?,?)",
            (target.project_id, target.run_handle, target.attempt, target.claim_id, kind, b'{}', self.now))
        project_holds_schema.release_hold(self.store._connection, target.project_id, 'reservation', target.claim_id,
                                          kind, now_ms=self.now)

    def record(self, digest, number=1):
        with self.store._transaction():
            project_result_contexts.record_outcome(self.store, claim_binding(number), digest)

    def successor(self):
        self.base.reopen(result_capacity=self.fixture.capacity)

    def claim(self, number=1):
        return claim_binding(number).claim_id

    def material(self, number=1):
        row = self.store._connection.execute("SELECT material FROM project_result_publications WHERE claim_id=?",
                                             (self.claim(number),)).fetchone()
        return None if row is None else bytes(row[0])

    def prepared(self, number=1):
        return tuple(self.store._connection.execute(
            "SELECT prepared_digest,retained_until_ms,state FROM result_reservations WHERE claim_id=?",
            (self.claim(number),)).fetchone())

    def counts(self):
        connection = self.store._connection
        return (connection.execute("SELECT COUNT(*) FROM terminal_outbox").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM records WHERE record_type='dispatch_result'").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM records WHERE record_id LIKE 'client_usage:%'").fetchone()[0])

    def files(self):
        return sorted(path.name for path in self.fixture.directory.iterdir() if path.name.startswith('.result-'))

    def obligation(self, number=1):
        row = self.store._connection.execute(
            "SELECT state,release_kind,request_id FROM project_holds WHERE kind='reservation' AND identity=?",
            (self.claim(number),)).fetchone()
        return None if row is None else tuple(row)

    def plant(self, number, mutate):
        """Alter the stored material and keep it hashing to the prepared digest (so only the replay checks can refuse)."""
        data = json.loads(self.material(number))
        mutate(data)
        changed = canonical_json(data)
        self.store._connection.execute("UPDATE project_result_publications SET material=? WHERE claim_id=?",
                                       (changed, self.claim(number)))
        self.store._connection.execute("UPDATE result_reservations SET prepared_digest=? WHERE claim_id=?",
                                       (keyed_digest(KEY, changed), self.claim(number)))

    def refused(self, number=1, reason='history_conflict'):
        """Replay is refused, and nothing was committed, retired or deleted."""
        before = (self.counts(), self.files(), self.prepared(number)[2], self.store._result_counts())
        with self.assertRaises(HistoryError) as caught:
            self.store.replay_publication(claim_binding(number))
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertEqual(before, (self.counts(), self.files(), self.prepared(number)[2], self.store._result_counts()))

    def settle_lost(self, number=1):
        self.settle('lost', number)


class _Patches:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class KeptPacketRetryTests(ReplayFixture):
    """P1: a failure with the service live is retried from the kept packet until it commits, once."""

    def test_the_prepare_transaction_stores_the_exact_material_and_the_commit_removes_it(self):
        self.strand(1)
        stored = self.material()
        self.assertIsNotNone(stored)
        self.assertEqual(keyed_digest(KEY, stored), self.prepared()[0])
        self.assertEqual(('reserved', None), (self.prepared()[2], self.release_kind()[1]))
        self.consistent()  # a legal recovery state passes both SH-7 validators
        self.commit()
        self.assertIsNone(self.material())
        self.assertEqual('committed', self.prepared()[2])
        self.consistent()

    def test_P1_each_transient_failure_is_retried_from_the_kept_packet_and_commits_once(self):
        self.bound(1)
        for number, point in enumerate(POINTS, start=1):
            with self.subTest(point=point):
                if number > 1:
                    self.bound(number)
                with self.fail_at(point, OSError('transient')):
                    with self.assertRaises(HistoryError):
                        self.commit(number)
                first = self.prepared(number)
                self.assertEqual('reserved', first[2])
                kept = self.material(number)
                self.assertIsNotNone(kept)  # the material is kept for the retry, and for a successor
                self.now += 17  # a retry must not restart retention
                self.commit(number)  # the retry, from the same packet
                last = self.prepared(number)
                self.assertEqual(first[:2], last[:2])  # prepared_digest and retained_until_ms unchanged
                self.assertEqual(('committed', None), (last[2], self.material(number)))
                self.assertEqual(number, self.counts()[0])  # one terminal per attempt
                self.assertEqual((number, number), self.counts()[1:])  # one dispatch_result and one usage record each
                self.assertEqual(self.packet(number)[0], self.store.read_result(claim_binding(number)))
        self.consistent()
        # The run then releases normally: retention ends it.
        self.now = self.reservation(len(POINTS)).retained_until_ms
        self.assertEqual(len(POINTS), self.store.expire_results(limit=10))
        self.consistent()

    def test_P1_a_retry_that_changes_the_material_is_refused_and_moves_nothing(self):
        self.bound(1)
        with self.fail_at('write', OSError('transient')):
            with self.assertRaises(HistoryError):
                self.commit(1)
        before = self.prepared()
        body, content = self.packet(1)
        other = json.loads(content)
        other['observed_at_ms'] = 5
        with self.assertRaises(HistoryError) as caught:
            self.store.commit_result(canonical_json(other), claim_binding(1), assignment(), body,
                                     client_usage=self.usage())
        self.assertEqual('history_conflict', caught.exception.reason_code)
        self.assertEqual(before, self.prepared())


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class StopBeforeAndAfterTheHandoffTests(ReplayFixture):
    """P2', P3': a stop before the directory fsync (a: the final link survived; b: only a partial) or after it."""

    def test_P2_a_stop_before_the_final_link_ends_lost_by_settlement_with_nothing_half_recorded(self):
        for number, point in enumerate(('create', 'write', 'fsync', 'link'), start=1):
            with self.subTest(point=point):
                self.strand(number, stop_at=point)
                self.successor()
                self.assertEqual([], self.store.replay_publications())  # no verifiable final body: nothing to replay
                self.assertEqual('unrecoverable',
                                 self.store.arbitrate_publication(*self.attempt(number), 'lost-%d' % number))
                self.assertEqual('releasing', self.obligation(number)[0])
                self.settle_lost(number)
                self.assertGreaterEqual(self.store.expire_results(limit=10), 1)
                self.assertEqual('expired', self.row_state(number))
                self.assertIsNone(self.material(number))
                self.assertEqual((0, 0, 0), self.counts())
                self.assertEqual('lost_with_reservation', self.release_kind(number)[1])
                self.consistent()
        self.assertEqual([], self.files())

    def test_P2prime_b_only_a_partial_file_is_lost_and_its_files_and_material_are_cleaned(self):
        self.strand(1, stop_at='link')
        partial = [name for name in os.listdir(self.fixture.directory) if name.endswith('.partial')]
        self.assertEqual(1, len(partial))
        self.assertEqual([], [name for name in self.files() if name.endswith('.json')])
        self.successor()
        self.assertEqual('unrecoverable', self.store.arbitrate_publication(*self.attempt(1), 'lost-1'))
        self.settle_lost(1)
        self.store.expire_results(limit=10)
        self.assertEqual(([], None), (self.files(), self.material(1)))
        self.consistent()

    def test_P2prime_a_a_final_link_that_survived_a_stop_before_the_directory_fsync_is_recovered_and_committed(self):
        for number, point in ((1, 'unlink_partial'), (2, 'fsync_directory')):
            with self.subTest(point=point):
                self.strand(number, stop_at=point)
                self.successor()
                self.assertEqual([self.claim(number)], self.store.replay_publications())
                self.assertEqual(('committed', None), (self.prepared(number)[2], self.material(number)))
                self.assertEqual(self.packet(number)[0], self.store.read_result(claim_binding(number)))
                self.assertEqual(self.counts()[0], number)
                self.consistent()

    def test_P3_a_stop_after_the_handoff_is_completed_from_the_store_exactly_once(self):
        self.strand(1)
        self.successor()
        self.assertEqual([self.claim()], self.store.replay_publications())
        self.assertEqual([], self.store.replay_publications())
        self.assertEqual((1, 1, 1), self.counts())
        self.assertEqual('committed', self.prepared()[2])
        self.assertEqual(self.packet(1)[0], self.store.read_result(claim_binding(1)))
        self.assertEqual('committed', self.context_state())
        self.consistent()

    def test_P3prime_the_start_up_the_expiry_and_the_lost_sweeps_each_run_first_and_it_commits_once(self):
        def start_up(store, number):
            return store.replay_publications()

        def expiry(store, number):
            store.expire_results(limit=10)
            return store.replay_publications()

        def lost(store, number):
            self.assertEqual('recovered', store.arbitrate_publication(*self.attempt(number), 'lost-%d' % number))
            return []
        for number, first in enumerate((start_up, expiry, lost), start=1):
            with self.subTest(first=first.__name__):
                self.strand(number)
                self.successor()
                first(self.store, number)
                for later in (start_up, expiry, lost):  # in turn, in every order
                    later(self.store, number)
                self.assertEqual(number, self.counts()[0])
                self.assertEqual('committed', self.prepared(number)[2])
                self.assertEqual(('held', None), self.obligation(number)[:2])  # never classified lost
                self.assertEqual(('committed', None), (self.context_state(number), self.release_kind(number)[1]))
        self.consistent()

    def test_the_replay_pass_completes_at_most_four_publications_and_the_next_pass_the_rest(self):
        for number in range(1, 7):
            self.strand(number)
        self.successor()
        first = self.store.replay_publications()
        self.assertEqual(4, len(first))  # REPLAY_PASS_LIMIT: each pass reads at most four bodies
        second = self.store.replay_publications()
        self.assertEqual(2, len(second))
        self.assertEqual(sorted(self.claim(n) for n in range(1, 7)), sorted(first + second))
        self.assertEqual([], self.store.replay_publications())
        self.assertEqual(6, self.counts()[0])
        # A claim whose job is publishing in this service is skipped; a limit past the bound is refused.
        self.strand(7)
        self.strand(8)
        self.assertEqual([self.claim(8)], self.store.replay_publications(skip=frozenset({self.claim(7)})))
        with self.assertRaises(HistoryError):
            self.store.replay_publications(limit=5)
        self.assertEqual([self.claim(7)], self.store.replay_publications())
        self.consistent()

    def test_P3prime_an_expiry_pass_alone_neither_deletes_nor_classifies_a_stranded_publication(self):
        self.strand(1)
        self.now = 10_000_000  # far past the lease deadline and the hard deadline
        self.successor()
        self.assertEqual(0, self.store.expire_results(limit=10))
        self.assertEqual(('reserved', 1), (self.prepared()[2], len(self.files())))
        self.assertIsNotNone(self.material())
        self.consistent()


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class ReplayEligibilityTests(ReplayFixture):
    """R1-R6, R3': replay completes only a still-eligible publication; an ineligible one only finishes cleanup."""

    def test_R1_a_retiring_reservation_with_its_final_file_is_cleanup_only(self):
        self.strand(1)
        self.settle_lost(1)
        with mock.patch.object(project_history.os, 'unlink', side_effect=OSError('cut')):
            with self.assertRaises(HistoryError):
                self.store.expire_results(limit=10)
        self.assertEqual('retiring', self.row_state())
        self.assertIsNone(self.material())  # the envelope goes with the `retiring` row (one transaction)
        self.successor()
        self.assertEqual([], self.store.replay_publications())
        self.refused(reason='history_result_expired')
        self.store.expire_results(limit=10)
        self.assertEqual(('expired', (0, 0, 0), []), (self.row_state(), self.counts(), self.files()))
        self.consistent()

    def test_R2_a_reservation_released_lost_or_crashed_is_cleanup_only(self):
        for number, kind in ((1, 'lost'), (2, 'crashed')):
            with self.subTest(kind=kind):
                self.strand(number)
                self.settle(kind, number)
                self.assertEqual([], self.store.replay_publications())
                self.refused(number, 'history_result_expired')
                self.store.expire_results(limit=10)
                self.assertEqual(('expired', None), (self.row_state(number), self.material(number)))
                self.assertEqual((0, 0, 0), self.counts())
        self.consistent()

    def test_R2_a_reservation_released_completed_by_the_account_is_not_replayed(self):
        self.strand(1)
        project_holds_schema.release_hold(self.store._connection, claim_binding(1).project_id, 'reservation', self.claim(),
                                          'completed', now_ms=self.now)
        self.assertEqual([], self.store.replay_publications())
        self.refused(1, 'history_result_expired')
        self.now = self.prepared()[1]
        self.assertEqual(1, self.store.expire_results(limit=10))
        self.assertEqual(('expired', None, [], (0, 0, 0)),
                         (self.row_state(), self.material(), self.files(), self.counts()))

    def test_R3prime_a_replay_candidate_past_its_lease_deadline_with_the_obligation_held_is_replayed(self):
        self.strand(1)
        self.successor()
        self.now = 1000 + 60_000 + 5_000_000  # past `expires_at_ms` and the hard deadline
        before = self.files()
        self.assertEqual([self.claim()], self.store.replay_publications())
        self.assertEqual(('committed', before), (self.prepared()[2], self.files()))  # replayed; nothing deleted
        self.assertEqual((1, 1, 1), self.counts())
        self.consistent()

    def test_R5_a_context_that_is_not_bound_refuses_the_replay_and_nothing_changes(self):
        self.strand(1)
        self.store._connection.execute("UPDATE project_result_contexts SET state='ended' WHERE run_handle=?",
                                       (self.attempt()[0],))
        self.assertEqual([], self.store.replay_publications())
        self.refused()
        self.assertIsNotNone(self.material())
        self.settle_lost()  # the account release is the legitimate cleanup prerequisite
        self.assertEqual(1, self.store.expire_results(limit=10))
        self.assertEqual(('expired', None, [], (0, 0, 0)),
                         (self.row_state(), self.material(), self.files(), self.counts()))

    def test_R6_an_envelope_that_does_not_hash_to_the_prepared_digest_is_refused(self):
        self.strand(1)
        self.store._connection.execute("UPDATE project_result_publications SET material=? WHERE claim_id=?",
                                       (canonical_json({**json.loads(self.material()), 'result_digest': '0' * 64}), self.claim()))
        self.refused()
        self.assertEqual([], self.store.replay_publications())

    def test_R6_an_altered_binding_assignment_record_or_usage_record_is_refused_and_kept_charged(self):
        def other_attempt(data):
            data['binding']['attempt'] = 9

        def other_run(data):
            data['binding']['run_id'] = 'run-other'

        def other_assignment(data):
            data['assignment_digest'] = '0' * 64

        def other_record_run(data):
            data['record']['run_id'] = 'run-other'

        def other_record_result(data):
            data['record']['result_digest'] = '1' * 64

        def other_result(data):
            data['result_digest'] = '2' * 64
            data['record']['result_digest'] = '2' * 64

        def other_usage(data):
            data['client_usage_record']['run_id'] = 'run-other'

        def foreign_usage_id(data):
            data['client_usage_record']['record_id'] = 'client_usage:' + '3' * 64
        cases = (other_attempt, other_run, other_assignment, other_record_run, other_record_result, other_result,
                 other_usage, foreign_usage_id)
        for number, mutate in enumerate(cases, start=1):
            with self.subTest(mutate=mutate.__name__):
                self.strand(number)
                self.plant(number, mutate)
                self.refused(number)
                self.assertEqual('reserved', self.prepared(number)[2])
                self.assertIsNotNone(self.material(number))
        self.assertEqual([], self.store.replay_publications())  # the sweep isolates each refusal
        self.assertEqual((0, 0, 0), self.counts())
        self.assertEqual(len(cases), len(self.files()))  # kept, charged, for retirement
        self.assertEqual(len(cases), self.store._result_counts()[0])

    def test_R6_usage_phase_provider_and_time_each_refuse(self):
        for number, (field, value) in enumerate((('phase', 'before'), ('provider', 'other'), ('observed_at_ms', 1001)), 1):
            with self.subTest(field=field):
                self.strand(number)
                self.plant(number, lambda data: data['client_usage_record'].__setitem__(field, value))
                self.refused(number)

    def test_R6_an_altered_body_is_refused_and_an_absent_usage_record_is_a_replay_without_one(self):
        self.strand(1)
        path = self.fixture.directory / self.store._result_names(claim_binding(1))[1]
        body = path.read_bytes()
        path.write_bytes(body[:-1] + (b'X' if body[-1:] != b'X' else b'Y'))
        self.refused()
        self.strand(2)
        self.strand(3, usage=False)
        data = json.loads(self.material(2))
        data.pop('client_usage_record')
        self.store._connection.execute('UPDATE project_result_publications SET material=? WHERE claim_id=?',
                                       (canonical_json(data), self.claim(2)))
        self.refused(2)
        self.assertEqual([self.claim(3)], self.store.replay_publications())
        self.assertEqual((1, 1, 0), self.counts())  # `no observation was supplied`, never `unavailable`

    def test_R4prime_a_live_publication_racing_a_settlement_loses_in_both_commit_orders(self):
        # (i) arbitration commits first; the authenticated live retry returns that delivery.
        self.bound(1)
        with self.fail_at('fsync_directory', OSError('transient')):
            with self.assertRaises(HistoryError):
                self.commit(1)
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(1), 'lost-1'))
        self.settle_lost(1)
        before = self.counts()
        self.commit(1)  # recovery won; authenticated live retry is idempotent even after settlement
        self.assertEqual(before, self.counts())
        self.assertEqual((1, 1, 1), self.counts())  # the body was committed before the settlement
        # (ii) a settlement that found nothing to recover: no terminal after it.
        self.bound(2)
        with self.fail_at('write', OSError('transient')):
            with self.assertRaises(HistoryError):
                self.commit(2)
        self.assertEqual('unrecoverable', self.store.arbitrate_publication(*self.attempt(2), 'lost-2'))
        with self.assertRaises(HistoryError) as caught:
            self.commit(2)
        self.assertEqual('history_result_expired', caught.exception.reason_code)
        self.assertEqual((1, 1, 1), self.counts())
        self.assertEqual('reserved', self.prepared(2)[2])
        self.assertEqual('releasing', self.obligation(2)[0])
        self.consistent()

    def test_R4prime_live_commit_wins_before_arbitration_and_settlement(self):
        self.bound()
        delivery = self.commit()
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(), 'live-first'))
        self.settle_lost()
        self.assertEqual(delivery, self.commit())
        self.assertEqual((1, 1, 1), self.counts())
        self.assertEqual(self.packet()[0], self.store.read_result(claim_binding()))


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class ArbitrationTests(ReplayFixture):
    """X1-X3 (S-3): the project writer arbitrates before the account is asked."""

    def test_X1_a_verifiable_publication_is_recovered_and_the_settlement_then_leaves_the_body(self):
        self.strand(1)
        self.successor()
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(), 'lost-1'))
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(), 'lost-1'))  # idempotent
        self.assertEqual((1, 1, 1), self.counts())
        self.settle_lost()
        self.assertEqual(0, self.store.expire_results(limit=10))
        self.assertEqual(('committed', ('held', None)), (self.row_state(), self.release_kind()))
        self.assertEqual(self.packet(1)[0], self.store.read_result(claim_binding(1)))
        self.record('f' * 64)  # still recordable
        self.assertEqual('recorded', self.context_state())
        self.consistent()

    def test_X2_no_material_or_a_body_that_does_not_verify_is_unrecoverable_and_closes_the_gate(self):
        self.strand(1)
        material = self.material(1)
        self.store._connection.execute("DELETE FROM project_result_publications WHERE claim_id=?", (self.claim(1),))
        self.strand(2)
        path = self.fixture.directory / self.store._result_names(claim_binding(2))[1]
        path.write_bytes(b'not the body')
        for number in (1, 2):
            with self.subTest(number=number):
                self.assertEqual('unrecoverable',
                                 self.store.arbitrate_publication(*self.attempt(number), 'lost-%d' % number))
                self.assertEqual(('releasing', None, 'lost-%d' % number), self.obligation(number))
                # Restore every verification input; only the releasing authorization gate can refuse.
                if number == 1:
                    self.store._connection.execute('INSERT INTO project_result_publications VALUES(?,?,?)',
                                                   (claim_binding(1).project_id, self.claim(1), material))
                else:
                    path.write_bytes(self.packet(2)[0])
                with self.assertRaises(HistoryError) as live:  # a later live commit
                    self.commit(number)
                self.assertEqual('history_result_expired', live.exception.reason_code)
                with self.assertRaises(HistoryError) as replay:  # and a later replay
                    self.store.replay_publication(claim_binding(number))
                self.assertEqual('history_result_expired', replay.exception.reason_code)
                self.assertEqual('reserved', self.prepared(number)[2])
        self.assertEqual((0, 0, 0), self.counts())
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))

    def test_X3_a_releasing_row_or_a_settlement_row_already_present_is_settling_and_cleanup_follows(self):
        self.strand(1)
        self.strand(2)
        self.store._connection.execute("UPDATE project_holds SET state='releasing',request_id='first' "
                                       "WHERE kind='reservation' AND identity=?", (self.claim(1),))
        self.assertEqual('settling', self.store.arbitrate_publication(*self.attempt(1), 'second'))
        self.assertEqual('first', self.obligation(1)[2])  # the first request id is never replaced
        self.store._connection.execute(
            "INSERT INTO project_account_settlements VALUES(?,?,?,?,?,?,?)",
            (claim_binding(2).project_id, *self.attempt(2), self.claim(2), 'lost', b'{}', self.now))
        self.assertEqual('settling', self.store.arbitrate_publication(*self.attempt(2), 'third'))
        self.assertEqual((0, 0, 0), self.counts())
        self.assertEqual([], self.store.replay_publications())
        # Cleanup follows the settlement: the obligation is released, and the next pass retires the row.
        project_holds_schema.release_hold(self.store._connection, claim_binding(2).project_id, 'reservation', self.claim(2),
                                          'lost', now_ms=self.now)
        self.assertEqual(1, self.store.expire_results(limit=10))
        self.assertEqual(('expired', None), (self.row_state(2), self.material(2)))
        self.settle_lost(1)
        self.assertEqual(1, self.store.expire_results(limit=10))
        self.assertEqual(('expired', None, [], (0, 0, 0)),
                         (self.row_state(1), self.material(1), self.files(), self.counts()))

    def test_an_attempt_with_no_reservation_has_nothing_to_arbitrate(self):
        self.assertEqual('none', self.store.arbitrate_publication('hrn_' + 'a' * 48, 1, 'lost-x'))



@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class ReviewRegressionTests(ReplayFixture):
    def test_arbitration_preserves_read_envelope_and_commit_failures_for_retry(self):
        faults = (('read', 'history_service_unavailable'), ('envelope', 'history_conflict'),
                  ('commit', 'history_conflict'))
        for number, (kind, reason) in enumerate(faults, 1):
            with self.subTest(kind=kind):
                self.strand(number)
                if kind == 'envelope':
                    self.store._connection.execute('UPDATE project_result_publications SET material=? WHERE claim_id=?',
                                                   (b'{}', self.claim(number)))
                    patch = contextlib.nullcontext()
                else:
                    patch = mock.patch.object(self.store, '_read_result_file' if kind == 'read' else
                                              '_commit_terminal_locked', side_effect=HistoryError(reason))
                with patch, self.assertRaises(HistoryError) as caught:
                    self.store.arbitrate_publication(*self.attempt(number), 'retry')
                self.assertEqual(reason, caught.exception.reason_code)
                self.assertEqual(('held', None, None), self.obligation(number))
                self.assertEqual((0, 0, 0), self.counts())

    def test_missing_body_is_proven_loss_but_missing_hold_never_authorizes_settlement(self):
        self.strand(1)
        (self.fixture.directory / self.store._result_names(claim_binding())[1]).unlink()
        self.assertEqual('unrecoverable', self.store.arbitrate_publication(*self.attempt(), 'missing-body'))
        self.strand(2)
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='reservation' AND identity=?", (self.claim(2),))
        with self.assertRaises(HistoryError) as caught:
            self.store.arbitrate_publication(*self.attempt(2), 'missing-hold')
        self.assertEqual('history_service_unavailable', caught.exception.reason_code)
        self.assertEqual((0, 0, 0), self.counts())

    def test_bound_context_without_hold_or_lease_cannot_replay(self):
        self.strand()
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='reservation' AND identity=?", (self.claim(),))
        # Corrupt recovery fixture: preserve the bound context while removing its source lease.
        self.store._connection.execute('PRAGMA foreign_keys=OFF')
        self.store._connection.execute('DELETE FROM result_leases WHERE claim_id=?', (self.claim(),))
        self.store._connection.execute('PRAGMA foreign_keys=ON')
        before = (self.counts(), self.files(), self.prepared(), self.material(),
                  self.store._result_counts(), rc.context_row(self.store, *self.attempt()), self.release_kind())
        self.assertLess(self.now, self.reservation().reserved_until_ms)
        with self.assertRaises(HistoryError) as caught:
            self.commit()  # live publication must require the hold even before the deadline
        self.assertEqual('history_result_expired', caught.exception.reason_code)
        self.assertEqual(before, (self.counts(), self.files(), self.prepared(), self.material(),
                                 self.store._result_counts(), rc.context_row(self.store, *self.attempt()),
                                 self.release_kind()))
        self.now = 10_000_000
        self.refused(reason='history_result_expired')

    def test_migrated_reserved_row_without_material_can_be_fenced_without_bound_context(self):
        from tests import test_project_holds_schema as schema_tests
        schema = schema_tests.SchemaStepTests(methodName='runTest')
        schema.setUp()
        self.addCleanup(schema.doCleanups)
        # The fixture prepares claim 4 using today's writer; format 17 had no publication table.
        schema.store._connection.execute('DELETE FROM project_result_publications')
        schema.format_17_store()
        history = schema.upgrade()
        for number, reason in ((2, 'history_conflict'), (4, 'history_result_expired')):
            with self.subTest(source='lease' if number == 2 else 'reference'):
                target = binding(number)  # migration may retain a lease-born or a reference reservation
                self.assertEqual('legacy_no_context', rc.context_row(history, target.run_handle, target.attempt)[0])
                self.assertTrue(project_holds_schema.custody_consistent(history._connection, KEY))
                with self.assertRaises(HistoryError) as caught:
                    history.replay_publication(target)
                self.assertEqual(reason, caught.exception.reason_code)
                before = tuple(history._connection.execute('SELECT * FROM result_reservations WHERE claim_id=?',
                                                          (target.claim_id,)).fetchone())
                self.assertEqual('unrecoverable', history.arbitrate_publication(target.run_handle, target.attempt, 'legacy'))
                self.assertEqual(('releasing', 'legacy'), tuple(history._connection.execute(
                    "SELECT state,request_id FROM project_holds WHERE kind='reservation' AND identity=?",
                    (target.claim_id,)).fetchone()))
                self.assertEqual(before, tuple(history._connection.execute('SELECT * FROM result_reservations WHERE claim_id=?',
                                                                          (target.claim_id,)).fetchone()))
                self.assertEqual('settling', history.arbitrate_publication(target.run_handle, target.attempt, 'again'))
                self.assertIsNone(history._connection.execute('SELECT 1 FROM terminal_outbox WHERE claim_id=?',
                                                             (target.claim_id,)).fetchone())

    def test_retry_during_adoption_attestation_preserves_the_installed_delivery(self):
        self.strand()
        pending = types.SimpleNamespace(binding=claim_binding(), state='failed', delivery=None, grant=object(),
                                        adoption_pending=False, selected=object(), reason='history_service_unavailable',
                                        terminal_evidence=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock = threading.RLock()
        handle = result_persistence.ResultPersistenceHandle()
        lane._jobs = {handle: pending}
        lane._closed, lane._fault = False, False
        lane._pid, lane._owner = os.getpid(), mock.Mock()
        lane._history = lambda: self.store
        lane._incoming = queue.Queue()
        lane._ready = threading.Event()
        lane._bootstrap_history = False
        lane._requests = lane._store = None
        lane._unanswered, lane._earlier_held = {}, 1
        request = types.SimpleNamespace(kind='arbitrate', arguments=(*self.attempt(), 'race'))
        lane._incoming.put(('earlier', request))
        attesting, retried = threading.Event(), threading.Event()
        errors = []

        def retry():
            try:
                if not attesting.wait(2):
                    raise AssertionError('writer never reached attestation')
                lane.retry(handle)
            except BaseException as error:
                errors.append(error)
            finally:
                retried.set()
                lane._closed = True
                lane._incoming.put(None)

        def attest(_):
            attesting.set()
            self.assertTrue(retried.wait(2), 'retry must execute during attestation')
            return b'evidence'

        lane._thread = threading.Thread(target=retry, name='d4-adoption-retry')
        lane._thread.start()
        try:
            with mock.patch.object(self.store, 'attest_terminal_delivery', side_effect=attest), \
                    mock.patch.object(lane, '_publish', wraps=lane._publish) as publish:
                lane._run()  # actual operation dispatch, including any stale queued publish
        finally:
            lane._thread.join(3)
        self.assertFalse(lane._thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(('done', 'recovered'), (request.state, request.result))
        self.assertFalse(lane._fault)
        self.assertEqual(('committed', None, None, b'evidence'),
                         (pending.state, pending.reason, pending.selected, pending.terminal_evidence))
        self.assertEqual(self.store.committed_delivery(claim_binding()), pending.delivery)
        publish.assert_not_called()
        self.assertEqual((1, 1, 1), self.counts())

    def test_stale_queued_publish_leaves_an_already_installed_delivery_alone(self):
        self.strand()
        self.store.replay_publication(claim_binding())
        delivery, evidence = self.store.committed_delivery(claim_binding()), object()
        pending = types.SimpleNamespace(state='committed', reason=None, delivery=delivery,
                                        terminal_evidence=evidence, selected=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock, lane._ready = threading.RLock(), threading.Event()
        lane._jobs = {'live': pending}
        lane._bootstrap_history = False
        lane._requests = lane._store = None
        lane._closed, lane._fault = True, False
        lane._unanswered = {}
        lane._incoming = queue.Queue()
        lane._incoming.put(('publish', 'live'))
        lane._incoming.put(None)
        with mock.patch.object(lane, '_publish', wraps=lane._publish) as publish:
            lane._run()
        self.assertFalse(lane._fault)
        self.assertEqual(('committed', None), (pending.state, pending.reason))
        self.assertIs(delivery, pending.delivery)
        self.assertIs(evidence, pending.terminal_evidence)
        publish.assert_not_called()
        # A later acknowledgement failure still owns its installed delivery;
        # adoption must not take its state away from acknowledgement retry.
        pending.binding, pending.adoption_pending = claim_binding(), False
        pending.grant = None
        pending.state, pending.reason = 'failed', 'history_service_unavailable'
        history = mock.Mock(committed_delivery=mock.Mock(return_value=delivery))
        lane._adopt_committed(history, {self.claim()})
        self.assertEqual(('failed', 'history_service_unavailable'), (pending.state, pending.reason))
        self.assertIs(delivery, pending.delivery)
        self.assertIs(evidence, pending.terminal_evidence)
        history.committed_delivery.assert_not_called()

    def test_four_refusals_and_a_skipped_claim_do_not_starve_the_next_candidate(self):
        for number in range(1, 7):
            self.strand(number)
        for number in range(1, 5):
            self.store._connection.execute('UPDATE project_result_publications SET material=? WHERE claim_id=?',
                                           (b'{}', self.claim(number)))
        skip = frozenset({self.claim(6)})
        self.assertEqual([], self.store.replay_publications(skip))
        self.assertEqual([self.claim(5)], self.store.replay_publications(skip))

    def test_settlement_with_no_persistence_defers_before_account_call(self):
        driver = types.SimpleNamespace(_runtime=types.SimpleNamespace(persistence=None))
        with mock.patch.object(project_driver, 'AccountClient') as account:
            with self.assertRaises(HistoryError) as caught:
                project_driver.RunDriver._arbitrate_publication(driver, None, {'bridge_run_id': 'durable'})
            self.assertEqual('history_service_unavailable', caught.exception.reason_code)
        account.assert_not_called()

    def test_failed_adoption_retries_on_later_expiry_without_material(self):
        self.strand()
        pending = types.SimpleNamespace(binding=claim_binding(), state='failed', delivery=None, grant=object(),
                                        lease_binding=None, adoption_pending=False, selected='kept', reason='history_service_unavailable',
                                        terminal_evidence=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock = threading.RLock()
        lane._jobs = {'live': pending}
        lane._history = lambda: self.store
        lane._service_scope = None
        lane._service_instance = 'test'
        lane._unanswered = {}
        lane._earlier_held = 1
        self.store._orchestration_owner = mock.Mock(return_value=mock.Mock())
        self.store.settle_earlier_registrations = mock.Mock()
        with mock.patch.object(self.store, 'attest_terminal_delivery', side_effect=[HistoryError('history_service_unavailable'), b'evidence']) as attest:
            for _ in range(2):
                request = types.SimpleNamespace(kind='expire')
                lane._earlier_held = 1
                lane._earlier(request)
                self.assertEqual('done', request.state)
            self.assertEqual('committed', pending.state)
            self.assertEqual(2, attest.call_count)
        self.assertEqual(b'evidence', pending.terminal_evidence)
        self.assertIsNone(self.material())

    def test_P1_live_lane_file_fault_retries_the_identical_selected_packet(self):
        self.bound()
        body, content = self.packet()
        record = json.loads(content)
        packet = types.SimpleNamespace(body=body, report=None, fenced=record['fenced'], reviewed=record['reviewed'],
                                       terminal_state=record['terminal_state'], validation_reason=record.get('validation_reason'))
        selected = types.SimpleNamespace(packet=packet)
        observed_at_ms = self.now
        pending = types.SimpleNamespace(binding=claim_binding(), assignment=canonical_json(assignment()),
                                        selected=selected, observed_at_ms=observed_at_ms, grant=None,
                                        state='queued', delivery=None, terminal_evidence=None, reason=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock = threading.RLock()
        handle = result_persistence.ResultPersistenceHandle()
        lane._jobs = {handle: pending}
        lane._closed, lane._fault = False, None
        lane._pid, lane._thread = os.getpid(), threading.current_thread()
        lane._owner = mock.Mock()
        lane._incoming = queue.Queue()
        lane._history = lambda: self.store
        with self.fail_at('fsync_directory', OSError('transient')):
            with self.assertRaises(HistoryError):
                lane._publish(pending)
        pending.state = 'failed'
        before = self.prepared()[:2]
        kept = self.material()
        self.now += 50
        lane.retry(handle)
        self.assertEqual(('publish', handle), lane._incoming.get_nowait())
        self.assertIs(selected, pending.selected)
        with mock.patch.object(self.store, 'publish_result', wraps=self.store.publish_result) as publish:
            lane._publish(pending)
        self.assertIs(body, publish.call_args.args[2])
        self.assertEqual(observed_at_ms, publish.call_args.args[3].observed_at_ms)
        self.assertEqual(before, self.prepared()[:2])
        self.assertEqual(keyed_digest(KEY, kept), self.prepared()[0])
        self.assertEqual(('committed', None, (1, 1, 1)), (pending.state, pending.selected, self.counts()))

    def test_L3_arbitration_adopts_once_and_an_equal_repeat_preserves_all_evidence(self):
        self.strand()
        pending = types.SimpleNamespace(binding=claim_binding(), state='failed', delivery=None, grant=object(),
                                        adoption_pending=False, selected='kept', reason='history_service_unavailable',
                                        terminal_evidence=None)
        lane = object.__new__(result_persistence.ResultPersistenceLane)
        lane._lock = threading.RLock()
        lane._jobs = {'live': pending}
        lane._history = lambda: self.store
        lane._unanswered = {}
        effective = []
        install = lane._install_delivery

        def installed(job, delivery, evidence):
            before = job.delivery
            install(job, delivery, evidence)
            if before is None and job.delivery is not None:
                effective.append(job.binding.claim_id)

        with mock.patch.object(lane, '_install_delivery', side_effect=installed), \
                mock.patch.object(self.store, 'attest_terminal_delivery', return_value=b'evidence'):
            for _ in range(2):
                lane._earlier_held = 1
                request = types.SimpleNamespace(kind='arbitrate', arguments=(*self.attempt(), 'arb'))
                lane._earlier(request)
                self.assertEqual(('done', 'recovered'), (request.state, request.result))
            delivery, evidence = pending.delivery, pending.terminal_evidence
            selected = object()
            pending.selected = selected
            equal = dataclasses.replace(delivery)
            lane._install_delivery(pending, equal, b'different evidence')
        self.assertEqual([self.claim()], effective)
        self.assertIs(delivery, pending.delivery)
        self.assertIs(evidence, pending.terminal_evidence)
        self.assertIs(selected, pending.selected)
        self.assertEqual((1, 1, 1), self.counts())

    def test_driver_observer_rejects_a_later_uncommitted_or_incomplete_settlement(self):
        from tests.test_publication_replay_driver import StoppedInstanceOrderTests
        observer = StoppedInstanceOrderTests(methodName='runTest')
        observer.store_rows = lambda: dict(terminals=1, usage=1, material=0, states=['committed'])
        bad_later = [('settle', ('committed',), 'target'), ('settled', 'target'),
                     ('settle', ('reserved',), 'target'), ('settled', 'target')]
        incomplete = [('settle', ('committed',), 'target')]
        for events in (bad_later, incomplete):
            with self.subTest(events=events), self.assertRaises(AssertionError):
                observer.check(events, 'target')
        observer.check([('settle', ('committed',), 'target'), ('settled', 'target')], 'target')

    def test_driver_post_fsync_fault_signal_is_set_only_when_the_fault_fires(self):
        from tests.test_publication_replay_driver import HeldLiveCommit
        self.bound()
        with HeldLiveCommit() as held:
            self.assertFalse(held.failed.is_set())
            with self.assertRaises(HistoryError):
                self.commit()
            self.assertTrue(held.failed.is_set())
            self.assertEqual(1, held.live_attempts)
        self.assertEqual('reserved', self.prepared()[2])
        self.assertEqual(self.packet()[0], (self.fixture.directory / self.store._result_names(claim_binding())[1]).read_bytes())

    def test_missing_lease_hold_blocks_live_commit_without_changing_custody(self):
        self.strand()
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='reservation' AND identity=?", (self.claim(),))
        before = (self.prepared(), self.material(), self.files())
        with self.assertRaises(HistoryError) as caught:
            self.commit()
        self.assertEqual('history_result_expired', caught.exception.reason_code)
        self.assertEqual(before, (self.prepared(), self.material(), self.files()))
        self.assertEqual((0, 0, 0), self.counts())

    def test_zero_row_release_requires_a_verified_existing_fence(self):
        self.strand()
        self.store._connection.execute('DELETE FROM project_result_publications WHERE claim_id=?', (self.claim(),))
        # A zero-row update alone proves no fence. Simulate that outcome inside the actual transaction.
        self.store._connection.execute("CREATE TEMP TRIGGER ignore_release BEFORE UPDATE OF state ON project_holds "
                                       "WHEN NEW.kind='reservation' AND NEW.state='releasing' "
                                       "BEGIN SELECT RAISE(IGNORE); END")
        with self.assertRaises(HistoryError) as caught:
            self.store.arbitrate_publication(*self.attempt(), 'no-fence')
        self.assertEqual('history_service_unavailable', caught.exception.reason_code)
        self.assertEqual(('held', None, None), self.obligation())
        self.assertEqual((0, 0, 0), self.counts())


if __name__ == '__main__':
    unittest.main()
