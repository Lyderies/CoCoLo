All pre-existing assertions and seven original witnesses are unchanged. The following ledger lists every added assertion and rejection context in the r1 fixture class. Fixture Git commits are disposable; the worktree remains uncommitted.

| Witness/helper | File:line | Added assertion |
|---|---|---|
| test_r1_unanchored_files_never_generate_proofs | tests/test_approved_edits.py:230 | `self.assertTrue(data['tests'], 'anchored positive control must rebind')` |
| test_r1_unanchored_files_never_generate_proofs | tests/test_approved_edits.py:223 | `self.assertEqual(b'VALUE = 3\n', after['tests/optional.py'])` |
| test_r1_unanchored_files_never_generate_proofs | tests/test_approved_edits.py:225 | `self.assertEqual({}, p.build(root, [manifest], set(seed))['tests'], 'TF-27 unanchored file contaminated a legacy proof')` |
| test_r1_unanchored_files_never_generate_proofs | tests/test_approved_edits.py:237 | `self.assertRaises(subprocess.CalledProcessError)` |
| test_r1_unchanged_stale_old_proofs_are_refused | tests/test_approved_edits.py:243 | `self.assertEqual({}, p.changed_proofs({'kept': row}, {'kept': row}, {'kept': row}))` |
| test_r1_unchanged_stale_old_proofs_are_refused | tests/test_approved_edits.py:246 | `self.assertRaisesRegex(ValueError, 'trusted old proof', msg='TF-27 stale unchanged old proof accepted')` |
| test_r1_owner_record_authenticates_first_introduction | tests/test_approved_edits.py:258 | `self.assertEqual(data['tests'], p.checked_bindings(root))` |
| test_r1_owner_record_authenticates_first_introduction | tests/test_approved_edits.py:265 | `self.assertEqual(data['tests'], p.checked_bindings(root))` |
| test_r1_owner_record_authenticates_first_introduction | tests/test_approved_edits.py:255 | `self.assertRaisesRegex(ValueError, 'owner approval', msg='TF-27 unauthorized first introduction accepted')` |
| test_r1_owner_record_authenticates_first_introduction | tests/test_approved_edits.py:262 | `self.assertRaisesRegex(ValueError, 'owner approval record changed or uncommitted')` |
| test_r1_introduction_requires_ancestry_sources_and_algorithm | tests/test_approved_edits.py:275 | `self.assertEqual(data['tests'], p.checked_bindings(root))` |
| test_r1_introduction_requires_ancestry_sources_and_algorithm | tests/test_approved_edits.py:272 | `self.assertRaisesRegex(ValueError, 'introduction', msg='TF-27 invalid introduction accepted: ' + invalid)` |
| test_r1_deleted_admission_documents_are_refused | tests/test_approved_edits.py:280 | `self.assertEqual(data['tests'], p.checked_bindings(root))` |
| test_r1_deleted_admission_documents_are_refused | tests/test_approved_edits.py:290 | `self.assertEqual(data['tests'], p.checked_bindings(root))` |
| test_r1_deleted_admission_documents_are_refused | tests/test_approved_edits.py:283 | `self.assertRaisesRegex(ValueError, 'missing admitted', msg='TF-27 deleted admission silently accepted')` |
| test_r1_deleted_admission_documents_are_refused | tests/test_approved_edits.py:287 | `self.assertRaisesRegex(ValueError, 'missing admitted')` |
| test_r1_replay_uses_guarded_discovery_and_rejects_duplicate_ids | tests/test_approved_edits.py:313 | `self.assertEqual(set(ids), set(p.inventory(root, root, ids)))` |
| test_r1_replay_uses_guarded_discovery_and_rejects_duplicate_ids | tests/test_approved_edits.py:309 | `self.assertRaises(subprocess.CalledProcessError, msg='TF-27 replay accepted ' + fault + ' discovery')` |
| test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions | tests/test_approved_edits.py:339 | `self.assertTrue(any((e.startswith('cannot verify approved test edits:') for e in errors)), 'TF-27 metadata accepted invalid admission: ' + invalid)` |
| test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions | tests/test_approved_edits.py:346 | `self.assertEqual(expected, set(errors))` |
| test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions | tests/test_approved_edits.py:351 | `self.assertEqual({('root:test_case.Case.test_changed', f) for f in p.FIELDS}, changed)` |
| test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions | tests/test_approved_edits.py:352 | `self.assertEqual({n + suffixes[f] for n, f in changed}, set(red) - set(errors), repr(red))` |
| test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions | tests/test_approved_edits.py:353 | `self.assertTrue(expected <= set(red))` |
