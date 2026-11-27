"""RECOV: real history writer passes and wake publications, with a controlled wall clock."""
import unittest
from unittest import mock

import tests.support  # noqa: F401

requirements = __import__('test_framework').requirements
WHY = 'Real history writer and ledger with controlled time; publisher transport calls the actual ledger directly.'


def setUpModule():
    # Fixture modules load schemas and runtime digests: defer them until execution, never test discovery/import.
    global b6, accounts, persistence, assignment, binding, holds, project_start, project_wake, HistoryError
    global orchestration_store, service_record
    from tests import test_b6_unattended_close as b6
    from tests import test_account_launch_claims as accounts
    from tests import test_result_persistence as persistence
    from tests.learning_support import assignment
    from tests.test_project_history import binding
    from codex_agents import project_holds_schema as holds, project_start, project_wake
    from codex_agents import orchestration_store, service_record
    from codex_agents.history_contracts import HistoryError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.writer = persistence.ResultPersistenceTests(methodName='runTest')
        self.writer.setUp()
        self.addCleanup(self.writer.doCleanups)
        self.account = accounts.AccountLaunchClaimsTests(methodName='runTest')
        self.account.setUp()
        self.addCleanup(self.account.doCleanups)
        self.ledger = self.account.ledger
        self.project = self.account.project
        self.now = self.writer.fixture_time_ns() // 1_000_000
        self.old_due = self.now - 120_000
        self.sent = []

        def send(payload, timeout):
            self.sent.append(dict(payload))
            return self.ledger.publish_wake(self.project, now_ms=self.now, **payload)

        self.publisher = project_wake.WakePublisher(None, None, self.writer.project, send=send,
                                                    start_sender=False)
        self.addCleanup(self.publisher.close)
        holds.install_wake_guard(self.writer.project, self.publisher)
        self.addCleanup(holds.install_wake_guard, self.writer.project, None)
        # The live shape: a reservation already fenced for settlement retains its expired deadline. It cannot
        # release by time: the account must supply verified closure (its binding can still be held until later).
        self.writer._wall_origin_ns -= 240_000 * 1_000_000
        history = self.writer.open_history()
        target = binding(1, project=self.writer.project)
        reserved = history.reserve_result(target, assignment(),
            hard_deadline_at_ms=self.writer.fixture_time_ns() // 1_000_000 + 1000)
        self.old_due = reserved.reserved_until_ms
        self.assertEqual('unrecoverable', history.arbitrate_publication(target.run_handle, target.attempt,
                                                                       'pending-closure'))
        self.publisher.open(history._connection)
        history.close()
        self.writer._wall_origin_ns += 240_000 * 1_000_000
        self.publisher.drain()
        # Only the serving outcome matters here; claims and owner-proof surfaces are unchanged E2c tests.
        self.ledger._connection.execute(
            "INSERT INTO project_starts(project_id,failures,outcome,updated_at_ms) VALUES(?,0,'serving',?)",
            (self.project, self.now))

    def line(self, now=None):
        return project_start.recovery_line(self.ledger.start_record(self.project), self.account.fixture.project,
                                           self.now if now is None else now)

    def pass_once(self):
        self.writer.answered(self.writer.lane.expire_results())
        self.publisher.drain()

    @requirements('RECOV-1', why_not_scenario=WHY)
    def test_initial_validated_writer_open_gives_the_service_its_first_check(self):
        self.writer.stop_lane(self.writer.lane)
        self.writer.new_lane(bootstrap_history=True)
        self.publisher.drain()
        self.assertIsNone(self.line())

    @requirements('RECOV-1', why_not_scenario=WHY)
    def test_successful_writer_passes_keep_the_live_releasing_reservation_healthy(self):
        with mock.patch.object(self.ledger, '_clock_ms', return_value=self.now):
            account_holds = self.ledger.account_holds_status(self.project)
        self.assertEqual(1, account_holds['binding']['held'])
        self.assertGreater(account_holds['binding']['next_eligible'], self.now + 120_000)
        self.assertEqual(0, account_holds['run_slot']['held'])
        self.assertEqual('recovery pending: service unresponsive', self.line())  # reproduce the installed shape
        # Two minutes of successful passes: mere repeated reads must not resurrect the false warning.
        for offset in range(0, 121, 10):
            self.now += 10_000
            self.writer._wall_origin_ns += 10_000 * 1_000_000
            self.pass_once()
            self.assertIsNone(self.line(), (offset, self.sent[-1]))
            self.assertIsNone(self.line())

    @requirements('RECOV-3', why_not_scenario=WHY)
    def test_stopped_progress_returns_the_exact_E2c_9_warning_at_the_threshold(self):
        self.pass_once()
        due = self.ledger.start_record(self.project)['due_at_ms']
        self.assertGreater(due, self.old_due)
        grace = int(project_start.START_RECHECK_SECONDS * 1000)
        self.assertIsNone(self.line(due + grace - 1))
        self.assertEqual('recovery pending: service unresponsive', self.line(due + grace))
        self.assertEqual('recovery pending: service unresponsive', self.line(due + grace + 1))

    @requirements('RECOV-1', 'RECOV-3', why_not_scenario=WHY)
    def test_unrelated_writes_and_sender_reads_neither_erase_nor_renew_progress(self):
        self.pass_once()
        checked = self.ledger.start_record(self.project)['due_at_ms']
        self.now += 30_000
        self.publisher.touch()
        self.publisher.after_settle(self.old_due)
        self.publisher.drain()
        self.assertEqual(checked, self.ledger.start_record(self.project)['due_at_ms'])
        self.assertIsNone(self.line())
        self.publisher.drain()
        grace = int(project_start.START_RECHECK_SECONDS * 1000)
        self.assertEqual('recovery pending: service unresponsive', self.line(checked + grace))

    @requirements('RECOV-3', why_not_scenario=WHY)
    def test_failed_maintenance_does_not_supply_progress(self):
        with mock.patch.object(persistence.ProjectHistory, 'expire_results', side_effect=HistoryError('history_unavailable')):
            request = self.writer.lane.expire_results()
            self.writer.wait(lambda: request.state != 'queued')
        self.publisher.drain()
        self.assertEqual('failed', request.state)
        self.assertEqual(self.old_due, self.ledger.start_record(self.project)['due_at_ms'])
        self.assertEqual('recovery pending: service unresponsive', self.line())

    @requirements('RECOV-1', 'RECOV-3', why_not_scenario=WHY)
    def test_real_hold_transaction_between_passes_preserves_without_renewing_the_floor(self):
        self.pass_once()
        checked = self.ledger.start_record(self.project)['due_at_ms']
        self.now += 30_000
        self.writer._wall_origin_ns += 30_000 * 1_000_000
        target = binding(2, project=self.writer.project)
        # Use the existing exclusive writer, dispatching a real history operation with its real transaction.
        # The fixture bypasses only the public open-binding operation allowlist, not the store or hold hooks.
        request = persistence.result_persistence._EarlierLeases('open_binding', arguments=(
            'reserve_result', (target, assignment()),
            {'hard_deadline_at_ms': self.writer.fixture_time_ns() // 1_000_000 + 1000}))
        reservation = self.writer.answered(self.writer.lane._queue_counted(
            request, '_earlier_held', persistence.result_persistence.EARLIER_SLOTS))
        self.assertEqual(('reserved', target), (reservation.state, reservation.binding))
        self.publisher.drain()
        self.assertEqual(checked, self.ledger.start_record(self.project)['due_at_ms'])
        grace = int(project_start.START_RECHECK_SECONDS * 1000)
        self.assertIsNone(self.line(checked + grace - 1))
        self.assertEqual('recovery pending: service unresponsive', self.line(checked + grace))
        self.pass_once()
        self.assertGreater(self.ledger.start_record(self.project)['due_at_ms'], checked)

    @requirements('RECOV-1', 'RECOV-3', why_not_scenario=WHY)
    def test_persistently_failed_substeps_do_not_renew_progress_and_warning_returns_at_threshold(self):
        steps = (
            (orchestration_store.OrchestrationOwner, 'end_earlier'),
            (orchestration_store.OrchestrationOwner, 'end_idle'),
            (persistence.ProjectHistory, 'settle_earlier_registrations'),
            (persistence.ProjectHistory, 'release_earlier_unarmed_leases'),
            (persistence.ProjectHistory, 'expire_result_leases'),
            (self.writer.lane, '_adopt_committed'),
            (service_record, 'record_pass'),
        )
        grace = int(project_start.START_RECHECK_SECONDS * 1000)
        for owner, name in steps:
            with self.subTest(step=name):
                self.pass_once()
                checked = self.ledger.start_record(self.project)['due_at_ms']
                error = RuntimeError('record pass failed') if name == 'record_pass' else HistoryError('history_unavailable')
                with mock.patch.object(owner, name, side_effect=error) as failed:
                    for _ in range(3):
                        self.now += grace // 3
                        self.writer._wall_origin_ns += (grace // 3) * 1_000_000
                        self.pass_once()  # suppressed failure still allows the cleanup/request to finish
                    self.assertEqual(3, failed.call_count)
                self.assertEqual(checked, self.ledger.start_record(self.project)['due_at_ms'])
                self.assertIsNone(self.line(checked + grace - 1))
                self.assertEqual('recovery pending: service unresponsive', self.line(checked + grace))

    @requirements('RECOV-1', 'RECOV-3', why_not_scenario=WHY)
    def test_future_and_null_deadlines_and_clean_stop_are_preserved(self):
        self.pass_once()
        self.writer.stop_lane(self.writer.lane)
        history = self.writer.open_history()
        self.addCleanup(history.close)
        self.assertEqual(('releasing', self.old_due), tuple(history._connection.execute(
            "SELECT state,due_at_ms FROM project_holds WHERE kind='reservation'").fetchone()))
        self.assertTrue(self.publisher.finish(faulted=False,
                                              final_due=lambda: holds.next_hold_due(history._connection)))
        self.assertEqual(('clean_stop', self.old_due),
                         (self.sent[-1]['state'], self.sent[-1]['next_due_at_ms']))
        for due in (self.now + 300_000, None):
            history._connection.execute('UPDATE project_holds SET due_at_ms=?', (due,))
            # A new publisher represents a live successor, not the stopped sender above.
            publisher = project_wake.WakePublisher(None, None, self.writer.project,
                send=lambda payload, timeout: {'epoch': 1, 'seq': payload['seq'], 'outcome': 'accepted'},
                start_sender=False)
            self.addCleanup(publisher.close)
            holds.install_wake_guard(self.writer.project, publisher)
            holds.wake_checked(self.writer.project, history._connection, now_ms=self.now)
            publisher.drain()
            self.assertEqual(('exact', due), publisher.acknowledged)


class BackoffTests(unittest.TestCase):
    @requirements('RECOV-2', 'E2a-7', why_not_scenario=WHY)
    def test_release_then_crash_before_exact_leaves_collector_pending_instead_of_later_B6_retry(self):
        fixture = b6.OrchestrationStore(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        account = accounts.AccountLaunchClaimsTests(methodName='runTest')
        account.setUp()
        self.addCleanup(account.doCleanups)
        snapshot = fixture.open()
        handle = snapshot['orchestration_handle']
        fixture.block(handle)
        blocker = 'clm_' + handle[-8:]
        db, project = fixture.store._connection, fixture.store.project_id
        with fixture.store._transaction():
            holds.take_hold(db, project, 'reservation', blocker, now_ms=fixture.now)
        fixture.now += b6.ORCHESTRATION_IDLE_MS + 120_000
        publisher = project_wake.WakePublisher(None, None, project, start_sender=False,
            send=lambda payload, timeout: account.ledger.publish_wake(account.project, now_ms=fixture.now, **payload))
        self.addCleanup(publisher.close)
        holds.install_wake_guard(project, publisher)
        self.addCleanup(holds.install_wake_guard, project, None)
        publisher.open(db)
        publisher.drain()
        self.assertEqual(0, fixture.owner.end_idle())
        publisher.drain()
        retry = fixture.now + holds.UNATTENDED_BACKOFF_MS
        self.assertEqual(retry, account.ledger.start_record(account.project)['due_at_ms'])
        # Commit the release, then lose the process before its transaction can queue/send exact.
        with mock.patch.object(holds, 'wake_settled'):
            with fixture.store._transaction():
                holds.release_hold(db, project, 'reservation', blocker, 'lost', now_ms=fixture.now)
        self.assertEqual(snapshot['started_at_ms'] + b6.ORCHESTRATION_IDLE_MS, holds.next_hold_due(db))
        publisher.drain()  # no stale queued exact may overwrite the pending either
        row = account.ledger._connection.execute('SELECT state,due_at_ms FROM project_wake WHERE project_id=?',
                                                (account.project,)).fetchone()
        self.assertEqual('pending', row['state'])
        self.assertTrue(row['due_at_ms'] is None or row['due_at_ms'] <= holds.next_hold_due(db))

    @requirements('RECOV-2', why_not_scenario='Real orchestration owner and hold register with a recording publisher.')
    def test_B6_backoff_is_published_and_other_holds_and_release_reset_still_win(self):
        fixture = b6.OrchestrationStore(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        snapshot = fixture.open()
        handle = snapshot['orchestration_handle']
        fixture.block(handle)
        blocker = 'clm_' + handle[-8:]
        holds.take_hold(fixture.store._connection, fixture.store.project_id, 'reservation', blocker,
                        now_ms=fixture.now)
        fixture.now += b6.ORCHESTRATION_IDLE_MS + 120_000
        sent = []
        publisher = project_wake.WakePublisher(None, None, fixture.store.project_id, start_sender=False,
            send=lambda payload, timeout: (sent.append(dict(payload)) or
                {'epoch': 1, 'seq': payload['seq'], 'outcome': 'accepted'}))
        self.addCleanup(publisher.close)
        holds.install_wake_guard(fixture.store.project_id, publisher)
        self.addCleanup(holds.install_wake_guard, fixture.store.project_id, None)
        publisher.open(fixture.store._connection)
        publisher.drain()
        self.assertEqual(0, fixture.owner.end_idle())
        retry = fixture.now + holds.UNATTENDED_BACKOFF_MS
        publisher.drain()
        self.assertEqual(retry, holds.next_hold_due(fixture.store._connection))
        self.assertEqual(('exact', retry), (sent[-1]['state'], sent[-1]['next_due_at_ms']))
        self.assertTrue(publisher.finish(faulted=False,
            final_due=lambda: holds.next_hold_due(fixture.store._connection)))
        self.assertEqual(retry, sent[-1]['next_due_at_ms'])
        # Another outstanding hold must still wake earlier than that backed-off orchestration.
        db, project = fixture.store._connection, fixture.store.project_id
        holds.take_hold(db, project, 'reservation', 'earlier', now_ms=fixture.now, due_at_ms=fixture.now + 1)
        self.assertEqual(fixture.now + 1, holds.next_hold_due(db))
        holds.release_hold(db, project, 'reservation', 'earlier', 'completed', now_ms=fixture.now)
        holds.release_hold(db, project, 'reservation', blocker, 'lost', now_ms=fixture.now)
        self.assertEqual(snapshot['started_at_ms'] + b6.ORCHESTRATION_IDLE_MS,
                         holds.next_hold_due(db))
