# Fix round: primary reruns

Use the supplied test venv and named modules only. Unset
`COCOLO_TEST_B4T_SKIP_SOCKETS` for native acceptance. The builder's explicit
skip flag is preserved by the runner; an earlier custom flag was stripped,
causing two attempted native fixture starts to fail. Those failures are in
`evidence/fix-round/second-named.txt`; they are not passes or sandbox excuses.

## Socket acceptance — exact new test names

Module `codex:tests.test_b4t_native` (scenario layer, integration stage):

- `NativeArchiveTests.test_B4t_A1_no_other_process_is_inspected` (T-36).
- `NativeArchiveTests.test_B4t_A1_idle_predecessor_with_native_identity_updates_without_a_signal` (T-37a).
- `NativeArchiveTests.test_B4t_A1_idle_predecessor_updates_without_process_proof` (T-37b; injected EPERM).
- `AcceptedResidualStubTests.test_same_user_impostor_with_expected_arguments_is_the_only_signal_target` (T-12 / A1.7 item 6 / TL).
- `AcceptedResidualStubTests.test_same_user_impostor_with_unchanged_entry_hash_and_held_lock_refuses_adoption` (macOS unnamed-holder residual; Linux has I5).
- `AcceptedResidualStubTests.test_same_user_impostor_ignoring_TERM_is_KILLed_after_bound` (real TERM/KILL bounds).
- `ArchivedServiceAuditTests.test_B4t_A1_real_P66_service_does_not_exec_during_a_claim` (T-43).
- `ArchivedServiceAuditTests.test_B4t_A1_real_V34_service_does_not_exec_during_a_claim` (T-43; requires the archived V34 source, plain offline skip if absent).

The residual stub fixture needs a C compiler; lack of one is an explicit skip,
not acceptance. It presents the trusted launch arguments with an unchanged
entry on disk. These tests record the accepted trust level and do not claim to
attest the running executable. Native fixture startup and these added scenarios
remain unvalidated here. Run on both supported platforms, with each supported
macOS interpreter for the I1–I4 read/NOTE_EXEC checks.

## Primary addendum and PTY regressions — exact names

In `codex:tests.test_project_start`:

- `ReleasedLedgerUpgradeTests.test_E2a_1_a_released_0_8_59_ledger_backs_up_restores_and_moves_after_the_upgrade` (HIGH-P1).
- `ReleasedLedgerUpgradeTests.test_E2c_10_a_released_0_8_59_ledger_opens_and_works_under_this_build` (same released-ledger upgrade path).
- `StartTests.test_E2c_7_another_version_s_live_host_is_not_replaced` (literal oracle restored).
- `OtherVersionTests.test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced` (MED-P3; fixture's ledger now originates under the earlier build rather than lowering a published floor).

PTY cases:

- `root:test_setup_wizard_direct`, `WizardDirectTests.test_an_earlier_versions_records_are_upgraded_only_when_approved` (MED-P2).
- `root:test_setup_update`, `SetupUpdateTests.test_B4t_the_busy_takeover_finishes_before_install_and_completes_after_service_start`.
- `root:test_setup_update`, `SetupUpdateTests.test_B4t_a_failed_service_stop_is_resumable_before_the_installer_writes`.

The owner approval wording has a passing real-Wizard component witness. The
0.8.59 owner-open upgrade has a passing offline witness using released source;
the full released-collector/backup/move case above is still owed.

## Mixed modules excluded from the offline copier pass

`codex:tests.test_learning_migration` ran its four offline classes: 15/15 pass
in 4.871 s. The primary must rerun these socket/CLI cases:

- `MigrationCliTests.test_command_moves_reports_refuses_and_rolls_back`.
- `MigrationCliTests.test_first_start_of_the_project_service_moves_the_history_by_itself`.

The live reply-producer soak also remains a primary named-module gate:
`codex:tests.test_reply_shape_live`, class `LiveReplyShapeTests` (all five checks:
replayed groups, exact table shapes, complete operation production, optional
field production, and no undeclared fields). Its transitive native groups
exceed the builder's allowed run size; component real-producer checks pass.

## Full native owner/lifecycle gate

Run `scripts/e2e_lifecycle.py --flows L11,L12 --jobs 1 --report <report>`.
This runs the `upd_all` scene group, including:

- `l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds` (strict business-row/schema comparison).
- `l12_3_update_with_a_running_child_goes_ahead_and_the_child_ends_lost`.
- `l12_3_finished_result_stays_recordable_after_the_update`.
- `l12_3_no_half_installed_file_at_any_step`.
- `l12_5_second_project_updated_after_the_first_works_and_the_first_still_works`.

The pre-existing full owner-command SIGKILL/start-path matrix is still a native
S5 gate: J1–J14 including J4a/J6a and internal stage-B cuts; bound/unbound/in-flight
opens; late admission, inherited locks, Q's real child, and overflow count across
crash/resume. The component/archive/mutation evidence must not be substituted
for those native scenarios. No native pass is claimed by this fix round.

`test_runtime_release` has the expected current-digest failure until the primary
assigns the release version/catalogue entry at merge; VERSION and the catalogue
remain untouched in this worktree.
