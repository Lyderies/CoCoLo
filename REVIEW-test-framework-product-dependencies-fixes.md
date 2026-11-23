# Product changes and legacy test proof ownership

TF-26 requirements first: `cc3ef2c7`; initial failing witnesses `912ad5c8`; ownership fix `249d4f7d`. The later positional/keyword-default witness `765cd2e5` precedes fix `813661df`; release/policy precedence witness `ed171c73` precedes fix `cc236c6b`. No product code fix is included in these branches.

The primary's failure is reproduced exactly: the four lines from product revision `2357016b` cause **1,028 legacy errors across 79 modules** on the `5ab0a9d3` framework, plus the 80 already-deferred lifecycle errors. The primary's `stable()` suspicion is incomplete: the old `walk()` already excludes most product functions, but imported product constants are hashed by value and imported defaults retain captured values. `COLLECTOR_RUNTIME_DIGEST` changes when the product source changes. Source ownership also formerly trusted test-looking module names; the new alias witness covers that separate boundary.

The first ownership fix leaves seven errors, all in `CliObservationRuntimeTests`, whose `hint(..., digest=COLLECTOR_RUNTIME_DIGEST)` helper captures the product digest as a keyword default. The default witness reproduces this independently (including positional defaults); the final policy records imported defaults by identity and retains captured values for test-owned defaults.

## TF-23 fix table

Every repair witness below has an old-code red record at exact `5ab0a9d3` (only the RSA product edit and the added witnesses/catalogue are present), its intended diagnostic, and a green record. `scripts/test-review-witnesses-product-dependencies.json` binds the evidence. The two additional tests for shared-helper/constant edits and assertion edits/renames are **positive protection controls**: they pass on both old and new code, and are not misrepresented as old-code failures.

| TF-26 repair | Enforcing file:line | Witness file:line | Positive control |
|---|---|---|---|
| product_only_constant_and_implementation_changes_keep_legacy_proof | `scripts/test_framework.py:452` | `tests/test_product_dependencies.py:43` | Unchanged product/test state is equal; a product implementation-only change is accepted; shared-helper edits remain rejected. |
| product_symbol_identity_changes_are_not_ignored | `scripts/test_framework.py:537` | `tests/test_product_dependencies.py:77` | Same identity is equal; another product module with equal values changes the proof. |
| source_ownership_wins_over_test_looking_product_alias | `scripts/test_framework.py:415` | `tests/test_product_dependencies.py:84` | Unchanged source is equal; a product function with a test-looking module alias may change its implementation. |
| imported_product_defaults_use_identity_but_owned_defaults_keep_values | `scripts/test_framework.py:472` | `tests/test_product_dependencies.py:94` | Unchanged defaults are equal; product positional/keyword defaults may vary, but a test-owned default change is rejected. |
| migration_rejects_wrong_provenance_and_changed_test_identity | `scripts/test_owned_baseline.py:23` | `tests/test_product_dependencies.py:118` | Correct provenance and unchanged IDs/fingerprints are accepted before and after wrong source, changed ID/fingerprint and unresolved dependency controls. |
| later_bookkeeping_proof_takes_precedence_over_older_policy_record | `scripts/test_owned_baseline.py:63` | `tests/test_product_dependencies.py:105` | Old release records receive the new policy; a later release record wins even with the same test implementation fingerprint. |

Exact final old-code replay: **0.105896 job-seconds**, default 16 jobs; six intended failed witnesses and two passing protection controls. Final affected run with the exact RSA product edit: **34.292031 job-seconds**, default 16 jobs, **120 tests pass**, **zero legacy errors**, exactly 80 lifecycle metadata errors, no budget failure. Detailed records, worker logs, raw SHA-256 digests and temporary source-replay provenance are in `measurements/test-framework/product-dependencies/`. The default and precedence intermediate reds and the seven-error observation are preserved as additional evidence, not substituted for the exact-base replay.

## Immutable migration

`scripts/test-legacy-test-owned-v1.json` (introduction `d4f9ef00`) and `test-legacy-test-owned-v2.json` (introduction `7a58d4d3`) are new versions. V1 remains unchanged. Both translate the same **5,739** admitted legacy IDs from trusted `5ab0a9d3`, keeping every test implementation fingerprint unchanged; v2 changes 334 dependency records relative to v1 as defaults acquire identity semantics. All earlier baselines, active legacy lists, debt lists and ceilings remain byte-identical to the reviewed base.

The generator extracts only the trusted Git snapshot into an owned temporary directory, verifies old discovery against every admitted test/dependency fingerprint, then computes the new dependencies using a committed algorithm. It never fingerprints candidate test/helper edits into a new expectation. Provenance includes the full source revision/tree, algorithm revision/hash and generator revision. Runtime checks verify immutable introduction, trusted source/tree, committed algorithm hash and the historical ID/implementation set. Existing shrink-only eligibility and ordinary implementation/helper comparisons remain authoritative.

`rebind_release_bookkeeping.py` retains the original interpretation for historical immutable records. New command-generated release records also carry translated test-owned dependencies; later release bindings take precedence over the older policy snapshot even when a method's implementation fingerprint is unchanged. The new policy never restores a retired test or makes an altered assertion eligible.

No existing assertion was removed or changed; eight new ownership/protection tests were added. All original product tests and assertions remain unchanged. The temporary product edit is restored after each diagnostic run and is not committed. `fix/rsa-step3` and its worktree are untouched. No installation, push, control-project edit or general cleanup was performed.

The one authorized full run and the post-full RSA affected-module verification follow below, with the four reporting classes retained.

TF-23 manifest validation before the full run: 5.000910 job-seconds, default 16 jobs, 10 tests pass; exactly 80 deferred lifecycle metadata errors. All 15 pre-existing baseline/debt files and all package source/test files are unchanged from 5ab0a9d3.
