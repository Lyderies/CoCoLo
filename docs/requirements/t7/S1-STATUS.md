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
  setup restarts the shared collector): L9.1, L9.2, L9.3, G5. About 2 min.

## How values are read (never the command that made the run)
Project history (read-only sqlite, per project): dispatch_result.terminal_state, event.stop_reason. Account ledger
capacity table (read-only): active_run_capacity rows. The account service's own per-run read
(`account.capacity_status` over its socket, run by the installed client library through the stand-in Python).
The project service's live `learning-run-status`. Per-orchestration report counts (G5).

## Test-only mechanism (decision 128)
e2e_support.BOOT now reads HOME/.e2e-bin/patches.json: source rewrites applied to a named product module as it is
imported (each must match exactly once or the import fails loudly). Used for: the project service's LIVENESS limits
(L7.2), the billing probe reuse and cooldown (60 s scale, so a probe is a real claim on a slot, L9.1/G5), and the
fault injection below. No product code is changed.

## Fault injection (one fault turns this slice red)
`E2E_T7S1_FAULT=slot_not_released` skips the UPDATE that releases a finished run's slot in the installed
learning_store and in account_cleanup (the live release path). Result (2026-11-24): every S1 check goes red (slots
leak, later children are refused and the per-run reads say held); the S0 groups do not read the variable. Without the
variable the official run (`--flows L6,L7,L9 --jobs 2`, report /tmp/claude-501/t7s1/r.json) passed every check except
the L6.1 finding check: 21 checks, S1 scenes 68 s, 34 s, 127 s (wall about 6 min with the all-flow G checks).

## Facts that differ from the requirement text (primary decision needed)
1. L9.1 says two children at once and the third refused. The installed limits are six per machine and three per
   project (MAX_ACTIVE_ATTEMPTS, project_service.py:38; INSTALLED_ACTIVE_RUN_LIMITS, 0.3.3 C8). The check proves
   the installed numbers: three in each of two projects run at once; a fourth of one project is refused
   `preparation_full`; a child of a third project is refused `claim_busy`; the billing probe is refused
   `probe_busy`. Statement text in requirements.json left as is.
2. The project's preparation slot (3 per project) is freed by recording the result (learning-record-host-result), not
   by the child ending; the account slot is released when the child ends. The checks record each result as the
   primary does.

## Product findings (no product code changed)
- L6.1 (red check, l6_1_tool_the_client_denied_is_reported_in_the_result): the bridge builds
  report["permission_denials"] (bridge.py:1631) but the dispatch reply drops it; a child the client denied a tool
  ends completed and neither the reply nor the result says so. A child that calls a tool the run does not allow is
  stopped and says so (green check).
- L6.2: `dispatch-preflight` refuses an uncommitted read path before any admission ("assignment read path is not
  committed", no admission record, no slot). `learning-open-host-run` still ADMITS a run for that path; the dispatch
  is then refused `preparation_export_failed` with no slot, and the run stays opened (counted abandoned at close).
- Not provable without the owner: a real OS sandbox or network deny (the fake client cannot enforce one), see the
  analysis list "Cannot be proven" item 1.

## Assertions changed
None. No S0 check or existing assertion was edited. Additions to shared files: e2e_support.BOOT (patches block),
e2e_core.GROUP_SECONDS (three group names), e2e_lifecycle.py (one import), requirements.json (`checks` arrays of the
nine IDs).
