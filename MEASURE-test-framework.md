# Test framework measurements — step 0

Measured 2026-11-22 on `feat/test-framework` at `a1d25560` (0.8.47), in the new
`cocolo-testfw` worktree. No tests, assertions, production files, runner files or committed scheduling
estimates were changed. No optimization is implemented here. The other session may have been using
the same host; its load was not controlled or measured.

## Baseline (one full run, default jobs)

Command, from this worktree:

```sh
python3 -B scripts/parallel_tests.py --save-timings /private/tmp/cocolo-testfw-baseline-timings.json
```

Python 3.14.7; macOS 27.2.1 arm64; 18 reported CPUs; DEFAULT jobs = `max(1, cpu_count - 2)` = **16**.
No `--jobs` override. **4,849.08 job-seconds**, **329.6 seconds runner wall time**, 371 worker units,
288 modules, 5,683 tests reported by unittest (includes **95 existing skips**, counted from worker logs). Discovery/import time before the
runner starts its wall clock is outside that 329.6 s. Job-seconds are elapsed worker time, not CPU time.

| Existing stage | Modules | Job-seconds |
|---|---:|---:|
| unit | 76 | 29.60 |
| integration | 208 | 4,411.06 |
| e2e | 4 | 408.42 |
| Total | 288 | 4,849.08 |

Exit 1, only failed unit: `root:test_e2e_requirements`. Its
`E2ERequirementsTests.test_every_requirement_is_proved_by_a_check_that_exists`
fails 80 subtests because each requirement has an empty `checks` list
(`tests/test_e2e_requirements.py:53`). This agrees with T7 and the supplied analysis: it is the
unfinished step-4 coverage gate, not a test/requirement contradiction. No failure was suppressed.

The old plan's 535 s / 3,189 job-seconds at six jobs is a different run and concurrency. This run has
shorter wall time but more job-seconds; host contention and concurrency are possible causes, not isolated
findings. Decision 75 makes total wall time a watched metric, not a five-minute acceptance target.

Evidence is committed under `measurements/test-framework/`: `baseline-timings.json` is the runner's exact
saved file; `baseline-run.json` is its final result; `baseline-tests.json` is a fresh discovery inventory
(688 root, 4,865 codex, 130 claude tests). The latter is input for the step-2 grandfather generator, not a
coverage waiver. Sum only the 288 discovered module keys in the timing file. It also contains per-test
scheduling estimates and retained historical keys: summing every JSON value would double count.

## Measurement method and limits

The second run selected ONLY the 15 heaviest modules from that baseline through the unchanged
`parallel_tests.py`, with its default 16 jobs and the same committed scheduling estimates. The wrapper
prepended an observer to the runner's in-memory worker source; no repository runner/test file was edited.
It ran 73 workers / 844 tests reported by unittest, exited 0, and measured **2,339.63 job-seconds** and
**160.5 s runner wall time**. Existing skips were retained; exit 0 does not prove every optional platform
case executed. This is an instrumented diagnostic pass, NOT an optimized after-run or a comparable full run.

`profile-timings.json` is the exact saved timing file; `profile-run.json` holds the result;
`profile-summary.json` holds all 73 worker names, phase counters, call counters and selected cProfile
function costs. The observer, selector and summarizer are in `capture/`. For publication,
worktree paths in function labels become `<worktree>`, scripts resolve the root from the working
directory, and thread-local construction uses an import alias (the identifier hook otherwise treats
the attribute spelling as a hostname). These publication-only changes leave all measurements unchanged. To replay this capture on this host, copy those three scripts to
`/private/tmp/cocolo_tf_probe.py`, `cocolo_tf_profile.py`, `cocolo_tf_summarize.py` respectively, and run the
selector after the baseline above. Use a fresh/empty task-owned profile output directory; do not combine
workers from different runs. These are measurement artifacts, not shipped code or a new test framework.

The observer uses a process-local audit hook for launch events and git executable counts, deduplicating
Popen/posix_spawn; real callable wrappers time sleep, Condition.wait, select, Python os.fsync and macOS
F_FULLFSYNC, plus subprocess.run calls whose executable is git. Calls retain their behavior and arguments.
unittest's setup, body, teardown and cleanup dispatch are timed; cProfile supplies additional function costs.

Interpretation limits:

- Process counts are launch audit events/attempts in observed workers, including deliberate failing launches;
  they do not certify every launch succeeded. Exec'd children are not instrumented, and their own subprocesses,
  sleeps and flushes are outside these counters. Worker starts and discovery processes are also excluded.
- The git count includes direct Popen git calls; the timed git subset covers subprocess.run only. It is not
  the total git cost. cProfile child communication and wait costs include time waiting for child work.
- Sleep/Condition/select counters cover observed Python threads, can overlap one another and the test body,
  and can exceed wall time (many service threads wait simultaneously). They are not additive savings.
- Fsync counters are Python API calls, not kernel syscall counts. SQLite/C-level and exec'd-child flushes
  are not visible. Test mocks can deliberately bypass these APIs. A zero never proves no disk durability work.
- Fixture phase columns time TestCase dispatch, including addCleanup work under cleanup. Class/module
  fixtures and imports are in the remaining worker time; detailed function costs are in the JSON. Tests
  that construct fixtures inside the body count that work in body, as unittest does.
- Instrumentation perturbs timings; scheduling/host load also differs. No optimization saving is claimed
  from the difference between the baseline and profile columns. Linux and Python 3.11 remain unmeasured.

## Fifteen heaviest modules

Together: **2,552.35 baseline job-seconds (52.6%)**. Suite prefixes below are canonical runner names.
Each slash-separated phase cell is setup / body / teardown+cleanup seconds from the profile.

| Module | Baseline job-s | Profile job-s | Workers | Setup / body / cleanup s | Setup share |
|---|---:|---:|---:|---|---:|
| `codex:tests.test_project_driver` | 390.32 | 387.37 | 11 | 43.26 / 322.21 / 16.59 | 11.2% |
| `codex:tests.test_bridge` | 299.50 | 287.65 | 9 | 33.07 / 244.88 / 5.99 | 11.5% |
| `root:test_setup_wizard_direct` | 265.86 | 241.25 | 8 | 2.72 / 232.62 / 2.23 | 1.1% |
| `root:test_setup_straight` | 264.44 | 239.35 | 4 | 1.17 / 235.23 / 1.22 | 0.5% |
| `codex:tests.test_write_merge` | 241.24 | 199.55 | 10 | 0.03 / 196.03 / 1.27 | 0.0% |
| `root:test_setup_sh` | 177.88 | 155.25 | 5 | 1.03 / 151.20 / 0.88 | 0.7% |
| `root:test_setup_update` | 167.88 | 140.54 | 5 | 0.99 / 136.50 / 0.84 | 0.7% |
| `root:test_install_claude_upgrade` | 120.33 | 112.93 | 3 | 15.10 / 94.09 / 2.31 | 13.4% |
| `codex:tests.test_host_commands` | 119.56 | 99.54 | 3 | 5.25 / 91.92 / 1.34 | 5.3% |
| `root:test_install` | 110.14 | 98.61 | 3 | 11.17 / 84.72 / 1.29 | 11.3% |
| `codex:tests.test_lost_run_settlement` | 98.64 | 100.03 | 4 | 10.05 / 82.09 / 5.89 | 10.1% |
| `codex:tests.test_learning_cli` | 95.44 | 85.21 | 3 | 10.05 / 73.85 / 0.41 | 11.8% |
| `codex:tests.test_trial_run_driver_bugs` | 70.22 | 73.37 | 2 | 7.96 / 58.37 / 6.19 | 10.8% |
| `codex:tests.test_container_stop` | 67.62 | 63.23 | 1 | 0.05 / 61.77 / 0.48 | 0.1% |
| `codex:tests.test_run_network` | 63.28 | 55.75 | 2 | 0.02 / 47.43 / 6.78 | 0.0% |

All time cells below are **call count / measured seconds**. Process and git columns are audit counts.

| Module | Process / git events | Git run calls / s | Sleep calls / s | Condition waits / s | Select calls / s | fsync + fullfsync calls / s |
|---|---:|---|---|---|---|---|
| `codex:tests.test_project_driver` | 7288 / 3252 | 1723 / 20.59 | 78906 / 312.51 | 74391 / 4599.11 | 0 / 0.00 | 2723 / 0.16 |
| `codex:tests.test_bridge` | 7838 / 2684 | 1317 / 15.77 | 15601 / 98.85 | 12334 / 724.45 | 0 / 0.00 | 1693 / 0.05 |
| `root:test_setup_wizard_direct` | 4685 / 2075 | 1827 / 22.68 | 9393 / 219.87 | 929 / 31.57 | 0 / 0.00 | 46 / 0.00 |
| `root:test_setup_straight` | 5703 / 2570 | 2236 / 22.52 | 5135 / 118.79 | 339 / 8.05 | 0 / 0.00 | 36 / 0.00 |
| `codex:tests.test_write_merge` | 11290 / 10364 | 1265 / 24.09 | 409 / 4.27 | 6 / 1.05 | 0 / 0.00 | 3 / 0.01 |
| `root:test_setup_sh` | 331 / 84 | 84 / 1.56 | 5 / 0.15 | 0 / 0.00 | 1636 / 105.68 | 0 / 0.00 |
| `root:test_setup_update` | 3703 / 1664 | 1452 / 14.09 | 3154 / 73.17 | 216 / 4.95 | 0 / 0.00 | 32 / 0.00 |
| `root:test_install_claude_upgrade` | 1752 / 689 | 595 / 6.57 | 13 / 0.02 | 0 / 0.00 | 0 / 0.00 | 6 / 0.00 |
| `codex:tests.test_host_commands` | 553 / 13 | 12 / 1.20 | 62 / 1.18 | 13 / 0.07 | 0 / 0.00 | 4369 / 0.17 |
| `root:test_install` | 2302 / 1058 | 942 / 13.06 | 21 / 0.03 | 0 / 0.00 | 0 / 0.00 | 265 / 0.01 |
| `codex:tests.test_lost_run_settlement` | 1782 / 749 | 341 / 3.86 | 18829 / 92.56 | 19457 / 1047.62 | 0 / 0.00 | 715 / 0.03 |
| `codex:tests.test_learning_cli` | 407 / 31 | 31 / 0.44 | 782 / 12.54 | 281 / 8.78 | 0 / 0.00 | 394 / 0.01 |
| `codex:tests.test_trial_run_driver_bugs` | 1810 / 739 | 323 / 3.54 | 13384 / 56.23 | 13590 / 797.27 | 0 / 0.00 | 651 / 0.03 |
| `codex:tests.test_container_stop` | 441 / 0 | 0 / 0.00 | 1182 / 45.32 | 60 / 0.15 | 29 / 10.44 | 440 / 0.01 |
| `codex:tests.test_run_network` | 276 / 0 | 0 / 0.00 | 260 / 4.16 | 86 / 1.16 | 239 / 44.54 | 1 / 0.00 |

## Where each module spends its time

These are measured observations and source explanations, not replacement coverage or permission to remove assertions.

| Module suffix | Interpretation / next narrow investigation |
|---|---|
| `test_project_driver` | 322.21 s in bodies; 43.26 s setup. Thousands of git/child launches plus overlapping service waits. Profile main child communication: 131.37 s. Inspect immutable fixture seeds and decision-only paths; retain real launch/concurrency checks. |
| `test_bridge` | 244.88 s body, 33.07 s setup; 7,838 launch events. Process-tree and bridge contracts need real children, but parsing/refusal decisions can be reviewed for plain-function proofs. |
| `test_setup_wizard_direct` | Only 2.72 s setup, 232.62 s body. Already an in-process driver. 219.87 s observed sleep largely waits for wizard work; child communication is 159.75 s and overlaps waits. Fixed settle calls total only 2.23 s. |
| `test_setup_straight` | 1.17 s setup, 235.23 s body; 5,703 launch events, 172.59 s main child communication. Prepared setUp repos alone cannot remove this cost: setup/install work happens in the bodies. |
| `test_write_merge` | 196.03 s body; 11,290 launch audit events, including 10,364 git events. Repeated fault/kill fixtures dominate. This is the legacy write_merge module, NOT the new merge-library tests on the other branch. |
| `test_setup_sh` | 151.20 s body with 105.68 s select waits on terminal output. Keep real PTY/terminal boundaries; this is not a candidate to globally replace select with a fake. |
| `test_setup_update` | 0.99 s setup, 136.50 s body; 102.87 s main child communication. Repeated real installer work within in-process flows is more promising than fixture micro-optimization. |
| `test_install_claude_upgrade` | 15.10 s setup (13.4%), 94.09 s body. installed_base already caches per worker and copies its snapshot. Investigate repeated seed construction across three fragments, not a claim that every test reinstalls in setUp. |
| `test_host_commands` | 91.92 s body, 71.69 s main child communication. Its run_cli already has an in-process branch; retain protected-copy/path/entry-point proofs and identify remaining value-only checks before moving any. |
| `test_install` | 11.17 s setup (11.3%), 84.72 s body; 2,302 launches and 1,058 git events. Prepared immutable repo inputs meet the >10% setup screen here, but full installer behavior remains real. |
| `test_lost_run_settlement` | 82.09 s body and 10.05 s setup. Large aggregate service waits overlap concurrent work. Real killed-host recovery is the requirement; move only decision arithmetic when an equivalent statement is identified. |
| `test_learning_cli` | 10.05 s setup (11.8%), 73.85 s body; 59.18 s main child communication. Setup does git init, owner-key and provider enrollment. Review reusable immutable inputs while keeping keys/homes and service state isolated. |
| `test_trial_run_driver_bugs` | 58.37 s body, 7.96 s setup (10.8%), 6.19 s cleanup. Reuses DriverHarness; improvements to its fixture may apply, but its regression outcomes stay exact. |
| `test_container_stop` | 61.77 s body; 45.32 s sleep, 10.44 s select, 18.52 s main subprocess wait (overlapping). Separate deadline/failure arithmetic from real watchdog/process-lifetime checks. |
| `test_run_network` | 47.43 s body, 6.78 s cleanup; 44.54 s select waits dominate. Retain actual containment/socket proofs; inspect redundant idle waiting and teardown notification first. |

## Four cheapest candidates, in order of change scope

These are proposals for requirements-first work in later steps. No assertion changes are authorized by
this table and no numeric savings are promised. Tests are deleted only after a named start-to-finish check.

1. **Signal wizard readiness instead of repeated polling.** `tests/wizard_direct_driver.py:394` and `:408`
   poll at 20 ms. Preserve every expectation and deadline, and use the existing output/queue seam to signal
   new output. The three in-process modules recorded 412 s of sleep, but much overlaps useful work; their
   settle calls total just **3.63 s**, so this is a small mechanical win to measure, not a 412 s saving.
2. **Reuse immutable fixture inputs only where setup is material.** Project driver (43.26 s), bridge
   (33.07 s), install upgrade (15.10 s), install (11.17 s), learning CLI (10.05 s), and trial-driver bugs
   (7.96 s) each exceed 10% setup in this pass; lost-run settlement is just over 10% (10.05 s).
   Use isolated copies, not shared running services, mutable history, owner identity or sockets.
   `tests/test_install.py:179` already caches installed_base per worker; first inspect duplicated seed work
   across fragments. The wizard modules' 0.5–1.1% setup shares rule out promising large setUp savings there.
3. **Move remaining CLI value/format decisions to plain calls.** `test_host_commands` spends 71.69 s in main
   child communication; `packages/codex-agents/tests/test_host_commands.py:920` already demonstrates an
   in-process path. Requirement-by-requirement, keep the same expected values; keep at least one real call
   per entry point and all protected-copy/security/process-boundary cases. The 71.69 s is an upper scope of
   investigation, not recoverable time or a reason to remove those checks.
4. **Use controllable time for decision-only stop/network cases.** Observed waits are substantial in
   container_stop and run_network. Existing stop tests already contain fake-clock seams
   (`packages/codex-agents/tests/test_container_stop.py:1358`). Extend only to arithmetic/state decisions;
   keep real deadline, kill, watchdog and containment evidence. This needs requirement/test review before
   refactoring, and readiness/teardown notification changes must retain all assertions.

The larger, already-approved merge work is separate: `test_write_merge` repeatedly creates git/fault/kill
fixtures. Its Workspace deliberately suppresses normal macOS F_FULLFSYNC (`:152`), retaining specific
real-flush tests, so the tiny observed flush cost is NOT a durability proof. Step 3's durable/volatile model,
one table checker, normal sample and complete soak are the planned remedy; do not remove real flushes or
weaken kill/fault assertions for speed. The new merge-library branch was not rebased or measured here.

## Handoff and later measurements

- Step 0 assertion ledger: **none removed, changed or weakened; no tests deleted**.
- No production/runner/test files changed; only this report and measurement artifacts are added.
- Full-suite failure left open: 80 lifecycle requirements with no check, intentionally addressed in step 4.
- Observability limits (descendant processes/native SQLite flushes), existing skips, Python 3.11 and Linux
  remain explicit gaps. Use appropriate real-OS evidence later; this capture does not prove power-loss safety.
- There is no before/after optimization comparison yet. Append an entry after EACH step-5 module with
  timing-file job-seconds, named replacement checks, assertion file:line replacements and retained real runs.

## Step-1 r1 baseline budget census (before reconciliation)

Source: `baseline-timings.json`, one complete module total per declared baseline module.

| Layer | Budget job-s | Baseline modules | Over budget |
|---|---:|---:|---:|
| unit | 5 | 76 | 0 |
| component | 30 | 0 | 0 |
| crash | 60 | 0 | 0 |
| scenario | 60 | 212 | 16 |
| soak | 900 | 0 | 0 |

Option A records these modules and their original ceilings in `scripts/test-budget-debt.json`.
Every remaining entry is named even in filtered runs; only a complete under-budget result permits retirement.
No new measurements or speedups are implied by this census.

## Step 3 — affected merge validation measurements

All values below come from the saved worker timing records in the merge worktree's
`measurements/test-framework/` directory. These are affected-module runs, not a new full-suite measurement.
The normal inventory changed under TF-18 and complete enumeration moved to explicit soak; these runs do
not establish a like-for-like full-sweep speedup. Step-5 heaviest-module pruning has not started.

| Scope | Tests | Job-seconds | Evidence / result |
|---|---:|---:|---|
| Eight unaffected merge modules, default 16 jobs | 204 | 77.310938334 | `merge-review-final.run.json`; all workers pass, no budget failure |
| Explicit unaffected adversarial subset | 77 | 55.348261750 | `merge-unblocked-final.json`; pass, 3 existing image skips; one C-3 test not run |
| Explicit unaffected model subset | 4 | 8.190793250 | `merge-unblocked-final.json`; pass; undo C-3 test not run |
| Last complete model sample module | 5 | 9.710025250 | `merge-model-sweep-third.run.json`; 16 nested-folder subcases red in one test |
| Portable replay and four mutation proofs | 16 | 1.527262709 | `merge-portable-green.run.json`; pass |
| Invalid ci/complete-soak selection | 0 | 0 | `merge-soak-refusal.run.json`; expected preflight refusal |

The two explicitly partial rows do not retire module budget debt or certify a complete module. The current
strict nested-folder requirement conflict is described in `../cocolo-mergelib/CONTRADICTIONS-merge-checker.md`.
Normal setup copies a disposable prepared repository, replay-key discovery is linear, and child exit polling
uses a bounded 1ms wait. Full persistence-kind coverage is in the model sample inventory; real process samples
retain OS boundary checks. Exact assertions and their complete-soak replacements are listed in the merge ledger.

## Step-2 r1 review measurement

Default 16 jobs, five affected modules, 94 passing tests: **20.502791041042656 job-seconds**. Discovery 16.54204099997878s; preflight 0.6366840830305591s; trusted history validation 0.201113874965813s (inside preflight, not added to job-seconds). History validation is below even the 5s unit budget; no cache is justified. The new deterministic-value/source baseline makes discovery more expensive; it is recorded separately and is not hidden as worker time. No full suite was rerun. All baseline debt entries remain warned; unselected modules are explicitly unmeasured.

Cross-worktree integration exposed 522 root-dependent value hashes. The portable baseline preserves all 5683 IDs and the original immutable baselines, with 306 original source files verified against commit 438e1af1. It normalizes only worktree-root path prefixes; the relocation witness proves equality across roots and inequality after a real expected-constant edit.

| R1 portable baseline verification | Tests | Worker job-seconds | Discovery seconds | Preflight seconds |
|---|---:|---:|---:|---:|
| Five affected modules, portable fingerprints | 95 | 19.20834891695995 | 44.57896899996558 | 0.6031110000330955 |
| After resolving root once per fingerprint, affected core module | 44 | 0.7585082500008866 | 15.707939458952751 | 0.5907899580779485 |

Both runs have no worker/budget failures and exactly 80 missing step-4 requirements. The latter validates every immutable legacy fingerprint during discovery, then executes only the affected core module. History validation is 0.2059470409876667s in the latter run. Neither original nor portable baseline was regenerated.

## Step-3 r1 final affected-module measurements

All commands used scripts/parallel_tests.py at its default 16 jobs. The ten small merge modules passed 242 tests in **76.9704737511347 job-seconds**. The final checker/model/mutation follow-up passed 75 tests in **34.64630295801908 job-seconds**. No full suite was rerun.

| Real campaign, separately invoked | Checks | Job-seconds | 60s module budget |
|---|---:|---:|---|
| adversarial, including original image opt-ins | 49 (3 skipped) | 45.18175304098986 | pass |
| owners | 20 | 21.68256016698433 | pass |
| kills | 5 | 31.07644525001524 | pass |
| forward_faults | 1 | 30.46376620803494 | pass |
| recovery_faults | 3 | 18.176061125006527 | pass |

Separate runs total **146.5805857910309 job-seconds**. The complete 78-check census is asserted (75 adversarial methods plus three image methods); the split changes scheduling only, preserving every method and assertion. Normal fault inventories now cover every observed persistence operation/phase/side/commit signature on both real and model lanes. Full inventories remain explicit soak; no normal result certifies soak.

The earlier five-campaign concurrent measurement was **475.9808796250145 job-seconds**, with all test bodies passing but all five module budgets failing under filesystem contention. This is an outstanding performance gate, not cleared by the separate measurements. No ceiling was increased and no fault kind removed. The original unsplit expanded campaign was **139.0616121669882 job-seconds** and over its 60s module budget. The final results therefore establish functional coverage and isolated module budgets, not a concurrent full-suite budget pass. Raw evidence and digests are in the merge branch's measurements/test-framework/review-r1-merge-evidence.json.

## Round-2 scoped verification

All rows use the runner's DEFAULT 16 jobs; no full suite was repeated. Job-seconds come from each `--save-timings` `.run.json`, retained as digest-bound extracts in `measurements/test-framework/review-r2-framework/runs.json` (merge evidence in the merge worktree's `review-r2-merge/runs.json`).

| Run | Checks | Job-seconds | Result |
|---|---:|---:|---|
| Framework intended red | 93 | 19.983886332949623 | Intended provenance/plan/gate/environment failures; exact logs saved |
| Framework first six-module green | 103 | 21.244645251019392 | All workers pass |
| Framework final four-module green | 95 | 20.520382499031257 | All workers pass |
| Final guard assertion-preservation follow-up | 37 | 19.76074541604612 | All workers pass |
| Merge integrated eleven-module green | 253 | 90.15853699890431 | All workers pass, including four mutation proofs |
| Separate real crash campaign | 49 | 45.08007408300182 | 46 pass, three existing mounted-image opt-ins skipped |

No budget failures in these green runs. Overall exit remains 1 for exactly 80 lifecycle metadata failures. This is not a new end-to-end performance claim; concurrent real-campaign budgets remain an open gate. No old timing ceilings were increased or debt retired by this pass.

## Round-3 scoped verification

Saved `--save-timings` `.run.json` records, DEFAULT jobs throughout; only affected modules ran. No full-suite performance claim or budget increase.

| Run | Checks / job-seconds / jobs |
|---|---|
| Exact-base framework red | 10 checks; 5.5558232920011505 job-seconds; DEFAULT 16 jobs |
| Final framework green | 115 checks; 26.99471895804163 job-seconds; DEFAULT 16 jobs |
| Exact-base merge red | 7 checks; 1.983410916000139 job-seconds; DEFAULT 16 jobs |
| Integrated merge green | 270 checks; 112.91237787512364 job-seconds; DEFAULT 16 jobs |
| Separate real crash campaign | 49 checks; 44.652967874950264 job-seconds; DEFAULT 16 jobs |

Green workers pass with no budget failures; real campaign retains three image opt-in skips. Overall exit remains 1 for 80 lifecycle metadata errors. Concurrent real-campaign performance remains open. The immutable dependency migration changes 4,419 fingerprints with zero unresolved entries and unchanged 5,683 legacy IDs.

## First integrated full-run corrections

Primary's supplied report: 410.8 s wall, 6217.4 job-seconds, default 16 jobs, 394 units / 6186 tests; no timing file was saved. Exact supplied module totals are preserved in `measurements/test-framework/release-0850/primary-full-summary.json`. Do not treat these as a newly measured local run.

Affected framework: 31.580453 job-seconds; final source binding and integration witnesses: 0.794242. Affected merge campaigns: 161.515750 s wall / 161.490655 job-seconds, 16 jobs, every module below 60 s. Details and unchanged-assertion accounting: REVIEW-test-framework-fullrun-fixes.md. Full integrated measurement follows after the single full run.

Single integrated full run (`f69da048`): **496.778018 s elapsed / 4535.410045 job-seconds**, default 16 jobs, 395 units / 6193 tests. Zero budget failures. Every merge-library module below 60 s (largest adversarial 44.722 s). Four new debt entries retired; final debt list and every failed unit/cause are in REVIEW-test-framework-fullrun-fixes.md. This is the only full run; subsequent bookkeeping validation uses affected modules only. Original full-run diagnostics remain preserved.
