# S15 hold kind × failure exit evidence

Decision 382 continuation at `4bc4dbbd`. This is an index of executable rows,
not an additional test harness. Names below denote actual tests; no row is an
executable skip. The original B0-2 terminal expectation remains deferred under
132/377; B4s's replacement checks release without inventing a terminal.

The axes are **acquisition boundaries**, not permission to release every hold
on every error. Before a hold's acquisition there is nothing to release. After
an uncertain send/launch/ACK the owner retains custody until its own evidence
arrives. A project run exit does not create an account billing probe; probes
have their own reservation/admission/preparation/launch lifecycle.

## Hold-kind rows

| Hold kind | Before launch: refusal / cancellation / failure | Completion / no result / uncertain reply | Service or collector restart; clocks | Release sensitivity |
| --- | --- | --- | --- | --- |
| Account `binding` | Open first acquires it; withdrawal releases an unclaimed binding. A refused open/claim adds no downstream hold: N. | Terminal acceptance consumes it; a denied/fenced claim abandons it. Lost/duplicate withdrawal preserves its first receipt: A, P, R. | Unclaimed expiry is durable; rollback cannot revive it. A covers transaction cuts; existing `test_run_holds` covers claimed expiry/rollback. | A: actual `run_holds_binding_released` trigger disabled. |
| Account `run_slot` | Acquired by a successful claim, never by refused open/claim: N. A prelaunch claim remains charged until its verified terminal/release or no-launch settlement. | A uses actual terminal and cleanup writers for normal release. C reopens a lost claim reply and proves exactly one slot. Missing cleanup retains custody. | A uses real crash predicate after boot-clock regression; CR-1 decisions survive later uptime. Native container proof remains S27a/c. | A: actual slot-release trigger disabled, normal and boot-change rows. |
| Account `probe` | Actual reserve takes it. Reserved expiry closes unlaunched requests; an admitted original is retained while its owner still holds it. | `test_u4_probe_execution_capacity` covers completed/denied history and replay through owner writers. A covers the actual unlaunched expiry writer. | A reopens an admitted store row with no original, passes deadline + grace, then releases exactly once. Container journal recovery remains S27b/d. | A: actual probe-release trigger disabled for reserved and orphaned-admitted expiry. |
| Project `lease` | Unarmed unsent/expired/successor withdrawal: R. Refusal before reserve has no lease: N. Armed uncertainty is never treated as unsent. | Binding transfers duty to reservation; denial and genuine account fence release it: R/P. Lost unbound settlement: S. | S: before settlement/release, after real release before commit, lost committed reply, reopen/replay. A clock alone cannot free an armed lease. | R, P, S each suppress their reached release and fail the positive oracle. |
| Project `reservation` | Not acquired before actual claim bind. Lease-to-reservation transfer stays charged: R. | Completed release helper after an actual account cleanup receipt: P. Lost settlement: S. Mere publication/expiry does not end the obligation (`test_project_hold_register`). | S: bound settlement at all four cuts, reopen, stable first receipt. Native gone/failed-stop proof stays S27c. | P completed; S lost, each independently suppressed. |
| Project `open_binding` | Recorded before registration; unarmed withdrawal/account-final: P. Armed fence uses actual account fence writer: P. | Bind releases `claimed`; fence releases `fenced`; replay never changes the first receipt: S/P. | `test_open_withdrawal` and `test_open_withdrawal_successor` bind successor and lost-reply paths. S/P exercise the corresponding store release writers. | P withdrawn/account-final/fenced; S claimed. |
| Project `result_custody` | Lease end without a result releases unfinished context; unsent, denied/fenced and lost-unbound paths: R/S/P. | After publication, custody stays until retention or explicit orchestration closure: R/P. Reservation completion alone is not a custody release. | S checks companion release and later expiry; R/P check real context/body unlinking. Retention-floor forward/back controls are in `test_deadlines_retention`. | R unsent/retention; S unfinished settlement; P orchestration-closed. |
| Project `driver_entry` | Real registration takes it; retirement and handover end memory ownership, not account execution: R. | Handover retains lease/open-binding duty with the sweep owner: R. | M requires retained unlocked instance-inode proof; live lock and wall rollback retain ownership. Lost registration reply and cuts around recovery release are covered. | R retired/handed-over; M service-stopped. |
| Project `preparation` memory hold | Real preparation registration, retired / submit-refused / never-launch-local: R. | Cleanup uncertainty is not a release proof. | M covers live-lock retention, stopping proof, lost registration reply and recovery cuts. | R three normal reasons; M service-stopped. |
| Project `orchestration` | No orchestration acquired by refused unauthenticated open: N. Existing orchestration survives a single run's refusal/cancel. | Explicit primary close releases `closed`: R. Recorded result retention has independent rules. | Unattended close uses stored due boundary, not rolled-back wall origin: R/M. Repeated close preserves the first release. | R primary/unattended. |
| Host `worker` entry | Prepared local input is withdrawable before claim. H's actual fixed worker fails decoding before launch, returns, cleans, then retires. | Returned packet alone is insufficient; release precedes retirement. No child is fabricated or declared gone. | Live process/container recovery is delegated to its durable owner; memory disappears only with stopped host or verified cleanup. Native container rows remain S27a/c. | H independently disables actual worker deletion; same baseline oracle fails. |
| Host `result` entry | No result before actual fixed return: N/H. | H obtains the actual fixed return through `owner.result`, proves stable repeated access, then drops the retained result after local release. | Durable custody is separately S/R/P; losing host memory is not an account release. | H suppresses only retained-result deletion. |
| Host `execution` entry | N proves no ticket for refused dispatch; H proves actual never-launched fixed return is clean. | Registry ACK is after the external/durable decision; uncertain execution is retained. H's ACK is explicitly a local fixture, not native authority. | A/S/M exercise durable restart obligations; native launched cleanup proof is S27a/c. | H suppresses only registry pending-entry deletion. |
| Host `monitor` entry | N proves no monitor acquisition on refusal. H binds one to its actual original fixed worker. | T crosses completed/cancel/no-result/deadline/clock rollback; neither terminal nor cleanup alone permits forgetting. | Invalid shared clock latches `monitor_unavailable`; cleanup/terminal observations still allow removal. M covers durable driver restart ownership. | H monitor deletion, plus T each of five exit rows. |
| Host preparation entry / workspace | Q builds a genuine committed export, cancels/discards it, checks actual removal and worker withdrawal. | Failed cleanup retains entry/workspace; existing `test_codex_preparation` and PREPCLEAN tests bind retries and host replacement. | Stopped-instance preparation ownership is M; native interrupted preparation cleanup remains in existing PREPCLEAN integration tests. | Q suppresses actual preparation retirement. |
| Probe worker preparation / workspace | Q exercises actual invalid-input failure and a genuine prepared probe's cancellation, real workspace removal, retirement and zero byte charge. | A prepared probe is not an admitted/running model. An issued ticket or unconfirmed child cannot be retired as an unbound preparation. | Existing M9/M10 account-lane restart tests bind retained unknown workers and proof-gated replacement. Native launched probe recovery is S27b/d. | Q independently suppresses retirement on failed and cancelled preparation rows. |

## Executed socket-free row keys

All keys name `tests.test_s15_failure_matrix.FailureMatrixTests` unless stated.
The named runner executes these methods, including every parameterized row.

- **A**: `test_account_holds_cross_transaction_cuts_and_lost_release_reply`
  (six acquisition/release paths × before effect, after effect before commit,
  after outer commit). `test_account_release_trigger_disabled_fails_the_ordinary_release_oracle`
  independently disables each path's actual SQLite release trigger.
- **C**: `test_lost_claim_reply_restarts_owner_without_another_claim_or_slot`.
- **N**: `test_refused_open_claim_and_dispatch_do_not_acquire_downstream_holds`;
  `test_lost_and_duplicate_dispatch_replies_keep_one_original_without_new_custody`.
  These execute the real service handlers with original controlled frontend
  fixtures and exact pre/post hold snapshots; they do not authenticate a native peer.
- **H**: `test_actual_failure_before_launch_frees_worker_result_execution_and_monitor`;
  `test_each_local_host_deletion_disabled_fails_the_same_failure_oracle` (four kinds).
- **Q**: `test_host_and_probe_preparation_cancellation_and_failure_remove_actual_workspaces`;
  `test_each_preparation_retirement_disabled_fails_the_same_cleanup_oracle` (three paths).
- **P**: `test_project_withdrawal_completion_and_closed_custody_replay_keep_first_release`;
  `test_each_project_final_release_disabled_fails_the_same_row` (six paths).
  Completed reservation tests the exact internal release helper after real account
  writers; closed-custody tests control only the owner's closure observation.
- **T**: `test_monitor_exit_matrix_requires_terminal_and_cleanup_before_forgetting`;
  `test_each_monitor_exit_with_forgetting_disabled_fails_the_same_oracle` (five exits).
- **S**: `tests.test_s15_settlement_matrix.SettlementMatrixTests` (8 cut scenarios;
  4 independent release controls).
- **M**: `tests.test_s15_memory_matrix.MemoryMatrixTests` (8 scenarios; 2 controls).
  Its `before_registration` cell observes absence before normal registration;
  it does not simulate a crash before registration.
- **R**: `tests.test_s15_release_paths.ReleasePathTests` (18 release paths and
  18 independent controls). All ordinary replay checks compare full first-release rows.

The new module adds **24** independently suppressed releases (6 account + 4
local host + 3 preparation + 6 project + 5 monitor), to the previous **24**.
Negative acquisition and intentional retention rows have no release to disable;
calling them a passing release mutation would invent evidence.

## Full-service reference evidence, not rerun here

Existing service/CLI scenarios retain their stronger end-to-end scope:
`test_hold_lifecycle.HoldLifecycleTests.test_B0_6_a_completed_run_frees_every_hold`,
`test_host_slots.HostSlotLifecycleTests.test_M1_M3_M4_four_runs_end_on_their_failure_exits_then_a_fifth_dispatches_without_a_restart`,
`test_project_driver.HostRestartTests.test_after_a_host_restart_the_run_is_reported_lost_and_the_primary_can_continue`,
`test_result_lease_lane.ResultLeaseLaneTests.test_lost_durable_claim_bind_reply_retries_exactly_without_refresh_or_early_execution`,
`test_dispatch_client_runtime.DispatchClientRuntimeTests.test_actual_dispatch_metadata_is_idempotent_and_uses_original_writer`,
and `test_account_lane_restart.BillingLaneRestartTests` M9/M10 rows.
These are existing source bindings, **not a claim that this no-socket run executed
them**. Socket-free rows above prove their specified store/local boundaries;
they do not substitute fixed fixture credentials for native authority or process proof.

## Native container carry-ins (unchanged, no passing claim)

| Step | Required rows |
| --- | --- |
| S27a / RS-H1 | Run slot × dead container with result → completed; dead without result → lost; alive after deadline → alert. Launch confirmation retains container identity. |
| S27b / C5 | Probe worker and admitted probe × restart before launch → `probe_unlaunched_restart`; lost launched probe container → `probe_lost`. |
| S27c / B5b | Reservation and run slot × crash with container and cgroup proven gone → crashed settlement; absent proof or failed stop → retained `stop_failed`. |
| S27d / O0C-U4-3 | Dead probe/reaper replay releases exactly one slot; probe journal bounds capacity. |
