# S9 assertion ledger

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
