"""TF-27c: later-generation approved-edit admission witnesses."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_approved_edits as fixtures
from test_framework import requirements


@requirements('TF-27', 'TF-23', why_not_scenario='Two committed admission generations exercise the real rebinder, replay and gate in disposable Git repositories.')
class ApprovedEditsLaterBinding(unittest.TestCase):
    policy = fixtures.ApprovedEdits.policy
    commit = fixtures.ApprovedEditsR1.commit
    write_json = fixtures.ApprovedEditsR1.write_json
    snapshot = fixtures.ApprovedEditsR1.snapshot
    fixture = fixtures.ApprovedEditsR1.fixture
    migration = fixtures.ApprovedEditsMigration.migration

    def retired_first_generation(self, root, p, *, migrated=True, legacy='test-legacy.json'):
        import hashlib
        first_manifest, seed, active, retired = self.migration(
            root, p, None if migrated else 'undecorated', outside_manifest=True)
        if not migrated:
            # Effective eligibility alone cannot retire an identity still in
            # either raw list. Leave its changed proof unbound in generation 1.
            for name in ('test-legacy.json', 'test-legacy-0850.json'):
                self.write_json(root / 'scripts' / name, {
                    **active, 'tests': {n: r['fingerprint'] for n, r in seed.items()
                                      if n != retired or name == legacy}})
            self.commit(root, 'keep changed unbound identity in legacy lists')
            self.enterContext(mock.patch.object(p, 'active_ids', return_value=set(seed) - {retired}))
        first = p.build(root, [first_manifest])
        self.assertNotIn(retired, first['tests'])
        self.assertEqual({'root:test_case.Case.test_active'}, set(first['tests']))
        self.write_json(root / p.BINDINGS / '1.json', first)
        introduction = self.commit(root, 'first binding after normal migration' if migrated else
                                   'first binding with still-listed unbound proof')
        self.assertEqual(first['tests'], p.checked_bindings(root))
        path = root / 'tests/test_other.py'
        path.write_text(path.read_text().replace('self.assertTrue(True)', 'self.assertTrue(False)'))
        edit = self.commit(root, 'second generation edits another source')
        manifest = dict(schema=1, decision='2', transitions=[dict(file='tests/test_other.py',
            before_commit=introduction, edit_commit=edit, edits=[dict(line=4,
            old='        self.assertTrue(True)\n', new='        self.assertTrue(False)\n')])])
        self.write_json(root / p.MANIFESTS / '2.json', manifest)
        approval = p.fw.decode_json((root / p.APPROVALS).read_text())
        approval['decisions']['2'] = dict(binding='0',
            manifest_sha256=hashlib.sha256((root / p.MANIFESTS / '2.json').read_bytes()).hexdigest())
        self.write_json(root / p.APPROVALS, approval)
        self.commit(root, 'second owner manifest and approval')
        return seed, active, retired, first

    def test_second_generation_excludes_previously_migrated_unbound_proof(self):
        import parallel_tests as runner
        import test_owned_baseline as owned
        import rebind_approved_edits as live_policy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            seed, active, retired, first = self.retired_first_generation(root, p)
            first_bytes = (root / p.BINDINGS / '1.json').read_bytes()
            status, diagnostic = self.command(root, p)
            self.assertEqual(0, status, 'TF-27c-2 normally migrated proof blocked generation 2: ' + diagnostic)
            second = p.fw.decode_json((root / p.BINDINGS / '0.json').read_text())
            self.assertEqual({'root:test_other.Other.test_unrelated'}, set(second['tests']))
            self.assertNotIn(retired, second['tests'])
            self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())
            self.commit(root, 'second binding introduction')
            self.assertEqual({**first['tests'], **second['tests']}, p.checked_bindings(root))
            current = p.inventory(root, root, seed)
            discovered = {module: {n: r for n, r in current.items() if n.rsplit('.', 2)[0] == module}
                          for module in ('root:test_case', 'root:test_other')}
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.object(runner, 'SUITES', {'root': root}), \
                 mock.patch.object(runner, 'inventory', return_value=discovered), \
                 mock.patch.object(runner, 'legacy_versions', side_effect=lambda *args: (copy.deepcopy(active), copy.deepcopy(active), [])), \
                 mock.patch.object(runner.framework, 'validate_sources', return_value=[]), \
                 mock.patch.object(owned, 'checked', return_value=seed), \
                 mock.patch.object(live_policy, 'checked_bindings', side_effect=p.checked_bindings):
                self.assertEqual([], runner.metadata()[-1])
                catalogue = root / 'scripts/test-requirements.json'
                catalogue_bytes = catalogue.read_bytes()
                self.write_json(catalogue, {})
                self.assertEqual([retired + ": unknown requirement 'TF-27'"], runner.metadata()[-1])
                catalogue.write_bytes(catalogue_bytes)
            # A proof retired only after these introductions retains the
            # independent current-identity and normal-migration checks.
            later = 'root:test_case.Case.test_active'
            for name in ('test-legacy.json', 'test-legacy-0850.json'):
                self.write_json(root / 'scripts' / name, {
                    **active, 'tests': {n: f for n, f in active['tests'].items() if n != later}})
            self.commit(root, 'later undecorated retirement of a bound proof')
            with self.assertRaisesRegex(ValueError, later + ': retired bound test has no requirement'):
                p.checked_bindings(root)

    def test_second_generation_still_checks_unbound_legacy_proof(self):
        for legacy in ('test-legacy.json', 'test-legacy-0850.json'):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy()
                _, _, unbound, _ = self.retired_first_generation(root, p, migrated=False, legacy=legacy)
                for name in ('test-legacy.json', 'test-legacy-0850.json'):
                    listed = p.fw.decode_json((root / 'scripts' / name).read_text())['tests']
                    self.assertEqual(name == legacy, unbound in listed,
                                     'TF-27c still-listed fixture must exercise each legacy list alone')
                self.assertIn(unbound, p.listed_ids(root))
                status, diagnostic = self.command(root, p)
                self.assertEqual(1, status)
                self.assertIn(unbound + ': trusted old proof differs from admitted baseline', diagnostic)
                self.assertFalse((root / p.BINDINGS / '0.json').exists())

    def test_sibling_binding_introductions_are_not_linear(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            _, first, _ = self.fixture(root, p)
            first_bytes = (root / p.BINDINGS / '1.json').read_bytes()
            first_manifest = p.read_manifests(root)[0]
            subprocess.run(['git', 'reset', '--soft', 'HEAD^'], cwd=root, check=True)
            (root / p.BINDINGS / '1.json').unlink()
            common = self.commit(root, 'common source and first owner manifest')
            (root / p.BINDINGS / '1.json').write_bytes(first_bytes)
            first_introduction = self.commit(root, 'first binding on sibling branch')
            self.assertEqual(first['tests'], p.checked_bindings(root))
            subprocess.run(['git', 'checkout', '-qb', 'second', common], cwd=root, check=True)
            path = root / 'tests/test_other.py'
            path.write_text(path.read_text().replace('self.assertTrue(True)', 'self.assertTrue(False)'))
            edit = self.commit(root, 'second source on independent branch')
            manifest = dict(schema=1, decision='2', transitions=[dict(file='tests/test_other.py',
                before_commit=common, edit_commit=edit, edits=[dict(line=4,
                old='        self.assertTrue(True)\n', new='        self.assertTrue(False)\n')])])
            self.write_json(root / p.MANIFESTS / '2.json', manifest)
            approval = p.fw.decode_json((root / p.APPROVALS).read_text())
            approval['decisions']['2'] = dict(binding='0',
                manifest_sha256=hashlib.sha256((root / p.MANIFESTS / '2.json').read_bytes()).hexdigest())
            self.write_json(root / p.APPROVALS, approval)
            self.commit(root, 'second owner manifest')
            second = p.build(root, [manifest], prior=[first_manifest], admitted=first['tests'])
            self.write_json(root / p.BINDINGS / '0.json', second)
            second_introduction = self.commit(root, 'second binding on sibling branch')
            subprocess.run(['git', 'checkout', '-q', '--detach', first_introduction], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=TF27', '-c', 'user.email=tf27@example.invalid',
                            '-c', 'core.hooksPath=/dev/null', 'merge', '-q', '--no-edit', '--no-gpg-sign',
                            second_introduction], cwd=root, check=True)
            order = p.git(root, 'rev-list', '--reverse', '--topo-order', 'HEAD').decode().split()
            self.assertLess(order.index(first_introduction), order.index(second_introduction))
            self.assertEqual(1, subprocess.run(['git', 'merge-base', '--is-ancestor',
                first_introduction, second_introduction], cwd=root).returncode)
            with self.assertRaisesRegex(ValueError, 'approved-edit generations are not linear'):
                p.checked_bindings(root)

    def test_introduction_checks_earlier_only_source_even_after_restoration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            _, _, _, first = self.retired_first_generation(root, p)
            status, diagnostic = self.command(root, p)
            self.assertEqual(0, status, diagnostic)
            second = p.fw.decode_json((root / p.BINDINGS / '0.json').read_text())
            self.assertEqual({'tests/test_other.py'}, set(second['after_sources']))
            self.commit(root, 'valid second introduction')
            expected = {**first['tests'], **second['tests']}
            self.assertEqual(expected, p.checked_bindings(root))
            subprocess.run(['git', 'reset', '--soft', 'HEAD^'], cwd=root, check=True)
            earlier = root / 'tests/fixture.py'; admitted = earlier.read_bytes()
            earlier.write_bytes(b'VALUE = 99\n')
            self.commit(root, 'second introduction hides unlisted earlier-only source change')
            earlier.write_bytes(admitted)
            self.commit(root, 'restore earlier source after invalid introduction')
            with self.assertRaisesRegex(ValueError, 'introduction source/algorithm differs from replay'):
                p.checked_bindings(root)

    def test_full_history_refuses_merge_hidden_independent_document_addition(self):
        for directory in ('MANIFESTS', 'BINDINGS'):
            with self.subTest(document=directory), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy()
                _, first, _ = self.fixture(root, p)
                self.assertEqual(first['tests'], p.checked_bindings(root))
                original = p.revision(root, 'HEAD')
                common = p.revision(root, 'HEAD^')
                name = getattr(p, directory) + '/1.json'
                content = (root / name).read_bytes()
                subprocess.run(['git', 'checkout', '-qb', 'independent', common], cwd=root, check=True)
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(content + b'\n')
                hidden = self.commit(root, 'conflicting independent introduction')
                subprocess.run(['git', 'checkout', '-q', '--detach', original], cwd=root, check=True)
                merge = subprocess.run(['git', '-c', 'user.name=TF27', '-c', 'user.email=tf27@example.invalid',
                                        '-c', 'core.hooksPath=/dev/null', 'merge', '--no-commit',
                                        '--no-ff', hidden], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(1, merge.returncode)
                (root / name).write_bytes(content)
                self.commit(root, 'merge keeps original bytes and hides the other addition')
                simplified = p.git(root, 'log', '--format=%H', 'HEAD', '--', name).decode().split()
                self.assertNotIn(hidden, simplified)
                with self.assertRaisesRegex(ValueError, 'conflicting independent introductions'):
                    p.checked_bindings(root)

    def test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            snapshot = self.snapshot
            absent = 'codex:test_extra.Extra.test_absent'
            def with_untouched_suite(repo, source=None):
                ids = snapshot(repo, source)
                package = repo / 'packages/codex-agents'
                (package / 'tests').mkdir(parents=True)
                (package / 'src').mkdir()
                (package / 'tests/test_extra.py').write_text(
                    'import unittest\nclass Extra(unittest.TestCase):\n    def test_absent(self):\n'
                    '        self.assertEqual(1, 1)\n')
                for name in ('test-layers.json', 'test-stages.json'):
                    mapping = p.fw.decode_json((repo / 'scripts' / name).read_text())
                    self.write_json(repo / 'scripts' / name, {**mapping, 'codex:test_extra': 'unit'})
                for name in ('test-legacy.json', 'test-legacy-0850.json'):
                    self.write_json(repo / 'scripts' / name,
                                    {'baseline': 'fixture', 'tests': dict.fromkeys([*ids, absent], '')})
                return {**ids, absent: {}}
            with mock.patch.object(self, 'snapshot', side_effect=with_untouched_suite):
                seed, _, _ = self.fixture(root, p)
            self.assertIn(absent, seed)
            subprocess.run(['git', 'reset', '--soft', 'HEAD^'], cwd=root, check=True)
            for name in ('test-legacy.json', 'test-legacy-0850.json'):
                path = root / 'scripts' / name
                data = p.fw.decode_json(path.read_text()); del data['tests'][absent]
                self.write_json(path, data)
            introduction = self.commit(root, 'first introduction never inspects codex suite')
            self.assertNotIn(absent, p.listed_ids(root))
            self.assertTrue(p.checked_bindings(root))
            path = root / 'packages/codex-agents/tests/test_extra.py'
            path.write_text(path.read_text().replace('self.assertEqual(1, 1)', 'self.assertEqual(2, 1)'))
            edit = self.commit(root, 'first approved edit in previously untouched suite')
            manifest = dict(schema=1, decision='2', transitions=[dict(
                file='packages/codex-agents/tests/test_extra.py', before_commit=introduction,
                edit_commit=edit, edits=[dict(line=4, old='        self.assertEqual(1, 1)\n',
                                             new='        self.assertEqual(2, 1)\n')])])
            self.write_json(root / p.MANIFESTS / '2.json', manifest)
            approval = p.fw.decode_json((root / p.APPROVALS).read_text())
            approval['decisions']['2'] = dict(binding='0',
                manifest_sha256=hashlib.sha256((root / p.MANIFESTS / '2.json').read_bytes()).hexdigest())
            self.write_json(root / p.APPROVALS, approval)
            self.commit(root, 'second manifest cannot grandfather an unverified migration')
            status, diagnostic = self.command(root, p)
            self.assertEqual(1, status, 'TF-27c absent undecorated ID was treated as migrated')
            self.assertIn(absent + ': retired changed legacy proof has no requirement', diagnostic)
            self.assertFalse((root / p.BINDINGS / '0.json').exists())

    def test_sole_binding_dirty_source_precedes_proof_rebuild(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            seed, first, _ = self.fixture(root, p)
            self.assertEqual(first['tests'], p.checked_bindings(root))
            path = root / 'tests/test_case.py'
            path.write_bytes(path.read_bytes() + b'\nUNLISTED = 1\n')
            stale = copy.deepcopy(seed)
            stale['root:test_case.Case.test_changed']['fingerprint'] = 'stale'
            with mock.patch.object(p, 'seed_records', return_value=stale), \
                 self.assertRaisesRegex(ValueError, '^unlisted change in approved-edit files$'):
                p.checked_bindings(root)

    def command(self, root, p, binding='0'):
        import contextlib
        import io
        stderr = io.StringIO()
        status = 0
        with mock.patch.object(p, '__file__', str(root / 'scripts/rebind_approved_edits.py')), \
             mock.patch.object(sys, 'argv', ['rebind_approved_edits.py', '--binding', binding]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            try:
                p.main()
            except SystemExit as error:
                status = error.code
        return status, stderr.getvalue()

    def second_manifest(self, root, p, *, wrong_binding=False):
        import hashlib
        seed, first, _ = self.fixture(root, p)
        first_bytes = (root / p.BINDINGS / '1.json').read_bytes()
        path = root / 'tests/test_case.py'
        parent = p.revision(root, 'HEAD')
        path.write_text(path.read_text().replace('self.assertEqual(2, 1)', 'self.assertEqual(3, 1)'))
        edit = self.commit(root, 'second approved edit to the same source and proof')
        manifest = dict(schema=1, decision='2', transitions=[dict(file='tests/test_case.py',
            before_commit=parent, edit_commit=edit, edits=[dict(line=4,
            old='        self.assertEqual(2, 1)\n', new='        self.assertEqual(3, 1)\n')])])
        self.write_json(root / p.MANIFESTS / '2.json', manifest)
        approval = p.fw.decode_json((root / p.APPROVALS).read_text())
        approval['decisions']['2'] = dict(binding='99' if wrong_binding else '0',
            manifest_sha256=hashlib.sha256((root / p.MANIFESTS / '2.json').read_bytes()).hexdigest())
        self.write_json(root / p.APPROVALS, approval)
        self.commit(root, 'second owner manifest and authority')
        return seed, first, first_bytes

    def two_generations(self, root, p):
        seed, first, first_bytes = self.second_manifest(root, p)
        status, diagnostic = self.command(root, p)
        self.assertEqual(0, status, 'TF-27c second generation must bind only decision 2: ' + diagnostic)
        second = p.fw.decode_json((root / p.BINDINGS / '0.json').read_text())
        self.assertEqual({'2'}, set(second['manifests']))
        self.assertEqual(first['after_sources'], second['before_sources'])
        self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())
        self.commit(root, 'second binding introduction (name sorts before first)')
        current = p.inventory(root, root, seed)
        expected = {name: {f: current[name][f] for f in p.FIELDS} for name in second['tests']}
        self.assertTrue(expected, 'TF-27c positive control must change an existing proof')
        self.assertEqual(expected, second['tests'])
        self.assertNotEqual(first['tests'], second['tests'])
        self.assertEqual(expected, p.checked_bindings(root))
        return first, second, first_bytes

    def test_two_generations_pass_gate_and_no_covered_manifest_is_rebound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            _, second, first_bytes = self.two_generations(root, p)
            status, diagnostic = self.command(root, p, '3')
            self.assertEqual(1, status)
            self.assertIn('no new approved-edit manifest', diagnostic)
            self.assertFalse((root / p.BINDINGS / '3.json').exists())
            self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())
            # Current-source and admitted-history checks still fail closed.
            path = root / 'tests/test_case.py'; content = path.read_bytes()
            path.write_bytes(content + b'\nUNLISTED = 1\n')
            with self.assertRaisesRegex(ValueError, 'unlisted change'):
                p.checked_bindings(root)
            path.write_bytes(content)
            self.assertEqual(second['tests'], p.checked_bindings(root))
            (root / p.BINDINGS / '1.json').unlink()
            with self.assertRaisesRegex(ValueError, 'missing admitted'):
                p.checked_bindings(root)

    def test_second_generation_relisting_first_decision_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            first, second, _ = self.two_generations(root, p)
            forged = copy.deepcopy(second)
            forged['manifests'].update(first['manifests'])
            # Replace the introduction, so duplicate detection is exercised
            # independently of immutable-byte and owner-name rejection.
            subprocess.run(['git', 'reset', '--soft', 'HEAD^'], cwd=root, check=True)
            self.write_json(root / p.BINDINGS / '0.json', forged)
            self.commit(root, 'invalid second introduction repeats decision 1')
            with self.assertRaisesRegex(ValueError, 'duplicate approved-edit binding'):
                p.checked_bindings(root)

    def test_new_decision_approval_naming_wrong_binding_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            self.two_generations(root, p)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            self.second_manifest(root, p, wrong_binding=True)
            status, diagnostic = self.command(root, p)
            self.assertEqual(1, status)
            self.assertIn('2: owner approval does not authorize manifest digest and binding name', diagnostic)
            self.assertFalse((root / p.BINDINGS / '0.json').exists())

    def test_first_binding_tampering_after_second_exists_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            _, second, first_bytes = self.two_generations(root, p)
            path = root / p.BINDINGS / '1.json'
            path.write_bytes(first_bytes + b'\n')
            with self.assertRaisesRegex(ValueError, 'immutable approved-edit document'):
                p.checked_bindings(root)
            self.commit(root, 'tamper with earlier binding after second introduction')
            with self.assertRaisesRegex(ValueError, 'immutable approved-edit document'):
                p.checked_bindings(root)
            path.write_bytes(first_bytes)
            with self.assertRaisesRegex(ValueError, 'immutable approved-edit document'):
                p.checked_bindings(root)
            self.commit(root, 'restore original first binding bytes')
            self.assertEqual(second['tests'], p.checked_bindings(root))
