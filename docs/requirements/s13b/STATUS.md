# S13b: green, ready for primary review and socket reruns

Worktree `cocolo-s13b`, branch `feat/s13b-due-drain`, base `d1af5e86` (accepted S13a).
Changes are uncommitted per the user's instruction. No sockets, install, service changes, or commits performed.
No test run remains in flight.

## Requirements and implementation

RS-10-DUE-PROD / RS-10 (P:1713-1714), RS-B section 12, decision 354:
`ResultPersistenceLane` requests one lease start pass at limit 64 per expiry request.
`ProjectHistory` exposes a writer-local continuation flag after a completed full pass or successful backoff;
each call resets it, including an empty pass. The internal request carries that flag back to
`RunDriver._expire_results`, which schedules one further request on the next owner step.
Queued requests are left alone. A short pass with no backoff, or a failed request, retains the ten-second cadence.
A selection whose inspection/backoff fails before progress does not cause an immediate retry spin.

The strict S13a selection remains ONE selection of at most min(limit,64), through the existing partial index.
All-blocked rows get one follow-up empty pass, then wait for periodic maintenance and durable next_check_ms.
Existing 1 s doubling to 300 s backoff, selection order, release resets and public integer return values are preserved.
The extra ProjectHistory change only exports pass progress to the writer; no schema, persisted record shape,
reason code, account/collector operation or import-time I/O change was made.

The production measurement uses the actual `RunDriver._expire_results` and real persistence writer;
it advances a virtual clock by the real request duration plus each 0.5 s owner tick, without a socket host.
It never calls the lease drain directly or substitutes its own scheduling decision.
[production.json](production.json): **1,000 released in 16 passes, 8.311 simulated seconds**, all released within 60 s.
A real socket-host integration rerun remains with the primary.

## Red -> green

[red.log](red.log): unchanged base product, 6 tests in 3.665 s, 4 expected failures and 2 baseline guards green.
The planned S13 production flip measured **990.956 s, 100 passes, 60 released within 60 s** before any product edit.
[red-failed-backoff.log](red-failed-backoff.log): additional failure-path regression red (two immediate requests instead of one),
then corrected by setting the full-pass flag only after the inspection loop completes.

| Requirement | Test in DueDrainTests (unless noted) | Evidence |
|---|---|---|
| RS-10-DUE-PROD | S13Budgets.test_rs10_finding_the_service_maintenance_cadence_misses_the_60_s_drain_of_1000_due_holds | red pinned miss -> no violations, 16 passes, all 1,000 within budget |
| S13B-NEXT | test_full_pass_queues_one_request_on_next_step_and_selects_at_most_64_per_call | red 120 left instead of 66 -> requests at the same clock drain 64,64,2 |
| S13B-BOUND | same test; test_full_blocked_pass_reaches_eligible_rows_then_waits_for_backoff | red at base's 10-row cadence -> per-call selections 64,64,2 and blocked-prefix selections 64,1; S13a guards also green |
| S13B-BACKOFF | test_non_full_backed_off_pass_continues_then_empty_pass_stops_busy_loop; test_full_blocked_pass_reaches_eligible_rows_then_waits_for_backoff | red missing next requests -> blocked rows deferred, empty pass stops, rows behind 64 blockers progress |
| S13B-IDLE | test_full_selection_that_cannot_back_rows_off_waits_instead_of_spinning | red 2 requests instead of 1 -> normal cadence; non-full/no-backoff and queued/failed guards green at base and after change |

Only the planned existing RS-10-DUE-PROD assertions changed; exact old -> new list: [assertions.md](assertions.md).
No existing tests renamed or other existing assertions changed. New tests and IDs are registered in test-layers,
test-stages, test-timings, test-requirements and tests/requirements/s13b.json; production source statement updated too.

## Verification

Final registered run: **84 tests green**, metadata_errors [], requirement_failures [], budget_failures [], failed_units [].
Runner 11.5 s, preflight 7.8 s separately. [verification.json](verification.json) holds exact worker outcomes,
seconds, requirement statuses and product source hashes; [final-registered.log](final-registered.log) is the runner output.

| codex module | Tests | Seconds |
|---|---:|---:|
| tests.test_s13b_due_drain | 6 | 2.47 |
| tests.test_s13_budgets | 10 | 6.49 |
| tests.test_s13a_lease_pass | 10 | 10.36 |
| tests.test_result_leases | 23 | 2.55 |
| tests.test_project_history | 35 | 1.16 |

Initial registered attempt had metadata_errors [] but the component guard rejected subprocess setup in the
reused worker fixture. Replaced setup with the actual liveness owner and idle worker pool directly;
component guard now proves the new module uses neither processes nor sockets. An intermediate discovery caught
an imported TestCase alias; it was changed to a module import before any test execution. Final evidence is clean.
`git diff --check` passes. Exact rerun from the repository root:

```sh
PY=$(ls -d /private/tmp/claude-501/*/bbfe95e0*/scratchpad/tfvenv/bin/python | head -1)
TMPDIR=/tmp/claude-501/s13b "$PY" -B scripts/parallel_tests.py --jobs 2 \
  --module codex:tests.test_s13b_due_drain --module codex:tests.test_s13_budgets \
  --module codex:tests.test_s13a_lease_pass --module codex:tests.test_result_leases \
  --module codex:tests.test_project_history
```

## Primary handoff

Primary commits after review and reruns socket-bearing modules in a suitable environment (not run here):
`codex:tests.test_project_driver`, `codex:tests.test_result_persistence`,
`codex:tests.test_result_lease_lane`, `codex:tests.test_project_hold_register`,
`codex:tests.test_deadlines_retention`, `codex:tests.test_b7b_budgets` (real startup budgets).
Use the same Python and short TMPDIR with named `parallel_tests.py --module` arguments.
The primary also owns the affected/full checks; no schema-change-wide backup/restore rerun is triggered.
