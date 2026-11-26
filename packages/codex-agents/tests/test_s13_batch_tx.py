"""RS-10-DUE-TX-1..3: a start-pass batch commits once, with a savepoint per row (injected faults on a real store)."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_leases as leases

from codex_agents import project_result_contexts
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

WHY = 'Real store; faults are injected at the verify, row-write and COMMIT points of the batch.'
ROWS = 8


class Crash(BaseException):
    """Stands for the process dying: not an Exception, so no handler in the product may treat it as a failure."""


class BatchTransactionTests(unittest.TestCase):
    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.fixture.capacity = ResultCapacity(2048, 2048 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen(result_capacity=self.base.fixture.capacity)
        for number in range(1, ROWS + 1):
            self.base.reserve(number)
        self.finished = []
        original = project_result_contexts.release_unfinished
        def ending(history, *args):
            original(history, *args)
            return True  # every released row has a context file to remove
        for name, value in (('release_unfinished', ending),
                            ('finish_quietly', lambda history, handle, attempt: self.finished.append((handle, attempt)))):
            patcher = mock.patch.object(project_result_contexts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def store(self):
        return self.base.store

    def handle(self, number):
        return leases.lease_binding(number).run_handle

    def states(self):
        return [self.store._connection.execute("SELECT state FROM result_leases WHERE run_handle=?",
                                               (self.handle(n),)).fetchone()[0] for n in range(1, ROWS + 1)]

    def hold(self, number):
        return dict(self.store._connection.execute("SELECT * FROM project_holds WHERE kind='lease' AND identity=?",
                                                   (self.handle(number) + ':1',)).fetchone())

    def fail_verify_of(self, number, error):
        self.store.__dict__.pop('_verified_leases', None)
        original = ProjectHistory._verify_lease
        def verify(history, row):
            if row['run_handle'] == self.handle(number):
                raise error
            return original(history, row)
        return mock.patch.object(ProjectHistory, '_verify_lease', verify)

    def reopen_after_crash(self):
        self.base.reopen(result_capacity=self.base.fixture.capacity)

    @requirements('RS-10-DUE-TX-1', why_not_scenario=WHY)
    def test_a_row_that_fails_verification_rolls_back_alone_is_backed_off_and_the_others_commit(self):
        # Passes on the per-row design too: the guarantee was kept when the commit moved to the batch.
        with self.fail_verify_of(3, HistoryError('history_incompatible')):
            with self.assertRaises(HistoryError) as raised:
                self.store.release_earlier_unarmed_leases(frozenset(), limit=64)
        self.assertEqual('history_incompatible', raised.exception.reason_code)
        self.assertEqual(['expired'] * 2 + ['reserved'] + ['expired'] * (ROWS - 3), self.states())
        held = self.hold(3)
        self.assertEqual('held', held['state'])
        self.assertEqual(1, json.loads(held['evidence'])['start_pass_failures'])
        self.assertGreater(held['next_check_ms'], self.base.fixture.now)
        self.assertEqual([n for n in range(1, ROWS + 1) if n != 3],
                         [n for n in range(1, ROWS + 1) if self.hold(n)['state'] == 'released'])
        self.assertEqual(ROWS - 1, len(self.finished))  # file removal only for rows whose release committed
        self.assertFalse(self.store._connection.in_transaction)

    @requirements('RS-10-DUE-TX-2', why_not_scenario=WHY)
    def test_a_kill_in_the_middle_of_the_batch_leaves_every_row_reserved_and_the_next_pass_releases_each_once(self):
        # Red on the per-row design: rows before the kill had already committed.
        calls = []
        original = ProjectHistory._set_lease
        def set_lease(history, value):
            calls.append(value)
            if len(calls) == 5:
                raise Crash()
            return original(history, value)
        with mock.patch.object(ProjectHistory, '_set_lease', set_lease):
            with self.assertRaises(Crash):
                self.store.release_earlier_unarmed_leases(frozenset(), limit=64)
        self.assertEqual([], self.finished)
        self.reopen_after_crash()
        self.assertEqual(['reserved'] * ROWS, self.states())
        self.assertEqual(ROWS, self.store.release_earlier_unarmed_leases(frozenset(), limit=64))
        self.assertEqual(['expired'] * ROWS, self.states())
        self.assertEqual(sorted((self.handle(n), 1) for n in range(1, ROWS + 1)), sorted(self.finished))
        self.assertEqual(0, self.store.release_earlier_unarmed_leases(frozenset(), limit=64))
        self.assertEqual(ROWS, len(self.finished))  # no double release, no lost removal

    @requirements('RS-10-DUE-TX-2', why_not_scenario=WHY)
    def test_a_kill_at_the_batch_commit_leaves_every_row_reserved_and_removes_no_file(self):
        # Passes on the per-row design only for the first commit; kept as the commit-point case.
        real = self.store._connection.execute
        def execute(sql, *args):
            if sql == 'COMMIT':
                raise Crash()
            return real(sql, *args)
        with mock.patch.object(self.store._connection, 'execute', execute):
            with self.assertRaises(Crash):
                self.store.release_earlier_unarmed_leases(frozenset(), limit=64)
        self.assertEqual([], self.finished)
        self.reopen_after_crash()
        self.assertEqual(['reserved'] * ROWS, self.states())
        self.assertEqual(ROWS, self.store.release_earlier_unarmed_leases(frozenset(), limit=64))
        self.assertEqual(ROWS, len(self.finished))

    @requirements('RS-10-DUE-TX-3', why_not_scenario=WHY)
    def test_an_error_that_is_not_a_history_error_rolls_the_whole_batch_back_and_the_next_pass_redoes_it(self):
        # Red on the per-row design: rows before the error had already committed.
        with self.fail_verify_of(5, RuntimeError('verify exploded')):
            with self.assertRaisesRegex(Exception, 'verify exploded'):
                self.store.release_earlier_unarmed_leases(frozenset(), limit=64)
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(['reserved'] * ROWS, self.states())
        self.assertEqual(['held'] * ROWS, [self.hold(n)['state'] for n in range(1, ROWS + 1)])
        self.assertEqual([], self.finished)
        self.assertEqual(ROWS, self.store.release_earlier_unarmed_leases(frozenset(), limit=64))
        self.assertEqual(['expired'] * ROWS, self.states())
        self.assertEqual(ROWS, len(self.finished))


if __name__ == '__main__':
    unittest.main()
