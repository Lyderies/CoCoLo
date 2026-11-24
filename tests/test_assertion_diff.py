"""The report distinguishes assertion edits, moves, deletions, and non-code text."""
import importlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import assertion_diff as diff
from test_framework import requirements


@requirements('TF-7')
class AssertionDiffTests(unittest.TestCase):
    def test_added_removed_and_changed_assertions_have_locations(self):
        before = 'class Case:\n def test_value(self):\n  self.assertEqual(1, value)\n  assert ready\n def test_deleted(self):\n  self.assertTrue(ok)\n'
        after = 'class Case:\n def test_value(self):\n  self.assertEqual(2, value)\n  assert ready\n def test_added(self):\n  assert new\n'
        report = diff.compare_sources(before, after, 'tests/test_case.py')
        self.assertEqual([{'name': 'Case.test_deleted', 'line': 5}], report['deleted_test_methods'])
        self.assertEqual('self.assertEqual(1, value)', report['changed'][0]['before']['text'])
        self.assertEqual(3, report['changed'][0]['after']['line'])
        self.assertEqual(['assert new'], [r['text'] for r in report['added']])
        self.assertEqual(['self.assertTrue(ok)'], [r['text'] for r in report['removed']])

    def test_multiline_async_context_and_plain_asserts(self):
        source = 'async def test_async():\n with self.assertRaises(ValueError):\n  assert (\n   value == 1\n  )\n'
        report = diff.compare_sources('', source, 'tests/test_async.py')
        self.assertEqual(2, len(report['added']))
        self.assertIn('value == 1', report['added'][1]['text'])
        self.assertEqual('test_async', report['added'][0]['scope'])
        self.assertEqual([{'name': 'test_async', 'line': 1}], diff.compare_sources(source, '', 'p')['deleted_test_methods'])

    def test_comments_strings_and_line_shifts_do_not_report_assertion_changes(self):
        before = 'def test_ok():\n assert good\n'
        after = '# assert bad\n"self.assertFalse(bad)"\n' + before
        report = diff.compare_sources(before, after, 'tests/test_case.py')
        self.assertFalse(any(value for key, value in report.items() if key != 'path'))

    def test_deleted_file_includes_every_test_and_helper_assertion(self):
        before = 'def helper():\n assert setup\nclass Case:\n def test_a(self):\n  self.assertTrue(ok)\n def test_b(self):\n  pass\n'
        report = diff.compare_sources(before, '', 'tests/deleted.py')
        self.assertEqual(2, len(report['removed']))
        self.assertEqual(['Case.test_a', 'Case.test_b'], [r['name'] for r in report['deleted_test_methods']])

    def test_path_filter_covers_root_and_package_tests(self):
        for path in ('tests/test_case.py', 'packages/a/tests/test_case.py', 'tests/helpers.py'):
            self.assertTrue(diff.test_path(path))
        for path in ('scripts/product.py', 'tests/notes.md'):
            self.assertFalse(diff.test_path(path))

    def test_commit_report_handles_added_deleted_files_and_parse_errors(self):
        before = 'def test_removed():\n assert old\n'
        after = 'def test_new():\n assert new\n'
        def git(*args, **kwargs):
            if args[0] == 'rev-parse':
                return (('a' if args[-1].startswith('base') else 'b') * 40 + '\n').encode()
            if args[0] == 'diff':
                return b'tests/new.py\0tests/deleted.py\0tests/bad.py\0scripts/product.py\0'
            # ls-tree is called as ('ls-tree', '-z', revision, '--', path).
            revision, path = args[2], args[-1]
            exists = (revision.startswith('a') and path == 'tests/deleted.py') or (revision.startswith('b') and path != 'tests/deleted.py')
            if args[0] == 'ls-tree':
                return b'blob' if exists else b''
            if args[0] == 'show':
                return (before if path.endswith('deleted.py') else 'def broken(' if path.endswith('bad.py') else after).encode()
            self.fail(args)
        # show has revision:path as its only argument.
        def fake_git(*args, **kwargs):
            if args[0] == 'show':
                path = args[1].split(':', 1)[1]
                return (before if path.endswith('deleted.py') else 'def broken(' if path.endswith('bad.py') else after).encode()
            return git(*args, **kwargs)
        with mock.patch.object(diff, 'git', side_effect=fake_git):
            report = diff.commit_report('base', 'head')
        self.assertEqual(3, len(report['files']))
        self.assertIn('parse_error', report['files'][0])
        self.assertEqual('test_removed', report['files'][1]['deleted_test_methods'][0]['name'])
        self.assertEqual('assert new', report['files'][2]['added'][0]['text'])

    def test_ci_is_a_new_report_only_job_and_codeowners_cover_the_scope(self):
        root = Path(__file__).resolve().parents[1]
        jobs = importlib.import_module('test_ci_stages').jobs((root / '.github/workflows/ci.yml').read_text())
        report = jobs['assertion-diff']
        self.assertIn('continue-on-error: true', report)
        self.assertIn('scripts/assertion_diff.py', report)
        self.assertIn('|| true', report)
        self.assertNotIn('pip install', report)
        owners = (root / '.github/CODEOWNERS').read_text()
        for pattern in ('/tests/**', '/packages/*/tests/**', '/scripts/parallel_tests.py', '/scripts/test_framework.py', '/scripts/test_worker.py', '/.github/'):
            self.assertIn(pattern + ' ', owners)
