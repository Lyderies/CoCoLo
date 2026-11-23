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
import dataclasses
import hashlib
import json
import os
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_deadlines_retention as dr
from tests import test_record_context as rc
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_project_history import binding

from codex_agents import project_history, project_holds_schema, project_result_contexts
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
                self.commit(number)  # the retry, from the same packet
                last = self.prepared(number)
                self.assertEqual(first[:2], last[:2])  # prepared_digest and retained_until_ms unchanged
                self.assertEqual(('committed', None), (last[2], self.material(number)))
                self.assertEqual(number, self.counts()[0])  # one terminal per attempt
                self.assertEqual((number, number), self.counts()[1:])  # one dispatch_result and one usage record each
                self.assertEqual(self.packet(number)[0], self.store.read_result(claim_binding(number)))
        self.consistent()
        # The run then releases normally: retention ends it.
        self.now = self.reservation(1).retained_until_ms
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
        with self.assertRaises(HistoryError):
            self.store.commit_result(canonical_json(other), claim_binding(1), assignment(), body,
                                     client_usage=self.usage())
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

    def test_R6_an_envelope_that_does_not_hash_to_the_prepared_digest_is_refused(self):
        self.strand(1)
        self.store._connection.execute("UPDATE project_result_publications SET material=? WHERE claim_id=?",
                                       (self.material() + b' ', self.claim()))
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

    def test_R6_an_altered_body_is_refused_and_an_absent_usage_record_is_a_replay_without_one(self):
        self.strand(1)
        path = self.fixture.directory / self.store._result_names(claim_binding(1))[1]
        body = path.read_bytes()
        path.write_bytes(body[:-1] + (b'X' if body[-1:] != b'X' else b'Y'))
        self.refused()
        self.strand(2)
        self.plant(2, lambda data: data.pop('client_usage_record'))
        self.assertEqual([self.claim(2)], self.store.replay_publications())
        self.assertEqual((1, 1, 0), self.counts())  # `no observation was supplied`, never `unavailable`

    def test_R4prime_a_live_publication_racing_a_settlement_loses_in_both_commit_orders(self):
        # (i) the settlement's arbitration is first: the live publication is then refused.
        self.bound(1)
        with self.fail_at('fsync_directory', OSError('transient')):
            with self.assertRaises(HistoryError):
                self.commit(1)
        self.assertEqual('recovered', self.store.arbitrate_publication(*self.attempt(1), 'lost-1'))
        self.settle_lost(1)
        self.assertEqual((1, 1, 1), self.counts())  # the body was committed before the settlement
        # (ii) a settlement that found nothing to recover: no terminal after it.
        self.bound(2)
        with self.fail_at('write', OSError('transient')):
            with self.assertRaises(HistoryError):
                self.commit(2)
        self.assertEqual('unrecoverable', self.store.arbitrate_publication(*self.attempt(2), 'lost-2'))
        with self.assertRaises(HistoryError):
            self.commit(2)
        self.assertEqual((1, 1, 1), self.counts())
        self.assertEqual('reserved', self.prepared(2)[2])
        self.assertEqual('releasing', self.obligation(2)[0])
        self.consistent()


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
        self.store._connection.execute("DELETE FROM project_result_publications WHERE claim_id=?", (self.claim(1),))
        self.strand(2)
        path = self.fixture.directory / self.store._result_names(claim_binding(2))[1]
        path.write_bytes(b'not the body')
        for number in (1, 2):
            with self.subTest(number=number):
                self.assertEqual('unrecoverable',
                                 self.store.arbitrate_publication(*self.attempt(number), 'lost-%d' % number))
                self.assertEqual(('releasing', None, 'lost-%d' % number), self.obligation(number))
                with self.assertRaises(HistoryError):  # a later live commit
                    self.commit(number)
                with self.assertRaises(HistoryError):  # and a later replay
                    self.store.replay_publication(claim_binding(number))
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

    def test_an_attempt_with_no_reservation_has_nothing_to_arbitrate(self):
        self.assertEqual('none', self.store.arbitrate_publication('hrn_' + 'a' * 48, 1, 'lost-x'))


if __name__ == '__main__':
    unittest.main()
