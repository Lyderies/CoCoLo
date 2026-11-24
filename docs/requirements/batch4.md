# Batch 4 integration (merge/batch-0859: 0.8.58 + RS-B B6 + bkfix + expfix + T7 S3)

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
