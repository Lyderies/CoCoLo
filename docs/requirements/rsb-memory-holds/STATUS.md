# RS-B-4 memory holds — handoff

Implemented on `feat/rsb-memory-holds`, base `4a00f22d` (0.8.62), and left
uncommitted as requested. VERSION and the runtime digest catalogue are unchanged.
No schema step, installation, merge, push or live-service changes. No test run
remains in flight. The requirement map preceded tests and implementation.

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
