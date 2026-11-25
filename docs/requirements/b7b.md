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

## Requirement-to-test map and measurements

macOS 27.2.1 arm64, CPython 3.14.7, disposable local stores, 2026-11-25.
Raw numeric evidence: [b7b/measurements.json](b7b/measurements.json). No production
services, databases or result content were used. Timing samples describe this
machine/run, not a guaranteed production latency.

| Requirement | Test / evidence | Result |
|---|---|---|
| RS-B-B7-4 | `RecoveryTests` (empty/restart, strict 20-second boundary, source-held reservation, sweep blockers/refusals/fences/current failed stop); `RecoveryScopeTests` (two projects, read-only, privacy, has_holds release); `AccountObservationTests`; `OwnRecoveryTests` | Red before implementation; store/driver tests green. Own updates do not refresh sweep age; duplicate identities count once and one specific reason replaces settlement. |
| RS-B-B7-6 | `RecoveryTests.test_response_requires_closed_reasons_types_and_stale`; `PlainTerminalOutputTests.test_plain_terminal_output_without_socket_privilege`; existing `HoldsProtocolTests`, `HoldsClientStatusTests`; reply compatibility guard | Red missing key -> green, including real report/client/CLI from isolated plain terminal. Real socket tests cannot bind in this sandbox. |
| RS-B-B7-5 / B1 | `BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures` | **Partial:** five actual reserve/arm/bind/commit register calls total **0.266 ms**. Full normal-run writer delta, release tail and transaction commit costs **unmeasured**, so no claim against <5 ms. |
| B2 | same harness, store reopen including validators, three samples each | **Partial:** median empty **2.578 ms**, 1,064 held leases **4.182 ms**, delta **+1.604 ms**. Full service startup against <=+100 ms is **unmeasured** (sockets blocked). Setup bypasses only repeated capacity checks, verifies once with the unmodified check, then reopens/validates before measurement. |
| B3 | same harness, actual `release_earlier_unarmed_leases(limit=64)` | **Pass for this path:** maximum **18.942 ms <50 ms**, over 16 batches (last has 40 rows). Includes ordinary durable source/register transactions. |
| B4 | same harness, actual product queries at 0 / 100,000 valid released custody/expired context tombstones | **FAIL:** `_holds` **1,722 -> 9,001,722 VM instructions**. Equal-work budget is not met. No threshold changed. Context candidates **223/223**, reconciliation **157/157**, absent outcome lookup **15/15**; live context plan uses its partial index. Source lease/reservation tombstones are not separately covered. |
| B5 | same harness, 1,000 eligible unarmed leases behind 64 live/blocked leases | **Pass for this path:** **1,000 released in 16 batches <=18**. Does not establish a memory-kind drain or account round-trip bound. |
| B6 | same harness, one eligible lease, 128 live blockers and 50 arrivals | **Partial:** target released in **1 batch**, **0.409 ms**. Re-due/backoff with continuing arrivals in the generic register scheduler is **unmeasured**: that scheduler and memory hold tracking are absent. No substitute scheduler was benchmarked. |
| RS-D S3 | both `BudgetMeasurements` tests | Retirement of ten: **8.625/7.512 ms <50 ms**, **10,512/10,512 VM instructions**. Successful authenticated outcome lookup, maximum of five samples: **0.641/0.663 ms <5 ms**, **491/491 VM instructions**. Context candidate/reconciliation work equal above. Store open with 64 real context files: **2.071 -> 3.385 ms**, **+1.314 ms**. Whole service startup against <=+200 ms remains **unmeasured**. Retirement timings include VM counter overhead. |

B7-5 is **partially measured with a recorded failure**, not an all-budgets-passed
claim. B4's report query regression and the absent generic scheduler require primary
follow-up; the other partial rows need full lifecycle/service measurements.
