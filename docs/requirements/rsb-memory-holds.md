# RS-B memory holds (B2; decision 129.1 A)

Base: `4a00f22d`, version 0.8.62. VERSION and the runtime catalogue stay unchanged.
Authority: supervisor-slices B2 and RS-B draft 2 §6, draft 3 §3/§5/§7,
draft 4 §4. Existing B3 withdrawal, B6 close and S2/S3 fencing remain authoritative.

**RS-B-4:** Driver entries and preparation slots have instance-bound durable
holds. Registration and its hold commit atomically; preparation intent commits
before submit. The driver records release only after actual retirement. A
successor releases a stopped instance's rows as `service_stopped` only upon
positive journal-lock evidence; missing locks and hung owners retain holds.
Exhausted local fences hand custody to the sweep before detaching memory.
Tombstones cannot be taken again. No schema version or historical-memory
reconstruction is needed: the existing columns and release kinds suffice.

| Matrix row | Required behavior | Tests in `tests.test_memory_holds` unless qualified |
|---|---|---|
| M1 | Kill before registration commit, after preparation intent, after discard, after retire; reopen and recover once | `tests.test_memory_holds_crash.MemoryCrashTests.test_M1_transition_kills`, `test_M1_entry_deleted_before_release_record`; real writer ordering in `tests.test_memory_holds_lane` |
| M2 | Unknown cleanup beyond 12 attempts retains preparation and entry capacity | `test_M2_unknown_cleanup_keeps_both_holds`, `test_M2_abandon_does_not_drop_entry_before_preparation_retirement` |
| M3 | Registration/preparation versus close, both serialized orders | `test_M3_close_and_takes_both_orders` |
| M4 | Real hung subprocess holding lock, then missing lock: no release | `tests.test_memory_holds_crash.MemoryCrashTests.test_M4_hung_and_missing_lock` |
| M5 | Repeated recovery is idempotent; graceful stop retires own memory before releasing lock | `test_M5_recovery_once_and_graceful_stop`; `tests.test_memory_holds_lane.MemoryLaneTests.test_M5_graceful_stop_retires_rows_before_unlock` |
| M6 | Closed and idle registered entries retire without client abandonment; describe says run_closed | `test_M6_autonomous_retirement`; `tests.test_memory_holds_lane.MemoryLaneTests.test_M6_denied_closed_run_retires_without_another_client` |
| M7 | Refused registration removes entry while preserving independent withdrawal duty; faulted writer sends once | `test_M7_registration_refusal_preserves_duty`; real-lane refusal and faulted-writer tests in `tests.test_memory_holds_lane`; existing B3 wire/store regressions |
| M8 | Submit raises after intent: submit_refused compensation, entry retained, no slot consumed | `test_M8_submit_refused_compensates` |
| I1 | 1,000 interleaved owner/writer observations across three identities; legal transition states, equality at quiescence | `test_I1_interleaved_observations`, `test_I1_take_and_release_reply_interleavings` |
| L4 | Ten refused fences: all four never-launch predicates, each violated independently; lease remains armed | `test_L4_never_launch_predicates` |
| L6 | Durable handoff, detach worker and lane, live sweep sees armed lease without restart | `test_L6_live_handoff`; real lane detachment and same-service fencing tests in `tests.test_memory_holds_lane` |
| L7 | Kill after handoff and after detach before release; sweep ownership survives; memory service_stopped | `tests.test_memory_holds_crash.MemoryCrashTests.test_L7_handoff_kills` |

Additional checks cover validator refusals, tombstone reuse and B7 tracking.
The four-state accounting oracle includes queued and committed-but-unobserved
requests, fenced preparation intent and releasing handoff, as clarified by draft 4.
Crash tests use parent-directed SIGKILL after a pipe checkpoint; M4 uses a real
flock holder. No timer, missing row or missing file is stopping evidence.

B7 reports all tracked kinds, eligibility and memory-blocked close reasons.
S6 historical memory remains absent; only its temporary untracked display is
superseded. Wider B1 normal-run and B6 recurring-arrival budgets remain explicitly
unmeasured; the memory transactions themselves are measured without claiming
those wider budgets. See STATUS.md and assertions.md for evidence and exact scope.
