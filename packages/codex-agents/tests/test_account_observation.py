"""Original-owner journal and real inert provider probes; enrollment is synthetic."""
from dataclasses import replace
import threading
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_api as fixtures
from tests.learning_support import fake_provider_tools, trusted_tools

from codex_agents import account_observation, providers
from codex_agents.account_observation import (
    AccountObservationOwner, ObservationContext, ProbeCompletion, run_observation_probe,
)
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_store import DEFAULT_RETENTION_MS, LearningLedger
from codex_agents.provider_observation_contracts import ObservationError, ObservationInput
from codex_agents.sql_read_budget import SQLReadLimits


class AccountObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountApiTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger, self.views = self.fixture.ledger, self.fixture.views
        self.owner = AccountObservationOwner(self.views, SQLReadLimits(10_000, 1000, 1_048_576))
        self.context = ObservationContext(str(self.fixture.roots[0]))
        self.allowed = True
        self.tools = self.fixture.base / 'fake-tools'
        fake_provider_tools(self.tools)
        self.request = ObservationInput('codex')

    def reserve(self, identity='original', *, request=None, context=None, owner=None):
        return (owner or self.owner).reserve(self.fixture.projects[0], fixtures.RUNTIME, 'b' * 64, identity,
            request or self.request, context or self.context, lambda: self.allowed)

    def probe(self, reservation):
        with trusted_tools(self.tools):
            return run_observation_probe(reservation.capture)

    def provider_rows(self):
        return self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE source='provider'").fetchone()[0]

    def test_actual_probe_off_owner_does_not_block_other_project_and_known_reply_never_reprobes(self):
        reservation = self.reserve()
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original, completions, failures = providers.observe_codex, [], []
        owner_thread = threading.current_thread()
        sql_threads = []
        self.ledger._connection.set_trace_callback(lambda sql: sql_threads.append(threading.current_thread()))
        self.addCleanup(self.ledger._connection.set_trace_callback, None)

        def held(*args, **kwargs):
            self.assertIsNot(threading.current_thread(), owner_thread)
            ready.set()
            if not resume.wait(5):
                raise AssertionError('fixture probe was not released')
            return original(*args, **kwargs)

        def run():
            try:
                completions.append(run_observation_probe(reservation.capture))
            except BaseException as error:
                failures.append(error)

        with trusted_tools(self.tools), mock.patch.object(providers, 'observe_codex', new=held):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual('active', self.views.project(self.fixture.projects[1])['status'])
                self.assertEqual(0, self.provider_rows())
            finally:
                resume.set()
                thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, len(completions))
        fresh = self.owner.complete(reservation, completions[0])
        self.assertFalse(fresh['known'])
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('reprobe')):
            known = self.reserve()
        self.assertEqual({**fresh, 'known': True}, known)
        self.assertTrue(all(thread is owner_thread for thread in sql_threads))

    def test_changed_request_or_context_does_not_rebind_original_pending_work(self):
        reservation = self.reserve()
        self.assertIs(reservation, self.reserve())
        for changes in ({'request': ObservationInput('claude')},
                        {'context': ObservationContext(str(self.fixture.roots[1]))},
                        {'context': ObservationContext(str(self.fixture.roots[0]))}):
            with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
                self.reserve(**changes)
        self.assertEqual(1, len(self.owner._pending))
        self.assertEqual(0, self.provider_rows())

    def test_revocation_after_probe_prevents_all_records_and_original_request_cannot_retry(self):
        reservation = self.reserve()
        completion = self.probe(reservation)
        self.allowed = False
        with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
            self.owner.complete(reservation, completion)
        self.assertEqual(0, self.provider_rows())
        self.allowed = True
        with self.assertRaisesRegex(ObservationError, 'observation_unavailable'):
            self.reserve()

    def test_revocation_after_first_actual_write_rolls_back_records_and_commit_journal(self):
        reservation = self.reserve()
        completion = self.probe(reservation)
        original = self.ledger._append_checked_locked

        def revoke(*args, **kwargs):
            value = original(*args, **kwargs)
            self.allowed = False
            return value

        with mock.patch.object(self.ledger, '_append_checked_locked', new=revoke):
            with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
                self.owner.complete(reservation, completion)
        self.assertEqual(0, self.provider_rows())
        self.assertEqual('pending', self.ledger._connection.execute('SELECT state FROM account_observation_requests').fetchone()[0])

    def test_copied_probe_completion_cannot_publish_for_original_capture(self):
        reservation = self.reserve()
        actual = self.probe(reservation)
        forged = ProbeCompletion(replace(actual.capture), actual.records, actual.reason)
        with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
            self.owner.complete(reservation, forged)
        self.assertFalse(self.owner.complete(reservation, actual)['known'])

    def test_restart_preserves_pending_request_without_probe_or_replacement(self):
        self.reserve()
        self.ledger.close()
        reopened = LearningLedger(self.fixture.state)
        self.addCleanup(reopened.close)
        owner = AccountObservationOwner(AccountOwnerViews(reopened), SQLReadLimits(10_000, 1000, 1_048_576))
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('restart probe')):
            with self.assertRaisesRegex(ObservationError, 'observation_unavailable'):
                self.reserve(owner=owner)
        self.assertEqual(1, reopened._connection.execute('SELECT COUNT(*) FROM account_observation_requests').fetchone()[0])

    def test_known_reply_cannot_resurrect_deleted_provider_metadata(self):
        reservation = self.reserve()
        self.owner.complete(reservation, self.probe(reservation))
        self.ledger._connection.execute("DELETE FROM records WHERE source='provider'")
        with self.assertRaisesRegex(ObservationError, 'observation_unavailable'):
            self.reserve()
        self.assertEqual(0, self.provider_rows())

    def test_missing_journal_companion_never_remints_original_request(self):
        reservation = self.reserve()
        self.owner.abort(reservation)  # No worker was started.
        self.ledger._connection.execute('DELETE FROM account_observation_requests')
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('missing-journal retry')):
            with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
                self.reserve()
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_requests').fetchone()[0])

    def test_missing_origin_companion_never_adopts_remaining_journal(self):
        reservation = self.reserve()
        self.owner.abort(reservation)
        self.ledger._connection.execute('PRAGMA foreign_keys=OFF')
        self.ledger._connection.execute('DELETE FROM account_observation_origins')
        self.ledger._connection.execute('PRAGMA foreign_keys=ON')
        with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
            self.reserve()
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_requests').fetchone()[0])

    def test_project_root_change_during_probe_withholds_all_original_metadata(self):
        reservation = self.reserve()
        completion = self.probe(reservation)
        self.ledger._connection.execute('UPDATE projects SET canonical_root=? WHERE project_id=?',
            (str(self.fixture.base / 'replacement-root'), self.fixture.projects[0]))
        with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
            self.owner.complete(reservation, completion)
        self.assertEqual(0, self.provider_rows())

    def test_root_change_after_first_actual_write_rolls_back_records_and_original_root(self):
        reservation = self.reserve()
        completion = self.probe(reservation)
        append = self.ledger._append_checked_locked
        def changed(*args, **kwargs):
            result = append(*args, **kwargs)
            self.ledger._connection.execute('UPDATE projects SET canonical_root=? WHERE project_id=?',
                (str(self.fixture.base / 'replacement-root'), self.fixture.projects[0]))
            return result
        with mock.patch.object(self.ledger, '_append_checked_locked', new=changed):
            with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
                self.owner.complete(reservation, completion)
        self.assertEqual(0, self.provider_rows())
        self.assertEqual(str(self.fixture.roots[0]), self.ledger._connection.execute(
            'SELECT canonical_root FROM projects WHERE project_id=?', (self.fixture.projects[0],)).fetchone()[0])

    def test_known_reply_cannot_rebind_original_registered_root(self):
        reservation = self.reserve()
        self.owner.complete(reservation, self.probe(reservation))
        self.ledger._connection.execute('UPDATE projects SET canonical_root=? WHERE project_id=?',
            (str(self.fixture.base / 'replacement-root'), self.fixture.projects[0]))
        with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
            self.reserve()

    def test_deadline_and_clock_rollback_withhold_probe_without_replacing_durable_request(self):
        for index, offset in enumerate((-1, self.owner._timeout)):
            with self.subTest(offset=offset):
                reservation = self.reserve('expired_' + str(index))
                completion = self.probe(reservation)
                with mock.patch.object(self.ledger, '_clock_ms', return_value=reservation.row['observed_at_ms'] + offset):
                    with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
                        self.owner.complete(reservation, completion)
                with self.assertRaisesRegex(ObservationError, 'observation_unavailable'):
                    self.reserve('expired_' + str(index))
        self.assertEqual(0, self.provider_rows())

    def test_known_reply_obeys_original_record_privacy_expiry_without_refresh(self):
        reservation = self.reserve()
        reply = self.owner.complete(reservation, self.probe(reservation))
        records = [tuple(row) for row in self.ledger._connection.execute('SELECT * FROM records')]
        with mock.patch.object(self.ledger, '_clock_ms', return_value=reply['observed_at_ms'] + DEFAULT_RETENTION_MS):
            with self.assertRaisesRegex(ObservationError, 'observation_unavailable'):
                self.reserve()
        self.assertEqual(records, [tuple(row) for row in self.ledger._connection.execute('SELECT * FROM records')])

    def test_034_R6_finished_observations_go_a_day_after_their_deadline_and_free_the_limit(self):
        """0.3.4 R6: in 0.8.29 every observation a project ever asked for counted against 4,096 and none
        was removed. An origin and its request now go together a day after the request's deadline."""
        reservation = self.reserve()
        self.owner.complete(reservation, self.probe(reservation))
        deadline = reservation.row['deadline_at_ms']

        def rows():
            return tuple(self.ledger._connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('account_observation_origins', 'account_observation_requests'))
        self.assertEqual((1, 1), rows())
        self.ledger.sweep_account_retention(now_ms=deadline + account_observation.OBSERVATION_REPLAY_MS)
        self.assertEqual((1, 1), rows())
        self.ledger.sweep_account_retention(now_ms=deadline + account_observation.OBSERVATION_REPLAY_MS + 1)
        self.assertEqual((0, 0), rows())
        with mock.patch.object(account_observation, 'MAX_OBSERVATION_REQUESTS', 1):
            self.reserve('after-purge')

    def test_034_R6_the_collector_runs_the_account_retention_sweep(self):
        from codex_agents import learning_store
        source = Path(learning_store.__file__).read_text()
        loop = source[source.index('next_sweep = time.monotonic() + WORKER_RESULT_SWEEP_SECONDS'):]
        self.assertIn('self.ledger.sweep_account_retention()', loop[:loop.index('listener.accept()')])

    def test_durable_quota_survives_abort_and_known_reply_still_works_at_capacity(self):
        with mock.patch.object(account_observation, 'MAX_OBSERVATION_REQUESTS', 1):
            reservation = self.reserve()
            reply = self.owner.complete(reservation, self.probe(reservation))
            self.assertEqual({**reply, 'known': True}, self.reserve())
            with self.assertRaisesRegex(ObservationError, 'observation_full'):
                self.reserve('over-quota')
            second = self.owner.reserve(self.fixture.projects[1], fixtures.RUNTIME, 'b' * 64, 'other-project',
                self.request, ObservationContext(str(self.fixture.roots[1])), lambda: True)
            self.owner.abort(second)
            with self.assertRaisesRegex(ObservationError, 'observation_full'):
                self.owner.reserve(self.fixture.projects[1], fixtures.RUNTIME, 'b' * 64, 'after-abort',
                    self.request, ObservationContext(str(self.fixture.roots[1])), lambda: True)
        self.assertEqual(2, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])
