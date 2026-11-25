# Assertion changes

No assertions in tests present at the unit's base are removed or changed. S14 is absent from this unit's
base; its accepted measurements are used as a sourced projection input and its
backup boundary checks remain a primary integration gate when S14 is merged.

New S14b test revision, before outcome retention code: the real-footprint test's
`assertIsNotNone(unreferenced outcome)` is replaced by `assertIsNone(unreferenced
outcome)`, an exact outcome-count/status summary assertion and a digest lookup
returning that summary. Reason: the reader audit proves a 90-day learning window;
keeping all full outcomes failed measured sizing once released receipts and
partial summaries were included. Linked outcomes still have the original
preservation assertion, with an explicit evidence link in their fixture. This
strengthens the lifecycle requirement rather than weakening a storage threshold.
