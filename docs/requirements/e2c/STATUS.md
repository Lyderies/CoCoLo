# E2c status (resume notes)

Worktree ~/Code/temp/cocolo-e2c, branch feat/rse-e2c, base b124a345 (0.8.59 + RS-E E2a).
Requirements: docs/requirements/e2c/requirements.md (E2c-1..11). Test module: tests/test_project_start.py.
Run: tfvenv python, `env -i PATH=... HOME=$HOME TMPDIR=/tmp/claude-501/e2c PYTHONPATH=<wt>/scripts python -B -m unittest`.
Not blocked.

## Steps
1. Requirements written. DONE
2. Red tests. DONE. Red run: `tests.test_project_start` fails at import (`codex_agents.project_start` missing):
   `Ran 1 test, errors=1`, 0.7 s.
3. Code. DONE (first cut): new `codex_agents/project_start.py` (ProjectStarter: starter thread + 2 workers, claim,
   pre-start steps, copy verify under pin, spawn from the copy with bare env, kill at 2x timeout, probation, stranded
   reconcile, `recovery_line`); learning_store (`project_starts` table, `START_CLAIM_SQL`, `StartClaim`,
   claim_due_starts/set_start_child/finish_start/release_start/start_probation/outstanding_starts/unpark_start/
   start_record; CollectorServer starts the starter after ready, joins it at stop; owner-signed ops `start_status`,
   `start_unpark`); collector_contracts (`start_unpark` mutating); collector_client (start_status, start_unpark);
   client_runtime (learning-status `recovery` = start_status line when a launch record exists); cli
   (learning-start-project-service calls start_unpark first); runtime_files (project_start.py).
4. Green. tests.test_project_start 20 OK (alone ~53 s; under parallel_tests 24-51 s). Fixture fixes found on the way:
   break_store makes 0700 dirs; the other-version hint needs live sockets; the E2a KILLER's bare hold is removed
   after the kill (as E2a does); T11 stops the collector before killing the host (else probation counts it).
   Named modules via scripts/parallel_tests.py --module, all "metadata_errors": []:
   batch A (project_start, lifetime_cap_guard, runtime_release, e2a_launch_record, e2a_wake_wire,
   cli_status_client_boundary, upgrade_handover, reply_shape_compatibility, b4m_compatibility, rsf_s1_ingress,
   ledger_lock_limits, collector_stale, project_service, root:test_test_stages) 50.9 s: only test_runtime_release
   fails (expected, see assertions.md) and one T11 ordering flake, fixed and rerun green (24.3 s run C);
   batch B (learning_store, learning_cli, account_host, project_service_startup, rsf_s1_ingress_socket,
   holds_report, admission_inputs, signature_job_intake, signature_inputs, account_api) 29.8 s all OK.
5. Registry: tests/requirements/e2c.json (source), scripts/test-requirements.json E2c-1..11,
   test-layers (scenario), test-stages (integration), test-timings (53 s). lifetime_caps.json: 3 sites.
6. Commit: see git log. Open for the primary: runtime digest bookkeeping (assertions.md); test_reply_shape_live not
   run (no new account op; collector ops are not in the reply-shape tables).

## Plan per design test
T1 StartTests E2c-1; T2 StartTests E2c-2 limits/round robin + BackoffTests E2c-4 park/unpark/probation;
T3 StartTests blocked spawn (E2c-2); T4 E2c-6; T5 E2c-7; T6 E2c-5; T10 BudgetTests E2c-3; T11+T14 StrandedTests
E2c-10; T12 E2c-8; T13 StopTests E2c-11; status StatusTests E2c-9. T8/T9 covered by E2a-5/6.

## Review round 1 fix round (decision 241; review reviews/040-e2c-r1/RESULT-codex.md, NOT-READY)

Base d16a9448. Every finding has a test shown RED on the d16a9448 code (product files swapped back to HEAD, new tests
run, product files restored), then green. Red run, 12 tests, 214 s: FAILED (failures=9, errors=1):
- HIGH 1: `ProductionIntervalTests.test_E2c_3_1000_due_projects_with_launch_records_drain_within_60_seconds`
  red "296 of 1000 projects not drained in 60.1 s"; `BudgetTests.test_E2c_3_a_collector_drains_1000...` (now at the
  product's intervals) red "0 != 298"; `FairnessTests.test_E2c_2_a_retry_never_goes_ahead_of_a_project_not_yet_tried`
  red (untouched projects missing from the first pass). Green: drain 2.1 s, 17 claims, longest hold 8-10 ms.
  Fix: successive bounded claim batches at once while a batch is full (SWEEP_BATCH_GAP_SECONDS), first claim at once
  after the stranded pass; a fair cursor (due time, project) in START_CLAIM_AFTER_SQL; a pass wraps when it ends.
- HIGH 2: `OwnershipTests.test_E2c_10_a_child_whose_pid_record_is_refused_is_still_timed_killed_and_reaped` red
  ("never failed": the child was released, served, row 'serving'); `OwnershipTests.test_E2c_2_refused_ledger_steps_
  are_retried_and_no_worker_ends` red (both workers died; attempt left outstanding). Fix: `_spawn` owns the child
  from the spawn (pid record retried SETTLE_TRIES, then only missing; any error kills and reaps); `_settle`/`_step`
  retry refused ledger steps, then the starter thread retries every tick; workers catch everything per claim.
- HIGH 3: `SlotTests.test_E2c_2_surviving_stranded_children_hold_their_slots` red (a fresh start began beside two
  surviving stranded children). Fix: a surviving stranded child's claim is `_reserved` (holds a slot, own) and watched
  every tick (`_watch_reserved`: serves -> started with its probation, ends -> failed, 2x timeout -> failed unsignalled).
- HIGH 4: `ProbationTests.test_E2c_4_a_serving_recheck_never_replaces_an_active_probation` red ('started' !=
  'serving'); `ProbationTests.test_E2c_4_a_probation_is_kept_across_a_collector_restart` red (KeyError
  'probation_pid': nothing kept). Fix: project_starts.probation_attempt/probation_pid/probation_until_ms (+ partial
  index), set by finish_start('started', child_pid); claims skip a row in probation; a recheck never replaces it;
  start_probation(attempt=) ends it; the restart pass (`probation_starts` pages, `_reconcile_probations`) watches a
  previous collector's probations (`_watch_foreign`: ended before its end -> failed early, outlived -> streak ends).
  `BudgetTests.test_E2c_4_the_probation_pass_reaches_every_probation_past_one_page` red (TypeError child_pid).
- MED 1: `OwnerCommandTests.test_E2c_4_the_owner_start_command_unparks_with_the_collector_down` red (park kept).
  Fix: cli learning-start-project-service: `_start_collector_quietly` -> `_unpark_start` -> `_start_project_host`.
- MED 2: production intervals (above); T1 now enforces the interval deadline; `TrustBoundaryTests.test_E2c_6_
  location_refusals_hold_in_the_product_path` (collector without the temp_roots seam refuses the copy; direct
  launch_target_problem refusals); `OtherVersionTests.test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced`
  (a real project host process of an earlier runtime, 0.8.1 with a different digest, authorized by its own code).
  These pass on d16a9448 too (no product bug); red shown by mutation instead (one run, 3 FAILED, 45.6 s): handover
  check removed -> T5-real red; launch_target_problem ignored -> trust test red ("recovery: started"); first claim
  delayed 3 s -> T1 red ("no start began within one interval").
- LOW (caps): lifetime_caps.json: project_start.py::_claim (START_QUEUE_LIMIT), ::_worker (START_SLOTS),
  ::_record_child (SETTLE_TRIES), ::_reconcile_probations, learning_store.py::probation_starts; past-capacity test
  `FairnessTests.test_E2c_2_past_the_queue_and_slot_limits_every_due_project_is_still_started` (200 due through a
  queue of 16 and 2 slots) red on d16a9448 ("0 != 122": one batch per 5 s).
- LOW (digest): left for the primary (assertions.md); runtime-releases.json 0.8.59 stays dcc18633....

Fixture change only (assertions.md 3): T11 serves out a 1 s probation before the restart kill.
Green: tests.test_project_start 36 + lifetime_cap_guard via `scripts/parallel_tests.py --module` 38 s,
`"metadata_errors": []`, failed_units []. Registry: 15 new checks in scripts/test-requirements.json; timing 85 s.

Neighbour regression (parallel_tests --module, 21 named modules + root:test_test_stages, 42 s, metadata_errors []):
first run found 3 tests that count ledger changes or waits in an in-process collector's first seconds
(rsf_s1_ingress_socket RSF-S1-1, e2a_launch_record E2a-3, ledger_lock_limits E1a-3): the fix round had moved the
first claim to "at once". Put back to one STARTER_CHECK_SECONDS after ready (as d16a9448; E2c-1 allows it); rerun of
project_start, rsf_s1_ingress_socket, e2a_launch_record, ledger_lock_limits, lifetime_cap_guard: 91 tests, 44 s, all
green, metadata_errors []. test_runtime_release red for the digest only (computed fc19c3a2b77de508e7712685d53ad4c6
279b6c8249b09bc84353e0cf3d504d04; 0.8.59 entry kept dcc18633..., primary's bookkeeping).
