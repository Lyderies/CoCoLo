# B4s status (2026-11-24, builder 2: build complete, waiting for the primary)
Requirement map: b4s.md. Changed assertions and setups: assertions.md. Red: red.md (de132086). WIP code: 2946ee4b.
Key identity (§9): PROVED (KeyIdentityTests). No schema step (20 stays); the decision-131 partial index was NOT added.
- RS-B-B4-1..8: DONE, green (S/W/D modules plus the cap guard: 47 tests, 41 s).
- A2: setup fixed (bind_result_lease fails once, so the lease stays claim_pending); assertions unchanged.
- StepBoundaryTests (B4-4, 5 tests) and LostLifecycleLimitTests (B4-8) green with no code change.
- lifetime_caps.json lists lost_run_closure.py::checked_lost_run_closure and ::verify_lost_run_closure (BOUNDED).
- Decision 132: account.lost_run_closure added to the three exact op-set pins (81 tests green).
- test_lost_run_settlement restart test: setup now waits for the journal `settled` (a B4-4 replay race); assertions unchanged.
- RS-B-B4-9 RC-3: NOT met. With the skip removed it fails; skip kept with the real cause (B3 open-withdrawal gap
  for a run refused at its lease: withdraw_unsent needs a lease, run.persistence is never cleared, the open_binding hold
  stays held). Primary decision: fix it in B3 scope or widen B4s.
- B0-2 stays skipped by spec.
- Affected run: stopped by the primary (too broad). The primary runs the affected/full set before merge.
Rerun: scratchpad t.sh (env -i allowlist, TMPDIR=/tmp/claude-501/b4s) tests.test_account_settlement
tests.test_account_settlement_wire tests.test_account_settlement_driver tests.test_lifetime_cap_guard
tests.test_lost_run_settlement tests.test_publication_replay tests.test_deadlines_retention tests.test_hold_lifecycle
