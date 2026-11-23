# B2 requirement map (DESIGN-0.4.0-B2-outcome.md draft 2 + s2; owner decision 92 A/A/A)
Code: packages/codex-agents/native-package/.codex/orchestration/codex_agents/ (RT). Tests: packages/codex-agents/tests/test_b2_service_outcome.py. Lines at the B2 production commit.

| Req | Code (file:line) | Tests |
|---|---|---|
| B2-1 | result_persistence.py:1013,1018 (_record_published after publish) and :1158 (record_pass in the expire request: restart/backstop); project_history.py:2687 admit_service_result | test_service_records_committed_terminal_without_primary; test_orchestration_closes_after_primary_never_returns |
| B2-2 | project_history.py:2712 _admit_host_result (one body for both entries; no recorder input to the records) | test_outcome_digest_equal_whoever_records (measured, byte-identical via two copies of one store) |
| B2-3 | service_record.py:97 ServiceRecordAuthority.check (a-e), project_history.py:2702 (before I/O) and :2808,2846 | test_each_break_is_refused..., test_context_in_any_state_but_committed_is_refused, test_a_tampered_body_is_refused... |
| B2-4 | project_history.py:2864 _known_receipt (K2 read-only; works after close); the existing history_result_expired stands after retirement | test_primary_retrieval_idempotent_after_service_record; test_retrieval_after_normal_close |
| B2-5 | client_runtime.py:942 RECHECK_REASONS, :1162, :1180 (named, before any admit call); host_client_mismatch and dispatch_not_found unchanged | test_recheck_refusals_are_named_and_write_nothing (reason set + source; NOT a live client run: see report) |
| B2-6 | history_api.py HISTORY_OPERATIONS unchanged; admit_service_result has no HistoryRequest, no mention in history_api/history_service/service_host | test_operation_set_unchanged; test_service_entry_not_reachable_over_socket; test_unenrolled_owner_and_other_class_refused |
| B2-7 | project_history.py:2712-2810 (existing digest/terminal/reference checks stay); service_record.py insert_recorder pattern check | test_forged_and_replayed_outcomes_refused (a-g) |
| B2-8 | project_history.py:2840-2860 (receipt looked up first inside the transaction; the loser writes nothing and takes K2) | test_service_and_primary_race_exactly_one_record (both orders) |
| B2-9 | service_record.py:175 record_pass; project_history.py flock (history_writer_busy); service_record.py:97 other_runtime; history_service.py (primary passes the writer's runtime) | test_restart_a..e |
| B2-10 | service_record.py:253 recording_status (store level) | test_status_shows_recorder_and_pending; test_a_receipt_with_no_recorder_row...; test_attribution_survives_reservation_retirement |
| B2-11 | service_record.py (no QA writes, no accepted); project_result_contexts.stored_*_context unchanged | test_service_record_feeds_verifier_open; test_service_never_writes_qa_or_accepted; test_unreviewed_marker_in_status |
| B2-12 | service_record.py:168 WINDOW_SQL, :175 record_pass (cap 10, window 160, cursor over committed-context rowids) | test_service_recording_past_every_limit; test_recording_pass_cost_with_tombstones |
| B2-13 | service_record.py UNRECORDABLE + check (orchestration closed/interrupted/expired); same at both entries | test_closed_or_expired_not_recorded |
| B2-14 | service_record.py:85-165; project_history.py:2665 (default instance runs when omitted), :2687 (required keyword), :2712 (type test) | test_final_check_required_and_typed; test_final_check_refuses_each_state; test_final_check_is_load_bearing; test_primary_omitting_final_check_still_runs_the_predicate |
| B2-15 | project_history.py:2837 _after_body_io seam; in-transaction check :2846 | test_close_vs_record_barrier; test_idle_end_vs_record_barrier; test_expiry_vs_record_barrier; test_retirement_and_lost_owner...; test_admission_first_then_close... |
| B2-16 | project_history.py:2864 (K1/K2/K3) | test_known_receipt_branches |
| B2-17 | service_record.py:31 schema, :68 insert_recorder (plain INSERT in the K1 transaction only); receipt bytes untouched | test_recorder_first_winner_immutable; test_receipt_versions_1_and_2_unchanged |
| B2-18 | service_record.py:175-250 (permanent set, backoff 1 min doubling to 30 min, health signals) | test_selection_invariants; test_permanent_refusals_do_not_starve; test_transient_retry_backoff; test_refusal_health_signal |
| Schema 19 -> 20 | project_history.py:64,201-203,473,509,539 | SchemaStepTests (3) |

Deviations from the design text (also in the report): (1) `final_check` is a required keyword at the service entry
only; at the primary's `admit_host_result` it defaults to the store's own ServiceRecordAuthority instance, so every
existing caller keeps working and none can skip the predicate (any non-ServiceRecordAuthority is history_invalid_input).
(2) The `unrecordable:<reason>` labels are new history reason codes `history_unrecordable_<reason>` (expired reuses
history_result_expired). (3) B2-10/B2-18 are store level: recording_status, health_signals; the CLI text, the
`already_recorded_by` output and the HM-S8 monitor wiring are not done (they need the status/health surfaces).
(4) The orchestration state in the tests is set through OrchestrationOwner._current; a real journal close/idle end is not
driven end to end.
