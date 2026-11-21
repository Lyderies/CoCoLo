"""Recurring fix (stale collector, 21 November 2026): the review runner's verdict and stage-file rules.

Requirements:
(d) a result with no verdict is refused before it is recorded.
(e) learning-review verify and finish refuse a stage file the runner did not write: wrong mode or shape, a
    digest its own recorded reply does not hold, a provider, verdict or summary its saved route and result do
    not state, or a replaced route, assignment or result.
(f) the stage binding is an HMAC under a key derived from the host's private digest key (the state folder),
    so a writer who can only reach the work folder cannot make one, even by recomputing the old unkeyed
    sha256; the key is never in the work folder; a missing or loose key, or a state folder inside the work
    folder or the packet, stops the review before any child starts.
(g) the state folder the key comes from is the one every host command is told to use.

Regression tests (red on the revision before them) are the (f) and (g) ones and the rerun-after-a-state-failure
case; the (d)/(e) tests and the keyed-binding ones named GUARD pass on the revision before and keep their
behaviour. These tests pass the state folder only when the runner accepts one (`base.TAKES_STATE`), so they run against a
runner from before the keyed binding and fail there on the requirement, not on the call.
"""
import json
import os
import unittest
from pathlib import Path

from codex_agents import review_runner
from tests import test_review_runner as base


class ReviewRunnerProvenanceTests(unittest.TestCase):
    setUp = base.ReviewRunnerTests.setUp
    run_step = base.ReviewRunnerTests.run_step
    guard_real_state = base.ReviewRunnerTests.guard_real_state
    state_kw = base.ReviewRunnerTests.state_kw

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

    def test_e_a_writer_who_recomputes_the_unkeyed_digest_is_refused(self):
        # Mode, shape, both digests and an unkeyed sha256 over the files all agree: only a binding made with the
        # host's private key tells this was not written by the runner.
        import hashlib
        self.staged()
        for prefix, stage, record in (('', 'worker.json', 'record.json'), ('v', 'verifier.json', 'vrecord.json')):
            loaded = {name: json.loads((self.work / (prefix + name + '.json')).read_text())
                      for name in ('route', 'assignment', 'result')}
            current = json.loads((self.work / stage).read_text())
            forged = 'e' * 64
            unkeyed = hashlib.sha256(json.dumps(
                {'digest': forged, 'provider': current['provider'], 'verdict': current['verdict'],
                 'summary': current['summary'], 'review': self.spec['id'], 'project': str(self.root.resolve()),
                 'stage': prefix or 'worker', **loaded}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.rewrite(stage, digest=forged, binding=unkeyed)
            self.rewrite(record, host_outcome_digest=forged)
        self.assertEqual('review_stage_not_runner_written', self.refused())
        self.assertEqual('review_stage_not_runner_written', self.refused('verify'))

    def test_e_a_binding_made_under_another_state_is_refused(self):
        """GUARD on ddefa327 (red on 8ec26a08, which has no key): a binding made under another state's key
        is refused."""
        self.staged()
        other = self.work.parent / 'other-state'
        other.mkdir(mode=0o700)
        (other / 'digest.key').write_bytes(bytes(range(1, 33)))
        (other / 'digest.key').chmod(0o600)
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('finish', self.spec, self.work, self.root, cli=base.FakeCli(), **self.state_kw(other))
        self.assertEqual('review_stage_not_runner_written', stopped.exception.reason_code)

    def test_e_the_key_is_not_in_the_work_folder_and_a_missing_or_loose_key_stops_the_review(self):
        """GUARD on ddefa327 (red on 8ec26a08): neither the host key nor the key derived from it is in any work
        file, and a loose or missing key refuses finish."""
        self.staged()
        key = (self.state / 'digest.key').read_bytes()
        for path in self.work.iterdir():
            self.assertNotIn(key, path.read_bytes(), path.name)
            self.assertNotIn(key.hex().encode(), path.read_bytes(), path.name)
            import hashlib
            import hmac
            derived = hmac.new(key, b'review-stage-binding-v1\0' + str(self.root.resolve()).encode(),
                               hashlib.sha256).digest()
            self.assertNotIn(derived, path.read_bytes(), path.name)
            self.assertNotIn(derived.hex().encode(), path.read_bytes(), path.name)
        os.chmod(self.state / 'digest.key', 0o644)  # not the owner-only secret the host keeps
        self.assertEqual('review_state_unavailable', self.refused())
        os.chmod(self.state / 'digest.key', 0o600)
        (self.state / 'digest.key').unlink()
        self.assertEqual('review_state_unavailable', self.refused())

    def restore_key(self):
        key = self.state / 'digest.key'
        key.write_bytes(bytes(range(32)))
        os.chmod(key, 0o600)

    def test_f_a_missing_or_loose_key_stops_the_review_before_any_child_starts(self):
        """Requirement (f). Regression: ddefa327 reads the key only after the worker has run and been recorded."""
        self.addCleanup(self.restore_key)
        for damage in ('missing', 'loose'):
            with self.subTest(damage=damage):
                self.restore_key()
                key = self.state / 'digest.key'
                if damage == 'loose':
                    os.chmod(key, 0o644)
                else:
                    key.unlink()
                cli = base.FakeCli()
                with self.assertRaises(review_runner.ReviewError) as stopped:
                    self.run_step('worker', cli)
                self.assertEqual(('review_state_unavailable', []), (stopped.exception.reason_code, cli.calls))

    def test_f_a_rerun_stopped_by_the_state_check_leaves_no_earlier_marker_for_finish(self):
        """Requirement (e)/(f): a stage that starts again removes its earlier markers even when the state
        check stops it. Regression on 0bfe28b9 (which checks the state before forgetting, so the old markers
        survive and finish accepts them); it passes on ddefa327, where the key was read late."""
        self.addCleanup(self.restore_key)
        self.staged()
        os.chmod(self.state / 'digest.key', 0o644)
        with self.assertRaises(review_runner.ReviewError) as stopped:
            self.run_step('worker', base.FakeCli())
        self.assertEqual('review_state_unavailable', stopped.exception.reason_code)
        self.restore_key()
        with self.assertRaises(review_runner.ReviewError) as later:
            self.run_step('finish', base.FakeCli())
        self.assertEqual('review_stage_not_ready', later.exception.reason_code)

    def keyed_state(self, folder):
        folder.mkdir(parents=True, mode=0o700)
        (folder / 'digest.key').write_bytes(bytes(range(32)))
        os.chmod(folder / 'digest.key', 0o600)
        return folder

    def stopped_before_any_child(self, state, *, work=None, root=None, spec=None):
        cli = base.FakeCli()
        with self.assertRaises(review_runner.ReviewError) as stopped:
            review_runner.run('worker', spec or self.spec, work or self.work, root or self.root, cli=cli,
                              **self.state_kw(state))
        self.assertEqual(('review_state_unavailable', []), (stopped.exception.reason_code, cli.calls))

    def test_f_a_state_folder_inside_the_work_folder_the_packet_or_the_project_is_refused(self):
        """Requirement (f). Regression: ddefa327 accepts a state folder inside the packet's project or beside
        it, and only compared the work folder and packet by exact path."""
        for inside in (self.work / 'state', self.root / 'reviews' / 'x' / 'state', self.root / 'state'):
            with self.subTest(inside=str(inside.relative_to(inside.parents[2]))):
                self.stopped_before_any_child(self.keyed_state(inside))

    def test_f_a_work_folder_packet_or_project_inside_the_state_folder_is_refused(self):
        """Requirement (f), the reverse containment. Regression: ddefa327 checks one direction only."""
        state = self.keyed_state(self.state.parent / 'wide-state')
        self.stopped_before_any_child(state, work=state / 'work')
        inner = state / 'project'
        (inner / 'reviews' / 'x').mkdir(parents=True)
        self.stopped_before_any_child(state, root=inner)

    def test_f_the_overlap_check_is_not_fooled_by_letter_case(self):
        """Requirement (f): a state folder inside the work folder, named with other letter case, is refused
        (the folders are compared by identity; macOS volumes are case-insensitive by default)."""
        probe = self.work.parent / 'CaseProbe'
        probe.mkdir()
        if (self.work.parent / 'caseprobe').exists() is False:
            self.skipTest('this volume is case-sensitive')
        self.work.mkdir(parents=True, exist_ok=True)
        folder = self.keyed_state(self.work / 'state')
        self.stopped_before_any_child(Path(str(folder).replace(str(self.work), str(self.work).swapcase())))

    def test_e_the_files_the_runner_wrote_are_still_accepted(self):
        cli = self.staged()
        self.run_step('finish', cli)
        self.assertEqual('learning-close-orchestration', cli.calls[-1])
