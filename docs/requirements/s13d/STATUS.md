# S13d status

**Continuation (2026-11-26): A/B done, ready for review.** Worktree base
`ce81bd96` (0.8.67), branch `feat/s13d-idle`. Read "Continuation" at the end
first; the sections before it are the earlier sandboxed Codex session's notes
(their "blocked"/"uncommitted" remarks are superseded). No VERSION or runtime
digest catalogue changes. No install, push, merge, schema or record-shape change.

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

## Continuation (A/B and final checks)

Method: each arm ran `scripts/parallel_tests.py --jobs 16 --keep --module
codex:tests.test_lost_run_settlement --module codex:tests.test_project_start
--module codex:tests.test_project_service_startup --module root:test_install`
from its own tree, unsandboxed, clean env, short TMPDIR, arms interleaved
(rotated order each round). Trees: base `ce81bd96`, Codex `17c946a8`, head
`1da5664e`, no-pacer (head with only PassPacer reverted: collector,
account host/ingress and `serve()` back to the fixed 2 ms wait), final (this
commit with the original E2c-4 test, P2) and fixed (final's E2c-4 fixture fix,
P3). Raw rows: [evidence/ab-continuation.tsv](evidence/ab-continuation.tsv).
Cells are passing runs / runs.

| Phase | Arm | E2c-4 | E2c-9 | idle-stop | lost_run (module) | E2c-3 | L15.1 deletion | wall s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P1 | base | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 60-75 |
| P1 | codex 17c946a8 | 0/8 | 8/8 | 8/8 | **0/8** | 7/8 | 8/8 | 70-116 |
| P1 | head 1da5664e | 0/8 | 8/8 | 8/8 | 8/8 | 7/8 | 8/8 | 77-100 |
| P1 | no-pacer | 0/8 | 8/8 | 8/8 | 8/8 | 7/8 | 8/8 | 76-106 |
| P2 | base | 6/8 | 8/8 | 8/8 | 8/8 | 7/8 | 6/8 | 64-85 |
| P2 | final, original E2c-4 | 6/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 65-94 |
| P3 | base | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 | 53-66 |
| P3 | fixed (committed) | **8/8** | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 52-61 |

E2c-4 in P1 non-base arms is Codex's rewritten test; base and P2 run the
original. idle-stop = `test_a_host_stops_when_its_hint_is_withdrawn_or_it_has_been_idle`.

Findings:
- **E2c-9 and idle-stop did not fail in any of 64 runs** (any arm); no change
  is attributable. They stay known load flakes with no fix here.
- **lost_run**: Codex's lock-only-when-settled change fails
  `test_a_lost_reply_and_repeated_restarts_settle_the_run_exactly_once` 8/8
  (next host finds the lock held); head's `finally` release passes 8/8 and 16/16
  later. Module seconds (summed fragments) are the same at base and head
  (base 123-181, head/final 123-186): the 136.7 s overage is load, not S13d;
  base is over its 60 s budget in the same runs.
- **L15.1 / S13d-3 target**: base failed 3/24 (one `(1, False, 'stopped')`
  deletion-not-ready = the cleanup-pending race; two `ProcessLookupError` in
  `stop_hosts` = the S13d-4 test race); branch arms 0/40. The S13d-3
  regressions copied onto base product code fail 3/3 (lock released while the
  writer still runs) and pass on this branch.
- **E2c-4**: Codex's frozen-clock rewrite failed 24/24 ("failure was not
  recorded"): the collector marks the project due on its real clock after the
  policy clock is frozen, so no claim ever matches. The original wall-clock test
  flakes 2/24 at base and 2/8 on the branch (`waits[1] > waits[0]`, 1.640 vs
  1.645 s: child start time dominates 300/600 ms). With the one-wait fixture fix
  (advance the policy clock to the wake row's due mark) it passed 8/8 under load.
- **E2c-3** (scheduler outliers 51-56 ms vs 50 ms, one production-drain start
  failure): 1/8 in base P2 and 1/8 in each P1 non-base arm; claim-path code is
  unchanged, the test runs on a ledger in the test process; not attributable.
- **PassPacer is not the cause**: no-pacer and head have identical failure
  profiles (E2c-4 rewrite 0/8 in both, E2c-3 1/8 in both, lost_run 8/8 in both).

Decisions:
- **Kept (product):** S13d-2 git cache; S13d-1 PassPacer; S13d-3 host lock held
  through writer drain and final settlement, released in `finally` after the
  one final settlement attempt, plus the failed-constructor bounded drain and
  the lock release in `settle_lost_runs_later`. Evidence: deterministic
  regression red on base / green here, base L15.1 deletion race 1/24 vs 0/40,
  no test worse than base.
- **Reverted:** Codex's `if settled: release` form (it alone broke lost_run 8/8;
  already replaced by the `finally` in `1da5664e`). Nothing else reverted.
- **Kept (tests):** `stop_hosts` ProcessLookupError suppression (base 2/24
  ProcessLookupError, branch 0/40; no assertion changed); the E2c-4 rewrite with
  its due-mark fix. Pairs: [assertions.md](assertions.md) (E2c-4 old wall-clock
  begin-to-begin waits -> committed `next_start_at_ms - outcome_at_ms` waits,
  plus exact `[300, 600, 600, 600]`; fixed 2 s sleep -> acknowledged empty claim
  pass; due-mark wait added; `GitExecutableTests` call-site helper; E2c-3
  unchanged).

Final checks (unsandboxed, TMPDIR under /tmp/claude-501/):
- `tests.test_idle_cpu_budget` 3x: OK, OK, OK (15.4-15.5 s each).
- Named `--module` run of test_lost_run_settlement, test_s13d_regressions,
  test_project_start, test_project_service_startup, test_collector_exit:
  123 tests, 0 failed units, `metadata_errors: []`, `budget_failures: []`,
  43.7 s wall at --jobs 16 (rc 0).
- `--jobs 8 --affected ce81bd96`: 895 s wall, preflight 7.5 s,
  7,767 tests (codex 6,454, root 1,179, claude 134), `metadata_errors: []`,
  `requirement_failures: []`. Failed units: only `codex:tests.test_runtime_release`
  (runtime digest pin, stale by design until release bookkeeping). One budget
  failure: `codex:tests.test_host_slots` 65.3 s vs 60 s (known S13c item). A/B
  3 rounds each beside `root:test_install` at --jobs 8: base 51.4-55.4 s,
  this branch 49.6-51.4 s (summed fragments), so it is load, not S13d.
  The first attempt used a PATH with `/usr/bin` before Homebrew and failed
  `root:test_setup_sh` (setup.sh refuses the macOS python3 shim); it was
  stopped and rerun with Homebrew first. No run is left in flight.
- Not stopped (not this session's): two orphaned processes from an earlier
  builder's rounds in this tree, started 10:54-10:55 before this session
  (a test_project_start starter wrapper and a collector_host); left for the
  primary.

