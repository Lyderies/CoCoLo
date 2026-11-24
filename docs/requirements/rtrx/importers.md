# Affected importer and fixture modules

No importer run was started: the required stop on an additional existing-assertion conflict happened before production implementation.
Inventory is conservative: direct routing/admission imports, original-open references, and reverse test-fixture dependency closure.
Socket detection is static and includes fixture dependencies; it is a primary-run candidate list, not a claim of an observed sandbox failure.

| Module | Direct import | Open reference | Socket dependency | Result |
| --- | --- | --- | --- | --- |
| codex:tests.b2_extra_runs |  | yes | yes | not run: required conflict stop |
| codex:tests.project_supervisor_fixture |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_billing_admission |  |  |  | not run: required conflict stop |
| codex:tests.test_account_billing_execution |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_billing_jobs |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_claim_jobs |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_claim_operation |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_claim_storage |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_cleanup_release |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_cleanup_schema |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_ingress |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_lane_restart |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_launch_claims |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_observation_failure |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_observation_runs |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_observation_schema |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_open_schema |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_original_open |  | yes | yes | not run: required conflict stop |
| codex:tests.test_account_slot_health |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_store_recovery |  |  | yes | not run: required conflict stop |
| codex:tests.test_account_terminal_delivery |  |  | yes | not run: required conflict stop |
| codex:tests.test_active_run_capacity |  | yes | yes | not run: required conflict stop |
| codex:tests.test_admission_history_growth |  | yes | yes | not run: required conflict stop |
| codex:tests.test_admission_inputs |  | yes |  | not run: required conflict stop |
| codex:tests.test_admission_metadata | yes |  |  | not run: required conflict stop |
| codex:tests.test_append_lookup |  |  | yes | not run: required conflict stop |
| codex:tests.test_b2_service_outcome |  |  | yes | not run: required conflict stop |
| codex:tests.test_b2_service_outcome_hosts |  |  | yes | not run: required conflict stop |
| codex:tests.test_b2_service_outcome_paths |  |  | yes | not run: required conflict stop |
| codex:tests.test_b2_service_outcome_round2 |  |  | yes | not run: required conflict stop |
| codex:tests.test_b2_service_outcome_sweep |  |  | yes | not run: required conflict stop |
| codex:tests.test_backup_installed_project |  |  | yes | not run: required conflict stop |
| codex:tests.test_billing_gate | yes |  | yes | not run: required conflict stop |
| codex:tests.test_billing_probe_worker |  |  | yes | not run: required conflict stop |
| codex:tests.test_billing_rejudge |  |  | yes | not run: required conflict stop |
| codex:tests.test_billing_writers |  |  | yes | not run: required conflict stop |
| codex:tests.test_binding_lookup |  |  | yes | not run: required conflict stop |
| codex:tests.test_bridge | yes | yes | yes | not run: required conflict stop |
| codex:tests.test_capacity_status_api |  |  | yes | not run: required conflict stop |
| codex:tests.test_claim_denial_recovery |  |  | yes | not run: required conflict stop |
| codex:tests.test_claude_activity |  |  | yes | not run: required conflict stop |
| codex:tests.test_claude_init_plugins |  |  | yes | not run: required conflict stop |
| codex:tests.test_claude_structured_output |  |  | yes | not run: required conflict stop |
| codex:tests.test_cleanup_contracts |  |  | yes | not run: required conflict stop |
| codex:tests.test_cleanup_custody |  |  | yes | not run: required conflict stop |
| codex:tests.test_cleanup_receipt_lifetime |  |  | yes | not run: required conflict stop |
| codex:tests.test_cli_billing_runtime |  |  | yes | not run: required conflict stop |
| codex:tests.test_cli_open_client_runtime |  |  | yes | not run: required conflict stop |
| codex:tests.test_codex_activity_execution |  |  | yes | not run: required conflict stop |
| codex:tests.test_codex_preparation |  |  | yes | not run: required conflict stop |
| codex:tests.test_codex_verifier_preparation |  |  | yes | not run: required conflict stop |
| codex:tests.test_codex_worker |  |  | yes | not run: required conflict stop |
| codex:tests.test_collector_read_scope |  |  | yes | not run: required conflict stop |
| codex:tests.test_collector_stale |  |  | yes | not run: required conflict stop |
| codex:tests.test_container_recovery |  |  | yes | not run: required conflict stop |
| codex:tests.test_contracts | yes |  |  | not run: required conflict stop |
| codex:tests.test_deadlines_retention |  |  | yes | not run: required conflict stop |
| codex:tests.test_diagnostic_delivery |  |  | yes | not run: required conflict stop |
| codex:tests.test_dispatch_client_runtime |  |  | yes | not run: required conflict stop |
| codex:tests.test_dispatch_preflight | yes |  |  | not run: required conflict stop |
| codex:tests.test_evaluation | yes |  |  | not run: required conflict stop |
| codex:tests.test_event_retention |  |  | yes | not run: required conflict stop |
| codex:tests.test_event_shapes |  |  | yes | not run: required conflict stop |
| codex:tests.test_health_containers |  |  | yes | not run: required conflict stop |
| codex:tests.test_health_monitor |  |  | yes | not run: required conflict stop |
| codex:tests.test_hold_lifecycle |  |  | yes | not run: required conflict stop |
| codex:tests.test_host_open_lane |  |  | yes | not run: required conflict stop |
| codex:tests.test_host_open_provenance |  | yes | yes | not run: required conflict stop |
| codex:tests.test_host_slots |  |  | yes | not run: required conflict stop |
| codex:tests.test_large_project_backups |  |  | yes | not run: required conflict stop |
| codex:tests.test_launch_signature_preparation |  |  |  | not run: required conflict stop |
| codex:tests.test_learning_chunk_membership |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_cli |  | yes | yes | not run: required conflict stop |
| codex:tests.test_learning_cycle_core |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_engine | yes |  | yes | not run: required conflict stop |
| codex:tests.test_learning_migration |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_outcome_pages |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_qa_upgrade |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_retention |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_source_image |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_source_image_legacy |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_source_transfer |  |  | yes | not run: required conflict stop |
| codex:tests.test_learning_store |  | yes | yes | not run: required conflict stop |
| codex:tests.test_ledger_busy_answers |  | yes | yes | not run: required conflict stop |
| codex:tests.test_ledger_lock_limits |  |  | yes | not run: required conflict stop |
| codex:tests.test_legacy_capacity_upgrade |  |  | yes | not run: required conflict stop |
| codex:tests.test_lost_run_settlement |  |  | yes | not run: required conflict stop |
| codex:tests.test_open_request_lifetime |  |  | yes | not run: required conflict stop |
| codex:tests.test_open_withdrawal |  |  | yes | not run: required conflict stop |
| codex:tests.test_open_withdrawal_successor |  |  | yes | not run: required conflict stop |
| codex:tests.test_owned_execution |  |  | yes | not run: required conflict stop |
| codex:tests.test_owner_backup_streamed |  |  | yes | not run: required conflict stop |
| codex:tests.test_owner_terminal_flow |  |  | yes | not run: required conflict stop |
| codex:tests.test_prepared_dispatch |  |  | yes | not run: required conflict stop |
| codex:tests.test_probe_failure_causes |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_cleanup |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_driver |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_execution_grant |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_execution_processes |  | yes | yes | not run: required conflict stop |
| codex:tests.test_project_hold_register |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_holds_schema |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_host_open |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_host_result |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_ingress |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_learning_corpus |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_learning_identity |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_learning_qa |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_open_scope |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_operational_qa |  | yes | yes | not run: required conflict stop |
| codex:tests.test_project_qa_check_receipts |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_qa_commit |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_release_ack |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_request_queue |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_result_usage |  |  | yes | not run: required conflict stop |
| codex:tests.test_project_worker_result |  |  | yes | not run: required conflict stop |
| codex:tests.test_providers | yes | yes |  | not run: required conflict stop |
| codex:tests.test_publication_replay |  |  | yes | not run: required conflict stop |
| codex:tests.test_publication_replay_driver |  |  | yes | not run: required conflict stop |
| codex:tests.test_publication_replay_proofs |  |  | yes | not run: required conflict stop |
| codex:tests.test_publication_retry |  |  | yes | not run: required conflict stop |
| codex:tests.test_record_context |  |  | yes | not run: required conflict stop |
| codex:tests.test_refusal_steps |  |  | yes | not run: required conflict stop |
| codex:tests.test_required_host_open |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_commit_busy |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_denial_lane |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_lease_lane |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_leases |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_lifetime |  |  | yes | not run: required conflict stop |
| codex:tests.test_result_persistence |  |  | yes | not run: required conflict stop |
| codex:tests.test_retirement_sweep |  |  | yes | not run: required conflict stop |
| codex:tests.test_review_qa |  |  | yes | not run: required conflict stop |
| codex:tests.test_review_runner |  |  | yes | not run: required conflict stop |
| codex:tests.test_review_runner_provenance |  |  | yes | not run: required conflict stop |
| codex:tests.test_review_verdict_required |  |  | yes | not run: required conflict stop |
| codex:tests.test_routing | yes |  |  | not run: required conflict stop |
| codex:tests.test_routing_judgment | yes |  |  | not run: required conflict stop |
| codex:tests.test_routing_risk | yes |  |  | not run: required conflict stop |
| codex:tests.test_rsa_step3 |  |  | yes | not run: required conflict stop |
| codex:tests.test_run_holds |  | yes | yes | not run: required conflict stop |
| codex:tests.test_run_provenance |  |  | yes | not run: required conflict stop |
| codex:tests.test_signature_execution |  |  |  | not run: required conflict stop |
| codex:tests.test_signature_input_wire |  |  |  | not run: required conflict stop |
| codex:tests.test_signature_inputs |  | yes |  | not run: required conflict stop |
| codex:tests.test_signature_job_intake |  | yes |  | not run: required conflict stop |
| codex:tests.test_signature_payloads |  |  |  | not run: required conflict stop |
| codex:tests.test_signature_process |  |  | yes | not run: required conflict stop |
| codex:tests.test_signature_worker_pool |  |  |  | not run: required conflict stop |
| codex:tests.test_state_location |  |  | yes | not run: required conflict stop |
| codex:tests.test_status_write_frequency |  |  | yes | not run: required conflict stop |
| codex:tests.test_synchronized_races |  | yes | yes | not run: required conflict stop |
| codex:tests.test_trial_run_driver_bugs |  |  | yes | not run: required conflict stop |
| codex:tests.test_type_lookups |  |  | yes | not run: required conflict stop |
| codex:tests.test_validation_cli_client_boundary |  |  |  | not run: required conflict stop |
| root:test_setup_wizard_steps | yes |  |  | not run: required conflict stop |


## Restart result (current)

Eight core modules executed; see [restart-results.json](restart-results.json). Read-only affected selection chooses all 331 runnable modules; the following 323 were not executed at the required additional-conflict stop. This is not a full affected run. Primary follow-up after resolution: routing/admission/provider importer modules first, then the remaining selected modules.

- `claude:tests.test_contracts`
- `claude:tests.test_job_read_tool`
- `claude:tests.test_journal`
- `claude:tests.test_native_fixtures`
- `claude:tests.test_package_cli_state`
- `claude:tests.test_package_writes`
- `claude:tests.test_packaging`
- `claude:tests.test_previous_release`
- `claude:tests.test_previous_release_catalogue`
- `claude:tests.test_previous_release_recorded`
- `claude:tests.test_stream_projection`
- `claude:tests.test_subscription_stream`
- `claude:tests.test_telemetry`
- `codex:tests.test_account_api`
- `codex:tests.test_account_billing_admission`
- `codex:tests.test_account_billing_execution`
- `codex:tests.test_account_billing_jobs`
- `codex:tests.test_account_claim_jobs`
- `codex:tests.test_account_claim_operation`
- `codex:tests.test_account_claim_storage`
- `codex:tests.test_account_cleanup_release`
- `codex:tests.test_account_cleanup_schema`
- `codex:tests.test_account_host`
- `codex:tests.test_account_ingress`
- `codex:tests.test_account_lane_restart`
- `codex:tests.test_account_launch_claims`
- `codex:tests.test_account_observation`
- `codex:tests.test_account_observation_backup`
- `codex:tests.test_account_observation_failure`
- `codex:tests.test_account_observation_jobs`
- `codex:tests.test_account_observation_runs`
- `codex:tests.test_account_observation_schema`
- `codex:tests.test_account_open_schema`
- `codex:tests.test_account_original_open`
- `codex:tests.test_account_restart`
- `codex:tests.test_account_slot_health`
- `codex:tests.test_account_store_recovery`
- `codex:tests.test_account_terminal_delivery`
- `codex:tests.test_active_run_capacity`
- `codex:tests.test_admission_history_growth`
- `codex:tests.test_admission_input_decode`
- `codex:tests.test_admission_inputs`
- `codex:tests.test_app_cli_client_boundary`
- `codex:tests.test_app_server`
- `codex:tests.test_append_lookup`
- `codex:tests.test_atomic_rename`
- `codex:tests.test_b2_service_outcome`
- `codex:tests.test_b2_service_outcome_hosts`
- `codex:tests.test_b2_service_outcome_paths`
- `codex:tests.test_b2_service_outcome_round2`
- `codex:tests.test_b2_service_outcome_sweep`
- `codex:tests.test_backup_installed_project`
- `codex:tests.test_billing_gate`
- `codex:tests.test_billing_probe_api`
- `codex:tests.test_billing_probe_worker`
- `codex:tests.test_billing_rejudge`
- `codex:tests.test_billing_writers`
- `codex:tests.test_binding_lookup`
- `codex:tests.test_bridge`
- `codex:tests.test_bridge_contract_boundary`
- `codex:tests.test_bridge_deadline_delivery`
- `codex:tests.test_bridge_liveness`
- `codex:tests.test_capacity_status_api`
- `codex:tests.test_catalog`
- `codex:tests.test_claim_denial_recovery`
- `codex:tests.test_claim_lookup_growth`
- `codex:tests.test_claude_activity`
- `codex:tests.test_claude_init_plugins`
- `codex:tests.test_claude_structured_output`
- `codex:tests.test_cleanup_contracts`
- `codex:tests.test_cleanup_custody`
- `codex:tests.test_cleanup_receipt_lifetime`
- `codex:tests.test_cli_billing_runtime`
- `codex:tests.test_cli_ledger_boundary`
- `codex:tests.test_cli_observation_runtime`
- `codex:tests.test_cli_open_client_runtime`
- `codex:tests.test_cli_reports`
- `codex:tests.test_cli_status_client_boundary`
- `codex:tests.test_client_hint_cause`
- `codex:tests.test_codex_activity`
- `codex:tests.test_codex_activity_execution`
- `codex:tests.test_codex_preparation`
- `codex:tests.test_codex_verifier_preparation`
- `codex:tests.test_codex_worker`
- `codex:tests.test_collector_exit`
- `codex:tests.test_collector_paths`
- `codex:tests.test_collector_read_scope`
- `codex:tests.test_collector_stale`
- `codex:tests.test_container_launch_journal`
- `codex:tests.test_container_recovery`
- `codex:tests.test_container_runtime`
- `codex:tests.test_container_stop`
- `codex:tests.test_contracts`
- `codex:tests.test_control_api`
- `codex:tests.test_control_loop`
- `codex:tests.test_control_process`
- `codex:tests.test_core_api_events`
- `codex:tests.test_core_api_hardening`
- `codex:tests.test_core_api_reads`
- `codex:tests.test_core_api_round3`
- `codex:tests.test_core_api_round4`
- `codex:tests.test_core_api_round5`
- `codex:tests.test_core_api_round6`
- `codex:tests.test_core_api_talk_control`
- `codex:tests.test_core_api_terminal`
- `codex:tests.test_core_api_transport`
- `codex:tests.test_deadlines_retention`
- `codex:tests.test_diagnostic_delivery`
- `codex:tests.test_dispatch_client_runtime`
- `codex:tests.test_dispatch_delivery_cli`
- `codex:tests.test_dispatch_preflight`
- `codex:tests.test_dispatch_wait`
- `codex:tests.test_evaluation`
- `codex:tests.test_event_retention`
- `codex:tests.test_event_shapes`
- `codex:tests.test_execution_activity`
- `codex:tests.test_execution_limits`
- `codex:tests.test_gate_policy`
- `codex:tests.test_git_plumbing`
- `codex:tests.test_hardened_git`
- `codex:tests.test_health_containers`
- `codex:tests.test_health_contention`
- `codex:tests.test_health_monitor`
- `codex:tests.test_history_api`
- `codex:tests.test_history_service_upgrade`
- `codex:tests.test_hold_lifecycle`
- `codex:tests.test_hook_client_boundary`
- `codex:tests.test_hook_config`
- `codex:tests.test_host_commands`
- `codex:tests.test_host_open_lane`
- `codex:tests.test_host_open_provenance`
- `codex:tests.test_host_paths`
- `codex:tests.test_host_slots`
- `codex:tests.test_hosts`
- `codex:tests.test_installer`
- `codex:tests.test_lane_health`
- `codex:tests.test_large_project_backups`
- `codex:tests.test_launch_journal`
- `codex:tests.test_launch_receipts_layout`
- `codex:tests.test_launch_signature_preparation`
- `codex:tests.test_learning_adapters`
- `codex:tests.test_learning_analysis`
- `codex:tests.test_learning_benchmark`
- `codex:tests.test_learning_chunk_membership`
- `codex:tests.test_learning_chunk_upgrade`
- `codex:tests.test_learning_cli`
- `codex:tests.test_learning_contracts`
- `codex:tests.test_learning_cycle_core`
- `codex:tests.test_learning_engine`
- `codex:tests.test_learning_hook`
- `codex:tests.test_learning_live`
- `codex:tests.test_learning_migration`
- `codex:tests.test_learning_outcome_pages`
- `codex:tests.test_learning_qa_upgrade`
- `codex:tests.test_learning_retention`
- `codex:tests.test_learning_source_image`
- `codex:tests.test_learning_source_image_legacy`
- `codex:tests.test_learning_source_transfer`
- `codex:tests.test_learning_store`
- `codex:tests.test_ledger_busy_answers`
- `codex:tests.test_ledger_lock_limits`
- `codex:tests.test_ledger_startup`
- `codex:tests.test_ledger_waits`
- `codex:tests.test_legacy_capacity_upgrade`
- `codex:tests.test_legacy_history_compatibility`
- `codex:tests.test_lifecycle_cli_runtime`
- `codex:tests.test_lifetime_cap_guard`
- `codex:tests.test_liveness_clock`
- `codex:tests.test_lost_run_settlement`
- `codex:tests.test_merge_library_adversarial`
- `codex:tests.test_merge_library_contract`
- `codex:tests.test_merge_library_disk`
- `codex:tests.test_merge_library_forward_faults`
- `codex:tests.test_merge_library_harness`
- `codex:tests.test_merge_library_kills`
- `codex:tests.test_merge_library_machine`
- `codex:tests.test_merge_library_model_sweep`
- `codex:tests.test_merge_library_mutations`
- `codex:tests.test_merge_library_owners`
- `codex:tests.test_merge_library_r2`
- `codex:tests.test_merge_library_r3`
- `codex:tests.test_merge_library_recovery`
- `codex:tests.test_merge_library_recovery_faults`
- `codex:tests.test_merge_library_review`
- `codex:tests.test_merge_library_states`
- `codex:tests.test_merge_library_table`
- `codex:tests.test_open_request_lifetime`
- `codex:tests.test_open_withdrawal`
- `codex:tests.test_open_withdrawal_successor`
- `codex:tests.test_owned_execution`
- `codex:tests.test_owner_backup_streamed`
- `codex:tests.test_owner_key_contracts`
- `codex:tests.test_owner_record_payload`
- `codex:tests.test_owner_terminal_flow`
- `codex:tests.test_phase_report`
- `codex:tests.test_phase_report_exit`
- `codex:tests.test_prepared_dispatch`
- `codex:tests.test_probe_failure_causes`
- `codex:tests.test_project_cleanup`
- `codex:tests.test_project_cold_start_r4`
- `codex:tests.test_project_driver`
- `codex:tests.test_project_execution_grant`
- `codex:tests.test_project_execution_processes`
- `codex:tests.test_project_history`
- `codex:tests.test_project_hold_register`
- `codex:tests.test_project_holds_schema`
- `codex:tests.test_project_host_open`
- `codex:tests.test_project_host_result`
- `codex:tests.test_project_ingress`
- `codex:tests.test_project_learning_corpus`
- `codex:tests.test_project_learning_identity`
- `codex:tests.test_project_learning_live`
- `codex:tests.test_project_learning_qa`
- `codex:tests.test_project_monitor`
- `codex:tests.test_project_open_scope`
- `codex:tests.test_project_operational_qa`
- `codex:tests.test_project_orchestration`
- `codex:tests.test_project_qa_check_receipts`
- `codex:tests.test_project_qa_commit`
- `codex:tests.test_project_release_ack`
- `codex:tests.test_project_request_queue`
- `codex:tests.test_project_result_usage`
- `codex:tests.test_project_results`
- `codex:tests.test_project_runtime`
- `codex:tests.test_project_service`
- `codex:tests.test_project_service_startup`
- `codex:tests.test_project_worker_result`
- `codex:tests.test_publication_replay`
- `codex:tests.test_publication_replay_driver`
- `codex:tests.test_publication_replay_proofs`
- `codex:tests.test_publication_retry`
- `codex:tests.test_qa_security_diagnostics`
- `codex:tests.test_qa_tree_scan`
- `codex:tests.test_rate_limit_contracts`
- `codex:tests.test_record_context`
- `codex:tests.test_refusal_steps`
- `codex:tests.test_release_batch_queries`
- `codex:tests.test_required_host_open`
- `codex:tests.test_result_commit_busy`
- `codex:tests.test_result_denial_lane`
- `codex:tests.test_result_diagnostics`
- `codex:tests.test_result_lease_lane`
- `codex:tests.test_result_leases`
- `codex:tests.test_result_lifetime`
- `codex:tests.test_result_persistence`
- `codex:tests.test_result_publication`
- `codex:tests.test_result_purge_flow`
- `codex:tests.test_result_read_api`
- `codex:tests.test_retirement_sweep`
- `codex:tests.test_review_qa`
- `codex:tests.test_review_runner`
- `codex:tests.test_review_runner_provenance`
- `codex:tests.test_review_verdict_required`
- `codex:tests.test_rsa_step3`
- `codex:tests.test_run_driver_retries`
- `codex:tests.test_run_holds`
- `codex:tests.test_run_network`
- `codex:tests.test_run_provenance`
- `codex:tests.test_runtime_release`
- `codex:tests.test_service_harness`
- `codex:tests.test_service_host`
- `codex:tests.test_service_ownership_audit`
- `codex:tests.test_service_protocol`
- `codex:tests.test_service_queue`
- `codex:tests.test_service_transport`
- `codex:tests.test_session_guard`
- `codex:tests.test_shutdown_watchdog`
- `codex:tests.test_signature_execution`
- `codex:tests.test_signature_input_wire`
- `codex:tests.test_signature_inputs`
- `codex:tests.test_signature_job_intake`
- `codex:tests.test_signature_payloads`
- `codex:tests.test_signature_process`
- `codex:tests.test_signature_worker_pool`
- `codex:tests.test_sigterm_stress`
- `codex:tests.test_skill_commands`
- `codex:tests.test_sql_read_budget`
- `codex:tests.test_state_location`
- `codex:tests.test_status_write_frequency`
- `codex:tests.test_supervision`
- `codex:tests.test_synchronized_races`
- `codex:tests.test_trial_run_driver_bugs`
- `codex:tests.test_type_lookups`
- `codex:tests.test_usage_report`
- `codex:tests.test_validation`
- `codex:tests.test_validation_cli_client_boundary`
- `codex:tests.test_watchdog_lifetime`
- `codex:tests.test_write_merge`
- `root:test_assertion_diff`
- `root:test_attest`
- `root:test_check_identifiers`
- `root:test_ci_stages`
- `root:test_e2e_requirements`
- `root:test_e2e_strictness`
- `root:test_entrypoint_inventory`
- `root:test_identifiers_parity`
- `root:test_install`
- `root:test_install_claude_upgrade`
- `root:test_install_report_validation`
- `root:test_login_start`
- `root:test_measure_test_behaviour`
- `root:test_measure_test_behaviour_probe`
- `root:test_owner_tool_bounds`
- `root:test_setup_sh`
- `root:test_setup_straight`
- `root:test_setup_update`
- `root:test_setup_wizard_direct`
- `root:test_setup_wizard_effects`
- `root:test_setup_wizard_git`
- `root:test_setup_wizard_progress`
- `root:test_setup_wizard_refusals`
- `root:test_setup_wizard_source`
- `root:test_setup_wizard_steps`
- `root:test_setup_wizard_terminal`
- `root:test_test_framework`
- `root:test_test_framework_affected`
- `root:test_test_framework_guards`
- `root:test_test_framework_integration`
- `root:test_test_framework_r2`
- `root:test_test_framework_r3`
- `root:test_test_framework_review`
- `root:test_test_stages`
- `root:test_wizard_clock`

### Deferred socket-dependent importer modules (135)

These are static dependency flags from the preserved inventory, not observed socket permission failures. No socket tests were found impossible to run because none were attempted after the stop.

- `codex:tests.b2_extra_runs`
- `codex:tests.project_supervisor_fixture`
- `codex:tests.test_account_billing_execution`
- `codex:tests.test_account_billing_jobs`
- `codex:tests.test_account_claim_jobs`
- `codex:tests.test_account_claim_operation`
- `codex:tests.test_account_claim_storage`
- `codex:tests.test_account_cleanup_release`
- `codex:tests.test_account_cleanup_schema`
- `codex:tests.test_account_ingress`
- `codex:tests.test_account_lane_restart`
- `codex:tests.test_account_launch_claims`
- `codex:tests.test_account_observation_failure`
- `codex:tests.test_account_observation_runs`
- `codex:tests.test_account_observation_schema`
- `codex:tests.test_account_open_schema`
- `codex:tests.test_account_original_open`
- `codex:tests.test_account_slot_health`
- `codex:tests.test_account_store_recovery`
- `codex:tests.test_account_terminal_delivery`
- `codex:tests.test_active_run_capacity`
- `codex:tests.test_admission_history_growth`
- `codex:tests.test_append_lookup`
- `codex:tests.test_b2_service_outcome`
- `codex:tests.test_b2_service_outcome_hosts`
- `codex:tests.test_b2_service_outcome_paths`
- `codex:tests.test_b2_service_outcome_round2`
- `codex:tests.test_b2_service_outcome_sweep`
- `codex:tests.test_backup_installed_project`
- `codex:tests.test_billing_gate`
- `codex:tests.test_billing_probe_worker`
- `codex:tests.test_billing_rejudge`
- `codex:tests.test_billing_writers`
- `codex:tests.test_binding_lookup`
- `codex:tests.test_bridge`
- `codex:tests.test_capacity_status_api`
- `codex:tests.test_claim_denial_recovery`
- `codex:tests.test_claude_activity`
- `codex:tests.test_claude_init_plugins`
- `codex:tests.test_claude_structured_output`
- `codex:tests.test_cleanup_contracts`
- `codex:tests.test_cleanup_custody`
- `codex:tests.test_cleanup_receipt_lifetime`
- `codex:tests.test_cli_billing_runtime`
- `codex:tests.test_cli_open_client_runtime`
- `codex:tests.test_codex_activity_execution`
- `codex:tests.test_codex_preparation`
- `codex:tests.test_codex_verifier_preparation`
- `codex:tests.test_codex_worker`
- `codex:tests.test_collector_read_scope`
- `codex:tests.test_collector_stale`
- `codex:tests.test_container_recovery`
- `codex:tests.test_deadlines_retention`
- `codex:tests.test_diagnostic_delivery`
- `codex:tests.test_dispatch_client_runtime`
- `codex:tests.test_event_retention`
- `codex:tests.test_event_shapes`
- `codex:tests.test_health_containers`
- `codex:tests.test_health_monitor`
- `codex:tests.test_hold_lifecycle`
- `codex:tests.test_host_open_lane`
- `codex:tests.test_host_open_provenance`
- `codex:tests.test_host_slots`
- `codex:tests.test_large_project_backups`
- `codex:tests.test_learning_chunk_membership`
- `codex:tests.test_learning_cli`
- `codex:tests.test_learning_cycle_core`
- `codex:tests.test_learning_engine`
- `codex:tests.test_learning_migration`
- `codex:tests.test_learning_outcome_pages`
- `codex:tests.test_learning_qa_upgrade`
- `codex:tests.test_learning_retention`
- `codex:tests.test_learning_source_image`
- `codex:tests.test_learning_source_image_legacy`
- `codex:tests.test_learning_source_transfer`
- `codex:tests.test_learning_store`
- `codex:tests.test_ledger_busy_answers`
- `codex:tests.test_ledger_lock_limits`
- `codex:tests.test_legacy_capacity_upgrade`
- `codex:tests.test_lost_run_settlement`
- `codex:tests.test_open_request_lifetime`
- `codex:tests.test_open_withdrawal`
- `codex:tests.test_open_withdrawal_successor`
- `codex:tests.test_owned_execution`
- `codex:tests.test_owner_backup_streamed`
- `codex:tests.test_owner_terminal_flow`
- `codex:tests.test_prepared_dispatch`
- `codex:tests.test_probe_failure_causes`
- `codex:tests.test_project_cleanup`
- `codex:tests.test_project_driver`
- `codex:tests.test_project_execution_grant`
- `codex:tests.test_project_execution_processes`
- `codex:tests.test_project_hold_register`
- `codex:tests.test_project_holds_schema`
- `codex:tests.test_project_host_open`
- `codex:tests.test_project_host_result`
- `codex:tests.test_project_ingress`
- `codex:tests.test_project_learning_corpus`
- `codex:tests.test_project_learning_identity`
- `codex:tests.test_project_learning_qa`
- `codex:tests.test_project_open_scope`
- `codex:tests.test_project_operational_qa`
- `codex:tests.test_project_qa_check_receipts`
- `codex:tests.test_project_qa_commit`
- `codex:tests.test_project_release_ack`
- `codex:tests.test_project_request_queue`
- `codex:tests.test_project_result_usage`
- `codex:tests.test_project_worker_result`
- `codex:tests.test_publication_replay`
- `codex:tests.test_publication_replay_driver`
- `codex:tests.test_publication_replay_proofs`
- `codex:tests.test_publication_retry`
- `codex:tests.test_record_context`
- `codex:tests.test_refusal_steps`
- `codex:tests.test_required_host_open`
- `codex:tests.test_result_commit_busy`
- `codex:tests.test_result_denial_lane`
- `codex:tests.test_result_lease_lane`
- `codex:tests.test_result_leases`
- `codex:tests.test_result_lifetime`
- `codex:tests.test_result_persistence`
- `codex:tests.test_retirement_sweep`
- `codex:tests.test_review_qa`
- `codex:tests.test_review_runner`
- `codex:tests.test_review_runner_provenance`
- `codex:tests.test_review_verdict_required`
- `codex:tests.test_rsa_step3`
- `codex:tests.test_run_holds`
- `codex:tests.test_run_provenance`
- `codex:tests.test_signature_process`
- `codex:tests.test_state_location`
- `codex:tests.test_status_write_frequency`
- `codex:tests.test_synchronized_races`
- `codex:tests.test_trial_run_driver_bugs`
- `codex:tests.test_type_lookups`
