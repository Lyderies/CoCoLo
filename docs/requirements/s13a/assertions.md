# S13a assertion changes

- `S13Budgets.test_b6_finding_the_lease_start_pass_has_no_bounded_selection_and_no_back_off`:
  old expected violations `['selection unbounded: 179 rows selected in one batch',
  'no back-off: blocked rows are examined again by every batch']` -> new expected `[]`.
  This is the planned S13a flip of RS-B-9-B6-MECH; the measurement is unchanged.
- No other existing assertion changed or removed.

## Harness rule alignment (decision 354; DESIGN-0.4.0-RS-B.md section 12 l.817-819, section 5 l.225)

No assertion value changed (B5 still `released == 1000`, `batches <= 18`, 64 per batch checked).
- `scripts/measure_b7b.py` B3/B5 drain loop: old "stop when a pass releases 0" -> new "stop only when a pass releases 0 AND
  no due held unarmed lease row remains" (a pass that backs off blocked rows is followed by the next pass, as the driver does).
- `scripts/measure_s13.py` `measure_due_holds`: same rule change (no effect with no blocked rows). Added `batch_examined`
  (sum of rows selected per call) beside the per-statement `batch_selected`.
- New/rewritten S13a test `test_64_blocked_rows_ahead_of_eligible_rows_do_not_starve_them` (S13A-FAIR): drives the pass
  repeatedly; asserts `sum(selections) <= 64` per call (red on 8bb9f1b1: 128).
