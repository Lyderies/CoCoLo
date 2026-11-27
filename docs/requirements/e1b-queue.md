# E1b owner queue, slice A

Base: `7322d61f` (0.8.64 plus U3/U4); candidate 0.8.65. Source: PLAN-0.4.0
E1b-3/4/7 and the p38 inventory, slice 6. This slice supplies a standalone
same-process queue, with trusted callbacks and an explicit `execute_next()` owner
pump. It does not open a ledger, start a service, or change existing callers.
E1b-1/2/8 and collector/service wiring remain later work. The fix round below
adds the standalone E1b-5/6 error and shutdown contracts.

## Contract and tests

All names below are methods of `tests.test_ledger_owner_queue.LedgerOwnerQueueTests`.

| ID | Clause | Test |
| --- | --- | --- |
| E1b-3 | Queued and running requests retain global, project and project-count custody until handled, including abandonment and caller expiry. No lock spans callback execution. | `test_running_abandonment_keeps_global_and_project_custody`, `test_abandoned_queue_entry_is_handled_without_execution`, `test_running_timeout_keeps_custody_until_whole_completion` |
| E1b-3 | FIFO within each project, one turn per active project per round; one project's backlog cannot take another project's share. Reuse ProjectFairQueue scheduling and ProjectRequestQueue custody rules without changing them. | `test_project_backlog_yields_turns_and_preserves_fifo`, `test_running_project_keeps_project_count_slot` |
| E1b-3 | Overflow immediately raises the caller-selected existing busy reason and increments exact per-caller/per-cause counters; snapshots contain counters, not work. | `test_overflow_is_named_counted_and_snapshot_is_detached` |
| E1b-3 | Limits are validated constructor configuration; defaults are named once and sized by decision 304. Foreign/copied handles and another owner thread cannot consume work. | `test_limits_and_handle_owner_validation` |
| E1b-4 | Admission uses the earlier of the caller's absolute monotonic deadline and the configured 800 ms maximum. Waiting uses only its remaining budget; expiry refuses once and never recycles unfinished custody. | `test_wait_uses_remaining_deadline_and_counts_once`, `test_expired_queued_work_is_skipped_and_acknowledged`, `test_running_timeout_keeps_custody_until_whole_completion` |
| E1b-4 | Once execution begins it runs whole even after expiry. A late result or an on-time result collected after its deadline answers busy, never success. | `test_late_execution_commits_whole_and_answers_busy`, `test_reply_collected_after_deadline_answers_busy` |
| E1b-7 | Each accepted handle exposes a `handled` Event set after execution/skipping, custody release and outcome publication, independently of a caller timeout. Tests await it, using events and a controlled clock rather than sleeps or polling. | `test_running_abandonment_keeps_global_and_project_custody`, `test_running_timeout_keeps_custody_until_whole_completion`, `test_late_execution_commits_whole_and_answers_busy` |

Slice A used provisional limits of 16 retained requests, 8 projects and 2 per
project. Slice B adopts decision 304 after E0b-v2 and LOCKHOLD: **12 retained
requests, 8 projects, 6 per project, 800 ms maximum wait**. These live in
`DEFAULT_LEDGER_QUEUE_LIMITS`; `LedgerOwnerQueue(limits=...)` overrides them.
The count bound covers callbacks/references, not arbitrary callback payload
bytes: only trusted internal callers may submit bounded work.

`offer(project_id, operation, caller=..., busy_reason=..., deadline_ns=...)`
accepts a callable, never wire-selected code. Busy reasons are the existing
`account_busy`, `open_account_busy`, or `operation_rejected`; queue causes are
internal counters (`queue_full`, `project_full`, `project_limit`, `deadline`,
`closed`, and `abandoned` when an abandoned caller still requests an answer).
Counters count one terminal refusal per accepted request and each refused offer.
Abandonment alone is not a refusal. Calling `wait()` on an abandoned handle
answers busy and records its refusal once. Completed handles belong to callers;
the queue releases its references and slots before setting `handled`.

The owner pump binds to its first calling thread, including an empty call.
`wait_for_work(timeout)` supports blocking owner waits without binding it.
Operation Exceptions return a plain error result; process-control exceptions
stay with the owner, as defined below. No database, file or thread creation
occurs at module import or queue construction.

The unit harness forbids blocking thread waits (TF-12). Running-state tests use
an event handoff to a synchronous frontend continuation inside the operation;
this deterministically exercises the same custody transitions. `handled` waits
observe ready events, and the pending caller wait is tested with a controlled
Event wait and clock. The fix-round component module adds event-coordinated real thread
lifecycle evidence; ledger/service contention remains later integration work.

## Fix round contract (review 040, base 7322d61f)

- E1b-6 / MED-1: `close()` atomically stops admission (existing caller busy
  reason, internal cause `closed`), signals an idle owner, and is idempotent.
  A running callback completes whole; only then are queued callbacks refused,
  acknowledged and released without execution. With no running callback close
  drains immediately. Close never waits for a callback and visits at most the
  configured retained-request bound. `drain(timeout)` closes and waits at most
  the supplied finite nonnegative seconds for all acknowledgements, returning
  false on timeout and true once drained; it cannot cancel a running callback.
- E1b-5 / MED-2: catch only Exception from operations. Return a fresh plain
  `{"ok": false, "reason_code": "operation_rejected"}` result, retaining neither
  the exception nor its traceback. The owner continues to the next request.
  KeyboardInterrupt/SystemExit propagate only on the owner; their handle is
  acknowledged with the same plain error result, never a false success.
- E1b-3 / MED-3: `wait_for_work(timeout)` uses a Condition and a finite
  nonnegative seconds timeout; returns true for pending work, false for timeout
  or closure. Offer and close notify waiters. Waiting does not bind the owner.
- E1b-3 / LOW-3: refusal counters retain at most 16 caller buckets, including
  reserved `other`; the first 15 non-reserved labels keep individual counters and
  subsequent labels aggregate into `other`, preserving totals without eviction.
- LOW-1 documented: first execute_next (even empty) permanently binds the owner;
  startup code must call it only on the final owner thread.
- LOW-2 documented: deadline wins over abandonment if both are observed before
  a refusal is counted. An existing terminal refusal is never counted twice.
- LOW-4 documented: an on-time result collected at/after its deadline still
  answers busy. Future retrying adapters must keep operations idempotent.

Fix-round tests are in `tests.test_ledger_owner_queue_lifecycle`; its component
layer permits event-coordinated real threads. No sleeps or polling are used.
