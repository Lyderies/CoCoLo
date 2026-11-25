"""RS-B-4 memory custody: real store transactions and controlled owner/writer turns."""
import json
import random
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_b6_unattended_close as b6
from codex_agents import project_driver, project_holds_schema
from codex_agents.history_contracts import HistoryError
from codex_agents.launch_journal import LaunchJournal

requirements = __import__('test_framework').requirements
WHY = 'Memory ownership and store transactions are exercised without a socket boundary.'


def memory():
    from codex_agents import memory_holds
    return memory_holds


class MemoryFixture(b6.OrchestrationStore):
    def setUp(self):
        super().setUp()
        self.snapshot = self.open()
        self.handle = self.snapshot['orchestration_handle']
        self.key = ('hrn_' + 'b' * 48, 1)
        self.journal = LaunchJournal(self.directory.parent / 'launches')
        self.addCleanup(self.journal.release)
        self.instance = self.journal.instance

    def register(self, key=None):
        key = key or self.key
        return memory().register(self.store, self.handle, *key, 'run-' + key[0][-4:], self.instance,
                                 launches=self.journal.directory)

    def row(self, kind, key=None):
        key = key or self.key
        row = self.store._connection.execute('SELECT * FROM project_holds WHERE kind=? AND identity=?',
            (kind, project_holds_schema.attempt_identity(*key))).fetchone()
        return None if row is None else dict(row)

    def prep(self, key=None):
        return memory().take_preparation(self.store, *(key or self.key), self.instance)

    def release(self, kind, reason='retired', key=None):
        return memory().release(self.store, kind, *(key or self.key), self.instance, reason)


class MemoryHoldTests(MemoryFixture):
    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_register_requires_explicit_journal_directory(self):
        # The sibling lock exists: finding it must not authorize an omitted directory.
        for options in ({}, {'launches': None}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                    memory().register(self.store, self.handle, *self.key, 'run', self.instance, **options)
                self.assertEqual(0, self.store._connection.execute(
                    'SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])
                self.assertIsNone(self.row('driver_entry'))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_register_uses_explicit_non_sibling_journal_directory(self):
        journal = LaunchJournal(self.directory / 'custom-journal')
        self.addCleanup(journal.release)
        memory().register(self.store, self.handle, *self.key, 'run', journal.instance,
                          launches=journal.directory)
        row = self.row('driver_entry')
        self.assertEqual(('held', journal.instance), (row['state'], row['instance']))
        info = (journal.directory / (journal.instance + '.lock')).stat()
        facts = json.loads(row['evidence'])
        self.assertEqual((info.st_dev, info.st_ino), (facts['lock_device'], facts['lock_inode']))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M3_close_and_takes_both_orders(self):
        self.register()
        self.now += b6.ORCHESTRATION_IDLE_MS
        self.assertEqual(0, self.owner.end_idle())
        self.prep()
        self.assertEqual('held', self.row('preparation')['state'])
        self.release('preparation')
        self.release('driver_entry')
        self.now += b6.ORCHESTRATION_IDLE_MS  # The preparation take is new activity.
        self.assertEqual(1, self.owner.end_idle())
        for call in (self.register, self.prep):
            with self.assertRaises(HistoryError):
                call()
        self.assertEqual('released', self.row('preparation')['state'])
        other = self.open('other')
        self.handle = other['orchestration_handle']
        self.key = ('hrn_' + 'c' * 48, 1)
        self.register()
        self.close(other)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.prep()
        self.assertIsNone(self.row('preparation'))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M5_recovery_once_and_graceful_stop(self):
        self.register()
        self.prep()
        # A free, kept instance lock is positive evidence; repeated starts preserve tombstones.
        self.journal.keep_lock = True
        self.journal.release()
        for _ in range(3):
            memory().recover(self.store, self.journal.directory, 'current')
        for kind in ('driver_entry', 'preparation'):
            self.assertEqual(('released', 'service_stopped'),
                (self.row(kind)['state'], self.row(kind)['release_kind']))
        before = self.row('driver_entry')
        self.reopen()
        memory().recover(self.store, self.journal.directory, 'current')
        self.assertEqual(before, self.row('driver_entry'))
        self.assertTrue(project_holds_schema.register_consistent(self.store._connection))
        with self.assertRaises(HistoryError):
            self.register()

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_registration_rollback_validator_and_status(self):
        original = project_holds_schema.take_hold
        def refuse(*args, **kwargs):
            if args[2] == 'driver_entry':
                raise HistoryError('history_unavailable')
            return original(*args, **kwargs)
        with mock.patch.object(project_holds_schema, 'take_hold', side_effect=refuse):
            with self.assertRaises(HistoryError):
                self.register()
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])
        self.assertIsNone(self.row('driver_entry'))
        self.register()
        self.prep()
        from codex_agents.project_holds_report import holds_report
        from codex_agents.holds_report_contracts import checked_holds_report
        report = checked_holds_report(holds_report(self.store))
        for kind in ('driver_entry', 'preparation'):
            self.assertEqual('tracked', report['project_holds'][kind]['tracking'])
            self.assertEqual(1, report['project_holds'][kind]['held'])
        self.assertEqual({'tracking': 'not_tracked', 'held': 0, 'releasing': 0,
            'past_due': 0, 'next_eligible': None, 'oldest_taken_at_ms': None},
            report['project_holds']['orchestration'])
        self.store._connection.execute("UPDATE project_holds SET instance='foreign' WHERE kind='preparation'")
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_live_memory_prevents_attempt_source_purge(self):
        from tests.learning_support import assignment
        from codex_agents.result_contracts import ResultLeaseBinding
        self.register()
        binding = ResultLeaseBinding(**self.scope.as_data(), orchestration_handle=self.handle,
            run_handle=self.key[0], attempt=1, run_id='run', claim_request_id='claim', claim_request_digest='d'*64)
        self.store.reserve_result_lease(binding, assignment())
        lease = self.store.release_unsent_result_lease(binding)
        self.assertFalse(self.store._attempt_released(lease))
        self.release('driver_entry')
        self.assertTrue(self.store._attempt_released(lease))

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M4_sweeper_cannot_prune_memory_stopping_evidence(self):
        self.register()
        self.journal.keep_lock = True
        self.journal.release()
        successor = LaunchJournal(self.journal.directory)
        self.addCleanup(successor.release)
        successor.keep_lock = True
        successor.memory_instances = None  # Writer start pass has not answered yet.
        successor.forget_instance(self.instance)
        self.assertTrue((successor.directory / (self.instance + '.lock')).exists())
        successor.memory_instances = frozenset({self.instance})
        successor.forget_instance(self.instance)
        self.assertTrue((successor.directory / (self.instance + '.lock')).exists())
        memory().recover(self.store, successor.directory, successor.instance)
        successor.memory_instances = frozenset()
        successor.forget_instance(self.instance)
        self.assertFalse((successor.directory / (self.instance + '.lock')).exists())

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_validator_refuses_missing_tracked_registration_hold(self):
        self.register()
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='driver_entry'")
        self.assertFalse(project_holds_schema.register_consistent(self.store._connection))
        with self.assertRaisesRegex(HistoryError,'history_incompatible'):
            self.reopen()

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_status_memory_blocks_close_eligibility_and_has_holds(self):
        from codex_agents.project_holds_report import holds_report
        from codex_agents.recovery_report import RecoveryNotes, RECOVERY_REASONS
        self.store._recovery_notes = RecoveryNotes()
        self.store._recovery_notes.publish(dict.fromkeys(RECOVERY_REASONS,0))
        untracked = {'tracking': 'not_tracked', 'held': 0, 'releasing': 0,
            'past_due': 0, 'next_eligible': None, 'oldest_taken_at_ms': None}
        self.assertEqual(untracked, holds_report(self.store)['project_holds']['orchestration'])
        self.register()
        self.now += b6.ORCHESTRATION_IDLE_MS
        report = holds_report(self.store)
        self.assertEqual(1,report['project_holds']['driver_entry']['held'])
        self.assertEqual(untracked,report['project_holds']['orchestration'])
        self.assertEqual(1,report['recovery_pending']['reasons']['has_holds'])
        self.release('driver_entry')
        report = holds_report(self.store)
        self.assertEqual(untracked,report['project_holds']['orchestration'])
        self.assertEqual(0,report['recovery_pending']['reasons']['has_holds'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M7_registration_refusal_preserves_duty(self):
        self.store.record_open_binding(*self.key, 'run:' + 'a' * 64, 'd' * 64, b6.CLIENT,
                                       instance=self.instance, now_ms=self.now)
        self.close(self.snapshot)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.register()
        self.assertIsNone(self.row('driver_entry'))
        self.assertEqual('held', self.row('open_binding')['state'])
        self.store.mark_open_withdrawing(*self.key, 'refused_before_claim',
            project_id=self.store.project_id, runtime_digest=self.scope.runtime_digest)
        self.assertEqual('releasing', self.row('open_binding')['state'])


class DeferredLane:
    """Only scheduling is controlled: every queued operation uses the actual store."""
    def __init__(self, store):
        self.store, self.requests = store, []

    def memory_hold(self, operation, *args):
        request = SimpleNamespace(state='queued', result=None, reason=None)
        self.requests.append((request, operation, args))
        return request

    def flush(self):
        for request, operation, args in self.requests:
            if request.state != 'queued':
                continue
            try:
                request.result = getattr(memory(), operation)(self.store, *args)
                request.state = 'done'
            except HistoryError as error:
                request.state, request.reason = 'failed', error.reason_code


class DriverMemoryTests(MemoryFixture):
    def driver(self):
        driver = object.__new__(project_driver.RunDriver)
        driver._runtime = SimpleNamespace(persistence=DeferredLane(self.store))
        driver._journal = self.journal
        driver._runs, driver._retired, driver._giving_back, driver._cleanup_retry = {}, {}, [], {}
        driver._clock = lambda: self.now
        driver._preparation = mock.Mock()
        driver._preparation.snapshot.return_value = {'state': 'cleaned'}
        driver._preparation.submit.side_effect = lambda context: object()
        driver._preparation.capacity.return_value = {'jobs': 0}
        driver._memory = memory().DriverMemory(driver)
        return driver

    def make_run(self, driver, key=None):
        key = key or self.key
        run = project_driver._Run(key, self.scope, b6.CLIENT,
            SimpleNamespace(orchestration_handle=self.handle), 'run', 'd' * 64)
        run.reporting = SimpleNamespace(state='done')
        driver._runs[key] = run
        return run

    def tick(self, driver):
        driver._runtime.persistence.flush()
        driver._memory.step()

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M8_submit_refused_compensates(self):
        from codex_agents.codex_preparation import PreparationError
        self.register()
        driver = self.driver()
        run = self.make_run(driver)
        run.preparation = object()
        driver._preparation.submit.side_effect = PreparationError('preparation_full')
        driver._fail = lambda value, reason: setattr(value, 'state', 'failed')
        driver._memory.prepare(run)
        driver._preparation.submit.assert_not_called()
        self.tick(driver)
        self.assertEqual('failed', run.state)
        self.assertEqual('held', self.row('driver_entry')['state'])
        self.tick(driver)
        self.assertEqual('submit_refused', self.row('preparation')['release_kind'])
        self.assertIsNone(run.prepared)

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M2_unknown_cleanup_keeps_both_holds(self):
        self.register()
        self.prep()
        driver = self.driver()
        run = self.make_run(driver)
        run.prepared, run.state, run.closed = object(), 'failed', True
        driver._memory.preparations[run.prepared] = (run.key, 'retired')
        driver._preparation.snapshot.return_value = {'state': 'unknown'}
        driver._give_back(run)
        for _ in range(15):
            self.now += 1000
            driver._retire_given_back()
            driver._memory.step()
        self.tick(driver)  # Observe the queued status write, without freeing either slot.
        driver._preparation.retire.assert_not_called()
        self.assertIn(run.key, driver._runs)
        for kind in ('driver_entry', 'preparation'):
            self.assertEqual('held', self.row(kind)['state'])
        self.assertEqual('cleanup_unknown', json.loads(self.row('preparation')['evidence'])['reason'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M2_abandon_does_not_drop_entry_before_preparation_retirement(self):
        import hashlib
        self.register()
        self.prep()
        driver = self.driver()
        run = self.make_run(driver)
        run.closed,run.state,run.prepared = True,'failed',object()
        driver._memory.preparations[run.prepared]=(run.key,'retired')
        driver._reply=mock.Mock()
        line=json.dumps({'protocol':1,'operation':'run.abandon','run_handle':run.key[0],
            'attempt':1,'inputs_digest':run.inputs_digest}).encode()
        driver._request(object(),SimpleNamespace(request_digest=hashlib.sha256(line).hexdigest(),
            host_client=b6.CLIENT,context=b'{}'),line+b'\n')
        self.assertIn(run.key,driver._runs)
        self.assertEqual('held',self.row('driver_entry')['state'])
        self.assertEqual('held',self.row('preparation')['state'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_existing_failed_view_oracle_remains_valid(self):
        # Reuse the unchanged pure assertion from B3 without its socket service setup.
        from tests.test_open_withdrawal import DriverWithdrawalTests
        DriverWithdrawalTests.test_W5_a_failed_run_is_kept_for_a_late_prepare_for_a_bounded_time_and_then_released(self)

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_M6_autonomous_retirement(self):
        self.register()
        driver = self.driver()
        driver._fenced, driver._lost = {}, {}
        driver._earlier_listed = driver._swept = True
        driver._earlier_fences = {}
        run = self.make_run(driver)
        run.closed, run.state = True, 'failed'
        run.open_mark = SimpleNamespace(state='done')
        run.duty_done_at = self.now
        driver._memory.drop(run)
        self.tick(driver)
        self.assertNotIn(run.key, driver._runs)
        self.assertEqual('retired', self.row('driver_entry')['release_kind'])
        self.assertEqual('run_closed', driver._unheld_reply(run.key[0], b6.CLIENT)['reason_code'])
        # Existing idle timer enters the same automatic retirement path without another client request.
        run = self.make_run(driver, ('hrn_' + 'c' * 48, 1))
        run.opened_at = self.now - project_driver.REGISTERED_IDLE_SECONDS
        driver._fail = mock.Mock()
        project_driver.RunDriver._advance(driver, run)
        self.assertEqual('registered_idle', run.withdraw_reason)
        driver._fail.assert_called_once_with(run, 'run_abandoned')

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_L4_never_launch_predicates(self):
        driver = self.driver()
        run = self.make_run(driver)
        run.state = 'failed'
        self.assertTrue(driver._memory.can_hand_over(run))
        for field, value in (('state', 'fencing'), ('receipt', {}), ('binding', object()),
                             ('future', object()), ('fence_at', 0.0)):
            original = getattr(run, field)
            setattr(run, field, value)
            self.assertFalse(driver._memory.can_hand_over(run), field)
            setattr(run, field, original)

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_L6_live_handoff(self):
        # The store's handoff must retain the armed obligation and transfer only its ownership.
        self.register()
        self.prep()
        identity = project_holds_schema.attempt_identity(*self.key)
        with self.store._transaction():
            # A real sealed lease is supplied by the established lease fixture in the integration test below.
            self.store._connection.execute("UPDATE project_holds SET state='releasing',release_kind='handed_over' "
                "WHERE kind='driver_entry' AND identity=?", (identity,))
        driver = self.driver()
        run = self.make_run(driver)
        run.state, run.closed = 'failed', True
        run.open_mark = SimpleNamespace(state='done')
        handle = run.prepared = object()
        driver._memory.preparations[handle] = (run.key, 'never_launch_local')
        driver._give_back(run)
        self.assertEqual('held', self.row('preparation')['state'])
        driver._retire_given_back()
        self.tick(driver)
        driver._memory.drop(run, reason='handed_over')
        self.tick(driver)
        self.assertNotIn(run.key, driver._runs)
        self.assertEqual('never_launch_local', self.row('preparation')['release_kind'])
        self.assertEqual('handed_over', self.row('driver_entry')['release_kind'])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_I1_take_and_release_reply_interleavings(self):
        driver = self.driver()
        rng, runs, seen = random.Random(129104), [], set()
        for digit in 'bce':
            key = ('hrn_' + digit*48,1)
            self.register(key)
            run = self.make_run(driver,key)
            run.preparation = object()
            runs.append(run)
            driver._memory.prepare(run)
        for step in range(1000):
            run = rng.choice(runs)
            if step >= 400 and run.prepared is not None:
                driver._give_back(run)
            if rng.randrange(4)==0:
                driver._retire_given_back()
            if step >= 700:
                driver._memory.drop(run)
            if rng.randrange(4)==0:
                driver._runtime.persistence.flush()
            # Sample before observing completed requests, including committed-but-unobserved replies.
            for current in runs:
                for kind in ('driver_entry','preparation'):
                    row = self.row(kind,current.key)
                    present = current.key in driver._runs if kind=='driver_entry' else any(
                        key==current.key for key,_ in driver._memory.preparations.values())
                    take = driver._memory.takes.get(current.key) if kind=='preparation' else None
                    release = driver._memory.releases.get((kind,current.key))
                    if take is not None:
                        label = 'P0prime' if take[1].state=='done' else 'P0'
                        self.assertFalse(present)
                        self.assertEqual('held' if label=='P0prime' else None,None if row is None else row['state'])
                    elif present:
                        label = 'C'
                        self.assertIsNone(release)
                        self.assertEqual('held',row['state'])
                    elif release is not None:
                        label = 'Dprime' if release[1] is not None and release[1].state=='done' else 'D'
                        self.assertEqual('released' if label=='Dprime' else 'held',row['state'])
                    else:
                        label = 'A'
                        self.assertTrue(row is None or row['state']=='released')
                    seen.add(label)
            if rng.randrange(3)==0:
                driver._memory.step()
        for _ in range(3):
            self.tick(driver)
        self.assertEqual({'A','C','D','Dprime','P0','P0prime'},seen)
        self.assertEqual({},driver._runs)
        self.assertEqual(0,self.store._connection.execute("SELECT COUNT(*) FROM project_holds WHERE kind IN "
            "('driver_entry','preparation') AND state<>'released'").fetchone()[0])

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_retired_tombstones_rotate_past_bound(self):
        driver = self.driver()
        first = None
        for number in range(project_driver.MAX_RETIRED_RUNS + 1):
            run = self.make_run(driver, ('hrn_' + format(number, '048x'), 1))
            run.reporting = None  # Refused before registration: only the bounded memory tombstone exists.
            first = run.key if first is None else first
            self.assertTrue(driver._memory.drop(run))
        self.assertEqual(project_driver.MAX_RETIRED_RUNS, len(driver._retired))
        self.assertNotIn(first, driver._retired)
        self.assertEqual({}, driver._runs)

    @requirements('RS-B-4', why_not_scenario=WHY)
    def test_I1_interleaved_observations(self):
        driver = self.driver()
        rng = random.Random(1291)
        runs = []
        for digit in 'bce':
            key = ('hrn_' + digit * 48, 1)
            self.register(key)
            run = self.make_run(driver, key)
            run.preparation = object()
            runs.append(run)
            driver._memory.prepare(run)
        for _ in range(1000):
            run = rng.choice(runs)
            if run.state == 'registered':
                driver._memory.prepare(run)
            if rng.randrange(2):
                driver._runtime.persistence.flush()
            if rng.randrange(2):
                driver._memory.step()
            for current in runs:
                row = self.row('preparation', current.key)
                pending = driver._memory.takes.get(current.key)
                present = current.prepared is not None
                self.assertTrue((present and row is not None and row['state'] == 'held') or
                                (not present and pending is not None))
        self.tick(driver)
        self.assertEqual(3, sum(run.prepared is not None for run in runs))
        self.assertEqual(3, self.store._connection.execute("SELECT COUNT(*) FROM project_holds "
            "WHERE kind='preparation' AND state='held'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
