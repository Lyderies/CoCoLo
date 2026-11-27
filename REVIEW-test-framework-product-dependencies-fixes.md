# Product changes and legacy test proof ownership

TF-26 requirements first: `cc3ef2c7`; initial failing witnesses `912ad5c8`; ownership fix `249d4f7d`. The later positional/keyword-default witness `765cd2e5` precedes fix `813661df`; release/policy precedence witness `ed171c73` precedes fix `cc236c6b`. No product code fix is included in these branches.

The primary's failure is reproduced exactly: the four lines from product revision `2357016b` cause **1,028 legacy errors across 79 modules** on the `5ab0a9d3` framework, plus the 80 already-deferred lifecycle errors. The primary's `stable()` suspicion is incomplete: the old `walk()` already excludes most product functions, but imported product constants are hashed by value and imported defaults retain captured values. `COLLECTOR_RUNTIME_DIGEST` changes when the product source changes. Source ownership also formerly trusted test-looking module names; the new alias witness covers that separate boundary.

The first ownership fix leaves seven errors, all in `CliObservationRuntimeTests`, whose `hint(..., digest=COLLECTOR_RUNTIME_DIGEST)` helper captures the product digest as a keyword default. The default witness reproduces this independently (including positional defaults); the final policy records imported defaults by identity and retains captured values for test-owned defaults.

## TF-23 fix table

Every repair witness below has an old-code red record at exact `5ab0a9d3` (only the RSA product edit and the added witnesses/catalogue are present), its intended diagnostic, and a green record. `scripts/test-review-witnesses-product-dependencies.json` binds the evidence. The two additional tests for shared-helper/constant edits and assertion edits/renames are **positive protection controls**: they pass on both old and new code, and are not misrepresented as old-code failures.

| TF-26 repair | Enforcing file:line | Witness file:line | Positive control |
|---|---|---|---|
| product_only_constant_and_implementation_changes_keep_legacy_proof | `scripts/test_framework.py:453` | `tests/test_product_dependencies.py:43` | Unchanged product/test state is equal; a product implementation-only change is accepted; shared-helper edits remain rejected. |
| product_symbol_identity_changes_are_not_ignored | `scripts/test_framework.py:550` | `tests/test_product_dependencies.py:77` | Same identity is equal; another product module with equal values changes the proof. |
| source_ownership_wins_over_test_looking_product_alias | `scripts/test_framework.py:416` | `tests/test_product_dependencies.py:84` | Unchanged source is equal; a product function with a test-looking module alias may change its implementation. |
| imported_product_defaults_use_identity_but_owned_defaults_keep_values | `scripts/test_framework.py:473` | `tests/test_product_dependencies.py:94` | Unchanged defaults are equal; product positional/keyword defaults may vary, but a test-owned default change is rejected. |
| migration_rejects_wrong_provenance_and_changed_test_identity | `scripts/test_owned_baseline.py:23` | `tests/test_product_dependencies.py:135` | Correct provenance and unchanged IDs/fingerprints are accepted before and after wrong source, changed ID/fingerprint and unresolved dependency controls. |
| later_bookkeeping_proof_takes_precedence_over_older_policy_record | `scripts/test_owned_baseline.py:63` | `tests/test_product_dependencies.py:105` | Old release records receive the new policy; a later release record wins even with the same test implementation fingerprint. |

Exact final old-code replay: **0.105896 job-seconds**, default 16 jobs; six intended failed witnesses and two passing protection controls. Final affected run with the exact RSA product edit: **34.292031 job-seconds**, default 16 jobs, **120 tests pass**, **zero legacy errors**, exactly 80 lifecycle metadata errors, no budget failure. Detailed records, worker logs, raw SHA-256 digests and temporary source-replay provenance are in `measurements/test-framework/product-dependencies/`. The default and precedence intermediate reds and the seven-error observation are preserved as additional evidence, not substituted for the exact-base replay.

## Immutable migration

`scripts/test-legacy-test-owned-v1.json` (introduction `d4f9ef00`) and `test-legacy-test-owned-v2.json` (introduction `7a58d4d3`) are new versions. V1 remains unchanged. Both translate the same **5,739** admitted legacy IDs from trusted `5ab0a9d3`, keeping every test implementation fingerprint unchanged; v2 changes 334 dependency records relative to v1 as defaults acquire identity semantics. All earlier baselines, active legacy lists, debt lists and ceilings remain byte-identical to the reviewed base.

The generator extracts only the trusted Git snapshot into an owned temporary directory, verifies old discovery against every admitted test/dependency fingerprint, then computes the new dependencies using a committed algorithm. It never fingerprints candidate test/helper edits into a new expectation. Provenance includes the full source revision/tree, algorithm revision/hash and generator revision. Runtime checks verify immutable introduction, trusted source/tree, committed algorithm hash and the historical ID/implementation set. Existing shrink-only eligibility and ordinary implementation/helper comparisons remain authoritative.

`rebind_release_bookkeeping.py` retains the original interpretation for historical immutable records. New command-generated release records also carry translated test-owned dependencies; later release bindings take precedence over the older policy snapshot even when a method's implementation fingerprint is unchanged. The new policy never restores a retired test or makes an altered assertion eligible.

No existing assertion was removed or changed; eight new ownership/protection tests were added. All original product tests and assertions remain unchanged. The temporary product edit is restored after each diagnostic run and is not committed. `fix/rsa-step3` and its worktree are untouched. No installation, push, control-project edit or general cleanup was performed.

The one authorized full run and the post-full RSA affected-module verification follow below, with the four reporting classes retained.

TF-23 manifest validation before the full run: 5.000910 job-seconds, default 16 jobs, 10 tests pass; exactly 80 deferred lifecycle metadata errors. All 15 pre-existing baseline/debt files and all package source/test files are unchanged from 5ab0a9d3.

## Full-run generated-helper regression and correction

The single full run at `93624f19` exposed `ReviewTests.test_fixture_and_transitive_helper_changes_change_dependency_fingerprint` (`tests/test_test_framework_review.py:81`). Its unchanged assertion at line 100 saw equal empty dependency hashes for two different generated helpers. The new ownership rule had excluded an exec-generated test module lacking `__file__`. This is an implementation regression against TF-7/26, not a test/requirement contradiction.

Requirements already require test-owned generated helper changes to be detected. New witness `261f3acb` reproduces it before fix `d56e084c`; the intended red is at **93624f19**, where this regression was introduced, not falsely attributed to 5ab0a9d3. `tests/test_product_dependencies.py` now also checks that an unchanged generated fixture is stable and a separate generated product namespace remains external. The implementation recognizes the defining test namespace when no source file exists and does not infer ownership from the current working directory. File-backed product source still takes precedence.

Red: **0.316762 job-seconds**, both the new witness and unchanged original review test fail for the intended hash-equality reason. Green after the fix: **35.992503 job-seconds**, default 16 jobs, **135 tests pass** across all nine affected framework modules, including r2 and review; exactly 80 deferred lifecycle metadata errors, zero legacy errors, no budget failure. All 5,739 existing legacy fingerprints still match v2; no additional baseline replacement was needed. The added TF-23 declaration and `generated-*.run.json` records preserve this distinction from the six original exact-5ab0a9d3 repair witnesses.

## RSA branch verification after the full run

On our own merge-library worktree at `d56e084c`, temporarily applied the exact product file, new test module, requirement document and additive changes to three metadata files from **2357016b**, preserving our TF-26 catalogue entries. Ran the **nine** modules directly importing account_claim_jobs plus `tests.test_rsa_step3`; every overlay file was restored in `finally`. The RSA branch/worktree was never edited.

Result: **23.239675 s runner elapsed / 195.150842 job-seconds**, default **16 jobs**, **15 units / 199 tests**, **zero failed workers**, **zero legacy errors**, exactly **80 lifecycle metadata errors**, **zero budget failures/retirements**. The supplied RSA3-3 late-completion probe reports its owner-designated `expected_failure`; it remains visible as non-passing requirement coverage and was not modified. All input hashes, module names and additive catalogue keys are in `rsa-overlay-provenance.json`; structured results and the RSA worker log are retained beside it.

Generated-helper follow-up enforcing line: `scripts/test_framework.py:508`; new witness `tests/test_product_dependencies.py:118`; original unchanged witness `tests/test_test_framework_review.py:81`. Its declared red base is 93624f19, red 0.316762 job-seconds → green 35.992503, with unchanged-fixture and external-generated-product controls.

## Single full run — four reporting classes

The one authorized full run used clean merge-library revision **93624f19**, the pinned prepared environment, exact base **b531844030fa619c1f62ddd6c092ebc2ea4fca38** and `--save-timings`, with **16 default jobs**. Results: **504.505392 s runner elapsed / 4658.292280 job-seconds**, **397 units / 6209 tests**. Discovery 35.357534 s and preflight 18.162080 s are separate; measured end-to-end wall time is 558.110523 s. Raw timings, structured report, command/revision/hash provenance and failed worker logs are retained. The aggregate exit is 1; the record is not relabelled as an all-green full run. No second full suite was run.

### 1. Failed units

| Full-run unit | Disposition |
|---|---|
| `root:test_e2e_requirements` | Expected: 80 deferred step-4 lifecycle checks. |
| `root:test_test_framework_review` | Generated-helper regression described above. Fixed in d56e084c; the original failing test is unchanged and passes in the 135-test affected rerun. Full red remains preserved. |

The earlier watchdog incident did not recur in this full run. No additional failed workers remain unexplained from this run.

### 2. Deferred lifecycle metadata

Exactly **80** lifecycle metadata errors, **zero legacy errors**, and **zero other metadata errors**. Step 4 closes these existing lifecycle obligations and was not started here.

### 3. Not-run obligations

`full-outstanding-obligations.json` preserves all **52** exact missing check IDs, requirements, raw status, reason and closing step/lane. None is counted as a pass.

| Obligations | Reason | Closing evidence |
|---|---|---|
| `review:TF-5`, `review:TF-6`, `review:TF-19`, `review:TF-22` | Registered review records were not supplied to this invocation. | Supply the cited owner/CoCoLo review records. |
| `review:TF-21` | Step-5 review remains out of scope. | Complete step 5 and its review. |
| `planned:TF-19` | Step-3 acceptance placeholder is still registered. | Replace with executed, cited acceptance evidence. |
| `planned:TF-20` | Step-4 acceptance is deferred. | Complete step 4. |
| Complete soak checks (ML-5/6/10/11/12/13/18/19; TF-18/19) | Complete sweep is excluded from the normal ci profile. | Run the soak campaign; exact IDs are in the artifact. |
| Three real-image checks (ML-4/12) | Normal run did not enable the opt-in real filesystem/image fixtures. | Execute the approved image/platform lane. Raw `skipped` outcomes and strict non-passing coverage remain intact. |

Python 3.11/Linux remain platform evidence outside this macOS/Python 3.14.7 run. The separate RSA run retains its supplied RSA3-3 expected-failure probe; it is not converted to passing requirement coverage.

### 4. Budget debt and observed overruns

The full run has **two budget failures**. Tests, scheduling, limits and debt ceilings are unchanged by this fix. A separate affected-only run of these two modules at default 16 jobs passes, but that does **not** erase the full-run overruns or establish a clean concurrent full-suite timing result:

| Module | Effective limit | Full-run seconds | Isolated affected seconds |
|---|---:|---:|---:|
| `codex:tests.test_container_runtime` | 60.000 | 63.398968 | 44.898609 |
| `root:test_setup_update` | 167.880 existing debt ceiling | 171.402884 | 115.120282 |

Isolated timing run: **160.018891 job-seconds**, 26.755622 s runner elapsed, no failed worker, no budget failure/retirement. These two concurrent-full-run timing overruns remain a reported limitation; no timing exemption, ceiling increase or test change was made. No new merge-library module exceeds its budget. The same **14** debt entries remain, with no additions, removals or ceiling edits:

| Module | Existing ceiling seconds | Full-run seconds |
|---|---:|---:|
| `codex:tests.test_bridge` | 299.500 | 289.562 |
| `codex:tests.test_container_stop` | 67.620 | 62.041 |
| `codex:tests.test_host_commands` | 119.560 | 94.549 |
| `codex:tests.test_learning_cli` | 95.440 | 91.325 |
| `codex:tests.test_lost_run_settlement` | 98.640 | 87.023 |
| `codex:tests.test_project_driver` | 390.320 | 311.226 |
| `codex:tests.test_trial_run_driver_bugs` | 70.220 | 69.992 |
| `codex:tests.test_write_merge` | 241.240 | 193.499 |
| `root:test_install` | 110.140 | 92.071 |
| `root:test_install_claude_upgrade` | 120.330 | 108.855 |
| `root:test_setup_sh` | 177.880 | 155.496 |
| `root:test_setup_straight` | 264.440 | 230.565 |
| `root:test_setup_update` | 167.880 | 171.403 |
| `root:test_setup_wizard_direct` | 265.860 | 229.735 |

TF-26's requested product-change boundary, immutable migration and RSA verification are complete. The full-run generated-helper regression is corrected with unchanged-test evidence. This is a scoped merge handoff with the two full-run timing overruns disclosed, not a claim that the aggregate full suite is entirely green.

Final TF-23 evidence validation: **5.184498 job-seconds**, default 16 jobs, 10 tests pass, exactly 80 lifecycle metadata errors, no failed worker or budget failure. Both original assertions and all pre-existing baseline/release-binding files remain unchanged; all temporary overlays are restored.
