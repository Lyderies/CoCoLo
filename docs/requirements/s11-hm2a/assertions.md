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

## Final fix (confirming check r2c)

No existing test assertion was removed, weakened or changed; six tests were added (five in `test_health_workspaces`
`FinalFixTests`, one in `test_codex_preparation`). Changed, old -> new, with the reason:

- `tests/lifetime_caps.json` `health_workspaces.py::_listing`: "ends" old, a rotating cursor and "nothing else is kept
  between passes" -> new, the kept listing (every entry read counts, deadline every `_LISTING_DEADLINE_EVERY`
  entries, when it is closed); witness old `test_HM2A_6_more_entries_than_one_pass_scans_are_reached_by_later_passes`
  (still present and green) -> new `test_HM2A_6_a_cursor_deep_in_a_huge_folder_stays_within_the_cap_and_progresses`
  (it is the test that goes past the cap with a deep position). Reason: review MED, the cursor is gone.
- `tests/lifetime_caps.json`: new `health_workspaces.py::_finish` (1 check, the `MAX_MARKER_BYTES` size check on the
  marker read before it is removed), witness `test_HM2A_6_a_folder_rmdir_failing_after_its_marker_went_is_still_resumed`.
- Requirement statements HM2A-1, -5 and -6 amended (s11-hm2a.md, tests/requirements/s11.json,
  scripts/test-requirements.json, where the new checks are listed); each earlier clause is kept, the final-fix
  clause is added. In s11-hm2a.md the limit line "chmod by name needs a no-follow chmod" is replaced by the
  O_SEARCH descriptor rule, and "(removed last)" by the check-then-unlink-then-rmdir order.
- `WorkspaceCheck.cursor` (an attribute no test read) is replaced by `WorkspaceCheck._scan` and `close()`.
- `codex_preparation._write_owner_marker` now returns None or a fixed reason code instead of always None; its caller
  ignores the value (the folder without a marker is the record the health check reads).

## IP-B 0.8.68 wiring (HM2A-11)

- `test_health_containers...CollectorWiringTests.test_HM_1_9...`: check names `['self_check', 'containers']` -> `['self_check', 'containers', 'workspaces']`, and the view's states likewise (S11 registers the workspaces check; holds is absent there because that collector has no ledger owner). The fixture patches `WorkspaceCheck._container_path` to None so the pass does not read the machine's real scratch folder.
- `test_health_monitor...SweepTests.test_HM_0_5_a_collector_with_only_its_own_checks_is_healthy`: expected check names gain `'workspaces'` the same way. SweepTests, StatusTests and ScopeTests.test_HM_0_4_the_collectors_health... gain the same `_container_path` patch (fixture isolation only; no assertion weakened: without it a developer machine's own leftover `prepare-*` folders would be reported as findings).

## Review r1 of IP-B (HM2A-11 bound)

- No existing assertion changed or removed. Added two tests (index-backed read among 50,000 tombstones; deadline with an injected clock returns None, then the full answer on a fresh clock). `LIMIT 65` plus `ORDER BY` (SCAN + temp sort, no deadline) is replaced by the primary-key range `project_id=? AND kind='preparation'`, no sort, a progress handler and loop checks against one deadline, always cleared in finally. No new index (the primary key already serves it; its range still visits that project's preparation rows, which the deadline bounds). A store without `history_identity` (the bare table of the older bound test) is read by the same filter without the project key, under the same deadline.

## Decision 365 (0.8.69 live fix: workspaces records_unreadable)

No test assertion was removed, weakened or changed; eight tests were added (`LiveFindingTests`, HM2A-12 and HM2A-13),
all red on `eb1eae9a` where they could be (six failed or errored there; the corrupt-store and no-reason-finding tests
pin behaviour that must not change and passed). Changed, old -> new:

- `tests/lifetime_caps.json`: key `memory_holds.py::pending_workspaces` -> `memory_holds.py::pending_workspaces_detail`
  (the counted limits moved into the function that now does the read; `pending_workspaces` is a one-line wrapper);
  same limits, same witness test.
- `learning_store.py` wiring: `pending=lambda: memory_holds.pending_workspaces(root)` -> `pending=memory_holds.pending_workspaces_reader(root)`
  (a callable with the same return value plus a `reason` attribute); `check._pending()` still returns a set or None.

## Decision 365, review r1 fix round (HM2A-12, HM2A-13)

Five tests added (all red on 00451171). One fixture changed, no assertion removed or weakened:

- `LiveFindingTests.history(older=True)`: old = drop `project_holds` only (a current-version store left without the table);
  new = drop `project_holds` and set `PRAGMA user_version=17`, the real older shape (project_holds arrived with step 18).
  Reason: the rule now accepts zero only for a validated older store, so a current-version store missing the table is damage
  (pinned by `test_HM2A_12_a_current_store_that_lost_project_holds_is_not_the_older_shape`). The three tests using it keep
  their assertions.
- `memory_holds._read_pending`: a history with no or several `history_identity` rows now raises the unrecognised reason
  (`history_unrecognised:<project id>`) instead of `history_unreadable:DatabaseError:<project id>`; no existing test pinned the old text.
- `memory_holds`: a pending row whose evidence is not an object, or whose `workspace` is not text, is `evidence_unreadable:<project id>`
  (before: a non-object raised AttributeError; a missing workspace was silently skipped). Every success is also checked against
  the single deadline after the open and the last query.
- Requirement statements HM2A-12 and HM2A-13 amended (s11-hm2a.md, tests/requirements/s11.json, scripts/test-requirements.json).
