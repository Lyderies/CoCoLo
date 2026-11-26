# S9 assertion ledger

## Finish verification (2026-11-26, WIP 5b30cd61)

Old → new: **none**. No assertion, test fixture, runtime implementation, module
registration, stage or budget was edited in this finish pass. The five regressions
below were independently red on the exact `856c91b7` archived runtime (0.369 s),
then green in both the direct 83-test run (5.404 s) and named metadata run.
The full learning-store module's five socket errors are retained unchanged and
listed for primary rerun in STATUS.md. Verification details are durable in
[finish-evidence.json](finish-evidence.json).

## Targeted follow-up (040-s9-r2c)

No assertion present at `856c91b7` was removed, changed or weakened. Five new
regressions in `test_health_ledger_fix.LedgerFixTests` cover the accepted findings:

- S9-HM4-5: `test_project_deletion_cancels_snapshot_and_later_check_completes`
  requires successful deletion within 750 ms, read-snapshot release, no false
  completion, a newly started copy and later full `ok` completion.
- S9-HM4-5: `test_any_exclusive_maintenance_releases_health_snapshot_on_owner`
  requires the shared exclusive-maintenance path to succeed on the owner within
  750 ms after releasing the snapshot.
- S9-HM4-5: `test_maintenance_releases_snapshot_while_cleanup_is_pending`
  holds asynchronous cleanup back and still requires maintenance to cancel and
  release the detached reader within 750 ms.
- S9-HM4-2: `test_sustained_growth_alarms_immediately_after_each_daily_renewal`
  requires the warning at renewal and throughout the six-hour replacement window
  over three daily renewals. Existing short-burst/truncate assertions are retained.
- S9-HM4-1: `test_copy_full_and_io_errors_report_space_or_io_without_damage`
  requires FULL, IOERR and extended IOERR_WRITE to report unreadability with
  “space or IO”, no damaged-data/private-path text and no completion timestamp.

All five are red against the exact `856c91b7` runtime and green after the fixes;
see STATUS.md for timings. No test module, stage or budget was changed.

The lifetime-cap inventory (not a test assertion) changes
`health_ledger.py::measure` from **2 → 3** checks to account for candidate maturity.
Its description now explains retention and disposal of the old/candidate samples;
its witness changes from `test_short_bursts_after_renewal_and_truncate_are_quiet`
to `test_sustained_growth_alarms_immediately_after_each_daily_renewal`.
Both tests still run unchanged/new respectively, and the lifetime-cap guard's
assertions remain intact.

## Historical assertion changes

The initial S9 round removed or changed no existing assertions. The fixed-scope
review round below replaces assertions where the accepted requirement changed.

Two new-test fixture arrangements were corrected when restart persistence was
added: growth boundary cases now use separate project identities so their saved
histories are independent; the restart growth test measures the first post-restart
sample before an ordinary visit can renew its sample window. Their assertions
remain unchanged. There is no old-to-new assertion replacement to record.

## Fix round assertion replacements

- `test_quick_check_every_visit`: two live quick SQL executions -> one resumable
  quick VM and full VM on the finished copy, with no live quick execution. This
  directly replaces the restart loop rejected by MED-1.
- `test_wal_copy_allows_writer_between_page_slices`: copied project count 2 -> 1;
  also require live count 2. HIGH-2 requires the first snapshot, excluding later writes.
- `test_owner_thread_and_total_budget`: every open_reader call on ledger owner ->
  live enumeration on ledger owner, dedicated snapshot reader checked by exact
  worker-thread identity; mutable owner connection still has no transaction after handoff.
  The user explicitly allows a dedicated read-only snapshot; no mutable connection
  or cursor is handed across threads. The <=750 ms assertion remains.
- `test_settings_reload_and_status`: accepted 17/11 byte values -> accepted
  17/11 MiB values, with corresponding size/rate boundary assertions. LOW-5 now
  requires rejecting 1-byte limits; the same reload, summary and warning checks remain.
- `test_default_settings_are_editable_outside_protected_runtime`: health-limits.json,
  1234 bytes -> shared user-settings.json, 1234 MiB. Same path/override assertions.
- `test_warning_reaches_owner_attention`: 1-byte threshold fixture -> 1 MiB
  threshold with a real >1 MiB payload. Owner attention assertion unchanged.
