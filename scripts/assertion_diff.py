#!/usr/bin/env python3
"""Report assertion changes and deleted tests between two commits (stdlib only).

This is review information: it makes no judgment about whether a change is valid.
"""
from __future__ import annotations

import argparse
import ast
from difflib import SequenceMatcher
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_path(path):
    parts = Path(path).parts
    return path.endswith('.py') and 'tests' in parts


def inspect_source(source):
    """Group complete assertion expressions by lexical scope; ignore comments/strings."""
    tree = ast.parse(source)
    lines = source.splitlines()
    assertions, methods = {}, {}

    def walk(node, scope=()):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                methods['.'.join(scope)] = node.lineno
        is_assertion = isinstance(node, ast.Assert) or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith('assert'))
        if is_assertion:
            assertions.setdefault('.'.join(scope), []).append({
                'line': node.lineno,
                'text': '\n'.join(lines[node.lineno - 1:node.end_lineno]).strip(),
            })
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree)
    return assertions, methods


def compare_sources(before, after, path):
    old, old_methods = inspect_source(before)
    new, new_methods = inspect_source(after)
    report = {'path': path, 'added': [], 'removed': [], 'changed': [],
              'deleted_test_methods': [{'name': name, 'line': line} for name, line in old_methods.items()
                                       if name not in new_methods]}
    for scope in sorted(old.keys() | new.keys()):
        left, right = old.get(scope, []), new.get(scope, [])
        matcher = SequenceMatcher(a=[r['text'] for r in left], b=[r['text'] for r in right], autojunk=False)
        for kind, a, b, c, d in matcher.get_opcodes():
            if kind == 'equal':
                continue
            removed, added = left[a:b], right[c:d]
            paired = min(len(removed), len(added)) if kind == 'replace' else 0
            report['changed'].extend({'scope': scope, 'before': x, 'after': y}
                                     for x, y in zip(removed[:paired], added[:paired]))
            report['removed'].extend({'scope': scope, **r} for r in removed[paired:])
            report['added'].extend({'scope': scope, **r} for r in added[paired:])
    return report


def git(*args, root=ROOT):
    return subprocess.check_output(['git', *args], cwd=root)


def commit_report(base, head, *, root=ROOT):
    # Resolve options and symbolic refs once, then use only full commit hashes in git commands.
    base = git('rev-parse', '--verify', '--end-of-options', base + '^{commit}', root=root).decode().strip()
    head = git('rev-parse', '--verify', '--end-of-options', head + '^{commit}', root=root).decode().strip()
    paths = [p.decode() for p in git('diff', '--name-only', '--no-renames', '-z', base, head, '--', root=root).split(b'\0') if p]
    reports = []
    for path in sorted(filter(test_path, paths)):
        sources = []
        for revision in (base, head):
            # Absence is expected for a newly added or deleted file; other git errors propagate.
            exists = git('ls-tree', '-z', revision, '--', path, root=root)
            sources.append(git('show', revision + ':' + path, root=root).decode() if exists else '')
        try:
            report = compare_sources(*sources, path)
        except SyntaxError as error:
            report = {'path': path, 'parse_error': str(error)}
        if any(value for key, value in report.items() if key != 'path'):
            reports.append(report)
    return {'base': base, 'head': head, 'files': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base')
    parser.add_argument('head')
    args = parser.parse_args(argv)
    print(json.dumps(commit_report(args.base, args.head), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
