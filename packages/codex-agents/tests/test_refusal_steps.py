"""S1-H1b: every refusal names its next step (PLAN H1b-1 to H1b-3)."""
import re
import unittest

import tests.support  # noqa: F401
from codex_agents import refusal_steps
from codex_agents.account_open_contracts import OPEN_REASON_CODES
from codex_agents.client_runtime import START_REASONS
from codex_agents.dispatch_api import DISPATCH_REASONS
from codex_agents.history_contracts import HISTORY_REASON_CODES
from codex_agents.service_transport import SERVICE_REFUSALS

CODES = sorted(set(OPEN_REASON_CODES) | set(DISPATCH_REASONS) | set(START_REASONS) | set(SERVICE_REFUSALS)
               | set(HISTORY_REASON_CODES))


class RefusalStepTests(unittest.TestCase):
    def test_H1b_2_every_listed_code_has_its_own_or_its_familys_step(self):
        for code in CODES:
            with self.subTest(code=code):
                step = refusal_steps.next_step(code)
                self.assertNotEqual(refusal_steps.DEFAULT, step)
                self.assertRegex(step, r"^[A-Z][^/\\\\]{9,239}[.)]$")  # plain text, no path

    def test_H1b_1_specific_codes_name_their_specific_step(self):
        for code, words in (('result_capacity_full', 'learning-close-orchestration'),
                            ('open_service_other_version', 'learning-start-project-service'),
                            ('billing_attestation_missing', 'attest.py')):
            with self.subTest(code=code):
                self.assertIn(words, refusal_steps.next_step(code))

    def test_an_unknown_code_gets_the_default(self):
        self.assertEqual(refusal_steps.DEFAULT, refusal_steps.next_step('something_new'))
        self.assertEqual(refusal_steps.DEFAULT, refusal_steps.next_step(None))
        self.assertTrue(re.search('learning-status', refusal_steps.DEFAULT))


if __name__ == '__main__':
    unittest.main()
