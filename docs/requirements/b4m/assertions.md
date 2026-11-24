# Existing assertions

None changed. Existing error codes and the active-child retry advice remain intact.
The new tests' binding fixture was corrected to include a string CLI path in its manifest;
no test assertion was weakened or changed to fit the implementation.

## Batch integration (merge/batch-0857)

- No existing assertion changed. `history.holds_report` (added by B7a after baseline 8d4a7ce3) is added to
  `REPLY_FIELDS` with exactly the B7a producer keys (`project_holds`, `result_custody`, `result_retirement_repairs`).
  `test_every_existing_reply_matches_previous_release_catalogue` already supports post-baseline operations
  (previous catalogue is a subset; the table must equal the API operation sets), so no new mechanism was needed.
- `test_reply_shape_live` DRIVERS: added `test_holds_report` to the account/history group so the new op is produced live.
- RS-F S1 `runtime_upgrade_required` refusals are error replies (no `result`), so they do not pass the result guard;
  the client validates them separately (fixed extra fields `message`, `update_command`).
- Integration fix (test_holds_report.py, B7a x RS-D S2): `UPDATE project_holds SET release_kind='retention'` (and the
  later `='orchestration_closed'`) old: all rows -> new: `... WHERE kind='result_custody'`. Reason: RS-D S2 makes the
  orchestration hold a real released row whose release_kind CHECK rejects 'retention'; the assertion only concerns custody rows.
- tests/lifetime_caps.json: added `account_service.py::runtime_upgrade_reply` (RS-F S1, per-request bounded, nothing stored).
