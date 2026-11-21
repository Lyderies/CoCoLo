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
from codex_agents.learning_store import COLLECTOR_PROTOCOL_VERSION, CollectorServer
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


if __name__ == '__main__':
    unittest.main()
