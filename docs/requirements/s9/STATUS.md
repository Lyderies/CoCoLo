# S9 — Fix round (040-s9-r1)

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
checkpoint truncation can wait for snapshot release, as with any WAL reader.

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
primary-owned learning_store.py or collector/service wiring was edited.

## Primary must rerun (socket restriction / integration gates)

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
