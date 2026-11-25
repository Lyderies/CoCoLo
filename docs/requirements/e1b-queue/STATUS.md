# E1b queue slice A status

## Fix round

Review 040 fixes on `feat/e1b-owner-queue`, starting at `3044a1ed` against
base `7322d61f`. This round uses that base, not `cbc757d5`. Changes are left
uncommitted in the sandboxed worktree. VERSION and the runtime digest catalogue
are unchanged; release bookkeeping belongs to the primary at merge.

Requirements were recorded in `../e1b-queue.md` before tests and runtime edits.
The original 11 queue tests are unchanged. New tests are in
`tests.test_ledger_owner_queue_lifecycle.LedgerQueueLifecycleTests` (component
layer: real in-process threads, no processes, sleeps or polling). Every thread
uses event handoffs and bounded waits/joins. The new module is registered in
layers, stages, timings and the requirement catalogue; E1b-5/6 sources are in
`tests/requirements/e1b-queue.json`.

| Requirement / review item | Test methods in the new lifecycle class | Evidence |
| --- | --- | --- |
| E1b-6, E1b-7 / MED-1 | `test_close_idle_refuses_and_acknowledges_queued_work_idempotently`, `test_close_is_bounded_while_running_and_drains_after_whole_completion`, `test_drain_waiter_wakes_after_final_acknowledgement`, `test_close_wakes_waiting_owner_and_timeout_is_bounded` | RED: missing close/drain/wait API. GREEN: idle close acknowledges and releases queued work; running close returns before release, refuses new admission, preserves whole commit, then refuses/acknowledges queued work; repeated close is safe. Drain times out while blocked, and a waiting drainer wakes after final acknowledgement. Invalid/unbounded timeout values are rejected. |
| E1b-5, E1b-7 / MED-2 | `test_operation_error_is_plain_result_and_owner_serves_next_request`, `test_base_exceptions_stay_on_owner_and_are_acknowledged` | RED: KeyError reached owner and stopped the pump; KeyboardInterrupt/SystemExit reached the caller. GREEN: ordinary exception becomes a fresh plain operation_rejected result, no exception is retained on the handle, and the next request completes on the same owner. Both process-control exceptions reach only the owner; custody and handled acknowledgement still complete. |
| E1b-3 / MED-3 | `test_offer_wakes_waiting_owner_without_polling`, `test_close_wakes_waiting_owner_and_timeout_is_bounded` | RED: work wait/signal API absent. GREEN: a waiter is inside Condition.wait before offer/close; it wakes by notification (Condition.wait returns true), rather than timeout. Offer permits execution and close returns false to the idle owner. Zero timeout returns immediately; finite timeout is mandatory. |
| E1b-3 / LOW-3 | `test_overflow_refusal_counters_are_bounded_without_close`, `test_refusal_caller_buckets_are_bounded_and_preserve_totals` | RED: 100 caller buckets instead of 16, independently of shutdown. GREEN: both overflow and closed refusal paths keep 15 individual labels plus reserved other, with exact totals of 100. No external reason-code set changes. |
| E1b-3, E1b-4 / LOW-1, LOW-2 | `test_documented_owner_binding_and_deadline_precedence` | PASS before and after; documented rather than changed. Even an empty execute_next binds the first owner permanently, so startup must execute only on its final owner thread. Deadline wins when both expiry and abandonment are observed before the first refusal; an already counted cause is retained. |
| E1b-4 / LOW-4 | Existing `LedgerOwnerQueueTests.test_reply_collected_after_deadline_answers_busy` | PASS unchanged. Documented rather than changed: an on-time result collected at/after its deadline answers busy. Future retrying adapters must keep operations idempotent. |

E1b-3/4/7 original custody, fairness, deadline and handled assertions all remain
passing; their full test mapping is in `../e1b-queue.md`. LOW-3 is fixed;
LOW-1/2/4 preserve the reviewed behavior with explicit documentation and tests.
`closed` is a private queue counter cause: callers still receive their selected
existing busy reason. Normal operation errors return exactly
`{"ok": false, "reason_code": "operation_rejected"}` with a fresh dictionary per
wait; exception text and tracebacks do not cross to callers. Deadline/abandonment
still take precedence over delivering a result.

### Red-first and assertion accounting

The first new-module red run reproduced caller KeyboardInterrupt and terminated
the worker. Before any runtime edit, the new fixture was corrected to turn a
caller BaseException into an assertion failure; the identical expected-result
assertion remains. The old-to-new pairing is in `assertions.md`. No assertion
from the reviewed commit was edited or removed.

The completed pre-implementation red run executed 10 tests: four failed subcases
(including both process-control exception cases) and six errors. It reproduced
missing shutdown/work APIs, owner error propagation, and unbounded refusal
buckets. Worker 0.110 s, discovery 2.674 s, preflight 7.048 s;
**metadata_errors []**, **budget_failures []**. Owner-binding/deadline precedence
was already passing and is intentionally documented, not claimed as newly fixed.

### Named verification

Using the designated test interpreter as `$PY`:

```sh
"$PY" -B scripts/parallel_tests.py --jobs 4 \
  --module codex:tests.test_ledger_owner_queue \
  --module codex:tests.test_ledger_owner_queue_lifecycle \
  --module codex:tests.test_service_queue \
  --module codex:tests.test_ledger_waits \
  --module codex:tests.test_runtime_release
```

36 tests: **35 passed**, one expected release assertion failed. All registered
E1b-3/4/5/6/7 requirements report **passed**; **metadata_errors []**,
**budget_failures []**, no guard violations. Worker wall 0.343 s, job-seconds
0.917 s, discovery 2.656 s, preflight 6.746 s.

| Module | Result | Worker seconds |
| --- | --- | --- |
| `tests.test_ledger_owner_queue` | PASS, 11 unchanged tests | 0.178 |
| `tests.test_ledger_owner_queue_lifecycle` | PASS, 10 new tests | 0.180 |
| `tests.test_service_queue` | PASS, 7 unchanged tests | 0.107 |
| `tests.test_ledger_waits` | PASS, 5 unchanged tests | 0.117 |
| `tests.test_runtime_release` | 2 PASS; expected FAIL in `test_current_runtime_is_recorded_for_the_current_version` because runtime digest differs from unchanged catalogue | 0.335 |

After strengthening wakeup assertions to require notification rather than
expiration, reran only the lifecycle module with the same named-module runner:
**10/10 PASS**, worker 0.160 s (test body 0.004 s), discovery 2.679 s, preflight
6.750 s; **metadata_errors []**, **budget_failures []**, no guard violations.
That partial run reports E1b-3/4/7 as not_run because their original-module
checks were unselected; those checks passed in the five-module run above.
The timing registry records the final lifecycle worker measurement. No test
or full run remains in flight. `git diff --check` passes.

### Files and remaining scope

Changed files in this fix round:

- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/ledger_owner_queue.py`
- `packages/codex-agents/tests/test_ledger_owner_queue_lifecycle.py` (new)
- `docs/requirements/e1b-queue.md`
- `docs/requirements/e1b-queue/STATUS.md`
- `docs/requirements/e1b-queue/assertions.md`
- `tests/requirements/e1b-queue.json`
- `scripts/test-layers.json`
- `scripts/test-stages.json`
- `scripts/test-timings.json`
- `scripts/test-requirements.json`

No collector/service wiring, schema, durable record shape, public reply shape,
external reason-code inventory, or operation set changed. Backup/restore/move
and record-enumeration modules are therefore outside this round; the closed
cause exists only inside queue refusal counters. Queue bounds cover callback
references; trusted adapters remain responsible for bounded payload sizes.
Close is bounded by retained capacity and never waits for the running operation;
drain waits within a caller-supplied finite timeout. A permanently blocked
callback cannot be canceled or declared drained.

Not run: full suite or affected-set run (forbidden by scope), ledger/service
integration, process/socket fixtures, PTYs and threaded stress (outside this
standalone fix round). No required fix-round check needs sockets or PTYs, and
none was blocked on them. E1b-1/2/8 and service-level E1b-5/6 integration remain
future work; this round defines and tests the standalone queue APIs only.
