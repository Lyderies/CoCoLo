# Unit 3.8 slice 4 status

Base `230c2fc0` (VERSION 0.8.62), branch `feat/ap5-evidence-lookup`.
Implementation and requested requirement checks complete; changes left uncommitted
in the named worktree because this session cannot write Git metadata.

## Requirement evidence

The complete requirement → test → evidence map is in [ap5-lookup.md](../ap5-lookup.md).

- AP5-2: `CandidateEvidenceLookupTests.test_AP5_2_fixed_links_have_flat_vm_work_as_unrelated_records_grow`
  failed before code (10,076 → 210,063 steps), passes after code (164 → 148);
  the former scan grows 8,005 → 148,005. A preliminary implementation exposed
  SQLite choosing the type index for invalidation (4,651 → 94,623); explicit
  digest-index selection closes that second scan without changing the test.
- AP5-3: `test_AP5_3_mixed_evidence_answers_and_link_order_match_former_scan`
  and `test_AP5_3_public_admission_duplicates_and_history_order_match` pass
  before and after replacement. Expected refusals are asserted independently
  as well as compared to the former algorithm. Public append, duplicate replies,
  stored evidence order and history sequence/order match.
- AP3: `test_AP3_digest_index_is_additive_on_first_open` and
  `test_AP3_indexed_copy_opens_reads_and_appends_with_real_previous_release`
  failed before the index existed and pass afterward. The latter extracts the
  recorded previous release's real native package using `git archive` from
  source commit `782a7eed`, recorded as 0.8.61 in `previous-releases.json`.
  Isolated Python imports that source (checks its path, release and schema 17),
  opens a populated indexed copy, checks all original records/order/late values,
  validates candidate links, writes a late event, checks SQLite integrity, and
  reopens. Current code then reopens and verifies the copy and prior write.
  No release-code monkeypatch, schema downgrade, reauthorization or service used.

## Named runs

Use the prepared test interpreter as `$PY` from the repository root. Every runner
invocation below reported `metadata_errors: []`; the new module is registered as
scenario/integration and AP3/AP5-2/AP5-3 have reciprocal requirement-source links.
Times below are runner worker wall time, with preflight shown separately.

1. Red: `$PY -B scripts/parallel_tests.py --module codex:tests.test_candidate_evidence_lookup`:
   5 tests, 3 expected failures, 2 behavior checks pass; 2.6 s, preflight 6.90 s.
2. Final focused/source/copy run: modules `tests.test_candidate_evidence_lookup`,
   `tests.test_type_lookups`, `tests.test_append_lookup`,
   `tests.test_learning_source_image`, `tests.test_learning_source_image_legacy`,
   `tests.test_learning_source_transfer`, `tests.test_backup_installed_project`,
   `tests.test_backup_reply_private`, `tests.test_account_observation_backup`:
   **80/80 pass**, 5.5 s, preflight 6.90 s.
3. Additional copier/enum run: `tests.test_candidate_evidence_lookup`,
   `tests.test_learning_migration`, `tests.test_large_project_backups`,
   `tests.test_owner_backup_streamed`, `tests.test_refusal_steps`,
   `tests.test_reply_shape_compatibility`: **85/87 pass**, 22.3 s,
   preflight 6.96 s. The two migration CLI failures are described below.
4. Final existing-store/runtime run: `tests.test_learning_store` and
   `tests.test_runtime_release`: **97/103 pass**, 6.5 s, preflight 6.99 s.
   Store: 95/100 pass; five socket-bind errors. Runtime: 2/3 pass; the sole
   failure is the intentionally unchanged release digest catalogue.
5. Baseline classification: extract untouched `230c2fc0` Codex package/tests
   and scripts using `git archive` into a disposable temporary directory;
   run `$PY -B -m unittest tests.test_learning_migration` from that copy's
   `packages/codex-agents` with its scripts on `PYTHONPATH` and a short `TMPDIR`.
   **15/17 pass**, 17.22 s. The same two CLI failures reproduce at the same
   assertions: `MigrationCliTests.test_command_moves_reports_refuses_and_rolls_back`
   (project-store cycle falls back to the retired account ledger) and
   `MigrationCliTests.test_first_start_of_the_project_service_moves_the_history_by_itself`
   (`orchestration_client_unavailable`). These are baseline sandbox/service
   failures, not changes caused by this index.

For runs 2–4, prepend `codex:` and pass each listed module using its own
`--module` argument. No full suite, affected selection, live install, commit,
push or service-owner mutation was performed. No pre-existing assertion changed;
see [assertions.md](assertions.md).

## Limits and handoff

Unix socket binds are denied by this sandbox. Existing collector socket tests
cannot complete here; the CLI migration checks that require the project service
also fail. The older-release compatibility test uses a real subprocess but no
socket or PTY, and passes here. No PTY test is required by this slice.

Blocked existing `LearningStoreTests` methods:

- `test_collector_rejects_forged_binding_and_unavailability_is_bounded`
- `test_second_collector_cannot_replace_the_active_socket`
- `test_app_server_cli_normalizes_and_ingests_through_collector`
- `test_collector_serializes_concurrent_clients`
- `test_spoofed_project_manifest_cannot_replace_active_collector`

The store tracebacks report `PermissionError` at the Unix listener's `bind`,
wrapped as collector endpoint unavailable. The primary should rerun these five
checks and the two named migration CLI checks in an environment allowing sockets.

`test_runtime_release.test_current_runtime_is_recorded_for_the_current_version`
is expected to fail because the collector runtime changed. VERSION and
`runtime-releases.json` are deliberately untouched; the primary owns release
bookkeeping at merge. The index is additive only; record/table/column formats
are unchanged. Project-history and project-holds builders were not edited.
The first-open measurement requested in the review is completed in the fix round
below. Full-suite validation remains for the primary.

## Fix round

Scope: review corrections MED-1, MED-2 and LOW only, on `d8d6e192`, based on
`230c2fc0` (VERSION 0.8.62). No owner ledger was read or copied. Changes are left
uncommitted in this worktree as instructed for the sandbox.

### MED-1 / AP3: large synthetic first-open cost — PASS

`CandidateIndexBuildTests.test_AP3_large_synthetic_first_open_index_cost` builds
a new disposable account ledger using **230,000 public `LearningLedger.append`
calls**, one project, unique event IDs/content digests, and normal optional
configuration/tool/item digest fields. Every append reports stored and the exact
next sequence. No bulk SQL fixture inserts, mocked writes, or owner data.

The ledger reaches 306.9 MiB before compaction. After dropping the new index,
populating, compacting and checkpointing, the pre-index database is **286.461 MiB
(300,376,064 bytes)**. Normal `LearningLedger` open recreates the index under its
usual writer fence. A timing wrapper delegates the actual connection `execute`
unchanged and measures just that `CREATE INDEX`; wall-clock open timings include
the rest of normal startup. Checkpointed database growth is **24.984 MiB
(26,198,016 bytes)**, to **311.445 MiB (326,574,080 bytes)**. Both opens retain
all 230,000 rows and the index exists.

| Measurement | Seconds |
| --- | ---: |
| Synthetic population through public append | 54.632 |
| First open with index absent | 0.205403 |
| Actual digest index build, within first open | 0.198591 |
| Reopen with index already present | 0.005369 |
| Idempotent existing-index DDL, within reopen | 0.000048 |

**The build is below one second at this synthetic scale.** This is a local macOS,
Python 3.14 / SQLite 3.53.4 measurement with filesystem caches warmed by synthetic
population/compaction, not a cold-disk latency guarantee. The fixture consists of
synthetic events; it does not measure a growing feedback history.

The initial minimal-event fixture also passed all measurement assertions:
260,000 public appends, 272.891 MiB compacted database, first open 0.246943 s,
index build 0.240047 s, reopen 0.005515 s, index growth 28.246 MiB. Its 63.6 s
worker time exceeded the scenario budget. Filling the three ordinary optional
digest fields reaches the requested byte scale with fewer real appends; all
assertions are unchanged. The final worker is **56.5 s**, below the 60 s budget.

### MED-2 / AP3, AP5-3: index recovery and plain errors — PASS

- `test_AP3_reopen_recreates_digest_index_and_preserves_lookup_answers`: drops
  the populated ledger's index, reopens normally, then invokes **both** pinned
  queries. Accepted evidence and owner-rejected evidence give the same answers
  as the frozen former validator; invalidation returns false/true respectively.
- `test_AP5_3_missing_digest_index_gives_plain_ledger_error`: drops the index on
  the live connection and invokes each query independently. Red: two raw SQLite
  `OperationalError`s. Green: both raise only `LedgerError` with the exact plain
  message `candidate evidence lookup is unavailable; reopen the ledger`.
  Both query sites translate SQLite failures, suppress raw exception chaining,
  and the index-creation site documents the normal-open invariant.

### LOW / AP3: unavailable previous source in shallow CI — PASS (allowed fallback)

The checked-in previous-release catalogue contains the Claude bundle and a
source-commit pointer, not the previous Codex runtime. Compatibility therefore
still extracts the actual recorded 0.8.61 Codex source. If Git, the recorded
object, or the archive operation is unavailable, it now fails plainly naming
**release 0.8.61 and source commit `782a7eed`**, with shallow-CI fetch guidance.
It never skips or substitutes current code. Existing real previous-runtime
version/path/schema/read/validate/append/integrity/reopen assertions remain.

`test_AP3_missing_previous_source_has_actionable_failure` was red for missing
object, archive timeout and missing executable (three raw exceptions), then green
for all three explicit failures. The unchanged real previous-release compatibility
test also passes with the source available here. Assertion accounting is in
[assertions.md](assertions.md): raw archive failure → explicit named-source
failure; no existing compatibility assertion was weakened or removed.

### Named validation and handoff

All fix-round runner invocations use `$PY -B scripts/parallel_tests.py` from the
repository root with the listed modules supplied as separate `--module codex:…`
arguments. Every invocation reports **`metadata_errors: []`**. The large module
is registered in layers (scenario), stages (integration), timings, and AP3's
requirement checks. No new requirement IDs or record/schema shapes were added.

1. Red, `tests.test_candidate_evidence_lookup`: 8 tests, 5 error subcases in the
   two new diagnostic tests; 2.2 s worker, 7.09 s preflight. The new normal-reopen
   test already passed and needed no recovery implementation change.
2. First measurement/regression run: `tests.test_candidate_index_build`,
   `tests.test_candidate_evidence_lookup`, `tests.test_append_lookup`,
   `tests.test_type_lookups`, `tests.test_runtime_release`: **18/19 tests pass**,
   63.6 s wall / 69.222 s worker sum, 7.16 s preflight. Measurement assertions
   pass; initial measurement module is 3.6 s over budget, corrected as above.
   The only test failure is the expected unchanged runtime-release digest pin.
   Append/type regression modules pass **7/7**, workers 1.1 s / 1.4 s.
3. Final run, `tests.test_candidate_index_build` and
   `tests.test_candidate_evidence_lookup`: **9/9 pass**, 56.5 s wall / 59.198 s
   worker sum, 6.92 s preflight; **no budget failures**, no requirement failures.
   This includes the final explicit-error wording and real previous-release test.
   AP5-2 remains flat: 162 → 146 VM steps, former scan 8,004 → 148,004.

VERSION and the runtime digest catalogue are unchanged. The primary still owns
release bookkeeping; the expected runtime pin failure is not suppressed. No
full suite or unrelated copier/backup run was needed: this round changes only
error translation and tests/docs, not tables, columns or record shapes. No
requested check needs a socket or PTY. The existing sandbox socket limitations
and primary reruns listed above remain; those modules were not rerun this round.

## Files changed

- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/learning_store.py`
- `packages/codex-agents/tests/test_candidate_evidence_lookup.py` (new)
- `packages/codex-agents/tests/test_candidate_index_build.py` (fix round, new)
- `scripts/test-layers.json`
- `scripts/test-stages.json`
- `scripts/test-timings.json`
- `scripts/test-requirements.json`
- `tests/requirements/ap5-lookup.json` (new)
- `docs/requirements/ap5-lookup.md` (new)
- `docs/requirements/ap5-lookup/STATUS.md` (new)
- `docs/requirements/ap5-lookup/assertions.md` (new)
