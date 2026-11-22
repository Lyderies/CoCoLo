# Merge checker contradictions found during requirement-first reconciliation

These tests are stopped at the disputed assertion; no expectation is changed to make the library pass.
The raw failure log is retained in the task's temporary run directory. The 127 contract tests passed unchanged.

## C-1: no recovery report does not imply operation-start tree

Affected tests: Adversarial.test_ml_11_kill_cleanup, test_ml_11_kill_forward,
test_ml_11_kill_undo, test_ml_13_undo_kill_around_combined_commit,
test_ml_6_kill_both_sides_A_B.
Disputed new harness assertion: merge_library_support.py:789, comparing operation_start to the tree when
recover returns no reports. This assertion was introduced during this session's checker migration.
Requirement text: states §2, "A killed process returns no report; the checker reads recovery or status and
always rereads history"; D6, "durable landed, reverted, resolved ... no direction, tree unchanged, cleanup only".
Why: an already durable terminal row may produce no recovery report. Requiring S in that case contradicts
the required durable direction. The requirement-derived replacement must query status and run the single
checker against reread history; an empty recovery-report sequence is insufficient evidence either way.
No production change should force an extra report solely to satisfy this mistaken harness assertion.

## C-2: transient injected unreadability versus later readable oracle view

Affected tests: Adversarial.test_ml_12_fault_sweep_reverse and test_ml_12_fault_sweep_recovery.
Disputed existing assertion: report_oracle compares its later independent readable path to the report's
classification after an injected read/open failure (base versus unknown).
Requirement text: ML-14, "The report class per path matches the tree. It lists merged files, conflicts,
leftover staging objects and 'unknown' for inaccessible contents, and I/O is distinct from owner."
Why: a one-shot injection makes the library's classification read inaccessible, but the later checker read
succeeds after that injection has been consumed. Requiring base rather than unknown contradicts the
inaccessible-content requirement. The replacement needs independent injection provenance identifying the
unreadable path and boundary; it must not accept unknown on an arbitrary readable path based only on the report.
These assertions remain in place pending this contradiction's review; no broad unknown allowance was added.

## Owner response

Owner explicitly authorized the documented requirement-based replacements for C-1 and C-2.
C-1 now consults status when recovery is silent and durable rows exist, then runs check_end_state.
C-2 derives unreadability only from the independently selected validation read/open/fstat/lstat fault path;
it requires unknown for that path and keeps exact classification assertions for every other path.
The assertion replacement ledger records the old and new locations.
