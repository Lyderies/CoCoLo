"""O0c guard: no count-based limit enters the runtime without saying how what it counts ends.

Lifetime caps (a limit on things nothing ever removes) kept coming back: 0.3.4 removed seven, then
account_open_requests blocked every child of a project (ANALYSIS-0.4.0-lifetime-caps.md). Every place
the runtime counts against a limit is listed in lifetime_caps.json, keyed by file and function. The
places audited on 2026-11-21 are the baseline. A new place, or more limit checks in a listed function,
fails this test until it is listed with how the counted things end and the test that proves a run past
the limit still works (the 0.3.4 ResultLimitTests shape). Decision 382 classifies the broad scan:
every entry needs kind and ends; count/bytes/time need past-limit tests, while
validation/read_only/precondition need ends only. Test-name existence is a static check, not
a substitute for reviewing the bound test's real past-limit stimulus.
"""

from __future__ import annotations

import ast
import collections
import json
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
RUNTIME = PACKAGE / "native-package" / ".codex" / "orchestration" / "codex_agents"
REGISTRY = Path(__file__).with_name("lifetime_caps.json")
TESTS = Path(__file__).parent

# A SQL bound read past its limit, a row count, a comparison with a MAX_ constant, or a size check.
CAP = re.compile(r"LIMIT \?|COUNT\(|[<>]=? *(?:self\._?|_?limits\.)*(?:MAX_[A-Z_]+|max_\w+|[A-Z][A-Z0-9_]{3,}\b)|len\([^)]*\) *>=")


def cap_sites(source: str, name: str) -> collections.Counter:
    """(file, innermost function) -> number of lines in it that check a count against a limit."""
    tree = ast.parse(source)
    spans = [(node.lineno, node.end_lineno, node.name) for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    sites = collections.Counter()
    for number, line in enumerate(source.splitlines(), 1):
        if CAP.search(line):
            inside = [span for span in spans if span[0] <= number <= span[1]]
            function = min(inside, key=lambda span: span[1] - span[0])[2] if inside else "<module>"
            sites[f"{name}::{function}"] += 1
    return sites


def runtime_sites() -> collections.Counter:
    sites = collections.Counter()
    for path in sorted(RUNTIME.glob("*.py")):
        sites.update(cap_sites(path.read_text(encoding="utf-8"), path.name))
    return sites


KINDS = frozenset(('count', 'bytes', 'time', 'validation', 'read_only', 'precondition'))
BOUNDED_KINDS = frozenset(('count', 'bytes', 'time'))


def problems(sites: collections.Counter, registry: dict, test_names: set[str]) -> list[str]:
    found = []
    for key, entry in sorted(registry.items()):
        kind = entry.get('kind')
        if not isinstance(kind, str) or kind not in KINDS:
            found.append(f"{key}: missing or invalid 'kind'")
        fields = ('ends', 'test') if isinstance(kind, str) and kind in BOUNDED_KINDS else ('ends',)
        for field in fields:
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                found.append(f"{key}: missing non-empty '{field}'")
        if isinstance(entry.get('test'), str) and entry['test'].strip() and entry['test'] not in test_names:
            found.append(f"{key}: its test {entry['test']} does not exist")
        count = sites[key]
        if count != entry['checks']:
            found.append(f"{key}: {count} limit checks, {entry['checks']} listed; describe the change")
    for key, count in sorted(sites.items()):
        entry = registry.get(key)
        if entry is None:
            found.append(f"{key}: a new count-based limit; list it in lifetime_caps.json with 'ends' "
                         "(how what it counts goes away) and 'test' (a past-the-limit test)")
    return found


def test_names() -> set[str]:
    names = set()
    for path in TESTS.glob("test_*.py"):
        names.update(re.findall(r"^\s*def (test_\w+)", path.read_text(encoding="utf-8"), re.M))
    return names


@__import__('test_framework').requirements('S15-O0C-GUARD', why_not_scenario='The count-site and registry guard is a static AST/JSON contract; malformed and missing metadata are tested directly.')
class LifetimeCapGuardTests(unittest.TestCase):
    def test_every_count_based_limit_in_the_runtime_is_listed_with_its_end(self):
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))["sites"]
        self.assertEqual([], problems(runtime_sites(), registry, test_names()))

    def test_a_new_limit_is_caught(self):
        source = ("def take(rows):\n"
                  "    if len(rows) >= MAX_ROWS:\n"
                  "        raise Full()\n"
                  "    db.execute('SELECT 1 FROM t WHERE p=? LIMIT ?', (p, MAX_ROWS + 1))\n")
        sites = cap_sites(source, "new_module.py")
        self.assertEqual({"new_module.py::take": 2}, dict(sites))
        found = problems(sites, {}, set())
        self.assertEqual(1, len(found))
        self.assertIn("a new count-based limit", found[0])

    def test_a_listed_function_gaining_a_limit_is_caught(self):
        sites = collections.Counter({"m.py::take": 2})
        self.assertIn("2 limit checks, 1 listed", problems(sites, {"m.py::take": {"checks": 1}}, set())[-1])

    def test_overlisted_and_zero_site_check_counts_are_caught(self):
        entry = {'checks': 2, 'kind': 'count', 'ends': 'released', 'test': 'test_past_limit'}
        for actual in (0, 1):
            with self.subTest(actual=actual):
                found = problems(collections.Counter({'m.py::take': actual}),
                                 {'m.py::take': entry}, {'test_past_limit'})
                self.assertTrue(any(f'{actual} limit checks, 2 listed' in issue for issue in found))

    def test_every_registry_entry_requires_nonempty_end_and_test_even_if_not_a_current_site(self):
        for field in ('ends', 'test'):
            for bad in (None, '', '  ', 0, [], {}):
                with self.subTest(field=field, value=bad):
                    entry = {'checks': 0, 'kind': 'count', 'ends': 'freed after closure', 'test': 'test_past_limit'}
                    entry[field] = bad
                    self.assertIn("missing non-empty '" + field + "'",
                                  problems(collections.Counter(), {'stale.py::take': entry}, {'test_past_limit'})[0])
            entry = {'checks': 0, 'kind': 'count', 'ends': 'freed after closure', 'test': 'test_past_limit'}
            del entry[field]
            self.assertIn("missing non-empty '" + field + "'",
                          problems(collections.Counter(), {'stale.py::take': entry}, {'test_past_limit'})[0])

    def test_kinds_require_bounds_only_for_count_bytes_and_time(self):
        for kind in ('count', 'bytes', 'time', 'validation', 'read_only', 'precondition'):
            with self.subTest(kind=kind):
                entry = {'checks': 0, 'kind': kind, 'ends': 'temporary scope ends on return'}
                found = problems(collections.Counter(), {'m.py::take': entry}, set())
                self.assertEqual(kind in ('count', 'bytes', 'time'), bool(found))
                entry['test'] = 'test_past_limit'
                self.assertEqual([], problems(collections.Counter(), {'m.py::take': entry}, {'test_past_limit'}))
        for kind in (None, '', 'exempt', [], 1):
            with self.subTest(invalid_kind=kind):
                entry = {'checks': 0, 'kind': kind, 'ends': 'return', 'test': 'test_past_limit'}
                self.assertTrue(any('kind' in issue for issue in
                                    problems(collections.Counter(), {'m.py::take': entry}, {'test_past_limit'})))

    def test_a_listed_test_must_exist(self):
        sites = collections.Counter({"m.py::take": 1})
        entry = {"m.py::take": {"checks": 1, "kind": "count", "ends": "purged at close", "test": "test_missing_one"}}
        self.assertIn("does not exist", problems(sites, entry, set())[0])
        self.assertEqual([], problems(sites, entry, {"test_missing_one"}))


if __name__ == "__main__":
    unittest.main()
