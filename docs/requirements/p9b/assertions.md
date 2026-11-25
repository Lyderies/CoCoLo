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

## Final targeted allowlist fix on `1e6b5e13`

Requirement P9b-1 now requires exact rejection stdout and only observed, exact
stderr lines. In
`test_deterministic_malformed_signature_diagnostics_are_definitive`, the old
assertion that each of six synthetic `Couldn't parse signature: <fragment>`
lines with **empty stdout** returns False is replaced by assertions that each
of the eight exact OpenSSH diagnostic lines listed in the requirement, with
**exact rejection stdout**, returns False (LF and CRLF variants). The real
parser uses distinct prefixes; substring fragments and absent rejection stdout
no longer establish a verdict. A new rejection-output regression requires
those same eight lines with absent stdout to raise, also rejecting whitespace,
extra stdout, changed case, modified diagnostic lines and unknown stderr.
This replacement follows the accepted final requirement, before runtime edits.
All other existing assertions, including infrastructure precedence, wrong-key
and truncated-signature cache assertions, are unchanged. New tests add real
wrong-namespace caching and every requested transient/unknown stderr case at
the maintenance cache boundary, with recovery without restart.

The first green attempt exposed one more real deterministic line from the
unchanged payload-mismatch regression:
`Signature verification failed: incorrect signature`. It is added to the
requirement and exact-diagnostic test before the final runtime edit; that
existing cache assertion stays unchanged. The new retryability test initially
reused account `a` (the setUp row) on its eleventh case, finding an already-valid
cached row. Its account sequence now excludes `a`, ensuring a fresh unchecked
row for every case. No assertion changed. The corrected tests are rerun red
against the original denylist before the final runtime edit.
