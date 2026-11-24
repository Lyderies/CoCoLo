# B4s status (WIP checkpoint, 2026-11-24)
Key identity (§9): PROVED. Account `ledger.state.digest_key()` (LearningState) and project result key (CollectorPaths in serve_project, LearningState in open_offline_history) read the same `<state root>/digest.key`; a claim is only admitted after the account verifies the project's sealed lease with its key. Tests KeyIdentityTests (pass on base). No schema step (20 stays; table exists since 18), so the decision-131 partial index was NOT added.
- RS-B-B4-1 closure/new op `account.lost_run_closure` (lost_run_closure.py, account_lost_run.py, account_api/service/host): DONE, store+wire tests green; driver authority test green.
- RS-B-B4-2 outbox (`ProjectHistory.open_account_settlement`, driver `_settle_entry`/`_settlement_step`, lane `account_settlement[_after_stop]`): DONE, OutboxTests green.
- RS-B-B4-3 `record_account_settlement`: DONE, RecordSettlementTests green (V1,V2,V4,V5,V6a,V6b, planted-helper equivalence).
- RS-B-B4-4 order/replay: code DONE; StepBoundaryTests NOT RUN yet.
- RS-B-B4-5 readers (`_attempt_settled`, `_attempt_released`, orchestration_store `_unfinished`): DONE; A3 green.
- RS-B-B4-6 retry/tombstone after purge (settlement row purged with the attempt): DONE, green.
- RS-B-B4-7 validator rows (project_holds_schema): DONE, green.
- RS-B-B4-8 P2-P4 lifecycle (LostLifecycleLimitTests): NOT RUN yet.
- RS-B-B4-9 RC-3 un-skip: NOT STARTED.
Red driver test: A2 premise wrong: at state 'binding' the lease is already bound (settles as a reservation, kind lost). Rehold before the bind (e.g. fail `bind_claim` once) so the lease stays claim_pending.
Next: (1) fix A2 hold point; (2) run StepBoundaryTests + LostLifecycleLimitTests; (3) lifetime_caps.json entries for lost_run_closure.py::checked_lost_run_closure and ::verify_lost_run_closure (1 check each, BOUNDED message size; test test_B4_1_the_closure_past_the_size_limit_is_refused); (4) un-skip RC-3 (tests/test_result_lifetime.py:105), map in docs/requirements/b4s/assertions.md only if B3's end state differs; (5) assertions.md: add 'account.lost_run_closure' to the exact op-set asserts in test_admission_inputs.py:139, test_signature_inputs.py:146, test_signature_job_intake.py:96 (a decided new op; old->new listed); (6) write docs/requirements/b4s.md map; (7) affected run.
Known gap: B0-2 (test_hold_lifecycle.py:131, skipped) expects a lost terminal; B4s writes none by design (oracle S3): leave skipped, flag to primary.
Rerun: scratchpad t.sh (env -i allowlist, TMPDIR=/tmp/claude-501/b4s: long TMPDIR breaks socket paths) tests.test_account_settlement tests.test_account_settlement_wire tests.test_account_settlement_driver; affected: tfvenv python -B scripts/parallel_tests.py --affected 8d4a7ce3
