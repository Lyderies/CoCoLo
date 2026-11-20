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

    def test_H1b_1_3_a_printed_refusal_carries_its_next_step_and_keeps_its_fields(self):
        import contextlib
        import io
        import json
        from tests.test_bridge import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print({'ready': False, 'error': 'dispatch unavailable', 'reason_code': 'claim_busy'})
        printed = json.loads(out.getvalue())
        self.assertEqual(('dispatch unavailable', 'claim_busy', refusal_steps.next_step('claim_busy')),
                         (printed['error'], printed['reason_code'], printed['next_step']))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print({'ready': True, 'reason_code': None})  # not a refusal: unchanged
        self.assertNotIn('next_step', json.loads(out.getvalue()))

    def test_result_validation_codes_name_the_contract_step(self):
        from codex_agents.contracts import RESULT_VALIDATION_REASON_CODES
        for code in RESULT_VALIDATION_REASON_CODES:
            with self.subTest(code=code):
                self.assertEqual(refusal_steps.RESULT_STEP, refusal_steps.next_step(code))

    def test_an_unknown_code_gets_the_default(self):
        self.assertEqual(refusal_steps.DEFAULT, refusal_steps.next_step('something_new'))
        self.assertEqual(refusal_steps.DEFAULT, refusal_steps.next_step(None))
        self.assertTrue(re.search('learning-status', refusal_steps.DEFAULT))


if __name__ == '__main__':
    unittest.main()
