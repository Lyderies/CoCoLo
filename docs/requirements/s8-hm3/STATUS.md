# S8 HM-3 — Fix round

Review 040-s8-r1; comparison base ce81bd96 (0.8.67), reviewed HEAD 2a46a0f4 on feat/s8-hm3.
Changes remain uncommitted in the sandboxed worktree. VERSION and runtime-releases.json are unchanged.
This is one fixed-scope correction round; real service integration remains pending unsandboxed verification.

| Review item / requirement | Regression and evidence | Disposition |
| --- | --- | --- |
| MED-1 / S8-HM3-2 | test_project_overdue_stays_present_past_live_page; test_account_overdue_stays_present_and_project_scoped_past_live_page: original code loses overdue findings on alternating passes; both green after fix. Each has more than 64 live holds and checks five consecutive passes. | Overdue queries bypass ValidatorPages, use existing due indexes and LIMIT 1 with ordered eligible checks. Account query binds project_id. No rotating overdue window. |
| MED-2 / S8-HM3-3 | test_durable_service_status_reaches_monitor_and_clears_on_recovery: missing status support red -> green; fresh checks expose writer-fault and failed-stop codes in project-scoped monitor status. Real test_O10_D13_a_faulted_lane_writer_stops_the_service_and_the_next_one_settles and test_HM3_stop_signal_first_reaches_health_monitor_status are blocked at socket fixture setup. | serve_project persists fixed status before closing a faulted host, including decision 179. HoldsCheck consumes it; healthy successor startup clears it. Real tests drive serve_project, actual persistence lane, collector health pass and status view; no injected report or mock host. Do not claim real wiring green yet. |
| MED-3 / S8-HM3-6 | test_registration_does_not_edit_primary_owned_files_or_builtin_api: reviewed implementation red -> green. test_registration uses the unchanged factory then register(check), green. | learning_store.py and health_monitor.py are byte-identical to ce81bd96. Registration deferred to primary, exact insertion below. |
| LOW-1 / E1b-1, S8-HM3-5 | test_runtime_account_sql_runs_only_on_owner_without_opening_reader: independent ledger reader assertion red -> green. Real owner thread SQL trace, empty refusal map, closed transaction and no ledger open_reader call. | Bounded LedgerOwner.call requests own account pages and checks; transactions/progress callbacks end on the owner. Only project histories open independent readers. No E1b exception. |
| LOW-2 / S8-HM3-2 | test_account_overdue_uses_saved_project_clock_floor_without_advancing: no overdue under wall rollback red -> green; floor remains 100000. Existing project floor/boundary/backoff tests remain green. | Account and project observations use max(wall, saved result_identity floor) for that project, from the same project snapshot. Projects without a saved floor use wall time. |
| LOW-3 / S8-HM3-5 | test_unrelated_lifetime_caps_bytes_are_preserved: byte prefix red -> green; test_lifetime_cap_guard green. | All unrelated entries are byte-identical to base. Only S8 entries differ; the runtime page cap moved from run to its nested projects_page function. |
| LOW-4 / S8-HM3-5 | test_project_id_is_bound_in_validator_queries: literal project identity red -> green; existing indexed query-plan/read-only test green. | Project filters and singleton lookups bind project_id; predicate/window bind positions remain distinct. |

Requirements preceded regression tests. Initial reviewed-code run had seven assertion failures and two fixture errors;
fixture corrections gave overdue/account-floor/owner/SQL failures plus the missing durable status API. Two further MED-2 safety tests reproduced lost fault status on unreadable history and a skipped close on report I/O error; both are now green. No behaviour
assertion was weakened. All changed assertions and fixture corrections are paired in [assertions.md](assertions.md).

Named focused run: tests.test_health_holds (33 tests), tests.test_lifetime_cap_guard (4),
tests.test_health_containers (19; existing real-podman skip). All passed; worker phase 1.6 s,
metadata_errors [], budget_failures [], failed_units []. Exact sanitized results are in [evidence.json](evidence.json).
The original register, custody, page-progress, byte-budget, private-wording and stale-observation regressions remain green.

The wider named run completed in 47.2 s (119 tests, metadata_errors []). Runtime release had its expected single digest
pin failure (2 tests passed); no release files were changed. Health monitor: 22 passed, 7 socket errors. Project service:
10 passed, one subprocess socket-start failure. Startup: all 14 blocked at collector socket creation. Wake wiring had
socket/CLI start failures and one old fixture that suppressed directory creation; that fixture now uses the real directory
setup with all original assertions intact. A 36.4 s recheck confirms that compatibility test passes: wake wiring now has 19 passes and 12 socket/CLI-start failures; startup remains blocked (14 errors). Ledger owner wiring: 26 passed, three socket/host-probe errors, plus two deferred
claim timeout tests and their cleanup timeouts. Those timeout results are unresolved, not claimed green or attributed solely
to sockets; the primary must rerun them. No background test run is left in flight. No PTY is required.

The service status file stores only an existing fixed reason code, with atomic replacement, owner-only mode, no symlink
following and a bounded read. It ends at healthy successor startup or project deletion. No SQL schema, source record shape,
account operation or stored enum changed; source-copy/backup/migration enumeration gates are not triggered. Project cleanup
already removes every non-lock file. No test does persistence I/O at module import; no new modules or requirement IDs were
introduced. New tests are in existing registered scenario modules, with registry checks added.

Primary registration after S5: after the existing ContainerCheck registration in CollectorServer.__init__, add this exact
registration (with `from .health_holds import HoldsCheck` in the imports):

```python
self.health_monitor.register(HoldsCheck(self.state, sweep_interval_seconds=WORKER_RESULT_SWEEP_SECONDS))
```

For a literal single inserted line with no import edit, the equivalent is:

```python
self.health_monitor.register(__import__(__package__ + '.health_holds', fromlist=['HoldsCheck']).HoldsCheck(self.state, sweep_interval_seconds=WORKER_RESULT_SWEEP_SECONDS))
```

The primary should update the builder-only ownership guard for that authorized merge insertion and restore the collector
registry expectations to include holds, with the assertion replacements recorded. This builder makes no learning_store.py
change and keeps with_builtin_checks unchanged.

Primary socket reruns, with the prescribed test Python, a clean environment and a short TMPDIR:

```text
python -B scripts/parallel_tests.py --module codex:tests.test_project_service_startup --module codex:tests.test_project_service --module codex:tests.test_health_monitor --module codex:tests.test_e2a_wake_wire --module codex:tests.test_ledger_owner_wiring
python -B scripts/parallel_tests.py --module codex:tests.test_health_holds --module codex:tests.test_lifetime_cap_guard --module codex:tests.test_health_containers --module codex:tests.test_runtime_release
```

Fix-round files: health_holds.py, project_service.py; learning_store.py and health_monitor.py reverted to base;
test_health_holds.py, test_project_service_startup.py, test_e2a_wake_wire.py; collector assertions reverted in
test_health_monitor.py and test_health_containers.py; lifetime_caps.json, scripts/test-requirements.json,
tests/requirements/s8-hm3.json, requirements prose, STATUS.md, assertions.md and evidence.json. The earlier S8
runtime_files membership and module layer/stage/timing registrations remain. No commit, install, merge or live-service action.

Remaining primary gates: real normal/stop-first shutdown and successor recovery, unresolved owner deferred-claim timeouts,
merge registration after S5, unsandboxed affected checks against ce81bd96, macOS/Linux lifecycle checks, other-provider
review and merged-batch full suites. VERSION and catalogue remain the primary's responsibility.


## Final fix

Cause: test-side, not a product lock leak. The fix round added an observation helper to the real D13 startup test; it
built a second LedgerOwner on the live state root while the socket fixture's ledger still held that root's single writer
lock (flock), so it raised BlockingIOError (LedgerError). The reviewed product (serve_project reporting before
host.close) passes the base version of the D13 test unchanged: confirmed by running the base test file against the fix-round
product. The product holds no new lock across the stop (the status write is one atomic file replace, closed at once).

Fix: test_O10_D13_a_faulted_lane_writer_stops_the_service_and_the_next_one_settles is restored byte-identical to base.
Two new tests, test_HM3_faulted_service_reaches_health_monitor_status (normal path) and
test_HM3_stop_signal_first_reaches_health_monitor_status, run the real service and writer fault, then observe the real
HoldsCheck and HealthMonitor view on a snapshot of the state root (sqlite backup plus the status file) with its own ledger
owner: fault visible after the stop, gone after the healthy successor. No product code changed in this round.
The S8-HM3-3 registry entry lists the new test in place of the (untagged again) base D13 test.

Known pre-existing flake, not from S8: tests.test_project_service_startup L15.1 deletion tests
(test_L15_1_deletion_removes_the_projects_history_store, test_L15_1_deleting_a_project_stops_its_service_and_leaves_the_other_running)
sometimes see 'cleanup pending' (the stopped service still holds its history store), and stop_hosts can raise
ProcessLookupError. Reproduced on the unmodified 0.8.67 base (about 1 in 25 alone, more under parallel load).
