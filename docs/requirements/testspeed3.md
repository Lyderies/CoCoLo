# TESTSPEED-3: account_settlement_driver over budget under full-run load

Base: 0.8.65 (`c30e2129`), branch `fix/testspeed3`. Owner rule: least test running.

`codex:tests.test_account_settlement_driver` (scenario layer, 60 s budget, 1 fragment, 32 tests) took
61.2 / 63.8 / 60.9 / 62.7 s in the last four full runs (`--jobs 16`, about 490 units) and 49 s alone. Quiet runs pass.

## Requirements

- **TESTSPEED3-1:** `codex:tests.test_account_settlement_driver` and the two modules split out of it,
  `codex:tests.test_account_settlement_recovery` and `codex:tests.test_account_settlement_lifecycle`, each total at
  most 45 s, measured as the sum of the module's fragments' seconds (what the budget check counts), with full-run
  parallelism (`--jobs 16` beside `codex:tests.test_project_driver` and `root:test_install`).
- **TESTSPEED3-2:** no assertion is removed or weakened. Every changed assertion, renamed test, moved test or changed
  fixture behaviour is paired old -> new in [`testspeed3/assertions.md`](testspeed3/assertions.md).

Design requirements that follow from the findings below:

- **TESTSPEED3-3:** the separate dispatch processes of one settlement test class share one bytecode cache outside the
  source tree (`dispatch_pycache`, opt-in), so only the first compiles the runtime; the dispatch arguments are
  unchanged.
- **TESTSPEED3-4:** the review-round class `ReviewRoundOneDriverTests` runs from `test_account_settlement_recovery`,
  and `ClosureAuthorityTests`, `StatusAgreementTests` and `LostLifecycleLimitTests` from
  `test_account_settlement_lifecycle`, on the same harness (imported, not copied); every test runs exactly once, in
  one module or another, and is registered in the layer, stage and requirement tables.
- **TESTSPEED3-5:** the settlement tests wait for a successor host's sweep in tenths of a second: their harness runs
  the project driver's sweep every 0.1 s instead of every second; no deadline, retry or fence rule of the product
  changes, and no test asserts the one-second cadence.

## Findings (profile first)

Per-test seconds (one process, quiet): 25 harness tests of about 1.7 s each plus one of 6 s, 3 of 3-4 s; the seven
`ConfirmFixSweepTests` are milliseconds. Each harness test is a real collector, account services and project host
(threads), one real `dispatch` CLI process, a few `git`, `sandbox-exec` and fake-client starts, and a stop.

1. **Fixed cadence waits.** Eleven waits of about 1.0 s (`wait_for`) in the review-round and closure tests are the
   project driver's one-second sweep (`SWEEP_SECONDS`) after a host restart: the successor finds what the stopped host
   left at its next sweep. About 6 s of the 24 s of `ReviewRoundOneDriverTests`.
2. **No bytecode cache for the separate dispatch.** `DriverHarness.separate_dispatch` starts
   `python -I -B -S cli.py dispatch` every time (about 0.3 s, mostly compiling the runtime, more under load).
3. **The module is simply big.** What is left (fixture setup 0.3 s, open 0.5 s, the dispatch reaching its state 0.5 s,
   the stop) is the product path; 32 tests at that cost are 49 s however fast each step is.

## Fix

- `test_project_driver.DriverHarness.separate_dispatch` takes an opt-in class attribute `dispatch_pycache`: the
  dispatch then runs `-I -S -X pycache_prefix=<dir>` (no `-B`); the class cache is created in `setUpClass` and removed
  after the class (the same technique as TESTSPEED-4 and TESTSPEED2). Classes that do not set it are unchanged.
- The settlement harness patches `project_driver.SWEEP_SECONDS` to 0.1 for the test (TESTSPEED3-5).
- The module is split along its test classes into three modules of about the same length (the harness and
  `requirements` helper stay in `test_account_settlement_driver`; the others import them): driver (settlement
  through the driver, the step boundaries, the confirming sweep tests), recovery (`ReviewRoundOneDriverTests`),
  lifecycle (closure authority, status agreement, lost-lifecycle limits). `test_reply_shape_live` replays all three.

No product code changed, so no runtime digest re-record is needed.
