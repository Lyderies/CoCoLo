# Removed checks — test reset A

Decisions 125 and 126 retire fingerprint/approval governance. This ledger compares starting HEAD `36b7ef5f` with this worktree. Line numbers in the removal tables refer to that starting commit. CODEOWNERS means `.github/CODEOWNERS`; assertion_diff means `scripts/assertion_diff.py` and the new report-only `assertion-diff` CI job. No product assertion or product test method changed.

## Removed checks and their replacements

| Removed check / code path | Replacement |
| --- | --- |
| Legacy identity, implementation/fixture/helper/constant/default/closure fingerprints; proof migration, restoration and renaming gates (`describe_test`, `validate_metadata`) | retired: governed test text, replaced by CODEOWNERS + assertion_diff report; declared tags, IDs, reciprocal catalogue links, stage/layer validation and rationale checks stay. |
| Historical base discovery/comparison and release legacy overlays in preflight (`metadata`) | `--base` remains accepted and unused. Guarded discovery and current-tree metadata/source validation stay. |
| Approved-edit manifest authority, exact bytes, immutable introductions, ancestry, generation ordering, migrations/retirements, and binding replay | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| Release-bookkeeping append-only transitions, source hashes, immutable bindings and fingerprint overrides/replay | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. Product release tests remain. |
| Historical budget-debt growth/restore/ceiling binding checks | Ordinary owner review of `test-budget-debt.json`; actual per-module budgets, current debt warnings, shard aggregation and over-budget failures stay. |
| Digest/revision-bound registered review outcomes and red/green witness-file replay | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. `test-checks.json` keeps declared links; actual worker outcomes and complete run records determine executed coverage. |
| Requirement must have at least one check; untagged test must migrate into a proof | Empty checks become `unchecked`, reported with count and IDs in run records and the last output line; linked checks and catalogue validity still enforced. |
| Cross-stage/shard fingerprint equality | Compatible run identity, stages, layers, catalogue, budgets, disjoint workers and completeness remain checked by `combine_measurements` / `combine_run_records`. |
| Legacy baseline generation and test-owned baseline provenance | retired: governed test text, replaced by CODEOWNERS + assertion_diff report; `generate_test_metadata.py --catalogue` remains for current requirement links. |

## Deleted implementation functions

Each function below implements one of the retired checks above (including its helper plumbing); replacement: retired: governed test text, replaced by CODEOWNERS + assertion_diff report. Budget-history functions instead use ordinary review of current non-ceiling debt and retained runtime budgets.

| File | Removed functions (old lines) |
| --- | --- |
| `scripts/generate_test_owned_baseline.py` | `git` (17); `read_at` (21); `seed_at` (25); `admitted_at` (47); `extract` (51); `inventory` (68); `translate` (76); `generate` (90); `main` (114) |
| `scripts/parallel_tests.py` | `measured_history` (188); `measured_history.measured` (189); `legacy_versions` (200); `legacy_versions.git` (205); `legacy_versions.version` (207); `original_budget_versions` (247); `original_budget_versions.git` (249); `original_budget_versions.version` (263); `release_versions` (277); `release_versions.git` (279); `release_versions.version` (281); `release_versions.previous` (291); `budget_versions` (316) |
| `scripts/rebind_approved_edits.py` | `git` (25); `revision` (29); `is_ancestor` (33); `immutable` (41); `read_manifests` (74); `authorize` (93); `trusted_file` (115); `edit_bytes` (121); `exact_current` (145); `exact_base` (150); `sources` (155); `changed_proofs` (189); `inventory` (215); `seed_records` (269); `shrinking_ids` (281); `active_ids` (288); `listed_ids` (315); `build` (325); `admitted_paths` (375); `verify_introduction` (386); `binding_state` (407); `checked_bindings` (458); `check_later_retirements` (469); `main` (503) |
| `scripts/rebind_release_bookkeeping.py` | `git` (23); `revision` (27); `texts` (31); `transition_errors` (35); `unpack` (78); `child` (92); `source_record` (98); `base_bindings` (114); `inventory` (122); `build` (144); `source_hashes` (170); `checked_bindings` (174); `apply_fingerprints` (196); `write_new` (202); `main` (208) |
| `scripts/test_framework.py` | `_source` (43); `release_legacy` (127); `validate_budget_debt` (207); `source_bindings` (368); `dependency_fingerprint` (386); `dependency_fingerprint.covered` (396); `dependency_fingerprint.dependency_source` (398); `dependency_fingerprint.owned_path` (416); `dependency_fingerprint.identity` (423); `dependency_fingerprint.stable` (427); `dependency_fingerprint.global_value` (453); `dependency_fingerprint.default_values` (473); `dependency_fingerprint.default_values.declared` (485); `dependency_fingerprint.code_value` (506); `dependency_fingerprint.local` (508); `dependency_fingerprint.walk` (529); `validate_legacy_values` (608); `registered_outcomes` (689); `validate_witness_evidence` (785) |
| `scripts/test_owned_baseline.py` | `validate_document` (11); `checked_version` (23); `checked_version.git` (28); `checked` (56); `effective_records` (63) |

## Deleted or changed test methods

Every removed or changed test method is listed below, including methods renamed to keep their remaining checks and nested fixture methods. Tests of kept runner, guards, layers, requirement tags, goldens, Hypothesis, budgets and shard/combine behavior remain.

| Original file:line and method | Change | Replacement |
| --- | --- | --- |
| `tests/test_approved_edits.py:26` `ApprovedEdits.test_exact_transition_and_extra_unlisted_edits` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:37` `ApprovedEdits.test_wrong_text_line_endings_and_stale_base` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:50` `ApprovedEdits.test_only_changed_active_proofs_rebind_all_three_kinds` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:66` `ApprovedEdits.test_renamed_restored_and_unresolved_proofs_are_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:75` `ApprovedEdits.test_unrelated_legacy_proof_still_flags_and_retired_stays_retired` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:94` `ApprovedEdits.test_immutable_introduction_tampering_and_overwrite` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:120` `ApprovedEdits.test_recorded_owner_transitions_match_current_bytes` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:134` `ApprovedEdits.test_bridge_migration_plumbing_has_separate_exact_scope` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:255` `ApprovedEditsR1.test_r1_unanchored_files_never_generate_proofs` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:286` `ApprovedEditsR1.test_r1_unchanged_stale_old_proofs_are_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:296` `ApprovedEditsR1.test_r1_owner_record_authenticates_first_introduction` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:313` `ApprovedEditsR1.test_r1_introduction_requires_ancestry_sources_and_algorithm` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:323` `ApprovedEditsR1.test_non_git_metadata_admits_nothing_and_documents_fail_closed` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:355` `ApprovedEditsR1.test_r1_deleted_admission_documents_are_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:388` `ApprovedEditsR1.test_r1_replay_uses_guarded_discovery_and_rejects_duplicate_ids` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:411` `ApprovedEditsR1.test_r1_metadata_wiring_exact_diagnostics_and_invalid_admissions` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:519` `ApprovedEditsMigration.test_migration_outside_manifest_uses_candidate_and_introduction_requirements` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:553` `ApprovedEditsMigration.test_normal_migration_skips_binding_and_uses_requirement_gate` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:594` `ApprovedEditsMigration.test_later_bound_retirement_requires_original_identity_and_normal_migration` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:653` `ApprovedEditsMigration.test_retired_undecorated_changed_proof_is_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits.py:661` `ApprovedEditsMigration.test_decorated_proof_in_either_legacy_list_is_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:58` `ApprovedEditsLaterBinding.test_second_generation_excludes_previously_migrated_unbound_proof` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:100` `ApprovedEditsLaterBinding.test_second_generation_still_checks_unbound_legacy_proof` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:115` `ApprovedEditsLaterBinding.test_sibling_binding_introductions_are_not_linear` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:155` `ApprovedEditsLaterBinding.test_introduction_checks_earlier_only_source_even_after_restoration` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:175` `ApprovedEditsLaterBinding.test_full_history_refuses_merge_hidden_independent_document_addition` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:201` `ApprovedEditsLaterBinding.test_absent_undecorated_id_in_previously_untouched_suite_is_not_migrated` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:251` `ApprovedEditsLaterBinding.test_sole_binding_dirty_source_precedes_proof_rebuild` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:314` `ApprovedEditsLaterBinding.test_two_generations_pass_gate_and_no_covered_manifest_is_rebound` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:334` `ApprovedEditsLaterBinding.test_second_generation_relisting_first_decision_is_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:348` `ApprovedEditsLaterBinding.test_new_decision_approval_naming_wrong_binding_is_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_approved_edits_generations.py:360` `ApprovedEditsLaterBinding.test_first_binding_tampering_after_second_exists_is_refused` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_e2e_requirements.py:48` `E2ERequirementsTests.test_every_requirement_is_proved_by_a_check_that_exists` | Deleted/renamed | `E2ERequirementsTests.test_every_listed_requirement_check_exists_and_links_back`: validate the catalogue and reciprocal links; empty checks are reported, not failed. |
| `tests/test_product_dependencies.py:43` `ProductDependencies.test_product_only_constant_and_implementation_changes_keep_legacy_proof` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:54` `ProductDependencies.test_test_owned_helper_and_constant_edits_still_flag` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:67` `ProductDependencies.test_assertion_edit_and_rename_still_fail_legacy_gate` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:77` `ProductDependencies.test_product_symbol_identity_changes_are_not_ignored` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:84` `ProductDependencies.test_source_ownership_wins_over_test_looking_product_alias` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:94` `ProductDependencies.test_imported_product_defaults_use_identity_but_owned_defaults_keep_values` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:105` `ProductDependencies.test_later_bookkeeping_proof_takes_precedence_over_older_policy_record` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:118` `ProductDependencies.test_fileless_generated_helpers_keep_owned_namespace_and_external_identity` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_product_dependencies.py:135` `ProductDependencies.test_migration_rejects_wrong_provenance_and_changed_test_identity` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:64` `ReleaseBookkeeping.test_exact_primary_restoration_matches_installed_release` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:83` `ReleaseBookkeeping.test_correct_append_passes_and_every_unrelated_edit_is_rejected` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:111` `ReleaseBookkeeping.test_rebinding_retains_retirement_and_catches_later_fingerprint_edits` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:127` `ReleaseBookkeeping.test_rebind_command_rejects_invalid_source_and_overwrite` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:148` `ReleaseBookkeeping.test_claimed_pre_bump_commit_cannot_hide_old_record_edits` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:171` `ReleaseBookkeeping.test_later_edits_to_each_admitted_file_still_fail_metadata` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:185` `ReleaseBookkeeping.test_next_release_uses_the_same_narrow_procedure` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_release_bookkeeping.py:246` `ReleaseBookkeeping.test_immutable_binding_tampering_is_rejected` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:44` `MetadataTests.test_unknown_empty_and_untagged_proofs_and_missing_rationale_are_rejected` | Deleted/renamed | `MetadataTests.test_unknown_and_empty_tags_and_missing_tagged_rationale_are_rejected`: keep ID and rationale validation; untagged tests are allowed. |
| `tests/test_test_framework.py:54` `MetadataTests.test_decorator_inheritance_and_override_follow_the_actual_implementation` | Changed | Same method keeps inherited/overridden requirement-tag checks; fingerprint assertion retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:82` `MetadataTests.test_legacy_list_cannot_grow_restore_entries_change_fingerprints_or_rename_tests` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:100` `MetadataTests.test_migration_removes_legacy_entry_and_new_tagged_proof_is_required` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:304` `BudgetDebtTests.test_debt_can_only_shrink_and_does_not_exempt_new_modules` | Deleted/renamed | `BudgetDebtTests.test_budget_debt_does_not_exempt_new_modules`: retain current module budgets and non-ceiling debt; historical shrink/restore governance retired, replaced by ordinary owner review. |
| `tests/test_test_framework.py:371` `ReviewCoreTests.test_A1_C3_constant_fingerprints_have_equality_and_independent_change_controls` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:382` `ReviewCoreTests.test_A3_dynamic_helpers_need_explicit_source_dependencies` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:389` `ReviewCoreTests.test_A4_review_records_bind_digest_and_nonempty_revision` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:501` `ReviewEvidenceTests.test_B_note_stored_constant_hash_and_unresolved_dependencies_are_enforced` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:511` `ReviewEvidenceTests.test_C6_overridden_and_generated_cases_fail_preflight_by_full_id` | Deleted/renamed | `ReviewEvidenceTests.test_C6_overridden_and_generated_cases_keep_optional_requirement_tags`: keep overridden/generated discovery; untagged tests allowed. |
| `tests/test_test_framework.py:517` `ReviewEvidenceTests.test_C6_overridden_and_generated_cases_fail_preflight_by_full_id.Parent.test_value` | Deleted/renamed | Nested fixture retained under renamed `test_C6_overridden_and_generated_cases_keep_optional_requirement_tags`; requirement tags are optional. |
| `tests/test_test_framework.py:520` `ReviewEvidenceTests.test_C6_overridden_and_generated_cases_fail_preflight_by_full_id.Overridden.test_value` | Deleted/renamed | Nested fixture retained under renamed `test_C6_overridden_and_generated_cases_keep_optional_requirement_tags`; requirement tags are optional. |
| `tests/test_test_framework.py:584` `ReviewLifecycleTests.test_C3_C13_cross_module_helper_is_found_by_source_path` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:611` `ReviewLifecycleTests.test_B6_stage_records_must_share_source_and_layer_plans` | Changed | Same method keeps compatible layer, stage and budget plans plus run identity; fingerprint-plan equality retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework.py:642` `ReviewLifecycleTests.test_A1_relocated_worktrees_keep_equal_fixture_hashes_without_hiding_constant_edits` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_guards.py:327` `RunnerIntegrationTests.test_untagged_new_test_fails_the_actual_runner` | Deleted/renamed | `RunnerIntegrationTests.test_untagged_test_and_unchecked_requirement_are_reported_without_failure`: verifies exit 0, run-record count/IDs, and last-line report. |
| `tests/test_test_framework_guards.py:485` `RunnerIntegrationTests.test_review_C13_unpatched_runner_fails_closed_without_trusted_base` | Deleted/renamed | `--base` accepted for CI compatibility and unused; trusted-base fingerprint precondition retired. Existing CI base-selection job/step names and their shell regression test stay. |
| `tests/test_test_framework_integration.py:68` `ReleaseIntegration.test_release_overlay_is_immutable_and_candidate_changes_are_rejected` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_integration.py:85` `ReleaseIntegration.test_versioned_debt_has_measured_ceilings_and_cannot_grow` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_integration.py:103` `ReleaseIntegration.test_explicit_base_is_documented_and_missing_base_stops_before_discovery` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_integration.py:113` `ReleaseIntegration.test_committed_release_versions_reject_tamper_growth_and_restoration` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_r2.py:34` `RoundTwo.test_M1_unrelated_source_cannot_resolve_a_specific_dependency` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_r2.py:49` `RoundTwo.test_N1_same_basename_dependencies_both_contribute` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_r3.py:44` `RoundThree.test_p01_closure_fixture_state_invalidates_metadata` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_r3.py:54` `RoundThree.test_p01_positional_and_keyword_defaults_are_dependencies` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_r3.py:162` `RoundThree.test_TF23_declared_fixes_require_red_green_and_positive_control` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_review.py:62` `ReviewTests.test_registered_future_check_and_review_record_are_not_passing_evidence` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_review.py:81` `ReviewTests.test_fixture_and_transitive_helper_changes_change_dependency_fingerprint` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `tests/test_test_framework_review.py:105` `ReviewTests.test_missing_trusted_base_fails_closed_without_using_candidate_head` | Deleted/renamed | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |

## Removed metadata and evidence

| Removed paths | Replacement |
| --- | --- |
| `scripts/test-approved-edit-bindings/0851.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edit-bindings/0852.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/107.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/109.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/112.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/114.1.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/119.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/123.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-approved-edits/APPROVALS.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-budget-debt-0850-ceiling.json` | Ordinary owner review of current `scripts/test-budget-debt.json`; actual module budgets and aggregate shard checks remain. |
| `scripts/test-legacy-0850-ceiling.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-0850.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-bound-values.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-captured-values.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-ceiling.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-dependencies.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-portable-values.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-release-0850-bindings.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-release-0850-runtime.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-release-0850.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-test-owned-v1.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-test-owned-v2.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy-values.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-legacy.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-release-bindings/0.8.49.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-release-bindings/0.8.50.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-release-bindings/0.8.51.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-evidence.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-approved-edits.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-framework.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-integration.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-merge.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-product-dependencies.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-release-bookkeeping.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |
| `scripts/test-review-witnesses-runtime-integration.json` | retired: governed test text, replaced by CODEOWNERS + assertion_diff report. |

`scripts/test-requirements.json`, `test-stages.json`, `test-layers.json`, `test-layer-reasons.json`, `test-checks.json`, `test-timings.json`, non-ceiling `test-budget-debt.json`, `test-entrypoints.json` and `test-behaviour.json` remain. Stage/layer and behavior registries remove only retired framework entries and add assertion-diff coverage. Catalogue updates preserve current check links and retirement statements.

## Measurements archive and product docstrings

The primary created `measurements-0.8.52` at `36b7ef5f`, the last commit holding the archive. All tracked `measurements/**` evidence is removed and `/measurements/` is ignored. Historical evidence is available via that tag; source requirements remain active under `docs/requirements/`. No retained script or test reads the removed directory (the path-reference scan covers `scripts/`, `tests/` and every package test/script). `validate_sources` in `scripts/test_framework.py:387` resolves each catalogue source from the repository root and checks its exact statement/anchor, including the new requirement files.

| Old requirement source | Replacement |
| --- | --- |
| `measurements/b2/requirements.md` | `docs/requirements/b2.md`; catalogue source links updated. |
| `measurements/d2/requirements.md` | `docs/requirements/d2.md`; catalogue source links updated. |
| `measurements/d3/requirements.md` | `docs/requirements/d3.md`; catalogue source links updated. |
| `measurements/d4/requirements.md` | `docs/requirements/d4.md`; catalogue source links updated. |
| `measurements/e1/requirements.md` | `docs/requirements/e1.md`; catalogue source links updated. |
| `measurements/rsa-step3/requirements.md` | `docs/requirements/rsa-step3.md`; catalogue source links updated. |

Product changes already made by the primary (docstrings only; no imports or assertions removed):

- `packages/codex-agents/tests/test_b2_service_outcome.py:3`: `measurements/b2/requirements.md` → `docs/requirements/b2.md`.
- `packages/codex-agents/tests/test_deadlines_retention.py:254`: `measurements/d2/requirements.md` → `docs/requirements/d2.md`.

The authoritative source-reference lines also follow the moved maps: `REQUIREMENTS-b2-outcome.md:16`, `REQUIREMENTS-deadlines-retention.md:13`, `REQUIREMENTS-retirement-sweep.md:15`, and `REQUIREMENTS-publication-replay.md:18`. This keeps their exact statements equal to the catalogue; no requirement behavior changes.

No product-test import of a removed framework module needed removal. Product code is untouched.

## Verification and stop condition

The required interpreter/runner command was attempted with ten modules: `root:test_test_framework`, `root:test_test_framework_guards`, `root:test_test_framework_integration`, `root:test_test_framework_r2`, `root:test_test_framework_r3`, `root:test_test_framework_review`, `root:test_assertion_diff`, `root:test_e2e_requirements`, `root:test_ci_stages`, and `root:test_test_stages`. Output: `/tmp/reset-a-batch1.log`; intended timings: `/tmp/reset-a-batch1.json`. Interrupted with exit 130 after over four minutes in preflight; no worker tests ran, no run record or printed `preflight_seconds` was produced. No test pass/fail totals can be claimed.

A timed diagnostic of the existing metadata function identified recursive imported-file analysis in `golden_bypasses` (`scripts/test_framework.py:303–371`, called by `scripts/parallel_tests.py:234`). All three suite discoveries complete independently. Source validation takes about 0.012 s and metadata link validation about 0.002 s. The golden scan of `test_b2_service_outcome.py` alone took 10.372 s; the next module (`test_b2_service_outcome_hosts.py`) was still recursively analyzing imports when the 20 s diagnostic timeout fired. The interrupted required run's traceback confirms the same traversal. No caching, replacement analyzer or bypass was added: the user requires stopping if meeting acceptance needs new machinery.

A8 (quiet full run, preflight below 60 s) is unmet; a full run was not started. Remaining focused modules (`root:test_entrypoint_inventory`, `root:test_measure_test_behaviour`, `root:test_measure_test_behaviour_probe`, `codex:tests.test_runtime_release`, `root:test_install`) were not started because they share the blocked preflight. Socket-dependent framework tests were not reached; no socket permission failure was observed, so none is claimed as a sandbox exclusion. CODEOWNERS/CI scope and unchanged pre-existing job/step names were checked directly; all required metadata files remain; `validate_sources` passes after source-reference alignment. No commit was made.
