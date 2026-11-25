# E1b wiring: sole ledger owner

Unit 3.8 slice B starts at `afb6f610` (0.8.66). VERSION and the runtime digest
catalogue are merge-owner work and stay unchanged here. Authority: PLAN 0.4.0
E1b, inventory slices 7–8, accepted standalone queue (decision 298), measured
queue defaults (decision 304), and bounded holds (decision 305).

| Requirement | Contract | Matching checks in `tests.test_ledger_owner_wiring` |
| --- | --- | --- |
| E1b-1 | The opening thread builds account views and executes every ledger statement, including startup, collector, wake, starter, sweep, restart and close work. The queue first binds on that thread. | `test_all_sql_from_open_through_close_has_one_owner`, `test_sql_trace_without_transport_covers_wakes_sweeps_restart_and_close` |
| E1b-2 | An operation and its transaction-time rechecks remain one request; no transaction or cursor crosses the handoff. | `test_transaction_rechecks_are_one_request_and_revocation_rolls_back`, `test_real_original_open_rechecks_and_rolls_back_within_one_request` |
| E1b-5 | Passing handler errors retain current wire answers and the next request succeeds; fatal defects reach the host's C10 path. | `test_handler_refusal_preserves_answer_and_next_request_runs`, `test_fatal_sql_error_reaches_owner`, `test_real_account_handler_keeps_failure_answer_and_processes_next_request`, `test_sweep_failure_keeps_existing_containment_and_owner_serves_next` |
| E1b-6 | Health reads published state without an owner wait. Shutdown stops admission, finishes the final request, records exit, then closes on the owner. | `test_paused_owner_leaves_project_health_responsive`, `test_exit_is_recorded_after_final_handled_request` |
| E1b-8 | Signing, policy and filesystem preparation stay outside requests where current checks allow; current authority is checked again before commit. | `test_preparation_does_not_occupy_owner_and_commit_rechecks_authority`, `test_signature_preparation_yields_owner_and_changed_generation_refuses_commit` |

The trusted adapter submits whole operations to the existing fair queue. Offline
LearningLedger callers retain their current API. Account services are built and
pumped by the opening thread. Queue defaults are 12 retained requests, 8 projects,
6 retained requests per project and an 800 ms maximum caller wait. Running late
work finishes whole. A caller on another thread that stops waiting at its deadline
hears the existing busy answer (E1b-4). The opening thread runs its own requests: its
deadline refuses a request only before it starts (nothing consumed or registered), and
once a request has run its answer is delivered, as a direct call's was before the
wiring, so a deferred completion or begin ticket is never answered busy and lost. The
production collector opens its connection with `check_same_thread` (plus a guarded
`interrupt`), so SQLite itself refuses every other thread. The host loop wakes on the
queue's work signal and handles up to the queue's whole capacity (12) of ready requests
per pass. No new wire operation, refusal code or schema is introduced.

Tests use events and handled acknowledgements. Two existing default-value assertions and one explicit-cap fixture are updated
for decision 304; exact replacements are in `e1b-wiring/assertions.md`.
Additional checks cover default limits and bounded published health eviction.

The opening thread remains the host's main thread. Collector startup and stop
pump the queue while joining producers, avoiding startup/shutdown deadlock.
Account ingress sends each service request and deferred completion through the
owner; transaction rechecks stay within that request. Full admission answers
new account frames with their existing busy reply, while already-deferred jobs
retain custody and retry on a later turn. Wakes and starter operations use the same
facade. Existing caller error boundaries still decide containment; exceptions
cross only as plain type/message/code values, and process-control failures stay
on the owner. Fatal collector handlers still reach C10.

Preparation consists of read-only owner capture, off-owner signature/file work,
then one final transaction request. Cold hook binds and owner-record commits use
this split; known hook binds keep their fast path. The final transaction retains
current signer/owner generation, row and project checks. Original account opens
continue using their existing signature pool and final authorization/policy
checks. Filesystem durability, store-fence checks and final policy/signer reads
that decide a commit remain with that transaction. Capability publication needs
the registered project identity and runs outside the owner request after it.

Published project health is an eight-entry replaceable working set, not a
registration or admission limit. New projects evict the oldest publication;
a cold project (nothing published yet) waits at most the old 250 ms health wait for its one
refresh, so registered projects answer in full and unregistered ones keep `project_unregistered`;
only a late or refused refresh returns the existing `ledger_busy` shape. Each project has at most one
outstanding refresh. A write never runs display reads: its request only marks the publications it may
have changed (its project, or all for unlabelled requests and changes made outside a request), so
display reads cannot delay or fail a committed operation. Values that depend on the time stay as of the
read: hook freshness is recomputed from the published last-accepted time at every read, and a
publication that is marked or older than 1 s (slot deadlines, live holds and bindings) is refreshed first
with the same 250 ms wait. Frontend file counters are merged without SQL. Queue refusals join the existing `ledger_waits`
counters without a new wire code or field.
