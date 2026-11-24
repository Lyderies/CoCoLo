# RT-RX red run and required stop

Baseline production: `e654b2613baeceddbe974294556f4316a963bb78`; trusted review base: `b531844030fa619c1f62ddd6c092ebc2ea4fca38`.
Only the specified prepared interpreter and ordinary `scripts/parallel_tests.py` were used. Both runs selected exactly two modules at default jobs. No production file, existing test, golden JSON, or immutable framework proof was edited; no commit was created.

RT-RX-1–21 were registered with decorated tests in two new modules, new stage/layer declarations, an independently imported requirements source, and `scripts/generate_test_metadata.py --catalogue`. See [requirement map](requirement-map.json). Links record intended assertions, not satisfied requirements; implementation locations are explicitly pending.

| Module | Tests | Result | Worker seconds |
| --- | --- | --- | --- |
| codex:tests.test_route_exhausted | 6 | 1 failure, 5 errors | 0.158 |
| codex:tests.test_route_exhausted_open | 4 | 4 errors | 1.432 |

The behavior failure is resetless exhaustion persisting after a strictly later favorable reading (`ample != exhausted`). Errors name the absent classifier, trusted service snapshot, and bounded route/open APIs. Both modules are under their budgets; there are no budget failures. These are red tests against unchanged production, not a green gate.

The initial run additionally found four missing non-unit rationales in the new service module. A class-level rationale was added and the ordinary command repeated. The final run has no RT-RX/new-test metadata errors. It still reports 2856 unchanged unrelated global metadata errors, including `cannot verify approved test edits: unlisted change in approved-edit files`, existing requirements without checks, and existing legacy-proof errors. Their full messages are in the raw log. No gate bypass or unrelated repair was attempted.

Raw logs: `/tmp/rtrx-red.log`, `/tmp/rtrx-red-final.log`; final worker logs: `/private/tmp/ptb_6zqjbc/2.log`, `/private/tmp/ptb_6zqjbc/1.log`.
Measurements: `/tmp/rtrx-red-timings.json.run.json`, `/tmp/rtrx-red-final-timings.json.run.json`; summary: [red.json](red.json).

## Required stop before production implementation

Additional conflict at `packages/codex-agents/tests/test_provider_restrictions.py:81`: whole-record equality forbids the durable fallback/reset provenance required by RT-RX-3/4/5/20, even when the provenance is outside windows. [Exact old/proposed line and helper scope](conflicts.md). This is a different class from the authorized five-hour literal edits. The existing assertion is unchanged; its failure under the proposed representation is a structural compatibility finding, not an executed existing-test failure.

Green and importer runs: **not run**, as required by the stop instruction. [Importer inventory](importers.md) lists 154 affected modules with explicit not-run results and 135 static socket-dependency flags for the primary. No sandbox socket failure is claimed without execution.

Approved TF-27 edits remain unapplied, so there are no before/after line transitions to manifest from this attempt: admission :89; routing risk :301; routing judgment :71/:176; and the 124c lifetime literals. No existing test or helper was changed.

## Restart on new test framework (current result, 2026-11-24)

Continued the prepared tests rather than rewriting them. Registered both modules,
imported all 21 requirement statements into the catalogue and regenerated checks.
`test_route_exhausted` is unit/unit; `test_route_exhausted_open` is scenario/integration:
its reused real-ledger fixture generates an owner key using a subprocess.
The first restart registration used component and the guard correctly rejected that
fixture; the layer was corrected before the final core run. No framework guard was bypassed.

Commands used the user-specified interpreter, `-B scripts/parallel_tests.py`, and
`--base b531844030fa619c1f62ddd6c092ebc2ea4fca38`. The initial red run selected two
modules; the final run selected eight (never more than ten). Raw evidence:

- `/tmp/rtrx2-red.log`, `/tmp/rtrx2-red.json.run.json`: pure module 1 failure/5 errors
  against unchanged production (missing classifier, snapshot API and later clearing).
  Open module was blocked by the initial component-layer process guard; this is not
  represented as behavioral red evidence.
- `/tmp/rtrx2-core.log`, `/tmp/rtrx2-core.json.run.json`: 247 tests, 244 passing;
  all 10 prepared RT-RX tests pass. Failed checks: one admission-metadata test
  (six supersession/boundary subtests) and two provider tests. All metadata checks
  pass and there are no budget failures. Worker logs: `/private/tmp/pt8uo1lh13`.
- Green modules: `test_route_exhausted`, `test_route_exhausted_open`, `test_routing`,
  `test_routing_risk`, `test_routing_judgment`, `test_provider_restrictions`.
- Existing golden JSON files are unchanged. Three golden checks use the shared
  helper, the narrow legacy availability projection and new `.requirement` sidecars.
  [assertions.md](assertions.md) records every changed existing-test line (33 entries).

The supplied **stop on additional existing-test change** rule now applies to the
supersession/boundary setup conflict at `test_admission_metadata.py:107/113`.
[conflicts.md](conflicts.md) gives the exact scenario and proposed narrow resolution.
The provider failures require production fixes, not relaxed assertions.
Implementation is partial: classifier, service snapshot builder, routing filters,
structured results and in-process open helper exist, but CLI/live-path wiring is
unfinished. Passing prepared helper tests do not establish live integration.

Affected selection was evaluated read-only using the runner's unchanged `affected()`
function against `3d409a50`: it selects all 331 runnable modules because new tests
lack recorded coverage and metadata/non-Python files changed. The actual supplied
`--affected` command and further importer executions were **not started after the
mandatory stop**. [affected-selection.json](affected-selection.json) records the
selection; [importers.md](importers.md) lists primary follow-up modules and static
socket dependencies. No socket failure is claimed and no slowest-module performance
claim is made; those modules remain unmeasured. No commit was created.

## Continuation under 124e (2026-11-24)

Kept the first pass. Applied only the two approved boundary setup replacements
and recorded them as assertions.md entries 34/35 (35 total). All expected-value
and boundary assertion lines remain unchanged. Production now preserves the
merged later measured reset for explicit restrictions and the single-record
helper's legacy `ample` for null utilization. The shared usage classifier still
reports unknown for unmeasured readings, which clear no restrictions.

Repeated the exact prior eight-module selection with the supplied interpreter,
trusted base and ordinary runner: **247 tests, 246 passing, one failing**; all 10
RT-RX tests pass and both named provider regressions changed from red to green.
Elapsed 10.944s; no metadata errors, over-budget results or budget failures.
Raw log `/tmp/rtrx2-continue.log`; record `/tmp/rtrx2-continue.json.run.json`;
worker logs `/private/tmp/ptfg_an879`. [continuation-results.json](continuation-results.json)
holds per-module results and regression evidence.

Required stop: literal 124e removes fresh billing as well as the newer favorable
headroom reading. Six launch subcases at `test_admission_metadata.py:113` now
fail the unchanged billing-freshness gate. [conflicts.md](conflicts.md) records
the exact deltas and a concrete additional setup edit for primary review.
No additional existing-test edits or production billing relaxation were made.

Full affected execution remains the primary's task under the continuation
instruction. No selected socket test was blocked; unselected socket candidates
remain in [importers.md](importers.md). The runner lists 14 slowest-module budget
debts as unmeasured; no performance claim is made for those modules. Live CLI
wiring remains incomplete as recorded in the first pass. No commit was made.
