# S11 - HM-2a: leftover CoCoLo preparation folders (Unit 3.9, health monitor)

Authority: plan step S11 (HM-2, SW-8), owner decision 309 (preparation cleanup stays with the
driver), E1b-1 and decision 311 (ledger and history access goes through the owner thread).
Base: `ce81bd96` (0.8.67). VERSION and the digest catalogue stay unchanged.

New module `codex_agents/health_workspaces.py`: one health check, `workspaces`. It reads the
preparation folders in CoCoLo's scratch area and the records of who owns them. It never edits
`codex_preparation.py` state and never opens the ledger or history itself. Where it learns what is
owed to a driver (decision 309's pending removals) it is given a reader by its caller, and that
reader goes through the owner thread (HM2A-9).

A folder is "ours" only if it is `prepare-*`, directly in the real scratch container, a real folder
of this user (the same test as `codex_preparation.remove_recorded_workspace`, decision 309).

| ID | Accepted clause | Matching tests (`tests.test_health_workspaces`) |
| --- | --- | --- |
| HM2A-1 | A CoCoLo `prepare-*` folder with no live owner and no pending record, old enough to be sure, is removed. The removal is checked by looking for the folder afterwards. The pass reports it as fixed, in one plain sentence that names the folder, not its path. A removal that did not prove the folder gone is reported and tried again at the next pass. | `OrphanTests.test_HM2A_1_an_orphan_folder_is_removed_and_reported_fixed`, `test_HM2A_1_a_removal_that_does_not_prove_the_folder_gone_is_reported_and_retried` |
| HM2A-2 | A folder a live driver owns is kept: a folder named by any launch-journal entry of any project of this state root (any state, any service instance) is live. A folder younger than the minimum age is kept too (its owner may not have recorded it yet). | `LiveOwnerTests.test_HM2A_2_a_folder_named_by_a_launch_journal_entry_is_kept`, `test_HM2A_2_a_young_folder_is_kept_and_not_reported` |
| HM2A-3 | A symlink, a folder of another owner, or a file named like a preparation folder is reported and left exactly as it is: it is never followed, never removed, and what a link points at is never touched. | `UnsafeEntryTests.test_HM2A_3_a_symlink_is_reported_and_never_followed_or_removed`, `test_HM2A_3_a_foreign_owned_folder_is_reported_and_kept`, `test_HM2A_3_a_file_named_like_a_preparation_folder_is_reported_and_kept` |
| HM2A-4 | A folder pending under decision 309 (a driver recorded that it still owes its removal) is left to the driver: not removed, no double removal. Only a folder still pending after `PENDING_REPORT_PASSES` passes is reported, so the owner hears about one that never goes. Once it is gone nothing about it is kept. | `PendingTests.test_HM2A_4_a_pending_folder_is_left_to_the_driver`, `test_HM2A_4_a_folder_pending_for_many_passes_is_reported_then_forgotten_when_gone` |
| HM2A-5 | Path safety equals decision 309's `remove_recorded_workspace`: an absolute path, no `..`, a `prepare-*` name, the real scratch container as parent, a real folder owned by this user. A container that is itself a link, or not a real folder of this user, makes the pass remove nothing and say so. | `PathSafetyTests.test_HM2A_5_the_path_test_matches_remove_recorded_workspace_case_by_case`, `test_HM2A_5_a_container_that_is_a_link_is_reported_and_nothing_is_removed`, `test_HM2A_5_names_outside_the_prepare_pattern_are_never_listed` |
| HM2A-6 | Reads and work are bounded: at most `MAX_FOLDERS_LISTED` folders are read in a pass, at most `MAX_REMOVALS_PER_PASS` removed (the rest wait and are said to wait, and go at later passes), and removal stops when the check's time share ends. The launch-journal reader reads at most `MAX_JOURNAL_PROJECTS` projects and `MAX_JOURNAL_ENTRIES` entries, and a record reader may hand back at most `MAX_RECORD_PATHS` paths; past that the live set is not complete, so nothing is removed. Each limit is listed in `lifetime_caps.json` with a test that goes past it. | `BoundTests.test_HM2A_6_more_orphans_than_one_pass_removes_wait_and_all_go`, `test_HM2A_6_more_folders_than_one_pass_reads_are_reported_and_the_rest_still_work`, `test_HM2A_6_removal_stops_when_the_time_share_ends`, `test_HM2A_6_more_journal_entries_than_the_reader_takes_remove_nothing`, `test_HM2A_6_more_journal_projects_than_the_reader_takes_remove_nothing`, `test_HM2A_6_a_reader_that_hands_back_more_paths_than_the_check_takes_removes_nothing` |
| HM2A-7 | If the owner records cannot be read completely (the launch journals, or the pending records), nothing is removed that pass and one finding says so. A check that is not given both readers does nothing at all. | `FailSafeTests.test_HM2A_7_unreadable_live_records_remove_nothing_and_are_reported`, `test_HM2A_7_unreadable_pending_records_remove_nothing_and_are_reported`, `test_HM2A_7_a_reader_that_raises_is_reported_by_type_only`, `test_HM2A_7_a_check_without_its_readers_does_nothing` |
| HM2A-8 | Owner wording is plain: every finding is a closed `Finding` of one sentence that names the folder, never its path, an exception message or a decision number; scope is the machine (a folder belongs to no one project). | `WordingTests.test_HM2A_8_findings_are_plain_closed_and_carry_no_path` |
| HM2A-9 | The module never touches the ledger or history, and never edits preparation state: it imports no SQLite or ledger module and never calls a preparation or ledger writer. Its pending reader is injected; the primary wires it through the owner thread (E1b-1, decision 311). | `OwnerThreadTests.test_HM2A_9_the_module_has_no_ledger_or_preparation_write_access` |
| HM2A-10 | `register(monitor, state_root, pending=...)` adds the check to a health monitor with the launch-journal live set and the caller's pending reader. A pass then removes an orphan and the status view shows it fixed. | `RegistrationTests.test_HM2A_10_register_adds_the_check_to_a_monitor_and_a_pass_removes_and_shows_it` |

Limits stated plainly:

- The check is inert until it is given its two readers. Registration in the collector (with the
  readers; the pending reader goes through the owner thread) is done by the primary after S5
  (`learning_store.py` is not edited here). `health_workspaces.register(monitor, state_root, pending=...)` is the one call.
  `health_monitor.py` is deliberately unchanged: adding the check to `with_builtin_checks` would put an inert
  check in front of three existing name-list assertions and double-register once the wired one is added.
- A launch-journal entry that cannot be read means the live set is not known, so nothing is removed
  until it is fixed (HM2A-7); the owner is told each pass.
- A folder a driver creates and has not recorded yet is protected only by the minimum age
  (`MIN_ORPHAN_AGE_SECONDS`).
- Write workspaces are part b (S21), not here.
