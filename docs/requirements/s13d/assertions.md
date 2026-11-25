# S13d assertion changes

- `BackoffTests.test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks`:
  old backoff-growth assertion compared wall time between `begin` log events;
  replacement keeps `waits[1] > waits[0]`, derives waits from committed
  `next_start_at_ms - outcome_at_ms`, and additionally requires exactly
  `[300, 600, 600, 600]`. The old real-time 2 s park window could contain
  fewer than three slow child starts; a fixture-only policy clock now advances
  only after each acknowledged failure, to its committed retry deadline.
  The >= 3 failures, exact parked reason/command, no attempts after a new wake
  publication, continued parked status, owner-only unpark and fresh streak
  assertions all remain. The fixed 2 s sleep after publication is replaced by
  advancing beyond the park deadline and awaiting a completed empty claim pass.
  **Continuation fix (A/B):** as committed in `17c946a8` the rewrite failed
  24/24 under load with "failure was not recorded": the collector marks the
  stopped project due on its own real clock after the policy clock was frozen,
  so the frozen clock never reached the due mark. The fixture now waits for
  the wake row's `due_at_ms` and advances the policy clock to it before the
  loop. No assertion is changed or removed by this fix; it adds one wait
  ('never marked due'). Fixed rewrite: 8/8 beside `root:test_install` at
  `--jobs 16`; the original wall-clock test failed 2/24 at base and 2/8 on
  this branch (`waits[1] > waits[0]`: 1.640 s vs 1.645 s, child start time
  dominating the 300/600 ms backoff).
- `GitExecutableTests`: `git_plumbing.git_executable(...)` ->
  `self.resolve_git(...)` at the existing assertions and initial assignment.
  Expected values and assertion predicates are unchanged.
  Calls use a fresh-pin helper because each mocked installation describes a
  new process; it clears the cache before resolution and at cleanup. Cache
  reuse and per-call root exclusion have separate new regressions.
- `ProjectServiceStartupTests.stop_hosts`: no assertion changed; only
  ProcessLookupError is suppressed for a PID that exited after enumeration.
- E2c-3's two 50 ms maxima, indexed query plan, 1,000 claimed rows, batch size
  and flatness comparison are unchanged.

## Fix round (review r1, 2 MED)

- `test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks` (strengthened, nothing removed): old -> new. Old (this
  branch): after each acknowledged failure, jump the policy clock straight to the stored `next_start_at_ms`. New: before
  that jump, advance the policy clock to `next_start_at_ms - 1`, await a claim pass whose `now_ms >= deadline - 1`, then
  assert (a) no claim with `claimed > 0` since the advance, (b) no new `begin` attempt, (c) the failure count is
  unchanged; only then advance to the deadline and wait for the next failure. Reason: the review showed a starter that
  retried early, or passed a future timestamp to `claim_due_starts`, still passed. All earlier assertions (>= 3 failures,
  exact `[300, 600, 600, 600]` waits, parked line, no attempt after a wake publication, owner-only unpark, fresh streak)
  are unchanged. This is at least as strong as the original wall-clock version, which could only observe growth of the
  begin-to-begin gap. Mutation: `claim_due_starts(now_ms=_now_ms() + 1, ...)` in `project_start.py` turns it red
  ("a project was claimed before its retry deadline", `claimed: 1`); restored afterwards.
- No other existing assertion changed. New tests: `GitPinRevalidationTests` (8) in `test_s13d_regressions.py`
  (removed, replaced untrusted, replaced trusted, symlink retargeted trusted/untrusted, permission change, all gone,
  unchanged pin: 2 trust checks over 36 calls).
