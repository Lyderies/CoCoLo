TF-27c adds only the `ApprovedEditsLaterBinding` class; every pre-existing assertion remains unchanged. All added assertions and rejection contexts are listed below. Commits occur only inside disposable test repositories.

| File:line | Witness/helper | Assertion |
|---|---|---|
| tests/test_approved_edits.py:726 | two_generations | `self.assertEqual(0, status, 'TF-27c second generation must bind only decision 2: ' + diagnostic)` |
| tests/test_approved_edits.py:728 | two_generations | `self.assertEqual({'2'}, set(second['manifests']))` |
| tests/test_approved_edits.py:729 | two_generations | `self.assertEqual(first['after_sources'], second['before_sources'])` |
| tests/test_approved_edits.py:730 | two_generations | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits.py:734 | two_generations | `self.assertTrue(expected, 'TF-27c positive control must change an existing proof')` |
| tests/test_approved_edits.py:735 | two_generations | `self.assertEqual(expected, second['tests'])` |
| tests/test_approved_edits.py:736 | two_generations | `self.assertNotEqual(first['tests'], second['tests'])` |
| tests/test_approved_edits.py:737 | two_generations | `self.assertEqual(expected, p.checked_bindings(root))` |
| tests/test_approved_edits.py:745 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(1, status)` |
| tests/test_approved_edits.py:746 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertIn('no new approved-edit manifest', diagnostic)` |
| tests/test_approved_edits.py:747 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertFalse((root / p.BINDINGS / '3.json').exists())` |
| tests/test_approved_edits.py:748 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits.py:752 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'unlisted change')` |
| tests/test_approved_edits.py:755 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:757 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'missing admitted')` |
| tests/test_approved_edits.py:771 | test_second_generation_relisting_first_decision_is_refused | `self.assertRaisesRegex(ValueError, 'duplicate approved-edit binding')` |
| tests/test_approved_edits.py:782 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertEqual(1, status)` |
| tests/test_approved_edits.py:783 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertIn('2: owner approval does not authorize manifest digest and binding name', diagnostic)` |
| tests/test_approved_edits.py:784 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits.py:792 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:795 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:798 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:801 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
