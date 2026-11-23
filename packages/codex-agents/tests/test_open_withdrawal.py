"""Slice B3: `account.withdraw_open` from the project (PLAN-0.4.0-supervisor-slices.md B3; DESIGN-0.4.0-RS-B.md
draft 2 §8, draft 3 §4 and §7; DESIGN-0.4.0-RS-A.md F13).

A run that ends without its claim being sent (refused at preparation, cancelled before its claim, launch refused,
its registered run left idle, its service stopped) withdraws its open at the account. The request is authenticated
as the project's service for the run's host class (or as the host that opened it), bound to the binding's
generation and to the open's request digest, idempotent, refused plainly for an open that was claimed, fenced or
already withdrawn, and releases the binding's register row `withdrawn` in the same account transaction. The project
records its side on its own `open_binding` register row, and never withdraws an open whose lease was armed: the
fence owns that one.

Rows of the RS-B matrix (draft 3 §14.2): W1, W2, W3, W4a-W4e, W5. Unit stage for the wire and the store; the
rest is the integration stage (the in-process collector, account and project services, the real CLI).
"""
from contextlib import closing
import dataclasses
import hashlib
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_leases as leases
from tests.learning_support import attest_provider
from tests.test_bridge import CLEAN, git, null_filled, result
from tests.test_project_driver import DriverHarness
from tests.test_project_history import PROJECT_A, binding as history_binding

from codex_agents import codex_preparation, project_driver, service_transport
from codex_agents.account_api import (
    ACCOUNT_PROTOCOL_VERSION, AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_request, decode_account_response, encode_account_request,
)
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.history_contracts import HistoryError
from codex_agents.hosts import CLAUDE_HOST_CLIENT, CODEX_HOST_CLIENT, OWNER_CLIENT
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import LearningLedger
from codex_agents.open_withdrawal import (
    WITHDRAW_ERROR_CODES, WITHDRAW_REASONS, WithdrawError, WithdrawOpenInput, checked_withdraw_reply,
    custody_matches, decode_withdraw_open, withdrawal_custody, withdrawal_request_id,
)
from codex_agents.refusal_steps import DEFAULT, next_step
from codex_agents.service_enrollment import (
    account_endpoint_path, enrollment_preamble, proof_directory, read_capability, withdraw_proof,
)

HANDLE = 'hrn_' + '1' * 48
RUN_ID = 'run:' + '2' * 64
INPUTS = '3' * 64
PROJECT = 'prj_' + '4' * 32


def binding(number: int = 1):
    """test_project_history's attempt binding, with the canonical run id (`run:<64 hex>`) the wire requires."""
    return dataclasses.replace(history_binding(number),
                               run_id='run:' + hashlib.sha256(f'run-{number}'.encode()).hexdigest())


def withdrawal(**changes):
    values = dict(run_handle=HANDLE, attempt=1, run_id=RUN_ID, inputs_digest=INPUTS, generation=1,
                  reason='cancelled_before_claim')
    values.update(changes)
    return WithdrawOpenInput(**values)


@__import__('test_framework').requirements('RS-B-5', why_not_scenario='B3 checks the real codec, store, or enrolled service path; direct fault and custody controls expose the withdrawal boundary.')
class WithdrawWireTests(unittest.TestCase):
    """W1 (the encode and decode half), W4e (the reply check), the fixed request id and the reason codes."""

    def request(self, value=None, *, request_id=None):
        value = value or withdrawal()
        return AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open',
                              request_id or withdrawal_request_id(value.run_handle, value.attempt), PROJECT,
                              COLLECTOR_RUNTIME_DIGEST, value.payload())

    def test_W1_the_request_round_trips_through_the_account_codec(self):
        request = self.request()
        decoded = decode_account_request(encode_account_request(request))
        self.assertEqual(withdrawal(), decode_withdraw_open(decoded.payload))
        self.assertEqual(account_request_digest(request), account_request_digest(decoded))
        self.assertEqual({'run_handle', 'attempt', 'run_id', 'inputs_digest', 'generation', 'reason'},
                         set(request.payload))

    def test_W1_the_payload_is_closed(self):
        good = withdrawal().payload()
        for bad in ({**good, 'extra': 1}, {k: v for k, v in good.items() if k != 'generation'},
                    {**good, 'reason': 'because'}, {**good, 'generation': 0}, {**good, 'generation': True},
                    {**good, 'generation': '1'}, {**good, 'attempt': 0}, {**good, 'run_handle': 'hrn_x'},
                    {**good, 'run_id': 'run-1'}, {**good, 'inputs_digest': 'A' * 64}):
            with self.subTest(payload=bad), self.assertRaises(AccountError) as caught:
                encode_account_request(AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open', 'withdraw-1',
                                                      PROJECT, COLLECTOR_RUNTIME_DIGEST, bad))
            self.assertEqual('account_invalid_request', caught.exception.reason_code)
        # W4e: an older client may send no generation; the wire carries it and the account answers for it.
        self.assertIsNone(decode_withdraw_open({**good, 'generation': None}).generation)

    def test_the_reasons_are_a_fixed_set(self):
        self.assertEqual(('refused_before_claim', 'cancelled_before_claim', 'failed_before_claim',
                          'registered_idle', 'service_stopped'), WITHDRAW_REASONS)

    def test_the_request_id_is_fixed_per_attempt(self):
        first = withdrawal_request_id(HANDLE, 1)
        self.assertEqual(first, withdrawal_request_id(HANDLE, 1))
        self.assertNotEqual(first, withdrawal_request_id(HANDLE, 2))
        self.assertRegex(first, r'^withdraw-[0-9a-f]{32}$')

    def test_the_custody_names_exactly_the_payloads_run(self):
        custody = json.loads(withdrawal_custody(withdrawal()))
        self.assertEqual({'purpose': 'withdraw', 'run_handle': HANDLE, 'attempt': 1, 'run_id': RUN_ID,
                          'inputs_digest': INPUTS}, custody)

    def test_a_custody_past_its_byte_limit_is_refused_and_the_exact_custody_is_accepted(self):
        exact = withdrawal_custody(withdrawal())
        self.assertTrue(custody_matches(exact, withdrawal()))
        padded = exact[:-1] + b',"pad":"' + b'x' * 1_100 + b'"}'
        self.assertGreater(len(padded), 1_024)
        self.assertFalse(custody_matches(padded, withdrawal()))

    def reply(self, **changes):
        return {'state': 'withdrawn', 'run_handle': HANDLE, 'receipt': '5' * 64, 'known': False, **changes}

    def test_W4e_a_reply_for_another_handle_is_rejected(self):
        self.assertEqual(self.reply(), checked_withdraw_reply(self.reply(), withdrawal()))
        for bad in (self.reply(run_handle='hrn_' + '9' * 48), self.reply(state='claimed'), self.reply(known=1),
                    self.reply(receipt='x'), {**self.reply(), 'extra': None}):
            with self.subTest(reply=bad), self.assertRaises(WithdrawError):
                checked_withdraw_reply(bad, withdrawal())
        request = self.request()
        raw = canonical_json({'protocol_version': 1, 'request_id': request.request_id, 'project_id': PROJECT,
                              'runtime_digest': COLLECTOR_RUNTIME_DIGEST,
                              'request_digest': account_request_digest(request), 'ready': True,
                              'result': self.reply(run_handle='hrn_' + '9' * 48), 'reason_code': None})
        with self.assertRaises(AccountError) as caught:
            decode_account_response(raw, request)
        self.assertEqual('account_invalid_response', caught.exception.reason_code)

    def test_each_refusal_has_a_reason_code_and_a_next_step(self):
        self.assertEqual({'withdraw_invalid', 'withdraw_unavailable', 'withdraw_stale',
                          'withdraw_generation_required'}, set(WITHDRAW_ERROR_CODES))
        for code in WITHDRAW_ERROR_CODES:
            with self.subTest(code=code):
                self.assertNotEqual(DEFAULT, next_step(code))


def open_row(db, run_handle, attempt=1, project=PROJECT_A):
    row = db.execute("SELECT state,release_kind,request_id,request_digest,evidence FROM project_holds "
                     "WHERE project_id=? AND kind='open_binding' AND identity=?",
                     (project, f'{run_handle}:{attempt}')).fetchone()
    return None if row is None else tuple(row)


class _CountedCursor:
    def __init__(self, cursor, owner):
        self._cursor, self._owner = cursor, owner

    def __iter__(self):
        for row in self._cursor:
            self._owner.rows += 1
            yield row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._owner.rows += len(rows)
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        self._owner.rows += row is not None
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountedConnection:
    """Counts the rows read from the open-binding register by anything but a lookup of one identity."""

    def __init__(self, connection):
        self._connection, self.rows = connection, 0

    def execute(self, sql, *arguments):
        cursor = self._connection.execute(sql, *arguments)
        if 'open_binding' in sql and 'identity=?' not in sql.replace(' ', ''):
            return _CountedCursor(cursor, self)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


@__import__('test_framework').requirements('RS-B-5', why_not_scenario='B3 checks the real codec, store, or enrolled service path; direct fault and custody controls expose the withdrawal boundary.')
class OpenBindingStoreTests(unittest.TestCase):
    """The project's `open_binding` register row (draft 3 §7): taken before registration, marked `releasing` with its
    withdrawal request before the account is asked, released by that request's answer or by the claim's own end, and
    never marked for a lease that was armed."""

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    @property
    def store(self):
        return self.base.store

    def row(self, number=1):
        return open_row(self.store._connection, binding(number).run_handle)

    def record(self, number=1, run_id=None):
        self.store.record_open_binding(binding(number).run_handle, 1, run_id or binding(number).run_id, INPUTS,
                                       'codex', instance='svc-' + 'a' * 32, now_ms=1000)

    def test_the_row_is_taken_held_and_a_replay_is_a_no_op(self):
        self.record()
        self.assertEqual(('held', None, None, None), self.row()[:4])
        evidence = json.loads(self.row()[4])
        self.assertEqual((binding(1).run_id, INPUTS, 'codex'),
                         (evidence['run_id'], evidence['inputs_digest'], evidence['host_client']))
        self.record()
        self.assertEqual(('held', None, None, None), self.row()[:4])
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.record(run_id='run-other')

    def test_marking_writes_the_request_before_it_is_sent(self):
        self.record()
        marked = self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'cancelled_before_claim',
                                                  project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        state, kind, request_id, request_digest, evidence = self.row()
        self.assertEqual(('releasing', None), (state, kind))
        self.assertEqual(withdrawal_request_id(binding(1).run_handle, 1), request_id)
        expected = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open', request_id, PROJECT,
                                  COLLECTOR_RUNTIME_DIGEST, WithdrawOpenInput(
                                      binding(1).run_handle, 1, binding(1).run_id, INPUTS, 1,
                                      'cancelled_before_claim').payload())
        self.assertEqual(account_request_digest(expected), request_digest)
        self.assertEqual(request_digest, marked['request_digest'])
        # A second mark (another exit of the same run) keeps the first request: one request per attempt.
        self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'failed_before_claim',
                                         project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        self.assertEqual(request_digest, self.row()[3])
        self.assertEqual('cancelled_before_claim', json.loads(self.row()[4])['reason'])

    def test_an_armed_lease_is_never_withdrawn(self):
        self.base.reserve(1)
        self.record()
        self.store.arm_result_lease(leases.lease_binding(1))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'failed_before_claim',
                                             project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        self.assertEqual('held', self.row()[0])

    def test_an_unarmed_lease_withdrawn_locally_lets_the_open_be_withdrawn(self):
        self.base.reserve(1)
        self.record()
        self.store.release_unsent_result_lease(leases.lease_binding(1))
        self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'failed_before_claim',
                                         project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        self.assertEqual('releasing', self.row()[0])
        self.assertEqual([(f'{binding(1).run_handle}:1', withdrawal_request_id(binding(1).run_handle, 1))],
                         [(entry['identity'], entry['request_id']) for entry in self.store.open_withdrawals()])

    def test_the_answer_releases_the_row_once(self):
        self.record()
        self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'cancelled_before_claim',
                                         project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        self.store.release_open_binding(binding(1).run_handle, 1, 'withdrawn', receipt='6' * 64)
        self.assertEqual(('released', 'withdrawn'), self.row()[:2])
        self.assertEqual([], self.store.open_withdrawals())
        self.store.release_open_binding(binding(1).run_handle, 1, 'withdrawn', receipt='6' * 64)  # a replay
        self.assertEqual(('released', 'withdrawn'), self.row()[:2])
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.release_open_binding(binding(1).run_handle, 1, 'account_final')

    def test_a_bound_claim_releases_the_row_claimed_in_its_transaction(self):
        self.base.reserve(1)
        self.record()
        self.store.arm_result_lease(leases.lease_binding(1))
        self.base.bind(1)
        self.assertEqual(('released', 'claimed'), self.row()[:2])

    def test_a_stopped_services_open_is_marked_by_the_successor(self):
        self.record()  # instance svc-aaaa..., which is not this one
        self.base.reserve(2)
        self.record(2)
        self.store.arm_result_lease(leases.lease_binding(2))  # armed: the fence's, never withdrawn
        self.assertEqual(1, self.store.withdraw_earlier_open_bindings(
            'svc-' + 'b' * 32, project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST))
        self.assertEqual('releasing', self.row(1)[0])
        self.assertEqual('service_stopped', json.loads(self.row(1)[4])['reason'])
        self.assertEqual('held', self.row(2)[0])
        # The live instance's own rows are untouched.
        self.base.reserve(3)
        self.store.record_open_binding(binding(3).run_handle, 1, binding(3).run_id, INPUTS, 'codex',
                                       instance='svc-' + 'b' * 32, now_ms=1000)
        self.assertEqual(0, self.store.withdraw_earlier_open_bindings(
            'svc-' + 'b' * 32, project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST))
        self.assertEqual('held', self.row(3)[0])


    def counted(self):
        counted = _CountedConnection(self.store._connection)
        patcher = mock.patch.object(self.store, '_connection', counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counted

    def test_M2_the_successor_pass_reads_at_most_its_limit_of_rows_and_every_row_is_handled(self):
        for number in range(1, 26):
            self.record(number)  # an earlier service's: all to be withdrawn
        for number in range(26, 34):  # the live service's own: never touched
            self.store.record_open_binding(binding(number).run_handle, 1, binding(number).run_id, INPUTS, 'codex',
                                           instance='svc-' + 'b' * 32, now_ms=1000)
        for number in (34, 35):  # an earlier service's, but armed: the fence's, never withdrawn
            self.base.reserve(number)
            self.record(number)
            self.store.arm_result_lease(leases.lease_binding(number))
        counted, handled = self.counted(), 0
        for _ in range(10):
            counted.rows = 0
            marked = self.store.withdraw_earlier_open_bindings(
                'svc-' + 'b' * 32, project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST, limit=10)
            self.assertLessEqual(counted.rows, 10)  # bounded by the limit, not by the rows held
            self.assertLessEqual(marked, 10)
            handled += marked
            if not marked:
                break
        self.assertEqual(25, handled)
        states = {number: self.row(number)[0] for number in range(1, 36)}
        self.assertEqual({'releasing'}, {states[number] for number in range(1, 26)})
        self.assertEqual({'held'}, {states[number] for number in range(26, 36)})

    def test_M2_a_registration_reads_one_row_however_many_are_held_and_still_refuses_a_second_owner(self):
        for number in range(1, 31):
            self.record(number)
        counted = self.counted()
        self.store.record_open_binding(binding(40).run_handle, 1, binding(40).run_id, INPUTS, 'codex',
                                       instance='svc-' + 'a' * 32, now_ms=1000)
        self.assertLessEqual(counted.rows, 1)
        counted.rows = 0
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):  # the complete run-id check is kept
            self.store.record_open_binding(binding(41).run_handle, 1, binding(17).run_id, INPUTS, 'codex',
                                           instance='svc-' + 'a' * 32, now_ms=1000)
        self.assertLessEqual(counted.rows, 1)
        self.assertIsNone(self.row(41))

    def test_M1_the_withdrawal_list_returns_at_most_its_limit_and_the_rest_follow_as_these_are_released(self):
        for number in range(1, 13):
            self.record(number)
            self.store.mark_open_withdrawing(binding(number).run_handle, 1, 'cancelled_before_claim',
                                             project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        page = self.store.open_withdrawals(now_ms=5000, limit=5)
        self.assertEqual(5, len(page))  # past the limit: only a page is returned
        self.assertEqual(12, len(self.store.open_withdrawals(now_ms=5000, limit=50)))
        for entry in page:
            self.store.defer_open_withdrawal(entry['identity'], now_ms=10_000, base_ms=1000, max_ms=5000)
        later = {entry['identity'] for entry in self.store.open_withdrawals(now_ms=5000, limit=5)}
        self.assertFalse(later & {entry['identity'] for entry in page})  # the deferred rows no longer crowd it out

    def test_M1_a_withdrawal_is_listed_only_when_due_and_each_failure_waits_twice_as_long(self):
        for number in (1, 2):
            self.record(number)
            self.store.mark_open_withdrawing(binding(number).run_handle, 1, 'cancelled_before_claim',
                                             project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        first, second = (f'{binding(number).run_handle}:1' for number in (1, 2))
        self.assertEqual({first, second}, {entry['identity'] for entry in self.store.open_withdrawals(now_ms=5000)})
        for failures, delay in ((1, 1000), (2, 2000), (3, 4000), (4, 5000), (5, 5000)):  # capped at the maximum
            self.store.defer_open_withdrawal(first, now_ms=10_000, base_ms=1000, max_ms=5000)
            self.assertEqual(10_000 + delay, self._next_check(first))
            self.assertEqual(failures, json.loads(self._evidence(first))['failures'])
            due = [entry['identity'] for entry in self.store.open_withdrawals(now_ms=10_000 + delay - 1)]
            self.assertEqual([second], due)  # the failing row waits, the other is still listed
            due = [entry['identity'] for entry in self.store.open_withdrawals(now_ms=10_000 + delay)]
            self.assertEqual({first, second}, set(due))
        self.assertEqual('releasing', self.row(1)[0])  # a failure never finalizes a row

    # --- decision 108 A: database work, not returned rows ---

    HELD = "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,evidence) " \
           "VALUES(?,'open_binding',?,'held',1000,?)"

    def fill(self, earlier, own, start=1000):
        """Held open-binding rows written straight into the register: `earlier` of a stopped service, `own` of the live one."""
        from codex_agents import project_holds_schema
        rows = []
        for number in range(start, start + earlier + own):
            handle = 'hrn_' + f'{number:048x}'
            instance = 'svc-' + ('a' if number < start + earlier else 'b') * 32
            rows.append((PROJECT_A, f'{handle}:1', project_holds_schema.evidence_json(
                run_handle=handle, attempt=1, run_id='run:' + f'{number:064x}', inputs_digest=INPUTS,
                host_client='codex', instance=instance)))
        with self.store._connection:
            self.store._connection.executemany(self.HELD, rows)

    def steps(self, action):
        """The database work of one call: SQLite virtual-machine steps (a returned-row count cannot show it)."""
        count = [0]

        def tick():
            count[0] += 1
            return 0
        self.store._connection.set_progress_handler(tick, 1)
        try:
            action()
        finally:
            self.store._connection.set_progress_handler(None, 0)
        return count[0]

    def plans(self, action):
        """The query plan of every statement `action` ran against the register or the registered runs."""
        seen = []
        self.store._connection.set_trace_callback(seen.append)
        try:
            action()
        finally:
            self.store._connection.set_trace_callback(None)
        details = []
        for sql in seen:
            if sql.lstrip().upper().startswith('SELECT') and ('project_holds' in sql or 'project_registered_runs' in sql):
                details += [row[3] for row in self.store._connection.execute('EXPLAIN QUERY PLAN ' + sql)]
        return details

    def sweep(self, instance='svc-' + 'b' * 32, limit=10):
        return self.store.withdraw_earlier_open_bindings(instance, project_id=PROJECT,
                                                         runtime_digest=COLLECTOR_RUNTIME_DIGEST, limit=limit)

    def register(self, number):
        self.store.record_open_binding('hrn_' + f'{number:048x}', 1, 'run:' + f'{number:064x}', INPUTS, 'codex',
                                       instance='svc-' + 'a' * 32, now_ms=1000)

    def test_D108_registration_searches_its_indexes_and_its_work_does_not_grow_with_the_rows_held(self):
        self.fill(30, 0)
        small = self.steps(lambda: self.register(1))
        self.fill(1500, 0, start=5000)
        with self.store._connection:
            self.store._connection.executemany(
                'INSERT INTO project_registered_runs VALUES(?,?,?,?,?,?,?,NULL,NULL)',
                [(PROJECT_A, 'hrn_' + f'{n:048x}', 1, 'orc_' + f'{n:032x}', 'run:' + f'{n:064x}', 'svc-' + 'a' * 32, 1)
                 for n in range(20_000, 21_500)])
        large = self.steps(lambda: self.register(2))
        self.assertLess(large, small * 1.5 + 200)  # the work of one registration, not of the rows held
        details = self.plans(lambda: self.register(3))
        self.assertTrue(details)
        self.assertFalse([d for d in details if d.startswith('SCAN') and 'project_' in d], details)
        self.assertFalse([d for d in details if 'project_holds_kind_due' in d], details)  # not the whole live partial index
        self.assertTrue([d for d in details if 'project_registered_runs' in d and 'USING' in d and 'run' in d
                         and 'orchestration' not in d], details)  # a run-id index

    def test_D108_the_successor_pass_does_bounded_work_and_advances_across_conflicting_rows(self):
        self.fill(25, 25)
        baseline = self.steps(self.sweep)
        details_small = self.plans(self.sweep)
        self.fill(900, 900, start=9000)
        first = self.steps(self.sweep)
        self.assertLess(first, baseline * 1.5 + 200)  # bounded by the limit, not by the rows held
        details = self.plans(self.sweep)
        self.assertFalse([d for d in details + details_small if d.startswith('SCAN') and 'project_holds' in d], details)
        self.assertFalse([d for d in details + details_small if 'TEMP B-TREE' in d], details)  # no unbounded sort
        # Rows the pass cannot mark (their lease says so) never stop it from reaching the others.
        rows = [row[0] for row in self.store._connection.execute(
            "SELECT identity FROM project_holds WHERE kind='open_binding' AND state='held' "
            "AND json_extract(evidence,'$.instance')=? ORDER BY identity LIMIT 120", ('svc-' + 'a' * 32,))]
        stuck, original = set(rows), self.store.mark_open_withdrawing

        def conflicting(run_handle, attempt, *args, **kwargs):
            if f'{run_handle}:{attempt}' in stuck:
                raise HistoryError('history_conflict')
            return original(run_handle, attempt, *args, **kwargs)
        waiting = lambda: self.store._connection.execute(  # noqa: E731
            "SELECT COUNT(*) FROM project_holds WHERE kind='open_binding' AND state='held' "
            "AND json_extract(evidence,'$.instance')=?", ('svc-' + 'a' * 32,)).fetchone()[0]
        worst = 0
        with mock.patch.object(self.store, 'mark_open_withdrawing', conflicting):
            for _ in range(200):
                if waiting() == len(stuck):
                    break
                worst = max(worst, self.steps(self.sweep))
        self.assertEqual(len(stuck), waiting())  # every row that can be marked was, behind the conflicting ones
        self.assertLess(worst, baseline * 3 + 400)  # and no call did more than a bounded share of the work

    def test_D108_the_run_id_of_an_open_or_a_registered_run_cannot_be_taken_twice(self):
        self.register(1)
        with self.store._connection:
            self.store._connection.execute(
                'INSERT INTO project_registered_runs VALUES(?,?,?,?,?,?,?,NULL,NULL)',
                (PROJECT_A, 'hrn_' + f'{7:048x}', 1, 'orc_' + '7' * 32, 'run:' + f'{7:064x}', 'svc-' + 'a' * 32, 1))
        for number, owner in ((9, 1), (10, 7)):
            with self.subTest(owner=owner), self.assertRaisesRegex(HistoryError, 'history_conflict'):
                self.store.record_open_binding('hrn_' + f'{number:048x}', 1, 'run:' + f'{owner:064x}', INPUTS, 'codex',
                                               instance='svc-' + 'a' * 32, now_ms=1000)

    def test_D108_a_format_18_project_store_upgrades_to_19_and_keeps_its_rows_and_its_refusals(self):
        from codex_agents import project_history
        self.assertEqual(20, project_history._SCHEMA_VERSION)
        self.fill(5, 0)
        self.register(1)
        db = self.store._connection
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        added = {name for name in indexes if name in {'project_holds_open_run', 'project_holds_open_held',
                                                      'project_registered_runs_run'}}
        self.assertEqual({'project_holds_open_run', 'project_holds_open_held', 'project_registered_runs_run'}, added)
        before = sorted(tuple(row) for row in db.execute('SELECT * FROM project_holds'))
        for name in added:  # the format 18 never had these
            db.execute('DROP INDEX ' + name)
        db.execute('DROP TABLE project_result_recorders')  # schema20 (B2, decision 114.1 A): format 18 never had it
        db.execute('PRAGMA user_version=18')
        self.store.close()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):  # an ordinary start never upgrades
            self.base.fixture.open()
        self.base.fixture.store = self.base.fixture.open(allow_schema_upgrade=True)
        db = self.store._connection
        self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(added, {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")} & added)
        self.assertEqual(before, sorted(tuple(row) for row in db.execute('SELECT * FROM project_holds')))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.record_open_binding('hrn_' + f'{99:048x}', 1, 'run:' + f'{1:064x}', INPUTS, 'codex',
                                           instance='svc-' + 'a' * 32, now_ms=1000)
        self.assertEqual(6, self.sweep())  # the rows written before the upgrade are read and handled

    # --- decision 111 A: the successor window is bounded by the window, not by the rows before the position ---

    def _window_after_prefix(self, prefix):
        """Database work of one window call whose position has `prefix` rows of another service behind it."""
        owner = 'svc-' + 'a' * 32
        identity = f"{'hrn_' + f'{100_000 + 49:048x}'}:1"  # the 50th row of the first fill
        return self.steps(lambda: self.store._held_window('svc-' + 'b' * 32, (owner, identity), 10))

    def test_D111_the_successor_window_work_does_not_grow_with_the_rows_before_its_position(self):
        self.fill(70, 5, start=100_000)  # 50 rows behind the position, 20 after, and the live service's own
        small = self._window_after_prefix(50)
        self.fill(2000, 0, start=1000)  # 2,000 more rows of the same service, all before the position
        large = self._window_after_prefix(2050)
        self.assertLess(large, small * 1.5 + 200, (small, large))  # the window's work, not the rows passed

    def test_D111_the_windows_plans_name_the_open_binding_indexes_exactly(self):
        self.fill(5, 0)
        register = self.plans(lambda: self.register(50))
        self.assertTrue([d for d in register if 'INDEX project_holds_open_run' in d], register)
        self.assertTrue([d for d in register if 'INDEX project_registered_runs_run' in d], register)
        sweep = self.plans(self.sweep)
        self.assertTrue([d for d in sweep if 'INDEX project_holds_open_held' in d], sweep)

    def test_D111_a_row_with_a_real_armed_lease_is_refused_and_the_window_moves_on(self):
        self.base.reserve(1)
        self.record(1)
        self.store.arm_result_lease(leases.lease_binding(1))
        self.record(2)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.mark_open_withdrawing(binding(1).run_handle, 1, 'service_stopped',
                                             project_id=PROJECT, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        self.assertEqual('held', self.row(1)[0])
        first = self.store.withdraw_earlier_open_bindings('svc-' + 'b' * 32, project_id=PROJECT,
                                                          runtime_digest=COLLECTOR_RUNTIME_DIGEST, limit=1)
        second = self.store.withdraw_earlier_open_bindings('svc-' + 'b' * 32, project_id=PROJECT,
                                                           runtime_digest=COLLECTOR_RUNTIME_DIGEST, limit=1)
        self.assertEqual((0, 1), (first, second))  # the armed row is passed over, the next one is marked
        self.assertEqual(('held', 'releasing'), (self.row(1)[0], self.row(2)[0]))

    def _next_check(self, identity):
        return self.store._connection.execute("SELECT next_check_ms FROM project_holds WHERE kind='open_binding' "
                                              "AND identity=?", (identity,)).fetchone()[0]

    def _evidence(self, identity):
        return self.store._connection.execute("SELECT evidence FROM project_holds WHERE kind='open_binding' "
                                              "AND identity=?", (identity,)).fetchone()[0]


class WithdrawReads:
    """The account's binding and register rows, read beside the running services (a mixin for a driver harness)."""

    def setUp(self):
        self.drivers = []
        init = project_driver.RunDriver.__init__

        def recorded(driver, *args, **kwargs):
            init(driver, *args, **kwargs)
            self.drivers.append(driver)
        patcher = mock.patch.object(project_driver.RunDriver, '__init__', recorded)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def account_db(self):
        return closing(sqlite3.connect(f"file:{self.state.root / 'learning.sqlite3'}?mode=ro", uri=True, timeout=30))

    def project_db(self):
        path = next(self.state.root.glob('projects/*/history.sqlite3'))
        return closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30))

    def handle_digest(self, run_handle):
        return keyed_digest(self.paths.digest_key(), run_handle)

    def account_binding(self, run_handle):
        """(binding state, abandoned reason, register state, register release kind, register generation)."""
        with self.account_db() as db:
            binding_row = db.execute('SELECT binding_state,abandoned_reason FROM host_run_bindings WHERE handle_digest=?',
                                     (self.handle_digest(run_handle),)).fetchone()
            hold = db.execute("SELECT state,release_kind,generation FROM run_holds WHERE kind='binding' AND identity=?",
                              (self.handle_digest(run_handle),)).fetchone()
        return (*(binding_row or (None, None)), *(hold or (None, None, None)))

    def facts(self, run_handle):
        """The open's run id and inputs digest, as the account recorded them."""
        with self.account_db() as db:
            run_id = db.execute('SELECT run_id FROM host_run_bindings WHERE handle_digest=?',
                                (self.handle_digest(run_handle),)).fetchone()[0]
            content = db.execute('SELECT content FROM account_open_receipts WHERE handle_digest=?',
                                 (self.handle_digest(run_handle),)).fetchone()[0]
        return run_id, json.loads(bytes(content))['view']['inputs_digest']

    def project_row(self, run_handle):
        with self.project_db() as db:
            return open_row(db, run_handle, project=self.project_id)

    def payload(self, run_handle, **changes):
        run_id, inputs = self.facts(run_handle)
        values = dict(run_handle=run_handle, attempt=1, run_id=run_id, inputs_digest=inputs, generation=1,
                      reason='cancelled_before_claim')
        values.update(changes)
        return WithdrawOpenInput(**values)

    def withdraw_as(self, value, *, role='project', host_client=CODEX_HOST_CLIENT, root=None, project_id=None,
                    custody=b'same', request_id=None):
        """One withdraw_open through the real client, enrollment, ingress and dispatch, as `role` for `root`."""
        root = root or self.project
        if custody == b'same':
            custody = withdrawal_custody(value)
        endpoint = account_endpoint_path(self.paths)
        here = threading.get_ident()

        def preamble(socket_path, message):
            if threading.get_ident() != here or Path(socket_path) != endpoint:
                return None
            key, proofs = read_capability(self.paths, None, 'account'), proof_directory(socket_path)
            line = enrollment_preamble(key, role, host_client, 1, message, proofs, subject=str(root), context=custody)
            return line, lambda: withdraw_proof(proofs, line)

        enrollments = {'test-withdraw': preamble, **service_transport._ENROLLMENTS}
        with mock.patch.object(service_transport, '_ENROLLMENTS', enrollments):
            return AccountClient(endpoint, project_id or self.project_id, COLLECTOR_RUNTIME_DIGEST,
                                 timeout=10).withdraw_open(
                value, request_id=request_id or withdrawal_request_id(value.run_handle, value.attempt))

    def refused(self, value, **options):
        with self.assertRaises(AccountError) as caught:
            self.withdraw_as(value, **options)
        return caught.exception.reason_code

    def opened(self):
        """An open run whose project row is written (the open registers it with the project service)."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.wait_for(lambda: self.project_row(run_handle) is not None, seconds=30)
        return context, route_path, orchestration, run_handle


class WithdrawHarness(WithdrawReads, DriverHarness):
    pass


@__import__('test_framework').requirements('RS-B-5', why_not_scenario='B3 checks the real codec, store, or enrolled service path; direct fault and custody controls expose the withdrawal boundary.')
class WithdrawAccountTests(WithdrawHarness):
    """W1-W4 through the account's real route, from both sources."""

    def setUp(self):
        # The project service's own withdrawals are switched off here: each test sends its own.
        patcher = mock.patch.object(project_driver.RunDriver, '_withdraw_opens', lambda driver: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def admit(self, provider):
        self.second = Path(self.temporary.name) / 'second'
        self.second.mkdir()
        (self.second / 'source.txt').write_text('second marker\n')
        git(self.second, 'init', '-q')
        git(self.second, 'config', 'user.email', 'tests@example.invalid')
        git(self.second, 'config', 'user.name', 'Bridge Tests')
        git(self.second, 'add', '.')
        git(self.second, 'commit', '-qm', 'second')
        fingerprint = super().admit(provider)
        self.second_id = self.ledger.register_project(self.second, now_ms=1)
        identity = self.ledger.observe_provider(self.second_id, provider, project_root=self.tools, environ=CLEAN)[0]
        attest_provider(self.ledger, self.second_id, self.owner_key, provider, identity['account_fingerprint'])
        return fingerprint

    def test_W1_the_project_service_withdraws_a_never_claimed_open(self):
        *_, run_handle = self.opened()
        self.assertEqual(('pending', None, 'held', None, 1), self.account_binding(run_handle))
        reply = self.withdraw_as(self.payload(run_handle))
        self.assertEqual(('withdrawn', run_handle, False), (reply['state'], reply['run_handle'], reply['known']))
        self.assertRegex(reply['receipt'], r'^[0-9a-f]{64}$')
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))

    def test_W1_the_host_that_opened_it_withdraws_it(self):
        *_, run_handle = self.opened()
        reply = self.withdraw_as(self.payload(run_handle), role='account')
        self.assertEqual(('withdrawn', False), (reply['state'], reply['known']))
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))

    def test_W2_every_other_caller_or_run_is_refused_and_nothing_changes(self):
        *_, run_handle = self.opened()
        good = self.payload(run_handle)
        cases = {
            'another project as itself': dict(root=self.second, project_id=self.second_id),
            'another project as this one': dict(root=self.second),
            "another host class's service": dict(host_client=CLAUDE_HOST_CLIENT),
            "another host class's primary": dict(role='account', host_client=CLAUDE_HOST_CLIENT),
            "the owner's terminal": dict(role='account', host_client=OWNER_CLIENT),
            'no custody': dict(custody=None),
            'custody for another attempt': dict(custody=withdrawal_custody(self.payload(run_handle, attempt=2))),
            'custody for another run id': dict(custody=withdrawal_custody(self.payload(run_handle,
                                                                                        run_id='run:' + 'f' * 64))),
        }
        refusals = {name: self.refused(good, **options) for name, options in cases.items()}
        self.assertEqual({name: 'account_not_authorized' for name in cases}, refusals)
        wrong = {
            'a wrong run id': self.payload(run_handle, run_id='run:' + 'f' * 64),
            'a wrong inputs digest': self.payload(run_handle, inputs_digest='f' * 64),
        }
        self.assertEqual({name: 'account_not_authorized' for name in wrong},
                         {name: self.refused(value) for name, value in wrong.items()})
        self.assertIn(self.refused(good, project_id=self.second_id, root=self.second,
                                   custody=withdrawal_custody(good)), {'account_not_authorized', 'withdraw_unavailable'})
        self.assertEqual(('pending', None, 'held', None, 1), self.account_binding(run_handle))

    def test_W4a_an_identical_retry_after_success_gets_the_stored_receipt(self):
        *_, run_handle = self.opened()
        first = self.withdraw_as(self.payload(run_handle))
        again = self.withdraw_as(self.payload(run_handle))
        self.assertEqual({**first, 'known': True}, again)
        self.assertEqual({**first, 'known': True}, self.withdraw_as(self.payload(run_handle), role='account'))

    def prune(self, run_handle):
        """Record retention: the binding, its open request and receipt go; the register row stays its tombstone."""
        connection = sqlite3.connect(str(self.state.root / 'learning.sqlite3'), timeout=30)
        connection.create_function('ledger_now_ms', 0, lambda: time.time_ns() // 1_000_000)
        try:
            digest = self.handle_digest(run_handle)
            with connection:
                connection.execute('DELETE FROM account_open_receipts WHERE handle_digest=?', (digest,))
                connection.execute('DELETE FROM account_open_requests WHERE handle_digest=?', (digest,))
                connection.execute('DELETE FROM host_run_bindings WHERE handle_digest=?', (digest,))
        finally:
            connection.close()

    def test_W4b_an_identical_retry_after_the_binding_was_pruned_gets_the_stored_receipt(self):
        *_, run_handle = self.opened()
        value = self.payload(run_handle)
        first = self.withdraw_as(value)
        self.prune(run_handle)
        self.assertEqual((None, None, 'released', 'withdrawn', 2), self.account_binding(run_handle))
        self.assertEqual({**first, 'known': True}, self.withdraw_as(value))

    def test_W4c_the_same_request_from_another_caller_scope_or_project_reveals_nothing(self):
        *_, run_handle = self.opened()
        value = self.payload(run_handle)
        self.withdraw_as(value)
        self.assertEqual('account_not_authorized', self.refused(value, host_client=CLAUDE_HOST_CLIENT))
        self.assertEqual('account_not_authorized', self.refused(value, role='account', host_client=CLAUDE_HOST_CLIENT))
        self.assertIn(self.refused(value, root=self.second, project_id=self.second_id),
                      {'account_not_authorized', 'withdraw_unavailable'})

    def test_W4d_an_altered_request_after_the_transition_is_stale_and_final(self):
        *_, run_handle = self.opened()
        first = self.withdraw_as(self.payload(run_handle))
        self.assertEqual('withdraw_stale', self.refused(self.payload(run_handle, reason='failed_before_claim')))
        self.assertEqual('withdraw_stale', self.refused(self.payload(run_handle), request_id='withdraw-other'))
        self.assertEqual({**first, 'known': True}, self.withdraw_as(self.payload(run_handle)))

    def test_W4e_a_new_transition_without_a_generation_is_refused_generation_required(self):
        *_, run_handle = self.opened()
        self.assertEqual('withdraw_generation_required', self.refused(self.payload(run_handle, generation=None)))
        self.assertEqual(('pending', None, 'held', None, 1), self.account_binding(run_handle))

    def hold_evidence(self, run_handle):
        with self.account_db() as db:
            return tuple(db.execute("SELECT receipt,evidence FROM run_holds WHERE kind='binding' AND identity=?",
                                    (self.handle_digest(run_handle),)).fetchone())

    def test_L1_the_permanent_withdrawal_evidence_stays_within_its_512_byte_budget_at_its_largest(self):
        # DESIGN-0.4.0-RS-B.md §4: the evidence of a withdrawn row is kept for good, under 512 bytes per row.
        *_, run_handle = self.opened()
        value = self.payload(run_handle, attempt=(1 << 63) - 1, reason=max(WITHDRAW_REASONS, key=len))
        first = self.withdraw_as(value)
        receipt, evidence = self.hold_evidence(run_handle)
        self.assertEqual(first['receipt'], receipt)
        self.assertLessEqual(len(evidence.encode()), 512)
        self.assertEqual({**first, 'known': True}, self.withdraw_as(value))  # still replays from the compact form
        self.assertEqual('withdraw_stale', self.refused(dataclasses.replace(value, reason='failed_before_claim')))
        self.assertEqual('account_not_authorized', self.refused(value, host_client=CLAUDE_HOST_CLIENT))

    def test_L1_a_withdrawal_recorded_in_the_earlier_format_still_replays(self):
        from codex_agents.learning_contracts import content_digest
        *_, run_handle = self.opened()
        value = self.payload(run_handle)
        first = self.withdraw_as(value)
        digest, project = self.handle_digest(run_handle), self.project_id
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open',
                                 withdrawal_request_id(run_handle, 1), project, COLLECTOR_RUNTIME_DIGEST,
                                 value.payload())
        with self.account_db() as db:
            released = db.execute("SELECT released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
                                  (digest,)).fetchone()[0]
        earlier = {"decision": "binding-withdrawal-v2", "project_id": project, "handle_digest": digest,
                   "request_digest": account_request_digest(request), "generation": 1, "release_kind": "withdrawn",
                   "released_at_ms": released, "answer": {"state": "withdrawn"},
                   "caller_scope": keyed_digest(self.paths.digest_key(),
                                                'account-caller\0' + CODEX_HOST_CLIENT + '\0' + project),
                   "run_id": value.run_id, "inputs_digest": value.inputs_digest, "attempt": 1,
                   "reason": value.reason}
        receipt = content_digest(earlier)
        connection = sqlite3.connect(str(self.state.root / 'learning.sqlite3'), timeout=30)
        connection.create_function('ledger_now_ms', 0, lambda: time.time_ns() // 1_000_000)
        try:
            with connection:
                connection.execute("UPDATE run_holds SET receipt=?,evidence=? WHERE kind='binding' AND identity=?",
                                   (receipt, canonical_json(earlier).decode(), digest))
        finally:
            connection.close()
        self.assertEqual({**first, 'receipt': receipt, 'known': True}, self.withdraw_as(value))
        self.assertEqual('account_not_authorized', self.refused(value, host_client=CLAUDE_HOST_CLIENT))
        self.assertEqual('withdraw_stale', self.refused(dataclasses.replace(value, reason='failed_before_claim')))

    def held_at(self, holding):
        """Stop every run's advance where `holding` says, until the returned event is set."""
        release, seen, original = threading.Event(), [], project_driver.RunDriver._advance

        def held(driver, run):
            if not release.is_set() and holding(driver, run):
                seen.append(run.state)
                return None
            return original(driver, run)
        patcher = mock.patch.object(project_driver.RunDriver, '_advance', held)
        patcher.start()
        self.addCleanup(patcher.stop)
        return release, seen

    def test_W3_claim_first_the_withdrawal_is_unavailable_and_the_run_goes_on(self):
        release, seen = self.held_at(lambda driver, run: run.state == 'binding')
        context, route_path, orchestration, run_handle = self.opened()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: seen, seconds=30)
        self.assertEqual('withdraw_unavailable', self.refused(self.payload(run_handle)))
        self.assertEqual(('pending', None, 'held', None, 1), self.account_binding(run_handle))
        release.set()
        output, _ = dispatch.communicate(timeout=60)
        self.assertEqual('result', json.loads(output)['bridge_report']['outcome'], output)
        self.wait_for(lambda: (self.project_row(run_handle) or ('held',))[0] == 'released', seconds=30)
        self.assertEqual(('released', 'claimed'), self.project_row(run_handle)[:2])

    def test_W3_withdrawal_first_the_claim_is_refused_and_the_run_never_launches(self):
        release, seen = self.held_at(lambda driver, run: run.state == 'recording_open')
        context, route_path, orchestration, run_handle = self.opened()
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: seen, seconds=30)
        self.withdraw_as(self.payload(run_handle))
        release.set()
        output, _ = dispatch.communicate(timeout=60)
        self.assertNotEqual(0, dispatch.returncode, output)
        self.assertEqual([], self.executions())
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))
        # The run's own lease was armed, so its end is the fence's: the open row is released `fenced`, never
        # withdrawn by the project.
        self.wait_for(lambda: (self.project_row(run_handle) or ('held',))[0] == 'released', seconds=30)
        self.assertEqual(('released', 'fenced'), self.project_row(run_handle)[:2])


@__import__('test_framework').requirements('RS-B-5', why_not_scenario='B3 checks the real codec, store, or enrolled service path; direct fault and custody controls expose the withdrawal boundary.')
class DriverWithdrawalTests(WithdrawHarness):
    """W5: the project service withdraws each no-claim exit's open by itself, exactly once."""

    def setUp(self):
        self.transitions = []
        original = LearningLedger.withdraw_open

        def counted(ledger, *args, **kwargs):
            reply = original(ledger, *args, **kwargs)
            self.transitions.append((kwargs.get('run_handle'), reply['known']))
            return reply
        patcher = mock.patch.object(LearningLedger, 'withdraw_open', counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def withdrawn(self, run_handle, seconds=30):
        self.wait_for(lambda: (self.project_row(run_handle) or ('held',))[0] == 'released', seconds=seconds)
        self.assertEqual(('released', 'withdrawn'), self.project_row(run_handle)[:2])
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))
        self.assertEqual([(run_handle, False)], [t for t in self.transitions if t[0] == run_handle and not t[1]])
        return json.loads(self.project_row(run_handle)[4])

    def test_W5_a_never_leased_cancel_withdraws_its_open_once(self):
        *_, run_handle = self.opened()
        self.assertEqual('held', self.project_row(run_handle)[0])
        self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle, '--reason', 'owner_cancelled')
        self.assertEqual('cancelled_before_claim', self.withdrawn(run_handle)['reason'])

    def test_W5_a_failure_before_preparation_withdraws_its_open_once(self):
        context, route_path, _, run_handle = self.opened()
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('failed_before_claim', self.withdrawn(run_handle)['reason'])
        self.assertEqual([], self.executions())

    def test_W5_a_failed_run_is_kept_for_a_late_prepare_for_a_bounded_time_and_then_released(self):
        now = [100.0]
        stub = mock.Mock(_clock=lambda: now[0])
        run = mock.Mock(duty_done_at=None, withdraw_reason='failed_before_claim', failed_viewed=False, state='failed')
        project_driver.RunDriver._release_failed(stub, run)
        self.assertEqual('failed', run.state)  # still inside its view window
        now[0] += project_driver.FAILED_VIEW_SECONDS - 1
        project_driver.RunDriver._release_failed(stub, run)
        self.assertEqual('failed', run.state)
        now[0] += 2  # past the limit
        project_driver.RunDriver._release_failed(stub, run)
        self.assertEqual('released', run.state)

    def test_W5_an_idle_registered_run_is_retired_and_withdrawn(self):
        with mock.patch.object(project_driver, 'REGISTERED_IDLE_SECONDS', 0.5):
            *_, run_handle = self.opened()
            self.assertEqual('registered_idle', self.withdrawn(run_handle)['reason'])
        self.assertEqual([], [run for driver in self.drivers for run in driver._runs.values()
                              if run.key[0] == run_handle])

    def test_W5_a_lost_reply_is_asked_again_and_withdraws_once(self):
        original, calls = AccountClient.withdraw_open, []

        def lossy(client, value, **options):
            reply = original(client, value, **options)
            calls.append(reply['known'])
            if len(calls) == 1:
                raise AccountError('account_closed_without_reply')  # the account withdrew it; the reply is lost
            return reply
        with mock.patch.object(project_driver, 'WITHDRAW_RETRY_SECONDS', 0.05), \
                mock.patch.object(AccountClient, 'withdraw_open', lossy):
            *_, run_handle = self.opened()
            self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle, '--reason', 'owner_cancelled')
            self.withdrawn(run_handle)
        self.assertEqual([False, True], calls)

    def test_W3_withdrawal_while_the_provider_observation_is_in_flight_fails_the_run_before_any_claim(self):
        release, original, seen, reasons = threading.Event(), project_driver.RunDriver._advance, [], []
        fail = project_driver.RunDriver._fail

        def held(driver, run):
            if not release.is_set() and run.state == 'observing':
                seen.append(run.state)
                return None
            return original(driver, run)

        def failed(driver, run, reason):
            reasons.append((run.state, reason))
            return fail(driver, run, reason)
        with mock.patch.object(project_driver.RunDriver, '_advance', held), \
                mock.patch.object(project_driver.RunDriver, '_fail', failed):
            context, route_path, _, run_handle = self.opened()
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: seen, seconds=30)
            # The observation is still in flight: it is refused. The reason is the one the driver will send, so
            # its own duty is an exact replay of this transition.
            self.withdraw_as(self.payload(run_handle, reason='failed_before_claim'))
            release.set()
            output, _ = dispatch.communicate(timeout=60)
            self.assertNotEqual(0, dispatch.returncode, output)
            self.withdrawn(run_handle)
        self.assertEqual(('observing', 'observation_not_authorized'), reasons[0])
        self.assertEqual([], self.executions())  # no model start
        with self.project_db() as db:  # no lease was ever taken, so no claim was sent
            self.assertEqual([], db.execute("SELECT identity FROM project_holds WHERE kind='lease'").fetchall())
        # Withdrawn exactly once: the test's own transition; the driver's duty only replays it.
        self.assertEqual([(run_handle, False)], [t for t in self.transitions if t[0] == run_handle and not t[1]])

    def test_M7_a_registration_refused_after_the_binding_exists_withdraws_the_open(self):
        from codex_agents import project_history
        for code in ('history_conflict', 'history_service_unavailable'):
            with self.subTest(refusal=code), mock.patch.object(
                    project_history.ProjectHistory, 'record_registered_run', side_effect=HistoryError(code)):
                *_, run_handle = self.opened()
                self.assertEqual('refused_before_claim', self.withdrawn(run_handle)['reason'])
                self.assertEqual([], self.executions())

    def test_M7_a_writer_faulted_at_record_open_binding_gets_one_memory_withdrawal(self):
        from codex_agents import project_history
        with mock.patch.object(project_history.ProjectHistory, 'record_open_binding',
                               side_effect=HistoryError('history_service_unavailable')):
            context, route_path, _, run_handle = self.open_run()
            self.wait_for(lambda: self.account_binding(run_handle)[0] == 'abandoned', seconds=30)
        # No duty row could be written, so the one withdrawal is the run's own, sent from memory; the account holds it.
        self.assertEqual(('abandoned', 'withdrawn', 'released', 'withdrawn', 2), self.account_binding(run_handle))
        self.assertIsNone(self.project_row(run_handle))
        self.assertEqual([(run_handle, False)], [t for t in self.transitions if t[0] == run_handle and not t[1]])
        self.assertEqual([], self.executions())

    def test_M1_a_withdrawal_the_account_keeps_refusing_does_not_block_a_later_one(self):
        original, calls = AccountClient.withdraw_open, []
        *_, first = self.opened()
        *_, second = self.opened()
        stuck, free = sorted((first, second))  # the stuck row sorts first: the one an unfair driver stops on

        def refusing(client, value, **options):
            if value.run_handle == stuck:
                calls.append(time.monotonic())
                raise AccountError('account_not_authorized')  # an authorization refusal is never final
            return original(client, value, **options)
        with mock.patch.object(project_driver, 'WITHDRAW_RETRY_SECONDS', 0.2), \
                mock.patch.object(AccountClient, 'withdraw_open', refusing):
            for handle in (first, second):
                self.cli('learning-classify-host-run-abandoned', '--run-handle', handle, '--reason', 'owner_cancelled')
            self.withdrawn(free, seconds=15)  # the later withdrawal is not blocked by the refused one
            self.wait_for(lambda: len(calls) >= 3, seconds=15)
        gaps = [later - earlier for earlier, later in zip(calls, calls[1:])]
        self.assertGreaterEqual(gaps[0], 0.19)  # each retry of the refused row waits (twice as long each time)
        self.assertGreaterEqual(gaps[1], 0.39)
        self.assertEqual('releasing', self.project_row(stuck)[0])  # not finalized, not forgotten
        with self.project_db() as db:
            self.assertGreater(db.execute("SELECT next_check_ms FROM project_holds WHERE kind='open_binding' "
                                          "AND identity=?", (f'{stuck}:1',)).fetchone()[0], 0)

    def test_L2_a_failed_deferral_submission_keeps_the_batch_and_the_failed_rows_duty(self):
        original = AccountClient.withdraw_open
        lane = __import__('codex_agents.result_persistence', fromlist=['x']).ResultPersistenceLane
        defer = lane.open_binding
        *_, first = self.opened()
        *_, second = self.opened()
        stuck, free = sorted((first, second))

        def refusing(client, value, **options):
            if value.run_handle == stuck:
                raise AccountError('account_not_authorized')
            return original(client, value, **options)

        def failing(self_, operation, *arguments, **options):
            if operation == 'defer_open_withdrawal':
                raise HistoryError('history_writer_busy')  # the deferral cannot even be submitted
            return defer(self_, operation, *arguments, **options)
        with mock.patch.object(project_driver, 'WITHDRAW_RETRY_SECONDS', 0.2), \
                mock.patch.object(AccountClient, 'withdraw_open', refusing), \
                mock.patch.object(lane, 'open_binding', failing):
            for handle in (first, second):
                self.cli('learning-classify-host-run-abandoned', '--run-handle', handle, '--reason', 'owner_cancelled')
            self.withdrawn(free, seconds=15)  # the rest of the batch is still drained
        self.assertEqual('releasing', self.project_row(stuck)[0])  # the failed row's duty is kept, not lost

    def test_W5_the_entry_is_retired_only_after_its_row_is_releasing(self):
        from codex_agents.project_history import ProjectHistory
        gate, original = threading.Event(), ProjectHistory.mark_open_withdrawing

        def slow(store, *args, **kwargs):
            gate.wait(30)
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, 'mark_open_withdrawing', slow):
            *_, run_handle = self.opened()
            self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle, '--reason', 'owner_cancelled')
            time.sleep(0.3)
            self.assertEqual('held', self.project_row(run_handle)[0])
            self.assertTrue([run for driver in self.drivers for run in driver._runs.values()
                             if run.key[0] == run_handle])  # still held in memory: its duty is not yet durable
            gate.set()
            self.withdrawn(run_handle)
        self.wait_for(lambda: not [run for driver in self.drivers for run in driver._runs.values()
                                   if run.key[0] == run_handle], seconds=10)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (WithdrawWireTests, OpenBindingStoreTests, WithdrawAccountTests, DriverWithdrawalTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == '__main__':
    unittest.main()
