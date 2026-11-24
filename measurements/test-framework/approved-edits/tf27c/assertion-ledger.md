TF-27c round-two assertion ledger. Earlier refusal assertions remain; the round-one still-listed witness now runs each raw list alone and asserts that fixture precondition. The five new witnesses and both round-one witnesses have TF-23 red/green records in `r2/` and registry entries. Disposable fixture commits do not modify checkout history.

| File:line | Witness/helper | Assertion |
|---|---|---|
| tests/test_approved_edits.py:705 | retired_first_generation | `self.assertNotIn(retired, first['tests'])` |
| tests/test_approved_edits.py:706 | retired_first_generation | `self.assertEqual({'root:test_case.Case.test_active'}, set(first['tests']))` |
| tests/test_approved_edits.py:710 | retired_first_generation | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:734 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual(0, status, 'TF-27c-2 normally migrated proof blocked generation 2: ' + diagnostic)` |
| tests/test_approved_edits.py:736 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual({'root:test_other.Other.test_unrelated'}, set(second['tests']))` |
| tests/test_approved_edits.py:737 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertNotIn(retired, second['tests'])` |
| tests/test_approved_edits.py:738 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits.py:740 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual({**first['tests'], **second['tests']}, p.checked_bindings(root))` |
| tests/test_approved_edits.py:751 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual([], runner.metadata()[-1])` |
| tests/test_approved_edits.py:755 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertEqual([retired + ": unknown requirement 'TF-27'"], runner.metadata()[-1])` |
| tests/test_approved_edits.py:764 | test_second_generation_excludes_previously_migrated_unbound_proof | `self.assertRaisesRegex(ValueError, later + ': retired bound test has no requirement')` |
| tests/test_approved_edits.py:774 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertEqual(name == legacy, unbound in listed, 'TF-27c still-listed fixture must exercise each legacy list alone')` |
| tests/test_approved_edits.py:776 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertIn(unbound, p.listed_ids(root))` |
| tests/test_approved_edits.py:778 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertEqual(1, status)` |
| tests/test_approved_edits.py:779 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertIn(unbound + ': trusted old proof differs from admitted baseline', diagnostic)` |
| tests/test_approved_edits.py:780 | test_second_generation_still_checks_unbound_legacy_proof | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits.py:794 | test_sibling_binding_introductions_are_not_linear | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:816 | test_sibling_binding_introductions_are_not_linear | `self.assertLess(order.index(first_introduction), order.index(second_introduction))` |
| tests/test_approved_edits.py:817 | test_sibling_binding_introductions_are_not_linear | `self.assertEqual(1, subprocess.run(['git', 'merge-base', '--is-ancestor', first_introduction, second_introduction], cwd=root).returncode)` |
| tests/test_approved_edits.py:819 | test_sibling_binding_introductions_are_not_linear | `self.assertRaisesRegex(ValueError, 'approved-edit generations are not linear')` |
| tests/test_approved_edits.py:827 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual(0, status, diagnostic)` |
| tests/test_approved_edits.py:829 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual({'tests/test_other.py'}, set(second['after_sources']))` |
| tests/test_approved_edits.py:832 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertEqual(expected, p.checked_bindings(root))` |
| tests/test_approved_edits.py:839 | test_introduction_checks_earlier_only_source_even_after_restoration | `self.assertRaisesRegex(ValueError, 'introduction source/algorithm differs from replay')` |
| tests/test_approved_edits.py:847 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:860 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertEqual(1, merge.returncode)` |
| tests/test_approved_edits.py:864 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertNotIn(hidden, simplified)` |
| tests/test_approved_edits.py:865 | test_full_history_refuses_merge_hidden_independent_document_addition | `self.assertRaisesRegex(ValueError, 'conflicting independent introductions')` |
| tests/test_approved_edits.py:891 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertIn(absent, seed)` |
| tests/test_approved_edits.py:898 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertNotIn(absent, p.listed_ids(root))` |
| tests/test_approved_edits.py:899 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertTrue(p.checked_bindings(root))` |
| tests/test_approved_edits.py:914 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertEqual(1, status, 'TF-27c absent undecorated ID was treated as migrated')` |
| tests/test_approved_edits.py:915 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertIn(absent + ': retired changed legacy proof has no requirement', diagnostic)` |
| tests/test_approved_edits.py:916 | test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits.py:922 | test_sole_binding_dirty_source_precedes_proof_rebuild | `self.assertEqual(first['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:928 | test_sole_binding_dirty_source_precedes_proof_rebuild | `self.assertRaisesRegex(ValueError, '^unlisted change in approved-edit files$')` |
| tests/test_approved_edits.py:967 | two_generations | `self.assertEqual(0, status, 'TF-27c second generation must bind only decision 2: ' + diagnostic)` |
| tests/test_approved_edits.py:969 | two_generations | `self.assertEqual({'2'}, set(second['manifests']))` |
| tests/test_approved_edits.py:970 | two_generations | `self.assertEqual(first['after_sources'], second['before_sources'])` |
| tests/test_approved_edits.py:971 | two_generations | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits.py:975 | two_generations | `self.assertTrue(expected, 'TF-27c positive control must change an existing proof')` |
| tests/test_approved_edits.py:976 | two_generations | `self.assertEqual(expected, second['tests'])` |
| tests/test_approved_edits.py:977 | two_generations | `self.assertNotEqual(first['tests'], second['tests'])` |
| tests/test_approved_edits.py:978 | two_generations | `self.assertEqual(expected, p.checked_bindings(root))` |
| tests/test_approved_edits.py:986 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(1, status)` |
| tests/test_approved_edits.py:987 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertIn('no new approved-edit manifest', diagnostic)` |
| tests/test_approved_edits.py:988 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertFalse((root / p.BINDINGS / '3.json').exists())` |
| tests/test_approved_edits.py:989 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(first_bytes, (root / p.BINDINGS / '1.json').read_bytes())` |
| tests/test_approved_edits.py:993 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'unlisted change')` |
| tests/test_approved_edits.py:996 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
| tests/test_approved_edits.py:998 | test_two_generations_pass_gate_and_no_covered_manifest_is_rebound | `self.assertRaisesRegex(ValueError, 'missing admitted')` |
| tests/test_approved_edits.py:1012 | test_second_generation_relisting_first_decision_is_refused | `self.assertRaisesRegex(ValueError, 'duplicate approved-edit binding')` |
| tests/test_approved_edits.py:1023 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertEqual(1, status)` |
| tests/test_approved_edits.py:1024 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertIn('2: owner approval does not authorize manifest digest and binding name', diagnostic)` |
| tests/test_approved_edits.py:1025 | test_new_decision_approval_naming_wrong_binding_is_refused | `self.assertFalse((root / p.BINDINGS / '0.json').exists())` |
| tests/test_approved_edits.py:1033 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:1036 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:1039 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertRaisesRegex(ValueError, 'immutable approved-edit document')` |
| tests/test_approved_edits.py:1042 | test_first_binding_tampering_after_second_exists_is_refused | `self.assertEqual(second['tests'], p.checked_bindings(root))` |
