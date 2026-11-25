# RS-B-4 memory holds — handoff

Implemented on `feat/rsb-memory-holds`, base `230c2fc0` (0.8.62), plus accepted RS-F
S6 at `1aabb8e5`. Implementation and registration repair are committed through
`f69ff690`; the decision-286 review fixes below remain uncommitted as requested.
VERSION and the runtime digest catalogue are unchanged.
No schema step, installation, merge, push or live-service changes. No test run
remains in flight. The requirement map preceded tests and implementation.

## Review fix round (decision 286; current evidence)

This round changes only MED-1 and LOW-1 through LOW-4. Requirement text and
matching tests preceded the runtime changes. All checks use existing requirement
IDs; the two new methods are reciprocally registered under RS-B-4 in the existing
scenario module. No schema, persisted record shape, operation set or release
kind changed, so no new copier/backup/move/enumeration run is required.
`git diff --check` and the identifier scan pass; no absolute home paths or raw
logs were added to the documentation.

| Review item / requirement | Test or inspection | Evidence |
|---|---|---|
| MED-1 / RS-B-B7-1, RS-B-B7-6, RS-B-4 | `HoldsReportTests.test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields`; `MemoryHoldTests.test_registration_rollback_validator_and_status`; `test_status_memory_blocks_close_eligibility_and_has_holds` | RED before runtime edits, GREEN after. Orchestration is not_tracked with its pre-unit zero/null report, before and after the idle deadline and memory retirement. Memory kinds stay tracked; recovery has_holds still changes 1 to 0. Removed the orchestration eligibility query and corrected B7a's scope. |
| LOW-1 / RS-B-4 | `MemoryHoldTests.test_register_requires_explicit_journal_directory`; `test_register_uses_explicit_non_sibling_journal_directory`; all memory crash/lane checks | Omitted and null directories both RED (no refusal) then GREEN (history_invalid_input, no source or hold). Explicit non-sibling directory and recorded lock identity pass before/after; no red claimed for that compatibility case. Store/crash fixtures pass their own journal directory. |
| LOW-2 | Header here and in `rsb-memory-holds.md`, checked against git ancestry/history | Correct base 230c2fc0 plus accepted S6 1aabb8e5; implementation/repair committed through f69ff690, current review fixes uncommitted. VERSION/catalogue untouched. |
| LOW-3 / M4/M5 | Requirement limitation, inspected against `LaunchJournal.forget_instance` and `memory_holds.recover`; existing `MemoryCrashTests.test_M4_hung_and_missing_lock` | Documented the two-services/one-project window between recover passes and the resulting instance_unproven capacity leak. Rows remain held; absence never releases them. Existing missing-lock regression GREEN; no new test or fix of the stale-set window is claimed. |
| LOW-4 / M2/M5 | Requirement boundary, inspected against `RunDriver.settle_on_exit`; existing `DriverMemoryTests.test_M2_unknown_cleanup_keeps_both_holds` and `MemoryLaneTests.test_M5_graceful_stop_retires_rows_before_unlock` | Documented unknown-cleanup preparations retiring as retired at graceful stop even if workspace remains on disk, because holds count in-process capacity. Existing running-retention and graceful-stop checks GREEN; this item changes documentation only. |

Registered red run: 46 tests across `tests.test_holds_report` and
`tests.test_memory_holds`, 1.3 s wall, five assertion failure events (including
both missing-directory variants), `metadata_errors: []`. Final green run:
**68 tests / five modules / 5.6 s wall**, `failed_units: []`,
`metadata_errors: []`, `budget_failures: []`. Modules:

- `tests.test_holds_report` (26)
- `tests.test_memory_holds` (20)
- `tests.test_memory_holds_crash` (4)
- `tests.test_memory_holds_lane` (7)
- `tests.test_rsf_s6_classify` (11)

`tests.test_runtime_release`: three tests / 0.2 s wall, two pass and the current
version's digest check is expected RED; `metadata_errors: []`. The primary owns
VERSION/catalogue release bookkeeping. No socket or PTY test was run in this
round. The primary must rerun the changed budget pin and report transport in a
socket-capable environment, from the repository root with the supplied interpreter:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_b7b_budgets --module codex:tests.test_holds_report_socket
```

The budget module probes socket binding, so it was left entirely unrun here;
its numeric assertions remain unchanged. Rerun `tests.test_runtime_release`
after release bookkeeping. Earlier evidence/environment notes below describe
prior rounds, including the accepted primary 25-module repair gate.

Changed files this round (12): `docs/requirements/b7a.md`,
`docs/requirements/rsb-memory-holds.md`, this `STATUS.md`, `assertions.md`;
runtime `holds_report_contracts.py`, `project_holds_report.py`, `memory_holds.py`;
tests `test_holds_report.py`, `test_b7b_budgets.py`, `test_memory_holds.py`,
`memory_holds_fixture.py`; and `scripts/test-requirements.json`.
The four assertion replacements are paired in [assertions.md](assertions.md):
orchestration tracking/counts/eligibility revert to the pre-unit contract while
memory tracking, S6 historical-memory assertions and budget measurements remain.

## Earlier implementation evidence

The three new scenario modules have reciprocal RS-B-4 registry links and
`metadata_errors: []`. The final memory/limit run passes **33 tests in 5.595 s**:
18 memory-store/owner tests, seven actual driver/lane tests, four executable
crash/lock tests, and four lifetime-cap checks. The strengthened cleanup-gated
crash/successor module separately passes four tests in 5.172 s, including one
authenticated fence per recovered handoff. Exact timings,
case outcomes and red runs are retained in [evidence.json](evidence.json).

## Requirement → test → evidence

All rows serve **RS-B-4**. Module aliases below:

- **S**: `tests.test_memory_holds.MemoryHoldTests`
- **D**: `tests.test_memory_holds.DriverMemoryTests`
- **L**: `tests.test_memory_holds_lane.MemoryLaneTests`
- **C**: `tests.test_memory_holds_crash.MemoryCrashTests`

| Matrix row | Test names | Evidence |
|---|---|---|
| M1 | C.`test_M1_transition_kills`; C.`test_M1_entry_deleted_before_release_record`; L.`test_M1_writer_commit_waits_before_submit_and_retire_receipt_follows_actual_retire` | PASS. Parent-directed SIGKILL before registration commit, after commit, before submit, during a gated actual cleanup worker, after actual retire, and after driver deletion before its release write. Rollback leaves no registration hold; committed rows recover once as service_stopped, preserving an already retired preparation. Delayed writer answers cannot submit early. |
| M2 | D.`test_M2_unknown_cleanup_keeps_both_holds`; D.`test_M2_abandon_does_not_drop_entry_before_preparation_retirement` | PASS. More than 12 unknown-cleanup retries retain both rows and the entry; cleanup_unknown is recorded. Client abandonment cannot bypass retirement. The abandonment bypass ran red before its fix. |
| M3 | S.`test_M3_close_and_takes_both_orders` | PASS. Memory take first blocks unattended close; close first refuses registration/preparation with no new hold. Preparation take counts as new activity. |
| M4 | C.`test_M4_hung_and_missing_lock`; S.`test_M4_sweeper_cannot_prune_memory_stopping_evidence` | PASS. A real hung subprocess holds the flock. Held, deleted and replaced lock paths retain instance_unproven. Proof uses the recorded inode/device and the descriptor actually flocked. The pruning race ran red before its fix. |
| M5 | S.`test_M5_recovery_once_and_graceful_stop`; L.`test_M5_graceful_stop_retires_rows_before_unlock` | PASS. Repeated starts/reopen preserve the first release. Graceful stop clears actual retained owner/lane/preparation memory and records retired before unlocking. |
| M6 | D.`test_M6_autonomous_retirement`; L.`test_M6_denied_closed_run_retires_without_another_client` | PASS. Idle registered runs enter retirement; a verified denied run frees preparation and entry without a later client call and describes as run_closed. The actual denied-run test ran red before its fix. D.`test_existing_failed_view_oracle_remains_valid` reuses B3's unchanged bounded-view assertions. |
| M7 | S.`test_M7_registration_refusal_preserves_duty`; L.`test_M7_refused_registration_drops_entry_and_keeps_withdrawal_outbox`; L.`test_M7_faulted_writer_sends_one_memory_withdrawal` | PASS. Refusal removes the entry and leaves independent B3 withdrawal custody releasing. A genuinely faulted writer permits one fallback send. B3's 27 wire/store/import regression checks also pass. |
| M8 | D.`test_M8_submit_refused_compensates` | PASS. Committed preparation intent precedes submit; a raised submit compensates as submit_refused with no slot consumed, while retaining the driver entry. |
| I1 | D.`test_I1_interleaved_observations`; D.`test_I1_take_and_release_reply_interleavings` | PASS. Two deterministic 1,000-step interleavings across three identities include queued and committed-but-unobserved takes/releases, actual owner retirement, and equality at quiescence. |
| L4 | D.`test_L4_never_launch_predicates`; L.`test_L4_L6_ten_refused_fences_handoff_detaches_real_lane_and_same_service_lists` | PASS. Each of the four never-launch conditions is violated separately. Ten refusals commit handoff before actual lane/pool detachment; the lease remains armed until account evidence. |
| L6 | L.`test_L6_same_service_fences_handed_over_lease_once`; L4/L6 test above | PASS. The same live service lists and fences the detached lease once, with the verified closure releasing it; no restart or prepared worker remains. |
| L7 | C.`test_L7_handoff_kills` | PASS. Two SIGKILL checkpoints inside the actual writer handoff and actual lane detach retain sweep ownership and release stopped memory rows once. A real successor then fences each lease once. |

Additional passing checks cover transaction rollback, source/row validator
refusals, tombstone non-reuse, source-purge protection and B7's tracking,
close eligibility and has_holds counts. Missing-source, purge, status and the
unchanged B3 view oracle each exposed a red failure before its implementation fix.

## Implementation boundaries

`memory_holds.py` holds the store predicates and owner request bookkeeping.
Registration inserts its source and memory row in the existing registration
transaction; B3's independent open duty still commits first. Preparation intent
is durable before submit. Release follows actual retire, including given-back
preparations; unknown cleanup remains charged. Handoff transfers the armed lease
and binding duty to sweep before detaching local launch authority.

The existing schema already has instance, evidence, releasing and all required
release kinds. New registrations use the journal's 32-hex instance identity;
legacy reporting-only svc instances remain historical. Validators check the new
source/row relationship. No historical memory is reconstructed, and S6's
migration/classification code is unchanged. Its temporary not_tracked display is
superseded; its zero-invented-memory assertion is retained. All take_hold calls
remain inside their owning project transaction under S2's fence.

The journal retains stopping evidence until the writer has checked memory
obligations. Missing, replaced or contended locks never release rows. Graceful
stop records retired before unlocking; an uncertain/faulted exit keeps the lock
file for the next start. B6 eligibility and B7 status now include tracked memory.
The runtime file inventory includes the new module; the digest catalogue does not change.

## Named validation and remaining environment limits

- Core named batch: **195 pass / 10 modules / 7.471 s**, including hold schema,
  B6, B7 reports/recovery, S6 classification, orchestration, new memory modules
  and the cap guard. Later focused runs cover the subsequent owner fixes and
  stronger executable checkpoints.
- Final memory/limit batch: **33 pass / 5.595 s**, `metadata_errors: []`.
  The later cleanup-gated crash-only run is green, also with empty metadata errors.
- Updated B7 measurement batch: **34 pass, one service-startup skip / 29.318 s**.
  Existing B3/B4/B5/S3 limits and 0/100,000-tombstone VM comparisons pass unchanged.
  Memory writer transactions, including commit, have three measured samples and
  a median total **1.017 ms** against the 5 ms limit; positive-lock recovery releases both memory rows.
  Full normal-run B1 and generic recurring-arrival B6 budgets remain explicitly
  unmeasured. Memory-only samples do not claim those wider budgets.
- Copier/backup/restore/source/reply/enumeration batch: **282 tests / 50.427 s**,
  empty metadata errors. It had 279 passes, two sandbox migration failures and
  one superseded B7 tracking assertion. The B7 assertion and harness were then
  updated together and passed above. The 17 copier/enumeration modules completed;
  exact module lists and outcomes are in evidence.json.
- B3 wire/store plus client import purity: **27 pass / 0.806 s**. Runtime file-set
  inventory: **one pass**. The expected runtime digest assertion remains red
  until the primary's release bookkeeping; VERSION/catalogue were not changed.
- `tests.test_holds_report_socket`: two tests cannot bind Unix sockets
  (`PermissionError`, errno 1). No PTY is required by this unit.
- Untouched-base comparison proves identical existing failures: six in
  `test_result_persistence`, one in `test_result_lease_lane`, nine failing kernel/watchdog methods (ten unittest failure events) in
  `test_launch_journal`, and two in `test_learning_migration`. These need native
  worker/process inspection or service/socket privileges. No assertions were
  changed to hide them. Baseline runs took 11.087 s, 21.782 s and 12.385 s.
- `test_reply_shape_live` was stopped after 138 s when inspection showed that
  the named module recursively runs many unrelated driver modules. The runner
  reaped its owned workers. It is not claimed as complete; no such run is left
  active. Static reply-shape compatibility passed. The primary can choose an
  appropriate unsandboxed composite run separately.
- `git diff --check` and the repository identifier scan pass. No raw logs,
  temporary locations or absolute home paths are stored in this handoff.

Primary socket rerun, from the repository root with the supplied test interpreter:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_holds_report_socket --module codex:tests.test_learning_migration
```

The affected native worker modules above also need a primary environment with
process inspection. No full suite was requested or completed.

## Changed files and assertions

The exact inventory is [files.json](files.json). Runtime changes are
`memory_holds.py`, `project_driver.py`, `result_persistence.py`,
`project_history.py`, `project_holds_schema.py`, `launch_journal.py`,
`orchestration_store.py`, `holds_report_contracts.py`, `project_holds_report.py`,
`recovery_report.py` and `runtime_files.py`. Other changes are the three new test
modules and executable fixture, B7 measurement script/test, two status tests,
lifetime-cap registry, test metadata/requirement sources and requirement docs.

Four existing test expectations change solely for the accepted tracking/status
transition and its honest measurement scope. Each old → new replacement is in
[assertions.md](assertions.md). Existing custody, numerical budget and B3 view
assertions remain intact. Independent owner review and release bookkeeping are
left to the primary.

## Repair round (builder repair, after the S6 merge)

**Regression found unsandboxed.** The build's "baseline failures" were not baseline:
at the build commit and at the S6 merge, `tests.test_project_driver` failed in all 11
fragments (dispatch answered `run_failed`; undispatched runs classified
`already_closed` instead of `abandoned`), and `tests.test_project_hold_register` and
`tests.test_open_withdrawal` failed too. All three pass at base 0.8.62.

**Root cause.** `memory_holds.registration_hold` located the registering instance's
journal lock at `<history directory>.parent / launches`. In a real service the
history directory *is* the project directory (`project_service.history_directory`:
`<state>/projects/<id>`) and the driver's journal is `<state>/projects/<id>/launches`,
so the lock was looked for in `<state>/projects/launches`, never found, and every
registration transaction rolled back with `history_service_unavailable`. The new
memory tests passed only because their fixtures put the history in a sibling
`history`/`memory-store` directory, where the guess happens to be right.
A second, smaller defect: `_unheld_reply` read `self._retired` unconditionally,
which a driver without memory custody (the warm-up gate tests) never sets.

**Fix (code only; no existing test edited).**
- `result_persistence.py`: the lane's `register` request passes the attached
  journal's own directory (`self._memory_journal.directory`), or `None` without memory.
- `project_history.record_registered_run(memory=...)` now takes that directory
  (`None` = reporting only) and hands it to `registration_hold`.
- `memory_holds.registration_hold(..., launches)` uses the given directory and no
  longer infers it. That repair left a sibling-directory fallback in store-level
  `register`; LOW-1 in the review fix round below removes that fallback too.
- `project_driver._unheld_reply`: the retired-run lookup runs only with memory custody.

**Evidence.** Red before the fix: one `HostVerificationTests` dispatch reproduced
`run_failed`; an instrumented writer showed `registration_hold` raising on the missing
lock path above. Green after: the named gate of 25 modules (38 units, 770 tests,
121.8 s wall, `metadata_errors: []`, `budget_failures: []`, `failed_units: []`),
including `test_project_driver` (11 fragments), `test_project_hold_register`,
`test_open_withdrawal` and the three memory-holds modules. `test_runtime_release`
is expected to fail until VERSION and digests are bumped (not done here).

**Assertion review.** The holds-report and S6 memory `tracked` changes follow
decision 129.1 A (memory kinds report `not_tracked` only until this slice).
Decision 286 keeps orchestration untracked in this slice; its review-head
tracking and eligibility changes are reverted below. The budget assertion keeps
B1/B6 `unmeasured` with reasons and adds memory-only numbers; this round retains
that scope and restores orchestration to the untracked-kind pin.
The `lifetime_caps.json` entry registers the new bounded tombstone map (a registry
addition, not a changed assertion).
