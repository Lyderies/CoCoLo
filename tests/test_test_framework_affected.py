"""Dependency selection witnesses using synthetic maps, Git replies and coverage data."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests as runner
import test_affected as dependencies


class AffectedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.commit, self.base = 'a' * 40, 'b' * 40
        self.helper = 'packages/codex-agents/native-package/.codex/orchestration/codex_agents/result_persistence.py'
        self.holds = 'codex:tests.test_project_holds_schema'
        self.context = 'codex:tests.test_record_context'
        self.other = 'root:test_other'
        self.sources = {self.holds: 'packages/codex-agents/tests/test_project_holds_schema.py',
                        self.context: 'packages/codex-agents/tests/test_record_context.py',
                        self.other: 'tests/test_other.py'}
        self.modules = {module: [source] for module, source in self.sources.items()}
        self.modules[self.holds] += [self.helper, 'packages/codex-agents/tests/project_supervisor_fixture.py']
        self.modules[self.context] += [self.helper, 'packages/codex-agents/tests/project_supervisor_fixture.py']
        self.modules[self.other] += ['scripts/unrelated.py']
        self.write_map()
        self.changed, self.since_map, self.untracked = [], [], []
        self.ancestor = True
        self.git_calls = []
        self.git_patch = mock.patch.object(dependencies.subprocess, 'run', side_effect=self.git_fixture)
        self.git_patch.start()
        self.addCleanup(self.git_patch.stop)

    def write_map(self):
        path = self.root / dependencies.CACHE
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'commit': self.commit, 'modules': self.modules}))

    def git_fixture(self, command, **options):
        """Exact Git command/reply fixtures: component tests start no processes."""
        self.assertEqual(self.root, options['cwd'])
        self.assertTrue(options['check'])
        self.git_calls.append(command)
        if command == ['git', 'merge-base', '--is-ancestor', self.commit, 'HEAD']:
            if not self.ancestor:
                raise subprocess.CalledProcessError(1, command)
            output = ''
        elif command == ['git', 'diff', '--name-only', '--no-renames', '-z', self.base, '--']:
            output = '\0'.join(self.changed)
        elif command == ['git', 'diff', '--name-only', '--no-renames', '-z', self.commit, '--']:
            output = '\0'.join(self.since_map)
        elif command == ['git', 'ls-files', '--others', '--exclude-standard', '-z']:
            output = '\0'.join(self.untracked)
        else:
            self.fail('unexpected Git command: ' + repr(command))
        return subprocess.CompletedProcess(command, 0, output, '')

    def select(self):
        return dependencies.affected(self.root, self.base, self.sources)

    def assert_full(self, reason):
        selected, reasons = self.select()
        self.assertEqual(set(self.sources), set(selected))
        self.assertIn(reason, '\n'.join(reasons))
        for module in self.sources:
            self.assertIn(reason, '; '.join(selected[module]))

    def test_D4_fixture_only_dependency_selects_both_users(self):
        self.changed = [self.helper]
        selected, reasons = self.select()
        self.assertEqual({self.holds, self.context}, set(selected))
        self.assertEqual([], reasons)
        for module in selected:
            self.assertEqual(['executed changed file: ' + self.helper], selected[module])

    def test_unrelated_change_selects_only_its_own_module(self):
        self.changed = ['scripts/unrelated.py']
        selected, reasons = self.select()
        self.assertEqual({self.other: ['executed changed file: scripts/unrelated.py']}, selected)
        self.assertEqual([], reasons)

    def test_changed_test_selected_even_without_its_own_dependency_entry(self):
        # The file was seen by another worker, but its own map omitted it.
        source = self.sources[self.other]
        self.modules[self.holds].append(source)
        self.modules[self.other] = ['scripts/unrelated.py']
        self.write_map()
        self.changed = [source]
        selected, reasons = self.select()
        self.assertEqual({self.holds, self.other}, set(selected))
        self.assertIn('changed or new test module: ' + source, selected[self.other])
        self.assertEqual([], reasons)

    def test_new_test_is_run_and_unseen_python_forces_full_run(self):
        self.sources['root:test_new'] = 'tests/test_new.py'
        self.untracked = ['tests/test_new.py']
        self.assert_full('changed Python file was never recorded: tests/test_new.py')

    def test_no_map_forces_full_run(self):
        (self.root / dependencies.CACHE).unlink()
        self.assert_full('no usable coverage map')
        self.assertEqual([], self.git_calls)

    def test_non_ancestor_map_forces_full_run(self):
        self.ancestor = False
        self.assert_full('coverage map commit is not an ancestor of HEAD')

    def test_unseen_python_forces_full_run_including_deleted_files(self):
        for name in ['scripts/never_executed.py', 'packages/codex-agents/deleted.py', 'new.py']:
            with self.subTest(path=name):
                self.changed = [name]
                self.assert_full('changed Python file was never recorded: ' + name)

    def test_non_python_files_force_full_run(self):
        for name in ['scripts/config.json', 'scripts/setup.sh', 'packages/a/template.j2', 'setup.sh',
                     'requirements-test.txt', 'tests/merge-persistence-samples.json', 'tests/e2e/requirements.json',
                     '.github/workflows/ci.yml', 'tests/notes.md']:
            with self.subTest(path=name):
                self.changed = [name]
                self.assert_full('changed non-Python file: ' + name)

    def test_opaque_module_is_selected_on_any_python_change(self):
        (self.root / dependencies.CACHE).write_text(json.dumps(
            {'commit': self.commit, 'opaque': [self.other], 'modules': self.modules}))
        self.changed = [self.helper]
        selected, reasons = self.select()
        self.assertEqual({self.holds, self.context, self.other}, set(selected))
        self.assertIn('opaque in the coverage map (failed under coverage): any Python change', selected[self.other])
        self.changed = ['README.md']
        self.assertEqual(({}, []), self.select())

    def test_module_starting_reduced_env_children_is_selected_on_any_python_change(self):
        source = self.root / self.sources[self.other]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("subprocess.run(['x'], env={'PATH': '/usr/bin'})\n")
        self.changed = [self.helper]
        selected, _ = self.select()
        self.assertIn('starts children with a reduced environment (unmeasured): any Python change',
                      selected[self.other])
        source.write_text("subprocess.run(['x'], env={**os.environ, 'A': '1'})\n")
        selected, _ = self.select()
        self.assertNotIn(self.other, selected)

    def test_no_dependencies_changed_selects_nothing(self):
        self.changed = ['README.md']
        self.assertEqual(({}, []), self.select())

    def test_changes_since_recording_and_untracked_files_are_included(self):
        self.since_map = [self.helper]
        self.untracked = ['scripts/unrelated.py']
        selected, reasons = self.select()
        self.assertEqual(set(self.sources), set(selected))
        self.assertEqual([], reasons)

    def test_bad_or_incomplete_map_forces_full_run(self):
        path = self.root / dependencies.CACHE
        for contents in ['{', '[]', json.dumps({'commit': self.commit, 'modules': {self.other: ['../outside.py']}})]:
            with self.subTest(contents=contents):
                path.write_text(contents)
                self.assert_full('no usable coverage map')
        self.modules.pop(self.other)
        self.write_map()
        self.assert_full('module missing from coverage map: ' + self.other)

    def test_git_failure_forces_full_run(self):
        with mock.patch.object(dependencies, 'changed_files', side_effect=subprocess.CalledProcessError(128, ['git'])):
            self.assert_full('cannot determine changed files')


class RecordingTests(unittest.TestCase):
    def test_synthetic_worker_and_child_data_union_across_split_module(self):
        import coverage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work = root / 'work'
            work.mkdir()
            recorder = dependencies.CoverageRecorder(root, work, ['root:test_sample'])
            for index in ['1', '2']:
                folder = work / index
                command = recorder.command(folder, ['test_sample.Case.test_' + index])
                self.assertEqual([sys.executable, '-B', '-m', 'coverage', 'run', '--rcfile'], command[:6])
                self.assertEqual(str(recorder.bootstrap), command[7])
                config = Path(command[6]).read_text()
                self.assertIn('patch = subprocess', config)
                self.assertIn('parallel = true', config)
                for suffix, source in [('worker', 'tests/test_sample.py'), ('child', 'scripts/child_' + index + '.py')]:
                    data = coverage.CoverageData(basename=str(recorder.directory / index / ('.coverage.' + suffix)))
                    data.add_lines({str(root / source): {1}, str(root.parent / 'external.py'): {1},
                                    str(root / 'unexecuted.py'): set()})
                    data.write()
                recorder.collect(folder, 'root:test_sample')
            destination = recorder.write('c' * 40, 12.5)
            result = json.loads(destination.read_text())
            self.assertEqual('c' * 40, result['commit'])
            self.assertEqual(12.5, result['recording_seconds'])
            self.assertEqual({'root:test_sample': ['scripts/child_1.py', 'scripts/child_2.py', 'tests/test_sample.py']},
                             result['modules'])

    def test_cli_routes_dependency_modes_and_ordinary_runs_without_coverage(self):
        for args, options in [([], {}), (['--record-coverage'], {'record_coverage': True}),
                              (['--affected', 'BASE'], {'affected_base': 'BASE'})]:
            with self.subTest(args=args), mock.patch.object(runner, 'run', return_value=0) as run:
                self.assertEqual(0, runner.main(args))
                self.assertEqual(options, run.call_args.kwargs)
        for args in [['--record-coverage', '--module', 'root:test_sample'], ['--affected', 'BASE', '--stage', 'unit'],
                     ['--record-coverage', '--shard', '1/2'], ['--affected', 'BASE', '--profile', 'dev'],
                     ['--affected', 'BASE', '--record-coverage']]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(runner, 'run') as run:
                with self.assertRaises(SystemExit):
                    runner.main(args)
                run.assert_not_called()
