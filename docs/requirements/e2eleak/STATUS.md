# E2ELEAK status — 2026-11-25

Worktree: `cocolo-e2eleak`; branch: `fix/e2e-collector-leak`. Changes remain uncommitted as requested for the sandbox. The eight pre-existing leaked processes were not inspected or signalled. No VERSION, product runtime source, runtime digest catalogue, product record schema or collector operation changed.

The requested `docs/requirements/e2eleak.md` was absent at checkout. It now records E2ELEAK-1 through E2ELEAK-4 from the task's acceptance criteria; the matching source is `tests/requirements/e2eleak.json` and the test catalogue has reciprocal check links.

## Requirement → test → evidence

All names below are methods of `root:test_e2e_process_cleanup.WorldCleanupTests` (scenario layer, integration stage).

| Requirement | Witnesses | Red / green evidence |
| --- | --- | --- |
| E2ELEAK-1 | `test_failure_mid_flow_leaves_no_collector_or_world`; `test_pass_exit_and_timeout_leave_no_survivor`; `test_sigterm_and_hard_owner_death_remove_detached_collector`; `test_discarded_world_is_still_owned_until_cleanup`; `test_lifecycle_parent_sigterm_stops_workers_and_their_worlds`; `test_late_boot_without_ownership_helper_never_runs_product_code` | Before implementation, assertion and exception subcases both failed with **World collector survived flow end** (16.813 s). Later witnesses cover partial construction, timeout, atexit, SIGTERM, SIGKILL, a discarded reference and a late bootstrap. Each World-like host also starts a detached native project-service child with no World path in its argv. Both children and the World directory must disappear. |
| E2ELEAK-2 | `test_reaper_uses_world_marker_and_start_time_across_sessions`; `test_late_boot_without_ownership_helper_never_runs_product_code`; `test_verified_exiting_child_is_not_gone_until_waitpid_releases_it` | The ownership API was absent before implementation (`ModuleNotFoundError` red). Positive control stops only the matching pid/start/World marker. Stale start time, foreign marker, malformed JSON and an unrecorded lookalike remain untouched. A removed bootstrap helper cannot bypass registration. |
| E2ELEAK-3 | `test_drained_stop_precedes_term_and_kill_and_closed_world_refuses_spawn`; `test_drained_stop_uses_world_installed_client_without_starting_collector`; failure/pass/timeout witnesses above | Missing API red before implementation. Green verifies drained-client call → TERM → KILL, idempotence and refusal of registration after closing. A disposable installed-client fixture verifies `shutdown(only_if_drained=True)` uses the World socket without starting a collector. |
| E2ELEAK-4 | `test_runner_reaps_detached_registered_hosts_on_all_exit_paths`; `test_reaper_uses_world_marker_and_start_time_across_sessions`; `test_sigterm_and_hard_owner_death_remove_detached_collector`; `test_lifecycle_parent_sigterm_stops_workers_and_their_worlds` | Missing ownership API red; standalone lifecycle scheduler also had no worker ownership argument. Green uses the real runner for pass/failure/timeout/SIGTERM/interruption, deliberately without a World guardian so the reaper must stop the detached host. Lifecycle-parent SIGTERM covers worker → World → detached native service. |

Detailed run outcomes and durations are preserved in `evidence.json`. This is a selective verification, not a full suite or full lifecycle certification.

## Implementation

- `scripts/e2e_processes.py`: atomic World markers and pid/start records; microsecond kernel birth identity on macOS, boot ticks on Linux; serialized spawn registration/closing; own installed collector client's drained shutdown; verified TERM/KILL and bounded waits; marker-checked runner registry traversal. No command-name/process-table scan. Direct children remain pending through the kernel's exit/reap transition.
- `scripts/e2e_support.py`: strong ownership of every entered World, including discarded scene-build locals; context/finally/atexit and signal unwinding; an EOF-pipe guardian for abrupt owner death; bootstrap self-registration before product code and tracking of Popen children; direct terminal pid recording. World paths are normalized before identity comparison. Registry metadata is stripped from product environments.
- `scripts/e2e_lifecycle.py`: always closes entered Worlds even if scene registration failed; parallel worker groups have their own ownership journal/guardian, with nested World registries. Parent SIGTERM closes the guardian pipe before the thread pool waits for workers.
- `scripts/parallel_tests.py`: assigns a per-worker World registry and reaps it on normal exit, worker timeout and runner interruption/SIGTERM, even when the worker process group has already disappeared.

Signals unwind before ordinary World cleanup takes locks. This avoids re-entering a registration lock from a signal handler. Registration is closed before shutdown begins, so new bootstrap/product work fails closed.

## Verification and limitations

- Initial test attempt hit sandbox denial of `/bin/ps`; the test's liveness helper now uses only exact recorded pids with `os.kill(pid, 0)`. Kernel identity reads via libproc are permitted. The initial short-lived fixture processes expired after 90 s; no pre-existing processes were selected.
- A 262-test named-module run passed in 31.136 s, `metadata_errors: []`, no budget failures. Modules: `test_e2e_process_cleanup`, `test_e2e_strictness`, `test_e2e_run_control`, `test_e2e_recovery`, `test_e2e_learning_guard`, `test_e2e_requirements`, `test_test_framework_guards`.
- A later selective check caught one transient direct-child survivor assertion. The assertion was retained; cleanup was hardened to keep previously verified direct children pending until waitpid confirms exit, including when proc_pidinfo reports them gone first. Fifteen focused repetitions did not reproduce the original failure. `test_verified_exiting_child_is_not_gone_until_waitpid_releases_it` deterministically models the kernel transition and proves the wait. The final post-fix run passed 69 tests in 14.687 s, followed by all 11 new cleanup tests in 14.401 s; both report `metadata_errors: []` and all four E2ELEAK IDs passed.
- Real installed collector/socket/PTY lifecycle flows were not run: this builder's writable roots exclude the account's `~/.cache`, and `/bin/ps` is explicitly denied. The new scenarios use disposable World-like hosts and a disposable installed-client stand-in, with real detached processes, signals, birth identity and runner timeout handling. A primary smoke run with an installed collector is still appropriate outside this sandbox. The actual collector protocol and runtime are unchanged.
- Linux identity handling was not exercised on this macOS host. No full suite, `--affected`, product copier/backup modules or runtime-release module was run: there is no product schema/runtime change, and only named affected modules are authorized.
- No assertion from an existing test file was removed or changed. `assertions.md` accounts for the new fixture helper adjustment and coverage additions. No tests remain running at handoff.

Re-run the affected modules with the supplied test venv's Python (`PY`):

```sh
"$PY" -B scripts/parallel_tests.py --jobs 2 \
  --module root:test_e2e_process_cleanup \
  --module root:test_e2e_strictness \
  --module root:test_e2e_run_control \
  --module root:test_e2e_recovery \
  --module root:test_e2e_learning_guard \
  --module root:test_e2e_requirements \
  --module root:test_test_framework_guards
```

## Files changed

`scripts/e2e_processes.py`, `scripts/e2e_support.py`, `scripts/e2e_lifecycle.py`, `scripts/parallel_tests.py`, `tests/test_e2e_process_cleanup.py`, `scripts/test-layers.json`, `scripts/test-stages.json`, `scripts/test-timings.json`, `scripts/test-requirements.json`, `tests/requirements/e2eleak.json`, `docs/requirements/e2eleak.md`, and this directory's `STATUS.md`, `assertions.md`, `evidence.json`.
