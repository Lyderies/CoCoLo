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
