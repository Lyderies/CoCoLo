# S5 B4t build status

Base `ce81bd96` (0.8.67), branch `feat/b4t-takeover`. Scope: accepted Draft 2,
its fix round and final fix (decision 326), S5/SW-3 and decisions 129.2 A, 261,
272 and 323. VERSION and the runtime digest catalogue are unchanged. No commit,
install, live-service mutation or full-suite run was attempted.

Implementation and named-module work are present. **S5 is not through its
per-step gate and this worktree must not be installed.** Native macOS/Linux
lifecycle acceptance, other-provider review, security review and the primary's
batch gates remain. The source is reviewable here; the builder cannot supply
those external gates from this sandbox.

## Ordered work

1. Read the accepted requirements and builder rules. Added the initial 43-method
   T-1–T-35 scaffold and its scenario/integration/requirement registrations before
   changing product code. Its named run against the untouched product was red:
   2 failures and 55 error reports, including subtests, in 2.7 seconds;
   `metadata_errors: []`. Existing start-barrier guards were already green.
   These initial component tests were not complete native acceptance witnesses.
2. Built the bounded, owner-only atomic takeover journal and phase coordinator,
   including exact authorization undo before J5, truthful post-intent failures,
   J4a and J6a entry reconciliation and crash/resume postconditions.
3. Reused the existing quiesce start gate; added authenticated collector shutdown,
   owner-enrolled service stop, verified legacy process pinning, bounded sole-lock
   ownership proof, one TERM and same-pin escalation, and bounded straggler retries.
4. Added read-only successor custody at the six approved account functions. The
   four `earlier_runtime=` call sites are pinned by an AST witness. Admission,
   storage evidence, execution and provenance retain their strict runtime gates.
5. Added ledger 18/project 23 authority tables and floor checks under S2/S3.
   Pending old 17/22 migrations finish their original target before stage B.
   Genuine archived SIGKILL cuts exercise both stages, frozen clocks, preserved
   before-copies, missing-copy refusal and uninterrupted-result equivalence.
6. Adopted immutable original project custody and outbox obligations; kept open
   and paused orchestrations. Replaced the interim runtime-only close assertions.
   Current recording still requires its own takeover evidence and current host
   class; revocation during body I/O rolls the real handler's write back.
7. Wired the owner CLI and wizard before installer writes, moved records consent
   before stopping, added installed/complete checkpoints and reset-failed before
   systemd refresh. Only the running-child preflight refusal is removed.
8. Added the two new read operations and fixed outcome/phase wording, per-run
   lost annotations, B4m commands and troubleshooting text. Existing wire reply
   fields remain unchanged. The owner command reads other-project diagnostics
   after the bounded account-service start, without requiring a project service.
9. Strengthened the initial scaffolds with authentic archived claims/outboxes,
   A→B→C replay on both stores, memory-hold and removal recovery, and U1 cuts
   inside/after the old transaction. These stronger tests were added later;
   they must not be misreported as having all been committed red before code.
   Every subsequent product fix was preceded by its focused failing witness.
10. Ran the named copier/backup/move/enumeration and custody regressions below.
    Paired all 143 removed/changed existing assertion expressions in
    [assertion-audit.md](assertion-audit.md), with stimulus/fixture explanations
    in [assertions.md](assertions.md). The account target pins changed 17→18;
    original byte, receipt, rollback and manifest comparisons remain.

## Requirement evidence

All IDs have the initial red component run above. PASS below means the named
component/archive witness passed; it does not complete the native gate.

| ID | Passing named witness | Acceptance still owed |
|---|---|---|
| RS-B-B4t-0 | `JournalTests.test_B4t_0_update_with_a_running_child_goes_ahead`; `test_B4t_0_every_write_free_refusal_still_writes_nothing`; `OwnerIntegrationTests.test_update_check_replaces_only_the_running_child_refusal` | Full wizard, L12.3 and no-half-install native flow |
| RS-B-B4t-1 | `OwnerIntegrationTests.test_wizard_runs_the_takeover_after_approval_and_before_installer_apply`; collector-stop tests | Actual loaded collector/service stop durations and IP-B |
| RS-B-B4t-2 | `StoreTests.test_B4t_2_committed_results_stay_recordable`; `test_B4t_2_revoked_enrollment_refuses_recording`; own-takeover-authority negative | Live enrolled service/primary flow |
| RS-B-B4t-3 | `FloorTests.test_archived_0866_and_0832_cannot_reopen_either_published_store`; `ProcessProofTests`; `ServiceStopTests`; ledger/project straggler tests | Real kernel sole-holder witness FAILS safely here; native start-path matrix and legacy command exclusion |
| RS-B-B4t-4 | `StoreTests.test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once`; `MemoryTakeoverTests`; `RolloverTakeoverTests`; J4a/J6a tests | Real running P/Q children, orphan/deadline and complete service startup recovery |
| RS-B-B4t-5 | `ChainedOutboxTests.test_two_archived_outboxes_survive_A_B_C_with_one_effect_and_immutable_originals`; `StoreTests.test_B4t_5_no_double_record_in_any_order`; `AccountLineageTests` | Live transport retries and service crash at the native command boundary |
| RS-B-B4t-6 | `StoreTests.test_B4t_6_primary_recheck_idempotent_across_takeover` | Primary lifecycle recheck on both operating systems |
| RS-B-B4t-7 | `OwnerIntegrationTests.test_another_projects_unfinished_takeover_names_its_verified_B4m_command`; `test_recovery_handover_names_the_B4m_update_command`; other-project account reads | Installed-old-client lifecycle flow |
| RS-B-B4t-8 | `StoreTests.test_status_wording_uses_fixed_outcomes_and_other_project_commands`; `test_post_stop_count_includes_an_unsettled_orphan_but_excludes_a_result_committed_before_stop`; real new read producers; reply-shape compatibility | Full terminal/run-status/wizard native rendering and live producer soak |
| RS-B-B4t-9 | lineage/catalogue negatives; revoked-host real handler; tampered takeover-run and outbox read tests; unchanged-custody assertions | Native spoof/reuse races and both-platform review |
| RS-B-B4t-10 | `JournalTests.test_B4t_10_crash_at_each_phase_then_resume`; `MigrationCutsTests`; `RolloverTakeoverTests`; chained immutable replay | Whole owner command SIGKILL at every phase; authorization replace/directory-fsync cuts; native login restart/start races |

The journal phase tests use an operations port. They are not a substitute for
killing the real native owner command. T-6b, T-8's process census, T-9/T-10,
T-12/T-13 under real load, T-17 and the archived T-20–T-24 start matrix still
need that acceptance. T-15's archived row-preservation check is narrower than
the complete bound/unbound/in-flight open lifecycle matrix. The actual chained
fixture accepts B's second send and loses its closure reply; it verifies missing
project/account edges refuse without effects, but the alternative where that
send never reached the account still needs the native flow.

## Named runs and limits

No full run or `--affected` was used. Completed batches stayed below five minutes;
one extended account run was interrupted at 301.9 seconds as described below.
Module fragments below are selected by the normal named-module runner.

| Run | Result | Elapsed |
|---|---|---|
| First untouched-product B4t scaffold | 43 methods; expected red; metadata clean | 2.7 s |
| Boundary/API/U1/root checks before final diagnostics | 265 tests; B4t 99/100 pass; four history socket-listener failures; one metadata link subsequently fixed | B4t 32.122 s |
| Copier/migration run, 14 named modules / 17 fragments | 215/217 pass; two existing CLI migration failures | 86.7 s |
| Exact two CLI migration cases on untouched `ce81bd96` archive | Same two failures reproduce, same fixed product errors | 16.247 s |
| Custody/schema/runtime-close run | 342 tests; 327 pass; 11 additive target-pin failures, three account socket failures, one expected release-digest failure | 46.7 s |
| Target-pin corrections + cap guard + root login/e2e metadata | 131/131 pass; `metadata_errors: []`, `requirement_failures: []` | 15.6 s |
| Registered B4t + cap proof before last diagnostics | 103/104 pass (100 B4t + four cap tests); only native sole-holder proof refuses; `metadata_errors: []` | 22.6 s |
| Two new full wizard cases | Both blocked during fixture setup, before the wizard starts | 0.034 s |

The copier run covered `test_learning_source_image`, `test_learning_source_image_legacy`,
`test_learning_source_transfer`, `test_backup_installed_project`,
`test_account_observation_backup`, `test_large_project_backups`,
`test_owner_backup_streamed`, `test_backup_reply_private`, `test_learning_migration`,
`test_launch_receipts_layout`, `test_project_holds_schema`, `test_rsf_s2_fence`,
`test_rsf_s2_regressions` and `test_rsf_s3_resumable`. Additional account schema and
standalone restore coverage ran in the custody batch and target-pin rerun.
The runner reported existing per-module budget debt for large backups, owner
backup and S3; their thresholds were not changed.

Additional evidence after that checkpoint:

- Registered diagnostic/U1/S6/root run: 234/235 pass in 23.2 s; only the native
  sole-holder proof refuses; `metadata_errors: []`.
- Registered current-owner/lineage security run: 120/121 pass in 17.6 s; only
  that same native proof refuses; `metadata_errors: []`.
- Extended project schema/enumeration run: 397 tests, 128.7 s, metadata clean.
  It exposed additional target pins plus existing native listener failures.
- Extended account enumeration run: 404 tests completed across 14 fragments;
  the last billing fragment was still waiting for signing results. Interrupted
  at 301.9 s through its execution session; the runner's finally block reaped
  its owned worker group. Its interruption is recorded, not counted as a pass
  or as metadata validation.
- Focused post-fix schema/fixture/copy cases: 66/68 pass in 35.299 s. The two
  blocked cases are the socket-based collector test and the billing-signature
  fixture. The latter fails identically on untouched `ce81bd96` (20.549 s).
  The exact durable selection and rerun command are in
  [named-regressions.md](named-regressions.md).
- Missing/incomplete hook-aggregate recovery and historical 15/1 fixtures:
  five focused cases pass in 1.474 s after the existing failing tests exposed
  the omitted schema-18 additive repair.

Final owner/migration regression after the hook repair: 204/205 pass in
53.7 seconds; `metadata_errors: []`. The only failing case is the real native
sole-holder proof, so `requirement_failures` names RS-B-B4t-3. This run includes
the 106-method B4t module, cap guard, S3, S2 regressions, account observation
backup, installed-project backup, source transfer/image and account cleanup
release. All schema/copy/settlement checks in that selection pass.

All launched test sessions have finished or were explicitly interrupted and
reaped. No full run or test process is left in flight.

## Environment and remaining gate

- The billing signing-worker backup fixture times out awaiting its original
  signature and during cleanup on this tree and the untouched unit base; its
  genuine-sealed backup assertion remains for the primary.
- Unix socket bind is denied (`EPERM`), including the existing account/history
  listener fixtures. The two learning-migration CLI failures reproduce on the
  untouched unit base in this environment.
- The real macOS pin test gets a valid kernel pin and process identity, then
  refuses sole-holder proof because `proc_pidfdinfo` denies inspection of a
  same-user opener. The test remains failing, not skipped or weakened. The new
  bounded KERN_ARGMAX read fixed an earlier real argument-read error.
- The wizard fixture cannot create its trusted runtime cache outside writable
  roots. Its two new full-flow cases therefore do not reach the product.
- Linux, PTY/lifecycle flows and live IP-B were not run. The reply producer soak
  pulls in a transitive batch exceeding this builder's allowed run budget and
  remains for the primary. `test_project_start`'s native process scenarios also
  remain unrun here; the new B4m recovery-line component witness passes.
- `test_runtime_release` has the one expected current-digest mismatch; its file
  set and unique/increasing catalogue checks pass. The primary must assign the
  release version and catalogue entry at merge, as instructed.

The primary can rerun named modules with `$PY -B scripts/parallel_tests.py
--module codex:tests.test_b4t_takeover` and the modules listed above. For native
acceptance use the S5/SW-3 affected lifecycle flows on both systems, then the
review and quiet batch gates in plan section 1. No approval is requested here.


## Final implementation notes

The current-owner predicate requires the exact applicable takeover edge and its
prefix from the immutable original binding. A valid chain elsewhere in the
store is insufficient. Status validates the run mapping and its takeover edge,
checks releasing outboxes and deduplicates bridge IDs across successive updates.
The wizard's post-stop count includes a verified unfinished orphan, while a
result committed before the stop keeps its own outcome; run-status annotation
and capacity release still wait for the ordinary lost-settlement evidence.

The extended schema fixtures and all changed assertion expressions are in the
143-pair audit. The new helper restores historical layouts by dropping local
authority, never by changing an original receipt or result. The restored
current-ledger additive repair is an existing owner-open behavior, not a new
migration/admission permission.


[Changed-file manifest](files.md). `git diff --check` passes. No path in these
requirement documents names an absolute home directory; no raw logs are copied
here. No VERSION or digest-catalogue file appears in the changed-file list.
