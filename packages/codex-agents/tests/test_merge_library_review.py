"""R1 production seam witnesses: own reports, populated owners and all pre-A paths."""
import errno
import unittest
from unittest import mock
from codex_agents import merge_library as ml
from tests.merge_library_support import Fixture, Trace, put, tree, assert_commit_flushes


class ReviewSeams(unittest.TestCase):
    __requirements__ = ('ML-11', 'ML-12', 'ML-17', 'ML-19', 'ML-20', 'ML-23', 'TF-18', 'TF-19')
    __why_not_scenario__ = 'Independent fault provenance and exact syscall boundaries certify crash behavior and the shared oracle.'

    def fixture(self):
        f = Fixture()
        self.addCleanup(f.close)
        return f

    def test_B3_generated_EIO_checks_the_operations_own_report(self):
        from tests.test_merge_library_machine import MergeMachine
        machine = MergeMachine()
        self.addCleanup(machine.teardown)
        class DrawExchange:
            def draw(self, strategy, **kwargs):
                return next(point for point in strategy.elements if point[1].operation == 'exchange' and point[1].when == 'before')
        original = ml._Store.io
        def swallow(store, operation, function, *args, **kwargs):
            try:
                return original(store, operation, function, *args, **kwargs)
            except OSError as error:
                if operation != 'exchange' or error.errno != errno.EIO:
                    raise
                return original(store, operation, function, *args, **kwargs)
        with mock.patch.object(ml._Store, 'io', swallow):
            with self.assertRaisesRegex(AssertionError, 'ordinary I/O failure cannot end landed'):
                machine.invoke(lambda hook: machine.f.land(machine.batch, hook=hook), DrawExchange(), 'EIO')
        self.assertEqual('landed', machine.last.state)
        self.assertEqual(errno.EIO, machine.fault)
        self.assertEqual('exchange', machine.fired_call.operation)

    def test_C3_owner_empty_descendant_keeps_ancestor_out_of_emptied(self):
        f = self.fixture()
        put(f.project / 'docs/sub/file', b'old')
        (f.project / 'docs/owner-empty').mkdir()
        f.before = f.operation_before = tree(f.project)
        report = f.land(f.batch({'docs/sub/file': None}))
        self.assertEqual('landed', report.state)
        self.assertEqual({'docs/sub'}, set(ml.history(f.project, f.private)[-1].emptied_folders))
        self.assertTrue((f.project / 'docs/owner-empty').is_dir())

    def test_C4_land_and_undo_flush_all_staging_before_commit_A(self):
        f = self.fixture()
        batch = f.batch()
        forward = Trace()
        landed = f.land(batch, hook=forward)
        self.assertEqual('landed', landed.state)
        assert_commit_flushes(self, forward.calls)
        undo = Trace()
        report = ml.undo(f.project, f.private, landed.batch_no, hook=undo)
        self.assertEqual('landed', report.state)
        self.assertTrue(any(c.operation == 'sqlite_commit' and c.detail.get('commit') == 'A' for c in undo.calls))
        assert_commit_flushes(self, undo.calls)

    def test_C6_unreadable_cleanup_reread_does_not_skip_later_rows(self):
        f = self.fixture()
        f.land(f.batch({'a.txt': b'first'}))
        f.land(f.batch({'second': b'second'}, 'child-2'))
        original_rows, original_clean = ml._Store.rows, ml._clean_row
        cleaned = []
        def clean(store, row):
            cleaned.append(row['id'])
            if len(cleaned) == 1:
                raise OSError(errno.EIO, 'cleanup fault')
            return original_clean(store, row)
        def rows(store):
            if len(cleaned) == 1:
                raise OSError(errno.EIO, 'reread fault')
            return original_rows(store)
        with mock.patch.object(ml, '_clean_row', clean), mock.patch.object(ml._Store, 'rows', rows):
            reports = ml.cleanup(f.project, f.private)
        self.assertEqual([1, 2], cleaned)
        self.assertEqual(('unknown', 'io', False), (reports[0].state, reports[0].reason, reports[0].recorded))
        self.assertEqual('landed', reports[1].state)

    def test_C7_unrecorded_undo_parent_is_kept_empty_named_and_pinned(self):
        f = self.fixture()
        batch = f.batch({'removed/old.txt': None})
        landed = f.land(batch)
        fired = []
        def fault(call):
            if not fired and call.operation == 'mkdir' and call.when == 'after' and call.path == str(f.project / 'removed'):
                fired.append(call)
                raise OSError(errno.EIO, 'mkdir acknowledgement lost')
        report = ml.undo(f.project, f.private, landed.batch_no, hook=fault)
        self.assertEqual(1, len(fired))
        self.assertEqual(('needs_owner', 'io'), (report.state, report.reason))
        self.assertEqual([], list((f.project / 'removed').iterdir()))
        self.assertIn('removed', {p.path for p in report.paths})
        self.assertTrue(ml.history(f.project, f.private)[-1].contents_kept)

    def test_r2_M4_each_missing_provenance_field_breaks_generated_EIO_proof(self):
        from tests import test_merge_library_machine as generated
        for field in ('call', 'fault'):
            original = generated.check_end_state
            observed = []
            def omit(test, fixture, batch, report, **kwargs):
                observed.append((report.state, kwargs['call'].operation, kwargs['fault']))
                kwargs.pop(field)
                return original(test, fixture, batch, report, **kwargs)
            case = ReviewSeams('test_B3_generated_EIO_checks_the_operations_own_report')
            result = unittest.TestResult()
            with mock.patch.object(generated, 'check_end_state', omit):
                case.run(result)
            self.assertEqual([], result.errors, 'M4 proof failed before its outcome assertion')
            self.assertEqual(1, len(result.failures), 'M4 omission did not break the deterministic EIO proof: ' + field)
            self.assertIn('AssertionError not raised', result.failures[0][1])
            self.assertEqual([('landed', 'exchange', errno.EIO)], observed, 'M4 wrong generated failure boundary')
