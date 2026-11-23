# Installed-release restoration and routine bookkeeping

TF-25 requirements were committed first (`24b5cc0b`), then failing witnesses (`4a261f41`). Restoration `87d78e55` copies all three files exactly from primary `30bf1fa9` (same requested 0ca9c36f versions). The installed 0.8.49 record is retained, the packaging pin annotation restored, and both catalogue-count assertions are 10. All older records and every refusal/size assertion remain unchanged.

The restored file contains 14 test methods. The old runner emits 18 catalogue diagnostics: two implementation, two helper-dependency and fourteen value/source mismatches. The first metadata-red artifact also records one temporary TF-25 catalogue-text synchronization error, which is not evidence for the bookkeeping defect. No diagnostic is silently counted as a failed test.

New immutable version: `scripts/test-release-bindings/0.8.49.json` (introduction `9e095c4c`), regenerated from the restored files. It replaces only the 14 affected catalogue bindings; the old 0.8.50 snapshot from 11d36a06 and every earlier immutable baseline are untouched. The runner verifies the permitted transition against trusted Git sources, checks the admitted source chain and current three-file hashes, then performs ordinary test/helper/value comparisons. Retired IDs cannot be restored. Source equality and matching existing proofs authorize use across the two worktrees; mere Git ancestry is neither the admission proof nor a substitute for it.

| Fix / TF-25 boundary | Enforcement | Witness | Red → green / control |
|---|---|---|---|
| exact_primary_restoration_matches_installed_release | `packages/claude-agents/tests/test_previous_release_catalogue.py:88` | `tests/test_release_bookkeeping.py:25` | Exact c75eba51 replay → green; The trusted record names installed 0.8.49 and all three current files equal primary 30bf1fa9 byte for byte. |
| correct_append_passes_and_every_unrelated_edit_is_rejected | `scripts/rebind_release_bookkeeping.py:35` | `tests/test_release_bookkeeping.py:32` | Exact c75eba51 replay → green; Exact primary transition passes before and after fourteen independent catalogue, count, helper and packaging mutations are rejected. |
| rebinding_retains_retirement_and_catches_later_fingerprint_edits | `scripts/rebind_release_bookkeeping.py:189` | `tests/test_release_bookkeeping.py:55` | Exact c75eba51 replay → green; Admitted fingerprints pass; retired IDs stay absent; a later fingerprint edit fails the normal gate. |
| rebind_command_rejects_invalid_source_and_overwrite | `scripts/rebind_release_bookkeeping.py:201` | `tests/test_release_bookkeeping.py:71` | Exact c75eba51 replay → green; Authentic source reproduces the exact record; actual CLI refuses overwrite without changing bytes and wrong version without creating a file. |
| claimed_pre_bump_commit_cannot_hide_old_record_edits | `scripts/rebind_release_bookkeeping.py:148` | `tests/test_release_bookkeeping.py:90` | Exact c75eba51 replay → green; Unchanged admitted source/proof passes; a forged old record in both claimed revisions is rejected. |
| later_edits_to_each_admitted_file_still_fail_metadata | `scripts/rebind_release_bookkeeping.py:184` | `tests/test_release_bookkeeping.py:107` | Exact c75eba51 replay → green; Unmodified committed binding passes before/after each independent current-file edit is refused. |
| next_release_uses_the_same_narrow_procedure | `scripts/rebind_release_bookkeeping.py:144` | `tests/test_release_bookkeeping.py:121` | Exact c75eba51 replay → green; The subsequent 0.8.50 append/count/annotation rebinds the identical ID set using the same procedure. |
| immutable_binding_tampering_is_rejected | `scripts/rebind_release_bookkeeping.py:179` | `tests/test_release_bookkeeping.py:139` | Exact c75eba51 replay → green; Committed record passes; a changed stored fingerprint is refused; original still passes. |

TF-23 evidence declarations: `scripts/test-review-witnesses-release-bookkeeping.json`; records and raw logs under `measurements/test-framework/release-bookkeeping/`. Exact final-witness old-code replay: **0.165009 job-seconds**, eight intended failed witnesses. Affected green: **10.371962 job-seconds**, default 16 jobs, 32 tests, no failed worker, exactly 80 lifecycle metadata errors and no other metadata errors.

Supplemental bad-acceptance controls: the unanchored implementation accepted a forged record already present in the claimed before revision (`anchor-red.run.json`, 1.030097 job-seconds, `ValueError not raised`); after the anchor correction, a valid binding still accepted later edits to all three files (`postbind-red.run.json`, 2.834716 job-seconds, three intended `ValueError not raised` failures). That intermediate run includes the anchor correction and is not represented as an exact c75eba51 replay. The final green runs execute the valid controls and reject all of these mutations.

The new invalid-source CLI control originally used an obsolete pre-bump state and was correctly rejected at that earlier gate. Its setup now uses admitted 9e095c4c for that case; all assertions remain unchanged. Original test assertion changes are only the two owner-directed 9 → 10 restorations, mapped to requirement TF-25 in ASSERTIONS-test-framework.md. No tests deleted, limits lowered or refusal assertions weakened.

Procedure and exact command are in TESTING-framework.md. Routine bumps append one source-verified record, change the two matching counts and canonical annotations, then run `python -B scripts/rebind_release_bookkeeping.py --from <trusted-pre-bump> --source <previous-installed-source> --previous-release <version>` using the prepared pinned environment. The command refuses overwrite and creates a new immutable release-named file. New executable pins or unrelated changes require ordinary proof review.

The authorized full run and its four reporting classes follow below. No installs, pushes, control-project edits or general cleanup were performed.

## Single authorized full run

Revision `65ca8c20` on feat/merge-library; command in `measurements/test-framework/release-bookkeeping/full-provenance.json`. Exact trusted base `b531844030fa619c1f62ddd6c092ebc2ea4fca38`; prepared hash-pinned environment, Python 3.14.7/macOS; **16 default jobs, 509.456003 s elapsed, 4825.323536 job-seconds, 396 units, 6201 tests**. Flat timings: `full-timings.json`; full structured record: `full.run.json` in the same evidence directory. Exit 1 is preserved. Exactly one full suite was run; subsequent runs select only the affected framework module.

### 1. Failed units — one unexpected failure remains open

| Unit | Cause / disposition |
|---|---|
| `root:test_e2e_requirements` | Expected: 80 missing step-4 lifecycle checks. |
| `root:test_test_framework_guards` | Unexpected: `RunnerIntegrationTests.test_review_B3_runner_reaps_disabled_watchdog_with_diagnostics` received zero nested worker records and raised `IndexError` at original line 466. Its nested runner output was captured but not printed, so the reason for zero workers is not recoverable from the saved full-run log. No cause is asserted without evidence. |

The latter unit passes in an affected rerun (**19.177335 job-seconds**) with every original assertion intact and one additional cardinality assertion that includes the captured nested runner output on failure. Fifty identical probes also pass on the framework worktree (**49.164339 job-seconds**) and the actual merge-library worktree (**49.380317 job-seconds**). Both repeated runs retain every timeout, diagnostic-content and retained-file assertion. The temporary repeat harness is preserved as `watchdog-repeat-source.txt`; final source runs the original single probe plus the diagnostic assertion. These are diagnostic observations, **not proof that the unexplained full-run failure is fixed**. The new assertion is `tests/test_test_framework_guards.py:466`; existing assertions are now lines 467–469. Old-code failure, positive control and green evidence are in the release witness manifest and `watchdog-*.json`. No timeout or budget was raised, no guard changed, and no existing assertion removed.

**Readiness: not ready for merge under the owner's full-run criterion.** Both requested release changes are implemented and tested, but the unexpected full-run unit remains an open blocker. A future occurrence must retain the nested runner's now-exposed diagnostic before a requirement-based correction can be made. No second full run or speculative timing relaxation was performed.

### 2. Deferred lifecycle checks

Exactly **80** metadata errors, all for the deferred lifecycle requirements; **zero other metadata errors**. The same 80 missing checks cause the expected e2e worker failure. Step 4 closes them; it was not started in this pass.

### 3. Not-run obligations (separate from failed units)

`full-outstanding-obligations.json` names all 52 outstanding check IDs, their requirements, raw status, reason and closing step/lane. None is counted as passed evidence.

| Obligations | Reason | What closes them |
|---|---|---|
| `review:TF-5`, `review:TF-6`, `review:TF-19`, `review:TF-22` | Registered review records were not supplied to this invocation. | Attach the cited owner/CoCoLo review evidence records; a narrative claim is insufficient. |
| `review:TF-21` | Step-5 pruning review is outside this pass. | Complete step 5 and its review. |
| `planned:TF-19` | Step-3 acceptance placeholder remains registered. | Replace it with an executed, cited acceptance record. |
| `planned:TF-20` | Step-4 lifecycle acceptance is deferred. | Complete step 4. |
| Complete model/real soak campaigns (ML-5/6/10/11/12/13/18/19; TF-18/19) | Normal `ci` profile intentionally omits the complete sweep. | Execute the `soak` profile; all exact check IDs are in the obligations artifact. |
| Three `RealImages` checks (ML-4/12) | Opt-in real filesystem/disk-image fixture was unavailable. | Execute the mounted-filesystem/image lane. Raw unittest status remains `skipped` and strict requirement coverage remains non-passing; reporting these missing executions separately does not convert them to passes. |

Python 3.11/Linux execution remains external platform evidence; this macOS/Python 3.14.7 run does not prove it.

### 4. Budget debt

**Zero budget failures, zero retirement obligations.** All new merge-library modules remain under 60 seconds (largest: adversarial 43.139 s). The same 14 existing-module debts remain, with unchanged ceilings and no new admissions:

| Module | Ceiling seconds | Full-run seconds |
|---|---:|---:|
| `codex:tests.test_bridge` | 299.500 | 279.625 |
| `codex:tests.test_container_stop` | 67.620 | 65.655 |
| `codex:tests.test_host_commands` | 119.560 | 110.853 |
| `codex:tests.test_learning_cli` | 95.440 | 90.050 |
| `codex:tests.test_lost_run_settlement` | 98.640 | 94.260 |
| `codex:tests.test_project_driver` | 390.320 | 348.637 |
| `codex:tests.test_trial_run_driver_bugs` | 70.220 | 63.635 |
| `codex:tests.test_write_merge` | 241.240 | 221.280 |
| `root:test_install` | 110.140 | 106.016 |
| `root:test_install_claude_upgrade` | 120.330 | 115.349 |
| `root:test_setup_sh` | 177.880 | 171.686 |
| `root:test_setup_straight` | 264.440 | 252.260 |
| `root:test_setup_update` | 167.880 | 160.387 |
| `root:test_setup_wizard_direct` | 265.860 | 260.659 |

Old immutable baseline files and the 11d36a06 snapshot are unchanged. The three restored product files remain byte-identical to primary 30bf1fa9. The only permanent original-test assertion changes are the two owner-directed count restorations; the framework watchdog gets an additive diagnostic assertion. Assertions ledger and the TF-23 table preserve the distinction between the completed release fixes and the unresolved full-run incident.

Final affected TF-23 evidence-manifest validation: 4.857699 job-seconds, default 16 jobs, 10 tests; zero failed workers, exactly 80 deferred lifecycle metadata errors, zero budget failures. This validates evidence records and does not close the unexplained full-run failure.
