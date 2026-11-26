# S9 — Targeted follow-up (040-s9-r2c)

Reviewed baseline: `feat/s9-hm4` at `856c91b7`. Confirmed the primary repo's
`reviews/040-s9-r2c/RESULT-claude.md`: **READY** (static review), with the MED
maintenance finding and two LOW findings listed below. Independently reviewed
and verified WIP `5b30cd61` on 2026-11-26; no further runtime/test changes were
needed. All three fixes pass the named health modules. The additional full
learning-store module has only the five sandbox socket errors listed below.
Finish documentation/evidence remains uncommitted in this worktree, as requested.
VERSION and runtime-releases.json are unchanged.

## Current requirement → test → evidence

All five new methods below belong to
`tests.test_health_ledger_fix.LedgerFixTests`.

| Requirement | New regression | Red → green evidence |
|---|---|---|
| S9-HM4-5 (MED) | test_project_deletion_cancels_snapshot_and_later_check_completes | Red: deletion raises `maintenance requires all external readers to close`. Green: real owner deletes the project in <750 ms, snapshot release is acknowledged before deletion returns, cancellation does not advance completion, and a newly created copy later completes `ok`. |
| S9-HM4-5 (MED) | test_any_exclusive_maintenance_releases_health_snapshot_on_owner | Red: the shared maintenance entry point fails at its checkpoint. Green: owner-thread exclusive maintenance succeeds in <750 ms after snapshot release. |
| S9-HM4-5 (MED) | test_maintenance_releases_snapshot_while_cleanup_is_pending | Red: a detached copy awaiting asynchronous cleanup still blocks maintenance. Green: it stays registered until its reader closes; maintenance cancels it and succeeds in <750 ms without waiting for deferred cleanup. |
| S9-HM4-2 (LOW) | test_sustained_growth_alarms_immediately_after_each_daily_renewal | Red: sustained growth loses its warning 1 ms after renewal. Green: warnings persist at renewal, +1 ms, +4 min, and immediately before/at six-hour maturity, across three daily renewals. Existing burst/truncate and exact-boundary tests still pass. |
| S9-HM4-1 (LOW) | test_copy_full_and_io_errors_report_space_or_io_without_damage | Red: injected SQLITE_FULL (13), IOERR (10), and extended IOERR_WRITE (778) all report `store_corrupt`. Green: all report `store_unreadable` with “space or IO”, no damage/private-path text and no completion timestamp. Existing real corruption tests still pass. |

Independent red verification used the **exact native-package archived from
`856c91b7`**, with both `health_ledger.__file__` and `learning_store.__file__`
asserted inside that archive before loading the current five named regressions.
**Five red in 0.369 s**: three maintenance errors with
`maintenance requires all external readers to close`, a missing growth warning
at renewal +1 ms, and FULL/IOERR/IOERR_WRITE incorrectly reporting `store_corrupt`
(four assertion failures including subtests). This was run before the green
checks below. No test assertion was changed or removed in this finish pass, nor
by the targeted follow-up relative to `856c91b7`.

## Current final verification (independent finish pass)

- From `packages/codex-agents`, `TMPDIR=/tmp/claude-501/s9 PYTHONPATH=../../scripts
  python -B -m unittest tests.test_health_ledger_fix tests.test_health_ledger
  tests.test_health_holds tests.test_lifetime_cap_guard`: **83 PASS, 5.404 s**.
  The 300 MiB copy completed in **9/80** busy passes, max pass **0.151 s**,
  with **375** independent writer commits.
- Named runner for those same four modules, with the handoff's test venv Python:
  **83 PASS**; worker elapsed **3.187 s**, summed workers **8.512 s**, preflight
  **10.517 s**, discovery **2.867 s**. **`metadata_errors: []`**,
  **`budget_failures: []`**, all worker return codes zero. Module seconds:
  health_ledger_fix **3.726**, health_ledger **1.653**, health_holds **2.017**,
  lifetime_cap_guard **1.116**. Existing module layers/stages/budgets and existing
  S9 requirement IDs are retained; all five regressions are registered in
  `scripts/test-requirements.json` against `tests/requirements/s9.json`.
- Additional named module, same TMPDIR/PYTHONPATH:
  `python -B -m unittest tests.test_learning_store`: **100 run, 95 PASS,
  5 socket errors, 13.840 s**. All five fail at `listener.bind` with
  `PermissionError: [Errno 1] Operation not permitted`; no fixes or assertion
  changes were made for them. Existing external-reader refusal, deletion,
  expiry, artifact/export/backup and migration tests pass. The primary must
  rerun the module outside this sandbox. Failing LearningStoreTests methods:
  `test_app_server_cli_normalizes_and_ingests_through_collector`,
  `test_collector_rejects_forged_binding_and_unavailability_is_bounded`,
  `test_collector_serializes_concurrent_clients`,
  `test_second_collector_cannot_replace_the_active_socket`,
  `test_spoofed_project_manifest_cannot_replace_active_collector`.
- The system Python's first metadata-run attempt stopped before workers because
  it lacks `hypothesis`. Re-running with the handoff test venv produced the clean
  metadata result above; no dependency or harness code was changed.
- `git diff --check`: PASS. No schema, learning-record shape, derived observation
  shape, operation or reason-code changes. The previous growth baseline remains
  persisted until its in-memory replacement matures; candidates are removed on
  reset/eviction/pruning. Exact default rate equality remains quiet, as required
  by the existing strict-over boundary test; the renewal regression exercises
  sustained growth above that threshold across three renewals.
- No full-suite/affected run, pty, services, install, push or commit. No test
  process remains running. Durable finish results: [finish-evidence.json](finish-evidence.json).
  The prior-round integration rerun list below remains applicable. Runtime digest
  release bookkeeping remains the primary's responsibility; its prior-round
  expected mismatch was not rerun during this finish pass.

Targeted follow-up files (already in WIP): health_ledger.py, learning_store.py,
test_health_ledger_fix.py, lifetime_caps.json, scripts/test-requirements.json,
docs/requirements/s9.md, STATUS.md and assertions.md. This finish pass updates
STATUS.md/assertions.md and adds finish-evidence.json only.

Maintenance coordination is at `_begin_exclusive_maintenance`, covering deletion
and expiry alike. Its owner-only registry signals every health worker, awaits
the connection-release acknowledgement, then uses the existing checkpoint and
maintenance timeout. Snapshot thread joins and scratch deletion stay on cleanup
workers. Cancelled copies never reach scanning; a later pass creates a fresh one.

## Historical fix round (040-s9-r1)

Branch `feat/s9-hm4`, starting HEAD `3941c7a0`, base `a737ae9d`.
One fixed-scope review round; changes left uncommitted because this builder is
sandboxed. VERSION and runtime-releases.json are unchanged. No socket/pty test
was attempted. No subagents or full-suite/affected run was used.

## Requirement → test → evidence

Methods without a prefix are in `tests.test_health_ledger.LedgerHealthTests`;
`Fix.` means `tests.test_health_ledger_fix.LedgerFixTests`.

| Requirement | Tests | Evidence |
|---|---|---|
| S9-HM4-1 | test_quick_check_every_visit; Fix.test_300_mib_snapshot_and_scan_finish_under_process_writer; test_corrupted_copy_reported_without_private_text; test_full_corruption_latches_until_full_clean_result | MED-1 red: two live quick checks rather than zero. Green: quick/full VMs run on the finished copy and resume; 300 MiB gets `ok`, no pending finding. Existing corruption/privacy/latching cases PASS. |
| S9-HM4-2 | Fix.test_short_bursts_after_renewal_and_truncate_are_quiet; test_growth_rate_boundary_and_clock; test_size_boundary_report_only | MED-2 red: false alarm on 3.6 MB in four minutes. Green: daily renewal and size-drop/truncate cases are quiet, sustained >1 GiB/day warns. Exact rate and size boundaries remain PASS. Six-hour minimum window. |
| S9-HM4-3 | Fix.test_future_baseline_is_reset_after_clock_correction; test_completed_clock_and_growth_survive_restart; test_daily_completion_boundaries; test_incomplete_copy_does_not_complete_full_check | LOW-1 red: future baseline retained. Green: reset to corrected time/size, subsequent real growth detected. Completion-only clock, restart and 24-hour boundaries PASS. |
| S9-HM4-4 | Fix.test_shared_settings_and_initial_status; Fix.test_one_byte_limits_rejected; Fix.test_shared_settings_typed_bounds_and_no_import_io; test_default_settings_are_editable_outside_protected_runtime; test_settings_reload_and_status | LOW-2/3 red: missing shared module/shared file ignored. LOW-5 red: 1-byte threshold accepted. Green: one typed loader/file, actual pre-pass status, 1 MiB floor, bounds/duplicate/unknown/bad-file/symlink fallback. |
| S9-HM4-5 | Fix.test_copy_never_loads_another_sqlite_library; Fix.test_snapshot_close_preserves_owner_wal_write_lock; Fix.test_300_mib_snapshot_and_scan_finish_under_process_writer; Fix.test_copy_deletion_and_scan_join_are_off_owner; test_wal_copy_allows_writer_between_page_slices; test_owner_thread_and_total_budget | HIGH-1 red: second-library lookup and foreign process acquired owner's held WAL write lock. HIGH-2 red: no completion in 80 passes and copied later row. LOW-4 red: scan join on owner. All green: existing sqlite3 only, pinned snapshot excludes later row while live writer commits, async deletion/join, bounded owner wait. |
| S9-HM4-6 | test_registration_and_project_scope; test_warning_reaches_owner_attention; test_deleted_projects_drop_retained_observations; test_past_observation_cache_limit_still_reports_old_project_status | Existing scope, owner attention, retained warnings, pruning and deferred registration regressions PASS. The attention fixture now uses a valid 1 MiB limit and a real 2 MiB payload. |

Red evidence was captured before production fixes (initial regression run: eight
tests, 16.246 s, seven failures plus one error; includes cleanup's repeated failure).
The burst regression was separately red in 0.016 s. The three changed
quick/snapshot/default-path cases were separately red in 0.032 s. Extra loader
bounds/import checks pin already-satisfied behavior rather than claim new red bugs.
Exact old → new assertion replacements are in [assertions.md](assertions.md).

## Final verification

- Named runner: `codex:tests.test_health_ledger` (31),
  `codex:tests.test_health_ledger_fix` (10), `codex:tests.test_health_holds` (33),
  `codex:tests.test_lifetime_cap_guard` (4): **78 PASS**. Worker elapsed 4.5 s,
  summed workers 10.542 s; `metadata_errors: []`, `budget_failures: []`, no failed
  units. Preflight 12.363 s, discovery 4.368 s. Existing budget debts for unrun
  modules are printed as `not_measured`, not failures.
- The 300 MiB real WAL store completed copy + resumable quick + full scan in
  **17 of 80 busy passes**, max entire pass **0.156 s** (stronger than the 750 ms
  owner bound). The separate process committed **610** times, stayed alive through
  completion, and live `integrity_check` was `ok` after stopping that exact child.
- Additional named unittest run: **18 PASS, one expected FAIL, 0.964 s**.
  PassBoundsTests, FailureTests, RecordTests, HealthyTests in test_health_monitor,
  and the socket-free E1b-1 trace case
  `test_sql_trace_without_transport_covers_wakes_sweeps_restart_and_close` pass.
  Of test_runtime_release's three cases, exact runtime file set and version
  ordering pass; only `test_current_runtime_is_recorded_for_the_current_version`
  fails because the runtime changed while VERSION/catalogue remain untouched.
- `git diff --check`: PASS. No database, learning-record, saved-observation shape,
  operation or reason-code changes; broader copier/migration runs are not required
  by this fix. No tests, budgets or assertions outside S9 were changed.
- Durable results and red descriptions: [fix-round-evidence.json](fix-round-evidence.json).
  This handoff does not depend on temporary runner log paths.

Reproduce the registered non-socket run from the worktree root with the provided
venv's Python:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_health_ledger --module codex:tests.test_health_ledger_fix --module codex:tests.test_health_holds --module codex:tests.test_lifetime_cap_guard
```

## Implementation and shared settings decision

There is no ctypes, find_library, CDLL or second SQLite image in the copy path.
Python's `Connection.backup` loops in a dedicated worker and yields in its
progress callback after each eight-page grant. BEGIN + an actual schema read pins
one read snapshot before backup. The dedicated read-only connection is created,
queried and closed exclusively by that worker; the mutable owner connection is
never passed to it. This uses the user-authorized dedicated read-only snapshot
option. The original mutable ledger owner still owns enumeration and its own SQL;
its exact-thread and transaction assertions remain. No mutable transaction or
cursor crosses the owner handoff. The E1b-1 socket-free trace regression passes.

A shared 500 ms absolute deadline bounds owner waits; a busy foreground queue
reduces copy waits to 150 ms. One backup worker and one private-copy scan worker
are retained per check. On completion/error, the owner detaches resources to one
cleanup worker; no scan join or multi-GiB deletion happens in the owner request.
The next copy waits for cleanup, bounding retained copies/workers. close() joins
cleanup outside the owner call. Quick/full copy VMs pause and resume rather than
restart on the live database. WAL writers commit while the snapshot is pinned;
exclusive maintenance cancels the health snapshot and awaits reader release
before checkpoint truncation. The review correctly identified that the old
“checkpoint truncation can wait” claim was false: the 50 ms timeout raised instead.

ONE account-state `user-settings.json` and `codex_agents.user_settings.load(root)`
now own validated defaults and warnings. Typed keys are `store_size_bytes` (8 GiB),
`store_growth_bytes_per_day` (1 GiB/day), `disk_free_bytes` (2 GiB),
`disk_free_percent` (5), `no_merge_warning_ms` (5,400,000 = 1.5 h).
Defaults record decisions 340/341. S9 uses the first two; S10/S35 will consume the
already-accepted remaining keys using this same loader/file. S9 status displays
the values in force before its first pass and reloads each pass. Validation and
configuration examples are in docs/configuration.md. No automatic migration from
the unshipped per-feature health-limits.json is needed or introduced.

Registration/shutdown wiring remains deferred to the primary, as at review HEAD:
`health_ledger.register(self.health_monitor, self.state)`; retain the check and
call close() during collector shutdown before closing the ledger owner. No
primary-owned learning_store.py or collector/service wiring was edited in that
prior round. This targeted follow-up now changes the shared learning_store.py
maintenance entry point as described above; collector/service wiring stays deferred.

## Primary must rerun (socket restriction / integration gates)

- Full `codex:tests.test_learning_store` (five socket errors named above).
- Full `codex:tests.test_health_monitor`, including collector-scope, StatusTests,
  SweepTests and remaining scope cases.
- Full `codex:tests.test_ledger_owner_wiring` (the socket-free trace case already
  passes) and `codex:tests.test_cli_status_client_boundary` after register/close wiring.
- `codex:tests.test_project_runtime`, `codex:tests.test_dispatch_client_runtime`
  for protected runtime packaging, and socket-backed `codex:tests.test_state_location`.
- The primary's affected run against `a737ae9d`, lifecycle macOS/Linux flows,
  Linux SQLite/WAL snapshot validation, other-provider review and full-suite gates
  in a final/separate checkout. No socket or pty permission was requested here.
- Prior-round debt remains: compare unchanged `codex:tests.test_owner_backup_streamed`
  against the base for its previously reported 60 s budget overrun (40 tests passed,
  77.9 s elapsed). This round did not rerun or change that module/budget.

## Files changed in this fix round

- Runtime: health_ledger.py; new user_settings.py; runtime_files.py (exact file set).
- Tests: test_health_ledger.py; new test_health_ledger_fix.py; lifetime_caps.json.
- Registration: scripts/test-layers.json, test-stages.json, test-timings.json,
  test-requirements.json; tests/requirements/s9.json.
- Documentation: docs/configuration.md; docs/requirements/s9.md; this STATUS.md;
  assertions.md; new fix-round-evidence.json.
