# Test framework handoff

## Step 0 — measure (complete)
- Commit: `2846356e` — baseline, top-15 profiles, reproducible capture and discovery inventory.
- Decision 75 / step 0 evidence: `MEASURE-test-framework.md:16`, `measurements/test-framework/baseline-timings.json:1`.
- Full default run: 16 jobs, 4,849.08 job-seconds, 329.6 s runner wall; 5,683 tests (95 existing skips), 288 modules.
- Existing red: `tests/test_e2e_requirements.py:53` — 80 requirements have no check; unchanged, for step 4.
- Top-15 evidence: `MEASURE-test-framework.md:85`; 2,552.35 baseline job-seconds (52.6%); profile exit 0, 2,339.63 job-seconds, 844 test IDs selected exactly once.
- Cheapest candidates and measurement limits: `MEASURE-test-framework.md:150` and `:68`; no optimization savings claimed.
- Removed/changed assertions: none; deleted tests: none. Production, runner and test files unchanged.
- Open: Linux/Python 3.11 evidence, descendant/native-flush visibility, existing skips; scope and limits documented.

## Step 1 — requirements (complete; ready for review)
- Commit: `c5b1d0bc` — TF-1..TF-22 and step-0 handoff; measurement Markdown formatting only otherwise.
- TF-1..7 -> `REQUIREMENTS-test-framework.md:47` (runner, layers, coverage, shrink-only legacy list); TF-8..9 -> `:106` (budgets/timing).
- TF-10..13 -> `REQUIREMENTS-test-framework.md:125` (real-I/O guards); TF-14..15 -> `:153` (one gated golden helper).
- TF-16..18 -> `REQUIREMENTS-test-framework.md:170` (test-only Hypothesis/profiles/soak); TF-19..22 -> `:196` (merge/scenario/pruning/review gates).
- Validation: 22 unique sequential IDs; failing witnesses specified; identifier commit hook and whitespace checks pass.
- Red/green framework tests: not started; requirements review precedes step 2. Step-0 baseline failure remains visible.
- Removed/changed assertions: none; deleted tests: none. Existing source/tests/runner/stage files unchanged.
- Open for review: numeric budgets and stage/layer mapping (`:25`), catalogue and legacy fingerprint/base contract (`:69`, `:94`).
- Grandfather manifest generation and verified Hypothesis version/hash are step 2; committed baseline inventory supplies the starting IDs.
- Stopped after step 1 for primary/owner CoCoLo review; merge-library worktree untouched, no rebase or implementation begun.

## Step 2 — runner rules and test tools (implementation complete)
- Commits: `05225579` immutable 5,683-test baseline; framework implementation follows this report in the same commit.
- TF-1..18 -> `tests/test_test_framework.py`, `tests/test_test_framework_guards.py`; red/green witnesses in `measurements/test-framework/step2-evidence.json`.
- Latest affected run: 45 tests, four modules, no failed workers or over-budget modules; 8.534 job-seconds (`measurements/test-framework/step2-run.json`).
- Global exit remains 1: 80 missing lifecycle checks and TF-19..22 later-step checks; none waived or marked passing.
- Existing runner/CI-stage tests pass unchanged. Structured worker outcomes reject skipped-only proofs, zero exits without results and caught guard violations.
- CI prepares hashed test dependencies on Python 3.11 and combines six shard records before enforcing complete module budgets.
- Hypothesis 6.170.17 and sortedcontainers 2.4.0 installed only in the owner-authorized temporary test environment; hashes and primary-source provenance committed.
- Removed/changed assertions: none; deleted tests: none. New negative tests were run red before corresponding code changes.
- Review checkpoints moved to completion by owner; continuing to step 3. Linux execution remains an outstanding CI gate; local evidence is macOS.

## Step 1 review r1 — applied before implementation reconciliation
- Review source: `REVIEW-testfw-step1-r1.md`; fixes 1–14 and optional 15 applied; TF-8 option A selected by owner.
- Fix -> `REQUIREMENTS-test-framework.md` line: 1 -> 266, 2 -> 257, 3 -> 191, 4 -> 77, 5 -> 225.
- Fix -> `REQUIREMENTS-test-framework.md` line: 6 -> 23, 7 -> 63, 8 -> 87, 9 -> 103, 10 -> 165.
- Fix -> `REQUIREMENTS-test-framework.md` line: 11 -> 219, 12 -> 260, 13 -> 110, 14 -> 58, 15 -> 44.
- Additional fix-14 witnesses: TF-5 -> 97, TF-13 -> 183, TF-15 -> 202, TF-19 -> 250.
- Budget option A -> 128; discovery/preflight timings (fix 6) -> 147.
- Commit: this requirements/report commit; no executable or test assertions changed. Existing step-2 implementation now needs reconciliation with these stricter rules.
- Next review stop: after step 3, per latest owner instruction; steps 4–5 wait for that review.

## Step 2 — step-1 r1 implementation reconciliation
- Commits: `f9902898` fixes 1–14 plus optional 15; `29ddc549` immutable dependency hashes and budget census; implementation in this report's commit.
- TF-8 option A: 16 baseline scenario modules exceed 60s; ceilings generated from saved timings, warned every run, shrink-only/history checked (`MEASURE-test-framework.md`, `scripts/test-budget-debt.json`).
- TF-4/7/14: exact source comparison, registered pending review/future checks, transitive fixture hashes, explicit trusted bases, golden-bypass rejection (`tests/test_test_framework_review.py`).
- TF-1/9/10/11/18: bounded worker-group teardown, secrets/temp isolation, saved preflight failure timing, discovery-time guards, inherited sockets, explicit/conflicting soak/profile refusal (`tests/test_test_framework_guards.py`).
- Red/green ledger: `measurements/test-framework/review-evidence.json`; six affected modules passed 62 tests in 10.913 job-seconds before the last two added guard witnesses.
- Latest affected guard/review/stage run: 32 tests, no failed workers or budget failures, 16.771 job-seconds; `measurements/test-framework/review-framework-run.json`.
- TF-13/16 entry inventory names real checks for 13 entry points. Owner-approved replacement host modules passed 46 tests in 37.096 job-seconds (`review-replacement-run.json`).
- Assertion replacements: `ASSERTIONS-test-framework.md:10` lists the two removed host help-probe cases and their unchanged start-to-finish replacements; no test function deleted and no existing assertion weakened.
- Global exit still 1 for the 80 missing lifecycle checks; future ML/scenario/review records remain not_run, not passing. Linux/Python 3.11 execution still awaits CI; source grammar checked for 3.11.
- Next: carry reconciliation to merge branch and finish step 3; stop for review after step 3, as owner requested. No push or product install performed.

## Step 3 — implementation committed; requirement conflict needs review
- Commits on `feat/merge-library`: `8b060c4f` checker migration, `1f44c386` new witnesses, `ee12c570` durability model/library fixes and evidence; rebased on framework `4e5ca6a1`.
- TF-18/19 -> `../cocolo-mergelib/MERGE-test-framework-step3.md:9`; one checker, six-action Hypothesis machine, volatile/durable model, committed normal inventory and explicit soak lane.
- TF-19 table evidence: 40 explicit source labels / 63 positive-and-negative variants; the source's “41” count discrepancy is documented at `../cocolo-mergelib/CONTRADICTIONS-merge-checker.md:70`.
- Red/green evidence -> `../cocolo-mergelib/measurements/test-framework/merge-step3-evidence.json`; default 16-job affected run passed 204 tests in 77.310938334 job-seconds, no worker/budget failures.
- Unaffected crash subset: 81 tests (3 existing opt-in skips), 63.539055000 job-seconds; explicitly partial and excludes the two stopped C-3 cases (`merge-unblocked-final.json`).
- Four mutation proofs pass -> `../cocolo-mergelib/packages/codex-agents/tests/test_merge_library_mutations.py:24` (private location), `:35` (third commit), `:47` (missing A flush), `:54` (swallowed EIO).
- Assertion replacements -> `../cocolo-mergelib/ASSERTIONS-merge-checker.md:7` (38 changed/moved accepted assertions, each with replacement line); no test deleted; C-1/C-2 owner-approved corrections applied.
- Open C-3 -> `../cocolo-mergelib/CONTRADICTIONS-merge-checker.md:41`: nested recorded folders after undo cleanup failure conflict with literal empty-folder endpoints; two tests remain red, unchanged, stopped. No waiver or expected-failure marker.
- Latest portability/mutation follow-up: 16 tests pass in 1.527262709 job-seconds; ci selection of the soak module correctly refuses before execution. Full soak, Linux/Python 3.11 runtime and Ruff remain unverified.
- Global gate still reports the 80 missing step-4 lifecycle checks. Step 3 is not complete; steps 4–5 have not begun. No push, product install or control-project edit.
- Ready for review of the committed implementation and C-3 requirement decision; preserved the mandatory stop-on-contradiction rule without asking further questions while the owner is away.

## Step 2 — r1 review fixes
- Commits: failing witnesses `38e036fb`, `8aa5fba9`, `6c32fcb7`; fixture repair `e679d183`; implementation `438e1af1`, timeout diagnostic `c0718548`; relocation witness `d32f5c9d`, repair `c54e83e7`, root-resolution cost fix `6eb7cea0`. Requirement definitions are unchanged.
- A1/A3 -> `scripts/test_framework.py:360`; A2/C4 -> `scripts/test_framework.py:298`; A4 -> `scripts/test_framework.py:535`; A5 -> `scripts/test_framework.py:569`.
- A6 -> `scripts/test_framework.py:495`; A7/A8 -> `scripts/test_framework.py:190`, `scripts/test_framework.py:224`; A9/A10 -> `scripts/test_framework.py:69`; A11 -> `scripts/test_framework.py:51`; A12 -> `scripts/test_framework.py:558`, `scripts/generate_test_metadata.py:82`.
- B1/B2 -> `scripts/test_worker.py:66`, `scripts/test_worker.py:120`; B3/B4/B8/B10 -> `scripts/parallel_tests.py:434`, `scripts/parallel_tests.py:470`, `scripts/parallel_tests.py:85`, `scripts/parallel_tests.py:504`; B9 -> `scripts/test_worker.py:160`.
- B5/B6 -> `.github/workflows/ci.yml:73`, `.github/workflows/ci.yml:260`, `scripts/test_framework.py:259`; B7 -> `scripts/parallel_tests.py:71`.
- B-note confirmed: immutable v1 helper hashes remain checked; v2 referenced constants/explicit source files added in `scripts/test-legacy-values.json` (5683 IDs, frozen at first commit). The additional immutable `test-legacy-portable-values.json` removes root-dependent strings; 306 original sources were verified against `438e1af1` before generation, original baselines untouched (`portable-baseline-provenance.json`). Trusted ceiling/history checks -> `scripts/parallel_tests.py:179`, `scripts/parallel_tests.py:225`; unresolved/changed v2 hashes -> `scripts/test_framework.py:485`; failed/missing fragments cannot retire debt.
- C1/C2/C5 -> `tests/test_test_framework_guards.py:35`, `tests/test_test_framework_guards.py:71`, `tests/test_test_framework_guards.py:188`; C3/C6/C7 -> `tests/test_test_framework.py:576`, `tests/test_test_framework.py:503`, `tests/test_test_framework.py:33`.
- C8/C9/C10 -> `tests/test_test_framework_guards.py:212`, `tests/test_test_framework_guards.py:374`, `tests/test_test_framework.py:562`, `tests/test_test_framework_guards.py:445`, `tests/test_test_framework.py:239`.
- C11/C12 -> `tests/test_test_framework.py:265`, `tests/test_test_framework_guards.py:152`, `tests/test_entrypoint_inventory.py:40`; C13 -> `tests/test_test_framework_guards.py:491`, `tests/test_test_framework_guards.py:506`, `tests/test_test_framework_guards.py:454`, `tests/test_test_framework.py:489`; C15 -> `scripts/test-layers.json` declares both subprocess modules scenario.
- TF-1..18 evidence: all five affected modules / 95 tests pass at default 16 jobs, **19.20834891695995 job-seconds**, no worker/budget failures (`measurements/test-framework/review-r1-evidence.json`). The overall exit remains 1 for exactly 80 missing step-4 lifecycle checks; none is waived.
- B11 latest core retest: 44 tests pass in **0.7585082500008866 job-seconds**. History validation **0.2059470409876667s** needs no cache; discovery **15.707939458952751s**, preflight **0.5907899580779485s** are separate. Resolving the root once per fingerprint cut the prior 44.579s discovery cost without changing any baseline hash (`MEASURE-test-framework.md`).
- Assertion replacements -> `ASSERTIONS-test-framework.md` r1 appendix; no test deleted. C14: macOS/Python 3.14.7 executed; macOS and Linux Python 3.11 runtime, Ruff, and the final full requirement gate remain outstanding. Python 3.11 syntax parsing passed.
- Next checkpoint is the requested confirming review of steps 2–3. Steps 4–5 have not started; no push/product install/control-project edits.

## Step 3 — r1 fixes and decision 91 complete for review
- Commits on `feat/merge-library`: tests-first `76e490e2` through `e2f01c96`; implementation `7a09e822`; full discovery census `3fcd71b3` / `6e64cd7b`; read-only fault witness/fix `97e5512e` / `a721afbf`; final hand-off `a3901179`. Repaired framework code is integrated on that branch.
- TF-18/19, ML-1..30 and ML-R91 -> `../cocolo-mergelib/MERGE-test-framework-step3.md` r1 fix table: every A1–A10, B1–B11, C1–C8 and L0–L14 maps to its actual enforcing line. A1 snapshots durable authority independently before every fault.
- Decision 91=A -> `../cocolo-mergelib/REQUIREMENTS-merge-review.md:3`: both original C-3 methods pass; only recorded, identity-matching empty folder trees qualify. Foreign files/folders and replaced identities are rejected; original endpoint and full modes remain independent.
- Final ten affected merge modules: **242 passing tests, 76.9704737511347 job-seconds**, default 16 jobs, no worker/budget failure. Later checker/mutation follow-up: **75 passing tests, 34.64630295801908 job-seconds**.
- Final five real campaigns, each a separate default-16-job invocation: **146.5805857910309 job-seconds**, 78 checks (75 pass; three original mounted-image opt-ins skipped), every module below 60s. No full suite was rerun.
- All four mutation proofs pass: private-location removal -> ML30 forgery; omitted pre-A flush -> exact lost staged contents plus activated history-phase mutation; extra commit -> ML6 ordering; swallowed EIO landing -> single checker. Names and lines are in the merge hand-off.
- Assertion replacements -> `../cocolo-mergelib/ASSERTIONS-merge-checker.md`: all 38 original changed expressions now cite specific end-state cells and enforcing lines, plus r1 additions. No test deleted; the census preserves all original checks and image skip conditions.
- Evidence -> `../cocolo-mergelib/measurements/test-framework/review-r1-merge-evidence.json` (35 raw-report digest-bound extracts). Final metadata has exactly 80 missing step-4 lifecycle checks; no legacy fingerprint errors. Global exit remains 1, never presented as a complete green requirement gate.
- Still open: concurrent five-campaign budgets (475.9808796250145 job-seconds, all five over budget), full soak, mounted-image opt-ins, macOS/Linux Python 3.11 runtime, Ruff, and step-4 lifecycle coverage. Python 3.11 grammar and identifier/whitespace checks pass; no budget raised or fault coverage cut.
- No owner question remains. This is the requested review stop; steps 4–5 have not started. No push, product install, primary/control-project edits or general cleanup.

## Step 2 — round-2 scoped MED fixes (decisions 99 A / 100 A)
- Tests first: `4578546a`, `19ad4955`, `17a7d734`, `cc22fc21`; fixes `7c53616c`, `b0e4f432`; specific tags `d1bd5d63`, assertion preservation `e3b35ad5`.
- All M1–M8/N1–N3 -> `REVIEW-test-framework-r2-fixes.md:7`: complete requirement -> fix/witness file:line table, exact intended red diagnostics and counterfactual controls.
- Decision 100 -> `REQUIREMENTS-test-framework.md:163`, `scripts/parallel_tests.py:71`, `tests/test_test_framework_guards.py:566`: allowlist covers discovery and workers, local and CI-parent environments; token/password canaries are absent, allowed-variable controls survive.
- TF-7 bound baseline retains 5,683 IDs; 304 contributing sources verified against pre-pass `8cfab050`, 1,356 specific helper declarations, zero unresolved fingerprints; original immutable baselines untouched (`bound-baseline-provenance.json`).
- Final four modules: 95 checks, **20.520382499031257 job-seconds**; final guard follow-up: 37 checks, **19.76074541604612 job-seconds**, default 16 jobs, no worker/budget failures.
- Evidence -> `measurements/test-framework/review-r2-framework/runs.json`, sensitivity JSON and intended-red logs. Timings are saved-run job-seconds, not wall-time claims.
- Every changed assertion -> `ASSERTIONS-test-framework.md` round-2 table; no test deleted. Budget fixture contradiction reconciled by an explicit plan, unchanged expected assertions (`CONTRADICTIONS-review-r2.md`).
- All 17 LOWs listed in `REVIEW-test-framework-r2-fixes.md`; only L8 resolved, as explicitly mandated by decision 100. Others deliberately untouched.
- Global exit remains 1 for exactly 80 missing lifecycle checks. macOS/Linux Python 3.11 runtime, Ruff and the complete requirement gate remain open; Python 3.11 grammar checked only.
- Ready for the requested whole-worktree review; steps 4–5 remain stopped. No push, product install, primary/control-project edit or general cleanup.

## Step 3 — round-2 scoped MED fixes
- Tests first on `feat/merge-library`: `a2ab496b`, `3fb8af31`, `83ea6830`; fixes `81cc6fac`; specific tags `16be3593`; framework integration `67f89368` plus final framework hand-off merge.
- All M1–M6 -> `../cocolo-mergelib/REVIEW-merge-library-r2-fixes.md:7`: requirement -> exact enforcing/witness line and intended failure; earlier A1 HIGH remains fixed.
- Integrated eleven affected modules: **253 passing checks, 90.15853699890431 job-seconds**, default 16 jobs, no worker/budget failures; all four original mutation proofs pass.
- Separate real crash campaign: **49 checks (46 pass, three existing mounted-image skips), 45.08007408300182 job-seconds**, default 16 jobs, no worker/budget failure. Decision-91 cases pass unchanged.
- Evidence -> `../cocolo-mergelib/measurements/test-framework/review-r2-merge/runs.json` and `merge-sensitivity.json`; exact failures checked for both missing machine provenance fields and every named read/cancel/retry boundary.
- Changed assertions -> `../cocolo-mergelib/ASSERTIONS-merge-checker.md` round-2 appendix; no test deleted, sampling reduced, expected failure introduced or budget raised.
- All 13 merge LOWs and conditional review items remain listed in `../cocolo-mergelib/REVIEW-merge-library-r2-fixes.md`, deliberately unfixed.
- Open: full soak, mounted-image opt-ins, macOS/Linux Python 3.11 execution, Ruff, concurrent real-campaign budgets and the 80 lifecycle checks. No complete green gate is claimed.
- This completes the requested scoped pass and stops for whole-worktree review; steps 4–5 have not begun.

## Round-3 fixes

- Decision 102: all 15 MED addressed in one scoped pass; no HIGH was found. Every new LOW remains explicitly listed, with no LOW fix in this pass.
- Framework map (nine MEDs, p03, TF-23): `REVIEW-test-framework-r3-fixes.md`; merge map (six MEDs): `../cocolo-mergelib/REVIEW-merge-library-r3-fixes.md`. Each gives requirement, enforcing/witness file:line, old-code red, green and positive control.
- Requirements `56971db9`/`101a2c0a` and `cb3c073d`; tests `0e0a0ccf`/`6d16db54`/`c4cb8725`/`9aea7ffe`; fixes `d1d4646d`/`fb7b48c8`/`4d7333a5`; versioned baseline `f3d56636`; evidence `bc8beac3`/`5e88116b`.
- TF-23 -> `REQUIREMENTS-test-framework.md:304`, `scripts/test_framework.py:686`: small required-evidence manifest check; missing red/green/control fails. Semantic relevance still requires review.
- Every red replays exact reviewed implementation files at `9c18c4a3` and checks the intended diagnostic. Default-location and TF-ID setup mistakes were corrected before enforcement and are not counted as intended reds.
- Final framework: **115 checks; 26.99471895804163 job-seconds; DEFAULT 16 jobs**; integrated merge: **270 checks; 112.91237787512364 job-seconds; DEFAULT 16 jobs**. No failed workers or budget failures; four mutation proofs pass.
- Separate real campaign: **49 checks; 44.652967874950264 job-seconds; DEFAULT 16 jobs**, 46 passing / three existing image skips. Saved timing extracts and per-fix evidence are in each worktree's `measurements/test-framework/review-r3-*` directory.
- p03 already rejects all four categories at `scripts/test_framework.py:282`; valid/invalid real-combiner controls recorded; no speculative caller change.
- Assertion ledgers list every changed/moved instance; no test deleted. Golden valid-fixture reconciliation is in CONTRADICTIONS-review-r3.md. Baseline keeps 5,683 IDs, verifies 304 unchanged sources, preserves every older immutable baseline, zero unresolved fingerprints.
- Global gate remains exit 1 for the same 80 lifecycle checks. Full soak, image opt-ins, macOS/Linux Python 3.11 runtime, Ruff and concurrent real-campaign budgets remain open; Python 3.11 grammar only is verified locally.
- Framework branch is merged into feat/merge-library. Both branches stop for the requested short review; steps 4–5 have not started. No push, install, primary/control-project edit or general cleanup.


## First integrated full-suite fixes
- Merged b5318440 into both own branches; all 32 incoming test files preserved, all six discovery/resource test files unchanged.
- Requirements 97bc854c → red 9bbf1582 → fixes 56941ec4/51257b92/9f76022e; evidence c0c8c76f; retirement 190a6deb. TF-23/24 fix → line/control table: REVIEW-test-framework-fullrun-fixes.md.
- Discovery import/qualified names fixed; five layers corrected with reasons; guards unchanged; default-capacity runner isolates complete merge campaigns.
- Exact local full base: b531844030fa619c1f62ddd6c092ebc2ea4fca38. CI trusted event paths retained; nightly retained.
- ONE full run: 496.778018 s elapsed, 4535.410045 job-seconds, 16 default jobs, 395 units / 6193 tests; zero budget failures. Saved flat timing + run files under measurements/test-framework/release-0850.
- Seven unexpected units exposed missing primary bookkeeping; merge branch applies its runtime/cap registration and paired 0.8.50 version/digest with unchanged affected tests. Original full reds remain recorded.
- Remaining expected worker: root:test_e2e_requirements; 80 lifecycle metadata errors. Review/soak/image/future-step obligations preserved separately as owner requested; no claim of a green aggregate requirement gate.
- Debt: immutable new ceiling version; cli_billing_runtime/run_network retired from primary evidence, billing_gate/billing_rejudge/ledger_startup/session_guard retired from our full run. Fourteen current entries, unchanged ceilings, table in review report.
- All assertion corrections are new-witness requirement contradictions recorded in ASSERTIONS-test-framework.md; no original product/test assertions removed or changed.
- No push, install, shared/control edits, general cleanup or steps 4–5. Existing hash-pinned environment reused; Python 3.11/Linux remains CI evidence.
- Final affected bookkeeping verification: 159.420827 job-seconds, 254 tests, all workers pass; exactly 80 deferred metadata errors, no budget failure. Runtime metadata fixes and final binding snapshot are on feat/merge-library.

## Merge runtime bookkeeping follow-up
- Full-run exact runtime/file guards and O0c bounded-cap guard exposed missing primary 0ca9c36f metadata; requirements notes 4561b23b/b312c401 precede fixes db76fc52/f49092d9 and binding snapshot 11d36a06.
- TF-24/23 fix → file:line, unchanged red/green witnesses and positive controls: REVIEW-merge-library-fullrun-fixes.md; scripts/test-review-witnesses-runtime-integration.json.
- Final affected run: 159.420827 job-seconds, 36.655826 s elapsed, default 16 jobs; every worker passes, no budget failure, only 80 deferred lifecycle metadata errors.
- Four primary runtime metadata files copied exactly; all old tests/assertions and 32 incoming test files preserved. Fourteen active debt entries remain; immutable ceilings/baselines untouched.
- One full run only; its seven unexpected failures remain in evidence and are resolved by affected reruns. Review/soak/image/future-step obligations remain explicit, as authorized.

## Installed-release restoration and routine bookkeeping
- TF-25 requirements 24b5cc0b → failing witnesses 4a261f41 → exact primary restoration 87d78e55 → rebinding 9e095c4c/fbacae18; final TF-23 evidence 3993143a, merged for the full run at 65ca8c20.
- New immutable `scripts/test-release-bindings/0.8.49.json`; old baselines untouched. Narrow append/count/annotation procedure in TESTING-framework.md; eight positive/mutation witnesses with fix→line evidence in REVIEW-test-framework-release-bookkeeping-fixes.md.
- Affected green: 32 tests, 10.371962 job-seconds. ONE full run: default 16 jobs, 509.456003 s elapsed / 4825.323536 job-seconds, 396 units / 6201 tests, exact b5318440 base and saved timings.
- Exactly 80 deferred lifecycle metadata errors; no other metadata errors; no budget failures or retirements; 14 unchanged debt entries.
- Failed units: expected root:test_e2e_requirements and unexpected root:test_test_framework_guards (nested watchdog probe returned zero workers; original diagnostic lost). This unexpected failure remains open, so not ready for merge.
- Every original watchdog assertion retained; added output-bearing cardinality assertion at tests/test_test_framework_guards.py:466. Affected rerun passes (19.177335 job-seconds); 50-probe experiments pass on both worktrees (49.164339 / 49.380317), but do not establish a root-cause fix.
- Four reporting classes, every outstanding obligation/reason/closing lane and debt measurements are preserved in the review report and full-outstanding-obligations.json. No missing evidence converted to a pass.
- Assertions ledger records the two 9→10 restorations and additive diagnostic; no tests deleted or guards/budgets relaxed. No install, push, control-project edit, general cleanup or steps 4–5.
