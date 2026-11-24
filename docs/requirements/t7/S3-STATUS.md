# T7 S3 status: learning, backup, guard (L10.1, L10.2, L10.3, L11.1, L13.1, L13.2)

Branch feat/t7-s3, base 6a1ea9da (0.8.57, includes T7 S0 and S1, not S2). Module scripts/e2e_checks_learning_guard.py; guard
tests tests/test_e2e_learning_guard.py (red first: /tmp/claude-501/t7s3/red-first.txt, ModuleNotFoundError). Nothing pushed,
nothing installed, no product code changed.

## Scenes (two Worlds, so two parallel processes under --jobs)
- learn (one project + an unregistered second checkout, about 2 min): built in SECTIONS on first use by the check that needs
  them, so a G1/G8 break is charged to the check that ran the step. hooks (L10.1), migrate (L10.2), retention (L10.3 kept,
  future clock refused, raw-event removal), backup (L11.1), short (L10.3: a 60-second backup removed by retention; waits for real time).
- guard (two projects, about 50 s): L13.1, L13.2 through each project's protected copy (`World.copy_cli`) with Codex session
  records written by `World.codex_session` (the Codex session helper S3 owes: rollout file under ~/.codex/sessions, workspace,
  sandbox policy, writable entries).

## How values are read (never the command that made them)
Ledger sqlite read-only (event rows by code, ingestion table, sessions, stored_files registry, projects, record counts), the project
store's files and identity row, the stored backup file (sha256 of its bytes, integrity_check, record count), process-free. The
collector's own status (`learning-status`) for the counts the hook produced. Binding files for which copy serves which project.
Nothing is written into the product's state (a test forbids sqlite writes in the module).

## Fault (one fault, explicit argument only)
`--fault learning_guard_broken` (runner argument; E2E_* stripped everywhere; run_control and learning_guard each take only the
fault they own via `e2e_lifecycle.set_fault`). Five exact-once source rewrites in the stand-in Python: collector_client.ingest drops
the event (L10.1), client_runtime's full cycle report comes back with proposal_status pending (L10.2), learning_store verify reports a wrong digest
(L11.1), session_guard no longer knows danger-full-access (L13.1), host_commands.OwnRootRefused loses its message (L13.2).
`fault_isolated(normal, fault)` over every requirement ID: every S3 ID red, nothing outside S3 changes (G1 may not change).

## Results (2026-11-25, `--flows L10,L11,L13 --jobs 2`, whole run 4m12 because the every-flow G scenes run beside it)
Normal (/tmp/claude-501/t7s3/r.json): L10.1 green (2 checks), L10.2 green, L13.1 green, L13.2 green, L10.3 RED, L11.1 RED. L11.2 red
(no check: slice S5). G8 red (finding 1); G1-G5, G9 green. Seconds: learn scene about 110 s (hooks 30 s, migrate 5 s, short backup
64 s of which 61 s is waiting for real time), guard scene 50 s.
Fault `learning_guard_broken` (/tmp/claude-501/t7s3/fault.json, 4m09): L10.1, L10.2, L13.1, L13.2 newly red, L10.3 and L11.1 red as
before (their expectations fail one step earlier: raw events missing; digest not the file's), G1 unchanged, every other ID unchanged,
`fault_isolated` true. A first fault attempt patched `collector_client.learning_migration` for L10.2: the legacy cycle path then
exited non-zero and turned G1 red (rejected by the comparator), so the fault now returns the cycle report unfinished (exit 0).
Tests: tests.test_e2e_learning_guard (63, written first: ModuleNotFoundError red), test_e2e_requirements, test_e2e_strictness,
test_e2e_run_control green.

## Product findings (no product code changed)
1. G8: `learning-backup-project` prints the absolute backup path in its reply (`path`), which carries the home folder. L11.1 and
   the L10.3 expired-backup check are charged the G8 break (`capture_rule_broken`; their own expectations all pass). Decision: is
   an owner command's printed path an owner screen (allowed, decision 36) or an AI-read output (a leak)? If screen, run the backup
   through `World.terminal`; if output, the product drops the path from the reply.
2. L10.3 "controllable clock": a raw event expires 7 days after ingestion (retention-policy.json) and `learning-expire --now-ms` refuses
   a clock in the future, so no raw event can be expired in a run and "raw events go, summaries stay" cannot be shown.
   `l10_3_expired_raw_events_are_removed_and_their_summary_kept` is red by design. Options for the primary: a product clock for
   retention (a test-only injection point), or allow the harness to age rows in the scratch ledger (refused here: a write into the
   product's ledger).
3. `learning-expire` reports `expired_records` as the number of expired RECORD ids: removing an expired backup file alone reports 0.
4. A future clock for `learning-expire` is refused with the generic `input validation failed` (the cause, "retention time cannot be in
   the future", is not shown).
5. Interpretations (assertions.md): no recording switch exists, so "off" is the two real no-record paths; "unknown host" is a command
   with no Codex session / an unknown sandbox kind; `project_parent` cannot be reached in a World (the project's parent holds the
   home folder), `above_home` is what a session opened above the project gets.

## Decision 193 (2026-11-25)
Scene `retain` (group retain, about 35 s): World with `retention_patch` (raw-event window 8 s); five hook events, wait past expiry,
`learning-expire --now-ms now`. Normal (/tmp/claude-501/t7s3/r2.json): the raw-removal check passes its first seven expectations
(events expired, removed by learning-expire) and FAILS the last: `learning-expire` keeps NO event summary (summaries come only from the
collector's background sweep, `sweep_event_retention`); L10.3 stays red = product finding. Fault run (fault2.json): isolated true.
Tests 133 green (RawRetentionTests, BootHookTests red first). Decision 192: the G8 check stays strict until fix/backup-path merges.
