# S10 status (HM-6 disk and memory)

Worktree ~/Code/temp/cocolo-s10, branch feat/s10-resources from IP-B batch 0ed00ff7.

Done: requirements (docs/requirements/s10.md, tests/requirements/s10.json), tests
(packages/codex-agents/tests/test_health_resources.py, 14 tests; red = module missing, then green),
codex_agents/health_resources.py, the preflight RunDriver._resource_preflight (called in _answer for run.register, before _register; M11 fixtures call _register directly and stay untouched), new settings key
memory_available_percent, runtime_files.py entry, docs/configuration.md, registrations (test-layers, stages,
timings, requirements; metadata_errors []).

Not done (deferred by plan): the primary wiring of health_resources.register(monitor, state, workspace_root)
(like S8/S9), VM-disk rows (S19).

Rerun: cd packages/codex-agents; PYTHONPATH=../../scripts TMPDIR=/tmp/claude-501/s10 $PY -B -m unittest tests.test_health_resources
Proof: $PY -B scripts/parallel_tests.py --module codex:tests.test_health_resources

Modules run green after the change: test_lifetime_cap_guard, test_health_ledger (31), test_health_ledger_fix (15),
test_project_driver (109, 258 s), test_run_driver_retries (31), test_host_slots (10, 33 s),
test_publication_replay_driver (7, 24 s), test_trial_run_driver_bugs (20, 49 s), test_memory_holds (23),
test_account_settlement_driver (16, 12 s), test_health_resources (14, 0.4 s).
Known: test_runtime_release digest pin differs because runtime files changed (primary batch refreshes VERSION/digest;
not edited here). test_reply_shape_live: see report.

## Decision 355 follow-up
- Own reason code `resource_low` (history_contracts.HISTORY_REASON_CODES, refusal_steps.EXACT step); run.register reply
  carries `message`; client_runtime._run_driver accepts it only with resource_low; open report adds `resource_refusal`.
- Wired: CollectorServer.__init__ registers health_resources (state folder + memory; the collector has no workspace root).
- Changed assertion: test_health_containers CollectorWiringTests names/states gain 'resources' (assertions.md).
- Also green: test_refusal_steps, test_contracts, test_history_api, test_reply_shape_compatibility,
  test_dispatch_client_runtime, test_history_service_upgrade, test_dispatch_delivery_cli, test_project_host_open,
  test_service_ownership_audit, test_learning_cli (83 s), test_required_host_open, test_backup_reply_private.
- test_reply_shape_live: see commit message / report.

## Review r1 fix round (Codex r1 NOT-READY -> fix)
- HIGH 1: new driver op `run.check` (project_driver._OPERATIONS, RunDriver._check_new_run); client_runtime._check_new_run
  calls it in open_codex_client before history original scope and account admission. Identical retry of a registered
  run (same inputs digest + host class) is not probed. `_resource_preflight` at run.register stays as a backstop.
- HIGH 2: RuntimeResourceRefusal (a RuntimeOpenError, reason resource_low, plain message); the CLI prints it as the error,
  state refused, exit 2. The backstop refusal after admission raises it too (never "started").
- MED 3: health_resources.safe_path: printable, middle-shortened paths; refusal <= 1,000 chars and <= 3,000 escaped
  bytes (reply frame 4 KiB); finding text fits 240 with the guidance whole (finding_text).
- MED 4: ResourceCheck reports `settings_invalid` when user_settings.load warns; open reply carries `settings_warning`.
- Tests: tests/test_health_resources_open.py (registered: layers/stages/timings, registry checks S10-HM6-4/5).
- Red on 4a0473e9 (5): open low-disk refusal, backstop refusal, open settings_warning, long/unprintable paths,
  settings_invalid finding (identical-retry test passed before and after: a keep-passing guard).
- Run A (15 modules: health_resources(+_open), health_containers, project_driver, dispatch_client_runtime,
  run_driver_retries, refusal_steps, contracts, history_api, reply_shape_compatibility, dispatch_delivery_cli,
  project_host_open, lifetime_cap_guard, account_original_open, host_slots): 354 tests, 119.5 s wall; 2 failures fixed
  (RS-B-4 driver-reply list gains run.check's reply, see assertions.md; finding_text registered in lifetime_caps.json)
  plus why_not_scenario added. Run B (health_resources, _open, project_driver, lifetime_cap_guard, learning_cli):
  166 tests green, 118.1 s wall, metadata_errors []. test_reply_shape_live not run (primary, quiet machine).

## Decision 360 fix (review r2c)
- project_driver._check_new_run(orchestration_handle, attempt, inputs_digest, host_client): the retry exemption now matches
  orchestration, attempt, inputs digest and host class (the account's identity).
- client_runtime._check_new_run fails closed: missing run.sock or a failed/unanswered run.check -> RuntimeOpenError
  `open_service_unreachable` before account admission (no new reason code).
- Tests: 2 added to test_health_resources_open (checks registered in scripts/test-requirements.json); M11 test in
  test_account_original_open stubs `_check_new_run` (assertions.md).
- Green (13 modules incl. health_resources(+_open), project_driver, account_original_open, dispatch_client_runtime,
  run_driver_retries, refusal_steps, contracts, reply_shape_compatibility, dispatch_delivery_cli, learning_cli, host_slots,
  lifetime_cap_guard); metadata_errors []. test_reply_shape_live not run.
