"""O0c P1: a cleanup receipt counts only while its run's project can still release it
(ANALYSIS-0.4.0-lifetime-caps.md, P1; written before the code, the requirement tests fail first on 0.8.38 at 4b1b428).

`account_cleanup_receipts` counted every receipt without a normal release against MAX_RELEASE_RESERVATIONS (4,096
per project). A lost run (account_lost_run) or a crashed run (the crash rule, `_release_slot_locked`) has its slot
released by the account's written decision, but its project can never release its receipt any more, so the receipt
counted for good; at the limit every new run of the project was refused `claim_cleanup_unavailable`.

Requirements (P1):
- P1-1 a receipt counts only while its run's project can still release it: the account's `lost` or `crashed`
  release ends it in the same transaction, its kind the decision's (the register row, run_holds.release_kind);
- P1-2 a receipt is never released twice: a slot released normally gets no decision; a decided slot gets no
  second decision;
- P1-3 a late normal release after a lost or crashed end is refused plainly (`claim_cleanup_conflict`, whatever
  order the late terminal and the decision came in), changing nothing;
- P1-4 existing installs: receipts of runs already lost or crashed before the upgrade stop counting at the
  first open (RS-A's 14 -> 15 adoption), and a reopen changes nothing;
- P1-5 (acceptance, 0.3.4 ResultLimitTests shape) with the limit set to 2 before the services open, three lost
  runs and one crashed run, then a normal run succeeds.

Stages: the unit-fixture classes use a real ledger and real account transactions (the
test_account_cleanup_release fixture); the limit test runs the in-process collector, account and project services
(the test_lost_run_settlement fixture).
"""
import hashlib
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_terminal_delivery as fixtures
from tests.test_account_cleanup_release import AccountCleanupReleaseTests
from tests.test_bridge import null_filled, result
from tests.test_lost_run_settlement import LostRunAfterStopTests

from codex_agents import account_cleanup, learning_store, project_driver
from codex_agents.account_api import AccountError
from codex_agents.active_run_capacity import CRASHED_CLAIM_GRACE_MS
from codex_agents.cleanup_contracts import CleanupEvidence, encode_cleanup_evidence
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger


class ReceiptDecisionTests(AccountCleanupReleaseTests):
    """One run with its cleanup receipt reserved; its terminal is delivered at setUp unless a test asks
    for the decision first (`decided_before_terminal`)."""

    decided_before_terminal = None  # a release kind: the decision is written before the terminal arrives

    def setUp(self):
        # AccountCleanupReleaseTests.setUp, with the decision placed where a test needs it.
        self.base = fixtures.AccountTerminalDeliveryTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.key = self.base.fixture.key
        self.binding, self.source, self.caller = self.base.expected, 'project', self.base.caller_scope
        if self.decided_before_terminal is not None:
            self.decide(self.decided_before_terminal)
            self.base.fixture.base.stamp += 1_000  # the late terminal comes after the decision
        self.terminal = self.base.call()
        self.base.fixture.store.acknowledge_account_terminal(self.base.binding, canonical_json(self.terminal))
        self.cleanup = encode_cleanup_evidence(self.key, CleanupEvidence(self.binding, self.base.grant,
            self.base.delivery, self.terminal['receipt_digest']))
        from codex_agents.account_api import AccountRequest
        from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
        self.request = AccountRequest(1, 'account.release_capacity', 'original-release', self.binding.project_id,
                                      COLLECTOR_RUNTIME_DIGEST, self.base.submission.payload())
        self.service = self.make_service()

    @property
    def project(self):
        return self.binding.project_id

    @property
    def handle(self):
        return self.base.grant.handle_digest

    def live(self):
        return account_cleanup.live_cleanup_receipts(self.ledger, self.project)

    def journal(self):
        return tuple(self.ledger._connection.execute('SELECT * FROM account_cleanup_receipts').fetchone())

    def slot(self):
        return tuple(self.ledger._connection.execute(
            'SELECT a.released_at_ms,a.release_digest,r.state,r.release_kind,r.receipt FROM active_run_capacity a '
            "JOIN run_holds r ON r.kind='run_slot' AND r.project_id=a.project_id AND r.identity=a.handle_digest "
            'WHERE a.project_id=? AND a.handle_digest=?', (self.project, self.handle)).fetchone())

    def decide(self, kind):
        """The account's own end of the slot, as each path writes it: a lost settlement abandons the binding
        `service_lost` and releases the slot `lost` in one transaction (account_lost_run); the crash rule
        releases it `crashed` (the sweep). Returns whether a slot was released."""
        with self.ledger._account_transaction():
            if kind == 'lost':
                self.ledger._connection.execute(
                    "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                    "WHERE project_id=? AND handle_digest=? AND binding_state='pending'", (self.project, self.handle))
            return self.ledger._release_slot_locked(self.project, self.handle, kind, self.ledger._clock_ms(),
                                                    self.ledger._capacity_clock_reference())

    def assert_refused_plainly(self):
        """P1-3: a fixed refusal, the same every time, and nothing written."""
        journal, slot = self.journal(), self.slot()
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaises(AccountError) as caught:
                self.call()
            self.assertEqual('claim_cleanup_conflict', caught.exception.reason_code)
        self.assertEqual(journal, self.journal())
        self.assertEqual(slot, self.slot())
        self.assertIsNone(self.ledger._connection.execute(
            'SELECT cleanup_digest FROM account_cleanup_receipts').fetchone()[0])
        self.assertEqual(0, self.live())

    # P1-1
    def assert_decision_ends_the_receipt(self, kind):
        self.assertEqual(1, self.live())
        self.assertTrue(self.decide(kind))
        self.assertEqual(0, self.live())
        self.assertEqual(kind, self.slot()[3])  # the kind is the decision's, in the register row
        self.assertEqual({'live': 0, 'limit': account_cleanup.MAX_RELEASE_RESERVATIONS},
                         self.ledger.account_capacity(self.project)['cleanup_receipts'])

    def test_P1_1_a_lost_release_ends_the_receipt(self):
        self.assert_decision_ends_the_receipt('lost')

    def test_P1_1_a_crashed_release_ends_the_receipt(self):
        self.assert_decision_ends_the_receipt('crashed')

    def test_P1_1_the_receipt_ends_in_the_decision_s_own_transaction(self):
        """Rolled back with the decision: the receipt counts again exactly when the slot is held again."""
        class Stop(Exception):
            pass
        with self.assertRaises(Stop), self.ledger._account_transaction():
            self.assertTrue(self.ledger._release_slot_locked(self.project, self.handle, 'crashed',
                                                             self.ledger._clock_ms(),
                                                             self.ledger._capacity_clock_reference()))
            self.assertEqual(0, self.live())  # inside the transaction, already ended
            raise Stop
        self.assertEqual(1, self.live())
        self.assertIsNone(self.slot()[0])

    def test_P1_1_a_receipt_still_counts_while_its_project_can_release_it(self):
        """Control: an unreleased slot with no decision keeps its receipt counted."""
        self.assertEqual(1, self.live())
        self.assertEqual(('held', None), self.slot()[2:4])

    # P1-2
    def test_P1_2_a_normally_released_receipt_gets_no_decision(self):
        first = self.call()
        journal, slot = self.journal(), self.slot()
        self.assertEqual(0, self.live())
        for kind in ('lost', 'crashed'):
            with self.subTest(kind=kind):
                with self.ledger._account_transaction():
                    self.assertFalse(self.ledger._release_slot_locked(
                        self.project, self.handle, kind, self.ledger._clock_ms(),
                        self.ledger._capacity_clock_reference()))
        row = self.ledger._connection.execute('SELECT deadline_at_ms,execution_deadline_ns FROM active_run_capacity '
                                              'WHERE handle_digest=?', (self.handle,)).fetchone()
        with mock.patch.object(self.ledger, '_clock_ms', return_value=row[0] + CRASHED_CLAIM_GRACE_MS + 1), \
                mock.patch.object(self.ledger, '_capacity_clock_reference',
                                  return_value=row[1] + CRASHED_CLAIM_GRACE_MS * 1_000_000):
            self.ledger.release_due_account_holds(limit=64)
        self.assertEqual(journal, self.journal())
        self.assertEqual(slot, self.slot())
        self.assertEqual({**first, 'known': True}, self.call())
        self.assertEqual(0, self.live())

    def test_P1_2_a_decided_receipt_gets_no_second_decision(self):
        self.assertTrue(self.decide('lost'))
        journal, slot = self.journal(), self.slot()
        self.assertFalse(self.decide('crashed'))
        self.assertFalse(self.decide('lost'))
        self.assertEqual((journal, slot), (self.journal(), self.slot()))
        self.assertEqual(0, self.live())

    # P1-3
    def test_P1_3_a_late_release_after_a_crashed_end_is_refused_plainly(self):
        self.assertTrue(self.decide('crashed'))
        self.assert_refused_plainly()

    def test_P1_3_a_late_release_after_a_lost_end_is_refused_plainly(self):
        self.assertTrue(self.decide('lost'))
        self.assert_refused_plainly()


class LateTerminalReceiptTests(ReceiptDecisionTests):
    """The crash rule fired first; the run's terminal arrives afterwards (accepted: RS-A round 1 F9)."""

    decided_before_terminal = 'crashed'

    def test_P1_3_a_late_release_after_a_late_terminal_is_refused_plainly(self):
        self.assertEqual('crashed', self.slot()[3])
        self.assertIsNotNone(self.ledger._connection.execute(
            'SELECT terminal_record_id FROM account_cleanup_receipts').fetchone()[0])  # the late terminal is kept
        self.assert_refused_plainly()


class LateTerminalAfterLostTests(ReceiptDecisionTests):
    decided_before_terminal = 'lost'

    def test_P1_3_a_late_release_after_a_late_terminal_of_a_lost_run_is_refused_plainly(self):
        self.assertEqual('lost', self.slot()[3])
        self.assert_refused_plainly()


class StrandedReceiptAdoptionTests(ReceiptDecisionTests):
    """P1-4: a ledger as 0.8.37 left it (schema 14: no register, a lost settlement only abandons the binding),
    upgraded by the authorized path."""

    def schema_fourteen(self):
        from codex_agents.run_holds_schema import RUN_HOLDS_OBJECTS
        with self.ledger._account_transaction():
            for kind, name, _ in RUN_HOLDS_OBJECTS:
                if kind == 'trigger':
                    self.ledger._connection.execute(f'DROP TRIGGER IF EXISTS {name}')
            self.ledger._connection.execute('DROP TABLE IF EXISTS run_holds')
            self.ledger._connection.execute('PRAGMA user_version=14')
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.project,
                                                          ledger_schema_version=14)
        raw = canonical_json({'backup_sha256': hashlib.sha256(canonical_json(body)).hexdigest(), 'body': body})
        path = self.ledger._write_owner_file('backup', self.project, 'schema14_receipts_backup', raw)
        stamp = self.base.fixture.base.stamp
        self.ledger._register_stored_file(self.project, file_id='schema14_receipts_backup', kind='backup', path=path,
                                          digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=stamp + 60_000)
        self.ledger.close()

    def opened(self, now, monotonic, *, upgrade=True):
        operation = self.base.fixture.base  # the claim fixture that owns the ledger
        with mock.patch.object(LearningLedger, '_clock_ms', return_value=now), \
                mock.patch.object(LearningLedger, '_capacity_clock_reference', return_value=monotonic):
            ledger = LearningLedger(operation.fixture.fixture.state, client_capabilities=operation.fixture.fixture.clients,
                                    allow_schema_upgrade=upgrade)
        self.addCleanup(ledger.close)
        operation.ledger = ledger
        self.service = self.make_service()  # the account service of the reopened ledger
        return ledger

    def capacity(self):
        return self.ledger._connection.execute(
            'SELECT claimed_at_ms,deadline_at_ms,clock_reference_ns FROM active_run_capacity WHERE handle_digest=?',
            (self.handle,)).fetchone()

    def assert_adopted(self, kind, now, monotonic):
        self.assertEqual(1, self.live())
        self.schema_fourteen()
        self.opened(now, monotonic)
        self.assertEqual(kind, self.slot()[3])
        self.assertEqual(0, self.live())
        register, journal = self.ledger._connection.execute('SELECT * FROM run_holds').fetchall(), self.journal()
        self.ledger.close()
        self.opened(now, monotonic, upgrade=False)  # P1-4: idempotent, a reopen changes nothing
        self.assertEqual(0, self.live())
        self.assertEqual([tuple(r) for r in register],
                         [tuple(r) for r in self.ledger._connection.execute('SELECT * FROM run_holds').fetchall()])
        self.assertEqual(journal, self.journal())
        self.assert_refused_plainly()

    def test_P1_4_a_receipt_of_a_run_lost_before_the_upgrade_stops_counting(self):
        row = self.capacity()
        with self.ledger._account_transaction():  # as 0.8.37's lost settlement left it: binding settled, slot held
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                'WHERE project_id=? AND handle_digest=?', (self.project, self.handle))
        self.assert_adopted('lost', row['claimed_at_ms'] + 1, row['clock_reference_ns'] + 1)

    def test_P1_4_a_receipt_of_a_run_crashed_before_the_upgrade_stops_counting(self):
        row = self.capacity()
        self.assert_adopted('crashed', row['deadline_at_ms'] + CRASHED_CLAIM_GRACE_MS + 1,
                            row['clock_reference_ns'] - 1)  # after a reboot

    def test_P1_4_a_receipt_of_a_live_run_keeps_counting_after_the_upgrade(self):
        row = self.capacity()
        self.schema_fourteen()
        self.opened(row['claimed_at_ms'] + 1, row['clock_reference_ns'] + 1)
        self.assertEqual(('held', None), self.slot()[2:4])
        self.assertEqual(1, self.live())


class CleanupReceiptLimitTests(LostRunAfterStopTests):
    """P1-5, 0.3.4's ResultLimitTests shape: the receipt limit set to 2 before the services open."""

    def setUp(self):
        from codex_agents import project_service
        from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
        # A lost run's result reservation stays charged until it expires (route time limit plus 30 minutes:
        # a time bound, P2's family, not this cap), so the project's space for results is widened here to
        # let five runs in one go reach the account's receipt limit, which is what this test measures.
        wide = project_service._result_capacity()
        wide = ResultCapacity(wide.max_entries, 8 * MAX_RESULT_BODY_BYTES, wide.reservation_ms, wide.retention_ms)
        for patcher in (mock.patch.object(account_cleanup, 'MAX_RELEASE_RESERVATIONS', 2),
                        mock.patch.object(project_service, '_result_capacity', return_value=wide)):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def receipts(self):
        return self.cli('learning-status')['account_capacity']['cleanup_receipts']

    def stopped_while_running(self, before_stop=None):
        """A run whose child is running when its service stops (as on SIGTERM); the service stays stopped.
        `before_stop` runs while the run is held, its service still up."""
        original, seen = project_driver.RunDriver._advance, []

        def held(driver, run):
            if run.state == 'ready':
                seen.append(run.state)
                return None
            return original(driver, run)

        launched = len(self.launches())
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.object(project_driver.RunDriver, '_advance', held):
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: (seen and len(self.launches()) > launched) or dispatch.poll() is not None,
                          seconds=30)
            if not seen:  # refused before its launch: say why (0.8.38: claim_cleanup_unavailable)
                output, errors = dispatch.communicate(timeout=30)
                self.fail(f'run {launched + 1} never launched: {output.decode(errors="replace")[-600:]} '
                          f'receipts {self.receipts()}')
            if before_stop is not None:
                before_stop()
            self.stop_host()
        dispatch.communicate(timeout=30)
        return orchestration

    def test_P1_5_more_lost_and_crashed_runs_than_the_limit_never_stop_a_normal_run(self):
        """Limit 2: three lost runs and one crashed run, then a normal run succeeds. Under 0.8.38 the third
        lost run was refused `claim_cleanup_unavailable`: each lost run's receipt counted for good."""
        ledger = self.collector_ledger
        for number in range(3):
            with self.subTest(lost=number):
                orchestration = self.stopped_while_running()
                self.start_host()  # the next service settles it lost
                self.wait_for(lambda: self.counted() == 0, seconds=30)
                self.assertEqual(('abandoned', 'service_lost', 1), self.claimed_bindings()[-1])
                self.assertEqual({'live': 0, 'limit': 2}, self.receipts())
        with self.subTest(crashed=True):
            def crash():
                """While its service still holds it, the crash rule frees it: past its deadline and grace on
                both clocks, so the account writes `crashed` before any settlement."""
                row = ledger._connection.execute(
                    'SELECT handle_digest,deadline_at_ms,execution_deadline_ns FROM active_run_capacity '
                    'WHERE released_at_ms IS NULL').fetchone()
                with mock.patch.object(ledger, '_clock_ms', return_value=row[1] + CRASHED_CLAIM_GRACE_MS + 1), \
                        mock.patch.object(ledger, '_capacity_clock_reference',
                                          return_value=row[2] + CRASHED_CLAIM_GRACE_MS * 1_000_000):
                    ledger.release_due_account_holds(limit=64)
                crashed.append(row[0])
            crashed = []
            orchestration = self.stopped_while_running(before_stop=crash)
            self.assertEqual('crashed', ledger._connection.execute(
                "SELECT release_kind FROM run_holds WHERE kind='run_slot' AND identity=?", crashed).fetchone()[0])
            self.start_host()
            self.assertEqual({'live': 0, 'limit': 2}, self.receipts())
        self.assertEqual(4, ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])
        self.assertEqual('result', self.next_run(orchestration, 'after')['bridge_report']['outcome'])
        self.wait_for(lambda: self.receipts()['live'] == 0, seconds=30)  # released normally once it ends
        self.assertEqual(5, ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited ones run in their own modules."""
    suite = unittest.TestSuite()
    for case in (ReceiptDecisionTests, LateTerminalReceiptTests, LateTerminalAfterLostTests,
                 StrandedReceiptAdoptionTests, CleanupReceiptLimitTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
