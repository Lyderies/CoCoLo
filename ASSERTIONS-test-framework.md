# Assertion replacement ledger

## Step-2 review reconciliation: owner-approved entry-point probe corrections

TF-13 requires a real process boundary, not a zero exit for --help. Two new probe cases
incorrectly assumed argparse's SystemExit bypasses the hosts' mandatory exit fence. Work
stopped on those cases, the exact conflict was raised, and the owner explicitly selected
the named start-to-finish replacements. No shipped behavior or existing assertion changed.

| Removed assertion instance | Requirement | Replacement, unchanged |
|---|---|---|
| `tests/test_entrypoint_inventory.py:45` returncode == 0, collector_host case; `:46` usage output | TF-13/16 cold process boundary | `packages/codex-agents/tests/test_collector_exit.py:425` test_C10_a_shutdown_request_is_recorded: starts the actual collector, requests shutdown, observes exit and exact cause/steps |
| `tests/test_entrypoint_inventory.py:45` returncode == 0, project_host case; `:46` usage output | TF-13/16 cold process boundary | `packages/codex-agents/tests/test_project_service_startup.py:80` test_cli_starts_one_host_reuses_it_and_recovers_after_it_stops: starts actual host, checks active/paused/active states, reuse and restart |

No test function was deleted. The manifest moves these two cases to the named checks;
the cold-help assertions remain unchanged for entry points with that interface.
Evidence: `measurements/test-framework/review-replacement-run.json`, 46 tests, no failed workers.
The first attempt encountered the execution sandbox's Unix-socket bind denial; the authorized
rerun used actual local sockets. Neither the refusal nor its failed assertions was treated as passing.

## Step-2 r1 assertion replacements and fixture corrections

- Reviewed `tests/test_test_framework.py:266` exact `max_examples == 1000` for soak -> `tests/test_test_framework.py:265` `>= 1000`, exactly TF-17; dev/ci exact-value checks remain.
- The newly added default-profile witness compared distinct Hypothesis settings objects by identity/equality; it now checks default ci example count, derandomization and deadline fields at `tests/test_test_framework.py:272`. No production assertion was removed.
- The new fail-closed runner fixture now uses its own small root at `tests/test_test_framework_guards.py:454`; both nonzero-exit and trusted-base diagnostic assertions remain, with a stricter 20s outer bound. No validation method is replaced in this probe.
- Process denial markers are read before temporary-root teardown; caught-denial probes now catch Exception and assert reached-marker, nonzero exit and recorded violations. Sidecar-symlink rejection now also checks original golden and symlink. These strengthen existing assertions.
- No existing test function was deleted. All other reviewed assertion expressions remain; the two earlier host help-probe replacements above remain the named start-to-finish checks.

## Round-2 assertion replacements (decision 99 A)

No test function was deleted. All pre-pass expressions are retained or explicitly replaced below. New tests and added assertions do not replace unrelated checks.

| Before at `8cfab050` | Replacement in this worktree | Requirement / reason |
|---|---|---|
| `tests/test_test_framework.py:463` generic truthy dependency errors in all three pin cases | `tests/test_test_framework.py:467`; zero-error controls at :463/:470 and missing-root exclusion at :468 | TF-15/M5: one invalid pin/hash at a time; reject for its intended reason. |
| `tests/test_test_framework_review.py:67` wrong revision against a missing, unhashed record | `tests/test_test_framework_review.py:74`; valid digest-backed controls at :72/:76 | TF-4/N2: only revision changes; missing-record rejection remains :78. |
| `tests/test_test_framework_guards.py:196` asyncio.run(open_connection), with generic assertions :202–204 | `tests/test_test_framework_guards.py:203`, reached/network/violation/no-side-effect assertions at :208–212 | TF-11/M6: prebuilt loop reaches actual open_connection; original nonzero/violation/fixture.sock checks retained. |
| Same file :197 asyncio.run(start_server), same :202–204 assertions | Same replacement loop :203, assertions :208–212 | TF-11/M6: separate start_server case in both restricted layers, same checks retained. |
| Same file :451 non-passing coverage for potentially absent expected-failure ID | `tests/test_test_framework_guards.py:469`, exact status next lines, original non-passing coverage retained | TF-5/M8: both expected_failure and unexpected_success must actually execute. |
| Same file :497/:501 embedded elapsed assertions in expected-failing workers | `tests/test_test_framework_guards.py:520` and `tests/test_test_framework_guards.py:524`, after unchanged embedded <0.5s assertions | TF-12/N3: outer checks prove elapsed assertions completed. |

M3 changes only the budget fixture input at `tests/test_test_framework.py:289` to supply the authoritative fragment plan; every expected value/assertion is unchanged (CONTRADICTIONS-review-r2.md). M7 retains the old inventory test and all descendant-stop assertions, adds real runner checks at `tests/test_test_framework_guards.py:542` and external normal/interrupted elapsed assertions at `tests/test_test_framework_guards.py:397` / `tests/test_test_framework_guards.py:402`. No historical LOW assertion-ledger omissions were repaired in this scoped pass; L16 remains listed.

## Round-3 assertion replacements

No test was deleted. No expected outcome was relaxed.

| Before at 9c18c4a3 | Current replacement | Reason |
|---|---|---|
| `tests/test_test_framework.py:429` [] for a timed-out row with no plan | `tests/test_test_framework.py:431` | Same expected [], with authoritative plan and independently passing clean-row retirement control. |
| Same file :431 [] for failed returncode with no plan | `tests/test_test_framework.py:432` | Same expected []; only returncode differs from the clean control. Absent-plan rejection remains in the r2 tests. |
| `tests/test_test_framework_guards.py:111` nonzero exit on asyncio.run(open_unix_connection), :112 socket output | `tests/test_test_framework_guards.py:117` and next line | Both original assertions retained across both Unix APIs and both layers, now with reached/recorded-denial/no-socketpair/no-file assertions. |
| Same file :551 outcomes dictionary before cardinality assertions :555–556 | `tests/test_test_framework_guards.py:560` | Original union/cardinality/status assertions remain; new checks count and validate original worker records first. |

Golden positive fixture at `tests/test_test_framework.py:196` adds only required provenance; all original assertions are unchanged (CONTRADICTIONS-review-r3.md). The PR merge-base fixture at `tests/test_test_framework_guards.py:504` adds only actual event context; all five original assertions remain. The defaults witness fixture was corrected before implementation to mutate only __defaults__/__kwdefaults__; its inequality and behavior assertions remain. New subtests preserve both timeout/returncode and both Unix API failures independently. Historical LOW omissions are deliberately left as listed known gaps.

## First full-suite integration
- New witness contradiction: `ReleaseIntegration.test_existing_resource_modules_have_correct_layers_and_reasons` initially asserted `root:test_test_stages` was unit. TF-24 requires layer matching actual resource use; that existing discovery test launches processes and is already scenario. Stop recorded before implementation. Replace only this erroneous positive control with the existing plain unit `root:test_check_identifiers` at `tests/test_test_framework_integration.py:40`; all five reclassification assertions retained. No original test or assertion changed.
- New command witness incorrectly assumed exactly three CI base selectors; the existing PR e2e job also has one. TF-24 requires every CI selector's trusted base. Replace the count-3 assertion at `tests/test_test_framework_integration.py:104` with nonempty selectors and equality for **every** selector at lines 106–108. No selector is omitted.
- `tests/test_test_framework_integration.py:109`: the no-base control now traps `inventory()` rather than all `metadata()` processing. The same return-code and no-discovery assertions remain; reading the trusted-base policy before inventory is permitted by TF-24. Existing fixture tests' explicitly injected trust policies remain supported.
- Full-run retirement contradiction: `ReleaseIntegration.test_versioned_debt_has_measured_ceilings_and_cannot_grow` assumes all four newly admitted debts stay in the mutable current list. TF-8/24 and decision 86 instead require retiring a complete under-budget module. The full run proves all four qualify. Stop that witness, record its red on the valid retired manifest, then retain its four admission-ceiling assertions against the immutable introduction and add validation of the shrunk current list against that ceiling. No existing product assertion changes.
- Retirement witness replacement: `tests/test_test_framework_integration.py:86` reads the immutable admission ceiling; all four measured-ceiling assertions stay at line 92. New line 90 validates the current shrink-only list. Old red: `testfw-retirement-witness-red.json.run.json` (KeyError billing_gate, valid retired state; 0.576609 job-seconds). The earlier full run is the positive control with all four admitted entries present.

## Installed 0.8.49 catalogue restoration (TF-25)
- Restore the primary's exact `30bf1fa9` three-file state. `packages/claude-agents/tests/test_previous_release_catalogue.py:88` and `:96`: expected catalogue counts 9 → 10, preserving both equality assertions, the 2 MiB boundary and all refusal controls. The replacement requirement is the installed 0.8.49 record, not changed implementation behavior; this restoration was explicitly directed by the owner.
- New mutation setup uses the actual `_CATALOGUE_LIMIT` operand and asserts its presence; no fallback mutation may stand in for an executable-edit control. No assertion removed.
- Source inspection finds 14 test methods in both the requested primary file and the prior file (not 18). TF-25 binds the discovered set exactly and does not hard-code a count of test methods.
- New CLI invalid-source witness setup contradicted TF-25's admitted pre-bump state: after 0.8.49 is bound, c75eba51 contains the obsolete count-9 files. The command correctly rejected that anchor before checking the deliberately wrong version. Stop recorded on `ReleaseBookkeeping.test_rebind_command_rejects_invalid_source_and_overwrite`; replace only its invalid-source command's `--from` input with 9e095c4c (the admitted count-10 files), keeping all return-code, diagnostic, unchanged-bytes and no-new-file assertions. The overwrite case retains its original input. No assertion changed or removed.

## Release full-run diagnostic follow-up

`tests/test_test_framework_guards.py:466` adds `assertEqual(1, len(report['workers']), output)` before the existing timeout/log assertions (now lines 467–469). No existing assertion is removed, changed or weakened. The full-run old witness raised `IndexError` and lost the nested runner's captured preflight diagnostic; the additive assertion will include that output if zero workers recur. The 50-repeat diagnostic harness retains every assertion and only repeats the same case; it is preserved as measurement input and is not the shipped test. Final source runs the original single probe with the additive assertion. This improves future diagnosis, not proof that the unexplained full-run failure is fixed.

## TF-26 product dependency boundary

No existing assertion is removed, changed or weakened, and no test is deleted. `tests/test_product_dependencies.py` adds eight tests covering the product boundary, shared-helper/constant and assertion/rename protection, source ownership, symbolic defaults, immutable migration and release precedence. The protection controls pass on the old implementation as expected; six repair witnesses have intended exact-5ab0a9d3 reds and green evidence. Temporary RSA product edits and historical source replay are restored in `finally`; they change no product test assertion. All 5,739 baseline test IDs and implementation fingerprints remain identical across the new immutable dependency versions. See REVIEW-test-framework-product-dependencies-fixes.md for the enforcing/witness line table and evidence.

TF-26 full-run follow-up: `tests/test_test_framework_review.py:100` and its second helper assertion remain byte-identical to 5ab0a9d3. A ninth test, `ProductDependencies.test_fileless_generated_helpers_keep_owned_namespace_and_external_identity`, adds explicit generated-fixture and external-product controls. No original assertion is changed, removed or replaced. The generated-helper regression is corrected in code; its red base is 93624f19, with separate red/green evidence in the TF-26 report. RSA overlay tests, including the owner's expected-failure probe, are executed unchanged and then removed only from our temporary overlay; no RSA branch file is edited.

## TF-25b — Release-agnostic bookkeeping witnesses

Scope: `tests/test_release_bookkeeping.py` on `fix/tf25b-next-release`, based on `74f4d84ec5cfdba963ba0869fe0ea377cc1be01d` (the real 0.8.51 bookkeeping). No test is deleted. The rebind validator, three product bookkeeping files and all immutable binding files are unchanged. Synthetic release commits and CLI writes occur only in a disposable repository under the worker's temporary directory; the real worktree is never rebound by these witnesses.

Every replaced assertion instance below refers to `tests/test_release_bookkeeping.py` at `74f4d84e`. Method names identify the corresponding current assertion without depending on future line movement.

| Before | Replacement (TF-25b) |
|---|---|
| `test_exact_primary_restoration_matches_installed_release`, :27: last release equals literal `0.8.49` | Same equality against each admitted binding's `previous_release`; separately require binding filename/version agreement. |
| Same method, :29: exact bytes for each of the three files equal `30bf1fa9` | Same three byte equalities against the latest binding's immutable introduction; additionally verify every admission's before/after source hashes, chain continuity and compiled source record, and run `checked_bindings` over the entire chain. |
| `test_correct_append_passes_and_every_unrelated_edit_is_rejected`, :35: valid transition returns `[]` | Identical zero-error assertion in `check_transition_mutations`, called independently for every admitted transition. |
| Same method, :49: each executable/comment/size/count/helper mutation operand exists | Identical `assertIn` for each of the six operands, for every admission; the two count operands derive from that transition's catalogue size. |
| Same method, :51: every hostile mutation is rejected | Identical truthy-error assertion for all 14 named mutations, for every admission. Wrong-release mutation derives a distinct future version; old-record edit/removal/reordering, extra record, source/bundle/schema forgery, executable/comment/size/count/helper controls all remain. |
| Same method, :52: clean transition still returns `[]` after mutations | Identical post-mutation zero-error assertion for every admission. |
| `test_rebind_command_rejects_invalid_source_and_overwrite`, :73: wrong source version raises `source version` | Same exception type and exact diagnostic fragment in the temporary repo, using the latest admitted trusted source with a deliberately different version. |
| Same method, :76: trusted compiled record equals the installed `0.8.49` record | Same full-record equality for the latest admission and its trusted source. |
| Same method, :77: `write_new` rejects an existing immutable file | Same exception type/diagnostic and same existing baseline filename in the temporary repo. |
| Same method, :82 first assertion: overwrite CLI return code is 1 | Same return-code equality for the latest binding in the temporary repo. |
| Same method, :82 second assertion: overwrite diagnostic contains `immutable binding already exists` | Identical diagnostic assertion in the temporary repo. |
| Same method, :83: overwritten binding bytes remain unchanged | Identical original-bytes equality in the temporary repo. |
| Same method, :86 first assertion: invalid-source CLI return code is 1 | Same return-code equality against the latest admitted pre-bump files in the temporary repo. |
| Same method, :86 second assertion: invalid-source diagnostic contains `source version` | Identical diagnostic assertion; the fixture's pre-bump anchor matches its binding chain regardless of the real tree's release count. |
| Same method, :87: no invalid-version binding is written | Same no-file assertion for the fixture's deliberately invalid version. |
| `test_claimed_pre_bump_commit_cannot_hide_old_record_edits`, :101: forged before/after old-record edit raises `pre-bump bookkeeping sources` | Identical refusal type/diagnostic, replayed for each admission against its recorded before-source hashes and preceding binding state. |
| Same method, :104: clean build has 14 tests | Identical cardinality assertion for each admission, plus exact equality of the rebuilt immutable record (including translated policy when present). |
| `test_next_release_uses_the_same_narrow_procedure`, :129: next transition returns `[]` | Identical zero-error assertion for a synthesized next release from the latest admitted snapshot, with a real temporary Git source/commit. |
| Same method, :135: resulting test-ID set equals current admitted set | Identical set equality against the temporary repo's latest checked admission. |
| Same method, :136: shipped-catalogue fingerprint changes | Identical inequality for that same test ID across the next transition. |
| `test_immutable_binding_tampering_is_rejected`, :150: forged fingerprint raises `immutable bookkeeping binding` | Identical exception type/diagnostic applied independently to **every** admitted binding, including both `0.8.49.json` and `0.8.50.json`. |

All other existing assertions are retained: validator existence; retirement and later-fingerprint checks; all three later-source-edit refusals and their before/after 14-test controls; immutable-tamper before/after 14-test controls. Added next-release CLI controls independently reject a tampered latest admitted record (0.8.50 on this base) and an unrelated packaging edit with exit 1, the permitted-change diagnostic and no new binding, then accept the clean transition. Its output must equal the independently built record; after a temporary commit the entire extended immutable chain must verify.

### TF-23 red / green evidence

Interpreter: `the hash-pinned test venv (requirements-test.txt)`.
Both runs use `-B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module root:test_release_bookkeeping --module claude:tests.test_previous_release_catalogue`, default jobs, and `--save-timings /private/tmp/tf25b-{red,green}.json`. No other modules and no full gate are run.

- Red, original `74f4d84e`: root worker FAIL (8 tests, 4 failures and 1 error: three stale restoration subtests, the stale CLI anchor diagnostic, and the stale next-release proof); catalogue worker PASS (14 tests). Aggregate exit 1 also contains 1,497 pre-existing metadata errors on the untouched base; evidence `/private/tmp/tf25b-red.log`, `/private/tmp/tf25b-red.json.run.json`.
- Green selected workers, `74f4d84e` plus this test/document change: root PASS (8/8, return code 0, 17.992s); catalogue PASS (14/14, return code 0, 0.476s). Both workers have zero guard violations. The aggregate runner still exits 1: its 1,497 metadata errors are list-identical to red (zero added or removed), so aggregate green is not claimed; evidence `/private/tmp/tf25b-green.log`, `/private/tmp/tf25b-green.json.run.json`.
- Positive controls: PASS in `test_next_release_uses_the_same_narrow_procedure`: tampering the admitted 0.8.50 record and appending an unrelated packaging comment each produce CLI exit 1, `change outside permitted release bookkeeping`, and no 0.8.51 binding. The clean synthetic 0.8.51 append succeeds and its committed fixture chain verifies. Existing `0.8.49.json` and `0.8.50.json` both rebuild exactly and each independently rejects fingerprint tampering. The real validator and immutable binding bytes remain unchanged.
- Commits: none; changes are intentionally left uncommitted for the primary's line review (decision 117 A).

## TF-27 owner-approved edit bindings (6fa05ecd)

Replaced assertions: **none**. All product tests, helpers and existing framework-test assertions remain byte-identical. Manifest `old`/`new` assertion text describes already committed owner edits; this change does not apply those edits again. `tests/test_approved_edits.py` adds byte-exact replay, unlisted literal/helper/assertion, wrong text/newline, stale-base, I/D/V membership, renamed/restored/unresolved ID, unrelated proof, retired-entry, immutable-introduction/tamper and overwrite controls. New requirements precede the seven failing witnesses, and implementation follows their recorded red run.

Evidence and small module-run results are in `measurements/test-framework/approved-edits/` and `scripts/test-review-witnesses-approved-edits.json`. Uncommitted preparation is distinct from admission: green gate evidence uses a disposable committed repository. Main-worktree aggregate green is not claimed before introduction is committed. The 11 hold_lifecycle and one refusal_steps errors remain for ordinary migrations.

- Red: the seven new witnesses fail with `TF-27 missing approved-edit validator`; the six-module requested runner selection observes every supplied flag, including all **1,381** approved-edit errors (27 I, 452 D, 902 V).
- Green admission: both disposable committed candidates rebuild the **902** proofs exactly; all 1,381 targeted errors disappear. Nine framework modules pass **136/136** tests, without guard or budget failures. The final ten-module sample has **188 pass, eight collector-startup failures, one skip**; the same eight failures occur in the red hold_lifecycle worker. No product/environment workaround was applied.
- Remaining sample legacy errors: **12**, comprising hold_lifecycle **11** and refusal_steps **1**. Global preflight additionally retains **55** unrelated legacy flags (24 Podman-derived value flags and 31 setup_straight flags) and **80** lifecycle metadata errors; both aggregate gates exit 1. Neither a full run nor aggregate green is claimed.
- Actual command controls independently refuse an extra literal, different approved text, a renamed test, stale base, restored legacy eligibility, and a forged real binding. Each leaves the proposed new binding absent. Positive controls and immutable synthetic introduction/committed-tamper checks pass.
- All runs use the supplied pinned interpreter with `-B` and trusted base `b531844030fa619c1f62ddd6c092ebc2ea4fca38`; selections contain at most ten modules. Main HEAD remains `6fa05ecd`; commits exist only in disposable test repositories. See `gate-green-summary.json` and the two `runner-*-green.json` files in the evidence directory for counts, selections, provenance and retained diagnostics.
