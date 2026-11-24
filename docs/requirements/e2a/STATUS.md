# E2a status (resume notes)

Worktree ~/Code/temp/cocolo-e2a, branch feat/rse-e2a, base ace235a4 (0.8.56).
Requirements: docs/requirements/e2a.md (E2a-1..9). Tests: tests/test_e2a_launch_record.py (11),
tests/test_e2a_wake_wire.py (14). Registered in scripts/test-{layers,stages,requirements}.json (scenario/integration).

## Steps
1. Requirements written. DONE
2. Red tests. DONE. Red run: both modules fail at import (host_commands.launch_target_problem,
   codex_agents.project_wake missing): `Ran 2 tests, errors=2`, 0.4 s.
3. Code. DONE. Modules: learning_store (projects launch columns + project_wake, additive; explicit registration insert;
   record_launch/launch_record/publish_wake/reconcile; CollectorServer record_launch op, wake key at register,
   reconcile before ready + own thread per sweep interval), collector_client/collector_contracts (record_launch),
   service_enrollment (wake keys, verifier subject_keys -> Enrolled.project_bound), account_host (wake keys at start,
   WAKE_OPERATIONS, bound credential), account_service (wake_bound, dispatch), account_api + wake_contracts +
   reply_shapes (op, payload, reply), project_wake (publisher), project_holds_schema/project_history/
   project_result_contexts (hooks), project_service (wiring, clean_stop/writer fault), host_commands (launch target,
   pins, prune/replace/remove), cli (install-codex-rule records via running collector), runtime_files (2 new modules).
4. Green: tests.test_e2a_launch_record 11 OK (2.0 s); tests.test_e2a_wake_wire 14 OK (17 s).
   Existing named modules: test_host_commands+test_project_service_startup 61 (77 s, then host_commands 65 with e2a in
   59 s) OK after the pins fix; test_account_host, test_rsf_s1_ingress_socket, test_holds_report, test_ledger_lock_limits,
   test_project_service_startup 66 OK except G6 idle check (fixed: periodic reconcile moved to its own thread; rerun 29 OK
   25 s); test_collector_stale 16 OK 3 s; test_hold_lifecycle 9 OK 17 s; test_project_service 11 OK 1 s;
   test_b4m_compatibility, test_project_holds_schema, test_account_api, test_reply_shape_compatibility OK.
   Failing as expected (4 assertions, assertions.md): test_rsf_s1_ingress, test_admission_inputs,
   test_signature_job_intake, test_signature_inputs. Not run: test_reply_shape_live (> 5 min; needs a DRIVERS entry).
5. Commit: see git log.

## For the primary
- assertions.md: 4 op-set assertions (15 -> 16 mutations; three exact ACCOUNT_OPERATIONS sets + publish_wake).
- runtime-releases.json digest needs the usual refresh (runtime files changed; two new modules).
- Launch record is written only by install-codex-rule (the wizard's optional Codex rule step) through a running
  collector; Claude-only setups get no launch record (E2c would show `install changed`). Decide whether setup's
  registration/start step also installs the protected copy.

## Round 2 (primary decisions 170, 171)
- 170 applied: 4 assertions (assertions.md, "Approved: decision 170") + test_reply_shape_live DRIVERS entry (not run here).
- 171: E2a-10 (learning-register installs the protected copy and records the launch target for every project;
  host_commands.record_setup_launch, cli) and E2a-11 (learning-status `recovery` line via the new read-only collector
  op `launch_status`; client_runtime). Red: SetupRecordTests 2 errors (KeyError launch_record / recovery), 0.5 s.
  Green: test_e2a_launch_record + rsf_s1_ingress + admission_inputs + signature_job_intake + signature_inputs 105 OK
  10 s; test_cli_status_client_boundary 12 OK 9 s; test_e2a_wake_wire 14 OK 18 s; test_host_commands 54 OK 60 s;
  test_learning_cli 30 OK 63 s.

## Review round 1 (Codex security review NOT-READY at 5a024c5e: scratchpad e2a-review-result.md)
Red first on 5a024c5e (17 new tests; 15 red on assertions, not imports; 2 boundary tests green by design; the cap
guard was already red): `Ran 17 tests, failures=14, errors=1 (fixture), 6 s`, then paging red `5471 not less than 2266`.
Fixes (modules): learning_store (record_launch/launch_status owner proof + collector-side launch_target_problem;
project_wake_instances; wake_reconcile_page by rows examined, marker-aware, budgeted startup pass, wake thread resumes),
service_enrollment (WakeKeys in-memory table + dummy key, Enrolled.bound_project, collector owner key/proofs in
`<state>/collector-owner/`), account_host (table, bound auth by id before any lookup), collector_client (owner-signed
record_launch/launch_recorded), cli (offline record under CollectorMaintenance; 30 s timeout), host_commands (stable
project lock = copies folder flock, inode-checked pins, pin-before-commit, removal keeps registered), wake_contracts
(instance, WAKE_MARKER), account_service (instance), project_wake (marker/WakeRefused, invalidate queued exact,
finish(final_due)), project_holds_schema (refusal propagates; next due counts releasing open-binding next_check_ms),
project_history (withdrawal transitions guarded), project_driver/service_host (verified settlement bool),
project_service (final read-only due read), tests/lifetime_caps.json (3 sites).
Green: test_e2a_wake_wire 24 OK 20 s; test_e2a_launch_record 20, 1 error = the unlisted assertion (assertions.md, NOT
applied, primary decision); test_host_commands 54 OK; contract batch (holds_schema, account_api, reply_shape_compat,
b4m, lifetime_cap_guard, rsf_s1_ingress, admission/signature x3, project_service, collector_stale) 207 OK 16 s;
cli_status_client_boundary OK; project_service_startup+ledger_lock_limits+account_host+holds_report 62 OK 54 s;
learning_cli 30 OK 62 s; open_withdrawal+hold_lifecycle 58 OK 54 s; lost_run_settlement+container_recovery+
publication_replay_driver 63 OK 107 s. Not run: test_reply_shape_live (> 5 min), --affected (primary).
Open for the primary: the one unlisted assertion above; runtime digest refresh (not done, as instructed).
