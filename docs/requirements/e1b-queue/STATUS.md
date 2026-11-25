# E1b queue slice A status

Implemented on `feat/e1b-owner-queue`, base `7322d61f` (0.8.64 plus U3/U4), for
the 0.8.65 candidate. Changes remain uncommitted in the sandboxed worktree.
Requirements and the complete clause-to-test mapping are in `../e1b-queue.md`.

## Requirement evidence

All method names below belong to
`tests.test_ledger_owner_queue.LedgerOwnerQueueTests`.

| Requirement | Tests | Evidence |
| --- | --- | --- |
| E1b-3 | `test_running_abandonment_keeps_global_and_project_custody`, `test_abandoned_queue_entry_is_handled_without_execution`, `test_project_backlog_yields_turns_and_preserves_fifo`, `test_running_project_keeps_project_count_slot`, `test_overflow_is_named_counted_and_snapshot_is_detached`, `test_limits_and_handle_owner_validation` | PASS: retained queued/running/project custody, fair order, immediate existing busy reasons, exact counters and validated configuration/handles. |
| E1b-4 | `test_wait_uses_remaining_deadline_and_counts_once`, `test_expired_queued_work_is_skipped_and_acknowledged`, `test_running_timeout_keeps_custody_until_whole_completion`, `test_late_execution_commits_whole_and_answers_busy`, `test_reply_collected_after_deadline_answers_busy` | PASS: remaining-budget Event wait, 800 ms cap, earlier caller deadline, no queued execution after expiry, whole running callback completion and busy late answers. |
| E1b-7 | Running-abandonment, queued-abandonment, expired-queued, running-timeout and late-execution tests above | PASS: handled remains unset while running/abandoned/timed out, then acknowledges outcome and released custody. No sleeps or timed polling. |

Red-first evidence: documentation and all 11 tests preceded runtime code. The
first direct unittest invocation lacked the runner's scripts import path; with
that path supplied it failed on missing `codex_agents.ledger_owner_queue`.
That is feature-absence red evidence, not an independently executed semantic
failure for each assertion. After implementation, the first registered run
proved metadata_errors [] but failed on TF-12's unit blocking-wait guard.
The fixture correction, with every changed assertion paired to its replacement,
is recorded in `assertions.md`. No pre-existing test or assertion was edited.

## Named verification

Using the designated test interpreter as `$PY`:

```sh
"$PY" -B scripts/parallel_tests.py --jobs 4 \
  --module codex:tests.test_ledger_owner_queue \
  --module codex:tests.test_service_queue \
  --module codex:tests.test_ledger_waits \
  --module codex:tests.test_runtime_release
```

Final registered run: 26 tests across four named modules; 25 passed and one
expected release assertion failed. All E1b-3/4/7 requirements report **passed**;
**metadata_errors []**, **budget_failures []**. Worker wall 0.330 s, job-seconds
0.811 s, discovery 2.636 s and preflight 6.923 s. Individual module workers:

| Module | Result | Seconds |
| --- | --- | --- |
| `tests.test_ledger_owner_queue` | PASS, 11 tests, unit layer | 0.162 |
| `tests.test_service_queue` | PASS, unchanged fairness model | 0.160 |
| `tests.test_ledger_waits` | PASS, existing wait budget | 0.164 |
| `tests.test_runtime_release` | Expected one failure: current version's recorded digest differs; file-set completeness and version/digest ordering pass | 0.325 |

The new module is registered in layers, stages, timings and the requirement
catalogue, with authoritative E1b-3/4/7 statements in
`tests/requirements/e1b-queue.json`. The timing entry records the measured
0.16 s worker duration. `git diff --check` passes.

## Scope, files and remaining work

Added the queue module and unit tests, `docs/requirements/e1b-queue.md`, this
status, `assertions.md` and `tests/requirements/e1b-queue.json`. Updated only
the four test metadata files and the sorted runtime file list to include the
new module. `project_request_queue.py`, `service_queue.py` and their tests
are unchanged. No collector or learning-store wiring was added. VERSION and
the runtime digest catalogue remain unchanged; the primary owns their update.

The default limits (16 requests, 8 projects, share 2, maximum wait 800 ms) are
constructor configuration in one constant. Queue/share production values await
E0b-v2. The queue bounds retained callback references; trusted adapters must
supply bounded payloads. It creates no thread or ledger connection and does no
database/file I/O at import or construction.

Not run: full suite or affected-set run (forbidden by builder scope); the
process/socket-backed project writer fixture (unchanged, later integration);
E1b-1/2/5/6/8 wiring, real ledger commits, threaded stress, sockets and PTYs
(outside this standalone slice). No required slice-A check is blocked on sockets
or PTYs. Unit running-state evidence uses deterministic event continuations
because TF-12 forbids blocking thread waits; it is not thread stress evidence.
No schema, durable record shape, external reason-code set or operation set was
changed, so copier/backup/move/enumeration modules are not implicated.
