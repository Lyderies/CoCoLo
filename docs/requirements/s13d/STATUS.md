# S13d status

**PARTIAL — not ready for acceptance.** Worktree base `ce81bd96` (0.8.67),
branch `feat/s13d-idle`. Changes are uncommitted because this session is
sandboxed. No VERSION or runtime digest catalogue changes. No install, push,
merge, live-service access, schema change or record-shape change.

## Requirements and evidence

| Requirement | Tests / measurement | Result |
| --- | --- | --- |
| S13d-1 | `scripts/measure_idle_hosts.py`; existing E1b wiring, queue lifecycle, collector exit; read-only S7 first-response tests | **OPEN.** Real disposable collector exits before readiness because Unix socket bind is denied. No full-host idle CPU baseline, calibrated CPU-budget test or idle backoff has been implemented. Both production 2 ms waits remain unchanged; pumping stays at 12. |
| S13d-2 | `tests.test_s13d_regressions.GitPinTests.test_repeated_git_resolution_validates_candidates_once`; `test_cached_git_still_excludes_each_callers_root_and_its_symlink`; `tests.test_git_plumbing`; `tests.test_hardened_git` | **PASS locally.** Work-count regression red (36 resolutions vs 1), then green. Repeated real resolution cost fell from 36 discovery subprocesses to 1; caller exclusions, including symlinked roots and iterators, remain per call. |
| S13d-3 | `tests.test_s13d_regressions.HostCloseTests.test_host_lock_stays_held_until_writer_drain_and_final_settlement`; `test_faulted_host_keeps_lock_until_threads_stop_then_relinquishes_without_settling`; `FailedStartupTests.test_failed_startup_drains_writer_before_releasing_its_host_lock` | **PASS locally; lifecycle gate blocked.** Original close released the real kernel lock while the writer was still live (red). Lock now survives drain and final settlement. A real runtime/history-writer regression also found a failed-constructor lock leak after that change (red); bounded constructor drain fixes it (green). Actual L15.1 deletion still requires unsandboxed validation. |
| S13d-4 | `HostCleanupTests.test_stop_hosts_tolerates_a_pid_that_exits_after_enumeration`; `tests.test_project_start.BudgetTests.test_E2c_3_claim_batches_are_short_indexed_and_flat_at_100000_settled_rows`; `LedgerStartTests`; modified `BackoffTests.test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks` | **PARTIAL.** Disappearing PID reproduced as ProcessLookupError (red), narrowly suppressed (green). Claim assertions unchanged and green. Crash-loop scenario now uses acknowledged failures and committed retry deadlines, but its real-process validation is blocked before startup. |

## Measurements and individual flake verdicts

- **Git resolution: real repeated cost.** Same test interpreter, trusted executable,
  36 calls in one process. Before changes: 0.128516 s wall, 0.063265 s process CPU,
  36 subprocesses. After: 0.003530 s wall, 0.001857 s process CPU, 1 subprocess.
  These subprocesses discover Darwin temporary directories (`getconf`); they
  are not 36 git commands. Single-call observations were 0.006439 s before and
  0.004606 s after; performance numbers are observations, not timing assertions.
  The lazy one-entry cache is keyed by PID and temp environment; no import-time
  discovery. It retains trusted installation candidates, while caller roots are
  resolved and excluded on every call. Missing trusted candidates fail closed.
- **L15.1 cleanup pending: real product ordering race.** `close()` requested
  asynchronous writer shutdown and immediately dropped the host lock.
  `stop_project_host()` treats that lock's release as stopped, so deletion could
  hit a still-owned history store. Keep the lock through drain/settlement;
  faulted hosts relinquish without settling only after their threads stop.
  Failed construction uses the same bounded drain before relinquishing.
  The existing genuinely-busy-history-writer pending-cleanup assertions remain.
- **stop_hosts ProcessLookupError: test cleanup race.** A process can exit
  between enumeration and SIGTERM. Suppress only ProcessLookupError; preserve
  the bounded wait and final absence assertion. No product change for this case.
- **E2c-4 crash-loop count/window: test timing assumption.** A two-second
  elapsed-time streak can legitimately park after fewer than three slow child
  starts. The fixture now freezes only the starter's policy clock, advances it
  after each committed failure to the stored retry deadline, and checks exact
  300/600/600/600 ms waits before parking. Child processes, enrollment and
  startup timeouts still use their real clocks. The post-publication check waits
  for an acknowledged empty claim pass instead of sleeping. This scenario is
  **not claimed green** until it runs with sockets available.
- **E2c-3 62 ms vs 50 ms: not reproduced; no demonstrated product defect.**
  Ten repetitions of the original indexed/flatness test passed (4.677 s total),
  including both 1,000-due and 100,000-settled phases each time. The greatest
  observed batch was 1.488 ms. Both 50 ms maxima, exact row count, batch-size,
  query-plan and relative-flatness assertions remain. A scheduler/storage
  explanation for the reported outlier is plausible but unproven. Further
  investigation under the original full-run load remains open; no weakened
  CPU-time replacement for the elapsed lock-hold bound was made.

Exact assertion replacements: [assertions.md](assertions.md).

## Validation performed

Only named modules/tests; no full-suite or affected run.

- Final focused named run: **49 tests PASS**, 7.6 s wall / 13.186 job-seconds,
  **metadata_errors: []**. Modules: `tests.test_s13d_regressions` (6),
  `tests.test_git_plumbing` (11), `tests.test_hardened_git` (18),
  `tests.test_project_runtime` (4), `tests.test_ledger_owner_queue_lifecycle` (10).
- Named `tests.test_project_start` + `tests.test_project_service_startup`:
  51 tests, 14.0 s wall / 46.290 job-seconds, **metadata_errors: []**;
  overall FAIL because process/socket setup cannot run here. Startup's 13 tests
  error at collector bind. Starter process cases fail readiness; several
  ledger-only checks pass. This is not process/lifecycle acceptance evidence.
- Isolated E2c-3 budget + `LedgerStartTests`: 4 PASS, 0.676 s.
  The subsequent ten repetitions above retained all original assertions.
- Read-only S7 `test_collector_first_response` and `test_s7_owner_load`, imported
  with this worktree's `tests.support` and runtime first: 7 tests, 3.704 s;
  all 4 owner-load tests PASS; all 3 first-response tests fail before socket
  readiness. No writes to the S7 tree (`-B`).
- Earlier broader named run also attempted `tests.test_ledger_owner_wiring`,
  `tests.test_collector_exit`, `tests.test_service_host`, and
  `tests.test_runtime_release` (57.45 s wall overall). Socket/host process probes
  do not start; deferred signature-worker tests time out waiting/draining;
  process enumeration is also denied by the sandbox. These failures remain
  unverified outside the sandbox. The nontransport owner and collector-main
  tests pass. The runtime-release digest pin fails as expected because runtime
  files changed and the merge owner must update the catalogue/version.
- The first metadata run exposed missing why_not_scenario explanations for the
  new component tests. Added explicit explanations and reran to the clean
  metadata result above; no new test starts a process.
- `git diff --check` PASS. No PTY-dependent checks were required.

## Remaining work / exact reruns

`PY` below means the builder test-venv interpreter; no home path belongs here.

1. In a socket-capable session, obtain the baseline before altering the waits:

   ```sh
   TMPDIR=/private/tmp "$PY" -B scripts/measure_idle_hosts.py --seconds 10
   ```

   The tool starts only its own disposable collector/project processes, waits
   for readiness, warms them, and reports each process's own user + system CPU
   over the idle interval (excluding startup/shutdown and child CPU). It uses
   resource snapshots from inside those actual processes, not `ps` polling or
   a substitute loop. Cleanup addresses only its own Popen objects. Optional
   `--budget RATE` fails on excess CPU; no RATE has been invented or calibrated
   here. After measurement, write/register the requested scenario-layer budget
   test red first, then implement only measured idle changes and record green.
2. Rerun the changed crash-loop scenario and L15.1 through their named modules;
   validate E1b deadlines/ordering/published health and collector exits:

   ```sh
   TMPDIR=/private/tmp "$PY" -B scripts/parallel_tests.py \
     --module codex:tests.test_project_start \
     --module codex:tests.test_project_service_startup \
     --module codex:tests.test_ledger_owner_wiring \
     --module codex:tests.test_ledger_owner_queue_lifecycle \
     --module codex:tests.test_collector_exit \
     --module codex:tests.test_service_host \
     --module codex:tests.test_s13d_regressions
   ```

3. With `S7_TREE` set by the primary to its read-only S7 checkout, run the actual
   S7 checks against this runtime (no S7 changes or merge):

   ```sh
   TMPDIR=/private/tmp \
   PYTHONPATH="$PWD/packages/codex-agents:$PWD/scripts:$S7_TREE/packages/codex-agents/tests" \
     "$PY" -B -m unittest test_collector_first_response test_s7_owner_load
   ```

4. The primary still owns macOS/Linux lifecycle validation, other-provider
   review, affected/full-batch gates, and release bookkeeping. No outstanding
   test run is left in flight. No commit was attempted in the sandbox.

## Files changed

- Product: `packages/codex-agents/native-package/.codex/orchestration/codex_agents/git_plumbing.py`,
  `packages/codex-agents/native-package/.codex/orchestration/codex_agents/service_host.py`.
- Existing tests: `packages/codex-agents/tests/test_git_plumbing.py`,
  `packages/codex-agents/tests/test_project_service_startup.py`,
  `packages/codex-agents/tests/test_project_start.py`.
- New regressions: `packages/codex-agents/tests/test_s13d_regressions.py`.
- Measurement tool: `scripts/measure_idle_hosts.py`.
- Metadata: `scripts/test-layers.json`, `scripts/test-stages.json`,
  `scripts/test-timings.json`, `scripts/test-requirements.json`,
  `tests/requirements/s13d.json`.
- Requirements/evidence: `docs/requirements/s13d.md`,
  `docs/requirements/s13d/assertions.md`, this `STATUS.md`.
