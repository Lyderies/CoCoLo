# S13a assertion changes

- `S13Budgets.test_b6_finding_the_lease_start_pass_has_no_bounded_selection_and_no_back_off`:
  old expected violations `['selection unbounded: 179 rows selected in one batch',
  'no back-off: blocked rows are examined again by every batch']` -> new expected `[]`.
  This is the planned S13a flip of RS-B-9-B6-MECH; the measurement is unchanged.
- No other existing assertion changed or removed.
