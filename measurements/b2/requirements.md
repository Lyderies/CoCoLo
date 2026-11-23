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

Fix round 1 (reviews rev040b2r1a / rev040b2r1b). Tests: test_b2_service_outcome_paths.py (real orchestration end, idle end,
ownership, missing orchestration, K1/K2/K3 by state, altered rows, receipt versions 1 and 2, swapped publications, a failed
write), test_b2_service_outcome_hosts.py (the host's own authorization and the primary's record command through the history
service), test_b2_service_outcome_sweep.py (sweep isolation, limits, starvation, health; the writer lane's publication and
backstop request).

| Finding | Fix | Tests |
|---|---|---|
| MED-1 | record_pass isolates each candidate (any non-HistoryError is a transient `unexpected_error`: backoff, an `error` health signal, cursor progress kept per candidate); the lane suppresses a failure of the record in the publication and of the pass in the cleanup request, so the cleanup always runs | SweepIsolationTests, LaneRecordingTests |
| MED-2 | a missing or unverifiable orchestration view refuses (`history_unrecordable_orchestration_closed`) for the service entry and for every run with a stored context (K1, K3); only a primary-path run with no stored context (before D1) is let through | MissingOrchestrationTests |
| L1 | `ServiceRecordAuthority(service=True)` requires the writer's runtime; the primary service path passes its own; the primary's default instance (no runtime) stays, owner decision 114.2 open | AuthorityLowsTests.test_L1 |
| L2 | the race loser repeats the source and expected-field comparison before returning the winner's receipt | test_L2 |
| L3 | `check` returns the one clock sample the eligibility decision was made on; the admission uses it | test_L3 |
| L4 | health signals cleared on recovery, bounded by BACKOFF_MAP_LIMIT | SweepIsolationTests |
| L5 | no orchestration SQL prefilter: the in-memory permanent set replaces it (design 4.6 deviation, see below) | test_L5, RealOrchestrationEndTests |
| L7 | recorder key and foreign key deviation documented below | (document) |
| L8 | status of already_recorded_by, the HM-0 check and the report corrected below | RecordCommandTests |
| L9 | plain guidance for every new reason code and for history_result_expired (refusal_steps) | test_L9 |
| L10 | the orchestration report passes the writer's runtime to the recording status | test_L10, AuthenticatedRequestTests |
| limits | `limit` must be a positive int and is capped at SWEEP_ATTEMPTS; the window is `min(SWEEP_WINDOW, limit*16)` | SweepLimitTests |
| permanent refusals | each carries a fingerprint (context, reservation, orchestration checkpoint); a changed state is tried again | test_a_permanent_refusal_is_tried_again... |
| host class | an unsupported or other host class is `history_unrecordable_host_class`, not other_runtime | test_unsupported_host_class... |

Deviations from the design text (also in the report): (1) `final_check` is a required keyword at the service entry
only; at the primary's `admit_host_result` it defaults to the store's own ServiceRecordAuthority instance (no runtime),
so every existing caller keeps working and none can skip the predicate (any non-ServiceRecordAuthority is
history_invalid_input); the primary's service path passes the writer's runtime (owner decision 114.2 open).
(2) The `unrecordable:<reason>` labels are new history reason codes `history_unrecordable_<reason>` (expired reuses
history_result_expired). (3) B2-10/B2-18 are wired: the orchestration report carries the recording section (recorder,
what waits, why something cannot be recorded, the unreviewed marker), the record command prints `already_recorded_by`
(best effort), and the HM-0 `recording` check reads what the sweep published. The check is not registered by name in
the health monitor's own list (the monitor is owner-wired) and the heuristic label `primary (before B2)` stays.
(4) The recorder table is keyed by (project_id, record_id) with a foreign key to `history_identity(project_id)`, not by the
claim/reservation key of the design (4.5): the receipt's record id outlives the reservation's retirement, and the
foreign key is to the project identity row only, so a retired reservation never removes attribution. (5) No orchestration
SQL prefilter (design 4.6): selection reads committed contexts with a live reservation through the live-state index, and
the in-memory permanent set (fingerprinted) skips a refused run until its state changes. (6) The status lists runs whose
context is still held; after a reservation retires, attribution is the recorder row only. (7) The orchestration state in
the first module's tests is set through `OrchestrationOwner._current`; the fix-round modules drive the store's own close,
interruption and idle end instead (the account's release of the run, `_account_released`, is the one control used there).
(8) The host-authorization tests call the host's own `_authorize_history` and the history service directly, with the
transport replaced by a direct call (no sockets, no collector); the record command's orchestration status and result
read are answered by the same store.
