"""O0b B0-6: every hold a child run takes ends by itself (PLAN B0-1 to B0-6; DESIGN-0.4.0-B0-holds.md, B0-6
as revised in revision 3).

Integration stage: the in-process collector, account and project services (the test_result_lifetime fixture).
Each scenario records a baseline, shows it took the hold it targets, stops all client action at the failure
point, lets only production timers and sweeps run to a stated deadline, then checks every count before
teardown. Closing the orchestration stays the primary's step: scenarios check "closable" separately.

This module is written before any B0 code: each failure exit here fails on 0.8.37, by design, and is
listed with the B0 step that makes it pass.
"""
import json
import unittest
from unittest import mock

from tests.test_bridge import null_filled, result
from tests.test_lost_run_settlement import LostRunAfterStopTests
from tests.test_result_lifetime import ResultLifetimeTests

from codex_agents import codex_preparation


class HoldLifecycleTests(ResultLifetimeTests):
    def held(self):
        """Every hold this harness watches, by name (revision 3's list grows with each B0 step)."""
        ledger = self.collector_ledger._connection
        now = self.collector_ledger_now_ms()
        return {
            'live_results': self.cli('learning-status')['project_capacity']['results']['live'],
            'account_slots': ledger.execute(
                'SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0],
            # B0-1: a pending binding the cap and the drain still count.
            'pending_bindings': ledger.execute(
                "SELECT COUNT(*) FROM host_run_bindings WHERE binding_state='pending' AND expires_at_ms>?",
                (now,)).fetchone()[0],
        }

    def collector_ledger_now_ms(self):
        import time
        return time.time_ns() // 1_000_000

    def test_B0_6_baseline_is_empty(self):
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    @unittest.skip("gates the run supervisor: the owner narrowed B0 on 2026-11-20 (never-armed withdrawal)")
    def test_B0_1_a_run_refused_before_launch_frees_its_account_binding_by_itself(self):
        """B0-1 (fails on 0.8.37): the binding stays pending for 24 h, held against the cap and every drain."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.assertEqual(1, self.held()['pending_bindings'])  # the hold was taken
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        # No further client action: only the service's own work may free it.
        self.wait_for(lambda: self.held()['pending_bindings'] == 0, seconds=30)
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    def test_B0_narrowed_status_shows_a_held_binding(self):
        """O0b B0 (narrowed): learning-status shows each binding still held, so the hold is never invisible."""
        before = self.cli('learning-status')['account_capacity']['pending_bindings']
        self.open_run()
        after = self.cli('learning-status')['account_capacity']['pending_bindings']
        self.assertEqual(before['live'] + 1, after['live'])
        self.assertEqual(4096, after['limit'])

    def test_B0_6_a_completed_run_frees_every_hold(self):
        """Control: today's successful lifecycle already frees what it took (once recorded)."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.record(context, route_path, run_handle,
                    self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle))
        self.wait_for(lambda: self.held()['account_slots'] == 0 and self.held()['live_results'] <= 1, seconds=30)


class LostServiceHoldTests(LostRunAfterStopTests):
    """Holds left by a service that stops mid-run (the lost-run fixture restarts it)."""

    def reservation_terminal(self, run_handle):
        """The lost run's result reservation: (state, has a terminal), read without the service."""
        import sqlite3
        (path,) = list(self.state.root.rglob('history.sqlite3'))
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            row = connection.execute('SELECT state, terminal_record_id IS NOT NULL FROM result_reservations '
                                     'WHERE run_handle=?', (run_handle,)).fetchone()
        finally:
            connection.close()
        return None if row is None else (row[0], bool(row[1]))

    @unittest.skip("gates the run supervisor: the owner narrowed B0 on 2026-11-20 (stranded reservations)")
    def test_B0_2_a_reservation_left_by_a_service_that_stopped_gets_its_lost_terminal(self):
        """B0-2 (fails on 0.8.37): the next service settles the lost run at the account, but its result
        reservation keeps no terminal, so the orchestration can never close and the row is never purged."""
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        self.assertIsNotNone(self.reservation_terminal(run_handle))  # the hold was taken
        self.wait_for(lambda: self.reservation_terminal(run_handle)[1], seconds=30)


class LostSlotReleaseTests(LostRunAfterStopTests):
    """O0b RS-A-2: a lost settlement releases the run's slot itself, written once with its kind."""

    def test_RS_A_2_a_lost_run_s_slot_is_released_as_lost_by_its_settlement(self):
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        rows = [dict(row) for row in self.collector_ledger._connection.execute(
            # The kind is in the register row, written with the release (RS-A implementation decisions).
            "SELECT a.released_at_ms, r.release_kind FROM active_run_capacity a JOIN host_run_bindings h "
            "ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest "
            "JOIN run_holds r ON r.kind='run_slot' AND r.project_id=a.project_id AND r.identity=a.handle_digest "
            "WHERE h.binding_state='abandoned' AND h.abandoned_reason='service_lost'")]
        self.assertEqual(1, len(rows), rows)
        self.assertIsNotNone(rows[0]['released_at_ms'])  # written, not re-derived from the binding
        self.assertEqual('lost', rows[0]['release_kind'])


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited ones run in their own modules."""
    suite = unittest.TestSuite()
    for case in (HoldLifecycleTests, LostServiceHoldTests, LostSlotReleaseTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
