# Step 3 implementation and review status

Step 3 is **not complete**. The two C-3 tests remain red, unchanged, and stopped for review under the owner's
requirement-conflict rule. `CONTRADICTIONS-merge-checker.md` states the exact requirements, reproducer and
possible clarifications. Steps 4–5 have not begun; the requested next review checkpoint was step 3.

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

## Evidence

`measurements/test-framework/merge-step3-evidence.json` records each red and subsequent run's worker job-seconds.
The final eight-module default-job run passed 204 tests in **77.310938334 job-seconds**, with no worker or budget
failure. Its global exit is still 1 for the **80 missing step-4 lifecycle checks**, not waived or marked passing.
The separate explicitly partial run passed the unaffected 77 adversarial and 4 model tests in
**63.539055000 job-seconds**, including three existing opt-in image skips. It explicitly lists the two stopped
C-3 tests as not run; it is not a complete module gate. The portability/mutation follow-up passed 16 tests.

All four production mutation proofs pass: private-location removal is caught by ML-30 forgery; omitted pre-A
flush loses staged content after modeled power loss; an extra history commit is caught by ML-6 order; and a
swallowed EIO ending landed is rejected by the shared checker. The proof tests run the normal control first.

The strict nested-folder assertion is preserved. The last complete model-sample run has exactly 16 failing
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

The second command reproduces C-3; do not treat its red result as permission to weaken the assertion. The third
is the complete campaign entry point to run after the C-3 requirement decision. `ASSERTIONS-merge-checker.md`
lists each replaced/moved accepted assertion and its replacement line, plus normal-to-soak execution mappings.
