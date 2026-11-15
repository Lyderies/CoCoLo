"""Original admitted-run observations with inert tools; no model execution."""
import unittest
from unittest import mock

from tests import test_account_launch_claims as fixtures
from codex_agents.account_observation import AccountObservationOwner, ObservationContext, run_observation_probe
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_contracts import content_digest, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.provider_observation_contracts import ObservationError, ObservationInput
from codex_agents.sql_read_budget import SQLReadLimits


class AccountObservationRunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountLaunchClaimsTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger, self.project = self.fixture.ledger, self.fixture.project
        self.stamp = self.fixture.stamp + 1
        clock = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.stamp)
        clock.start()
        self.addCleanup(clock.stop)
        self.handle = self.fixture.arguments['run_handle']
        self.owner = AccountObservationOwner(AccountOwnerViews(self.ledger), SQLReadLimits(10_000, 1000, 1_048_576))
        self.context = ObservationContext(str(self.fixture.fixture.tools))

    def reserve(self, identity='original', *, phase='before', handle=None, provider='codex', project=None):
        return self.owner.reserve(project or self.project, COLLECTOR_RUNTIME_DIGEST, 'b' * 64, identity,
            ObservationInput(provider, handle or self.handle, phase), self.context, lambda: True)

    def marker(self):
        row = self.ledger._connection.execute('SELECT identity_digest,observed_at_ms FROM launch_observations '
            'WHERE project_id=? AND handle_digest=?',
            (self.project, keyed_digest(self.ledger.state.digest_key(), self.handle))).fetchone()
        return None if row is None else tuple(row)

    def provider_count(self):
        return self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE source='provider'").fetchone()[0]

    def test_later_before_request_invalidates_older_completion_without_replacing_original_run(self):
        first = self.reserve()
        first_marker = self.marker()
        completion = run_observation_probe(first.capture)
        before = self.provider_count()
        self.stamp += 1
        latest = self.reserve('latest')
        latest_marker = self.marker()
        self.assertNotEqual(first_marker, latest_marker)
        with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
            self.owner.complete(first, completion)
        self.assertEqual(before, self.provider_count())
        self.assertEqual(latest_marker, self.marker())
        reply = self.owner.complete(latest, run_observation_probe(latest.capture))
        identity = next(record for record in reply['records'] if record['record_type'] == 'provider_identity')
        self.assertEqual((content_digest(identity), self.stamp), self.marker())
        for record in reply['records']:
            if record['record_type'] != 'provider_identity':
                self.assertEqual((reply['run_id'], 'before'), (record['run_id'], record['phase']))
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertEqual({**reply, 'known': True}, self.reserve('latest'))

    def test_known_old_reply_cannot_refresh_newer_launch_marker_and_after_never_changes_it(self):
        first = self.reserve()
        first_reply = self.owner.complete(first, run_observation_probe(first.capture))
        self.stamp += 1
        latest = self.reserve('latest')
        self.owner.complete(latest, run_observation_probe(latest.capture))
        marker = self.marker()
        self.assertEqual({**first_reply, 'known': True}, self.reserve())
        self.assertEqual(marker, self.marker())
        self.stamp += 1
        after = self.reserve('after', phase='after')
        reply = self.owner.complete(after, run_observation_probe(after.capture))
        self.assertEqual('after', reply['phase'])
        self.assertEqual(first_reply['run_id'], reply['run_id'])
        self.assertEqual(marker, self.marker())

    def test_wrong_provider_or_unknown_handle_refuses_before_any_journal_or_probe(self):
        before = self.provider_count()
        for changes in ({'provider': 'claude'}, {'handle': 'hrn_' + '0' * 48}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
                self.reserve(**changes)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_requests').fetchone()[0])
        self.assertEqual(before, self.provider_count())
        self.assertIsNone(self.marker())

    def test_run_closed_during_probe_cannot_publish_or_replace_pending_generation(self):
        reservation = self.reserve()
        completion = run_observation_probe(reservation.capture)
        before, marker = self.provider_count(), self.marker()
        # Inject the original owner's abandonment while the probe is away;
        # this fixture makes no native cancellation/cleanup claim.
        self.ledger._connection.execute("UPDATE host_run_bindings SET binding_state='abandoned',"
            "abandoned_reason='owner_cancelled' WHERE project_id=? AND handle_digest=?",
            (self.project, keyed_digest(self.ledger.state.digest_key(), self.handle)))
        with self.assertRaisesRegex(ObservationError, 'observation_not_authorized'):
            self.owner.complete(reservation, completion)
        self.assertEqual(before, self.provider_count())
        self.assertEqual(marker, self.marker())
        self.assertEqual('pending', self.ledger._connection.execute('SELECT state FROM account_observation_requests').fetchone()[0])

