# S9 handoff

Branch: `feat/s9-hm4`. Base: `a737ae9d`. Implementation is left uncommitted in
this sandbox. No VERSION or runtime digest catalogue edits. Registration remains
deferred with S8; this is builder completion, not the plan's acceptance/full-suite gate.

| Requirement | Matching tests (test_health_ledger.LedgerHealthTests) | Evidence |
|---|---|---|
| S9-HM4-1 | test_corrupted_copy_reported_without_private_text; test_healthy_copy; test_quick_check_every_visit; test_failed_full_scan_reported_without_completion; test_full_corruption_latches_until_full_clean_result | Initial missing-module red; full-scan failure test also red before its fix; final PASS. |
| S9-HM4-2 | test_size_boundary_report_only; test_growth_rate_boundary_and_clock; test_warning_reaches_owner_attention | Initial missing-module red; final PASS. Size tested below/equal/above 8 GiB; rate below/equal/above 1 GiB/day, both scopes. |
| S9-HM4-3 | test_daily_completion_boundaries; test_incomplete_copy_does_not_complete_full_check; test_completed_clock_and_growth_survive_restart | Initial missing-module red; restart test red before persistence; final PASS. Exact 24-hour boundary is quiet; one millisecond over is overdue. |
| S9-HM4-4 | test_settings_reload_and_status; test_default_settings_are_editable_outside_protected_runtime; test_effective_defaults_shown_before_first_pass; test_settings_past_byte_and_integer_limits; test_settings_symlink_is_reported_without_following | Initial missing-module red; default-path, initial-status, oversized-integer and symlink tests separately red before fixes; final PASS. |
| S9-HM4-5 | test_owner_thread_and_total_budget; test_copy_past_page_limit; test_full_scan_resumes_after_quantum; test_one_pass_advances_many_copy_pages_with_shared_deadline; test_busy_queue_uses_shorter_slice_without_starving_copy; test_wal_copy_allows_writer_between_page_slices; test_import_does_not_open_files_or_databases | Initial missing-module red; multi-page advancement test red before slice loop; final PASS. Real owner-thread fixture runs without sockets. All new limits have ends/past-limit tests in lifetime_caps.json. |
| S9-HM4-6 | test_registration_and_project_scope; test_project_enumeration_past_page; test_warning_survives_other_visit; test_deleted_projects_drop_retained_observations; test_past_observation_cache_limit_still_reports_old_project_status | Initial missing-module red; deletion and cache-limit cases red before fixes; final PASS. Status reloads only the viewer's bounded derived metadata, so cache eviction does not hide its warnings. |

Final targeted verification:

- Named runner: `codex:tests.test_health_ledger`, `codex:tests.test_health_holds`,
  `codex:tests.test_lifetime_cap_guard`: 68 tests PASS; 4.6 seconds worker elapsed,
  S9 module 2.4 seconds; `metadata_errors: []`, no failed units or budget failures.
- Socket-free classes/case in `tests.test_health_monitor`: PassBoundsTests,
  FailureTests, RecordTests, HealthyTests, and ScopeTests' in-process scoping case:
  16 tests PASS, 0.878 seconds. No assertion edits or test skips were introduced.
- `codex:tests.test_runtime_release`: 2 PASS, 1 expected FAIL, 0.015 seconds test
  time. Only `test_current_runtime_is_recorded_for_the_current_version` fails
  because the runtime changed without a version/catalogue bump. The exact runtime
  file-set check PASSES; the new module is included in runtime_files.py.
- Additional named checks: `test_account_observation_backup`,
  `test_backup_installed_project`, `test_backup_reply_private`,
  `test_large_project_backups`, `test_learning_source_image`,
  `test_learning_source_image_legacy`, `test_learning_source_transfer`,
  `test_owner_backup_streamed`, `test_reply_shape_compatibility`, and
  `test_refusal_steps`: all 133 assertions/tests PASS, 51.1 seconds worker elapsed,
  `metadata_errors: []`, no failed units. Runner exit 1 for the existing
  `test_owner_backup_streamed` budget: 77.920 seconds summed across two concurrent
  fragments against its 60-second ceiling. No budget or assertion was changed.
  Isolated rerun (`--jobs 1 --module codex:tests.test_owner_backup_streamed`):
  all 40 tests PASS, 77.9 seconds elapsed, `metadata_errors: []`; runner exit 1
  solely for the same budget (77.867 seconds > 60). No base comparison was run,
  so this budget failure remains unattributed; the primary must compare this
  named module with the base before deciding its disposition.
- `git diff --check`: PASS.

Primary wiring, matching S8's deferred integration:

```python
from . import health_ledger
health_ledger.register(self.health_monitor, self.state)
```

Keep the returned check and call `close()` during collector shutdown, before the
ledger owner is closed. Its native backup handles close on that owner. No collector
registration or shutdown wiring was changed in the primary-owned learning_store.py.

Implementation details relevant to review:

- Resolve the system SQLite library outside the runtime owner request. Use its
  public backup API, with independent read-only source handles opened/stepped/closed
  only on the ledger owner. Python's backup API does not return after pages=N.
- A shared 500 ms owner deadline covers selection, quick check and copying; busy
  queues reduce copy work to 150 ms. Cleanup has a separate at-most-50 ms request
  allowance; copy scan shutdown joins for at most 100 ms. SQLite steps are eight
  pages, SQL progress checks every 256 VM instructions. OS I/O/scheduling is not
  preempted; the tests demonstrate the elapsed bound on temporary stores, and S14c
  remains responsible for large-ledger/load measurements.
- One resumable full-check worker runs only against a private consistent copy in
  the existing transient scratch area. It sleeps between granted time slices.
  Full checks are scheduled from first observation and again after 12 hours, ahead
  of the 24-hour completion deadline. Only an actual completed scan advances it.
- Size includes database and retained WAL bytes. Growth compares samples over a
  renewing daily window and survives restart. All results are reports only.
- The 32-entry observation cache evicts to fixed-size metadata; status reads at
  most the ledger and requested project's saved observations. Project metadata
  lives in that project's existing state directory, so project removal owns its
  lifetime. No rows, admissions or historical projects are capped.
- Configuration path: the account state root's optional `health-limits.json`.
  No general COCOLO settings loader exists in this base. Defaults/overrides and
  exact edit instructions are in docs/configuration.md. The primary should align
  this path if another settings entry point is pending elsewhere.

Unrun and required primary checks (socket restriction; no socket test was attempted):

- Full `codex:tests.test_health_monitor`, particularly its collector-scope test and
  StatusTests, plus SweepTests not included in the in-process subset above.
- Full `codex:tests.test_ledger_owner_wiring` and
  `codex:tests.test_cli_status_client_boundary` after wiring register/close.
- `codex:tests.test_project_runtime` and
  `codex:tests.test_dispatch_client_runtime` for the protected runtime copy.
- Socket-backed state migration coverage in `codex:tests.test_state_location`.
- Compare the unchanged `codex:tests.test_owner_backup_streamed` path against the
  base for the repeatable 60-second budget failure; do not raise its budget from
  this S9 worktree without the primary's evidence-based disposition.
- The primary's affected run against a737ae9d in a separate/final checkout,
  affected lifecycle flows on macOS/Linux, Linux native SQLite backup validation,
  other-provider review, and batch full-suite gates. No pty tests were needed or run.

Files changed: health_ledger.py (new); health_monitor.py; runtime_files.py;
test_health_ledger.py (new); lifetime_caps.json; test-layers.json;
test-stages.json; test-timings.json; test-requirements.json;
tests/requirements/s9.json (new); docs/configuration.md; docs/requirements/s9.md;
this STATUS.md and assertions.md. No changed/removed assertions; see assertions.md
for the two fixture-only corrections made while adding persistence.
