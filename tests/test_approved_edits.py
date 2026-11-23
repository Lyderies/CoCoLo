"""TF-27: exact source admission and immutable, shrink-only proof overlays."""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements


@requirements('TF-27', 'TF-23', why_not_scenario='Disposable Git history and byte-exact transitions isolate the approved-edit admission boundary.')
class ApprovedEdits(unittest.TestCase):
    def policy(self):
        path = ROOT / 'scripts/rebind_approved_edits.py'
        self.assertTrue(path.exists(), 'TF-27 missing approved-edit validator')
        spec = importlib.util.spec_from_file_location('approved_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exact_transition_and_extra_unlisted_edits(self):
        p = self.policy()
        old = b'class Case:\n    value = 18\n'
        edits = [{'line': 2, 'old': '    value = 18\n', 'new': '    value = 20\n'}]
        new = b'class Case:\n    value = 20\n'
        self.assertEqual(new, p.edit_bytes(old, edits))
        for extra in (b'\nEXTRA = 1\n', b'\ndef helper(): return 1\n', b'    assert False\n'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'unlisted change'):
                p.exact_current({'test.py': new}, {'test.py': new + extra})
        self.assertIsNone(p.exact_current({'test.py': new}, {'test.py': new}))

    def test_wrong_text_line_endings_and_stale_base(self):
        p = self.policy()
        edits = [{'line': 2, 'old': 'old\n', 'new': 'new\n'}]
        self.assertEqual(b'header\nnew\n', p.edit_bytes(b'header\nold\n', edits))
        for old in (b'header\nother\n', b'header\nold\r\n'):
            with self.assertRaisesRegex(ValueError, 'old line|stale'):
                p.edit_bytes(old, edits)
        for new in (b'header\nNEW\n', b'header\nnew\r\n'):
            with self.assertRaisesRegex(ValueError, 'unlisted change'):
                p.exact_current({'f': b'header\nnew\n'}, {'f': new})
        with self.assertRaisesRegex(ValueError, 'stale base'):
            p.exact_base({'f': b'admitted\n'}, {'f': b'candidate\n'})

    def test_only_changed_active_proofs_rebind_all_three_kinds(self):
        p = self.policy()
        row = dict(fingerprint='i', dependencies='d', dependency_values='v')
        seed = {name: dict(row) for name in ('changed', 'untouched')}
        before = {name: {**r, 'requirements': []} for name, r in seed.items()}
        after = copy.deepcopy(before)
        after['changed'].update(fingerprint='I', dependencies='D', dependency_values='V')
        after['new_tagged'] = {**row, 'requirements': ['TF-27']}
        self.assertEqual({'changed': dict(fingerprint='I', dependencies='D', dependency_values='V')},
                         p.changed_proofs(before, after, seed, candidate=after))
        self.assertEqual({}, p.changed_proofs(before, before, seed, candidate=before))
        for field in ('fingerprint', 'dependencies', 'dependency_values'):
            bad = copy.deepcopy(before); bad['changed'][field] = 'stale'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'trusted old proof'):
                p.changed_proofs(bad, after, seed, candidate=after)

    def test_renamed_restored_and_unresolved_proofs_are_refused(self):
        p = self.policy()
        row = dict(fingerprint='i', dependencies='d', dependency_values='v', requirements=[])
        seed = {'kept': row}; before = copy.deepcopy(seed)
        self.assertEqual({}, p.changed_proofs(before, before, seed, candidate=before))
        for after in ({'renamed': row}, {**before, 'restored': row}, {'kept': {**row, 'dependency_values': None}}):
            with self.subTest(after=after), self.assertRaisesRegex(ValueError, 'identity|unresolved|new untagged'):
                p.changed_proofs(before, after, seed, candidate=after)

    def test_unrelated_legacy_proof_still_flags_and_retired_stays_retired(self):
        p = self.policy()
        import test_framework as fw
        versions = [{'baseline': 'b', 'tests': {'kept': 'old', 'unrelated': 'same'}} for _ in range(3)]
        p.apply_fingerprints(versions, {'kept': {'fingerprint': 'new'}, 'retired': {'fingerprint': 'x'}})
        self.assertEqual({'kept': 'new', 'unrelated': 'same'}, versions[0]['tests'])
        previous = {'baseline': 'b', 'tests': {'kept': 'old'}}
        self.assertEqual({'kept'}, p.shrinking_ids(previous, previous))
        self.assertEqual(set(), p.shrinking_ids({'baseline': 'b', 'tests': {}}, previous))
        with self.assertRaisesRegex(ValueError, 'restored an ID'):
            p.shrinking_ids({'baseline': 'b', 'tests': {'kept': 'old', 'retired': 'x'}}, previous)
        records = {'kept': dict(fingerprint='new', requirements=[], rationale=''),
                   'unrelated': dict(fingerprint='same', requirements=[], rationale='')}
        args = dict(inventory={'m': records}, stages={'m': 'unit'}, layers={'m': 'unit'}, catalogue={},
                    legacy=versions[0], ceiling=versions[1], previous=versions[2])
        self.assertEqual([], fw.validate_metadata(**args))
        records['unrelated']['fingerprint'] = 'unlisted'
        self.assertIn('unrelated: legacy implementation changed; tag it', fw.validate_metadata(**args))

    def test_immutable_introduction_tampering_and_overwrite(self):
        p = self.policy()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            path = root / 'binding.json'; data = {'schema': 1, 'tests': {'kept': {'fingerprint': 'good'}}}
            p.write_new(path, data)
            with self.assertRaisesRegex(ValueError, 'uncommitted'):
                p.immutable(root, path)
            subprocess.run(['git', 'add', 'binding.json'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=TF27', '-c', 'user.email=tf27@example.invalid',
                            '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture', '--no-gpg-sign'], cwd=root, check=True)
            admitted, introduction = p.immutable(root, path)
            self.assertEqual(data, admitted)
            path.write_text(path.read_text().replace('good', 'evil'))
            with self.assertRaisesRegex(ValueError, 'immutable'):
                p.immutable(root, path)
            # Committing a later forgery does not change the first introduction.
            subprocess.run(['git', 'add', 'binding.json'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=TF27', '-c', 'user.email=tf27@example.invalid',
                            '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'forgery', '--no-gpg-sign'], cwd=root, check=True)
            with self.assertRaisesRegex(ValueError, 'immutable'):
                p.immutable(root, path)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                p.write_new(path, data)

    def test_recorded_owner_transitions_match_current_bytes(self):
        p = self.policy()
        manifests = p.read_manifests(ROOT, require_committed=False)
        self.assertEqual({'107', '109', '112', '114.1', '119'}, {m['decision'] for m in manifests})
        before, after = p.sources(ROOT, manifests)
        p.exact_current(after, {name: (ROOT / name).read_bytes() for name in after})
        self.assertNotIn('packages/codex-agents/tests/test_hold_lifecycle.py', after)
        self.assertNotIn('packages/codex-agents/tests/test_refusal_steps.py', after)
        for manifest in manifests:
            bad = copy.deepcopy(manifest)
            bad['transitions'][0]['before_commit'] = p.SOURCE_REVISION
            with self.subTest(decision=manifest['decision']), self.assertRaisesRegex(ValueError, 'stale base|old line|historical'):
                p.sources(ROOT, [bad])


@requirements('TF-27', 'TF-23', why_not_scenario='Tiny committed repositories exercise owner authority, replay discovery and the real metadata gate without executing product tests.')
class ApprovedEditsR1(unittest.TestCase):
    policy = ApprovedEdits.policy

    def commit(self, root, message='fixture'):
        subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
        subprocess.run(['git', '-c', 'user.name=TF27', '-c', 'user.email=tf27@example.invalid',
                        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', message, '--no-gpg-sign'], cwd=root, check=True)
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()

    def write_json(self, path, value):
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')

    def snapshot(self, root, source=None):
        import shutil
        (root / 'scripts').mkdir(exist_ok=True)
        (root / 'tests').mkdir(exist_ok=True)
        for name in ('test_framework.py', 'test_worker.py', 'test_profiles.py'):
            shutil.copyfile(ROOT / 'scripts' / name, root / 'scripts' / name)
        (root / 'tests/test_case.py').write_text(source or
            'import unittest\nclass Case(unittest.TestCase):\n    def test_changed(self):\n        self.assertEqual(1, 1)\n')
        (root / 'tests/test_other.py').write_text(
            'import unittest\nclass Other(unittest.TestCase):\n    def test_unrelated(self):\n        self.assertTrue(True)\n')
        for name, value in (('test-layers.json', 'unit'), ('test-stages.json', 'unit')):
            self.write_json(root / 'scripts' / name, {'root:test_case': value, 'root:test_other': value})
        ids = {'root:test_case.Case.test_changed': {}, 'root:test_other.Other.test_unrelated': {}}
        for name in ('test-legacy.json', 'test-legacy-0850.json'):
            self.write_json(root / 'scripts' / name, {'baseline': 'fixture', 'tests': dict.fromkeys(ids, '')})
        return ids

    def fixture(self, root, p, invalid=None):
        """Only immutable baseline inputs are substituted; replay and Git checks are real."""
        import hashlib
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        ids = self.snapshot(root)
        seed = p.inventory(root, root, ids)
        baseline = {'baseline': 'fixture', 'tests': {n: r['fingerprint'] for n, r in seed.items()}}
        for name, data in {
            'test-legacy.json': baseline, 'test-legacy-ceiling.json': baseline,
            'test-legacy-dependencies.json': {n: r['dependencies'] for n, r in seed.items()},
            'test-legacy-captured-values.json': {n: {'fingerprint': r['dependency_values']} for n, r in seed.items()},
            'test-requirements.json': {}, 'test-entrypoints.json': {},
        }.items():
            self.write_json(root / 'scripts' / name, data)
        base = self.commit(root, 'trusted source')
        path = root / 'tests/test_case.py'; old = path.read_text()
        new = old.replace('self.assertEqual(1, 1)', 'self.assertEqual(2, 1)')
        path.write_text(new); edit = self.commit(root, 'approved edit')
        manifest = dict(schema=1, decision='1', transitions=[dict(file='tests/test_case.py',
            before_commit=base, edit_commit=edit, edits=[dict(line=4,
            old='        self.assertEqual(1, 1)\n', new='        self.assertEqual(2, 1)\n')])])
        self.write_json(root / p.MANIFESTS / '1.json', manifest)
        self.enterContext(mock.patch.object(p, 'SOURCE_REVISION', base))
        self.enterContext(mock.patch.object(p, 'seed_records', return_value=seed))
        self.enterContext(mock.patch.object(p, 'active_ids', return_value=set(seed)))
        data = p.build(root, [manifest], set(seed))
        self.write_json(root / p.BINDINGS / '1.json', data)
        digest = hashlib.sha256((root / p.MANIFESTS / '1.json').read_bytes()).hexdigest()
        self.write_json(root / p.MANIFESTS / 'APPROVALS.json', {
            'schema': 1, 'decisions': {'2' if invalid == 'decision' else '1': {
                'manifest_sha256': digest if invalid != 'owner' else '0' * 64,
                'binding': '2' if invalid == 'binding' else '1'}}})
        algorithm = root / 'scripts/test_framework.py'; algorithm_bytes = algorithm.read_bytes()
        if invalid == 'source':
            path.write_text(old)
        if invalid == 'algorithm':
            algorithm.write_bytes(algorithm_bytes + b'\n# not the binding algorithm\n')
        if invalid == 'ancestry':
            subprocess.run(['git', 'reset', '--soft', base], cwd=root, check=True)
        self.commit(root, 'binding introduction')
        if invalid in ('source', 'algorithm'):
            path.write_text(new); algorithm.write_bytes(algorithm_bytes)
            self.commit(root, 'later repair cannot authorize introduction')
        return seed, data, baseline

    def test_r1_unanchored_files_never_generate_proofs(self):
        p = self.policy()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            ids = self.snapshot(root, 'import unittest\ntry:\n    from optional import VALUE\nexcept ImportError:\n    VALUE = 1\nclass Case(unittest.TestCase):\n    def test_changed(self):\n        self.assertEqual(VALUE, 1)\n')
            seed = p.inventory(root, root, ids); base = self.commit(root)
            path = root / 'tests/optional.py'; path.write_text('VALUE = 2\n'); parent = self.commit(root)
            path.write_text('VALUE = 3\n'); edit = self.commit(root)
            manifest = dict(schema=1, decision='1', transitions=[dict(file='tests/optional.py',
                before_commit=parent, edit_commit=edit, edits=[dict(line=1, old='VALUE = 2\n', new='VALUE = 3\n')])])
            self.write_json(root / p.MANIFESTS / '1.json', manifest)
            with mock.patch.object(p, 'SOURCE_REVISION', base), mock.patch.object(p, 'seed_records', return_value=seed):
                before, after = p.sources(root, [manifest])
                self.assertEqual(b'VALUE = 3\n', after['tests/optional.py'])
                p.exact_current(after, {'tests/optional.py': path.read_bytes()})
                self.assertEqual({}, p.build(root, [manifest], set(seed))['tests'],
                                 'TF-27 unanchored file contaminated a legacy proof')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            _, data, _ = self.fixture(root, p)
            self.assertTrue(data['tests'], 'anchored positive control must rebind')
            manifests = p.read_manifests(root)
            real_git = p.git
            def broken_git(repo, *args):
                if args == ('show', p.SOURCE_REVISION + ':tests/test_case.py'):
                    raise subprocess.CalledProcessError(128, ['git', 'show'], stderr=b'object store failure')
                return real_git(repo, *args)
            with mock.patch.object(p, 'git', side_effect=broken_git), self.assertRaises(subprocess.CalledProcessError):
                p.sources(root, manifests)

    def test_r1_unchanged_stale_old_proofs_are_refused(self):
        p = self.policy()
        row = dict(fingerprint='i', dependencies='d', dependency_values='v', requirements=[])
        self.assertEqual({}, p.changed_proofs({'kept': row}, {'kept': row}, {'kept': row}, candidate={'kept': row}))
        for field in p.FIELDS:
            stale = {**row, field: 'stale'}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'trusted old proof',
                    msg='TF-27 stale unchanged old proof accepted'):
                p.changed_proofs({'kept': stale}, {'kept': stale}, {'kept': row}, candidate={'kept': row})

    def test_r1_owner_record_authenticates_first_introduction(self):
        for invalid in (None, 'owner', 'binding', 'decision'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy(); _, data, _ = self.fixture(root, p, invalid)
                if invalid:
                    with self.assertRaisesRegex(ValueError, 'owner approval', msg='TF-27 unauthorized first introduction accepted'):
                        p.checked_bindings(root)
                else:
                    self.assertEqual(data['tests'], p.checked_bindings(root))
                    approval = root / p.MANIFESTS / 'APPROVALS.json'
                    approved = approval.read_bytes()
                    approval.write_bytes(approved + b'\n')
                    with self.assertRaisesRegex(ValueError, 'owner approval record changed or uncommitted'):
                        p.checked_bindings(root)
                    approval.write_bytes(approved)
                    self.assertEqual(data['tests'], p.checked_bindings(root))

    def test_r1_introduction_requires_ancestry_sources_and_algorithm(self):
        for invalid in (None, 'source', 'ancestry', 'algorithm'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy(); _, data, _ = self.fixture(root, p, invalid)
                if invalid:
                    with self.assertRaisesRegex(ValueError, 'introduction', msg='TF-27 invalid introduction accepted: ' + invalid):
                        p.checked_bindings(root)
                else:
                    self.assertEqual(data['tests'], p.checked_bindings(root))

    def test_r1_deleted_admission_documents_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy(); _, data, _ = self.fixture(root, p)
            self.assertEqual(data['tests'], p.checked_bindings(root))
            for name in (p.BINDINGS + '/1.json', p.MANIFESTS + '/1.json'):
                path = root / name; content = path.read_bytes(); path.unlink()
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'missing admitted',
                        msg='TF-27 deleted admission silently accepted'):
                    p.checked_bindings(root)
                self.commit(root, 'deleted admission')
                with self.assertRaisesRegex(ValueError, 'missing admitted'):
                    p.checked_bindings(root)
                path.write_bytes(content); self.commit(root, 'restore admission')
                self.assertEqual(data['tests'], p.checked_bindings(root))

    def test_r1_replay_uses_guarded_discovery_and_rejects_duplicate_ids(self):
        p = self.policy()
        for fault in (None, 'duplicate', 'socket'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); ids = self.snapshot(root)
                path = root / 'tests/test_case.py'
                if fault is None:
                    with path.open('a') as stream:
                        stream.write('\n    def test_filtered(self):\n        self.fail("load_tests must filter this")\n'
                                     '\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Case("test_changed")])\n')
                if fault == 'duplicate':
                    with path.open('a') as stream:
                        stream.write('\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Case("test_changed"), Case("test_changed")])\n')
                if fault == 'socket':
                    with path.open('a') as stream:
                        stream.write('\nimport socket\ntry:\n    socket.socket().close()\nexcept RuntimeError:\n    pass\n')
                if fault:
                    with self.assertRaises(subprocess.CalledProcessError,
                            msg='TF-27 replay accepted ' + fault + ' discovery'):
                        p.inventory(root, root, ids)
                else:
                    self.assertEqual(set(ids), set(p.inventory(root, root, ids)))

    def test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions(self):
        import parallel_tests as runner
        import test_owned_baseline as owned
        import rebind_approved_edits as live_policy
        for invalid in (None, 'source', 'missing'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy(); seed, data, baseline = self.fixture(root, p, invalid)
                unrelated = 'root:test_other.Other.test_unrelated'
                other = root / 'tests/test_other.py'
                other.write_text(other.read_text().replace('self.assertTrue(True)', 'self.assertTrue(False)'))
                current = p.inventory(root, root, seed)
                discovered = {module: {n: r for n, r in current.items() if n.rsplit('.', 2)[0] == module}
                              for module in ('root:test_case', 'root:test_other')}
                if invalid == 'missing':
                    (root / p.BINDINGS / '1.json').unlink()
                with mock.patch.object(runner, 'ROOT', root), \
                     mock.patch.object(runner, 'SUITES', {'root': root}), \
                     mock.patch.object(runner, 'inventory', return_value=discovered), \
                     mock.patch.object(runner, 'legacy_versions', side_effect=lambda *args: (copy.deepcopy(baseline), copy.deepcopy(baseline), [])), \
                     mock.patch.object(runner.framework, 'validate_sources', return_value=[]), \
                     mock.patch.object(owned, 'checked', return_value=seed), \
                     mock.patch.object(live_policy, 'checked_bindings', side_effect=p.checked_bindings):
                    errors = runner.metadata()[-1]
                    if invalid:
                        self.assertTrue(any(e.startswith('cannot verify approved test edits:') for e in errors),
                                        'TF-27 metadata accepted invalid admission: ' + invalid)
                    else:
                        suffixes = {'fingerprint': ': legacy implementation changed; tag it',
                                    'dependencies': ': legacy fixture/helper dependency changed; migrate its requirement proof',
                                    'dependency_values': ': legacy constants/helper sources changed or unresolved; migrate its requirement proof'}
                        expected = {unrelated + suffix for suffix in suffixes.values()}
                        self.assertEqual(expected, set(errors))
                        with mock.patch.object(live_policy, 'checked_bindings', return_value={}):
                            red = runner.metadata()[-1]
                        changed = {(n, field) for n in data['tests'] for field in p.FIELDS
                                   if seed[n][field] != data['tests'][n][field]}
                        self.assertEqual({('root:test_case.Case.test_changed', f) for f in p.FIELDS}, changed)
                        self.assertEqual({n + suffixes[f] for n, f in changed}, set(red) - set(errors), repr(red))
                        self.assertTrue(expected <= set(red))


@requirements('TF-27', 'TF-23', why_not_scenario='Disposable committed repositories replay a normal migration and verify its ordinary metadata checks.')
class ApprovedEditsMigration(unittest.TestCase):
    policy = ApprovedEdits.policy
    commit = ApprovedEditsR1.commit
    write_json = ApprovedEditsR1.write_json
    snapshot = ApprovedEditsR1.snapshot

    def migration(self, root, p, fault=None, *, outside_manifest=False):
        import hashlib
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        ids = self.snapshot(root)
        path = root / 'tests/test_case.py'
        path.write_text(path.read_text() + '    def test_active(self):\n        self.assertEqual(1, 1)\n')
        if outside_manifest:
            path.write_text('from fixture import VALUE\n' + path.read_text().replace(
                'self.assertEqual(1, 1)', 'self.assertEqual(VALUE, 1)'))
            (root / 'tests/fixture.py').write_text('VALUE = 1\n')
        ids['root:test_case.Case.test_active'] = {}
        seed = p.inventory(root, root, ids)
        baseline = {'baseline': 'fixture', 'tests': {n: r['fingerprint'] for n, r in seed.items()}}
        for name in ('test-legacy.json', 'test-legacy-0850.json', 'test-legacy-ceiling.json'):
            self.write_json(root / 'scripts' / name, baseline)
        self.write_json(root / 'scripts/test-legacy-dependencies.json', {n: r['dependencies'] for n, r in seed.items()})
        self.write_json(root / 'scripts/test-legacy-captured-values.json',
                        {n: {'fingerprint': r['dependency_values']} for n, r in seed.items()})
        self.write_json(root / 'scripts/test-entrypoints.json', {})
        base = self.commit(root, 'trusted legacy source')
        migrated = 'root:test_case.Case.test_changed'
        old = path.read_text()
        new = old.replace('self.assertEqual(1, 1)', 'self.assertEqual(2, 1)')
        edits = [dict(line=4, old='        self.assertEqual(1, 1)\n', new='        self.assertEqual(2, 1)\n'),
                 dict(line=6, old='        self.assertEqual(1, 1)\n', new='        self.assertEqual(2, 1)\n')]
        if fault != 'undecorated':
            new = 'from test_framework import requirements\n' + new.replace(
                '    def test_changed(self):', '    @requirements("TF-27")\n    def test_changed(self):')
            edits = [dict(line=1, old='', new='from test_framework import requirements\n'),
                     dict(line=4, old='', new='    @requirements("TF-27")\n'),
                     *[{**e, 'line': e['line'] + 2} for e in edits]]
        path.write_text(new)
        manifest_file = 'tests/test_case.py'
        if outside_manifest:
            manifest_file = 'tests/fixture.py'
            (root / manifest_file).write_text('VALUE = 2\n')
            edits = [dict(line=1, old='VALUE = 1\n', new='VALUE = 2\n')]
        active = copy.deepcopy(baseline)
        del active['tests'][migrated]
        for name in ('test-legacy.json', 'test-legacy-0850.json'):
            self.write_json(root / 'scripts' / name, baseline if fault == name else active)
        self.write_json(root / 'scripts/test-requirements.json', {'TF-27': {
            'statement': 'Normal migration', 'source': 'fixture', 'checks': [migrated]}})
        edit = self.commit(root, 'approved assertions and normal requirement migration')
        manifest = dict(schema=1, decision='1', transitions=[dict(file=manifest_file,
            before_commit=base, edit_commit=edit, edits=edits)])
        self.write_json(root / p.MANIFESTS / '1.json', manifest)
        digest = hashlib.sha256((root / p.MANIFESTS / '1.json').read_bytes()).hexdigest()
        self.write_json(root / p.APPROVALS, {'schema': 1, 'decisions': {
            '1': {'manifest_sha256': digest, 'binding': '1'}}})
        self.commit(root, 'owner manifest and approval')
        self.enterContext(mock.patch.object(p, 'SOURCE_REVISION', base))
        self.enterContext(mock.patch.object(p, 'seed_records', return_value=seed))
        def active_ids(repo, at=None):
            import json
            return set().union(*(json.loads(p.git(repo, 'show', (at or 'HEAD') + ':scripts/' + name))['tests']
                                 for name in ('test-legacy.json', 'test-legacy-0850.json')))
        self.enterContext(mock.patch.object(p, 'active_ids', side_effect=active_ids))
        return manifest, seed, active, migrated

    def test_migration_outside_manifest_uses_candidate_and_introduction_requirements(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            manifest, seed, _, migrated = self.migration(root, p, outside_manifest=True)
            self.assertEqual(['tests/fixture.py'], [t['file'] for t in manifest['transitions']])
            self.assertNotIn(migrated, p.listed_ids(root))
            self.assertEqual(['TF-27'], p.inventory(root, root, seed)[migrated]['requirements'])
            # The replay keeps the undecorated definition, but its helper changes.
            from generate_test_owned_baseline import extract
            with tempfile.TemporaryDirectory() as replay:
                snapshot = Path(replay)
                extract(root, p.SOURCE_REVISION, snapshot)
                (snapshot / 'tests/fixture.py').write_text('VALUE = 2\n')
                row = p.inventory(root, snapshot, seed)[migrated]
                self.assertEqual([], row['requirements'])
                self.assertNotEqual(seed[migrated]['dependency_values'], row['dependency_values'])
            data = p.build(root, [manifest])
            self.assertEqual({'root:test_case.Case.test_active'}, set(data['tests']))
            self.write_json(root / p.BINDINGS / '1.json', data)
            introduction = self.commit(root, 'binding introduction')
            self.assertEqual(data['tests'], p.checked_bindings(root))
            path = root / 'tests/test_case.py'
            path.write_text(path.read_text().replace('    @requirements("TF-27")\n', ''))
            self.commit(root, 'later definition cannot alter introduction migration')
            self.assertEqual(data, p.build(root, [manifest], set(data['tests']), at=introduction))
            self.assertEqual(data['tests'], p.checked_bindings(root))
            with self.assertRaisesRegex(ValueError, 'retired.*requirement'):
                p.build(root, [manifest])

    def test_normal_migration_skips_binding_and_uses_requirement_gate(self):
        import parallel_tests as runner
        import test_owned_baseline as owned
        import rebind_approved_edits as live_policy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p = self.policy()
            manifest, seed, active, migrated = self.migration(root, p)
            data = p.build(root, [manifest])
            self.assertEqual({'root:test_case.Case.test_active'}, set(data['tests']))
            self.assertNotIn(migrated, data['tests'])
            for field in p.FIELDS:
                stale = copy.deepcopy(seed); stale[migrated][field] = 'stale'
                with self.subTest(field=field), mock.patch.object(p, 'seed_records', return_value=stale), \
                     self.assertRaisesRegex(ValueError, 'trusted old proof'):
                    p.build(root, [manifest])
            self.write_json(root / p.BINDINGS / '1.json', data)
            self.commit(root, 'binding introduction')
            self.assertEqual(data['tests'], p.checked_bindings(root))
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
                self.write_json(catalogue, {})
                self.assertEqual([migrated + ": unknown requirement 'TF-27'"], runner.metadata()[-1])
            # Later retirement filters application, but must not rederive the
            # immutable binding using today's lists instead of introduction's.
            for name in ('test-legacy.json', 'test-legacy-0850.json'):
                self.write_json(root / 'scripts' / name, {**active, 'tests': {
                    n: f for n, f in active['tests'].items() if n not in data['tests']}})
            self.commit(root, 'later eligibility shrink')
            self.assertEqual({}, p.checked_bindings(root))

    def test_retired_undecorated_changed_proof_is_refused(self):
        for outside in (False, True):
            with self.subTest(outside_manifest=outside), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy()
                manifest, _, _, _ = self.migration(root, p, 'undecorated', outside_manifest=outside)
                with self.assertRaisesRegex(ValueError, 'retired.*requirement'):
                    p.build(root, [manifest])

    def test_decorated_proof_in_either_legacy_list_is_refused(self):
        for name, outside in ((name, outside) for name in ('test-legacy.json', 'test-legacy-0850.json')
                              for outside in (False, True)):
            with self.subTest(legacy=name, outside_manifest=outside), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); p = self.policy()
                manifest, seed, _, migrated = self.migration(root, p, name, outside_manifest=outside)
                # Effective eligibility may already exclude a superseded ID;
                # presence in either raw list must still reject its decorator.
                with mock.patch.object(p, 'active_ids', return_value=set(seed) - {migrated}), \
                     self.assertRaisesRegex(ValueError, 'changed legacy identity/metadata'):
                    p.build(root, [manifest])
                # Uncommitted list shrinkage cannot retire a proof still listed
                # in the candidate commit whose definition we discover.
                listed = root / 'scripts' / name
                data = p.fw.decode_json(listed.read_text())
                del data['tests'][migrated]
                self.write_json(listed, data)
                with self.assertRaisesRegex(ValueError, 'changed legacy identity/metadata'):
                    p.build(root, [manifest])
