# E2ELEAK status — review round 1, 2026-11-25

Worktree `cocolo-e2eleak`, starting commit `4db46b5a`. Fixed scope: HIGH1, HIGH2,
MED3, MED4, MED6, MED7, LOW8 and LOW9 from the READY-WITH-CORRECTIONS review.
Changes are uncommitted as requested in this sandbox. No VERSION, runtime digest
catalogue, product runtime, product schema, collector operation or process-name scan
changed. Pre-existing leaked processes were not inspected or signalled.

## Requirement → test → evidence

All test names below are methods of
`root:test_e2e_process_cleanup.WorldCleanupTests`. Requirements were recorded before
adding the failing witnesses, and implementation followed the red named-module run.
The complete per-test results and red diagnostics are retained in `evidence.json`
under `review_round_1`, alongside the earlier round's evidence.

| Review item / requirement | Test | Red → green evidence | Changed files |
| --- | --- | --- | --- |
| HIGH1 / E2ELEAK-2, -3 | `test_concurrent_world_spawns_and_boot_registration_do_not_serialize`; `test_spawn_racing_close_is_recorded_and_swept_or_refused` | Eight spawns took 1.272 s against the 0.85 s bound; close could not reach its final pass while a spawner held the lock. Both now pass. Eight concurrent spawn and bootstrap registrations each meet the bound. The final closing boundary waits for an in-flight spawn, sweeps its child and refuses a subsequent spawn. | `scripts/e2e_processes.py`, `scripts/e2e_support.py`, cleanup tests |
| HIGH2 / E2ELEAK-3 | `test_graceful_drain_can_spawn_a_child_and_leaves_no_survivor`; racing-close test above | Red: `RuntimeError: e2e World is closing` inside drained stop. Green: drain spawns a tracked child and cleanup reaps it. Closing is set only after drained stop and the TERM wait, under the exclusive registration lock, followed by a fresh survivors pass before KILL. | both harness files, cleanup tests |
| MED3 / E2ELEAK-2 | `test_reaped_popen_and_dead_survivors_prune_records_but_detached_live_remains`; `test_boot_owned_popen_prunes_record_after_wait_and_poll` | Red: both wait/poll retained records; dead survivor record remained; boot-owned wait retained its record. Green: both Popen wrappers prune matching records on wait/poll, survivors prunes dead identities, and a live detached process retains its record. The existing verified-exiting-child waitpid witness also passes. | both harness files, cleanup tests |
| MED4 / E2ELEAK-3 | `test_drained_client_discovery_is_cached_and_skipped_without_socket`; existing `test_drained_stop_uses_world_installed_client_without_starting_collector` | Red: two rglob calls for two drains. Green: one discovery, cached installed path reused, zero discovery without a socket; installed-client shutdown still passes. | `scripts/e2e_processes.py`, cleanup tests |
| MED6 / E2ELEAK-2, -4 | `test_reaper_and_world_close_refuse_directory_outside_expected_cache`; existing matching reaper/runner/guardian scenarios | Red: reaper deleted the outside-cache sentinel and wrong-prefix directory. Green: registry reaping, direct cleanup removal and World exit preserve outside-cache, wrong-prefix and symlink-escape sentinels. Positive reaper/runner/guardian scenarios use a disposable HOME's `.cache/e2e-*` and still remove their Worlds. | both harness files, cleanup tests |
| MED7 / E2ELEAK-4 | existing `test_reaper_uses_world_marker_and_start_time_across_sessions`; requirement-source/catalogue validation | Documentation correction only: replace the misleading “crosses sessions and cache roots” recovery claim with recorded detached ownership. If runner and guardian are both SIGKILLed, or the machine reboots, no next-run scan discovers the leftovers; surviving processes and directories require a manual sweep. No new behavioral red claimed. Existing no-lookalike-selection assertions stay green; source and catalogue wording agree. | requirement doc, source JSON, requirement catalogue, this STATUS |
| LOW8 / E2ELEAK-1 | `test_last_world_restores_previous_signal_handlers` | Red: previous-handler map remained populated and harness handlers stayed installed. Green: first of two closes retains the handlers; last close restores both exact previous handlers and clears the map. | `scripts/e2e_support.py`, cleanup tests |
| LOW9 / E2ELEAK-1 | `test_guardian_wait_failure_preserves_original_error_and_forgets_world` | Red: guardian TimeoutExpired replaced the original ValueError. Green: original flow/cleanup errors survive guardian wait failure and the World is always removed from `_LIVE_WORLDS`. Partial construction now passes its active exception into exit as well. | `scripts/e2e_support.py`, cleanup tests |

Shared evidence/registration files: `tests/test_e2e_process_cleanup.py`,
`scripts/test-requirements.json`, `docs/requirements/e2eleak/assertions.md`,
`docs/requirements/e2eleak/evidence.json` and this STATUS. Requirement authority/source:
`docs/requirements/e2eleak.md`, `tests/requirements/e2eleak.json`.

## Verification

- Red: named `root:test_e2e_process_cleanup`, 20 tests, 22.814 s measured execution,
  both worker units failed as expected; `metadata_errors: []`.
- Green: same named module, 20 tests, 18.780 s measured execution, all passed;
  `metadata_errors: []`, no budget failures. All E2ELEAK-1 through -4 passed.
- Harness regression: six named modules (`test_e2e_strictness`, `test_e2e_run_control`,
  `test_e2e_recovery`, `test_e2e_learning_guard`, `test_e2e_requirements`,
  `test_test_framework_guards`), 253 tests, 21.293 s measured execution, all passed;
  `metadata_errors: []`, no budget failures. Total: 273 passing tests across seven
  named modules. No tests remain running at handoff.
- No new test modules or requirement IDs were introduced. The existing cleanup module
  remains registered as scenario/integration; all added test links are reciprocal.
- No full suite, `--affected`, copier/backup/move modules or runtime-release check:
  no product schema, record shape or runtime source changed.
- Installed collector/socket/PTY lifecycle and L1 timing were not run in this sandbox.
  The primary will remeasure L1 unsandboxed. Real disposable subprocesses, detached
  hosts, kernel birth identities, signals and runner interruption/timeout were tested;
  collector-client protocol coverage uses the disposable installed-client stand-in.
  Linux identity was not exercised on this macOS host.
- Assertion accounting: fixture directory assertions move from `<scratch>/e2e-fixture`
  to `<scratch>/.cache/e2e-fixture`, with the same required absence; positive reaper
  fixtures likewise use a disposable cache root. No assertion is removed or weakened.

Re-run only the named modules with the supplied test venv Python (`PY`):

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

## Timing round (2026-11-25): tracking made L1 3x slower and broke the lifecycle scenes

Symptom: `scripts/e2e_lifecycle.py --flows L1` took 305 s on the base and over 14 minutes here, and the
G2/G3/G4 life scenes failed (`g1_bare_account_unavailable`, step `open`), with the billing probe
(`provider-observe --billing-probe`) waiting its whole 60 s deadline and `open` another 11 s.

Measured (single scene `g2_codex_primary_worker_answers_match_oracle_in_history`, same machine):
base 41.0 s pass; branch before the fix 98.4 s FAIL. Per-process cost was NOT the cause (boot +45 ms per
hosted start, World build 0.87 s both, setup 19.4 s vs 19.1 s both, close 0.5 s). Bisecting the boot
registration step (flag files, one mode per run): any file write in every hosted boot reproduced the failure
(`os.open(O_WRONLY)` fails, `O_RDONLY|O_CREAT`, `stat`, a 20 ms sleep, and a unix-socket send all pass).
The product's signature helper runs under a macOS sandbox profile that denies every file write; our boot
registration (record write, lock opened for append) raised there, the helper died, the signature lane
reported `signature_execution_unconfirmed`, and billing probes and host opens stalled behind it.
A second defect: `identity()` raised `PermissionError` for `/bin/ps` (privileged image, `proc_pidinfo`
EPERM), so every tracked `ps` spawn in a hosted process failed.

Fix (harness only, `scripts/e2e_processes.py`): `locked(shared=True)` opens the lock read-only (flock needs no
write access); `install_tracking` tolerates a `PermissionError` on the process's own record (a confined
helper is recorded by its spawning parent's tracked Popen); `identity()` returns None for EPERM/EACCES (a
privileged image can neither be inspected nor signalled by this user, so it is never ours to stop); the
macOS libproc handle is loaded once per process and `secrets` is no longer imported (less work per hosted
boot). Guarantees unchanged: records still carry pid, kernel start time and marker; reaping never uses a
name; a missing helper or a closing World still fails closed (only the sandbox write denial is tolerated).

Residual: a confined helper that is itself never spawned through the tracked Popen is not recorded; the
signature helper exits on stdin EOF and its parent (the collector) is recorded and stopped.

Evidence: new test `test_tracking_does_not_break_privileged_binaries_or_confined_helpers` (E2ELEAK-2), red
on the previous harness (both sub-cases fail: `PermissionError` for the privileged binary and for the
read-only records/lock), green now. Side by side `--flows L1` on the base and this branch, started together:
base 308.1 s, branch 314.9 s (+2.2 %); identical results (the two L1.2 checks fail the same way on both and
are unrelated). Named cleanup modules: 274 tests green, `metadata_errors: []`, no budget failures. No
e2e process or world of this run was left behind.

## Final round (2026-11-25): uninspectable processes kept, permission tolerance only for parent-recorded children

Scope: harness only (`scripts/e2e_processes.py`, `tests/test_e2e_process_cleanup.py`, this folder, the
E2ELEAK-2 check list in `scripts/test-requirements.json`). No product code, no VERSION or digest change.

| Finding | Requirement | Test | Evidence |
|---|---|---|---|
| HIGH `identity()` None on EPERM/EACCES pruned live records | E2ELEAK-2 | `test_identity_distinguishes_uninspectable_from_exited_on_permission_error`, `test_uninspectable_live_recorded_child_is_kept_and_reported_exited_one_pruned` | Red: no `UNINSPECTABLE` value, survivors treated it as gone. Green: EPERM then `os.kill(pid, 0)`: ESRCH is gone (pruned), anything else is `UNINSPECTABLE`; survivors keeps and reports it, `_signal` never signals it (start time unconfirmed), cleanup fails with live recorded processes. A tracked spawn of a privileged binary writes no record (start time unconfirmable), so `/bin/ps` still never fails its caller. |
| HIGH blanket `PermissionError` swallow | E2ELEAK-2 | `test_denied_registration_outside_tracked_popen_fails_closed` (posix_spawn and tracked-grandchild cases) | Red: both printed "tolerated". Green: tolerated only when `COCOLO_E2E_SPAWN` carries a nonce whose spawn note (written by the parent's tracked Popen before the child starts) names this process's direct parent and the World marker. posix_spawn children and grandchildren inheriting the nonce fail closed. Notes are removed on wait/poll. |
| MED lifetime witnesses | E2ELEAK-2 | `test_confined_helper_spawned_by_tracked_popen_is_recorded_by_parent_and_stopped` | (a) denied helper recorded by its parent, stopped at teardown; (b) and (c) above. Changed assertion listed in `assertions.md`. |
