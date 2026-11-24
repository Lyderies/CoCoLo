"""Executed-file dependencies for the test runner (stdlib plus optional coverage.py)."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


CACHE = '.cocolo-test-cache/coverage-map.json'


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def changed_files(root, base):
    # BASE to the working tree includes staged and unstaged edits; add new files too.
    return set(git(root, 'diff', '--name-only', '--no-renames', '-z', base, '--').split('\0')) - {''}


def affected(root, base, sources):
    """Return module -> selection reasons, plus reasons requiring the whole suite."""
    def full(reason):
        return {module: ['full run: ' + reason] for module in sources}, [reason]

    try:
        record = json.loads((root / CACHE).read_text())
        commit, modules = record['commit'], record['modules']
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            raise ValueError('invalid commit')
        if not isinstance(modules, dict) or not modules:
            raise ValueError('missing module dependencies')
        for module, files in modules.items():
            if not isinstance(files, list) or not files:
                raise ValueError('missing dependencies for ' + module)
            for name in files:
                if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
                        or '..' in PurePosixPath(name).parts):
                    raise ValueError('invalid repository-relative dependency')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return full('no usable coverage map: ' + str(error))
    try:
        git(root, 'merge-base', '--is-ancestor', commit, 'HEAD')
    except subprocess.CalledProcessError:
        return full('coverage map commit is not an ancestor of HEAD: ' + commit)
    except OSError as error:
        return full('cannot check coverage map ancestry: ' + str(error))
    try:
        changed = changed_files(root, base) | changed_files(root, commit)
        changed.update(set(git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')) - {''})
    except (OSError, subprocess.CalledProcessError) as error:
        return full('cannot determine changed files: ' + str(error))
    seen = {name for files in modules.values() for name in files}
    uncertain = []
    for name in sorted(changed):
        if name.endswith('.py') and name not in seen:
            uncertain.append('changed Python file was never recorded: ' + name)
        elif not name.endswith('.py') and name.startswith(('scripts/', 'packages/')):
            uncertain.append('changed non-Python file under scripts/ or packages/: ' + name)
    for module, source in sources.items():
        if module not in modules and source not in changed:
            uncertain.append('module missing from coverage map: ' + module)
    if uncertain:
        return {module: ['full run: ' + reason for reason in uncertain] for module in sources}, uncertain
    selected = {}
    for module, source in sorted(sources.items()):
        reasons = []
        if source in changed:
            reasons.append('changed or new test module: ' + source)
        reasons += ['executed changed file: ' + name for name in sorted(changed & set(modules.get(module, [])))]
        if reasons:
            selected[module] = reasons
    return selected, []


class CoverageRecorder:
    """Keep each worker's subprocess data together, then union split module fragments."""

    def __init__(self, root, directory, modules):
        # Ordinary runs never import or start coverage.
        import coverage
        self.coverage = coverage
        self.root, self.directory = root.resolve(), directory / 'coverage'
        self.directory.mkdir()
        self.dependencies = {module: set() for module in modules}
        self.bootstrap = self.directory / 'worker.py'
        self.bootstrap.write_text(
            'import sys\n' + f'sys.path.insert(0, {str(root / "scripts")!r})\n'
            'from test_worker import main\nraise SystemExit(main())\n')

    def command(self, folder, names):
        directory = self.directory / folder.name
        directory.mkdir()
        config = directory / 'coverage.ini'
        config.write_text('[run]\npatch = subprocess\nparallel = true\n'
                          f'data_file = {directory / ".coverage"}\ninclude = {self.root}/*\n')
        return [sys.executable, '-B', '-m', 'coverage', 'run', '--rcfile', str(config),
                str(self.bootstrap), *names]

    def collect(self, folder, module):
        directory = self.directory / folder.name
        measured = self.coverage.Coverage(data_file=str(directory / '.coverage'), config_file=False)
        measured.combine(data_paths=[str(directory)], strict=True)
        data = measured.get_data()
        for filename in data.measured_files():
            try:
                relative = Path(filename).resolve().relative_to(self.root).as_posix()
            except ValueError:
                continue
            if data.lines(filename):
                self.dependencies[module].add(relative)

    def write(self, commit, seconds):
        missing = [module for module, files in self.dependencies.items() if not files]
        if missing:
            raise ValueError('no recorded files for: ' + ', '.join(sorted(missing)))
        destination = self.root / CACHE
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps({'commit': commit, 'recording_seconds': seconds,
                                        'modules': {key: sorted(files) for key, files in self.dependencies.items()}},
                                       indent=1, sort_keys=True) + '\n')
        temporary.replace(destination)
        return destination
