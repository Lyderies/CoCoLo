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

## 4. Integration fix: learning producer matrix (RS-F S4)
- RS-F S4 added `run_slot_confirmation` to `learning_contracts.PRODUCER_AUTHORITY["owner"]` but not to
  `config/learning-producers.json`, so `install.py verify` refused every install ("learning producer matrix does not
  match enforced authority") and every lifecycle World's setup failed at step 4 (red: first batch3 e2e run, all IDs
  red, `the scripted setup of a ... project did not finish`). Fix: the owner row of learning-producers.json gains
  `run_slot_confirmation` (config follows the decided S4 requirement, decision 166). Green: install verify ready.
  No test asserted the old matrix. The defect is on feat/rsf-s4-slots itself (not a merge artefact).

## 5. Lifecycle run (S1 + S2 flows) and the open L14.1 / G1 finding
`e2e_lifecycle.py --flows L6,L7,L8,L9,L14,L15 --jobs 3` (203 s): L6.1 L6.2 L7.1 L7.2 L7.3 L9.1 L9.2 L9.3 G5 (S1) and
L8.1 L8.2 L8.3 L15.1 (S2) PASS, G2-G4 G8 G9 PASS. RED: L14.1 and G1, one cause (`capture_rule_broken:
g1_unexpected_exit` in the rec_service scene). Four `learning-run-status` steps not marked as expected refusals exit 2:
- phase "lost" (phase_view) and `status_second` (ctl.status): a lost run's status answers `ready: false,
  reason_code run_lost, terminal_state abandoned, capacity released` with exit 2. The check asserts exactly that
  content; only the exit code trips G1.
- phases "next" and "closed" (phase_view after the run is recorded): `ready: false, reason_code
  control_attempt_not_found` (a recorded run is forgotten), exit 2. No check asserts these replies.
NOT a merge artefact: the same four exits occur with the s2fix product (fix/t7s2-findings tree) and the pre-merge
feat/t7-s2 scripts in a scratch copy. The s2fix STATUS's green S2 run predates the S2 final G1 tightening.
Not changed here (needs a decision): either (a) the product exits 0 for a status read whose answer is the run's state
(lost; recorded/forgotten), or (b) the S2 check marks those four reads expected-to-refuse - lost-run reads arguably fall
under fix round 1's "run-status polls of a run that may ... be lost", the next/closed reads of a recorded run are
covered by no decision, and `test_refusal_is_expected_only_at_named_call_sites` (pinned 5 sites) would change.

## 6. Fault runs (same flows, --jobs 3)
- `--fault recovery_broken`: fault_isolated TRUE (changed L8.1 L8.2 L8.3 L15.1; nothing outside S2). Caveat: G1 and
  L14.1 are already red in the normal run, so they cannot show a change.
- `--fault slot_not_released`: fault_isolated FALSE only because `s1_not_red: [L6.2]` (changed G5 L6.1 L7.1 L7.2 L7.3
  L9.2 L9.3, nothing outside the targets). L6.2 (refused before admission: no slot is ever claimed) cannot be reached
  by a release fault, like L9.1; it was red in earlier fault runs only because the product bug kept it red in the
  normal run too (fixed in 0.8.57, L6 fix). Making it isolated means dropping L6.2 from
  `FAULT_TARGETS["slot_not_released"]` (and the pinned `set(S1_IDS) - {"L9.1"}` in test_e2e_run_control): an
  assertion change no decision covers. Not changed.

## Status
- Commits: S4 merge, s2fix merge, T7 S2 merge (fault union, shared helpers, registrations), producer-matrix fix,
  this doc. Named modules (235 tests, 114 s OK, 1 skip known: OrchestrationCountsCostTests), test_project_driver
  (99 tests, 254 s OK), root e2e/stage tests (107 tests, 4 s OK).
- Open for the primary: section 5 (L14.1/G1) and section 6 (L6.2 fault target). test_reply_shape_live (soak, not
  run) should confirm `history.orchestration_counts` is produced live by a DRIVERS module (test_b2_service_outcome_hosts
  drives it but is not listed).

## 0.8.58 full-run fixes (integration)
- Metadata (10 checks, 20 lines): the fix/t7s2-findings tests named L14.1, L15.1, L8.3 and RS-B-B4 but were never
  listed in scripts/test-requirements.json; now listed under each ID (metadata errors 10 -> 0).
- RS-F S4 `legacy_slots.deadline_sql` put `json_extract` into `crashed_claim`, so every capacity count (the deferred
  claim owner's machine/account counts, the source image's drain check) carried SQL JSON parsing, and the drain check
  read `records`. Fix: the intent's run id is read by a Python scalar `legacy_dispatch_run_id` (registered with
  `legacy_dispatch_deadline`, same rows; the sweep uses the same `intents_sql`), and `_drained` leaves the
  legacy-deadline branch out when, in the same hold, no unreleased `legacy_unknown` slot exists (it could decide
  nothing then). Behaviour unchanged; RS-F S4 tests green. No assertion changed; the source-image table count (31)
  did not change (that failure was the `JSON_` assertion, same cause).
- 0.8.58 runtime digest refreshed (not installed anywhere): c8581b79... -> 1ababa83...
