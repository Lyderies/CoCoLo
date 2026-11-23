# Step 3 implementation and review status

The r1 review fixes and owner decision 91=A are implemented. The two original C-3 test methods remain unchanged; the shared checker now admits only recorded empty folder trees with matching identities. The current fix/evidence hand-off is appended below. Steps 4–5 have not begun; this is the requested review stop.

## Implemented

- The single `check_end_state` reads durable history independently, checks full bytes/modes, pin dependencies,
  owner provenance, report classes, CU/EL handling, and operation starts/targets. Both real and modeled calls use it.
- All 40 explicit table labels have positive and negative witnesses (63 permitted variants). The source calls
  these 41 cells; the census discrepancy is documented rather than inventing an extra label.
- `MemoryDisk` separates inode contents/modes, directory namespace edges and SQLite committed rows. File flush,
  parent flush, cross-directory rename, removal and power loss have independent witnesses. Store I/O uses this
  backend through IOCall/IOOverride; no model operation writes the real owner tree.
- The six-action Hypothesis state machine inherits the pinned dev/ci/soak profiles and deadline=None. It selects
  persistence boundaries and retains minimized failing sequences. The repeated-land operation-start fixture
  correction is documented as C-4; expected trees never come from the library's success report.
- Store seam completion routes stat/listdir/read/open/close/rename-parent descriptors through hooks, with report
  reads identified separately from pre-commit validation. SQLite FULL commit is modeled atomically; read-only git
  and trusted seal callbacks remain real. This model does not claim physical power-loss or arbitrary git mutation coverage.
- Production fixes cover applying cancel, terminal retry refusal, unknown unreadable history, all emptied ancestors,
  cleanup/retirement durable rereads, and reporting unrecorded undo-created folders.
- Normal samples are checked in with normalized paths, stable occurrences, phase, side and commit label.
  Actual trace flush assertions remain platform-specific; only replay identity normalizes fsync/fullfsync.
- `scripts/generate_merge_samples.py` reproduces the inventory. `test_merge_library_soak` exposes complete
  campaigns under an explicit soak profile; ci/dev selection of that module fails before worker execution.

## Prior implementation evidence (before r1; historical)

`measurements/test-framework/merge-step3-evidence.json` records each red and subsequent run's worker job-seconds.
The final eight-module default-job run passed 204 tests in **77.310938334 job-seconds**, with no worker or budget
failure. Its global exit is still 1 for the **80 missing step-4 lifecycle checks**, not waived or marked passing.
The separate explicitly partial run passed the unaffected 77 adversarial and 4 model tests in
**63.539055000 job-seconds**, including three existing opt-in image skips. It explicitly lists the two stopped
C-3 tests as not run; it is not a complete module gate. The portability/mutation follow-up passed 16 tests.

All four production mutation proofs pass: private-location removal is caught by ML-30 forgery; omitted pre-A
flush loses staged content after modeled power loss; an extra history commit is caught by ML-6 order; and a
swallowed EIO ending landed is rejected by the shared checker. The proof tests run the normal control first.

Before decision 91, the strict nested-folder assertion was preserved. That historical model-sample run had exactly 16 failing
EIO/ENOSPC subcases in the undo test; four other model operation tests pass. Full soak is not certified.

Python 3.11 grammar and whitespace/identifier checks pass locally. Runtime evidence is macOS/Python 3.14;
Linux/Python 3.11 and Ruff execution remain CI checks (Ruff was unavailable locally and was not installed).
No full suite was rerun during this affected-module work. No product install, push, or control-project edit occurred.

## Reproduce

Use the owner-authorized test environment with the hashed test-only dependencies:

```sh
python -B scripts/parallel_tests.py --base a1d25560 --module codex:tests.test_merge_library_mutations --module codex:tests.test_merge_library_disk --module codex:tests.test_merge_library_harness --save-timings /tmp/merge-proof.json
python -B scripts/parallel_tests.py --base a1d25560 --module codex:tests.test_merge_library_model_sweep --save-timings /tmp/merge-model.json
python -B scripts/parallel_tests.py --base a1d25560 --profile soak --module codex:tests.test_merge_library_soak --save-timings /tmp/merge-soak.json
```

The second command now verifies decision 91, including both nested-folder cases. The third
is the complete campaign entry point; it is not yet certified. `ASSERTIONS-merge-checker.md`
lists each replaced/moved accepted assertion and its replacement line, plus normal-to-soak execution mappings.

## R1 fixes — current review hand-off

Tests-first commits: `76e490e2`, `211cdf71`, `6b5eade7`, `4a9825e9`, `15b85bde`, `c45a7fc0`, `e2f01c96`. Implementation: `7a09e822`; integration with the repaired framework: `57cf65d5`, `d23ec488`. Portable baseline repair `c54e83e7` validates unchanged legacy tests in both worktrees. The full discovery census witness `3fcd71b3` caught three accidentally filtered original image checks; `6e64cd7b` restores them and `9072d56c` restores their catalogue mappings. This preserves the earlier required rebase on framework `4e5ca6a1` and brings in the subsequent framework fixes without rewriting the recorded red-test commits.

| Fix | Changed enforcing line | Result |
|---|---|---|
| A1 | `packages/codex-agents/tests/merge_library_support.py:600` | Independent durable snapshot before every injected call; mandatory terminal snapshot and immutable direction in cleanup/retirement. |
| A2 | `packages/codex-agents/tests/merge_library_support.py:662` | Unknown requires actual history failure; readable durable rows and pins still checked. |
| A3 | `packages/codex-agents/tests/merge_library_support.py:636` | Retry is restricted by Q2; real sweep and generated operation pass action/fault provenance. |
| A4 | `packages/codex-agents/tests/merge_library_support.py:588` | All Q1–Q6 checks read POLICY; defaults retained and every alternate has an isolated checker-flip witness. |
| A5 | `packages/codex-agents/tests/merge_library_support.py:649` | Select operation rows independently before consulting report.batch_no; refused cannot hide applying authority. |
| A6 | `packages/codex-agents/tests/merge_library_support.py:738` | All unresolved and terminal rows pin their contents until independently established release; undo dependencies included. |
| A7 | `packages/codex-agents/tests/merge_library_support.py:957` | Both lanes reject stale keys and require operation/phase/side/commit coverage; unknown profile is rejected by runner. |
| A8 | `packages/codex-agents/tests/merge_library_support.py:817` | Protected non-event paths have no report-classification exemption; event exceptions require independent owner/moved/unreadable provenance. |
| A9 | `packages/codex-agents/tests/merge_library_support.py:1079` | Explicit per-test/class mapping or named ML mapping; unmapped methods fail. Complete definition is in the cited file, not an excerpt. |
| A10 | `packages/codex-agents/tests/merge_library_support.py:618` | Immutable starting evidence asserted in finally on every return. |
| B1 | `packages/codex-agents/tests/merge_library_disk.py:229` | Flush fd inode after rename; no stale-path flush. |
| B2 | `packages/codex-agents/tests/merge_library_disk.py:177` | Missing read raises FileNotFoundError. |
| B3 | `packages/codex-agents/tests/test_merge_library_machine.py:67` | Check own operation reports with fired call/errno; historical report still checks row agreement, latest tree endpoint calculated independently. |
| B4 | `packages/codex-agents/tests/test_merge_library_disk.py:111` | Specific missing-stage diagnostic, event-derived path/mode and proof that pre-A flush mutation activated in history phase. |
| B5 | `packages/codex-agents/tests/merge_library_disk.py:33` | Committed SQLite rows keyed by inode; unlink/reopen cannot resurrect; rename preserves identity. |
| B6 | `packages/codex-agents/tests/test_merge_library_disk.py:210` | Production write-finally close confirmed; power-loss latch blocks unwinding I/O until explicit resume before recovery. |
| B7/B8 | `packages/codex-agents/tests/merge_library_disk.py:266` | Honor O_TRUNC/O_DIRECTORY/O_NOFOLLOW; unlink/rmdir errors match path types. |
| B9 | `packages/codex-agents/tests/test_merge_library_disk.py:42` | Absence assertion strengthened; staged path/mode derived from batch. |
| B10 | `packages/codex-agents/tests/merge_library_disk.py:8` | SQL/nontrivial merge limits explicit; git cache includes repository identity; real seed root identity used. |
| B11 | `packages/codex-agents/tests/test_merge_library_disk.py:191` | Left-parent-only flush, exchange and unflushed-unlink controls. |
| C1 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:431` | Nested descriptor lifetimes and independent finally closes; close_fd handles hook failure before close. |
| C2 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:1060` | Allocation inspected and corrected for distinct later batch; cancel matches ticket batch rather than latest same-child row. |
| C3 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:732` | Only genuinely emptied ancestors recorded; populated owner/empty foreign child remains separate. Decision 91 handles recorded descendants. |
| C4 | `packages/codex-agents/tests/test_merge_library_review.py:50` | Named trace test verifies every land/undo pre-A staging flush; existing save flush satisfies it. |
| C5 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:764` | Reporting/phase context restored in finally. |
| C6 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:1494` | Unknown/io durable rereads guarded; later rows continue. land, rollback and undo read failures also guarded. |
| C7 | `packages/codex-agents/tests/test_merge_library_review.py:84` | Named unrecorded undo-folder proof; harness covers applying cancel, terminal retry, unreadable history, emptied ancestors and report context. |
| C8 | `packages/codex-agents/tests/merge_library_support.py:1079` | Full source shown; full soak remains explicitly unverified. Four normal campaign wrappers preserve all 78 original methods exactly once. |
| Decision 91 | `packages/codex-agents/tests/merge_library_support.py:577` | Only recorded, identity-matching directories with no foreign contents qualify; full modes and immutable original endpoint retained. |

### Assertion-ledger corrections

`ASSERTIONS-merge-checker.md` now maps all 38 original changed expressions to actual enforcing lines and specific table cells. No test was deleted or newly skipped. The three pre-existing mounted-image opt-ins remain explicit skips in the real campaign.

| Review | Corrected mapping |
|---|---|
| L0 | All 38 original rows now name specific A–I table cells and current enforcing lines. |
| L1/L2/L5 | Report/row agreement and terminal recorded authority, plus mandatory independent pre-fault terminal snapshot (A1); no inverted unknown/applying replacement. |
| L3 | Reverted maps to reverted report/row equality, not undone. |
| L4 | Exact sealed batch and adjacent operation-kind assertions apply to both forward and undo. |
| L6 | CU folder naming maps to the named/leftovers assertion, not kill-marker bytes. |
| L7 | Per-path S/T enforcement retained; A8 removes unsupported non-event class exemptions. |
| L8 | Refused tree equality maps to admission/no-row branches that execute before return. |
| L9 | Explicit exactly-one recorded row plus duplicate guard restores existence and uniqueness. |
| L10 | EL membership comes from independent S/T, not owner report naming. |
| L11 | Literal empty assertion is replaced only under owner decision 91=A by recorded inode-tree checks; no foreign content admitted. |
| L12 | Full expected directory tuple is checked before copying, with independent descendant modes. |
| L13 | Applying is allowed only for C3 unrecorded terminal-write failure; ordinary EIO cannot land. A reporting flag alone is rejected: the actual call must be read-only (including access/create/truncate/append flags) and the independent pre-fault state landed. `97e5512e` first demonstrated write/truncating-open bypasses; `a721afbf` closes them. |
| L14 | All test methods retained; complete/disjoint normal campaign partition proven by the harness. The 40 explicit cells versus prose 41 census remains a documentation note. |

The ledger also records B4/B9 assertion strengthening and B6's stopped speculative sqlite_close fixture, replaced by observed production write-finally close (see `CONTRADICTIONS-merge-checker.md`). The generated historical-report adapter uses independent operation endpoints, never current observed bytes as expected output.

### Remaining gates

Full soak is **not certified**. Linux/Python 3.11 and macOS/Python 3.11 runtime evidence and Ruff remain **outstanding**; the three existing mounted-image opt-in checks were discovered but skipped, not certified; local runtime is macOS/Python 3.14.7, with Python 3.11 grammar parsing passed. The overall runner exit remains 1 for the 80 missing step-4 lifecycle checks; no pending requirement is counted passed. Expanded real campaigns pass individually within 60s, but their concurrent five-module measurement exceeds budgets due to contention; no budget increase or coverage reduction was made. This performance gate remains open for the review/optimization stage. No full suite, installation, push or control-project edit occurred.

### Final affected-module evidence

The final ten-module run under the repaired runner passed **242 tests in 76.9704737511347 job-seconds**, default **16 jobs**, no failed workers and no budget failures. Metadata now reports exactly the 80 missing step-4 lifecycle checks, with no legacy fingerprint errors. The earlier integrated run exposed 522 root-dependent hashes; its failing preflight remains in the evidence, superseded by the source-verified portable baseline rather than suppressing errors.

All four mutation proofs passed again: `test_private_location_guard_mutation_is_caught_by_ML30_forgery`, `test_missing_A_flush_is_caught_by_post_crash_staging_contents`, `test_third_history_commit_is_caught_by_ML6_order`, and `test_swallowed_EIO_landed_is_caught_by_the_single_end_state_check` in `packages/codex-agents/tests/test_merge_library_mutations.py`. The missing-flush proof also requires the history-phase mutation to activate and the exact staged-content failure. Real C-3 remains `Adversarial.test_ml_12_fault_sweep_undo`; model C-3 remains `PersistenceSamples.test_undoing_persistence_samples`. Neither method's assertions were changed to fit production code.

Raw run records are retained in `/private/tmp/cocolo-review-r1`; `measurements/test-framework/review-r1-merge-evidence.json` contains timing/result extracts bound to their original SHA-256 digests. Extracts are explicitly not recombinable CI evidence.

| Final real campaign (separate default-16-job invocations) | Checks | Job-seconds |
|---|---:|---:|
| adversarial | 49 | 45.18175304098986 |
| owners | 20 | 21.68256016698433 |
| kills | 5 | 31.07644525001524 |
| forward_faults | 1 | 30.46376620803494 |
| recovery_faults | 3 | 18.176061125006527 |

The five runs total **146.5805857910309 job-seconds** across 78 checks (75 pass, three original image opt-ins skipped). Every module is below its 60s budget. These are separate affected-module runs, not a combined CI gate. The final read-only-provenance follow-up passes 75 checks in **34.64630295801908 job-seconds**, including both the model C-3 case and all four mutation proofs. All final records report exactly 80 missing step-4 checks and no other metadata errors.

The earlier concurrent five-module campaign used **475.9808796250145 job-seconds** and exceeded all five module budgets despite passing test bodies. That result remains in the evidence; isolated timings do not clear the concurrent performance gate.

## Round-2 MED pass — ready for whole-worktree review

All six MEDs from REVIEW-testfw-step3-r2.md are addressed under decision 99 A. `REVIEW-merge-library-r2-fixes.md:7` is the complete M1–M6 -> requirement/fix/witness file:line map, with exact intended-red and sensitivity diagnostics. Tests-first commits: `a2ab496b`, `3fb8af31`, `83ea6830`; implementation `81cc6fac`; specific mappings `16be3593`; integrated framework `67f89368` plus final hand-off merge.

Integrated affected run: 253 checks pass, **90.15853699890431 job-seconds**, DEFAULT 16 jobs; all four original mutation proofs pass. Separate real crash campaign: 49 checks (46 pass, three original mounted-image opt-ins skipped), **45.08007408300182 job-seconds**, DEFAULT 16 jobs. No worker/budget failures. Saved timing evidence and initial reds: `measurements/test-framework/review-r2-merge/runs.json`; sensitivity failures: `merge-sensitivity.json` in that directory. The two original decision-91 cases pass unchanged.

Every changed assertion is in the round-2 appendix of ASSERTIONS-merge-checker.md; no test deleted. All 13 LOW gaps and conditional full-worktree review items are explicitly listed in REVIEW-merge-library-r2-fixes.md, deliberately unfixed. Earlier A1 HIGH remains fixed. Open gates remain full soak, mounted-image execution, macOS/Linux Python 3.11 runtime, Ruff, concurrent real-campaign performance and 80 missing lifecycle requirements; the overall gate still exits 1. Steps 4–5 remain stopped for the requested review.
