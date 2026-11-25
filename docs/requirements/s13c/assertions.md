# S13C assertions

No assertion was removed, and no test was renamed, moved or deleted. Test counts are unchanged (driver 109, start 38,
host_slots 10, lost_run_settlement 27). The pairs below are the only changed assertions and fixture behaviours.

## Changed assertions (old -> new)

- `test_project_start.StartTests.test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client`, serving check
  (reason: the bound was a fixed second after the interval, which two real host processes cannot meet under load; S13C-2):
  - old: `served` = the time both `serving('never')` and `serving('stopped')` answered true, polled until `deadline`
    (`ready + STARTER_CHECK_SECONDS + min(STARTER_TICK_SECONDS, STARTER_CHECK_SECONDS) + 1.0`);
    `assertIsNotNone(served, 'the due projects were not serving within one interval')` and `assertLessEqual(served, deadline)`.
  - new: each project's serving time is recorded separately, polled until the later `begin` plus
    `project_host_client.START_TIMEOUT_SECONDS` (the starter's own start deadline, 8 s);
    `assertEqual(['never', 'stopped'], sorted(served))` and, per project, `assertLessEqual(served_at, begin_at +
    START_TIMEOUT_SECONDS)`. The assertions that each start BEGINS within one interval of the collector being ready
    (`assertLessEqual(begun[0]['t'], deadline)`, unchanged) and that the idle project is never started stay as they were.
  This is a looser serving bound (about 1.4 s -> 8 s from `begin`); it is the product's own deadline for a start, and
  `test_E2c_4_*` and the timeout tests keep proving that a host that does not serve by it is failed and killed. It was
  chosen over a derived multiple of the interval because the interval is a test constant (0.2 s) with no relation to host
  start-up time.

## Changed waits (old -> new), assertions kept

- `test_lost_run_settlement.LostRunAfterCrashTests.test_a_crashed_hosts_child_that_still_runs_keeps_its_capacity_until_it_ends`:
  old: `while time.monotonic() < now + 6: assertEqual(1, self.counted()); sleep(0.25)` (six one-second sweeps).
  new: `assertEqual(1, self.counted())` is checked after each of at least six sweeps the next host has collected (counted by
  wrapping `RunDriver._settle_lost`), each wait bounded at 60 s. Same assertion, same or more sweeps, at the 0.1 s sweep.
- `test_missing_or_unreadable_launch_evidence_falls_back_to_the_timed_rule`: old: `time.sleep(5)  # Several sweeps of the
  new host`; new: wait until five sweeps are collected (`through_sweeps(5)`), then the same assertions. Five seconds were
  five sweeps at the product cadence.
- `crash_while_running` / that test: `count_sweeps()` is installed before the next host starts (new helper, no assertion).

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
