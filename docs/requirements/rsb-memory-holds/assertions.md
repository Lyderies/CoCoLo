# Assertion changes

Accepted B2 supersedes temporary untracked status. No original custody or
lifecycle safety assertion is removed. B3's bounded failed-view assertions are
reused unchanged by the new socket-free regression test.

| Existing test | Old assertion | Replacement and reason |
|---|---|---|
| `test_holds_report.HoldsReportTests.test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields` | tracking is not_tracked for driver_entry, preparation and orchestration | tracking is tracked for every kind; identical zero/null and closed-field assertions. B2 tracks memory and B6 already tracks orchestrations. |
| `test_holds_report.HoldsProtocolTests.test_B7_6_nested_missing_extra_types_text_bounds_and_order_refused` | the malformed variant sets driver_entry tracking to tracked | the malformed variant sets it to not_tracked; the same invalid-response assertion remains. A tracked reply is now valid; the decoder rejects the superseded claim. |
| `test_rsf_s6_classify.ClassificationTests.test_mixed_store_backfills_orchestration_open_duty_and_preparation_without_guessing` | migrated memory categories say not_tracked | categories say tracked, with the existing zero-hold assertions retained. Historical memory is still never reconstructed; the new lifecycle is tracked. S6-5's registry statement changes only this temporary display expectation. |
| `test_b7b_budgets.BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures` | exactly three untracked kinds, with an instruction to revisit B1/B6 when tracking arrives | no untracked kinds, three samples of all four actual memory writer transactions including commits, their median sum below 5 ms, and two memory rows released by positive-lock recovery with three timing samples. The unchanged full-normal-run B1 and generic recurring-arrival B6 assertions still require unmeasured status and explicit reasons. Every prior B3/B4/B5/S3 numeric limit and VM-work equality assertion remains unchanged. Memory-only measurements never claim the wider budgets. |

New-test fixture corrections after their first run (no assertions removed or
weakened): M3 advances beyond the preparation take's new activity deadline; M7
uses B3's canonical run ID; M2 observes its queued status write; I1 queues all
three initial intents before sampling its preparation-only oracle and gives
each submit a distinct handle. A separate 1,000-step oracle adds release/reply
states and quiescent equality. The executable lease fixture supplies its required
assignment. L7's checkpoints use the actual writer handoff and actual lane detach,
then the real successor fences each lease once. The discard checkpoint gates the
actual cleanup worker before it can mark the slot cleaned.

## Repair round review

No existing assertion was changed or reverted in the repair. The four rows above
were reviewed against decision 129.1 A: the memory-kind `tracked` rows follow it.
Flagged for the primary (not reverted, since reverting would require removing
behaviour, not test edits): the `orchestration` tracking switch (b6.md lists it as
B7b's) and the `test_b7b_budgets` tripwire replacement (B1/B6 stay unmeasured).
