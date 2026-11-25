# S13C status

Branch `fix/s13c-headroom`, base 0.8.67 (`ce81bd96`). Test-only; no product code changed, no version bump, nothing pushed.
`metadata_errors: []` in every run. Requirements and findings: [`../s13c.md`](../s13c.md); changed assertions:
[`assertions.md`](assertions.md).

## Results

Sum of the module's fragments' seconds (what the budget check counts). "Loaded" = `--jobs 16` beside `root:test_install` and
`root:test_setup_wizard_direct` (the machine was shared and ran at load 6 to 26, so a single number has about 10% noise;
the base and after runs below were taken back to back with `root:test_install` and `root:test_setup_wizard_direct` within
2% of each other, which is the check that their load was the same).

| Module | Ceiling / budget | Base `ce81bd96`, loaded | After, loaded | After / ceiling |
| --- | --- | --- | --- | --- |
| `test_project_driver` | 520.4 | 602 | 577 | 111% |
| `test_project_start` | 183.4 | 199 | 211 (5 later runs, 3 modules: 185 to 195) | 101 to 115% |
| `test_host_slots` | 60 (layer budget, no debt entry) | 62 | 52 (58 in a heavier run) | 87% |
| `test_lost_run_settlement` | 119.8 | 117 | 110 | 92% |

One module at a time, quiet (serial `unittest`, wall seconds / CPU seconds including children):

| Module | Base | After |
| --- | --- | --- |
| `ProjectDriverTests` (24 of the 109 driver tests) | 45.9 / 38.9 | 43.4 / 37.0 |
| `test_project_start` | 158 / 88 | 124 / 51 |
| `test_host_slots` | 49.7 / 32.2 | 36 (after the idle change) / 30.7 |
| `test_lost_run_settlement` | 85.9 / 59.0 | 66.7 / 47.5 |

Verdict: S13C-1 (80% of ceiling) is NOT met for any of the four modules; every unmet target is disposed to plan step S42
(TR-1 test review and refactor), none is raised or waived here:

| Module | After / ceiling | 80% target | Disposition |
| --- | --- | --- | --- |
| `test_host_slots` | 87% (52 s of 60 s; 58 s in a heavier run) | 48 s, not met | S42 |
| `test_lost_run_settlement` | 92% (110 s of 119.8 s) | 95.8 s, not met | S42 |
| `test_project_driver` | about 111% (577 s of 520.4 s; 93% in a lighter run) | 416 s, not met | S42 |
| `test_project_start` | about 101 to 115% (185 to 211 s of 183.4 s) | 146.7 s, not met | S42 |

`test_host_slots` is back inside its 60 s budget (it was over it at 68 s on 0.8.67) and `test_lost_run_settlement` inside
its ceiling, but neither has the 80% margin. The cost of all four is the fixed per-test product path, not a regression: see
below. E2c-1 is restored to base and is a known load flake attributed to S13e, see "E2c-1" below.

## E2c-1: restored to base, load flake attributed to S13e

E2c-1's assertions are the base `ce81bd96` text ("serving within one interval" from `begin`); nothing is loosened, split or
bounded differently (`git diff ce81bd96` shows no change to them). The cause of its failure under load is project-host start
cost: the host process compiles the runtime itself (`-B -X pycache_prefix=/dev/null`, product flags), so `begin` -> serving
was 0.6 s quiet and 0.75 to 1.3 s loaded against about 1.2 s. That is plan step S13e (`PLAN-0.4.0-v2.md`); until it lands
E2c-1 may fail under load and is a known flake, not fixed here. `StarterBase.setUpClass` still compiles the runtime into the
class cache (fixture only). Two other latency assertions in the module failed once each under load
(`test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks`, `test_E2c_3_claim_batches_...`; see item 3 below); not changed.

## What the profile showed

- 0.8.67 versus 0.8.66, same four modules, same load: no difference beyond noise (driver 545 / 501 s, start 202 / 180 s,
  host_slots 56 / 61 s, lost_run_settlement 114 / 131 s). S1, S2 and S3 added no measurable per-test cost.
- Driver tests are about 3 s each at 85% of one core: an in-process collector, account services and project host; about 36 `git`
  runs (874 in 24 tests, 6.5 s of waiting), 2 to 3 `sandbox-exec` runs, `ps`, `ssh-keygen` sign/verify and one separate
  `dispatch` interpreter. Under `--jobs 16` the CPU is oversubscribed, which inflates the sum by about 1.5 times.
- `test_project_start` is wait-bound (1.3 times inflation, not 1.5): its tests set real probation, backoff, park and recheck
  windows of 1 to 4 s and assert that nothing happens inside them.

## Proposed product steps (not done here, test-only unit)

1. **Collector and project-host idle loops.** `collector_host.py` (`wait_for_work(0.002)` then `account.serve_once()`),
   `ProjectServiceHost.serve(idle_seconds=0.002)` and the harness `server.wait(.002)` run a full pass every 2 ms even when no
   work is queued; each pass drives the account claim and observation jobs, which re-read key files
   (`learning_store._read_key`) and re-classify provider state (`admission_metadata.reset_early`). A sample of the driver
   tests put about half of the non-blocking samples there. Measured effect of a 20 ms harness loop on one class: none
   (wall 43 s to 53 s, CPU 22.7 s to 21.3 s), so this is a suspicion, not a finding: a step should measure CPU per idle
   second of a collector and a host first (S1, `ledger_owner_queue.wait_for_work`).
2. **`git_executable()` runs `temp_roots()` per call**, which started `getconf` 26 times per run (the tests now cache it);
   the product could cache the pinned git for the process.
3. **Test-timing instability that is not this unit's**: `test_E2c_3_claim_batches_are_short_indexed_and_flat_at_100000_settled_rows`
   failed once in a loaded run with a single 62 ms claim outlier against its 50 ms bound (`test_project_start.py`
   `assertLess(max(settled), 0.050)`); `test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks` failed once in a loaded run
   (`failures >= 3` within its 2 s park window, two attempts fit). Both are latency assertions that need a derived bound or a
   retry-on-quiet design; not changed here.

## Not done / next

- The 80% target for `test_project_driver` and `test_project_start` needs fewer or cheaper per-test steps (a shared
  collector and account for tests that do not stop them, or a product-side saving above), which is a test-architecture
  change beyond a harness patch; or an owner decision to keep the budget-debt ceilings and re-record them from a quiet full run.
  The ceilings in `scripts/test-budget-debt-0850.json` were not touched.
- The idle window of `StoredContextRestartTests.test_D1_a_host_holding_recorded_workers_stops_at_the_ordinary_idle_time`
  (8 s) cannot be moved on its clock: `serve_project` builds the host inside the call.
- Rerun: `$PY -B scripts/parallel_tests.py --jobs 16 --module codex:tests.test_project_driver --module codex:tests.test_project_start --module codex:tests.test_host_slots --module codex:tests.test_lost_run_settlement --module root:test_install --module root:test_setup_wizard_direct --save-timings <file>` from the repository root, short `TMPDIR`.
- Last run of all touched modules (the four above, the three settlement modules, `root:test_install`,
  `root:test_setup_wizard_direct`; `--jobs 16`): no failed units, `metadata_errors: []`; it ran under a heavier load
  (`root:test_install` 122 s, `root:test_setup_wizard_direct` 312 s), so its module sums (driver 610, start 220, host_slots 58,
  lost_run_settlement 124, settlement driver / recovery / lifecycle 21 / 38 / 23 s) are the pessimistic end.
- `scripts/test-timings.json`: the four modules' module and per-test entries were refreshed from the loaded run that is the
  `After` column above.
