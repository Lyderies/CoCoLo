# S13a status: green; ready for primary review and socket reruns

Branch `feat/s13a-lease-pass`; prerequisite `a09e71f0` is an ancestor of HEAD.
Scope: RS-B section 5 lease start pass, bounded selection (64), due order, blocked retry
1 s doubling to 300 s, reset on release. Existing register timing/evidence columns
and partial index are reused; no schema, record-column, reason-code or operation change.
No commit or sockets authorized; changes remain in this worktree.

Tests: planned S13 mechanism flip plus `tests.test_s13a_lease_pass` (9 tests).
Assertion change: [assertions.md](assertions.md).

## Implementation

`project_history.py` selects at most `min(limit,64)` due held unarmed leases via
`project_holds_due`, ordered by `next_check_ms`, with source lookups by primary key.
Blocked live rows, host-open/reservation blockers and inspection refusals persist a
`start_pass_failures` counter in existing evidence, with 1,2,4,...,256,300,300 s delays.
Each defer is inside the inspection transaction; a refusal rolls its inspection back
before a separate defer transaction, preserving the existing error report and other-row progress.
The counter and check time reset when `_set_lease` ends the lease (`bound` or `expired`).
New lease check times start at creation time; zero would let new arrivals repeatedly
overtake a previously deferred due row. Source deadlines and lease authority are unchanged.

## Red -> green

Unchanged product: the flipped S13 assertion plus the first seven S13a tests ran in
2.668 s: seven failures, armed-row prefilter guard passed. Mechanism: 179 selected,
no backoff; bound: 179 > 64; backoff/refusal/reset: 0 instead of a future check;
order: insertion order; fairness: retried before due. Each requirement below had a red witness.

| Requirement | Red witness in `LeaseStartPassTests` (test prefix) | Green |
|---|---|---|
| RS-B-9-B6-MECH | S13 `test_b6_finding_...` planned flip | no violations |
| S13A-BOUND | `test_128_blocked_plus_arrivals_...` | bounded blocked/arrival selection, eligible releases, armed prefilter |
| S13A-BACKOFF | `test_backoff_doubles_...`, `test_inspection_refusal_...` | every delay and cap, pre-due skip, reopen, blocker and refusal |
| S13A-RESET | `test_release_resets_backoff_...` | start pass, explicit, expiry, bind |
| S13A-ORDER | `test_due_rows_are_selected_...` | earliest due first, future rows skipped |
| S13A-FAIR | `test_blocked_row_is_retried_...` | retry progresses despite 64 new blocked arrivals per step |

Registered `parallel_tests.py --jobs 2` run (test venv, `-B`, `TMPDIR=/tmp/claude-501/s13a`):
`metadata_errors: []`, `failed_units: []`, `requirement_failures: []`, 77 tests.
Modules: `codex:tests.test_s13a_lease_pass` 7.8 s; `codex:tests.test_s13_budgets` 7.6 s;
`codex:tests.test_result_leases` 2.1 s; `codex:tests.test_project_history` 1.3 s.
Runner elapsed 9.7 s (preflight 8.3 s separately).

Supplemental direct run: 108 tests pass in 22.274 s, combining
`tests.test_project_hold_register.RegisterWriteTests`, `.RegisterAdoptionTests`, `.DenialRegisterTests`,
`tests.test_holds_report`, `tests.test_project_holds_schema`, and
`tests.test_deadlines_retention.ContextSweepTests`. This covers source/register rollback,
adoption, denial release, the existing upgrade path and unchanged reporting.

Caller search identified context-retirement/recovery callers; a second registered run
passes 87 tests: `codex:tests.test_record_context` 8.2 s,
`codex:tests.test_retirement_sweep` 4.1 s, `codex:tests.test_recovery_line` 5.0 s.
Elapsed 9.1 s, preflight 8.1 s separately; `metadata_errors: []`, `failed_units: []`,
`requirement_failures: []`. Machine-readable summaries: [verification.json](verification.json).
`git diff --check` passes. No test run remains in flight.

## Primary handoff

No commit created. S13b production-cadence finding stays pinned and unchanged.
Primary must rerun socket modules: `codex:tests.test_project_hold_register`,
`codex:tests.test_result_lease_lane`, `codex:tests.test_project_driver`,
`codex:tests.test_deadlines_retention`, and `codex:tests.test_b7b_budgets` (real startup budgets).
No schema migration/copy-format change, so the schema-change-wide backup/restore rerun is not triggered.

Run registered checks from the repository root with the specified venv Python, `-B`,
and `TMPDIR=/tmp/claude-501/s13a`:

```sh
$PY -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_s13a_lease_pass --module codex:tests.test_s13_budgets --module codex:tests.test_result_leases --module codex:tests.test_project_history
$PY -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_record_context --module codex:tests.test_retirement_sweep --module codex:tests.test_recovery_line
$PY -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_project_hold_register --module codex:tests.test_result_lease_lane --module codex:tests.test_project_driver --module codex:tests.test_deadlines_retention --module codex:tests.test_b7b_budgets
```
