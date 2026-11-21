"""Recurring fix (stale collector, 21 November 2026): a review result without a verdict is refused.

A null or missing semantic_verdict on a result for a review (security reviewer or verification worker) was
filled in from the legacy status projection as "pass". It is now refused, never filled in. Results for
other roles keep the legacy projection.
"""
import json
import unittest

import tests.support  # noqa: F401
from codex_agents.bridge import remove_optional_nulls
from codex_agents.contracts import ContractError, ResultContractError, validate_result
from tests import test_bridge
from tests import test_contracts


def body(**changes):
    return {'assignment_id': 'assignment-1', 'status': 'completed', 'summary': 'Reviewed', 'artifacts': [],
            'evidence': [{'criterion_id': 'line-marker', 'type': 'review', 'reference': 'review:x',
                          'outcome': 'pass', 'details': 'ok'}],
            'unverified_claims': [], 'remaining_risks': [], **changes}


class ReviewVerdictRequiredTests(unittest.TestCase):
    def assignment(self, role):
        return {**test_contracts.ContractTests('test_assignment_accepts_bounded_input').assignment(), 'role': role}

    def test_a_missing_verdict_on_a_review_result_is_refused_not_passed(self):
        for role in ('codex_agents_security_reviewer', 'claude_agents_security_reviewer',
                     'codex_agents_verification_worker', 'claude_agents_verification_worker'):
            with self.subTest(role=role), self.assertRaises(ResultContractError) as refused:
                validate_result(body(), assignment=self.assignment(role))
            self.assertEqual('missing_required_fields', refused.exception.reason_code)
            self.assertIn('semantic_verdict', str(refused.exception))

    def test_a_null_verdict_after_the_strict_schema_is_refused_not_passed(self):
        original = json.loads(test_bridge.RESULT_SCHEMA)
        normalized = remove_optional_nulls(test_bridge.null_filled(body()), original)
        # Strengthened (never loosened): the earlier assertions accepted any ContractError, which the old
        # code also raised for a null verdict (invalid_status); the requirement is that it is refused as missing.
        for refused_body in (normalized, {**body(), 'semantic_verdict': None}):
            with self.assertRaises(ResultContractError) as refused:
                validate_result(refused_body, assignment=self.assignment('codex_agents_security_reviewer'))
            self.assertEqual('missing_required_fields', refused.exception.reason_code)

    def test_a_blocked_review_without_a_verdict_is_refused_not_read_as_blocked(self):
        # status "blocked" is projected to a completed run with the verdict "blocked"; the review still has to
        # state it. Likewise an explicit completed execution_state with the verdict left out.
        for role in ('codex_agents_security_reviewer', 'claude_agents_verification_worker'):
            for changes in ({'status': 'blocked'}, {'execution_state': 'completed'},
                            {'status': 'blocked', 'execution_state': 'completed', 'semantic_verdict': None}):
                with self.subTest(role=role, changes=changes), self.assertRaises(ResultContractError) as refused:
                    validate_result(body(**changes), assignment=self.assignment(role))
                self.assertEqual('missing_required_fields', refused.exception.reason_code)

    def test_a_blocked_review_that_states_its_verdict_is_accepted(self):
        checked = validate_result(body(status='blocked', semantic_verdict='blocked'),
                                  assignment=self.assignment('codex_agents_security_reviewer'))
        self.assertEqual('blocked', checked['semantic_verdict'])
        validate_result(body(status='blocked'), assignment=self.assignment('codex_agents_researcher'))

    def test_an_explicit_verdict_is_accepted_and_other_roles_keep_the_legacy_projection(self):
        checked = validate_result(body(semantic_verdict='ready'),
                                  assignment=self.assignment('codex_agents_security_reviewer'))
        self.assertEqual('ready', checked['semantic_verdict'])
        validate_result(body(), assignment=self.assignment('codex_agents_researcher'))
        # A failed review run states no verdict by design (not-applicable), never a pass.
        validate_result(body(status='failed', evidence=[]),
                        assignment=self.assignment('codex_agents_security_reviewer'))


if __name__ == '__main__':
    unittest.main()
