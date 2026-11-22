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

## C-3: nested emptied ancestors immediately after an undo cleanup fault — unresolved

Stopped tests: `Adversarial.test_ml_12_fault_sweep_undo` and
`PersistenceSamples.test_undoing_persistence_samples` (16 EIO/ENOSPC subcases).
No assertion is removed, weakened, skipped or marked expected-failure in the source.
Reproducer: the default batch creates `new/deep/add.txt`; successful forward cleanup is followed by undo.
The combined transaction durably marks undo landed and X undone. Inject before/after open, unlink,
full flush or rmdir of staging during cleanup. `new/deep` is empty but `new` still contains `deep`.
The strict checker rejects `new` at its `assertFalse(any(p.startswith(name + '/') for p in actual))`.
Requirement text: states E1 permits each EL to be "removed or present-empty with the same identity";
E4 requires "X's CR gone". Q5 says "yes, every emptied folder". Protocol 11.1(g) says
"a cleanup job runs after the terminal commit" and "It walks upward only through listed folders".
Why this needs a decision: an early post-terminal cleanup failure can retain both nested recorded folders;
the ancestor is not literally empty until its recorded child is removed. Reversing the terminal outcome or
omitting the ancestor from commit A would contradict other requirements. Unfaulted recovery removes both.
Possible requirement clarification (NOT implemented): permit an identity-checked forest of listed empty
folders pending cleanup, with no unlisted descendants; or apply the strict folder endpoint only after recovery.
Evidence: `measurements/test-framework/merge-model-sweep-third.run.json` and `merge-nested-folder-failure.log`.
This is the outstanding step-3 review blocker. The affected cases remain red; full soak is not certified.

## C-4: generated operation-start fixture after a failed undo — corrected from S's definition

The minimized sequence is successful land, failed undo before staging, then another land whose check refuses.
The generated fixture reset S to the original repository rather than the actual current operation-start tree.
States §1/§5 define S as "operation-start"; the repeated forward call starts from the still-landed child bytes.
The fixture now snapshots the independent disk view before each land, without changing `Fixture.before` or
any equality assertion. Rollback/recovery of a seeded applying row still uses that operation's original S.
The minimized failure and before/after evidence are retained; this correction changes no product outcome.

## Table census

The cited document says 41 cells, but §3 enumerates 40 labels (A1–A7, B1–B7, C1–C3, D1–D8,
E1–E4, F1–F2, G1–G6, H1–H2, I1). The new table test covers every explicit label and 63 permitted
variants, each paired with a rejected corrupt outcome. No fictitious 41st label was invented.
