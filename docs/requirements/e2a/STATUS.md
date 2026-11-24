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
