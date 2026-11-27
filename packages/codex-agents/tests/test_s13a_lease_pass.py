"""RS-B section 5: bounded due selection and durable retry timing on real project stores."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_leases as leases
from tests.test_project_history import binding
from tests.test_s13_budgets import measurements

from codex_agents.history_contracts import HistoryError
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

WHY = 'Real store and virtual clock prove selection, retry boundaries and persistence without service sockets.'


class LeaseStartPassTests(unittest.TestCase):
    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.fixture.capacity = ResultCapacity(2048, 2048 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen(result_capacity=self.base.fixture.capacity)

    @property
    def store(self):
        return self.base.store

    def row(self, number=1):
        return dict(self.store._connection.execute(
            "SELECT * FROM project_holds WHERE kind='lease' AND identity=?",
            (leases.lease_binding(number).run_handle + ':1',)).fetchone())

    def live(self, *numbers):
        return frozenset((leases.lease_binding(n).run_handle, 1) for n in numbers)

    @requirements('S13A-BOUND', why_not_scenario=WHY)
    def test_128_blocked_plus_arrivals_select_at_most_64_even_with_limit_100(self):
        for number in range(1, 130):
            self.base.reserve(number)
        live = set(self.live(*range(1, 129)))
        module = measurements()
        for batch in range(4):
            for number in range(130 + batch * 50, 180 + batch * 50):
                self.base.reserve(number)
                live.update(self.live(number))
            with module.instrumented(self.store) as connection:
                released = self.store.release_earlier_unarmed_leases(live, limit=100)
            self.assertLessEqual(max(connection.selections, default=0), 64)
            self.assertLessEqual(released, 64)
            if self.row(129)['state'] == 'released':
                break
        self.assertEqual('released', self.row(129)['state'])

    @requirements('S13A-BACKOFF', why_not_scenario=WHY)
    def test_backoff_doubles_from_one_second_to_300_and_skips_until_due_across_reopen(self):
        self.base.reserve()
        for delay in (1000, 2000, 4000, 8000, 16000, 32000, 64000, 128000, 256000, 300000, 300000):
            now = self.base.fixture.now
            self.assertEqual(0, self.store.release_earlier_unarmed_leases(self.live(1), limit=64))
            row = self.row()
            self.assertEqual(now + delay, row['next_check_ms'])
            self.assertEqual(61_000, row['due_at_ms'])
            self.base.reopen()
            self.base.fixture.now = now + delay - 1
            self.assertEqual(0, self.store.release_earlier_unarmed_leases(self.live(1), limit=64))
            self.assertEqual(row, self.row())
            self.base.fixture.now += 1

    @requirements('S13A-RESET', why_not_scenario=WHY)
    def test_release_resets_backoff_for_start_pass_explicit_release_expiry_and_bind(self):
        for number, action in enumerate(('pass', 'unsent', 'expiry', 'bind'), 1):
            self.base.reserve(number)
            self.store.release_earlier_unarmed_leases(self.live(number), limit=64)
            row = self.row(number)
            self.assertGreater(row['next_check_ms'], self.base.fixture.now)
            if action == 'pass':
                self.base.fixture.now = row['next_check_ms']
                self.assertEqual(1, self.store.release_earlier_unarmed_leases((), limit=64))
            elif action == 'unsent':
                self.store.release_unsent_result_lease(leases.lease_binding(number))
            elif action == 'expiry':
                self.base.fixture.now = row['due_at_ms']
                self.assertEqual(1, self.store.expire_result_leases(limit=64))
            else:
                self.store.arm_result_lease(leases.lease_binding(number))
                self.base.bind(number, hard_deadline_at_ms=self.base.fixture.now + 1000)
            released = self.row(number)
            self.assertEqual(('released', 0), (released['state'], released['next_check_ms']))
            self.assertNotIn('start_pass_failures', json.loads(released['evidence'] or '{}'))

    @requirements('S13A-ORDER', why_not_scenario=WHY)
    def test_due_rows_are_selected_by_next_check_not_lease_insertion_order(self):
        for number, due in ((1, 3000), (2, 1000), (3, 2000)):
            self.base.reserve(number)
            self.store._connection.execute("UPDATE project_holds SET next_check_ms=? WHERE kind='lease' AND identity=?",
                                          (due, leases.lease_binding(number).run_handle + ':1'))
        self.base.fixture.now = 2500
        self.assertEqual(1, self.store.release_earlier_unarmed_leases((), limit=1))
        self.assertEqual(['held', 'released', 'held'], [self.row(n)['state'] for n in (1, 2, 3)])
        self.assertEqual(1, self.store.release_earlier_unarmed_leases((), limit=1))
        self.assertEqual(0, self.store.release_earlier_unarmed_leases((), limit=64))
        self.assertEqual('held', self.row(1)['state'])
        self.base.fixture.now = 3000
        self.assertEqual(1, self.store.release_earlier_unarmed_leases((), limit=1))

    @requirements('S13A-FAIR', why_not_scenario=WHY)
    def test_blocked_row_is_retried_when_due_despite_a_full_batch_of_new_arrivals(self):
        self.base.reserve()
        self.store.release_earlier_unarmed_leases(self.live(1), limit=64)
        due = self.row()['next_check_ms']
        self.base.fixture.now = due - 1
        self.assertEqual(0, self.store.release_earlier_unarmed_leases((), limit=64))
        for batch in range(3):
            self.base.fixture.now = due + batch
            numbers = range(2 + batch * 64, 66 + batch * 64)
            for number in numbers:
                self.base.reserve(number)
            self.store.release_earlier_unarmed_leases(self.live(*range(2, 66 + batch * 64)), limit=64)
            if self.row()['state'] == 'released':
                break
        self.assertEqual('released', self.row()['state'])

    @requirements('S13A-FAIR', why_not_scenario=WHY)
    def test_64_blocked_rows_ahead_of_eligible_rows_do_not_starve_them(self):
        """Decision 354: strict 64 examined per call; eligible rows are released on later passes."""
        for number in range(1, 201):
            self.base.reserve(number)
        live = self.live(*range(1, 65))
        module = measurements()
        total, passes, per_pass = 0, 0, []
        while passes < 8:
            with module.instrumented(self.store) as connection:
                released = self.store.release_earlier_unarmed_leases(live, limit=64)
            passes += 1
            self.assertLessEqual(sum(connection.selections), 64)  # examined per call, not per statement
            per_pass.append(released)
            total += released
            if not released and passes > 1:
                break
        self.assertEqual(0, per_pass[0])  # first pass examines only the 64 oldest blocked rows and backs them off
        self.assertGreater(per_pass[1], 0)
        self.assertEqual(136, total)
        self.assertTrue(all(self.row(n)['state'] == 'held' and self.row(n)['next_check_ms'] > self.base.fixture.now
                            for n in range(1, 65)))

    @requirements('S13A-BOUND', why_not_scenario=WHY)
    def test_armed_rows_do_not_consume_the_unarmed_batch(self):
        for number in range(1, 66):
            self.base.reserve(number)
            if number <= 64:
                self.store.arm_result_lease(leases.lease_binding(number))
        self.assertEqual(1, self.store.release_earlier_unarmed_leases((), limit=64))
        self.assertEqual('released', self.row(65)['state'])
        self.assertTrue(all(self.row(n)['state'] == 'held' for n in range(1, 65)))

    @requirements('S13A-BACKOFF', why_not_scenario=WHY)
    def test_inspection_refusal_backs_off_and_does_not_stop_other_rows(self):
        self.base.reserve(1)
        self.base.reserve(2)
        original = self.store._lease
        def refuse_first(row):
            if row['run_handle'] == binding(1).run_handle:
                raise HistoryError('history_incompatible')
            return original(row)
        with mock.patch.object(self.store, '_lease', side_effect=refuse_first):
            with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
                self.store.release_earlier_unarmed_leases((), limit=64)
        self.assertEqual(self.base.fixture.now + 1000, self.row(1)['next_check_ms'])
        self.assertEqual('released', self.row(2)['state'])

    @requirements('S13A-BACKOFF', why_not_scenario=WHY)
    def test_host_open_blocker_backs_off_inside_the_inspection_transaction(self):
        self.base.reserve()
        # Fault injection: a host-open row is discovered during inspection. The source lease stays untouched.
        connection = self.store._connection
        proxy = mock.Mock(wraps=connection)
        def execute(sql, *args):
            if sql.startswith('SELECT 1 FROM project_host_opens'):
                return connection.execute('SELECT 1')
            return connection.execute(sql, *args)
        proxy.execute.side_effect = execute
        with mock.patch.object(self.store, '_connection', proxy):
            self.assertEqual(0, self.store.release_earlier_unarmed_leases((), limit=64))
        self.assertEqual(('held', self.base.fixture.now + 1000),
                         (self.row()['state'], self.row()['next_check_ms']))

    @requirements('S13A-BOUND', why_not_scenario=WHY)
    def test_all_eligible_rows_still_release_at_most_64_with_limit_100(self):
        for number in range(1, 129):
            self.base.reserve(number)
        self.assertEqual(64, self.store.release_earlier_unarmed_leases((), limit=100))
        self.assertEqual(64, self.store.release_earlier_unarmed_leases((), limit=100))
