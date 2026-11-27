"""RT-RX on the real wire: the CLI's advisory snapshot comes from the running account services.

The 0.8.53 install routed every task `not_checked` although its account services
were running: the project host had published its runtime hint before the
collector's account services listened, so the hint named no account endpoint and
the CLI's snapshot read gave up without saying why. Here the services start in
the order production starts them (the project host waits for the collector's
health only), and nothing on the route, snapshot or account path is patched for the
successful read (the refusal case patches only the snapshot reply).
"""
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401 -- the source CLI import path
from tests import service_harness
from tests.test_bridge import ClaudePrimaryCase
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.learning_contracts import content_digest
from codex_agents.providers import headroom_record
from codex_agents.service_enrollment import capability_path
from test_framework import requirements


class LiveSnapshotRouteTests(ClaudePrimaryCase):
    __why_not_scenario__ = ('Real CLI, real account and project services in-process (service harness) in '
                            'production start order; provider clients are the harness fakes, no model is launched.')
    def start_services(self, *, project_host=True):
        real = service_harness.AccountServiceHost
        published = threading.Event()
        gate_timed_out = []

        def account_after_hint(*arguments, **options):
            # Production: project_host.py starts the collector and waits for its health only;
            # the account services come up after the hint (the real install: 2 s apart).
            if not published.wait(20):
                gate_timed_out.append(True)  # the order under test was not reproduced
            return real(*arguments, **options)

        with mock.patch.object(service_harness, 'AccountServiceHost', account_after_hint), \
                mock.patch.object(service_harness, 'capability_path', lambda paths, project, role: paths.socket):
            super().start_services(project_host=False)
            if project_host:
                self.start_project_host(self.project)
            self.assertTrue(runtime_hint_path(self.paths, self.project).exists())
            published.set()
            self.wait_for(lambda: capability_path(self.paths, None, 'account').exists())
            self.assertEqual([], gate_timed_out, 'account services started before the hint was published')

    @requirements('RT-RX-7', 'RT-RX-18', 'RT-RX-19')
    def test_learning_route_reads_the_running_account_services_snapshot(self):
        fingerprint = self.admit('codex')
        stamp = self.ledger._clock_ms()
        reading = headroom_record(provider='codex', account_fingerprint=fingerprint,
            windows=[{'scope': None, 'window_minutes': 300, 'used_percent': 95, 'resets_at': None}],
            status=None, run_id=None, phase=None, observed_at_ms=stamp)
        self.ledger._store_provider_observations(self.project_id, [reading], timestamp=stamp)
        route = self.cli('learning-route', '--task', self.write('task.json', self.task()),
                         '--host-client', 'claude-host-v1')
        availability = route['availability']
        self.assertEqual('checked', availability['status'], availability)
        self.assertNotIn('reason_code', availability)
        codex = availability['providers']['codex']
        self.assertEqual((True, 'exhausted', fingerprint), (codex['known'], codex['state'], codex['account_fingerprint']))
        self.assertIn({'project_id': self.project_id, 'digest': content_digest(reading)}, codex['evidence_refs'])
        self.assertEqual('claude', route['provider'])
        self.assertIn({'phase': 'verifier', 'provider': 'codex', 'skip': 'usage'}, availability['candidates'])
        # A refused snapshot still routes (advisory, 124f), and says why it is not checked.
        from codex_agents.account_api import AccountClient, AccountError
        with mock.patch.object(AccountClient, 'provider_state_snapshot',
                               side_effect=AccountError('account_not_authorized')):
            blind = self.cli('learning-route', '--task', self.write('task.json', self.task()),
                             '--host-client', 'claude-host-v1')
        self.assertEqual(('not_checked', 'snapshot_unavailable:account_not_authorized'),
                         (blind['availability']['status'], blind['availability']['reason_code']))
        self.assertEqual('claude', blind['provider'])


if __name__ == '__main__':
    unittest.main()
