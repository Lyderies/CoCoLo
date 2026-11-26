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
