# T7 S2 status: recovery, status, deletion (L8.1, L8.2, L8.3, L14.1, L15.1)

Branch feat/t7-s2, base ace235a4 (0.8.56, includes T7 S0, NOT S1). Module scripts/e2e_checks_recovery.py; guard tests
tests/test_e2e_recovery.py. Nothing pushed, nothing installed. The shared-file hunks in scripts/e2e_support.py
(stand-in Python patch hook, `child_environment`) are byte-identical to S1's so the merge is clean; scripts/e2e_lifecycle.py
and scripts/e2e_core.py differ from S1 only in the `--fault` wiring (union the two modules' faults when both merge). The
helpers (Ctl, capacity read, project_view, add_project) are copies of S1's, trimmed: dedupe into one module after both merge.

## Scenes (two Worlds, so two parallel processes under --jobs)
- rec_service (one project, about 75 s): L8.1, L8.3 (service replaced), L14.1. A child runs 14 s; its project service is
  SIGKILLed mid-run (pid from the process table, `project_host.py`); `learning-start-project-service` starts the next
  service; run-status is polled (bounded) until run_lost and released; a second kill and restart; a next child claims the
  slot; close is tried, then the step the run's own status names (pause, `learning-classify-host-run-abandoned`), then close.
- rec_collector (one project, then a second one in the same HOME, about 115 s): L8.2, L8.3 (another project's setup), L15.1.
  Run 1: the shared collector (`collector_host.py`) is SIGKILLed mid-run; the owner's next command (`learning-status`)
  brings it back; a second kill and return. Run 2: the second project's scripted setup runs while run 2's dispatch runs.
  Then `learning-delete-project` on the second project.

## How values are read (never the command that made the run)
Process table (`ps`): service and collector pids, gone and new. Account ledger and project history (read-only sqlite):
capacity row, binding (state, reason, released), run tables counted by kind, the deleted project's rows per table.
The account service's own per-run read (`account.capacity_status`, over its socket, run by the installed client library),
POLLED with a bound (wait_released), never one immediate read; its bridge id comes from the ledger row noted BEFORE the
stimulus (a cut dispatch has no reply). `learning-run-status`, `learning-status` (managed_lifecycle, earlier_version_runs),
`learning-migrate-project --status`, `learning-orchestration-status`, `learning-orchestration-report`. Helper commands
(`step`) are captured like any step, a timeout included (G1/G8); `refuses=True` is passed only on the dispatch the kill
cuts short, the run-status reads of a run that may be lost or not yet running, and the close attempts.

## Fault injection
`--fault recovery_broken` (explicit runner argument only; E2E_* stripped from every child) rewrites the installed product
in the stand-in Python: the lost-run settlement no longer releases the slot (account_lost_run), a finished run's slot is
never released (learning_store, account_cleanup, same two rewrites as S1) and the ledger's `records` of a deleted project
stay. `fault_isolated(normal, fault)` over every requirement ID: all five S2 IDs red, nothing outside S2 changes.

## Results (2026-11-25)
Normal-run reds are product findings, not harness faults. See Evidence.

## Product findings (no product code changed; the checks stay red until fixed)
1. L8.3 (lost run): after the new service settles the run `run_lost`, the orchestration CANNOT close:
   `learning-close-orchestration` refuses `history_orchestration_pending` and its next step says "restart the project's
   service", which does nothing. Pausing and `learning-classify-host-run-abandoned` (the step run-status names) succeed but
   close still refuses, and the report keeps the run pending and active. The requirement says the orchestration can close.
2. L14.1: `learning-status` `managed_lifecycle.orchestrations_active` is 0 while an orchestration is active (the
   orchestration status and the report say active); after a lost run is settled the report still counts the run
   `pending` and `current_active` (abandoned 0) while learning-status counts it abandoned and run-status says released.
3. L15.1: `learning-delete-project` removes the ledger rows but leaves the deleted project's service process running
   (and its history store files on disk, empty here). The requirement says records and services.
4. Behaviour that differs from the requirement text, not a defect: L8.2 a killed collector does not make the run
   `run_lost`; the project service survives and the run ends normally, its slot released once the collector is back.
   L8.3 (another project's setup) does not replace the shared collector in 0.8.56 (the L8b incident is fixed); the check
   records `collector_kept` or `collector_replaced` and holds either way.

## Evidence (2026-11-25, `--flows L8,L14,L15 --jobs 2`)
Normal (/tmp/claude-501/t7s2/r.json): L8.1 green, L8.2 green, L8.3 RED (finding 1; its second check, another project's setup
mid-dispatch, is green with `collector_kept`), L14.1 RED (finding 2, first failing: orchestrations_active), L15.1 RED (finding 3,
last expectation; ledger, other-project-untouched and own-store expectations pass), G1-G4, G8, G9 green. Seconds per check
group: rec_service about 72 s, rec_collector about 110 s (the L5/G scenes run beside them; whole run about 3 min).
Fault `recovery_broken` (/tmp/claude-501/t7s2/fault.json): L8.1 and L8.2 newly red, L8.3, L14.1, L15.1 red (already red),
every other ID unchanged, `fault_isolated` true. Note L8.3, L14.1, L15.1 are red in the normal run for product reasons, so the
fault cannot "turn" them: it fails them at an earlier expectation (L14.1 at the next-run count, L15.1 at the deletion itself,
L8.3 second check at the slot read).
Tests: tests.test_e2e_recovery (14, new), tests.test_e2e_requirements, tests.test_e2e_strictness green.

## Fix round 1 (Codex review t7s2 NOT-READY), 2026-11-25: harness/checks only
Changes (old -> new for every assertion: assertions.md, "S2 fix round 1"): L8.2 reworded (decision 173) and judged from the
persisted ledger terminal (exactly run_lost or completed, released, counted once, service pid survived); L8.3 collector scene
uses this run's slot and running status, and judges the dispatching project's service (replaced or untouched, see below);
G1 refusal flags only on the kill-cut dispatch and the run-status polls (a guard test lists every flagged call site; reports,
recovery, close, deletion are expected-success); G8 `World.cli()` captures a timeout; L15.1 built to owner question 174's
recommendation (scrubbed tombstone, retained observations hold no project path or run identifier, no replacement service in the
final listing, survivor record IDs/digests); missing `terminal_observed`/`ready` fail; L8.1 next completion read from the
persisted ledger; L14.1 judges a `closed` phase, the migration status at every phase, and all last assertions (`expect_each`).
Finding: on 0.8.56 the second project's setup replaces neither the collector nor the dispatching project's service, so the L8.3
collector check judges both outcomes (reason `service_replaced` or `service_untouched`); needs a primary decision whether to add a
stimulus that forces a replacement.
A completed run's binding row stays `pending`; the harness reads it as terminal from completion evidence (consumed binding or a
released completion row, the rule `learning-status` counts host_runs_terminal by).
Evidence (/tmp/claude-501/t7s2r2/n.json, f.json): normal L8.1 green, L8.2 green (reason completed), L8.3 red (service scene: close
refuses, product finding 1; collector scene green), L14.1 red (findings 2), L15.1 red (finding 3: the deleted project's service
stays), G1 red (the close and orchestration reads that now must succeed; same in the fault run). Fault: L8.1, L8.2 newly red, L8.3
collector scene newly red, `fault_isolated` true (changed: L8.1, L8.2 only; the others were already red). Tests: tests.test_e2e_recovery
(31), tests.test_e2e_requirements, tests.test_e2e_strictness green.
