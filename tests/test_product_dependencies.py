"""TF-26: product changes must not masquerade as test-proof changes."""
import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test_framework as fw
from test_framework import requirements


class ProductDependencies(unittest.TestCase):
    __why_not_scenario__ = 'Isolated source fixtures exercise dependency ownership and invalid-proof controls without product side effects.'

    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'tests').mkdir(); (self.root / 'product').mkdir()

    def load(self, name, source, owned=True):
        path = self.root / ('tests' if owned else 'product') / (name + '.py')
        path.write_text(source)
        module = types.ModuleType(name); module.__file__ = str(path)
        sys.modules[name] = module; self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, str(path), 'exec'), vars(module))
        return module

    def fixture(self, digest='before', expected=1, body='return 1', assertion='1', method='test_value', product='product_contract'):
        self.load(product, f'DIGEST={digest!r}\nclass Service:\n    def value(self): {body}\ndef operation(): {body}\n', False)
        self.load('shared_contract', f'from {product} import DIGEST, Service, operation\nEXPECTED={expected}\ndef fixture():\n    return DIGEST, Service, operation, EXPECTED\n')
        module = self.load('test_contract_owner', f'import unittest\nfrom shared_contract import fixture\nclass Case(unittest.TestCase):\n    def {method}(self): self.assertEqual({assertion},fixture()[-1])\n')
        return module.Case(method)

    def record(self, **kwargs):
        return fw.describe_test(self.fixture(**kwargs))

    @requirements('TF-26','TF-7')
    def test_product_only_constant_and_implementation_changes_keep_legacy_proof(self):
        before = self.record()
        self.assertIsNotNone(before['dependency_values'])
        self.assertEqual(before, self.record(), 'valid unchanged proof is unstable')
        for change in ({'digest':'after'}, {'body':'return 2'}, {'digest':'after','body':'return 2'}):
            with self.subTest(change=change):
                after = self.record(**change)
                self.assertEqual(before, after, 'TF-26 product-only edit invalidated legacy proof')
                self.assertEqual([], fw.validate_legacy_values({'case':after},{'tests':{'case':before['fingerprint']}},{'case':{'fingerprint':before['dependency_values']}}))

    @requirements('TF-26','TF-7')
    def test_test_owned_helper_and_constant_edits_still_flag(self):
        before = self.record()
        self.assertEqual(before, self.record())
        after = self.record(expected=2)
        self.assertNotEqual(before['dependency_values'], after['dependency_values'])
        errors = fw.validate_legacy_values({'case':after},{'tests':{'case':before['fingerprint']}},{'case':{'fingerprint':before['dependency_values']}})
        self.assertTrue(any('legacy constants/helper sources changed' in e for e in errors), errors)
        case = self.fixture(); helper = sys.modules['shared_contract']
        helper.fixture = self.load('shared_contract', 'from product_contract import DIGEST, Service, operation\nEXPECTED=1\ndef fixture():\n    return DIGEST, Service, operation, EXPECTED+1\n').fixture
        sys.modules['test_contract_owner'].fixture = helper.fixture
        self.assertNotEqual(before['dependency_values'], fw.describe_test(case)['dependency_values'])

    @requirements('TF-26','TF-7')
    def test_assertion_edit_and_rename_still_fail_legacy_gate(self):
        old = self.fixture(); before = fw.describe_test(old); key='root:'+old.id(); module='root:test_contract_owner'
        legacy={'baseline':'fixture','tests':{key:before['fingerprint']}}
        def errors(case):
            return fw.validate_metadata(inventory={module:{'root:'+case.id():fw.describe_test(case)}},stages={module:'unit'},layers={module:'unit'},catalogue={},legacy=legacy,ceiling=legacy,previous=legacy)
        self.assertEqual([], errors(self.fixture()))
        self.assertTrue(any('legacy implementation changed' in e for e in errors(self.fixture(assertion='2'))))
        self.assertTrue(any('legacy' in e for e in errors(self.fixture(method='test_renamed'))))

    @requirements('TF-26','TF-7')
    def test_product_symbol_identity_changes_are_not_ignored(self):
        before=self.record()
        self.assertEqual(before,self.record())
        after=self.record(product='other_product_contract')
        self.assertNotEqual(before['dependency_values'],after['dependency_values'],'TF-26 changed product identity was ignored')

    @requirements('TF-26','TF-7')
    def test_source_ownership_wins_over_test_looking_product_alias(self):
        def record(body):
            product=self.load('test_looking_product',f'def operation(): {body}\n',False)
            self.load('shared_contract','from test_looking_product import operation\ndef fixture(): return operation\n')
            case=self.load('test_contract_owner','import unittest\nfrom shared_contract import fixture\nclass Case(unittest.TestCase):\n    def test_value(self): self.assertTrue(callable(fixture()))\n').Case('test_value')
            return fw.describe_test(case)
        before=record('return 1');self.assertEqual(before,record('return 1'))
        self.assertEqual(before,record('return 2'),'TF-26 product source followed a test-looking alias')

    @requirements('TF-26','TF-7','TF-23')
    def test_migration_rejects_wrong_provenance_and_changed_test_identity(self):
        try: policy=importlib.import_module('test_owned_baseline')
        except ModuleNotFoundError:self.fail('TF-26 missing immutable test-owned baseline validator')
        before=self.record();active={'case':before['fingerprint']}
        document={'schema':1,'policy':'test-owned-v1','source_revision':policy.SOURCE_REVISION,'tests':{'case':before}}
        self.assertEqual([],policy.validate_document(document,active))
        for mutate in ('source','id','fingerprint','unresolved'):
            changed=copy.deepcopy(document)
            if mutate=='source':changed['source_revision']='candidate-head'
            elif mutate=='id':changed['tests']['added']=changed['tests'].pop('case')
            elif mutate=='fingerprint':changed['tests']['case']['fingerprint']='changed'
            else:changed['tests']['case']['dependency_values']=None
            self.assertTrue(policy.validate_document(changed,active),mutate)
        self.assertEqual([],policy.validate_document(document,active))
