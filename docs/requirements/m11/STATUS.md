# M11 status

The primary's unsandboxed lifecycle run reached the retry after drain/replacement
and failed with `open_conflict` at `test_host_slots.py:301`. That test and all its
assertions are unchanged by this continuation. Full acceptance needs its rerun.

## Cause and fix

`client_runtime.py:1539` derives `cli-open-...` from `AccountOpenInput.payload()`.
Its inputs digest strips advisory availability through `route_identity`. The
supervisor scope is durable across ordinary replacement (`project_service.py:63`).
The retry uses the **same** request identity, original key, account handle and run id.

The CLI opens the account before `run.register` (`client_runtime.py:1540,1554`).
A refused registration leaves the committed `account_open_requests` journal and
`account_open_receipts`, pending `host_run_bindings`, its binding hold in
`run_holds`, and original launch inputs/provenance. It leaves no launch claim or
active execution capacity reservation. The driver refuses before creating a
project registration or result lease (`project_driver.py:588-607`).

Before the fix, `_capture` at `account_original_open.py:221` hashed the entire
original route, including its refreshed availability audit, into
`parameters_digest`. The same request with a new audit conflicted in
`request_journal` (old line 302, current line 307). The cause is inconsistent
identity rules, not a new request id on retry.

The fix at `account_original_open.py:225-226` applies `route_identity` to the
route in the parameter digest. Full original input and worker custody checks
remain. This selects the authorized idempotent-open recovery: replay the same
account receipt and original observation window, then register on the fresh
host, without a withdrawal or second binding/claim. Schema, record fields,
operations and reason codes are unchanged.

## Requirement -> test -> evidence

| ID | Test | Evidence |
| --- | --- | --- |
| M11-1 | `tests.test_run_driver_retries.DispatchSlotRegisterTests.test_M11_busy_dispatch_exhaustion_refuses_a_new_registration_without_a_claim` and `test_M11_a_known_registration_replays_at_dispatch_exhaustion_without_resubmission` | PASS after fix: refusal precedes registration/claim; known registration replay remains idempotent. |
| M11-2 | Same class, `test_M11_busy_dispatch_exhaustion_waits_for_drain_without_forcing_busy_false` | PASS after fix. Primary exercised live-child lifecycle up to final retry before the fix; full scenario still needs green. |
| M11-3 | `tests.test_open_retry_identity.OpenRetryIdentityTests.test_M11_availability_refresh_replays_the_leftover_journal_without_a_new_open` | RED before code: actual account `request_journal` raised `open_conflict`; GREEN after code. Real SQLite owner/preflight, no sockets or processes. Same keys, parameters, handle/run id, clock/deadline/seal, no extra writes/resources; changed original content still conflicts. |
| M11-1, M11-3 | `tests.test_account_original_open.OriginalAccountOpenJobTests.test_M11_refused_registration_retries_the_retained_open_after_availability_refresh` | Added socket-free committed-open reproduction through real client/account owners, synthetic registration refusal then acceptance. Asserts committed journal, pending binding + binding hold, zero claim/capacity, identical wire request/receipt on retry, no new clock/admission or durable rows. Sandbox: blocked by signature-worker timeout. PRIMARY UNSANDBOXED RERUN after the fix (2026-11-25, with test_host_slots and 6 related modules): PASS. |
| M11-1, M11-2, M11-3 | `tests.test_host_slots.DispatchSlotAdmissionTests.test_M11_busy_exhaustion_refuses_then_drains_replaces_and_dispatches_the_retried_run` | Primary pre-fix FAIL at retry with `open_conflict`; test unchanged; PRIMARY UNSANDBOXED RERUN after the fix: PASS. |
| M11-4 | `tests.test_host_slots.DispatchSlotRegistryTests.test_M11_registration_limit_is_bound_to_the_busy_drain_replacement_scenario` | Prior-turn RED -> GREEN registry binding preserved; four `LifetimeCapGuardTests` PASS again after fix. |

## Named-module runs

- Before code, `tests.test_open_retry_identity`: one test, ERROR `open_conflict`
  from `request_journal`, 0.049 s test / 0.6 s worker. `metadata_errors: []`.
  This is the product red witness.
- After code, `tests.test_open_retry_identity`, `tests.test_run_driver_retries`,
  `tests.test_lifetime_cap_guard`, `tests.test_runtime_release`: 32 tests,
  29 passed, two pre-existing deferred B0 skips, one expected runtime catalogue
  failure. Worker elapsed 1.7 s, summed modules 2.542 s; no budget failures;
  `metadata_errors: []`. Driver: 22 passed + two skips; guard: four passed;
  new unit: one passed.
- After code, `tests.test_project_open_scope`, `tests.test_account_open_schema`,
  `tests.test_route_exhausted_open`: 37 tests, 36 passed, one `EPERM` socket-bind
  error in `ProjectOpenScopeTests.test_actual_unix_client_process_has_no_sqlite_import_or_connection`.
  Scope: 19 passed + one socket error; schema: ten passed; routing: seven passed.
  Worker elapsed 14.0 s, summed modules 18.768 s; `metadata_errors: []`, no budget
  failures.
- Initial pre-fix `tests.test_account_original_open`: 28 tests, 14 passed,
  14 failed due to bounded signature-worker progress/drain timeouts, including
  the new committed-open check before its first receipt. Elapsed 169.0 s, summed
  shards 297.832 s, over the module's 60 s budget. Initial metadata flagged
  missing reciprocal links for the new check; fixed before the unit red run.
  Both later invocations prove `metadata_errors: []`. These timeouts provide no
  product red/green evidence; do not repeat the blocked run in this sandbox.

New module registered unit/unit in layer/stage metadata and timing registry;
no layer violations. Both new checks have reciprocal M11 links. Requirements
were clarified before tests. No existing assertion removed or changed.
`git diff --check` and `scripts/check_identifiers.py` also pass.

## Primary rerun and handoff

Outside the socket/signature-worker sandbox, with the supplied interpreter in `PY`:

```sh
"$PY" -B scripts/parallel_tests.py \
  --module codex:tests.test_host_slots \
  --module codex:tests.test_account_original_open \
  --module codex:tests.test_open_retry_identity --jobs 2
```

The unchanged host scenario configures two slots before startup, refuses the
third registration with a real child live, records/cancels/drains both runs,
waits for the production loop to exit, then retries the same third CLI inputs to
start a replacement and dispatch. Keep every assertion.

VERSION and runtime digest catalogue untouched by instruction. Runtime-release
has its expected digest mismatch; primary handles it at merge. No schema or
record-shape change, so copier/backup/move matrix is not triggered. No full suite
or PTY check. All invoked runs finished; nothing in flight. No commit attempted
in this sandbox; changes remain in the named worktree.

Continuation files: product `account_original_open.py`; tests
`test_account_original_open.py`, new `test_open_retry_identity.py`; script
layer/stage/timing/requirement registries; M11 requirement source, assertions and
this status. Prior M11 work remains in `test_host_slots.py`,
`test_run_driver_retries.py`, and `lifetime_caps.json`.
