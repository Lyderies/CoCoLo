# B7b: recovery reasons and measured budgets

Authority: ANALYSIS-0.4.0-rsb-b4-b7.md §7, RS-B draft 3 §12 and draft 4 §5,
RS-D-9/S3. B7a is merged. No schema change; no edit to project_start.py,
VERSION or the runtime digest catalogue.

## RS-B-B7-4

`history.holds_report` adds `recovery_pending` with exactly `reasons` and `stale`.
`reasons` always contains five nonnegative counts: `needs_settlement`,
`needs_process_evidence`, `has_holds`, `stop_failed`, `account_unreachable`.
Each unresolved row has one current reason (RS-B draft 3 §10). Reservation holds
and closing_denied/admitted_unbound leases default to `needs_settlement`, even
without a readable journal entry. More specific observations replace that reason:
`needs_process_evidence` when disappearance is unproved, `stop_failed` after a
failed container stop, and `account_unreachable` after an actual transport failure.
A later successful observation clears the failure. `has_holds` counts open
orchestrations blocked by live execution holds. Current failed admitted runs are
included; ordinary active runs do not acquire a failure observation.
The driver publishes an immutable project-local observation after a complete sweep;
the owner also publishes its failed-run/backoff observations without refreshing
the sweep age; run identities deduplicate overlapping observations internally. The
writer adds its current has_holds count without reading journal files or result
contents. No observation means stale. A reading is stale strictly after twice
EXPIRY_SECONDS since its last completed observation, using monotonic time. Failed
or incomplete sweeps do not refresh it. A restart begins stale. No identities,
paths, digests, result bodies, exception text or other project data are returned.

Tests: `tests.test_b7b_recovery` (real store and driver sweep with controlled external
boundaries); `tests.test_b7b_terminal` (plain isolated Python terminal, real framed
history exchange where sockets are permitted).

## RS-B-B7-5

Measure the combined code, report raw numbers and failures without loosening limits:
B1 added register writer work of a normal run <5 ms; B2 1,000 held rows startup
overhead <=100 ms; B3 a 64-row batch <50 ms; B4 equal query work at 0/100,000
tombstones and partial index plans; B5 1,000 eligible rows behind 64 blocked rows
within ceil((1000+64)/64)+1 = 18 batches, and one eligible behind 128 blocked rows
within 4; B6 the latter with re-due blocked rows and 50 arrivals/batch still within
4. RS-D S3: retirement of 10 <50 ms, outcome lookup <5 ms, startup overhead with
64 live contexts <=200 ms; compare retirement/lookup/reconciliation work at
0/100,000 tombstones. Measure product operations, not a substitute scheduler.
Missing operations or unavailable service startup measurements are reported as
unmeasured/failed, never inferred from design or a cheaper proxy.
Measured B3, B4, B5 and S3 retirement/lookup limits are executable assertions.
B4 includes 100,000 released lease and reservation rows as well as custody/context
tombstones, and measures recovery_pending including its has_holds query. Attempt
identities split at their single colon: the run-handle grammar excludes colons,
and the identity constructor must assert that invariant. Missing memory hold
tracking must stay asserted while B1/B6 remain unmeasured; service startup remains
separately unmeasured while only store-open timing is collected. A socket-bind
availability assertion fails if this environment limitation lifts, requiring full
B2/S3 service startup measurements then.

Tests/harness: `tests.test_b7b_budgets`; measured results and exact scope will be
recorded below and in `b7b/STATUS.md`.

## RS-B-B7-6

Grow report and closed response validator together; require every new key, reject
extra keys, bool/negative counts and non-bool stale. Capacity report stays unchanged.
`learning-status` prints recovery_pending (null if unavailable), through the existing
project enrollment and authorization. The plain-terminal test checks the real
new response shape, foreign project rejection, and output privacy. Existing B7a
exact-top-level assertion must grow with the report; record the replacement in
`b7b/assertions.md`. No other existing assertions may be weakened.
When recovery is stale, learning-status also prints the plain sentence
"Recovery was not checked recently." so zero counts cannot imply a recent check.
A history-only service (ProjectServiceHost with bundle_root=None) serves the
report without a RunDriver and must therefore return stale until a completed
observation exists. A client with an older runtime digest is refused with
history_not_authorized before authorization or report computation; the gate is
the service's runtime-digest equality check, not an assumed old-client decoder.

Sweep-local account failure capture is reset in a finally, including when
post-sweep cleanup/publication raises. A refusal (account_not_authorized or
account_wrong_owner) beside a transport failure must count only the latter as
account_unreachable.

## Requirement-to-test map and measurements

macOS 27.2.1 arm64, CPython 3.14.7, disposable local stores, 2026-11-25.
Raw numeric evidence: [b7b/measurements.json](b7b/measurements.json). No production
services, databases or result content were used. Timing samples describe this
machine/run, not a guaranteed production latency.

| Requirement | Test / evidence | Result |
|---|---|---|
| RS-B-B7-4 | `RecoveryTests`, `RecoveryScopeTests`, `AccountObservationTests`, `OwnRecoveryTests` | Original reasons/staleness/privacy tests retained. New finally exception test red → green. Simultaneous refusal/unreachable test already green: exactly one account_unreachable for either refusal pair. |
| RS-B-B7-6 | `test_learning_status_notice_only_when_recovery_is_stale`; `PlainTerminalOutputTests`; `test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report`; existing validators/compatibility | Missing plain stale notice red → green. History-only service returns stale zero counts. Runtime mismatch refused as history_not_authorized before authorizer/report; existing gate already green. Real socket tests blocked by EPERM. |
| RS-B-B7-5 / B1 | `BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures` | **Unmeasured (partial evidence):** five reserve/arm/bind/commit register calls total **0.175 ms**. Full normal-run writer delta, release tail and commit costs unmeasured; missing memory tracking is asserted. |
| B2 | same harness, three real store reopen samples each | **Unmeasured (partial evidence):** median empty **2.209 ms**, 1,064 held leases **3.929 ms**, delta **+1.720 ms**. Full service startup <=+100 ms unmeasured. Socket binding must still raise PermissionError; availability fails loudly requiring the full measurement. |
| B3 | same harness, actual `release_earlier_unarmed_leases(limit=64)` | **Met, asserted:** maximum **16.611 ms <50 ms**, 16 batches (last 40 rows), durable source/register transactions included. |
| B4 | same harness, real hold/context/recovery/has_holds queries at 0 / 100,000 tombstones | **Met, asserted:** `_holds` **994/994**, recovery_pending **455/455**, has_holds **335/335** VM instructions. Includes 100,000 released lease and 100,000 released reservation source rows/holds, plus custody/expired context tombstones. Context candidates **223/223**, reconciliation **157/157**, absent lookup **15/15**, retirement **10,512/10,512**. All deltas zero (allowed constant <=16); live context partial-index plan asserted. Red before fix: holds **1,722/9,001,722**, recovery **354/3,900,352**, has_holds **252/1,000,250**. Colon-free identity constructor assertion also red → green. |
| B5 | same harness, 1,000 eligible unarmed leases behind 64 blocked leases | **Met, asserted:** **1,000 released in 16 batches <=18**. Memory-kind drain/account round trips are not established. |
| B6 | same harness, one eligible lease, 128 live blockers, 50 arrivals | **Unmeasured (partial evidence):** target released in **1 batch**, **0.384 ms**. Generic re-due/backoff scheduler and memory holds remain absent (asserted); recurring-arrival bound is unmeasured. |
| RS-D S3 | both `BudgetMeasurements` tests | **Met for measured paths, asserted:** retirement of ten **45.986/7.973 ms <50 ms** including VM tracing; successful authenticated lookup maxima of five samples **1.071/0.722 ms <5 ms**, **491/491 VM instructions**. Store open with 64 real context files **2.240 → 3.896 ms**, **+1.656 ms**. Whole service startup <=+200 ms remains separately **unmeasured**, guarded by socket availability. |

Final budget named runner: **2 tests, 28.4 s, PASS**,
`metadata_errors: []`. See [b7b/STATUS.md](b7b/STATUS.md) for all six review items,
red/green tests, files, assertion replacements and execution limits.
B1/B2/B6 and S3 service startup remain explicitly unmeasured; measured paths pass
without changing their timing limits. B4's history-dependent query failure is fixed.
