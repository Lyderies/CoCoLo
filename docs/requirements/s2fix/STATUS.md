# s2fix status (2026-11-25): T7 S2 product findings, fix/t7s2-findings on 758a650c (batch 2), NOT installed

Findings from feat/t7-s2 (scripts/e2e_checks_recovery.py, docs/requirements/t7/S2-STATUS.md). Checks unchanged.
Base evidence: the S2 flows (`--flows L8,L14,L15 --jobs 2`) run on this base in a scratch copy with the S2 scripts
(/tmp/claude-501/s2fix/tree): L8.1, L8.2, both L8.3 GREEN already (B4s settles the lost run, the first close
succeeds); L14.1 RED (orchestrations_active 0), L15.1 RED (deleted project's service still running).

1. L8.3 close after a settled lost run: already met by B4s on this base (existing test
   test_account_settlement_driver.SettlementThroughTheDriverTests.test_A3; e2e L8.3 green). Remaining part fixed:
   the refusal's next step (`history_orchestration_pending`) said "restart the project's service"; it now names
   learning-run-status, waiting for running children (a lost one is settled by the next service), then the close.
   Test: test_refusal_steps.RefusalStepTests.test_L8_3_... (red -> green). Code: refusal_steps.EXACT.
2. L14.1 one truth:
   - learning-status orchestrations_* came from the ledger's `orchestrations` table (pre-project-store); now from the
     project store via a new owner-read op `history.orchestration_counts` (OrchestrationOwner.state_counts, which
     `_open_count` now also uses). cli learning-status replaces the ledger's three counts when the service answers.
   - learning-orchestration-report counted a settled lost attempt pending/current_active; it now counts it with the
     expired ones (abandoned), with the close's own reader (`ProjectHistory._attempt_settled`).
   Tests: test_account_settlement_driver.StatusAgreementTests (2, red -> green).
3. L15.1 deletion stops the project's service: project_host_client.stop_project_host (the hint-withdrawal path, any
   version, HINT_LOCK, bounded 10 s); cli learning-delete-project reports `project_service`
   (stopped/not_running/still_running). Tombstone and provider observations kept (accepted reading).
   Test: test_project_service_startup.ProjectServiceStartupTests.test_L15_1_... (red -> green).

Changed assertion: one (assertions.md), op-set pin in test_b2_service_outcome.AuthorityTests. Needs primary decision.
Red evidence: /tmp/claude-501/s2fix/red1.txt, red2.txt. Runtime digest NOT refreshed.

## Runs (final code, named modules, clean env, TMPDIR=/tmp/claude-501/s2fix/t)
- refusal_steps, reply_shape_compatibility, b2_service_outcome, holds_report, history_api, contracts,
  lifetime_cap_guard: 180 tests 24 s (only the pin failed before its listed change).
- b2_service_outcome(+_hosts,+_paths), project_orchestration, open_withdrawal_successor, cli_status_client_boundary,
  holds_report_socket, host_slots, project_service_startup, trial_run_driver_bugs, project_host_result, service_host:
  262 tests 258 s OK.
- account_settlement_driver, lost_run_settlement, learning_cli, host_commands, reply_shape_compatibility,
  b2_service_outcome, refusal_steps: 212 tests 287 s; 2 not this change: test_learning_cli
  test_trusted_host_lifecycle_opens_and_records_exact_assignment fails on the base too (KeyError reason_code);
  LostRunAfterCrashTests.test_a_crashed_hosts_child_that_still_runs... failed once under load, 2/2 green alone (27 s).
- test_project_driver alone: 99 tests 257 s OK.
- e2e S2 flows in the scratch copy with the fix (/tmp/claude-501/s2fix/fixed.json): L8.1, L8.2, L8.3 x2, L14.1,
  L15.1, G1-G5, G8, G9 all green, 257 s.

## Round 2 (Codex review NOT-READY, 2026-11-25), each test red first, then the fix
1. `history.orchestration_counts` is owner-only (service_host: separate branch, `host_client == OWNER_CLIENT`; the CLI
   asks with the owner label always). It reads active and paused ones only (`OrchestrationOwner.open_counts`): a
   row whose latest checkpoint's status is closed is named closed by SQLite without verifying it (no per-row
   checkpoint verify of closed rows, at most max_orchestrations + 1 other rows verified); closed = row count less
   those. Closed rows go after 90 days (purge_closed). lifetime_caps.json: `open_counts`. Tests:
   test_holds_report.OrchestrationCountsTests (2).
2. run-status no longer says `released` for an older `settled` journal entry whose project settlement is not yet
   recovered (`_settle_lost`: 'held' until `_recover_settled` is true), so it agrees with the report (pending).
   Test: ConfirmFixSweepTests.test_L14_1_an_old_settled_entry_is_released_in_run_status_only_once_its_recovery_is_done.
   learning-status with no running service shows the three counts null plus `orchestrations_unknown_reason`.
   Test: the one assertion changed below (test_learning_cli).
3. `stop_project_host(..., project_id=)`: a hint of another project id (newer registration at the same root) is left
   (`not_running`); a wedged host is `still_running`; hint-lock contention is `unknown` (never `not_running`). The CLI
   reports `ready: false` plus `reason` for still_running/unknown, and removes the project's history store
   (`project_service.remove_project_history`: under its writer lock, no symlink followed) once its service is
   stopped; a store still held is `ready: false`, `project_history_removed: false`. Tombstone and path-free provider
   observations (the ledger) are all that stays. Tests: test_project_service_startup test_L15_1_* (4 new).
- e2e S2 flows round 2 (scratch copy, /tmp/claude-501/s2fix/fixed2.json): L8.1, L8.2, L8.3 x2, L14.1, L15.1, G1-G5, G8, G9 all green, 255 s. Named modules round 2: holds_report/b2/refusal/history_api/contracts/project_orchestration/cli_status/lifetime guard 249 tests 36 s; settlement_driver+lost_run+learning_cli+host_commands+holds_socket+service_host+host_result+open_withdrawal 209 tests 302 s (1 known base failure KeyError reason_code); project_service_startup 12 tests 23 s; test_project_driver 99 tests 248 s OK.

## Round 3 (decision 185, 4 fixed-scope fixes; accepted on red -> green)
1. Counts subject-scoped: service_host `_authorize_history` puts history.orchestration_counts with the other project reads (any enrolled primary of THIS project, or owner); cli learning-status asks with its real label. Tests: test_b2_service_outcome_hosts.OrchestrationCountsAccessTests (red /tmp/claude-501/s2fix/red3-1.txt).
2. Closed-row scan: no status column exists (project_orchestrations has only handle/scope/sequence/digest); bounding needs a new indexed column = schema step -> rides B6 (decision 131). Skipped measurement test test_holds_report.OrchestrationCountsCostTests with the cause. NOT fixed.
3. Store deletion: project_service._safe_store_directory opens state root -> projects -> project id per component, no-follow, each a directory of this user; history.lock is kept (never unlinked while held). Tests: tests/test_project_store_removal.py (red3-2.txt).
4. Cleanup retry: marker file <state>/pending-history-cleanup/<project id> (not a ledger column: that would be a schema step) written at deletion, cleared when removal succeeds; learning-delete-project with the same --confirm-project-id and root resumes; learning-status says `project_cleanup_pending`. Test: test_project_service_startup ..._busy_history_writer_leaves_a_pending_cleanup... (collector-sweep retry not added).
