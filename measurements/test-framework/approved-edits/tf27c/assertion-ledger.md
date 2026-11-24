TF-27c round-two assertion ledger. Earlier refusal assertions remain; the round-one still-listed witness now runs each raw list alone and asserts that fixture precondition. The five new witnesses and both round-one witnesses have TF-23 red/green records in `r2/` and registry entries. Disposable fixture commits do not modify checkout history.

| File:line | Witness/helper | Assertion |
|---|---|---|
| tests/test_approved_edits_generations.py:38 | retired_first_generation | `self.assertNotIn(retired, first['tests'])` |
| tests/test_approved_edits_generations.py:39 | retired_first_generation | `self.assertEqual({'root:test_case.Case.test_active'}, set(first['tests']))` |
| tests/test_approved_edits_generations.py:43 | retired_first_generation | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:67 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual(0, status, 'TF-27c-2 normally migrated proof blocked generation 2: ' + diagnostic)` |
| tests/test_approved_edits_generations.py:69 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual({'root:test_other.Other.test_unrelated'}, set(second['tests']))` |
| tests/test_approved_edits_generations.py:70 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertNotIn(retired, second['tests'])` |
| tests/test_approved_edits_generations.py:71 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits_generations.py:73 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual({**first['tests'], **second['tests']}, p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:84 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual([], runner.metadata()[-1])` |
| tests/test_approved_edits_generations.py:88 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual([retired + ": unknown requirement 'TF-27'"], runner.metadata()[-1])` |
| tests/test_approved_edits_generations.py:97 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertRaisesRegex(ValueError, later + ': retired bound test has no requirement')` |
| tests/test_approved_edits_generations.py:107 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertEqual(name == legacy, unbound in listed, 'TF-27c still-listed fixture must exercise each legacy list alone')` |
| tests/test_approved_edits_generations.py:109 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertIn(unbound, p.listed_ids(root))` |
| tests/test_approved_edits_generations.py:111 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertEqual(1, status)` |
| tests/test_approved_edits_generations.py:112 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertIn(unbound + ': trusted old proof differs from admitted baseline', diagnostic)` |
| tests/test_approved_edits_generations.py:113 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits_generations.py:127 | test_sibling_binding_introductions_are_not_linear | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:149 | test_sibling_binding_introductions_are_not_linear | `self.assertLess(order.index(first_introduction), order.index(second_introduction))` |
| tests/test_approved_edits_generations.py:150 | test_sibling_binding_introductions_are_not_linear | `self.assertEqual(1, subprocess.run(['git', 'merge-base', '--is-ancestor', first_introduction, second_introduction], cwd=root).returncode)` |
| tests/test_approved_edits_generations.py:152 | test_sibling_binding_introductions_are_not_linear | `self.assertRaisesRegex(ValueError, 'approved-edit generations are not linear')` |
| tests/test_approved_edits_generations.py:160 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual(0, status, diagnostic)` |
| tests/test_approved_edits_generations.py:162 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual({'tests/test_other.py'}, set(second['after_sources']))` |
| tests/test_approved_edits_generations.py:165 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual(expected, p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:172 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertRaisesRegex(ValueError, 'introduction source/algorithm differs from replay')` |
| tests/test_approved_edits_generations.py:180 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:193 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertEqual(1, merge.returncode)` |
| tests/test_approved_edits_generations.py:197 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertNotIn(hidden, simplified)` |
| tests/test_approved_edits_generations.py:198 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertRaisesRegex(ValueError, 'conflicting independent introductions')` |
| tests/test_approved_edits_generations.py:224 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertIn(absent, seed)` |
| tests/test_approved_edits_generations.py:231 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertNotIn(absent, p.listed_ids(root))` |
| tests/test_approved_edits_generations.py:232 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertTrue(p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:247 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertEqual(1, status, 'TF-27c absent undecorated ID was treated as migrated')` |
| tests/test_approved_edits_generations.py:248 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertIn(absent + ': retired changed legacy proof has no requirement', diagnostic)` |
| tests/test_approved_edits_generations.py:249 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits_generations.py:255 | test_sole_binding_dirty_source_precedes_proof_rebuild | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:261 | test_sole_binding_dirty_source_precedes_proof_rebuild | `self.assertRaisesRegex(ValueError, '^unlisted change in approved-edit files$')` |
| tests/test_approved_edits_generations.py:300 | two_generations | `self.assertEqual(0, status, 'TF-27c second generation must bind only decision 2: ' + diagnostic)` |
| tests/test_approved_edits_generations.py:302 | two_generations | `self.assertEqual({'2'}, set(second['manifests']))` |
| tests/test_approved_edits_generations.py:303 | two_generations | `self.assertEqual(first['after_sources'], second['before_sources'])` |
| tests/test_approved_edits_generations.py:304 | two_generations | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits_generations.py:308 | two_generations | `self.assertTrue(expected, 'TF-27c positive control must change an existing proof')` |
| tests/test_approved_edits_generations.py:309 | two_generations | `self.assertEqual(expected, second['tests'])` |
| tests/test_approved_edits_generations.py:310 | two_generations | `self.assertNotEqual(first['tests'], second['tests'])` |
| tests/test_approved_edits_generations.py:311 | two_generations | `self.assertEqual(expected, p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:319 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(1, status)` |
| tests/test_approved_edits_generations.py:320 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertIn('no new approved-edit manifest', diagnostic)` |
| tests/test_approved_edits_generations.py:321 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertFalse((root / p.BINDINGS / '3.json').exists())` |
| tests/test_approved_edits_generations.py:322 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits_generations.py:326 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'unlisted change')` |
| tests/test_approved_edits_generations.py:329 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits_generations.py:331 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'missing admitted')` |
| tests/test_approved_edits_generations.py:345 | test_second_generation_relisting_first_decision_is_refused | `self.assertRaisesRegex(ValueError, 'duplicate approved-edit binding')` |
| tests/test_approved_edits_generations.py:356 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertEqual(1, status)` |
| tests/test_approved_edits_generations.py:357 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertIn('2: owner approval does not authorize manifest digest and binding name', diagnostic)` |
| tests/test_approved_edits_generations.py:358 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits_generations.py:366 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits_generations.py:369 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits_generations.py:372 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits_generations.py:375 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
