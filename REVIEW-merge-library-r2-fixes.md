# Round-2 scoped merge-library review fixes

Source: REVIEW-testfw-step3-r2.md; decision 99 A. All six MEDs addressed, no LOW fixes. The prior A1 HIGH remains fixed. Paths refer to this full worktree.

| Finding | Requirement | Fix | Witness | Intended failure / control |
|---|---|---|---|---|
| M1 | TF-18 | `packages/codex-agents/tests/merge_library_support.py:971` | `packages/codex-agents/tests/test_merge_library_r2.py:22` | Generator `scripts/generate_merge_samples.py:13` retains actual fsync/fullfsync signatures. Both discovered/one sampled fails new persistence boundary.*fullfsync in real and model lanes; both selected pass. Old generator fails M1 generator merged flush signatures. Replay keys retain portable full_flush normalization; coverage signatures do not. |
| M2 | TF-19 | `packages/codex-agents/tests/test_merge_library_states.py:104` | `packages/codex-agents/tests/test_merge_library_states.py:99` | Both control calls pass terminal_before=reverted. Deleting the owner folder fails cleanup changed paths outside listed empty folders. A permissive deletion mutant produces the intended AssertionError not raised. |
| M3 | TF-19/ML-23 | `packages/codex-agents/tests/merge_library_support.py:780` | `packages/codex-agents/tests/test_merge_library_r2.py:37` | Explicit S_IFDIR check precedes the recorded-empty-tree allowance. A fraudulent regular file fails EL must be a directory, without relying on traversal failure. Before fix: AssertionError not raised. |
| M4 | TF-18/19 | `packages/codex-agents/tests/test_merge_library_review.py:101` | `packages/codex-agents/tests/test_merge_library_review.py:18` | Full-worktree deterministic machine witness already existed at entry. New call/fault omission controls prove each is necessary: exactly one AssertionError not raised, no setup error, own report landed, fired exchange, injected EIO. |
| M5 | ML-12 | `packages/codex-agents/native-package/.codex/orchestration/codex_agents/merge_library.py:447` | `packages/codex-agents/tests/test_merge_library_r2.py:52` | Fallback raw close only if hook prevented syscall attempt. Real release/error/reuse simulation verifies one close and live reused descriptor; old code fails M5 retried close after the syscall already released the descriptor. |
| M6 | TF-19/ML-12 | `packages/codex-agents/tests/test_merge_library_r2.py:78` | `packages/codex-agents/tests/test_merge_library_r2.py:155` | Named fired-boundary witnesses cover rollback/undo/cleanup initial reads, land IO-handler and cancelled-validation rereads, cleanup durable reread/continuation, applying-cancel putback and terminal retry refusal. Removed handlers produce tracebacks containing each uniquely named injected EIO (undo also chains an unbound-local error after losing its initial-read guard); cancelled-putback bypass and permissive terminal retry fail their exact assertions. |

M6 individual fired-boundary locations: `packages/codex-agents/tests/test_merge_library_r2.py:78`, `packages/codex-agents/tests/test_merge_library_r2.py:89`, `packages/codex-agents/tests/test_merge_library_r2.py:98`, `packages/codex-agents/tests/test_merge_library_r2.py:107`, `packages/codex-agents/tests/test_merge_library_r2.py:121`, `packages/codex-agents/tests/test_merge_library_r2.py:136`, `packages/codex-agents/tests/test_merge_library_r2.py:155`.

Tests-first commits: `a2ab496b`, `3fb8af31`, `83ea6830`; implementation `81cc6fac`; specific requirement mappings `16be3593`. Framework integration `67f89368`. Sample inventory has unchanged selected keys/counts; only the actual flush-operation metadata changes. No sampling reduction, budget increase, new expected-failure marker or test deletion. ASSERTIONS-merge-checker.md records replacements.

`measurements/test-framework/review-r2-merge/runs.json` preserves digest-bound saved-timing extracts. Initial red run: 6.297720791073516 job-seconds, exact intended M1/M3/M5 failures. Integrated eleven affected modules: 253 checks, 90.15853699890431 job-seconds, default 16 jobs, no worker/budget failures. All four original mutation proofs passed in this run. The separate real crash campaign is recorded in the main hand-off. `merge-sensitivity.json` contains all mutation diagnostics, including both generated-EIO provenance omissions; `.py.txt` preserves the procedure with worktree placeholders.

Metadata still has exactly 80 missing step-4 lifecycle checks and no fingerprint errors; overall exit is 1. Executed macOS/Python 3.14.7. Open gates: full soak, mounted-image opt-ins, macOS/Linux Python 3.11 execution, Ruff, concurrent real-campaign performance and lifecycle coverage. Python 3.11 grammar is not runtime evidence. Conditional review items remain for the full-worktree verifier: kill whitelist/FAULTABLE, seed safety, cancel report, power-loss label and C4 flush paths.

## Known LOW gaps (all 13)

- L1: nonterminal snapshots are not compared.
- L2: A2 durable comparison remains optional.
- L3: policy strings remain unchecked.
- L4: Q5 also covers non-delete parents.
- L5: SQLite snapshot connection is not deterministically closed.
- L6: old A7 witness can return without raising under soak.
- L7: generic fallback rationale remains.
- L8: no dedicated cache/SQL/seed witness.
- L9: missing rollback batch remains unknown/io rather than refused/unknown_batch.
- L10: declare_ml_tests definition was absent from review excerpts; full-worktree verification remains required.
- L11: historical ledger enforcement and extra locations remain unverified.
- L12: B4 second-site closure remains unverified.
- L13: historical r1 fix-map line references remain inaccurate; the new r2 map does not rewrite that history.
