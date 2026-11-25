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

## Fix round (review r1)

No assertion was removed or weakened. Changed, old -> new, with the reason:

- `Case` fixture (`tests/test_health_workspaces.py`): old, `folder()` made a bare folder and `check()` built the check
  without a state root -> new, `folder()` also writes an owner marker naming a stopped launch-journal instance (its
  lock file exists, unheld) under `self.state`, and `check()` passes `state_root=self.state` (`marker=False` gives
  the old bare folder). Reason: HM2A-1 now requires positive owner proof (primary decision), so the earlier removal
  tests need an owner proven gone to test what they assert. Every earlier assertion is unchanged, and all 25 earlier
  tests pass on the r1 code with this fixture (shim run), so the fixture weakens none of them.
- `test_HM2A_10_register_adds_the_check_to_a_monitor_and_a_pass_removes_and_shows_it`: old
  `register(monitor, self.base / 'state', ...)` -> new `register(monitor, self.state, ...)`: the same path
  (`self.state` is `self.base / 'state'`), named through the fixture whose markers it must match.
- `tests/lifetime_caps.json`: `health_workspaces.py::run` (3 checks) -> `::_pass` (4: the owner-unknown age line is
  new); `::_listing` witness `test_HM2A_6_more_folders_than_one_pass_reads_are_reported_and_the_rest_still_work`
  (257 folders, below the 1,024 scan cap) -> `test_HM2A_6_more_entries_than_one_pass_scans_are_reached_by_later_passes`
  (1,025 entries; review LOW); `::read` 2 -> 4 checks, witness -> the projects-scan test; `::_records` 1 -> 2 checks,
  witness -> the items test; new `::spent`, `::_remove_tree`, `::_owner`.
- Requirement statements HM2A-1, -5, -6, -7 amended (s11-hm2a.md, tests/requirements/s11.json, test-requirements.json)
  to the fix-round clauses; the earlier clause text is contained in each.
- `too_many_folders` finding text: old "holds more than 256 preparation folders" -> new "holds more entries than one
  check looks at" (it now also covers the entry scan); no test asserted the old text.
