# B4s status (2026-11-24, review r1 fix round done: waiting for the primary)
## Review r1 (Codex NOT-READY on e57f84a5) -> fix round (one round; red evidence in red.md, changes in assertions.md)
- HIGH 1 delayed replay after account purge: settle_lost_run writes replay evidence (run id, claim id, lease digest;
  receipt = its digest) on the binding's run_holds tombstone; after OP-2 purges the binding, settle_lost_run and
  lost_run_closure answer from it (account_lost_run._known_claim/_run_state). A verified local settlement (outbox
  `settled`) finishes the journal when the account cannot answer (project_driver._settle_entry). Tests r1_H1 x2.
- HIGH 2 admitted-unbound open_binding leak: record_account_settlement releases it `claimed` (lease branch). r1_H2, A2.
- HIGH 3 (upgrade) old pre-settlement `settled` journals: each `settled` entry is checked once per service against
  the store (`_recover_settled`); an outstanding (`releasing`) hold is settled through `_settle_entry`; unrecovered
  entries are kept from `journal.prune(keep=...)`. Test r1_H3 (old settled journal).
- HIGH 3 (process mode) crash after claim admission before the first journal write: NOT FIXED, test written red and
  skipped with the cause: settle_lost_run needs the identity digest, which only the never-written journal entry holds
  (the store keeps none; the fence reply shape is fixed). Needs a process-mode journal intent before the claim or an
  account settlement taking identity from the launch observation: PRIMARY DECISION.
- MED crashed label: lost_run_closure reads the slot's run_holds release kind; `crashed` -> claim_cleanup_unavailable
  (B5). Test r1_M1.
- MED workspace: _remove_workspace returns confirmed absence; `_settle_lost`, `_settle_own_ended` (keeps the reply in
  run.own_reply until the workspace is gone) and ContainerRecovery._settle_gone write `settled` only then. r1_M2 x3.
- MED readers/validator: _attempt_settled also needs the attempt's register row released by the closure (kind +
  receipt) and the lease's claim id; open_account_settlement answers `settled` only for that (or a purged tombstone),
  else history_conflict; validator LEFT JOINs refuse a settlement naming no hold. r1_M3 x3.
- MED stored blob: _stored_blob(MAX_LOST_RUN_CLOSURE_BYTES) on both closure reads. r1_M4.
- MED RC-3 (B4-9): ResultPersistenceLane.retire_refused drops a job whose lease the store refused for result space;
  the driver ('leasing') retires it, so _retire_unclaimed withdraws the open. RC-3 un-skipped, green.
- Found already failing at e57f84a5: two test_project_holds_schema setups (settlement rows vs B4-7); setups fixed.
Runs (named modules only, each < 70 s): see the commit message / report.

## Earlier (build)
Requirement map: b4s.md. Changed assertions and setups: assertions.md. Red: red.md (de132086). WIP code: 2946ee4b.
Key identity (§9): PROVED (KeyIdentityTests). No schema step (20 stays); the decision-131 partial index was NOT added.
- RS-B-B4-1..8: DONE, green (S/W/D modules plus the cap guard: 47 tests, 41 s).
- A2: setup fixed (bind_result_lease fails once, so the lease stays claim_pending); assertions unchanged.
- StepBoundaryTests (B4-4, 5 tests) and LostLifecycleLimitTests (B4-8) green with no code change.
- lifetime_caps.json lists lost_run_closure.py::checked_lost_run_closure and ::verify_lost_run_closure (BOUNDED).
- Decision 132: account.lost_run_closure added to the three exact op-set pins (81 tests green).
- test_lost_run_settlement restart test: setup now waits for the journal `settled` (a B4-4 replay race); assertions unchanged.
- RS-B-B4-9 RC-3: met in the r1 fix round (see above).
- B0-2 stays skipped by spec.
- Affected run: stopped by the primary (too broad). The primary runs the affected/full set before merge.
Rerun: scratchpad t.sh (env -i allowlist, TMPDIR=/tmp/claude-501/b4s) tests.test_account_settlement
tests.test_account_settlement_wire tests.test_account_settlement_driver tests.test_lifetime_cap_guard
tests.test_lost_run_settlement tests.test_publication_replay tests.test_deadlines_retention tests.test_hold_lifecycle
r1 also: test_project_holds_schema test_project_hold_register test_run_holds test_launch_journal
test_container_launch_journal test_container_recovery test_container_stop test_open_request_lifetime
test_claim_denial_recovery test_result_leases test_project_cleanup test_result_lifetime test_open_withdrawal
test_open_withdrawal_successor test_publication_replay_driver test_publication_replay_proofs test_refusal_steps
test_trial_run_driver_bugs test_project_host_result test_project_results test_active_run_capacity
test_capacity_status_api test_project_driver
