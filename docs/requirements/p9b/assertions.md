# Assertion changes

No existing assertions are removed or changed. P9b adds a dedicated regression
module; all accepted P9 assertions remain in place.

Before implementation, two new-test fixture mistakes were corrected: the sixth
fingerprint now uses hexadecimal characters; the fourth deletion fixture uses
public `expire_report` rather than `sweep_learning_retention` (which only deletes
cases/candidates). Its `removed == 1` and predecessor-restoration assertions are
unchanged. Neither fixture correction relaxes an assertion.

## Review fix round on `9333772e`

All seven pre-existing tests and their assertions remain unchanged. Five tests
are added. Before production edits, the new EXPLAIN assertion was corrected:
`'SEARCH capture_candidates USING' in detail` →
`detail.startswith('SEARCH ') and 'capture_candidates USING' in detail`.
SQLite reports the TEMP table as `temp.capture_candidates`; the replacement
accepts schema qualification while still requiring indexed SEARCH access.
The full-records-scan prohibition and final-sort input bound are unchanged.
The corrected test is rerun red before production edits.

The first counting fixture placed its function in SELECT, where SQLite only
evaluates rows admitted into its top-N sorter. It was moved to an always-true
WHERE predicate on the union rowid so it counts every row before the final
sort. No assertion changed; this fixes the measurement, which initially
missed the unbounded input. The corrected measurement is rerun red before
production edits.
