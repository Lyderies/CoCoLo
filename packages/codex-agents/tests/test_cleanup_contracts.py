"""Closed metadata validation using a real synthetic account registration."""
from dataclasses import FrozenInstanceError, replace
import json
import unittest

import tests.support  # noqa: F401
from tests import test_account_terminal_delivery as fixtures
from codex_agents.cleanup_contracts import (
    CleanupError, CleanupEvidence, encode_cleanup_evidence, verify_cleanup_evidence,
)
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.terminal_delivery import terminal_receipt


class CleanupContractTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AccountTerminalDeliveryTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.key, self.binding = self.base.fixture.key, self.base.expected
        receipt = terminal_receipt(self.key, self.base.grant, self.base.delivery, known=False)
        self.value = CleanupEvidence(self.binding, self.base.grant, self.base.delivery, receipt['receipt_digest'])
        self.raw = encode_cleanup_evidence(self.key, self.value)

    def sealed(self, data):
        data['seal'] = keyed_digest(self.key, b'project-cleanup-receipt-v1\0' + canonical_json(data['cleanup']))
        return canonical_json(data)

    def reject(self, raw, *, expected=None, key=None):
        with self.assertRaises(CleanupError) as caught:
            verify_cleanup_evidence(self.key if key is None else key, self.binding if expected is None else expected, raw)
        self.assertEqual(('cleanup_invalid_evidence',), caught.exception.args)
        self.assertIsNone(caught.exception.__context__)

    def test_deterministic_roundtrip_exact_domain_frozen_values_and_no_authority(self):
        data = json.loads(self.raw)
        self.assertEqual({'protocol_version', 'cleanup', 'seal'}, set(data))
        self.assertEqual(self.raw, self.sealed(data))
        self.assertEqual(self.raw, encode_cleanup_evidence(self.key, self.value))
        self.assertEqual(self.value, verify_cleanup_evidence(self.key, self.binding, self.raw))
        with self.assertRaises(FrozenInstanceError):
            self.value.terminal_receipt_digest = 'f' * 64
        # A trusted key can construct metadata; this does NOT prove cleanup or
        # even an account acceptance. No account terminal has been submitted.
        self.assertEqual(0, self.base.count())
        changed = json.loads(self.raw)
        changed['cleanup']['terminal_receipt_digest'] = 'f' * 64
        verify_cleanup_evidence(self.key, self.binding, self.sealed(changed))
        self.base.held()
        self.assertNotIn(b'PRIVATE-BODY-CANARY', self.raw)

    def test_every_nested_object_rejects_missing_extra_and_nonobject_values(self):
        paths = [(), ('cleanup',), ('cleanup', 'binding'), ('cleanup', 'grant'),
                 ('cleanup', 'grant', 'binding'), ('cleanup', 'delivery'), ('cleanup', 'delivery', 'binding')]
        for path in paths:
            for mutation in ('missing', 'extra', 'list'):
                data = json.loads(self.raw)
                target = data
                for name in path:
                    target = target[name]
                if mutation == 'extra':
                    target['PRIVATE-EXTRA'] = True
                elif mutation == 'missing':
                    target.pop(next(iter(target)))
                elif path:
                    parent = data
                    for name in path[:-1]:
                        parent = parent[name]
                    parent[path[-1]] = list(target)
                else:
                    data = []
                with self.subTest(path=path, mutation=mutation):
                    self.reject(canonical_json(data))

    def test_exact_integer_types_ranges_and_bound_scopes_with_valid_outer_seals(self):
        paths = [('cleanup', 'binding'), ('cleanup', 'grant'), ('cleanup', 'grant', 'binding'),
                 ('cleanup', 'delivery'), ('cleanup', 'delivery', 'binding')]
        original = json.loads(self.raw)
        for path in paths:
            obj = original
            for name in path:
                obj = obj[name]
            for field, current in obj.items():
                if type(current) is not int:
                    continue
                for replacement in (True, float(current), str(current), -1, 2**64):
                    data = json.loads(self.raw)
                    target = data
                    for name in path:
                        target = target[name]
                    target[field] = replacement
                    with self.subTest(path=path, field=field, replacement=replacement):
                        self.reject(self.sealed(data))
        for field in vars(self.binding):
            current = getattr(self.binding, field)
            if type(current) is int:
                changed = current + 1
            elif field.endswith('digest'):
                changed = 'f' * 64
            elif field == 'project_id':
                changed = 'prj_' + 'f' * 32
            elif field == 'orchestration_handle':
                changed = 'orc_' + 'f' * 48
            elif field == 'run_handle':
                changed = 'hrn_' + 'f' * 48
            else:
                changed = 'different'
            with self.subTest(independent_expected_field=field):
                self.reject(self.raw, expected=replace(self.binding, **{field: changed}))

    def test_original_grant_and_delivery_bindings_are_verified_independently_of_outer_mac(self):
        for field in ('assignment_digest', 'handle_digest', 'registration_digest', 'lease_digest'):
            data = json.loads(self.raw)
            data['cleanup']['grant'][field] = 'f' * 64
            self.reject(self.sealed(data))
        for field, changed in (('claim_id', 'bridge:' + 'f' * 64), ('claim_digest', 'f' * 64)):
            data = json.loads(self.raw)
            data['cleanup']['delivery']['binding'][field] = changed
            self.reject(self.sealed(data))
        for field in ('record_content_digest', 'result_digest', 'record_id'):
            data = json.loads(self.raw)
            data['cleanup']['delivery'][field] = 'PRIVATE-INVALID'
            self.reject(self.sealed(data))

    def test_keys_bounds_noncanonical_duplicate_and_malformed_json_are_fixed_failures(self):
        for key in (b'', b'x' * 31, b'x' * 33, bytearray(self.key), 'PRIVATE-KEY', b'x' * 32):
            with self.subTest(key_type=type(key), key_length=len(key)):
                self.reject(self.raw, key=key)
                with self.assertRaises(CleanupError) as caught:
                    encode_cleanup_evidence(key, self.value)
                self.assertEqual(('cleanup_invalid_input',), caught.exception.args)
                self.assertIsNone(caught.exception.__context__)
        malformed = [b'', 'PRIVATE-RAW', bytearray(self.raw), self.raw + b'\n', b'\xff', b'x' * 16384,
                     b'x' * 16385, b'{"protocol_version":1,"protocol_version":1,' + self.raw[1:],
                     self.raw.replace(b'"protocol_version":1', b'"protocol_version":true')]
        for raw in malformed:
            self.reject(raw)


if __name__ == '__main__':
    unittest.main()
