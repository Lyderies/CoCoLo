# S3: RS-D-9 budgets and query plans

Base: `version/0.4.0` at `8d4a7ce3`; worktree branch `feat/rsd-s3-budgets`.
Sources: ANALYSIS-0.4.0-rsd-rse-closure.md, slice S3 and RS-D-9 row;
DESIGN-0.4.0-RS-D.md, RS-D-9 and draft 4 P4'; owner decision 90.2 A.

**RS-D-9-S2 — History-independent selection work.**
On equivalent live data, the retirement selector, stored worker context lookup by
outcome, and context sweep visit equal rows with zero and 100,000 retired
tombstones. Count SQLite work, not elapsed time.

**RS-D-9-S3 — Partial query indexes.**
EXPLAIN QUERY PLAN for those production statements uses the relevant partial
indexes and never scans the tombstone table. Check both the first page and the
cursor page of the two fair selectors.

**P4-REPORT — Measure the pre-handoff window.**
A script under scripts/ measures packet return to the directory fsync under load
and prints p99. It is a report with no numeric acceptance gate.

## Execution rules

Tests belong in tests/test_rsd_budget.py. Capture statements executed by the real
store methods rather than copying their SQL into the tests. Exercise their
selection work independently of file I/O and unrelated maintenance passes. Use
nonempty, identical live rows and one transaction of executemany inserts for the
historical fixture. The module must remain under 60 seconds.

If a selector scans history and no existing schema upgrade step carries the
needed index, stop and report the product finding: adding a schema migration is
outside this slice. Do not change a historical schema definition in place.
