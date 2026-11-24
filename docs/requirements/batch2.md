# Batch 0.8.57b integration (merge/batch-0857b: 0.8.56 + B4s + T7 S1)

Status: see the bottom of this file.

## 1. Merge of feat/rsb-b4s-settlement (conflicts)
- `orchestration_store.py` `_unfinished`: both sides kept. B4s's `_settled(binding)` helper and its skip
  (RS-B-B4-5: a reservation with a verified settlement row is finished, checked before the consumption check)
  plus RS-D S2's `unrecorded=None` keyword and its append on the verified-account-release branch (RS-D-2-N1/N2).
  A settled reservation is not counted as unrecorded (it never reaches the release branch).
- `scripts/test-requirements.json`: union (RS-B-B4m-1..3, RS-D-2-N1/N2 from HEAD; RS-B-B4 from B4s).

## 2. B4s x B4m x RS-F S1
- `reply_shapes.REPLY_FIELDS` gains `account.lost_run_closure` = exactly `lost_run_closure._FIELDS` (no optional).
  Without it B4m's shape guard refused every real closure (red: test_account_settlement_wire
  test_B4_1_a_matching_closure_decodes and the new table test errored; green after).
- `test_reply_shape_live.DRIVERS` (outcomes/lanes) gains `test_account_settlement_driver` so the op is produced live.
- `account.lost_run_closure` is in `ACCOUNT_OPERATIONS` (not a read op), so RS-F S1's runtime gate covers it.
  New module `tests/test_batch2_b4s_integration.py` (scenario/integration; same store fixture as test_rsf_s1_ingress):
  table row == producer fields, settle_lost_run row unchanged, missing/wrong runtime refused before the authorizer
  with zero rows changed.

## Assertion changes (old -> new)
- tests/test_rsf_s1_ingress.py `test_RSF_S1_2_every_account_mutation_refuses_missing_or_wrong_runtime_before_authorizer`:
  `assertEqual(15, len(mutations))` -> `assertEqual(16, len(mutations))`. Reason: B4s adds the account mutation
  `account.lost_run_closure` (RS-B-B4-1, a new operation by B4m rule); RS-F S1 requires every account mutation
  gated, so the inventory grows 23 -> 24 (docs/requirements/rsf-s1.md updated). The loop body is unchanged and now
  also covers the new op.
- tests/test_reply_shape_live.py DRIVERS: one module added (no assertion removed).

## 3. Merge of feat/t7-s1
- Clean merge (ee1f4f1d). New root module tests/test_e2e_run_control.py registered as root:test_e2e_run_control,
  stage integration, layer scenario (it starts a stand-in Python process). L6.1/L6.2 e2e checks are expected red
  until fix/l6-findings merges.

## Status
- Commits: 6520e2fd (B4s merge), b2b5d68c (B4s x B4m x RS-F S1), ee1f4f1d (T7 S1 merge), then the registration/run commit.
- Runs (named modules, clean env): settlement trio + lost_run_settlement + batch2 + rsd_runtime_close + reply_shape_compat
  + rsf_s1 x2: 119 tests 116 s OK; journals/container/claim_denial/container_stop/holds_schema/result_lifetime/cap_guard/
  admission_inputs/signature_inputs/signature_job_intake: 315 tests 150 s OK; open_withdrawal/b2_service_outcome/
  holds_report: 127 tests 46 s OK; root test_test_stages/e2e_requirements/e2e_strictness/e2e_run_control: 68 tests 4 s OK.
- test_project_driver alone: 93 tests 225 s OK. test_reply_shape_live alone (now replays test_account_settlement_driver
  too): 5 tests 596 s OK (over the 5-min single-run guide; it is one module replaying 31 drivers).
