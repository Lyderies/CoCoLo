"""O0c U4 CLI claim and probe after past-cap request history."""
import json
import time
import unittest
from unittest import mock
from test_framework import requirements
from tests import test_bridge as bridge_fixtures
from tests import test_result_lifetime as product_fixtures
from codex_agents import account_billing_admission as admission


class ProductProbeCapacityTests(product_fixtures.ResultLifetimeTests):
    def start_services(self, **options):
        self.admit('claude')
        super().start_services(**options)

    @requirements('O0C-U4-4', why_not_scenario='CLI and service smoke coverage isolates successful claim and probe after the small past-cap history.')
    def test_history_past_the_cap_allows_a_normal_claim_and_probe(self):
        self.enterContext(mock.patch.object(admission, 'MAX_PROBE_REQUESTS', 2))
        self.frames('claude', [bridge_fixtures.PROBE_INIT, bridge_fixtures.FAVOURABLE,
                               bridge_fixtures.result_frame('OK')])
        for number in range(3):
            reply = self.cli('provider-observe', '--provider', 'claude', '--billing-probe',
                             '--request-id', 'before-claim-' + str(number))
            self.assertEqual('launched' if number == 0 else 'reused', reply['probe'])
        self.assertEqual(3, self.collector_ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='committed'").fetchone()[0])
        context, route, orchestration, run = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(bridge_fixtures.null_filled(
            bridge_fixtures.result())))
        dispatched = self.cli('dispatch', *context, '--route', route, '--run-handle', run)
        self.record(context, route, run, dispatched)
        self.assertEqual(1, len(self.executions()))
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
        # Cross the real reuse/cooldown window without weakening either policy.
        after_window = time.time_ns() + (max(admission.PROBE_REUSE_MS, admission.PROBE_COOLDOWN_MS) + 1) * 1_000_000
        with mock.patch.object(time, 'time_ns', return_value=after_window):
            reply = self.cli('provider-observe', '--provider', 'claude', '--billing-probe',
                             '--request-id', 'after-claim')
        self.assertEqual('launched', reply['probe'])
        self.assertEqual(2, len([i for i in self.invocations('claude') if i['argv'][0] == '-p']))
        self.assertEqual(0, self.collector_ledger.account_capacity(self.project_id)['billing_probes']['live'])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    kind = ProductProbeCapacityTests
    for name in loader.getTestCaseNames(kind):
        if name in kind.__dict__:
            suite.addTest(kind(name))
    return suite
