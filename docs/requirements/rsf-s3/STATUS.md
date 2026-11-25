# RS-F S3 status

Implemented on `feat/rsf-s3-resumable`, base `d33859cc` (accepted S2 on
0.8.60). Changes are **uncommitted**, as instructed for this sandbox. No install,
push, merge, VERSION change or runtime release catalogue change was made.

## Requirement → test → evidence

Review correction round is complete. Requirements preceded the six added test
methods and production fixes. The red run reproduced **9 subcase failures in
10 tests, 31.4 s**: four context-cleanup failures, two pending-refusal failures,
and three missing/unsafe-copy failures. Added death-boundary and real-successor
coverage already passed before production changes; no artificial red is claimed
for those test-only additions. The green run passes **all 10 tests in 33.7 s**,
all five requirement IDs pass, and `metadata_errors: []` on every round run.

All added methods are in `tests.test_rsf_s3_resumable.ResumableMigrationTests`:

| Review item / requirement | Test | Evidence |
| --- | --- | --- |
| 1 / RSF-S3-1, RSF-S3-3, RSF-S3-4, RSF-S3-5 | `test_account_last_short_done_and_publication_commits_reopen_twice_identically`; `test_project_last_short_done_and_publication_commits_reopen_twice_identically` | Pass before and after the fixes. Twenty additional exact SIGKILL boundaries: before/after every class's final two-row batch (its cursor 130 and done=1 commit together), plus both publication commits. Publication observes successful full schema/register/coverage or custody validators before DROP. Before-commit death retains progress; after-commit death publishes. Every case reopens twice and equals the uninterrupted upgrade in every table except the separately asserted advancing epoch, including identities, counts, real owner signatures and sealed project bytes. |
| 2 / RSF-S3-1 | Requirements and this status; existing batch/cursor and frozen-clock tests | The first/final transaction limits and atomic, idempotent retry behavior are explicit below. Sweep batching is deferred for the stated source-scan reason. |
| 3 / RSF-S3-3 | `test_migration_context_is_reset_after_adoption_and_validation_exceptions` | Four red subcases → green. Both owners reset `_hold_migration` in `finally`, for adoption and final-validation exceptions. The project copy and attempt marker survive post-checkpoint validation failure; retry keeps the same copy byte for byte. |
| 4 / RSF-S3-2, RSF-S3-4 | `test_real_successor_after_sigkill_increments_epoch_and_stale_batch_cannot_commit` | Pass before and after fixes. For each store, a different process acquires after the first is killed and commits epoch +1 on resume. That real successor retains its connection after losing the lifetime lock; a third process acquires and increments again. The real epoch check refuses the stale capability, and its actual COMMIT rolls back one staged adoption-row change plus one cursor change. No `StoreFence.check` patch is used in this test. Two final reopens match the uninterrupted upgrade. |
| 5 / RSF-S3-3 | `test_pending_refusal_gives_plain_next_step_and_update_command` | Two red subcases → green. Both messages say to resume the drained upgrade and include `Run: ./setup.sh --update`; ordinary refusal leaves rows unchanged. |
| 6 / RSF-S3-3 | `test_project_resume_refuses_missing_kept_before_copy_without_writes`; validation-exception test above | Three red subcases → green: absent original copy, absent attempt marker, or a symlink replacing the copy. An unrelated before-copy cannot satisfy the gate. Refusal gives the recovery step/update command and leaves rows and epoch unchanged. Restoring the original recovery material allows completion. Recovery after failed final validation is documented below and in the requirement. |

The complete acceptance run now kills **54 exact child processes** and also
retains two real successor processes for stale-owner refusal. The subprocess
observer wraps actual commits/validators and calls their original implementations.
It adds no production fault hook or mocked commit.

Settled-tree round checks: **309 pass, 1 expected runtime digest-pin failure**
across 15 named modules (310 tests; 67.2 s total runner elapsed time). The
regression batch is 163 pass + the expected pin failure in 16.3 s; the named
backup/source-image batch is 136 pass in 17.2 s. Every batch has
`metadata_errors: []`, no budget failure and no timeout. Runtime inventory and
completeness checks pass. The existing S3 module remains registered as scenario
layer / integration stage; its new methods are in the requirement catalogue.
Structured results are appended as `rsfs3-r1-*` in [evidence.json](evidence.json);
reproducible named commands are in [commands.txt](commands.txt).

The **first transaction** (legacy account backfills for ledgers below 17) and
the **final transaction** (account release-due sweep plus full account/project
validators) are **not batch-bounded**. Each is atomic and idempotent on resume.
The 64-row bound is on adoption batches only. Bounding the sweep is deferred:
its legacy process-proof scan is also unbounded, so adding commits around its
1024-release loop would not establish a source-row bound within this fix round.

After post-checkpoint final-validation failure, keep the store pending and
services drained, preserve the database and recovery material, fix the cause
and retry `./setup.sh --update`. If recovery is necessary, use the kept project
`history.sqlite3.before-22.*` copy identified by the attempt marker, or the
verified pre-upgrade account backups via the supported recovery procedure;
verify the restored store before updating. Restore a missing project copy at
its original name before resuming. Never remove progress or lower the version
to force startup. See [the recovery requirement](../rsf-s3.md#recovery-after-a-checkpoint).

## Original implementation evidence (before this correction round)

The original four acceptance methods are in
`tests.test_rsf_s3_resumable.ResumableMigrationTests`:

- **A**: `test_account_killed_before_and_after_each_class_commit_reopens_twice_identically`
- **P**: `test_project_killed_before_and_after_each_class_commit_reopens_twice_identically`
- **R**: `test_revoked_batch_and_failed_validation_keep_progress_unpublished`
- **C**: `test_resumed_classification_keeps_original_wall_and_monotonic_evidence`

| Requirement | Tests | Evidence |
| --- | --- | --- |
| RSF-S3-1 | A, P, C | Red → green. Three account classes and five project classes, 130 source rows each; durable cursor 64 accompanies exactly 64 adopted rows. Before-commit death rolls back; after-commit death retains progress. Resume is interrupted again before cursor 128 commits. C first failed on a changed release digest, then passed with both original clocks persisted. |
| RSF-S3-2 | A, P, R; existing `tests.test_rsf_s2_fence` | Red → green. Every batch uses the original fenced owner connection and lifetime lock. R changes the actual SQLite takeover epoch just before the pending batch commit; the real fence refuses and rolls back both rows and cursor. Existing real historical executables still cannot displace/reopen the owners. Account batch commits retain the database-file hardening check. |
| RSF-S3-3 | A, P, R | Red → green. Ordinary startup refuses pending progress. Validation failure leaves the marker and committed progress; a later authorized open completes. Project before-copies survive interruption and resume without multiplication. A death before the first checkpoint retains the original rollback behavior. |
| RSF-S3-4 | A, P, R, C | Green: 27 exact child processes receive SIGKILL per acceptance-module run. Each class is tested before and after a real SQLite commit. Interrupted stores reopen twice and equal the uninterrupted store in all rows except the deliberately advancing machine-local epoch. Historical bytes include a real detached SSH owner signature, sealed lease/orchestration material and a committed terminal result. |
| RSF-S3-5 | A, P; source-image, backup, restore, move and inventory modules | Final formats remain ledger 17 and project 22. Temporary progress is absent after validation. Named copier batch: 196 tests pass; final hold/schema/source regressions: 184 pass. Runtime inventory/completeness checks pass; the current-release digest pin has the expected failure because the primary owns VERSION/catalogue bookkeeping. |

Final acceptance/fence/hold/cap batch: **91 tests pass in 28.7 s**, with
`metadata_errors: []`, no budget failures and all five S3 IDs marked `passed`.
This includes the four acceptance tests after strengthening the epoch fault to
change the real epoch and adding a genuine signed owner record.
Final source-image/schema/runtime batch: **95 pass, 1 expected runtime digest
pin failure in 6.8 s**, `metadata_errors: []`. Its inventory/completeness
assertions pass against the final runtime file set.

## Implementation and schema decision

`hold_migration.py` records one temporary progress row per store/class, with a
source rowid cursor, completion flag and frozen wall/monotonic readings. It
seeks source rows in batches of at most 64; target writes and cursor commit in
the same S2-fenced transaction. The owner lock stays held between transactions.
The account checks its protected database files before each batch commit.

The initial transaction completes fixed DDL and installs the **existing**
target version before its first durable batch, excluding older executables.
Pending metadata separately prevents current ordinary startup from returning
an owner. Only explicit upgrade authorization can resume. Register, custody and
schema validation precede deletion of the marker in the final transaction.
Small migrations with at most 64 source rows keep the original atomic path.
Project resume keeps the original upgrade copy/attempt marker until completion.

**No additional permanent schema step is needed.** Completed stores have exactly
the accepted ledger-17/project-22 schemas; progress exists only while an owner
is unavailable for admissions, backups and source-image capture. Source-image
version constants and project backup formats therefore remain unchanged.

## Validation record and limitations

Requirements preceded the tests, which preceded production changes. Initial
registered red: 3 tests, 18 boundary failures, `metadata_errors: []` (12.9 s).
The added clock regression was separately red: 4 tests, 1 failure, no metadata
errors (26.7 s). Final green evidence, individual outcomes, durations and
requirement results are retained as structured data in [evidence.json](evidence.json).
No raw logs or machine paths are stored here.

The audit attempted **73 explicitly named modules**, listed in
[modules.txt](modules.txt). No full run or affected-module run was invoked.
Successful batches include both backup formats, owner backups, source images
and transfers, legacy capacity/history, account observation/cleanup/open
schemas, learning chunk/QA upgrades, library copying/recovery, project removal,
record lookups and wire/refusal inventories. Commands are in
[commands.txt](commands.txt).

The settled-tree service checks are **not claimed green**. Unix socket bind
raises `PermissionError` (`Operation not permitted`); collector/project service
startup and orchestration calls then refuse. A clean-environment rerun with a
short TMPDIR confirmed these restrictions (158 tests attempted in 21.1 s).
Native process/environment inspection also fails, affecting owned-execution
and project-driver fixtures. The service/copier batch attempted 486 tests in
122.9 s; billing admission, billing gate and open-request lifetime workers
reached their explicit 60 s limits. Their unfinished methods require an owner
shell. No timeout is presented as a pass.

Earlier broad attempts included fixture digest errors because runtime inventory
ordering was corrected while workers were running. Those attempts are retained
for audit, then superseded by settled-tree reruns. Final account claim, launch,
capacity and cleanup modules all pass (the 79-test batch has only the expected
runtime-release pin failure). No existing assertion was relaxed for those errors.

The live reply-producer matrix was not run: it requires the soak profile,
working Unix sockets and native supervision. Installer/upgrade-handover service
checks likewise need the owner environment; S3 does not change their final
record formats. No PTY-specific check was required or run. Exact owner retry
commands are included below the completed commands. No test run remains in flight.

## Assertion audit and changed files

**No pre-existing test assertion was changed or removed.** Corrections and
strengthening of the new S3 fixture/assertions are paired in
[assertions.md](assertions.md). `git diff --check` and the identifier scanner pass.

Production: `hold_migration.py`, `learning_store.py`, `project_history.py`,
`run_holds_schema.py`, `project_holds_schema.py`, `runtime_files.py`.
Tests/metadata: `test_rsf_s3_resumable.py`, `lifetime_caps.json`, the layer,
stage, timing and requirement catalogues, `tests/requirements/rsf-s3.json`.
Documentation: `docs/requirements/rsf-s3.md` and this directory's status,
assertion audit, structured evidence, module list and commands.

This correction round changes only `hold_migration.py` (pending guidance),
`learning_store.py` and `project_history.py` (cleanup and copy gate),
`test_rsf_s3_resumable.py`, `tests/requirements/rsf-s3.json`,
`scripts/test-requirements.json`, the requirement document, and this directory's
`STATUS.md`, `assertions.md`, `evidence.json` and `commands.txt`. Other dirty
files are the original uncommitted S3 implementation. There is no new schema,
record shape, reason code or runtime catalogue change in this correction round.

No existing assertion was weakened, changed or removed; readiness assertions
were moved unchanged into the shared subprocess helper, as recorded in the
assertion audit. No commit, install or service takeover was performed.

## Primary socket/native reruns still required

This round does not claim the prior sandbox-blocked service/native checks are
green and did not repeat those known-restricted launches. The primary must use
a clean owner environment with a short TMPDIR and the named commands in
[commands.txt](commands.txt):

- Socket/service: `tests.test_learning_store`, `tests.test_learning_migration`,
  `tests.test_state_location`, `tests.test_account_ingress`,
  `tests.test_account_billing_admission`, `tests.test_billing_gate`,
  `tests.test_open_request_lifetime`, `tests.test_cleanup_receipt_lifetime`,
  `tests.test_event_retention`, `tests.test_e2a_launch_record`,
  `tests.test_health_contention`, `tests.test_learning_cli`,
  `tests.test_open_withdrawal`.
- Native supervision/process inspection: `tests.test_owned_execution`,
  `tests.test_project_start`, `tests.test_project_driver`.
- Installed update/takeover and live matrix: `tests.test_installer`,
  `tests.test_upgrade_handover`, and `tests.test_reply_shape_live` with the soak
  profile. These remain unrun here; no PTY-specific check was required.

No test process or full run remains in flight.
