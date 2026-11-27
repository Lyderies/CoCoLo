"""Recurring fix (stale collector, 21 November 2026): a collector says when its loaded code is out of date.

Python never reloads a collector's modules, so a collector started before a merge keeps running the old
code. The collector's health now carries `runtime_stale` (its runtime files differ from the digest it loaded),
learning-status shows the loaded digest and `collector_code_stale`, and the probe live check refuses with
`collector_code_stale` rather than run on old code.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests import test_learning_store as store_tests

from codex_agents import client_runtime, collector_client
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, collector_code_stale
from codex_agents import learning_store
from codex_agents.learning_store import (COLLECTOR_PROTOCOL_VERSION, CollectorServer, LearningLedger, LearningState,
                                        authorize_collector_runtime)
from codex_agents.runtime_files import RUNTIME_FILES, runtime_digest


class CollectorCodeStaleTests(unittest.TestCase):
    def copy_runtime(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for relative in RUNTIME_FILES:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ORCHESTRATION_ROOT / relative, root / relative)
        loaded = runtime_digest({relative: (root / relative).read_bytes() for relative in RUNTIME_FILES})
        return root, loaded

    def test_a_collector_is_stale_once_a_runtime_file_changes_after_it_loaded(self):
        root, loaded = self.copy_runtime()
        self.assertFalse(collector_code_stale(root=root, loaded=loaded))
        with (root / 'codex_agents' / 'account_billing_jobs.py').open('a') as handle:
            handle.write('\n# merged after the collector started\n')
        self.assertTrue(collector_code_stale(root=root, loaded=loaded))

    def test_a_missing_or_unreadable_runtime_file_is_stale_never_an_error(self):
        root, loaded = self.copy_runtime()
        (root / 'codex_agents' / 'bridge.py').unlink()
        self.assertTrue(collector_code_stale(root=root, loaded=loaded))

    def test_this_process_is_not_stale_when_nothing_changed(self):
        self.assertFalse(collector_code_stale())


class CollectorStaleReportTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)

    def health(self):
        server = CollectorServer(self.store.ledger)
        server.start()
        self.addCleanup(server.stop)
        return server._dispatch({'protocol': COLLECTOR_PROTOCOL_VERSION, 'op': 'health',
                                 'project_root': str(self.store.project_a)})

    def status(self):
        report = {}
        self.assertTrue(client_runtime._apply_health(report, self.health()))
        return report

    def test_the_collector_health_and_learning_status_show_the_loaded_digest_and_not_stale(self):
        health = self.health()
        self.assertIs(False, health['runtime_stale'])
        report = {}
        client_runtime._apply_health(report, health)
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, report['collector_runtime_digest'])
        self.assertIs(False, report['collector_code_stale'])
        self.assertIsNone(report['collector_code_note'])

    def test_learning_status_says_plainly_when_the_collector_runs_old_code(self):
        with mock.patch.object(collector_client, '_collector_runtime_digest', return_value='e' * 64):
            report = self.status()
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, report['collector_runtime_digest'])  # what it loaded
        self.assertIs(True, report['collector_code_stale'])
        self.assertIn('collector_code_stale', report['collector_code_note'])
        self.assertIn('restart', report['collector_code_note'])

    def test_the_probe_live_check_refuses_on_a_stale_collector_before_any_account_request(self):
        with mock.patch.object(client_runtime, 'CollectorPaths'), \
                mock.patch.object(client_runtime, 'CollectorClient') as client, \
                mock.patch.object(client_runtime, '_account_target') as target:
            client.return_value.health.return_value = {'runtime_stale': True}
            with self.assertRaises(client_runtime.RuntimeBillingProbeError) as refused:
                client_runtime.billing_probe_client('state', 'project')
        self.assertEqual('collector_code_stale', refused.exception.reason_code)
        target.assert_not_called()

    def test_the_probe_live_check_goes_on_when_the_collector_is_current_or_cannot_say(self):
        for answer in ({'runtime_stale': False}, {}, None):
            with self.subTest(answer=answer), \
                    mock.patch.object(client_runtime, 'CollectorPaths'), \
                    mock.patch.object(client_runtime, 'CollectorClient') as client, \
                    mock.patch.object(client_runtime, '_account_target', side_effect=client_runtime.DispatchError('x')) as target:
                if answer is None:
                    client.return_value.health.side_effect = OSError()
                else:
                    client.return_value.health.return_value = answer
                with self.assertRaises(client_runtime.RuntimeBillingProbeError) as refused:
                    client_runtime.billing_probe_client('state', 'project')
                self.assertEqual('account_unavailable', refused.exception.reason_code)
                target.assert_called_once()


class FreshClientMeetsAnOldCollectorTests(unittest.TestCase):
    """A client started after the files changed hashes the new files, so a collector that loaded the old ones
    reports another digest (or an older version). CollectorClient.health() refuses it before it can say
    `runtime_stale`; the status and the probe must still name and refuse that collector. These run the real
    health() against a real collector on a real socket, with the collector's loaded identity set old."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.state = LearningState(base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        self.project = base / 'project'
        (self.project / '.git').mkdir(parents=True)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.ledger.register_project(self.project)

    def collector(self, *, own_files_changed=None, **loaded):
        """A running collector whose loaded digest or version (not this client's) are the ones given. A
        collector that started before its files changed also finds its own files changed (`runtime_stale`)."""
        for name, value in loaded.items():
            patch = mock.patch.object(learning_store, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        if own_files_changed is not None:
            patch = mock.patch.object(learning_store, 'collector_code_stale', return_value=own_files_changed)
            patch.start()
            self.addCleanup(patch.stop)
        server = CollectorServer(self.ledger)
        server.start()
        self.addCleanup(server.stop)

    def health_reply(self, **changes):
        """The collector's health reply as it reaches the client, with these fields changed (None removes one)."""
        real = collector_client.CollectorClient._request

        def request(client, message):
            reply = real(client, message)
            if message.get('op') == 'health':
                reply = {key: value for key, value in {**reply, **changes}.items()
                         if not (key in changes and changes[key] is None and key != 'runtime_digest')}
                if 'runtime_digest' in changes and changes['runtime_digest'] is None:
                    reply['runtime_digest'] = None
            return reply
        patch = mock.patch.object(collector_client.CollectorClient, '_request', request)
        patch.start()
        self.addCleanup(patch.stop)

    def probe(self):
        with mock.patch.object(client_runtime, '_account_target', side_effect=client_runtime.DispatchError('x')) as target:
            try:
                client_runtime.billing_probe_client(self.state.root, self.project)
            except client_runtime.RuntimeBillingProbeError as refused:
                return refused.reason_code, target
        self.fail('the probe was not refused')

    def status(self):
        owner = mock.Mock()
        owner.owner_key.return_value = {'state': 'present', 'fingerprint': 'f', 'replacements': 0,
                                        'last_replaced_at_ms': None}
        with mock.patch.object(client_runtime, '_account_target', return_value=('prj_' + 'a' * 32, 'e')), \
                mock.patch.object(client_runtime, 'enroll_clients'), \
                mock.patch.object(client_runtime, 'AccountClient', return_value=owner):
            return client_runtime.status_client(self.state.root, self.project)

    def test_the_probe_refuses_a_collector_that_loaded_other_code_before_any_account_request(self):
        """GUARD (also passes on 8ec26a08): a collector that loaded other code and finds its files changed is refused by the probe before any account request."""
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64, own_files_changed=True)
        reason, target = self.probe()
        self.assertEqual('collector_code_stale', reason)
        target.assert_not_called()

    def test_the_probe_refuses_a_collector_of_an_older_package_version(self):
        """GUARD (also passes on 8ec26a08): an older package version is refused by the probe as stale."""
        self.collector(PACKAGE_VERSION='0.0.1')
        reason, target = self.probe()
        self.assertEqual('collector_code_stale', reason)
        target.assert_not_called()

    def test_the_probe_does_not_call_a_newer_collector_stale(self):
        """GUARD (also passes on 8ec26a08): a newer collector is never called stale."""
        self.collector(PACKAGE_VERSION='99.0.0')
        reason, target = self.probe()
        self.assertEqual('account_unavailable', reason)  # went on to the account request
        target.assert_called_once()

    def test_learning_status_shows_the_stale_note_and_the_digest_the_collector_loaded(self):
        """GUARD (also passes on 8ec26a08): status names a stale collector and the digest it loaded."""
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64, own_files_changed=True)
        report = self.status()
        self.assertIs(True, report['collector_code_stale'])
        self.assertIn('restart', report['collector_code_note'])
        self.assertEqual('e' * 64, report['collector_runtime_digest'])
        self.assertFalse(report['ready'])

    def test_learning_status_shows_the_stale_note_for_an_older_package_version(self):
        """GUARD (also passes on 8ec26a08): status names an older package version as stale."""
        self.collector(PACKAGE_VERSION='0.0.1')
        report = self.status()
        self.assertIs(True, report['collector_code_stale'])
        self.assertIn('collector_code_stale', report['collector_code_note'])

    def test_learning_status_of_a_current_collector_is_not_stale(self):
        """GUARD (also passes on 8ec26a08): a current collector is not stale."""
        self.collector()
        report = self.status()
        self.assertIs(False, report['collector_code_stale'])
        self.assertIsNone(report['collector_code_note'])

    def test_a_collector_that_does_not_report_its_own_files_and_differs_is_stale(self):
        """GUARD (also passes on 8ec26a08, whose blanket check calls it stale): an older collector that never reports runtime_stale is still stale."""
        # Built before `runtime_stale` existed: nothing says its files are unchanged, and restarting loads this
        # install's files.
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64)
        self.health_reply(runtime_stale=None)
        self.assertEqual('collector_code_stale', self.probe()[0])
        self.assertIs(True, self.status()['collector_code_stale'])

    def test_another_build_of_the_same_version_is_a_conflict_that_a_restart_does_not_fix(self):
        """GUARD on ddefa327. Requirement (r2 4b): another build of the same version, with the collector's own files unchanged, is a conflict and never says 'restart it'. Red on 8ec26a08, which calls it stale."""
        # A different digest, and the collector says its own files are unchanged: it would load the same code.
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64, own_files_changed=False)
        report = self.status()
        self.assertIs(False, report['collector_code_stale'])
        self.assertIn('collector_build_conflict', report['collector_code_note'])
        self.assertNotIn('restart it', report['collector_code_note'])
        self.assertIn('will not fix', report['collector_code_note'])
        self.assertEqual('e' * 64, report['collector_runtime_digest'])
        reason, target = self.probe()
        self.assertEqual('account_unavailable', reason)  # not called stale; the account request reports it
        target.assert_called_once()

    def test_a_malformed_identity_is_its_own_cause_never_stale(self):
        """GUARD on ddefa327. Requirement (r2 4a): a malformed digest, schema or version is `collector_identity_invalid`, never stale and never refused as stale. Red on 8ec26a08, which calls it stale."""
        for changes in ({'runtime_digest': None}, {'runtime_digest': 'not-a-digest'}, {'schema_version': 'one'},
                        {'runtime_version': 'not-a-version'}, {'runtime_version': None}):
            with self.subTest(changes=changes):
                self.collector()
                self.health_reply(**changes)
                report = self.status()
                self.assertIsNone(report['collector_code_stale'])
                self.assertIn('collector_identity_invalid', report['collector_code_note'])
                self.assertNotIn('restart it', report['collector_code_note'])
                reason, target = self.probe()
                self.assertEqual('account_unavailable', reason)
                target.assert_called_once()
                self.doCleanups()
                self.setUp()


if __name__ == '__main__':
    unittest.main()
