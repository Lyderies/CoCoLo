# Framework workflow

Use the prepared, hash-checked test-only environment. Shipped programs need no test dependencies.
Full runs use the runner's default jobs. Pass a trusted review base explicitly (the candidate cannot choose its own grandfather baseline):

```
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --save-timings /tmp/cocolo-timings.json
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module root:test_test_framework --save-timings /tmp/cocolo-affected.json
```

The flat timing file remains scheduling input. Its `.run.json` companion records current worker job-seconds,
separate discovery/preflight time, module totals, selected checks, budgets/debt and actual outcomes.
A debt warning does not mean failure; exceeding its unchanged ceiling does. A complete under-budget result
requires removing that module's debt entry before the next gate. Entries cannot be restored or ceilings edited.
Combine all shard `.run.json` files using `--combine-timings`; partial records never certify module budgets.

Declare modules in `scripts/test-stages.json` and `scripts/test-layers.json`. Decorate new/migrated tests with
`test_framework.requirements`; non-unit tests need `why_not_scenario`. Keep all assertions when migrating.
Generate catalogue links with `python -B scripts/generate_test_metadata.py --catalogue`; remove migrated legacy
entries with `--shrink-legacy`. Original source and supplementary fixture/helper fingerprint baselines are immutable.

Use `test_golden.compare` for checked-in UTF-8 expected output. Updates require exactly UPDATE_GOLDEN=1,
a known declared requirement and its adjacent `.requirement` sidecar. Update runs fail intentionally; review
and rerun normally. Temporary helper contract fixtures may inspect their own writes; checked-in golden bypasses fail.

Runs default to ci. Select dev or soak explicitly; a soak module cannot be selected with ci/dev, and multiple
profile selectors are rejected. Complete merge campaigns belong to the explicit soak inventory.
Registered future execution and semantic review records live in `test-checks.json`; absent evidence remains not_run.
A full gate cannot pass missing or unexecuted required evidence. Entry-point links live in `test-entrypoints.json`.

Owner's next checkpoint is after merge-library step 3. Assertion migrations are in ASSERTIONS-test-framework.md;
requirements and per-step evidence are in REQUIREMENTS-test-framework.md and REPORT-test-framework.md.

## Routine previous-release catalogue bookkeeping (TF-25)

Keep the installed previous release in `packages/claude-agents/previous-releases.json` when advancing the product version. Before editing, record the trusted pre-bookkeeping commit (`git rev-parse HEAD`) and the trusted source commit whose `native-package/VERSION` names the previous installed release.

Only these changes qualify for automatic rebinding:

1. Append one record to `packages/claude-agents/previous-releases.json`: its release, source commit, package version, compiled files and digest must exactly match that trusted source. Preserve every older record and schema. Use the existing canonical JSON formatting (indent 1, sorted keys, final newline).
2. In `packages/claude-agents/tests/test_previous_release_catalogue.py`, raise only the two `len(released_bundles(self.source))` expectations from N to N+1. Extend the release-list comment on the first assertion, including the matching trailing same-bundle count. Keep the 2 MiB boundaries, all refusals and all other text unchanged.
3. In `packages/claude-agents/src/claude_agents/packaging.py`, extend only the existing digest's annotation with `and VERSION (source COMMIT8)`, turning its previous final `and` into a comma. A new executable pin or changed bundle semantics require normal proof review; this command does not authorize them.

Use the prepared hash-pinned interpreter to verify and create a **new** immutable binding file:

```
python -B scripts/rebind_release_bookkeeping.py --from <trusted-pre-bookkeeping-commit> --source <trusted-previous-release-source> --previous-release <previous-installed-version>
```

The initial 0.8.49 repair used `--from c75eba51 --source b531844030fa619c1f62ddd6c092ebc2ea4fca38 --previous-release 0.8.49`. It creates `scripts/test-release-bindings/0.8.49.json`; never overwrite it or any older snapshot. For the next release, use its own prior commit/source/version and commit the new release-named file together with the three bookkeeping files. The command rejects unrelated edits and stale pre-bump proofs. Before-source hashes must match the last admitted state, including across worktrees; merely naming another Git commit cannot authorize changes to older records.

Run the affected modules at default job count, then the authorized full run with the same trusted base:

```
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module root:test_release_bookkeeping --module claude:tests.test_previous_release_catalogue --save-timings /tmp/cocolo-release-bookkeeping.json
```

The runner checks every binding's immutable introduction and permitted source transition, then verifies the current files and test fingerprints. Retired grandfather entries stay retired. The binding is a narrow permission for the recorded bookkeeping transition, not a waiver for later edits or changed helpers.


## Product fixes and test-owned legacy dependencies (TF-26)

Ordinary product implementation changes require no legacy proof rebinding. The gate fingerprints test-owned assertions, fixtures/helpers and their constants; product functions/classes and imported constants/defaults are referenced by identity. A changed helper, assertion or legacy test ID still requires the normal requirement proof migration.

The immutable `scripts/test-legacy-test-owned-v1.json` and `-v2.json` versions were generated from trusted `5ab0a9d3`, never candidate test/helper files. Reproduction commands for their committed algorithms are `python -B scripts/generate_test_owned_baseline.py --algorithm 249d4f7d` (v1 generator revision) and `python -B scripts/generate_test_owned_baseline.py --algorithm 813661df` (v2 generator revision), using the prepared hash-pinned environment and the matching historical generator revision. The generator refuses to overwrite an existing version. Do not run it for a product fix or change an immutable file: a future policy change needs a new reviewed version.

The ordinary command remains `python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --save-timings <timings.json>` at the default job count. TF-25 release bookkeeping still uses its documented rebinding command; new records include the test-owned dependency interpretation alongside the historical one.
