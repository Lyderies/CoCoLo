# Assertion changes

Accepted B2 supersedes temporary untracked status. No original custody or
lifecycle safety assertion is removed. B3's bounded failed-view assertions are
reused unchanged by the new socket-free regression test.

| Existing test | Old assertion | Replacement and reason |
|---|---|---|
| `test_holds_report.HoldsReportTests.test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields` | tracking is not_tracked for driver_entry, preparation and orchestration | tracking is tracked for driver_entry and preparation; orchestration retains its pre-unit not_tracked expectation under decision 286. Identical zero/null and closed-field assertions. |
| `test_holds_report.HoldsProtocolTests.test_B7_6_nested_missing_extra_types_text_bounds_and_order_refused` | the malformed variant sets driver_entry tracking to tracked | the malformed variant sets it to not_tracked; the same invalid-response assertion remains. A tracked reply is now valid; the decoder rejects the superseded claim. |
| `test_rsf_s6_classify.ClassificationTests.test_mixed_store_backfills_orchestration_open_duty_and_preparation_without_guessing` | migrated memory categories say not_tracked | categories say tracked, with the existing zero-hold assertions retained. Historical memory is still never reconstructed; the new lifecycle is tracked. S6-5's registry statement changes only this temporary display expectation. |
| `test_b7b_budgets.BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures` | exactly three untracked kinds, with an instruction to revisit B1/B6 when tracking arrives | only orchestration remains untracked under decision 286; three samples of all four actual memory writer transactions including commits, their median sum below 5 ms, and two memory rows released by positive-lock recovery with three timing samples. The unchanged full-normal-run B1 and generic recurring-arrival B6 assertions still require unmeasured status and explicit reasons. Every prior B3/B4/B5/S3 numeric limit and VM-work equality assertion remains unchanged. Memory-only measurements never claim the wider budgets. |

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
That repair flagged the orchestration switch and the budget tripwire replacement.
Decision 286 resolves orchestration in the fix round below; B1/B6 remain unmeasured.

## Review fix round (decision 286)

Every assertion changed from `f69ff690` is paired here. Memory tracking assertions,
the S6 memory-only display assertion and budget measurements remain intact.

| Test | Old at review head | Replacement and reason |
|---|---|---|
| `test_holds_report.HoldsReportTests.test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields` | every kind tracked | orchestration not_tracked, all other kinds tracked; restores its pre-unit form, retaining every zero/null field assertion. |
| `test_b7b_budgets.BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures` | UNTRACKED_KINDS is empty | UNTRACKED_KINDS is exactly orchestration; restores the pre-unit orchestration membership while memory stays tracked. All timing/status assertions retained; socket module awaits the primary. |
| `test_memory_holds.MemoryHoldTests.test_registration_rollback_validator_and_status` | driver_entry, preparation and orchestration tracked with held=1 | driver_entry and preparation still tracked with held=1; orchestration has the pre-unit report: not_tracked, held=0, releasing=0, past_due=0, next_eligible=null, oldest_taken_at_ms=null. Its durable row is unchanged; display remains B7b scope. |
| `test_memory_holds.MemoryHoldTests.test_status_memory_blocks_close_eligibility_and_has_holds` | initial orchestration next_eligible equals the idle deadline; held memory means past_due=0/next_eligible=null; released memory means past_due=1/next_eligible non-null | assert the entire pre-unit orchestration zero/null not_tracked report at all three boundaries. Retain driver_entry held=1 and recovery_pending has_holds 1 then 0. Eligibility projection is removed from this slice under decision 286. |

LOW-1 adds refusal assertions for omitted/null directories (no registered source
or driver-entry row) and verifies lock identity from an explicit non-sibling
journal. Existing store/crash fixtures now pass their journal directory; no
assertions in those fixtures changed. Both checks use the existing RS-B-4 ID and
scenario module registration; reciprocal checks are added to the catalogue.
