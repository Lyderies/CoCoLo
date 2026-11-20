"""S1-H1d: a review's worker, verifier and QA sequence as one COCOLO command (PLAN H1d-1 to H1d-4).

Unit stage: the CLI calls are a fake, so the order, the saved files and the stops are checked
without running a child.
"""
import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from codex_agents import review_runner

ROUTE = {'agent_type': 'codex_agents_security_reviewer', 'effort': 'high', 'verification_required': True,
         'execution_configuration': {'model': 'gpt-x'}}


def result(verdict='ready'):
    body = {'assignment_id': 'r-a1', 'status': 'completed', 'summary': 'done', 'evidence': [],
            'remaining_risks': [], 'unverified_claims': []}
    if verdict is not None:
        body['semantic_verdict'] = verdict
    return body


class FakeCli:
    def __init__(self, verdicts=('ready', 'pass'), refuse=None):
        self.calls, self.verdicts, self.refuse = [], list(verdicts), refuse

    def __call__(self, name, *args):
        self.calls.append(name)
        if name == self.refuse:
            return {'ready': False, 'reason_code': 'claim_busy', 'next_step': 'wait'}
        return {
            'learning-open-orchestration': {'orchestration_handle': 'orc_1'},
            'learning-route': ROUTE,
            'learning-open-host-run': {'run_handle': 'hrn_' + 'a' * 48},
            'learning-record-host-result': {'host_outcome_digest': 'd' * 64},
            'learning-run-operational-checks': {'check_receipt': 'rcpt', 'checks_passed': True},
            'learning-record-operational-qa': {'accepted': True},
            'learning-close-orchestration': {'closed': True},
        }.get(name, {'ready': True, 'valid': True}) if name != 'dispatch' else {
            'result': result(self.verdicts.pop(0)), 'bridge_report': {'terminal_state': None}}


class ReviewRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project'
        (self.root / 'reviews' / 'x').mkdir(parents=True)
        self.work = Path(temporary.name) / 'work'
        self.spec = {'id': 'rev1', 'summary': 'a review', 'packet': 'reviews/x', 'context': ['reviews/x/README.md'],
                     'objective': 'review it', 'verify_objective': 'check it', 'expected_output': 'findings',
                     'evidence': [{'id': 'a', 'description': 'point a'}]}

    def run_step(self, step, cli):
        return review_runner.run(step, self.spec, self.work, self.root, cli=cli)

    def test_H1d_1_worker_verify_finish_run_the_sequence_and_save_each_step(self):
        cli = FakeCli()
        self.run_step('worker', cli)
        self.assertEqual(['learning-open-orchestration', 'learning-route', 'validate-assignment', 'result-preflight',
                          'provider-observe', 'learning-open-host-run', 'dispatch', 'learning-record-host-result'],
                         cli.calls)
        self.assertTrue((self.work / 'dispatch.json').is_file())
        self.run_step('verify', cli)
        self.assertEqual('verification',
                         json.loads((self.work / 'vtask.json').read_text())['family'])
        self.assertEqual('d' * 64, json.loads((self.work / 'vtask.json').read_text())['verification_for_outcome'])
        summary = self.run_step('finish', cli)
        self.assertEqual(['learning-run-operational-checks', 'learning-record-operational-qa',
                          'learning-close-orchestration'], cli.calls[-3:])
        self.assertTrue(summary['accepted'])

    def test_H1d_2_a_result_without_a_verdict_stops_the_review(self):
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('worker', FakeCli(verdicts=(None,)))
        self.assertEqual('review_no_verdict', stopped.exception.reason_code)
        self.assertFalse((self.work / 'worker.json').exists())  # the next stage cannot start

    def test_H1d_3_a_bad_spec_is_refused_before_any_step(self):
        cli = FakeCli()
        for change in ({'packet': '../outside'}, {'extra': 1}, {'evidence': []}):
            with self.subTest(change=change):
                with self.assertRaises(review_runner.ReviewError) as stopped:
                    review_runner.run('worker', {**self.spec, **change}, self.work, self.root, cli=cli)
                self.assertEqual('review_invalid_spec', stopped.exception.reason_code)
        self.assertEqual([], cli.calls)

    def test_H1d_4_a_refused_step_stops_with_its_reason(self):
        cli = FakeCli(refuse='provider-observe')
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('worker', cli)
        self.assertEqual('claim_busy', stopped.exception.reason_code)
        self.assertEqual('provider-observe', cli.calls[-1])


class ReviewCommandTests(ReviewRunnerTests):
    def test_H1d_1_4_the_learning_review_command_refuses_a_bad_spec_with_its_next_step(self):
        import contextlib
        import io
        import sys
        from unittest import mock
        from tests.test_bridge import cli
        spec = Path(self.work.parent) / 'spec.json'
        spec.write_text(json.dumps({**self.spec, 'packet': '../outside'}))
        out = io.StringIO()
        argv = ['cli.py', 'learning-review', 'worker', '--project-root', str(self.root), '--spec', str(spec),
                '--work-dir', str(self.work)]
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out):
            self.assertEqual(2, cli.main())
        reply = json.loads(out.getvalue())
        self.assertEqual(('review_invalid_spec', False), (reply['reason_code'], reply['ready']))
        self.assertIn('spec', reply['next_step'])


if __name__ == '__main__':
    unittest.main()
