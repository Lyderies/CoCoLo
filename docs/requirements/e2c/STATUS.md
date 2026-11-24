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
