"""S1-H1d: a review's worker, verifier and QA sequence as one COCOLO command (PLAN H1d-1 to H1d-4).

Unit stage: the CLI calls are a fake, so the order, the saved files and the stops are checked
without running a child.
"""
import inspect
import json
import os
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
        if not hasattr(review_runner, 'CollectorPaths'):
            return  # a runner from before the keyed binding never reads a state folder
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

    def command_steps(self, fake):
        """Run worker, verify and finish through the learning-review command itself (its parser, then the
        runner), with the host commands replaced by `fake`; returns nothing, the calls are on `fake`."""
        import contextlib
        import io
        import sys
        from tests.test_bridge import cli as command
        spec = Path(self.work.parent) / 'spec.json'
        spec.write_text(json.dumps(self.spec))
        for stage in ('worker', 'verify', 'finish'):
            argv = ['cli.py', 'learning-review', stage, '--project-root', str(self.root), '--spec', str(spec),
                    '--work-dir', str(self.work), '--state-root', str(self.state)]
            out = io.StringIO()
            with mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(review_runner, 'project_cli', return_value=fake), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(0, command.main(), stage)
            self.assertTrue(json.loads(out.getvalue())['ready'], stage)

    def test_the_learning_review_command_forwards_its_state_root_to_every_host_command(self):
        """Requirement: the state folder the key is read from is the one every host command is told to use,
        through the command itself for all three stages. Regression: ddefa327 passes it to the runner but not
        on to the host commands."""
        fake = FakeCli()
        self.command_steps(fake)
        self.assertTrue(fake.arguments)
        for name, args in fake.arguments:
            self.assertIn('--state-root', args, name)
            self.assertEqual(str(self.state.resolve()), args[args.index('--state-root') + 1], name)

    def test_every_host_command_the_runner_calls_accepts_the_arguments_it_is_given_by_the_real_parser(self):
        """Requirement: every host command the runner calls accepts the arguments the runner builds (so
        `--state-root` cannot stop every real review). The argument list of each one is parsed by the real
        command-line parser; none exits. GUARD on 0bfe28b9 (the real parsers all accept it); on ddefa327 it fails
        by assertion because no --state-root was forwarded yet."""
        import contextlib
        import io
        from tests.test_bridge import cli as command
        fake = FakeCli()
        self.command_steps(fake)
        parser = command.build_parser()
        names = set()
        for name, args in fake.arguments:
            names.add(name)
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parsed = parser.parse_args([name, *args])
                except SystemExit:
                    self.fail(f'the real parser refused {name} {args}')
            self.assertIsNotNone(parsed.state_root, name)  # forwarded, and read by the parser as a path
            self.assertEqual(self.state.resolve(), parsed.state_root.resolve(), name)
        self.assertGreaterEqual(len(names), 10)  # every command of the three stages, not a sample

    def test_an_installed_cli_accepts_only_its_default_state_for_learning_review(self):
        """Requirement: an installed CLI pins `learning-review` to its default learning state, so the runner,
        which forwards the folder it resolved, works installed with the default and cannot be pointed at
        another folder's key. GUARD: unchanged behaviour of the CLI on ddefa327."""
        from codex_agents.host_commands import PRIMARY_HOST_COMMANDS
        from tests.test_bridge import cli as command
        self.assertIn('learning-review', PRIMARY_HOST_COMMANDS)
        manifest = self.work.parent / 'install-manifest.json'
        manifest.write_text('{}')
        other = self.work.parent / 'another-state'
        other.mkdir(mode=0o700)
        with mock.patch.object(command, 'INSTALL_MANIFEST', manifest), \
                mock.patch.object(command, 'default_state_root', return_value=self.state):
            default = argparse_namespace('learning-review', self.state)
            command._require_own_roots(default)
            self.assertIsNone(default.state_root)  # the default is accepted and then the CLI's own
            with self.assertRaises(command.OwnRootRefused):
                command._require_own_roots(argparse_namespace('learning-review', other))


class ReviewRunnerDefaultStateTests(unittest.TestCase):
    def test_default_bootstrap_does_not_repair_existing_state_or_create_overlapping_state(self):
        from codex_agents import host_paths
        from codex_agents.collector_paths import default_state_root

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / 'home'
            home.mkdir()
            project = home / 'project'
            (project / 'reviews' / 'x').mkdir(parents=True)
            (home / 'reviews' / 'x').mkdir(parents=True)
            spec = {'id': 'fresh', 'summary': 'fresh install review', 'packet': 'reviews/x',
                    'context': [], 'objective': 'review it', 'verify_objective': 'check it',
                    'expected_output': 'findings', 'evidence': [{'id': 'a', 'description': 'point a'}]}
            environment = {key: value for key, value in os.environ.items() if key != 'XDG_STATE_HOME'}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(host_paths, 'account_home', return_value=home), \
                    mock.patch.object(host_paths, 'legacy_state_root', return_value=None):
                state = default_state_root()
                cli = FakeCli()
                with self.assertRaises(review_runner.ReviewError) as stopped:
                    review_runner.run('worker', spec, home / 'work', home, cli=cli)
                self.assertEqual('review_state_unavailable', stopped.exception.reason_code)
                self.assertFalse(state.exists())
                state.mkdir(parents=True, mode=0o700)
                for key_mode in (None, 0o644):
                    with self.subTest(key_mode=key_mode):
                        key = state / 'digest.key'
                        if key_mode is not None:
                            key.write_bytes(b'x' * 32)
                            key.chmod(key_mode)
                        with self.assertRaises(review_runner.ReviewError) as stopped:
                            review_runner.run('worker', spec, home / 'work', project, cli=cli)
                        self.assertEqual('review_state_unavailable', stopped.exception.reason_code)
                        self.assertEqual([key] if key_mode else [], list(state.iterdir()))
                self.assertEqual([], cli.calls)

    def test_fresh_default_state_outside_project_is_created_and_review_completes(self):
        from codex_agents import host_paths
        from codex_agents.collector_paths import CollectorPaths, default_state_root

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / 'home'
            home.mkdir()
            project = home / 'project'
            (project / 'reviews' / 'x').mkdir(parents=True)
            spec = {'id': 'fresh', 'summary': 'fresh install review', 'packet': 'reviews/x',
                    'context': ['reviews/x/README.md'], 'objective': 'review it',
                    'verify_objective': 'check it', 'expected_output': 'findings',
                    'evidence': [{'id': 'a', 'description': 'point a'}]}
            environment = {key: value for key, value in os.environ.items() if key != 'XDG_STATE_HOME'}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(host_paths, 'account_home', return_value=home), \
                    mock.patch.object(host_paths, 'legacy_state_root', return_value=None):
                state = default_state_root()
                self.assertEqual(home / '.local' / 'state' / 'codex-agents', state)
                self.assertNotIn(project, state.parents)
                self.assertFalse(state.exists())
                cli = FakeCli()
                for step in ('worker', 'verify', 'finish'):
                    review_runner.run(step, spec, home / 'work', project, cli=cli)
                self.assertEqual(0o700, state.stat().st_mode & 0o777)
                self.assertEqual(32, len(CollectorPaths().digest_key()))
                self.assertEqual('learning-close-orchestration', cli.calls[-1])
                for name, arguments in cli.arguments:
                    self.assertEqual(str(state), arguments[arguments.index('--state-root') + 1], name)


def argparse_namespace(command, state_root):
    import argparse
    return argparse.Namespace(command=command, state_root=state_root, project_root=None, root=None, policy=None)


if __name__ == '__main__':
    unittest.main()
