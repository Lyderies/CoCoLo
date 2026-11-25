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
