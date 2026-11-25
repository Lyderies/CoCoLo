# S11 assertions

No existing assertion was removed or changed. `health_monitor.py` and `learning_store.py` are unchanged (see
[the requirements](../s11-hm2a.md), "Limits stated plainly").

Two tests were strengthened after the red commit, before any review, because their first form would have passed
without the limit they name (a new test or tighter proof, nothing weakened):

- `test_HM2A_6_more_journal_entries_than_the_reader_takes_remove_nothing`: old, 4,097 files holding `{}` and an
  expectation of `None` (which an unreadable entry gives too) -> new, the entry reader is patched to return a readable
  entry, exactly `MAX_JOURNAL_ENTRIES` files read in full (a set), one more gives `None`.
- Added `test_HM2A_6_a_reader_that_hands_back_more_paths_than_the_check_takes_removes_nothing` (HM2A-6) and
  `test_HM2A_10_register_adds_the_check_to_a_monitor_and_a_pass_removes_and_shows_it` (HM2A-10) with the code.
