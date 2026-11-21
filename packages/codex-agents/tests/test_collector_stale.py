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

    def collector(self, **loaded):
        """A running collector whose loaded digest or version (not this client's) are the ones given."""
        for name, value in loaded.items():
            patch = mock.patch.object(learning_store, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        server = CollectorServer(self.ledger)
        server.start()
        self.addCleanup(server.stop)

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
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64)
        reason, target = self.probe()
        self.assertEqual('collector_code_stale', reason)
        target.assert_not_called()

    def test_the_probe_refuses_a_collector_of_an_older_package_version(self):
        self.collector(PACKAGE_VERSION='0.0.1')
        reason, target = self.probe()
        self.assertEqual('collector_code_stale', reason)
        target.assert_not_called()

    def test_the_probe_does_not_call_a_newer_collector_stale(self):
        self.collector(PACKAGE_VERSION='99.0.0')
        reason, target = self.probe()
        self.assertEqual('account_unavailable', reason)  # went on to the account request
        target.assert_called_once()

    def test_learning_status_shows_the_stale_note_and_the_digest_the_collector_loaded(self):
        self.collector(COLLECTOR_RUNTIME_DIGEST='e' * 64)
        report = self.status()
        self.assertIs(True, report['collector_code_stale'])
        self.assertIn('restart', report['collector_code_note'])
        self.assertEqual('e' * 64, report['collector_runtime_digest'])
        self.assertFalse(report['ready'])

    def test_learning_status_shows_the_stale_note_for_an_older_package_version(self):
        self.collector(PACKAGE_VERSION='0.0.1')
        report = self.status()
        self.assertIs(True, report['collector_code_stale'])
        self.assertIn('collector_code_stale', report['collector_code_note'])

    def test_learning_status_of_a_current_collector_is_not_stale(self):
        self.collector()
        report = self.status()
        self.assertIs(False, report['collector_code_stale'])
        self.assertIsNone(report['collector_code_note'])


if __name__ == '__main__':
    unittest.main()
