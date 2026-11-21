"""S1-H1d: a review's worker, verifier and QA sequence as one COCOLO command (PLAN H1d-1 to H1d-4).

Unit stage: the CLI calls are a fake, so the order, the saved files and the stops are checked
without running a child.
"""
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.calls, self.arguments, self.verdicts, self.refuse = [], [], list(verdicts), refuse

    def __call__(self, name, *args):
        self.calls.append(name)
        self.arguments.append((name, args))
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


# The runner takes the host's state folder (for the key its stage bindings are made with); a version of the
# runner from before that has no such argument. The tests pass it only when it exists, so they can run
# against both and fail on the requirement rather than on the call.
TAKES_STATE = 'state_root' in inspect.signature(review_runner.run).parameters


class ReviewRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.guard_real_state()
        self.root = Path(temporary.name) / 'project'
        (self.root / 'reviews' / 'x').mkdir(parents=True)
        self.work = Path(temporary.name) / 'work'
        # The host's private state: its folder (0700) and the digest key in it (0600, 32 bytes).
        self.state = Path(temporary.name) / 'state'
        self.state.mkdir(mode=0o700)
        key = self.state / 'digest.key'
        key.write_bytes(bytes(range(32)))
        key.chmod(0o600)
        self.spec = {'id': 'rev1', 'summary': 'a review', 'packet': 'reviews/x', 'context': ['reviews/x/README.md'],
                     'objective': 'review it', 'verify_objective': 'check it', 'expected_output': 'findings',
                     'evidence': [{'id': 'a', 'description': 'point a'}]}

    def guard_real_state(self):
        """No test may read the developer's real host state (its digest key): a runner that is not given a
        state folder, or is given the real one, fails the test that did it."""
        from codex_agents.collector_paths import CollectorPaths, default_state_root
        real = CollectorPaths

        def guarded(root=None):
            if root is None or Path(root).resolve() == Path(default_state_root()).resolve():
                raise AssertionError('a test touched the real state root')
            return real(root)
        patch = mock.patch.object(review_runner, 'CollectorPaths', guarded)
        patch.start()
        self.addCleanup(patch.stop)

    def state_kw(self, state=None):
        return {'state_root': state or self.state} if TAKES_STATE else {}

    def run_step(self, step, cli, **options):
        return review_runner.run(step, self.spec, self.work, self.root, cli=cli, **self.state_kw(), **options)

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


class ReviewCorrectionTests(ReviewRunnerTests):
    def test_F1_a_symlinked_work_entry_is_refused_and_its_target_untouched(self):
        outside = Path(self.work.parent) / 'outside.json'
        outside.write_text('keep')
        self.work.mkdir(parents=True)
        (self.work / 'task.json').symlink_to(outside)
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('worker', FakeCli())
        self.assertEqual('review_unsafe_work_dir', stopped.exception.reason_code)
        self.assertEqual('keep', outside.read_text())

    def test_F2_a_failed_rerun_never_lets_the_next_stage_use_the_earlier_marker(self):
        self.run_step('worker', FakeCli())
        with self.assertRaises(review_runner.ReviewError):
            self.run_step('worker', FakeCli(verdicts=(None,)))  # rerun without a verdict
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('verify', FakeCli())
        self.assertEqual('review_stage_not_ready', stopped.exception.reason_code)

    def test_F3_F4_paths_are_absolute_and_the_close_status_is_passed_on(self):
        import os
        calls = []

        def cli(name, *args):
            calls.append((name, args))
            return FakeCli()(name, *args)
        relative_work = os.path.relpath(self.work)
        review_runner.run('worker', self.spec, relative_work, os.path.relpath(self.root), cli=cli, **self.state_kw())
        roots = [args[args.index('--project-root') + 1] for _, args in calls if '--project-root' in args]
        self.assertTrue(roots and all(Path(root).is_absolute() for root in roots))
        files = [arg for _, args in calls for arg in args if arg.endswith('.json')]
        self.assertTrue(files and all(Path(item).is_absolute() for item in files))
        review_runner.run('verify', self.spec, self.work, self.root, cli=cli, **self.state_kw())
        review_runner.run('finish', self.spec, self.work, self.root, cli=cli, status='failed', **self.state_kw())
        close = [args for name, args in calls if name == 'learning-close-orchestration'][0]
        self.assertEqual('failed', close[close.index('--status') + 1])

    def test_F5_evidence_values_are_checked_before_any_step(self):
        cli = FakeCli()
        for evidence in ([{'id': [], 'description': None}], [{'id': '', 'description': 'x'}]):
            with self.subTest(evidence=evidence), self.assertRaises(review_runner.ReviewError):
                review_runner.run('worker', {**self.spec, 'evidence': evidence}, self.work, self.root, cli=cli)
        self.assertEqual([], cli.calls)


class ReviewVerifierCorrectionTests(ReviewRunnerTests):
    def test_an_existing_folder_outside_the_project_is_refused_as_the_packet(self):
        (self.root.parent / 'outside').mkdir()  # it exists, so only the inside-the-project check refuses it
        cli = FakeCli()
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('worker', {**self.spec, 'packet': '../outside'}, self.work, self.root, cli=cli)
        self.assertEqual(('review_invalid_spec', []), (stopped.exception.reason_code, cli.calls))

    def test_an_empty_verdict_is_no_verdict_and_a_verifier_without_one_is_named(self):
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('worker', self.spec, self.work, self.root, cli=FakeCli(verdicts=('',)), **self.state_kw())
        self.assertEqual(('review_no_verdict', 'worker'), (stopped.exception.reason_code, stopped.exception.detail))
        review_runner.run('worker', self.spec, self.work, self.root, cli=FakeCli(), **self.state_kw())
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('verify', self.spec, self.work, self.root, cli=FakeCli(verdicts=(None,)), **self.state_kw())
        self.assertEqual(('review_no_verdict', 'verifier'), (stopped.exception.reason_code, stopped.exception.detail))

    def test_a_malformed_child_reply_stops_the_review_instead_of_crashing(self):
        def cli(name, *args):
            return {'ready': True} if name == 'learning-route' else FakeCli()(name, *args)
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('worker', self.spec, self.work, self.root, cli=cli, **self.state_kw())
        self.assertEqual('review_step_unreadable', stopped.exception.reason_code)


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

    def test_the_learning_review_command_forwards_its_state_root_to_every_host_command(self):
        """Requirement: the state folder the key is read from is the one every host command is told to use
        (an installed CLI pins it to its default and accepts only that). Run through the command itself."""
        import contextlib
        import io
        import sys
        from tests.test_bridge import cli as command
        spec = Path(self.work.parent) / 'spec.json'
        spec.write_text(json.dumps(self.spec))
        fake = FakeCli()
        out = io.StringIO()
        argv = ['cli.py', 'learning-review', 'worker', '--project-root', str(self.root), '--spec', str(spec),
                '--work-dir', str(self.work), '--state-root', str(self.state)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(review_runner, 'project_cli', return_value=fake), \
                contextlib.redirect_stdout(out):
            self.assertEqual(0, command.main())
        self.assertTrue(json.loads(out.getvalue())['ready'])
        self.assertTrue(fake.arguments)
        for name, args in fake.arguments:
            self.assertEqual(str(self.state.resolve()), args[args.index('--state-root') + 1], name)


if __name__ == '__main__':
    unittest.main()
