"""TF-19: regression witnesses for the reviewed end-state table, independent history input."""
import errno
import stat
import unittest
from dataclasses import replace

from tests.merge_library_support import Fixture, put, tree
from codex_agents import merge_library as ml


class EndStates(unittest.TestCase):
    __requirements__ = ('TF-19',)
    __why_not_scenario__ = 'Direct negative controls for the single crash-state oracle; lifecycle cannot certify an oracle rejects corrupt states.'

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.batch = self.f.batch({'a.txt': b'child'})
        self.start = tree(self.f.project)
        self.row = ml.HistoryRow(1, self.batch, 'reverted')
        self.report = ml.Report('reverted', batch_no=1, recorded=True,
                                paths=(ml.PathReport('a.txt', 'base'),))

    def check(self, row=None, report=None, **kw):
        from tests.merge_library_support import check_end_state
        rows = (row or self.row,)
        return check_end_state(self, self.f, self.batch, report or self.report,
                               kind='forward', read_history=lambda: rows, **kw)

    def test_E2_cleanup_fault_preserves_durable_reverted_and_restored_tree(self):
        self.check(call=ml.IOCall('unlink', 'after', 'cleanup'), fault=errno.EIO)
        put(self.f.project / 'a.txt', b'child')
        with self.assertRaises(AssertionError):
            self.check(call=ml.IOCall('unlink', 'after', 'cleanup'), fault=errno.EIO)

    def test_F1_retirement_fault_preserves_terminal_direction(self):
        self.check(call=ml.IOCall('sqlite_commit', 'before', 'retirement'), fault=errno.EIO)
        put(self.f.project / 'a.txt', b'child')
        self.check(row=replace(self.row, state='landed'),
                   report=replace(self.report, state='landed', paths=(ml.PathReport('a.txt', 'new'),)),
                   call=ml.IOCall('sqlite_commit', 'before', 'retirement'), fault=errno.EIO)

    def test_B3_pins_only_this_operation_and_rejects_partial_bytes(self):
        from tests.merge_library_support import check_end_state
        row = replace(self.row, state='needs_owner')
        report = replace(self.report, state='needs_owner', reason='io')
        unrelated = replace(self.row, batch_no=2, contents_kept=False)
        check_end_state(self, self.f, self.batch, report, kind='forward', read_history=lambda: (row, unrelated))
        put(self.f.project / 'a.txt', b'ch')
        with self.assertRaises(AssertionError):
            check_end_state(self, self.f, self.batch, report, kind='forward', read_history=lambda: (row, unrelated))

    def test_C3_record_pending_requires_applying_row_and_pinned_contents(self):
        report = replace(self.report, state='needs_owner', reason='io', recorded=False)
        self.check(row=replace(self.row, state='applying'), report=report)
        with self.assertRaises(AssertionError):
            self.check(report=report)
        with self.assertRaises(AssertionError):
            self.check(row=replace(self.row, state='applying', contents_kept=False), report=report)

    def test_B2_unrecorded_directory_is_empty_named_and_does_not_change_before(self):
        self.batch = self.f.batch({'created/add': b'child'})
        (self.f.project / 'created').mkdir()
        row = replace(self.row, batch=self.batch, state='needs_owner')
        report = replace(self.report, state='needs_owner', reason='io',
                         paths=(ml.PathReport('created/add', 'absent'), ml.PathReport('created', 'absent')))
        self.check(row=row, report=report, kept_candidates={'created'})
        self.assertEqual(self.start, self.f.before)
        put(self.f.project / 'created/foreign', b'owner')
        with self.assertRaises(AssertionError):
            self.check(row=row, report=report, kept_candidates={'created'})

    def test_G2_failed_undo_returns_post_cleanup_start_including_absent_empty_folders(self):
        self.batch = self.f.batch({'removed/old.txt': None})
        (self.f.project / 'removed/old.txt').unlink()
        (self.f.project / 'removed').rmdir()
        self.f.operation_before = tree(self.f.project)
        forward = replace(self.row, batch=self.batch, state='landed')
        undo = replace(forward, batch_no=2, state='reverted', kind='undo', undoes=1)
        report = ml.Report('reverted', batch_no=2, recorded=True,
                           paths=(ml.PathReport('removed/old.txt', 'absent'),))
        from tests.merge_library_support import check_end_state
        check_end_state(self, self.f, self.batch, report, kind='undoing', read_history=lambda: (forward, undo))
        put(self.f.project / 'removed/old.txt', b'deleted\n')
        with self.assertRaises(AssertionError):
            check_end_state(self, self.f, self.batch, report, kind='undoing', read_history=lambda: (forward, undo))

    def test_A5_waiting_owner_requires_owner_at_target_not_merely_retained(self):
        from tests.merge_library_support import check_end_state
        put(self.f.project / 'a.txt', b'owner')
        self.f.operation_before = tree(self.f.project)
        self.f.owner_versions['a.txt'] = {(stat.S_IFREG, 0o644, b'owner')}
        report = ml.Report('waiting_owner', paths=(ml.PathReport('a.txt', 'owner'),))
        check_end_state(self, self.f, self.batch, report, kind='forward', read_history=lambda: ())
        put(self.f.project / 'a.txt', b'child')
        with self.assertRaises(AssertionError):
            check_end_state(self, self.f, self.batch, report, kind='forward', read_history=lambda: ())

    def test_E1_unlisted_empty_folder_is_never_removed(self):
        (self.f.project / 'owner-empty').mkdir()
        self.f.operation_before = self.f.before = tree(self.f.project)
        self.check(call=ml.IOCall('unlink', 'before', 'cleanup'), fault=errno.EIO)
        (self.f.project / 'owner-empty').rmdir()
        with self.assertRaises(AssertionError):
            self.check(call=ml.IOCall('unlink', 'before', 'cleanup'), fault=errno.EIO)

    def test_B1_io_cannot_end_landed_unless_commit_uncertainty(self):
        put(self.f.project / 'a.txt', b'child')
        row = replace(self.row, state='landed')
        report = replace(self.report, state='landed', paths=(ml.PathReport('a.txt', 'new'),))
        with self.assertRaises(AssertionError):
            self.check(row=row, report=report, call=ml.IOCall('exchange', 'after', 'forward'), fault=errno.EIO)
        self.check(row=row, report=report, call=ml.IOCall('sqlite_commit', 'after', 'forward'), fault=errno.EIO)

    def test_checker_rereads_real_history_instead_of_trusting_report(self):
        put(self.f.project / 'a.txt', b'child')
        from tests.merge_library_support import check_end_state
        with self.assertRaises(AssertionError):
            check_end_state(self, self.f, self.batch, replace(self.report, state='landed'), kind='forward')

    def test_review_A1_cleanup_cannot_rewrite_terminal_direction(self):
        with self.assertRaisesRegex(AssertionError, 'terminal|landed'):
            self.check(call=ml.IOCall('unlink', 'before', 'cleanup'), fault=errno.EIO,
                       terminal_before='landed')
        with self.assertRaisesRegex(AssertionError, 'pre-fault durable'):
            self.check(call=ml.IOCall('unlink', 'before', 'cleanup'), fault=errno.EIO)

    def test_review_A2_unknown_needs_independent_history_failure(self):
        report = ml.Report('unknown', reason='io', recorded=False)
        with self.assertRaisesRegex(AssertionError, 'history failure'):
            self.check(report=report)
        self.check(report=report, call=ml.IOCall('sqlite_read', 'before', 'recovery'), fault=errno.EIO)

    def test_review_A5_admission_cannot_hide_applying_row(self):
        with self.assertRaisesRegex(AssertionError, 'applying|authority'):
            self.check(row=replace(self.row, state='applying'), report=ml.Report('refused'))

    def test_review_A6_terminal_rows_pin_contents_until_release(self):
        with self.assertRaisesRegex(AssertionError, 'pin'):
            self.check(row=replace(self.row, contents_kept=False))
        put(self.f.project / 'a.txt', b'child')
        with self.assertRaisesRegex(AssertionError, 'pin'):
            self.check(row=replace(self.row, state='landed', contents_kept=False),
                       report=replace(self.report, state='landed', paths=(ml.PathReport('a.txt', 'new'),)))

    def test_review_A8_unknown_cannot_authorize_protected_directory_deletion(self):
        (self.f.project / 'protected').mkdir()
        self.f.before = self.f.operation_before = tree(self.f.project)
        (self.f.project / 'protected').rmdir()
        report = replace(self.report, state='needs_owner', reason='io',
                         paths=self.report.paths + (ml.PathReport('protected', 'unknown'),))
        with self.assertRaises(AssertionError):
            self.check(row=replace(self.row, state='needs_owner'), report=report)

    def test_review_A10_early_return_cannot_mutate_start_evidence(self):
        from tests.merge_library_support import check_end_state
        def history():
            self.f.before['corruption'] = (stat.S_IFREG, 0o644, b'bad')
            return ()
        with self.assertRaisesRegex(AssertionError, 'start evidence'):
            check_end_state(self, self.f, self.batch, ml.Report('refused'), read_history=history)

    def test_review_A3_retry_landed_is_rejected(self):
        put(self.f.project / 'a.txt', b'child')
        with self.assertRaisesRegex(AssertionError, 'retry'):
            self.check(row=replace(self.row, state='landed'), report=replace(self.report, state='landed'), action='retry')

    def test_review_A4_policy_switches_are_live_and_independent(self):
        from tests.merge_library_support import POLICY, policy_outcomes
        from unittest.mock import patch
        defaults = policy_outcomes()
        alternatives = {'Q1': 'reverted_with_leftover', 'Q2': 'fresh_forward',
                        'Q3': 'record_pending', 'Q4': 'reverted_with_owner',
                        'Q5': 'direct_parents_only', 'Q6': 'wait_for_recovery'}
        for key, value in alternatives.items():
            with self.subTest(policy=key), patch.dict(POLICY, {key: value}):
                observed = policy_outcomes()
                self.assertNotEqual(defaults[key], observed[key])
                self.assertEqual({k: v for k, v in defaults.items() if k != key},
                                 {k: v for k, v in observed.items() if k != key})
        with patch.dict(POLICY, {'Q2': 'fresh_forward'}):
            put(self.f.project / 'a.txt', b'child')
            self.check(row=replace(self.row, state='landed'),
                       report=replace(self.report, state='landed', paths=(ml.PathReport('a.txt', 'new'),)), action='retry')

    def test_review_A7_real_samples_reject_stale_and_uncovered_kinds(self):
        import json
        from unittest.mock import patch
        from tests.merge_library_support import sample_boundaries
        call = ml.IOCall('write', 'before', 'forward')
        point = (('key', 1), call)
        for rows in ([{'key': ['stale', 1]}], [{'key': ['key', 1]}]):
            points = [point] if rows[0]['key'][0] == 'stale' else [point, (('new', 1), replace(call, operation='unlink'))]
            with self.subTest(rows=rows), patch('pathlib.Path.read_text', return_value=json.dumps({'samples': {'real': {'forward': rows}}})):
                with self.assertRaisesRegex(AssertionError, 'stale|boundary'):
                    sample_boundaries(points)

    def test_review_A9_unmapped_method_is_not_grandfathered(self):
        from tests.merge_library_support import declare_ml_tests
        class Unknown(unittest.TestCase):
            def test_unmapped(self):
                pass
        with self.assertRaisesRegex(ValueError, 'unmapped'):
            declare_ml_tests({'__name__': __name__, 'Unknown': Unknown})

    def test_review_L12_emptied_directory_mode_is_protected(self):
        self.batch = self.f.batch({'removed/old.txt': None})
        info = (self.f.project / 'removed').stat()
        (self.f.project / 'removed/old.txt').unlink()
        row = replace(self.row, batch=self.batch, state='landed', emptied_folders={'removed': (info.st_dev, info.st_ino)})
        report = replace(self.report, state='landed', paths=(ml.PathReport('removed/old.txt', 'absent'),))
        self.check(row=row, report=report)
        (self.f.project / 'removed').chmod(0o700)
        with self.assertRaisesRegex(AssertionError, 'mode'):
            self.check(row=row, report=report)


if __name__ == '__main__':
    unittest.main()
