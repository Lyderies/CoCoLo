# Batch 0.8.58 integration (merge/batch-0858: 0.8.57 + RS-F S4 + T7 S2 fixes + T7 S2 checks)

Base 6a1ea9da (version/0.4.0 = 0.8.57). Version bookkeeping is left to the primary.

## 1. Merge of feat/rsf-s4-slots (RS-F S4; decisions 164/166/168/169)
- Clean merge, no conflicts.
- Check (RS-F S1 mutating gate): the new collector op `confirm_legacy_slot` is in
  `collector_contracts.COLLECTOR_MUTATING_OPERATIONS` (added by the S4 branch itself), so `collector_client`'s
  mutating gate covers it; `learning_store`'s owner-record op list names it too. Not an account/history op, so
  B4m's `reply_shapes.REPLY_FIELDS` does not list it (that table holds account.* and history.* only).

## 2. Merge of fix/t7s2-findings (s2fix; decisions 177/184/185/187)
- Clean merge, no conflicts.
- Check (B4m reply table): `history.orchestration_counts` is in `reply_shapes.REPLY_FIELDS`
  (`required: active, closed, paused`), added by the branch. Op-set pins: `history_api` operation list,
  `service_host` owner/primary allowed sets and `test_b2_service_outcome`'s pinned set (decision 177) all name it.
  `test_holds_report_socket` pins request traces only (no op set). No other pin found.

## 3. Merge of feat/t7-s2 (T7 S2 checks; decisions 173/178/180/181)
Conflicts and resolutions:
- `docs/requirements/t7/assertions.md`: both sections kept (S1 sections, then S2 sections); text unchanged.
- `scripts/e2e_core.py` `GROUP_SECONDS`: union (S1 `run_ctl`, `run_silence`, `run_capacity` + S2 `rec_service`,
  `rec_collector`), values unchanged.
- `scripts/e2e_lifecycle.py` `--fault` wiring: UNION of both slices. Both checks modules are imported;
  `FAULT_MODULES = (e2e_checks_run_control, e2e_checks_recovery)`, `FAULTS` = every fault name they declare
  (`recovery_broken`, `slot_not_released`), the argparse `choices`. `select_fault(name)` hands the fault to the one
  module that declares it and sets every other module's `FAULT` to None (both in `run_checks` for --jobs 1 and in
  `_worker` for --jobs N; cleared in `finally`). Still chosen ONLY by the `--fault` argument (no environment).
- Helper deduplication (asked by the primary): the helpers S2 copied from S1 now live once in the new
  `scripts/e2e_checks_shared.py` (declares no check; named `e2e_checks_*` so every e2e source guard of
  test_e2e_strictness/test_e2e_requirements scans it): INIT, write_patches, project_view, add_project, set_up
  (label argument), child_frames, _text, step, Ctl, bridge_run, state_dir, project_history, project_records,
  ledger_slots, slot_of, ACCOUNT_READ, capacity_read. Both slice modules import them by name (so tests that patch
  `control.project_records` / `rec.capacity_read` still patch what the slice's checks call).
  Where the two copies differed, the union keeps each slice's behaviour:
  - `Ctl`: S1's superset (child bounds/path/probe options, per-child grant, record, cancel, runtime_digest; S2's
    `dispatch_in_background(refuses=)`, `run.text` captured). Per-slice class attributes: S1 `Ctl`
    DISPATCH_TIMEOUT None (harness default 90 s, as before) / FINISH_TIMEOUT 90; S2 `Ctl` 120 / 120 (as before).
    S2's children now get a per-child grant file with the same content as the parent grant (WORK_GRANT with
    filesystem `fixture/facts.txt:read`), and `bounds` is `route.bounds or {}`; no check reads either.
  - `capacity_read`: S2's form (through `step`, same capture/G1/G8 judging and `capacity_read_timed_out` failure as
    S1's inline copy); a non-dict reply reads as `{}` (S1 returned it raw).
  - `bridge_run`: S2's (a `run.bridge` noted before a stimulus, else the reply's); S1 runs never set `bridge`.
  - `project_records`, `ledger_slots`: same SQL and path in both.
  No check function of either slice was edited.

## Test edits (no assertion changed; source scope only)
The dedupe moved code that two existing tests read as source text. Their assertions are unchanged; only the
text they read was widened to include the shared module (framework plumbing following the requested dedupe):
- tests/test_e2e_recovery.py `SOURCE`: old = `e2e_checks_recovery.py` text -> new = `e2e_checks_shared.py` text +
  `e2e_checks_recovery.py` text (step..Ctl slice, `capacity_read_timed_out`, refusal call sites incl.
  `wait_running`, env= on every subprocess call, refuses=True count <= 24 all still asserted, now over both).
- tests/test_e2e_run_control.py `test_the_dispatch_helper_has_no_blanket_refuses`: old reads
  `e2e_checks_run_control.py` -> new reads `e2e_checks_shared.py` (where `Ctl.dispatch` now is); same assertion.

## Registration
- `root:test_e2e_recovery`: stage unit, layer unit (starts no process: "without starting a World").
- `codex:tests.test_project_store_removal`: stage unit, layer unit (filesystem only).
- RS-F S4's `test_rsf_s4_slots` / `test_rsf_s4_transport` came registered (integration / scenario).

## Status
See the batch commit message and the report to the primary for runs and timings.
