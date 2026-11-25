# S13C assertions

No assertion was removed, and no test was renamed, moved or deleted. Test counts are unchanged (driver 109, start 38,
host_slots 10, lost_run_settlement 27). The pairs below are the only changed assertions and fixture behaviours.

## Changed assertions (old -> new)

- `test_project_start.StartTests.test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client`, serving check
  (reason: two real host processes cannot be on the clock of a fixed second after the interval under load; S13C-2. Round 1
  fix: the first S13C version widened the bound to the start's 8 s deadline from `begin`, which a 2 s delay between `begin`
  and the spawn passed; that is withdrawn):
  - old (base): `served` = the time both `serving('never')` and `serving('stopped')` answered true, polled until `deadline`
    (`ready + STARTER_CHECK_SECONDS + min(STARTER_TICK_SECONDS, STARTER_CHECK_SECONDS) + 1.0`);
    `assertIsNotNone(served, 'the due projects were not serving within one interval')` and `assertLessEqual(served, deadline)`.
  - new (after round 1 fix): the bound is split at the process boundary and each part is measured from its own event.
    (1) The starter's own part: per project, `spawned_at - begun_at <= SPAWN_BOUND` (1.0 s; the `spawned` event is
    logged when `os.posix_spawn` of the host returns), measured 0.06 s quiet and 0.1 to 0.3 s at `--jobs 16` beside
    `root:test_install` and `root:test_setup_wizard_direct`. (2) The host process: both projects serve within
    `project_host_client.START_TIMEOUT_SECONDS` of their own spawn (`assertEqual(['never', 'stopped'], sorted(served))` and,
    per project, `assertLessEqual(served_at, spawned_at + START_TIMEOUT_SECONDS)`). The assertion that each start BEGINS
    within one interval of the collector being ready (`assertLessEqual(begun[0]['t'], deadline)`, unchanged) and that the
    idle project is never started stay as they were.
  - Mutation (a `time.sleep(2)` before `posix_spawn` of the host, in the test's collector launcher): old base assertion red
    ("not serving within one interval"), the withdrawn 8 s version green (the weakness), this version RED
    ("2.08 not less than or equal to 1.0 : never: the start took longer than 1.0 s from begin to spawning its host").
  - What is looser than the base: the host's own start-up (spawn to serving) is bounded by the product's 8 s deadline, not
    by about 1.2 s from `begin`. The host compiles the runtime itself (`-B -X pycache_prefix=/dev/null`, a product flag set), so
    no test cache can remove that cost: begin to serving was 0.6 s quiet and 0.75 to 1.3 s loaded, past the old 1.2 s.
    A product step to bring the end-to-end bound back is in `s13c/STATUS.md`.
  - Fixture (no assertion): `StarterBase.setUpClass` compiles the runtime into the class's bytecode cache
    (`compileall` with `-X pycache_prefix`) before any test's clock starts.

## Changed waits (old -> new), assertions kept

- `test_lost_run_settlement.LostRunAfterCrashTests.test_a_crashed_hosts_child_that_still_runs_keeps_its_capacity_until_it_ends`:
  old: `while time.monotonic() < now + 6: assertEqual(1, self.counted()); sleep(0.25)` (six one-second sweeps).
  new: `assertEqual(1, self.counted())` is checked after each of at least six sweeps the next host has completed (counted by
  wrapping `RunDriver._settle_lost`; one count per call that returned without raising, so a sweep that raises is not counted), each wait bounded at 60 s. Same assertion, same or more sweeps, at the 0.1 s sweep.
- `test_missing_or_unreadable_launch_evidence_falls_back_to_the_timed_rule`: old: `time.sleep(5)  # Several sweeps of the
  new host`; new: wait until five sweeps are completed (`through_sweeps(5)`), then the same assertions. Five seconds were
  five sweeps at the product cadence.
- `crash_while_running` / that test: `count_sweeps()` is installed before the next host starts (new helper, no assertion).
- `test_host_slots.HostReplacementTests` (`test_M3_...`, `test_M1_...`, `test_D1_...`): old: `thread.join(IDLE_SECONDS + 30)`
  after the steps, i.e. sit through the host's real 4 s idle window (plus the loop's one-second check); new:
  `until_idle(stuck, thread)` moves that host's clock `IDLE_SECONDS + 1` s ahead (`host._clock_ns`, the technique
  `PreparationCleanupLifecycleTests` already uses) and then does the same `thread.join(IDLE_SECONDS + 30)`. Every assertion
  stays: the host is still serving before the move (`assertTrue(thread.is_alive())` in M1), is gone after it
  (`assertFalse(thread.is_alive(), ...)`), its slot is settled, and a fresh host is started by the production path. The
  product's stop rule (`idle_ns() >= idle_stop_ns`, checked by the production loop) is what ends the host.

## Changed fixture behaviour (old -> new)

- `test_project_driver.DriverHarness` (every driver-based class, including the lost-run, host-slot and settlement classes):
  - `project_driver.SWEEP_SECONDS` is patched to 0.1 for each test and restored after it (`sweep_seconds`, opt out with
    `None`). Old: 1.0 (the settlement classes already had 0.1 from TESTSPEED3). No test asserts the one-second cadence
    (grep of `SWEEP_SECONDS`, `_next_sweep` and sweep timing in the four modules; `LRS-5` sets `_next_sweep` itself).
  - `setUpClass` creates `dispatch_pycache` (a temporary folder removed after the class). Old: `None`, so the separate
    dispatch ran `-I -B -S`; new: `-I -S -X pycache_prefix=<cache>`. The dispatch arguments are unchanged.
  - `host_paths.darwin_directory` is `functools.cache`d for the class. Old: a `getconf` process per call.
- `test_account_settlement_driver.SettlementHarness`: its own `setUpClass` (cache) and `SWEEP_SECONDS` patch were removed;
  the same two values now come from `DriverHarness`. The unused `shutil` and `tempfile` module imports went with them.
- `test_lost_run_settlement.LostRunAfterCrashTests.start_host_process`: the crash host runs `python -X pycache_prefix=<class
  cache> crash_host_fixture.py` instead of `python -B crash_host_fixture.py`.
- `test_project_start.StarterBase`: the collector launcher, the `cli.py` runs and the killer run `-I [-S] -X
  pycache_prefix=<class cache>` instead of `-I [-S] -B`. The hosts the starter spawns are unchanged (the product's own
  `-I -B -S -X pycache_prefix=/dev/null`, and E2c-5 still asserts exactly that argv).

## Registration only

`@requirements` gained `S13C-1`, `S13C-2`, `S13C-3` on the tests named in `scripts/test-requirements.json`.
