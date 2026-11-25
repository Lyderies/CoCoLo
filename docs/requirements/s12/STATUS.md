# S12 status

Base `ce81bd96` (0.8.67), branch `feat/s12-lanes`. Work is uncommitted because the
managed sandbox cannot write git metadata. No VERSION or runtime catalogue edit.
Implementation and regression tests are present; release acceptance is pending
unsandboxed macOS/Linux scenarios and the primary's affected/full-suite gates.

## Requirement evidence

Test names below are in `tests.test_s12_account_lanes` unless another module is named.

| Requirement | Test and evidence |
| --- | --- |
| M8 | `S12CollectorLoopTests.test_M8_signature_tree_clears_once_and_next_claim_succeeds`: real collector main loop, inconclusive signature proof then clear, idempotent next claim; blocked at the replacement helper by nested sandbox denial. Existing lane decision assertions pass. |
| M9 | `S12CollectorLoopTests.test_M9_billing_tree_clears_once_and_next_probe_succeeds`: real probe jobs, exactly one settlement and next probe after reopening; blocked before injected model cleanup because signature helpers cannot enter their sandbox. |
| M10 | `S12RecheckProofTests.test_fresh_proof_requires_group_session_and_marker_empty_and_never_signals`: red (never released) then green. Active/unreturned/container/foreign-birth and unreadable process negative controls pass. Full M10 collector scenario remains blocked by signature confinement. |
| H1c-1 | `S12RestartStatusTests.test_admitted_probe_wait_names_process_without_clearing_its_hold` passes; off-branch `test_account_lane_restart.BillingLaneRestartTests.test_N2_status_names_a_held_probe_process` red then green. |
| H1c-3 | `S12CollectorLoopTests.test_H1c_3_every_lane_child_must_clear_before_restart` passes through production collector main and real ledger; signature/probe/observation proofs clear on separate passes. `S12SignatureBirthTests.test_signature_restart_never_signals_a_root_with_unconfirmed_birth` red for all three adverse identities then green. Ported N4 pipe closure red then green. |
| B0-6 account rows (`S12-B0-6` registry scope) | All-lane collector wait passes. M8/M9/M10 collector scenarios assert no early settlement, exactly one eventual settlement, no repeated settlement on later passes and working replacement. Full lane scenarios require primary rerun. |
| E1b-1 | SQL trace of opening, collector checks, close and reopening stays on the opening thread. All-lane collector trace passes. Four named owner-wiring tests pass (below); per-lane end-to-end traces are blocked with their helpers. |
| S12-N1 | `test_account_lane_restart.ObservationGroupTests.test_N1_a_child_the_probe_reaped_itself_never_has_its_group_killed`: red (unrelated process killed), then green. |
| S12-N3 | `S12SettlementLimitTests.test_record_retry_limit_denies_once_and_later_passes_do_not_retry_records`: red (10 record calls), then green (3 record attempts, transient denial retried, one successful settlement). Busy record reads succeed on attempt 6. Oversized record reads: red (10), green (3, one denial). Ten owner passes exceed the new bound. |
| Q3 preserved | M9/M10 collector scenarios check that the lost caller callback is not called, valid records settle, and replay returns its kept answer. `S12CollectorLoopTests.test_Q3_admitted_never_launched_probe_still_spends_bootstrap` adds the explicit bootstrap negative control. Existing M10/M5 replay and cooldown assertions remain unchanged. Signature-dependent checks require primary rerun. |

## Completed local checks

- First named lane-module run against unchanged runtime: 32 tests, expected N1–N4
  failures plus environment timeouts; metadata validation was clean, 246 s elapsed.
- Focused N1/N4/status/proof/retry checks: 8 tests passed, 1.886 s.
- Focused all-lane collector/birth/proof/retry plus existing lane decisions:
  16 tests passed, 2.283 s.
- Expanded retry/status/proof/birth/all-lane collector checks: 9 tests passed, 0.492 s.
- `test_ledger_owner_wiring.LedgerOwnerWiringTests` methods
  `test_sql_trace_without_transport_covers_wakes_sweeps_restart_and_close`,
  `test_transaction_rechecks_are_one_request_and_revocation_rolls_back`,
  `test_handler_refusal_preserves_answer_and_next_request_runs`,
  `test_exit_is_recorded_after_final_handled_request`: 4 passed, 0.257 s.
- `test_collector_exit.CollectorMainTests`, `ExitRecordTests`, and the two socket-free
  `EmptyReplyTests`: 26 passed, 0.087 s. The existing M8 collector test initially
  exposed a missing optional status callback; compatibility was fixed without changing its assertions.
- Three `test_account_observation_jobs.AccountObservationJobTests` methods:
  `test_held_project_probe_cannot_block_second_project_probe_or_account_read`,
  `test_active_cancel_keeps_quota_until_actual_return_and_reply_retirement`,
  `test_H1a_a_worker_fault_is_published_by_the_worker_while_the_owner_does_not_drive`:
  3 passed, 0.808 s.
- `test_account_restart` and `test_lifetime_cap_guard`: all 19 tests pass in the named runner.
- Named `test_owned_execution` + `test_billing_probe_worker`: 109 tests, 58 failures,
  3 errors, 28.997 s. An isolated archive of unchanged `ce81bd96` produced the same
  totals (109 / 58 / 3, 36.574 s). These are environment-limited, not a green gate.
- `test_runtime_release.RuntimeReleaseTests.test_current_runtime_is_recorded_for_the_current_version`
  fails as expected because runtime changed and the primary owns release bookkeeping.
- Final named run of S12, restart, lifetime guard and release pin: 123.7 s,
  `metadata_errors: []`. S12's four signature-dependent scenarios timed out;
  the release pin failed as expected. Restart and lifetime guard passed.
- Final normal-proof guard refinement: 10 focused tests passed, 0.493 s,
  including the new red-to-green negative control for known live descendants
  and irrecoverable tracking loss. Fresh rechecks call the normal cleanup proof.
- Final metadata follow-up via the named restart and lifetime modules: both pass,
  1.594 s, `metadata_errors: []` after registering the final negative-control test.
- Final bounded-wait N1/N4 rerun: 2 passed, 0.917 s.
- No socket or PTY test was attempted. No full suite or `--affected` run was attempted.

The nested macOS confinement probe reports `sandbox_apply: Operation not permitted`.
Real model/tag scanning is also restricted here. Existing assertion changes and fixture
adaptations are listed in [assertions.md](assertions.md). No durable schema, refusal-code
set or record format changed; copier/backup/move enumeration gates are not triggered.

## Exact primary reruns

Use the test venv as `$PY`, a short clean `TMPDIR`, and the named module runner.
Run these modules separately (or in bounded named batches), unsandboxed:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_s12_account_lanes
$PY -B scripts/parallel_tests.py --module codex:tests.test_account_lane_restart
$PY -B scripts/parallel_tests.py --module codex:tests.test_account_billing_jobs
$PY -B scripts/parallel_tests.py --module codex:tests.test_account_billing_execution
$PY -B scripts/parallel_tests.py --module codex:tests.test_account_billing_admission
$PY -B scripts/parallel_tests.py --module codex:tests.test_owned_execution
$PY -B scripts/parallel_tests.py --module codex:tests.test_billing_probe_worker
$PY -B scripts/parallel_tests.py --module codex:tests.test_signature_execution
```

Socket-dependent tests that must run unsandboxed (not run here):

- Every test in `tests.test_account_host.AccountHostTests`:
  `test_status_reads_owner_key_through_the_enrolled_account_host`,
  `test_the_owners_terminal_reads_status_and_nothing_that_spends`,
  `test_a_key_added_after_the_collector_opened_the_ledger_is_recorded`,
  `test_a_key_reader_without_write_access_and_a_stopped_collector_get_nothing`,
  `test_the_collector_releases_the_ledger_promptly_after_a_shutdown_request`.
- `tests.test_account_observation_jobs.AccountObservationJobTests.test_actual_account_client_round_trip_through_original_account_ingress`.
- `tests.test_ledger_owner_wiring.LedgerOwnerWiringTests.test_all_sql_from_open_through_close_has_one_owner`.
- `tests.test_ledger_owner_wiring.LedgerOwnerHostProcessTests.test_review_production_connection_refuses_every_use_from_another_thread`.
- `tests.test_ledger_owner_wiring.LedgerOwnerHostProcessTests.test_review_host_loop_drains_ready_requests_each_pass`.
- `tests.test_collector_exit.EmptyReplyTests.test_C11_an_account_that_closes_the_connection_is_reported_as_such`.
- `tests.test_collector_exit.CollectorExitProcessTests` methods (including inherited methods):
  `test_S1_U9a_status_reports_how_long_each_start_up_phase_took`,
  `test_C10_a_signal_is_recorded`,
  `test_C10_a_shutdown_request_is_recorded`,
  `test_C10_an_error_in_the_account_loop_is_recorded_without_its_message`,
  `test_H1c_3_a_stuck_account_with_nothing_running_stops_itself_and_records_why`,
  `test_E1_the_last_self_restart_is_shown_from_start_up`,
  `test_C10a_an_error_in_the_collector_thread_is_recorded`,
  `test_C10c_an_exception_that_is_false_is_still_recorded_and_the_ledger_released`,
  `test_C10a_a_removed_socket_is_recorded`,
  `test_SD3_1_a_signal_while_a_health_note_is_published_still_stops_the_collector`,
  `test_SD3_2_a_signal_whose_exception_is_swallowed_still_stops_the_collector`,
  `test_SD3_3_a_lost_signal_in_a_step_that_never_returns_is_ended_within_the_bound`,
  `test_status_reads_owner_key_through_the_enrolled_account_host`,
  `test_the_owners_terminal_reads_status_and_nothing_that_spends`,
  `test_a_key_added_after_the_collector_opened_the_ledger_is_recorded`,
  `test_a_key_reader_without_write_access_and_a_stopped_collector_get_nothing`,
  `test_the_collector_releases_the_ledger_promptly_after_a_shutdown_request`.

The primary should run the complete named modules `test_account_host`,
`test_account_observation_jobs`, `test_ledger_owner_wiring`, and `test_collector_exit`
to include those socket cases. No S12 test needs a PTY. The plan also requires local
macOS/Linux lifecycle evidence, other-provider review and batch gates; none is claimed here.

## Files changed

Runtime files under the orchestration package: `account_billing_jobs.py`,
`account_host.py`, `account_observation_jobs.py`, `account_restart.py`,
`client_runtime.py`, `owned_execution.py`, `process_tree.py`,
`signature_execution.py`, and `collector_host.py`.
Tests: `test_account_lane_restart.py`, new `test_s12_account_lanes.py`,
`lifetime_caps.json`. Metadata: `scripts/test-layers.json`,
`test-stages.json`, `test-timings.json`, `test-requirements.json`, and
`tests/requirements/s12.json`. Documentation: `docs/requirements/s12.md`,
this status, and `assertions.md`.
