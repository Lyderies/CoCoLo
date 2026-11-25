# Assertion replacements

Owner decision 309 supersedes the former stop-after-twelve policy. No other
existing assertion is removed or weakened.

- `GivenBackPreparationTests.test_a_removal_that_keeps_being_refused_is_asked_a_bounded_number_of_times_and_kept`:
  old: exactly `MAX_CLEANUP_ATTEMPTS` requests after seventeen due steps;
  replacement: `test_PREPCLEAN_2_retries_continue_past_twelve_with_capped_monotone_backoff`
  asserts twenty requests, exact monotone capped pauses, no early requests,
  retained handle and no retirement. Reason: PREPCLEAN-2 requires continuing.
- `StuckSlotTests.test_M4_a_preparation_whose_removal_was_given_up_is_kept_and_counted_stuck`:
  old: thirteen due steps leave one stuck slot and permit idle replacement;
  replacement: `test_M4_pending_cleanup_keeps_custody_and_defers_replacement`
  keeps the preparation, asserts continuing retries, zero abandoned slots and
  refuses replacement even with a separate stranded run or dispatch exhaustion.
  Reason: PREPCLEAN-2 retains the live cleanup custodian until observed removal.


Exact assertion accounting (both tests are in `test_run_driver_retries.py`):

| Old assertion | Replacement | Reason |
| --- | --- | --- |
| Bounded-removal test: retry count equals `MAX_CLEANUP_ATTEMPTS` | `test_PREPCLEAN_2_retries_continue_past_twelve_with_capped_monotone_backoff`: count equals `number + 1` at each of twenty due steps, including requests 13..20; the same count is unchanged before each deadline | Decision 309 requires continued retries. |
| M4: post-loop stuck-slot count equals 1 | `test_M4_pending_cleanup_keeps_custody_and_defers_replacement`: post-loop stuck-slot count equals 0, alongside fourteen observed retry requests and the retained preparation | This remains live retry work, not abandoned work. |
| M4: `replace_when_idle()` is true while cleanup remains unknown | Same test: false while unknown, still false with an independent stranded run and dispatch exhaustion; true only after observed cleaned state and retirement | Automatic replacement cannot strand the original folder custodian. |

Preserved unchanged: the M4 initial zero-stuck assertion, both tests' retained
`['preparation']` assertions, and the bounded-removal test's assertion that
`retire` was not called while cleanup remained unknown. All other pre-existing
test assertions remain unchanged.
