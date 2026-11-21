"""Recurring fix (stale collector, 21 November 2026): the review runner's verdict and stage-file rules.

(d) a result with no verdict is refused before it is recorded; (e) learning-review finish (and verify)
refuses a stage file the runner did not write: wrong mode, wrong format, or a digest that is not the one its
own recorded reply holds.
"""
import json
import os
import unittest

from codex_agents import review_runner
from tests import test_review_runner as base


class ReviewRunnerProvenanceTests(unittest.TestCase):
    setUp = base.ReviewRunnerTests.setUp
    run_step = base.ReviewRunnerTests.run_step

    def staged(self):
        cli = base.FakeCli()
        self.run_step('worker', cli)
        self.run_step('verify', cli)
        return cli

    def refused(self, step='finish'):
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step(step, base.FakeCli())
        return stopped.exception.reason_code

    def test_d_a_result_without_a_verdict_is_not_recorded(self):
        cli = base.FakeCli(verdicts=(None,))
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('worker', cli)
        self.assertEqual('review_no_verdict', stopped.exception.reason_code)
        self.assertNotIn('learning-record-host-result', cli.calls)
        self.assertFalse((self.work / 'record.json').exists())

    def test_e_a_stage_file_with_a_wider_mode_is_refused(self):
        self.staged()
        os.chmod(self.work / 'worker.json', 0o644)
        self.assertEqual('review_stage_not_runner_written', self.refused())

    def test_e_a_stage_file_with_the_wrong_format_is_refused(self):
        self.staged()
        path = self.work / 'verifier.json'
        data = json.loads(path.read_text())
        for change in ({'extra': 1}, {'digest': 'not-a-digest'}, {'verdict': ''}, {'provider': 'other'}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**data, **change}))
                os.chmod(path, 0o600)
                self.assertEqual('review_stage_not_runner_written', self.refused())

    def test_e_a_digest_the_recorded_reply_does_not_hold_is_refused(self):
        self.staged()
        path = self.work / 'worker.json'
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, 'digest': 'e' * 64}))
        os.chmod(path, 0o600)
        self.assertEqual('review_stage_not_runner_written', self.refused())
        # The verify stage reads the worker's file too.
        self.assertEqual('review_stage_not_runner_written', self.refused('verify'))

    def rewrite(self, name, **changes):
        path = self.work / name
        path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))
        os.chmod(path, 0o600)

    def test_e_matching_forged_stage_and_record_files_are_refused(self):
        # Each file's mode, shape and the two digests agree, so only the binding to what the stage was made
        # from can tell this was not written by the runner.
        self.staged()
        for stage, record in (('worker.json', 'record.json'), ('verifier.json', 'vrecord.json')):
            self.rewrite(stage, digest='e' * 64)
            self.rewrite(record, host_outcome_digest='e' * 64)
        self.assertEqual('review_stage_not_runner_written', self.refused())
        self.assertEqual('review_stage_not_runner_written', self.refused('verify'))

    def test_e_another_allowed_provider_in_a_stage_file_is_refused(self):
        self.staged()
        self.rewrite('worker.json', provider='claude')  # the worker's route is a Codex one
        self.assertEqual('review_stage_not_runner_written', self.refused('verify'))
        self.assertEqual('review_stage_not_runner_written', self.refused())

    def test_e_a_verdict_or_summary_the_result_does_not_state_is_refused(self):
        self.staged()
        for change in ({'verdict': 'blocked'}, {'summary': 'all good'}):
            with self.subTest(change=change):
                path = self.work / 'verifier.json'
                original = path.read_text()
                self.rewrite('verifier.json', **change)
                self.assertEqual('review_stage_not_runner_written', self.refused())
                path.write_text(original)
                os.chmod(path, 0o600)

    def test_e_a_replaced_route_assignment_or_result_the_stage_was_made_from_is_refused(self):
        # finish hands the assignment and result paths to QA, so those are checked against the markers.
        for name, change in (('route.json', {'agent_type': 'claude_agents_security_reviewer'}),
                             ('assignment.json', {'role': 'codex_agents_researcher'}),
                             ('result.json', {'summary': 'changed after the review'}),
                             ('vassignment.json', {'role': 'codex_agents_researcher'}),
                             ('vresult.json', {'remaining_risks': ['none']})):
            with self.subTest(name=name):
                self.staged()
                self.rewrite(name, **change)
                self.assertEqual('review_stage_not_runner_written', self.refused())

    def test_e_running_a_stage_again_leaves_none_of_the_earlier_run_behind(self):
        self.staged()
        self.assertTrue((self.work / 'record.json').exists() and (self.work / 'vrecord.json').exists())
        with self.assertRaises(review_runner.ReviewError):
            self.run_step('worker', base.FakeCli(verdicts=(None,)))
        for name in ('worker.json', 'record.json', 'verifier.json', 'vrecord.json', 'vresult.json'):
            self.assertFalse((self.work / name).exists(), name)
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('finish', base.FakeCli())
        self.assertEqual('review_stage_not_ready', stopped.exception.reason_code)

    def test_e_the_files_the_runner_wrote_are_still_accepted(self):
        cli = self.staged()
        self.run_step('finish', cli)
        self.assertEqual('learning-close-orchestration', cli.calls[-1])
