# B4s requirement map (ANALYSIS-0.4.0-rsb-b4-b7.md §2; REQUIREMENTS-account-settlement.md)
Code: packages/codex-agents/native-package/.codex/orchestration/codex_agents/ (RT). Tests in packages/codex-agents/tests/:
S = test_account_settlement.py (store), W = test_account_settlement_wire.py, D = test_account_settlement_driver.py.
Red: de132086 (docs/requirements/b4s/red.md). No schema step (20 stays). Changed assertions: assertions.md.

| Req | Code (file:line) | Tests |
|---|---|---|
| §9 key identity | (existing) ledger.state.digest_key; project result key | S KeyIdentityTests (4) |
| RS-B-B4-1 closure, new op | lost_run_closure.py:19-85 (checked_/verify_lost_run_closure, MAX 8 KiB); account_lost_run.py:82; account_api.py:66,193,326,719; account_service.py:275,1039,1045,1292; account_host.py | W LostRunClosureWireTests (6, incl. settle_lost_run reply shape unchanged); S test_B4_1_the_closure_past_the_size_limit_is_refused; D test_B4_1_only_the_runs_own_project_role_gets_its_closure_once_settled; op-set pins (assertions.md) |
| RS-B-B4-2 outbox | project_history.py:2581 open_account_settlement; project_driver.py:1896 _settlement_step, :1915 _settle_entry; result_persistence.py:1167,1175 account_settlement[_after_stop] | S OutboxTests (7: B4_2 x3, Q1 x3, Q2); D test_A4 (identical request re-sent by the next service) |
| RS-B-B4-3 record_account_settlement | project_history.py:2632 | S RecordSettlementTests (V1, V2, V4, V5, V6a, V6b, planted-helper equivalence); D test_A1, test_A2 (lost_unbound) |
| RS-B-B4-4 order/replay | project_driver.py:1827 (_settle_lost), :1857 (container recovery settle=), :1915 _settle_entry; container_recovery.py | D StepBoundaryTests (5: stop after reply / after settlement / after workspace removal, one sequence for every caller, container journal after settlement) |
| RS-B-B4-5 readers | orchestration_store.py:318,350 (_unfinished); project_history.py:2717 _attempt_settled, :3035 _attempt_released | S test_B4_5_a_verified_settlement_releases_the_attempt_for_the_purge_only; D test_A3 |
| RS-B-B4-6 RS-13 retry/tombstone | project_history.py:2632 (stored receipt from the tombstone row; purge removes the settlement with the attempt) | S test_B4_6 x2 |
| RS-B-B4-7 validator | project_holds_schema.py:140-160 | S ValidatorTests (4) |
| RS-B-B4-8 P2-P4 | (combined path above, bounds 2) | D LostLifecycleLimitTests.test_B4_8_three_lost_lifecycles_settle_close_and_purge_and_a_fourth_completes |
| RS-B-B4-9 RC-3 | not met in B4s: still skipped, reason corrected (assertions.md): B3 open-withdrawal gap for a lease-refused run | tests/test_result_lifetime.py:105 (skipped) |
| O0c lifetime caps | lifetime_caps.json: lost_run_closure.py::checked_lost_run_closure, ::verify_lost_run_closure (BOUNDED, one message) | test_lifetime_cap_guard |

B0-2 (tests/test_hold_lifecycle.py:131) stays skipped by spec (no lost terminal is written, oracle S3).
