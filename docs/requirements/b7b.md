# B7b: recovery reasons and measured budgets

Authority: ANALYSIS-0.4.0-rsb-b4-b7.md §7, RS-B draft 3 §12 and draft 4 §5,
RS-D-9/S3. B7a is merged. No schema change; no edit to project_start.py,
VERSION or the runtime digest catalogue.

## RS-B-B7-4

`history.holds_report` adds `recovery_pending` with exactly `reasons` and `stale`.
`reasons` always contains five keys with nonnegative counts when fresh and null
counts when stale: `needs_settlement`,
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
and the identity constructor must raise ValueError for a colon, including under -O.
B2 memory transactions and positive-lock recovery are measured separately. B1 full normal-run delta and B6 generic recurring-arrival scheduling remain explicitly unmeasured; memory-only samples never establish those wider budgets.
Probe Unix socket binding with try/except OSError. When available, measure actual
ProjectServiceHost startup (writer bootstrap, RunDriver, listeners and published runtime hint)
with 1,064 held leases versus empty (B2 <=+100 ms) and 64 live contexts versus none
(S3 <=+200 ms), assert both limits and mark service_startup_status met only when met.
When binding is unavailable, retain store-open proxies, record unmeasured plus the
OSError reason, and skip only the service-startup subtest; never require bind failure.
Timing bounds use one warm-up then the median of three runs of the real operation;
destructive operations receive equivalent fresh work for every run. VM-step equality
at 0/100,000 tombstones remains the primary invariant.

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
"Recovery was not checked recently." and all five reported counts are null.
The response validator accepts null counts only when stale; fresh null counts,
negative/bool counts, missing/extra keys and non-bool stale remain invalid.
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

Final targeted evidence, 2026-11-25, macOS / CPython 3.14.7, disposable stores.
Raw samples and exact test results: [b7b/measurements.json](b7b/measurements.json).
Per-item red→green, files and primary rerun command: [b7b/STATUS.md](b7b/STATUS.md).
All changed assertions: [b7b/assertions.md](b7b/assertions.md).

| Requirement | Test / evidence | Result |
|---|---|---|
| RS-B-B7-4/-6 | `tests.test_b7b_recovery`; isolated plain CLI subprocess; report/response compatibility | Stale five null counts plus notice, fresh numeric counts; null accepted only stale. Recovery 13 pass; CLI subprocess pass; real socket terminal cases blocked by EPERM. |
| RS-B-B7-5 B2 / RS-D S3 startup | Budget test; real writer/driver startup lifecycle check | Bind probe catches OSError. Real host startup with writer, driver, listeners and hint is measured/asserted <=+100/+200 ms whenever binding works; both service_startup_status values unmeasured here with errno=1. Only that subtest skips; store proxies are recorded separately. Lifecycle functional check passes with binding alone stubbed, excluded from timings. |
| RS-B-B7-5 B3/B4/B5; RS-D S3 retirement/lookup | Both original budget tests | Warm-up + median of three: B3 maximum batch median 16.765 ms; ten-result retirement 6.869/7.217 ms; successful authenticated lookup 0.619/0.654 ms. Limits unchanged (<50/<5 ms). All seven paired VM deltas zero at 0/100,000 tombstones, and every corresponding retirement trial also equal. B5 releases 1,000 in 16 batches. |
| RS-B-B7-5 identity | `test_attempt_identity_requires_colon_free_run_handle`, also under -O | Explicit ValueError on colon, valid identity and real lease grammar rejection retained. |
| B1/B6 | Same budget harness | Remain unmeasured with explicit existing memory lifecycle/scheduler reasons. |

Final budget runner: **3 tests, 30.4 s, PASS**, one service-startup
subtest skip; `metadata_errors: []`. No VERSION or runtime digest catalogue bump.
