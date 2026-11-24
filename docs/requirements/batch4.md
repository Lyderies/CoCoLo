# Batch 4 integration (merge/batch-0859: 0.8.58 + RS-B B6 + bkfix + expfix + T7 S3 + RS-F S7 + RS-B B5a)

Base 1c293c44 (version/0.4.0 = 0.8.58). Version bookkeeping is left to the primary.

## 1. Merge of feat/rsb-b6-close (RS-B B6 + schema step 21; decisions 195/209/210/212/213)
Conflict and resolution:
- `orchestration_store.py` `_open_count` / `state_counts` / `open_counts` (batch 3 s2fix vs B6):
  - `_open_count`: B6's form (indexed `state='open'` count, no checkpoint parsed).
  - `state_counts`: removed; its only caller was batch 3's `_open_count`, now B6's indexed read.
  - `open_counts` (learning-status, `history.orchestration_counts`): keeps s2fix's behaviour (open rows, at most
    `max_orchestrations + 1`, verified one by one and split active/paused; an unverifiable one counts active) but
    selects them by B6's indexed `state='open'` instead of the `json_extract` over checkpoint content, and the closed
    count is an indexed `COUNT(*) ... state='closed'` (no closed checkpoint read; decisions 185.2/187, RS-B-B6-8).
- `docs/requirements/b6.md`: requirement headings `## RS-B-B6-N — Title` + blank line rewritten as
  `**RS-B-B6-N — Title.**` so the catalogue's statements (already in that form) match their cited source
  (metadata errors 8 -> 0; text otherwise unchanged).

Test change (decisions 185/187/195 cover it; batch 3 left it skipped "until B6"):
- `tests/test_holds_report.py` `OrchestrationCountsCostTests.test_L14_1_the_closed_count_parses_no_closed_checkpoint`:
  old = `@unittest.skip(...)`, body `assertIn('status', ORCHESTRATION_COLUMNS['project_orchestrations'])`
  -> new = un-skipped; `assertIn('state', ORCHESTRATION_STATE_COLUMNS['project_orchestrations'])` (B6 named the
  column `state`, values open/closed), the `(project_id,state)` index exists, and the measurement the name asks
  for: with 2 closed + 1 open orchestration, `orchestration_counts` gives {active 1, paused 0, closed 2}, no SQL
  statement it runs names a closed handle on `project_orchestration_checkpoints` or uses `json_extract`, and the
  closed count's query plan uses `project_orchestrations_state` only. Red shown by re-adding a checkpoint-content
  `json_extract` scan to `open_counts` (1 failure), green on the resolution.
- Schema/backup: RS-F S4/S5 and s2fix's pending-cleanup marker survive step 21 (test_project_store_removal,
  test_rsf_s4_slots, test_rsf_s4_transport, test_rsf_s5_probe, test_project_holds_schema, test_history_service_upgrade,
  test_backup_installed_project, test_owner_backup_streamed, test_large_project_backups green).
- No new history/account op in B6 (no reply-table/gate/pin change). Registrations came with the branch
  (`codex:tests.test_b6_unattended_close` component/integration, `root:test_rsd_budget` component/integration).

## 2. Merge of fix/backup-path (bkfix; decisions 192/194/197/202/205)
- Clean merge, no conflicts (cli.py and learning_store.py auto-merged with B6).
- No new op. Registration: `codex:tests.test_backup_reply_private` was unregistered (metadata: missing stage/layer)
  -> stage integration, layer scenario (one test starts a real Python child process; same as its sibling
  test_owner_backup_streamed).
- Assertion changes: only bkfix's own, listed in docs/requirements/bkfix/assertions.md (decisions 194/197/202/205).

## 3. Merge of fix/expire-summary (expfix; decisions 200/201/203/204)
Conflict and resolution:
- `packages/codex-agents/tests/lifetime_caps.json`: expfix re-sorted the file while B6 moved and reworded
  `project_history.py::runtime_close_status`; git kept both copies (B6's new text at B6's place, the old text at
  expfix's sorted place: a duplicate JSON key). Kept ONE entry with B6's text (B6 review r1 correction), removed the
  old duplicate; every expfix entry (`_expired_event_window`, `drain_event_retention`, `expire_report`, ...) kept.
  Verified no duplicate key; test_lifetime_cap_guard green.
- cli.py / learning_store.py auto-merged with bkfix.
- No new op; test_event_retention came registered. Assertion changes: only expfix's own
  (docs/requirements/expfix/assertions.md; decisions 200/204).

## 4. Merge of feat/t7-s3 (T7 S3 checks; decisions 193/198/207/211)
Conflicts and resolutions:
- `scripts/e2e_lifecycle.py` `--fault` wiring: UNION of S1, S2 and S3, argument-only. S3's `set_fault` (S1 + S3
  modules only) is dropped for batch 3's `select_fault`; `FAULT_MODULES = (e2e_checks_run_control,
  e2e_checks_recovery, e2e_checks_learning_guard)`; `FAULTS` (the argparse choices) = every fault the three declare
  (`slot_not_released`, `recovery_broken`, `learning_guard_broken`, plus S3's narrower ones); each fault is handed to
  the one module that declares it, every other module gets None (single process and every `--jobs` worker; cleared
  in `finally`). No environment variable selects a fault.
- `scripts/e2e_core.py` `GROUP_SECONDS`: union (S1 run_ctl/run_silence/run_capacity, S2 rec_service/rec_collector,
  S3 learn/guard/retain), values unchanged.
- `scripts/e2e_support.py` `World.cli` timeout branch: both sides capture the partial output for G1/G8 and answer
  exit -1. S2 records the step `refuses: False`; S3 passed the caller's `refuses`. Kept S2's (a step that timed out
  is a failed step, never an expected refusal; G1 already breaks on `timed_out` either way); S3's comment folded in.
  Both sides' tests (test_e2e_recovery timeout test, test_e2e_learning_guard G8 tests) pass.
- `docs/requirements/t7/assertions.md`: both sections kept (S2 sections, then S3 sections); text unchanged.
Helper deduplication (into scripts/e2e_checks_shared.py; no check function edited):
- `e2e_checks_learning_guard` imported `Ctl, add_project, child_frames, project_view, set_up, write_patches` from
  `e2e_checks_run_control` (S1's re-exports): now `Ctl, add_project, child_frames, project_view, write_patches`
  from `e2e_checks_shared`. `shared.Ctl`'s timeouts are S1's (dispatch = harness default, finish 90 s), exactly what
  S1's `Ctl` subclass set, so S3's runs are unchanged. `set_up` is a one-line S3 wrapper of `shared.set_up` with
  label "learning-guard" (S1's wrapper said "run-control"; the label is only the harness-error text).
- S3's private `state_root` (`HOME/.local/state/codex-agents`) is now `shared.state_dir` (same path), imported as
  `state_root` so its checks and tests (`lg.state_root`) are unchanged; the `STATE` tuple is gone.
- S3's own `step(world, *args)` (an installed-CLI command) differs from `shared.step` (a raw helper command) and stays.
Registration: `root:test_e2e_learning_guard` was unregistered (test_test_stages red) -> stage integration, layer
scenario (it starts real child processes: a sleeping shell and a Python script, for the G8 timeout tests).
No new product op in batch 4 (B6, bkfix, expfix, S3 add none): `reply_shapes.REPLY_FIELDS`, the RS-F S1 mutating
gate and the op-set pins are unchanged (test_reply_shape_compatibility, test_rsf_s1_ingress green).
Assertion changes in this batch: only the one in section 1 (OrchestrationCountsCostTests, decisions 185/187/195).

## 5. Merge of feat/rsf-s7-replay (RS-F S7, RSF-S7-1..6; decisions 214/215)
- project_history.py, learning_store.py, lifetime_caps.json, test-stages/test-layers: auto-merged with B6/bkfix/expfix.
  B6's step-21 schema, indexes and close rules are untouched (S7 adds no schema; its purge touches only released
  `reservation`/`lease` holds, B6's orchestration holds are never selected; B6's `release_hold` still clears an
  orchestration hold's evidence).
- Fixture change (not an assertion): `tests/rsf_s7_support.py` `ProjectFixture.migrate` simulates a format-18 store;
  with schema 21 it now also calls B6's `legacy_orchestration_state_layout` (the same fixture B6 used in
  test_open_withdrawal/test_project_holds_schema). Without it the 18 -> 21 upgrade refused `history_incompatible`
  (5 S7 errors) because step 21's indexes/column were already present.

## 6. Merge of feat/rsb-b5a-crashed (RS-B B5a, RS-B-B5a-1..6; decisions 216/217)
Conflicts and resolutions:
- `scripts/test-layers.json`, `test-stages.json`, `test-requirements.json`: textual (sorted neighbours); union of both
  sides, values unchanged (B5a's six modules + `RS-B-B5a`).
- `project_history.py` (auto-merged, semantic clash, three fixes):
  1. S7's `_purge_settlement_evidence` hard-coded `release_kind IN (?,?,?)` for the two B4s kinds + `lost_unbound`;
     B5a adds `crashed` to `RELEASE_KINDS`, so every purge pass raised (ProgrammingError -> `history_unavailable`;
     4 S7 tests + B5a's tombstone test red). Placeholders are now built from the kinds, so crashed evidence is
     purged inside the same 8-day window.
  2. One evidence value per released hold: B5a wrote its process proof, then S7's `release_hold(evidence=closure)`
     replaced it (COALESCE; B5a's two proof tests red). Now `record_account_settlement` computes ONE `retained`
     value (S7's closure, or for `crashed` B5a's minimal proof) and `release_hold` writes it once, same
     transaction. S7's pruning backfill writes only NULL/adopted evidence, so it never replaces a proof; the
     closure itself stays in the settlement row until pruning.
  3. S7's open-path tombstone lookup verified the hold's evidence as a closure; a crashed proof would answer
     `history_conflict`. A `crashed` tombstone whose evidence is B5a's four-field proof (`_crash_proof`) is
     adopted and then matched, as every tombstone already is, by request id + the digest of the complete
     original request; S7's replay-window refusal still comes first.
- New test module `tests/test_batch4_crashed_replay.py` (unit/unit, both checks listed under RS-B-B5a):
  proof survives pruning, the identical open replays `settled` within the window, another request conflicts, after
  the window both paths refuse `history_result_not_reserved` and the purge clears the proof. Red shown with
  `_crash_proof` patched to False (history_conflict), green on the resolution.
- Primary note: a crashed attempt's retained hold evidence is B5a's process proof, not the closure (RSF-S7-1 says
  "one authenticated closure per settled attempt"); still bounded, cleared at 8 days.
Assertion changes in sections 5-6: none (one fixture helper line, one new module).

## Proof (merge/batch-0859 at the S3 merge)
- Lifecycle: `python3 -I -B scripts/e2e_lifecycle.py --flows L6,L7,L8,L9,L10,L11,L13,L14,L15 --jobs 3 --report
  /tmp/claude-501/batch4/r.json` (282 s): every S1 (L6.1, L6.2, L7.1-L7.3, L9.1-L9.3), S2 (L8.1-L8.3, L14.1,
  L15.1) and S3 (L10.1, L10.2, L10.3, L11.1, L13.1, L13.2) ID PASSES, G1-G5, G8, G9 pass. L10.3 is green with expfix
  (red by design on the S3 branch alone, decision 211); L11.1 is green with bkfix (same). Runner exit 1 only from
  L11.2 `no_check` (no slice has a check for it; not an S1/S2/S3 ID).
- Fault runs (same flows, --jobs 3), each isolated by its own slice's `fault_isolated`:
  `slot_not_released` (291 s) changed G1, G5, L6.1, L7.1-L7.3, L9.2, L9.3 (all inside its targets / FAULT_ALSO_RED);
  `recovery_broken` (361 s) changed G1 + the five S2 IDs; `learning_guard_broken` (282 s) changed exactly the six
  S3 IDs. No ID outside its targets, no target left green, no ID missing.
- Named modules (packaged venv, from packages/codex-agents): merge 1 set (37 modules incl. every B6-touched test,
  test_holds_report, test_project_store_removal, test_rsf_s4_slots/transport, test_rsf_s5_probe, backup/schema
  modules, test_lifetime_cap_guard, test_reply_shape_compatibility, test_rsf_s1_ingress) 782 tests, 251 s + 121 s
  in two halves: one error in test_lost_run_settlement (KeyError terminal_state under the two halves' parallel
  load), its class passed twice alone (re-run). bkfix: test_backup_reply_private, test_owner_backup_streamed,
  test_installer, test_backup_installed_project 77 tests 43 s. expfix: test_event_retention,
  test_legacy_history_compatibility, test_lifetime_cap_guard 3 s; test_learning_cli 30 tests 58 s. After all
  merges: cap guard, reply shapes, rsf_s1_ingress, holds_report, event_retention, backup_reply_private, legacy
  history 82 tests 7 s. test_project_driver alone: 99 tests 247 s OK. Root: test_rsd_budget 6 tests; test_e2e_*
  (learning_guard, recovery, requirements, run_control, strictness) + test_test_stages 220 tests 5 s.
- Metadata (`parallel_tests.metadata()`): 0 errors.

## Proof after S7 + B5a merges and 0.8.59 bookkeeping
- Named modules (packaged venv, from packages/codex-agents): 23 modules (S7: rsf_s7_account/project/journal; B5a:
  crashed_settlement/process_evidence/account/driver_unit/settlement_socket, b5a_validation_privacy,
  account_settlement(+_wire), publication_replay; new test_batch4_crashed_replay; B6/schema: b6_unattended_close,
  holds_report, project_holds_schema, open_withdrawal, history_service_upgrade; reply_shape_compatibility,
  rsf_s1_ingress, event_retention, launch_journal, lifetime_cap_guard) 396 tests 67 s OK.
  test_lost_run_settlement alone 26 tests 77 s OK; test_project_driver alone 99 tests 248 s OK.
- Root: test_test_stages + test_e2e_* (5) + test_rsd_budget 226 tests 7 s OK. Metadata: 0 errors.
- Lifecycle: `--flows L6,L7,L8,L9,L10,L11,L13,L14,L15 --jobs 3 --report /tmp/claude-501/batch4/r2.json` (280 s):
  every S1/S2/S3 ID PASSES; runner exit 1 only from L11.2 `no_check` (unchanged).
- Release 0.8.59: VERSION, runtime-releases.json 0.8.59 = 8a333f07...efa6 (0.8.55-0.8.58 untouched),
  previous-releases.json 0.8.58 record (source 1c293c44, same bundle eb34209d), packaging.py comment, catalogue
  count 18 -> 19, CHANGELOG. No new runtime file (S7/B5a add tests only); learning-producers.json unchanged
  (test_providers green); no tracked .pyc. test_runtime_release + test_lifetime_cap_guard + test_providers 113 tests
  22 s; claude-agents test_previous_release(_catalogue,_recorded) + test_packaging 36 tests 1 s OK.
