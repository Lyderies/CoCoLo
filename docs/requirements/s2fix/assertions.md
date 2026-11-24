# s2fix: changed existing assertions (for the primary)

One existing assertion changed. Nothing removed.

| Test (file:line) | Old | New | Reason |
|---|---|---|---|
| tests/test_b2_service_outcome.py `AuthorityTests.OPERATIONS` (used by `test_operation_set_unchanged`, line ~423) | 26 history operations, ending `'history.holds_report'` | the same 26 + `'history.orchestration_counts'` | Finding 2 (L14.1): learning-status must count the project's orchestrations, which live in the project store (its service), not in the ledger table it read (always 0 since the project store). The reply-shape freeze (tests/test_reply_shape_compatibility.py, "New reply data requires a NEW operation") forbids adding the counts to `history.capacity_report` or `history.holds_report`, so the counts are a new owner-read operation (same authority as capacity/holds reports: `OWNER_HISTORY_OPERATIONS`, no mutation). Same pattern as decision 132 (a new op added to the exact op-set pins). The test's own purpose (B2-6, no `service_result` entry over the socket) is unchanged and still checked by `test_service_entry_not_reachable_over_socket`. |

Primary decision needed: accept the new operation (and this pin), or choose another carrier for the counts.
