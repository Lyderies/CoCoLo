# B6 red -> green evidence

Red = the final test text run against the base runtime (6a1ea9da, exported with `git archive` to a scratch folder,
the tests and scripts of this branch beside it); green = the same tests on this branch.

| Requirement | Tests (tests/test_b6_unattended_close.py unless named) | Red on base | Green |
|---|---|---|---|
| RS-B-B6-1 | RegisterRowTests x4 | 4/4 fail (no orchestration row, no state column) | pass |
| RS-B-B6-2 | EligibilityTests x3, EligibilityWithRunsTests x2, LeaseTests x2 | 7/7 fail | pass |
| RS-B-B6-3 | ReplayAndFairnessTests.test_B6_3_* | fail (no derived request) | pass |
| RS-B-B6-4 | ReplayAndFairnessTests x2, LeaseTests x2, EligibilityWithRunsTests x1 | 5/5 fail | pass |
| RS-B-B6-5 | LeaseTests K1, EligibilityWithRunsTests K2 | K1 fails (no register row); K2 passes on base (regression pin, as the analysis expected) | pass |
| RS-B-B6-6 | tests.test_rsd_runtime_close (RS-D-2-N1/N2) | built by S2, pass on base | pass (fixture line, assertions.md B) |
| RS-B-B6-7 | SchemaStepTests x5 (real format-20 store, failure, process death, unknown objects, indexes) | 5/5 fail | pass |
| RS-B-B6-8 | OrchestrationStateTests x1 | fail (no state column) | pass |
| RS-D-9-S2/S3 | tests/test_rsd_budget.py (root) x6 | 2/6 fail: retirement VM steps [296, 275] vs [700296, 500275]; plan `result_expiry` + temp sort | 6/6 pass |

Base run of the B6 module: 25 tests, failures=8, errors=19 (24 tests red, K2 green).

## Review round 1 (fix of the Codex security review of 700d41f8)

Red = the round's final test text run against 700d41f8 (exported with `git archive`, the new tests and
`store_leases.sql` beside it): 31 tests, failures=6, errors=4. Green = the same on the fix commit.

| Finding | Test (tests/test_b6_unattended_close.py) | Red on 700d41f8 | Green |
|---|---|---|---|
| HIGH adoption window (B6-7, B6-2) | ReviewAdoptionTests.test_B6_7_adoption_counts_the_latest_lease_and_reservation_take_and_dates_a_held_row_without_a_due_time | `86403600 != 86403500` (due from the checkpoint, not the lease take) | pass |
| HIGH retained history (B6-2) | ReviewRetainedHistoryTests.test_B6_2_a_complete_close_does_equal_work_at_0_and_100000_retained_rows_of_other_orchestrations | 100,000: `history_orchestration_unavailable` (the 4,096 bound over other orchestrations' rows); 0: the other orchestrations' two neighbour leases are parsed by the project-wide scan (`history_incompatible`) | pass, VM steps equal (primary and unattended close) |
| MED clock (B6-2) | ReviewClockTests.test_B6_2_the_service_pass_closes_at_the_floor_after_the_wall_clock_went_back | `('held', None)`: the service pass skipped the close | pass |
| MED crash copy (B6-7) | SchemaStepTests.test_B6_7_process_death_inside_the_step_leaves_20_and_a_later_attempt_upgrades (assertions added) | 2 copies after the retry | pass, 1 copy (format 20), no marker/partial |
| MED layout (B6-7) | ReviewLayoutTests.test_B6_7_an_index_predicate_whose_literal_differs_only_in_case_is_refused | accepted (failed later, `history_unavailable`), not refused before any write | pass |
| MED state (B6-8) | ReviewStateValidationTests.test_B6_8_a_closed_state_and_released_row_of_an_active_orchestration_is_refused_at_open | opened (`HistoryError not raised`) | pass |
| LOW clamp (B6-4) | ReviewBatchTests.test_B6_4_an_explicit_limit_above_four_is_clamped_to_four | 6 closed | pass |
| LOW cap registry | tests.test_lifetime_cap_guard (existing) | `<module>` and `_open_count` unlisted | pass |

## Final fix (decision 210: one targeted fix of fixed scope after the Codex confirming check of 020e530d)

Red = the new tests run on 020e530d's code (before any code change): 8 tests, failures=4, errors=2 (the cap test and
the retained-history test pass there, as expected). Item 5's cap test is red on 700d41f8 (`git archive`, the final
test file beside it): `history_orchestration_unavailable` from `_positions`, the bound itself. Green = the fix commit.

| Item | Test (tests/test_b6_unattended_close.py) | Red | Green |
|---|---|---|---|
| 1 HIGH lease selection (B6-2) | FinalLeaseSelectionTests.test_B6_2_a_held_lease_whose_binding_handle_was_changed_refuses_the_close_and_stays_held | 020e530d: `HistoryError not raised` (the close skipped its held lease) | pass: close refused, lease and rows held |
| 2 orphan consumption (B6-2) | FinalOrphanConsumptionTests.test_B6_2_an_orphan_consumption_of_the_target_refuses_both_closes | 020e530d: `HistoryError not raised` | pass; plan uses `project_execution_consumptions_orchestration`; ReviewRetainedHistoryTests equal VM steps still (its consumption grants now name their owners) |
| 3 crash copy (B6-7) | FinalCrashCopyTests.test_B6_7_a_stale_marker_after_inode_reuse_leaves_a_copy_it_did_not_make_alone | 020e530d: the copy it never made was deleted (`FileNotFoundError`) | pass |
| 3 crash copy (B6-7) | FinalCrashCopyTests.test_B6_7_a_dead_attempts_copy_with_another_hard_link_is_left_alone | 020e530d: empty marker content (`JSONDecodeError`) | pass: content names path/size/sha256; linked copy kept |
| 4 unverifiable pointer (B6-8) | FinalUnverifiablePointerTests x2 (state `closed` with released row; state `open`) | 020e530d: both opened (`HistoryError not raised`) | pass: `history_incompatible` |
| 5 cap isolation (B6-2) | FinalCapIsolationTests.test_B6_2_more_than_4096_valid_rows_of_another_orchestration_do_not_refuse_a_close | 700d41f8: `history_orchestration_unavailable` (4,097 VALID-seal neighbours); 020e530d: pass | pass |
