# T7 S1 status: run control (L6.1, L6.2, L7.1, L7.2, L7.3, L9.1, L9.2, L9.3, G5)

Branch feat/t7-s1, base dae5684a (accepted T7 S0). Module scripts/e2e_checks_run_control.py; guard tests
tests/test_e2e_run_control.py. Nothing pushed, nothing installed.

## Scenes (three Worlds, so three parallel processes under --jobs)
- run_ctl (one project): L7.1 cancel a running child; L7.3 hard timeout (the assignment's own bound, 4 s, below
  the route's); L6.1 child calling Write / WebFetch / Bash stopped (`claude_tool_not_allowed`); L6.2 uncommitted read
  path. About 65 s.
- run_silence (one project, silence limits scaled 3/1/2/4 s by the stand-in Python, decision 128, test only): L7.2.
  About 35 s.
- run_capacity (three projects, one HOME, one collector; all three set up before any child starts, because a later
  setup restarts the shared collector): L9.1, L9.2, L9.3, G5. About 2 min. Its billing probes (L9.1 probe_busy, G5)
  are made for a SECOND Claude account, attested in project C with the owner key and never probed before, so the
  installed five-minute reuse and cooldown cannot answer for it (no billing constant is rewritten). G5 order: close
  all three orchestrations, then ONE probe (launched), its `probe_admission` pass and `provider_billing` read in the
  account ledger, then the eight per-run reads and the ledger table.

## How values are read (never the command that made the run)
Project history (read-only sqlite, per project): dispatch_result.terminal_state, event.stop_reason. Account ledger
capacity table (read-only): active_run_capacity rows. The account service's own per-run read
(`account.capacity_status` over its socket, run by the installed client library through the stand-in Python).
The project service's live `learning-run-status`. Per-orchestration report counts (G5).

## Test-only mechanism (decision 128)
e2e_support.BOOT now reads HOME/.e2e-bin/patches.json: source rewrites applied to a named product module as it is
imported (each must match exactly once or the import fails loudly). Used for: the project service's LIVENESS limits
(L7.2, decision 128) and the fault injection below. Nothing else (no billing constant since fix round 1). No product
code is changed.

## Fault injection (one fault turns this slice red)
`--fault slot_not_released` (an explicit runner argument, passed to each --jobs worker as an argument; no environment
variable selects a fault, and World environments drop every E2E_* variable) skips the UPDATE that releases a
finished run's slot in the installed learning_store and in account_cleanup (the live release path). Evidence (fix
round 1, 2026-11-25): see "Fix round 1" below.

## Facts that differ from the requirement text (primary decision needed)
1. L9.1 says two children at once and the third refused. The installed limits are six per machine and three per
   project (MAX_ACTIVE_ATTEMPTS, project_service.py:38; INSTALLED_ACTIVE_RUN_LIMITS, 0.3.3 C8). The check proves
   the installed numbers: three in each of two projects run at once; a fourth of one project is refused
   `preparation_full`; a child of a third project is refused `claim_busy`; the billing probe is refused
   `probe_busy`. Statement text in requirements.json left as is.
2. The project's preparation slot (3 per project) is freed by recording the result (learning-record-host-result), not
   by the child ending; the account slot is released when the child ends. The checks record each result as the
   primary does.

## Product findings (no product code changed here; decision 151: fixed on fix/l6-findings, checks stay)
Both checks are RED against today's product, which is correct; they are known product findings, not harness faults.
- L6.1 (l6_1_tool_the_client_denied_is_reported_in_the_result, red): a child the client denied a tool ends
  `completed` with no terminal state, and nothing persisted names the denial (bridge.py:1631 builds
  report["permission_denials"]; the reply drops it). The check now requires exactly what fix/l6-findings a876f6f0
  produces: the stored dispatch_result `terminal_state == "failed"`, reply outcome `claude_tool_not_allowed`, and the
  stored result failed and naming `claude_tool_not_allowed` (the history keeps it as a keyed digest; recording the
  returned result against that digest must be accepted); plus the report's denial count/tool and read
  containment (kept). The same stored-result assertion was added to the green stray-tool check (same path), and it passes there.
- L6.2 (red, first failing assertion: the preflight refusal carries no `admission_created` field): the check also
  requires `learning-open-host-run` to REFUSE the uncommitted path and re-reads the admission history (project
  open provenance, ledger run bindings, capacity slots) after the open attempt and after the dispatch attempt.
  Today the open still ADMITS the run (finding unchanged).
- Not provable without the owner: a real OS sandbox or network deny (the fake client cannot enforce one), see the
  analysis list "Cannot be proven" item 1.

## Assertions changed
None. No S0 check or existing assertion was edited. Additions to shared files: e2e_support.BOOT (patches block),
e2e_core.GROUP_SECONDS (three group names), e2e_lifecycle.py (one import), requirements.json (`checks` arrays of the
nine IDs).

## Fix round 1 (Codex review t7s1 NOT-READY, scratchpad t7s1-review-result.md), 2026-11-25
Every HIGH and MED fixed in the harness/checks only; strictness unit tests in tests/test_e2e_run_control.py (red on
1ea7bf8b: 17 failures/errors, list in /tmp/claude-501/t7s1/red-first.txt; green after).
- HIGH L6.1 / L6.2: see Product findings (both red by design). Assertion changes listed in assertions.md.
- HIGH G5: close all three orchestrations, then ONE probe, its launch read in the account ledger (exactly one
  `probe_admission` pass of that account after the close, its `provider_billing` reading), then the eight reads.
- HIGH billing: the reuse/cooldown rewrites are gone (BOOT applies only the silence-limit patch and the fault). The
  probes use a second attested, never-probed Claude account (attest_probe_account), so they are genuinely eligible.
- HIGH fault isolation: `--fault NAME` argument only (module FAULT, set by the runner or worker); E2E_* dropped from
  every World environment and from the --jobs worker environment. S1 dispatches and the G5 probe are judged by the S1
  check that asserts their exit (refuses=True to the harness), so the fault does not turn G1 red (assertions.md, G1
  note: for the primary to read). Comparison `fault_isolated(normal, fault)` over every requirement ID.
- MED L9.3: exact run IDs per project. MED G8: capacity_read catches TimeoutExpired, captures partial output
  (timed_out) and answers `capacity_read_timed_out`.
Evidence: normal /tmp/claude-501/t7s1/r2.json (L6.1, L6.2 red; everything else green, G1 green); inherited variable
only (E2E_T7S1_FAULT set, no --fault) /tmp/claude-501/t7s1/r2-envonly.json: same verdicts as normal; fault
/tmp/claude-501/t7s1/fault2.json: L7.1, L7.2, L7.3, L9.2, L9.3, G5 newly red, L6.1/L6.2 stay red, G1 and every
other ID unchanged, `fault_isolated` true. L9.1 stays green under the release fault by construction (it judges the
refusals while the six slots are held for the first time, before any release; FAULT_TARGETS).

## Fix round 2 (decision 157), 2026-11-25
New HIGH (refuses only on expected refusals; G1 judges the rest), L9.3 (scheduled runs, 4/3/1), fault isolation (all
subprocesses strip E2E_*; comparator accepts only declared red IDs: S1 targets + G1), L6.2 (expected refusal reason).
Evidence: normal --flows L6,L7,L9 --jobs 2 (/tmp/claude-501/t7s1/r4.json): only L6.1, L6.2 red. Fault run
(fault3.json): L7.1-3, L9.2, L9.3, G5, G1 newly red, L9.1 green, fault_isolated true. One earlier normal run (r3) had
L9.2/G5 red on a per-run capacity read "not released" under load (passed alone and on rerun): possible flake, watch it.
Assertion changes: assertions.md, "fix round 2".
