# S11 status - HM-2a

Branch `feat/s11-hm2a`, base `ce81bd96` (0.8.67). Requirements and 23 red tests are committed first; the module,
caps and registrations follow. Not merged, not installed, VERSION and digest catalogue unchanged.

- Code: `codex_agents/health_workspaces.py` (new; reads launch journals read-only; pending reader injected).
- Tests: `tests/test_health_workspaces.py` (25), mapped in [s11-hm2a.md](../s11-hm2a.md); registered in
  `scripts/test-layers.json`, `test-stages.json`, `test-timings.json`, `scripts/test-requirements.json`
  (source `tests/requirements/s11.json`); count limits in `tests/lifetime_caps.json`.
- For the primary after S5: wire `health_workspaces.register(monitor, state_root, pending=<reader>)` in
  `learning_store.py` next to the container check, where `<reader>` returns the `workspace` paths of preparation hold
  rows whose evidence says `removal: pending`, read through the owner thread (E1b-1, decision 311), or None when it
  cannot be read. Until then the check is not part of the collector.
- Run: `scripts/parallel_tests.py --jobs 4 --module codex:tests.test_health_workspaces --module
  codex:tests.test_health_monitor --module codex:tests.test_codex_preparation --module
  codex:tests.test_lifetime_cap_guard` with a short TMPDIR; expect `metadata_errors: []`.

- `runtime_files.py` lists the new module (a test keeps the list equal to the package). The runtime digest in `runtime-releases.json` is now stale by design: the primary bumps VERSION and records it at merge (`tests.test_runtime_release` fails until then).

## Fix round (review r1: NOT-READY, 3 HIGH, 3 MED, 1 LOW)

Review: `reviews/040-s11-r1/RESULT-codex.md` in the control checkout. Principle (primary decision): deleting a folder
needs positive proof that its owner is gone; when in doubt nothing is deleted and it is reported. Tests were written
first; all 20 new tests (19 in `test_health_workspaces`, 1 in `test_codex_preparation`) were red on the r1 code (run with a shim that only accepts the new `state_root` argument, so
each fails on behaviour, not on the signature) and are green now. The 25 earlier tests pass on both.

| Item | Change | Red on r1 code -> green |
| --- | --- | --- |
| HIGH 1 owner proof | Runtime change: the preparation lane writes `cocolo-owner.json` (state root, project, journal instance; owner-only, O_EXCL, no-follow) in each new folder once `CodexPreparationLane.set_owner` is called; `RunDriver` calls it right after its `LaunchJournal` exists. The check removes a folder only when the marker names its state root and `launch_journal.instance_stopped` (free lock) holds. No marker, oversized marker, other state root, missing lock file or no state root: `owner_unknown`, never removed. Held lock: kept even after a 30-day clock jump. | `OwnerProofTests` (8, all removed the folder or errored on r1), `test_codex_preparation...test_HM2A_1_a_preparation_folder_names_the_service_that_owns_it` (no `set_owner`) |
| HIGH 2 TOCTOU | Container opened once per pass O_DIRECTORY/O_NOFOLLOW, fstat-verified (dir, uid, same dev/ino as its path); every level opened relative to its parent fd and checked against the looked-at dev/ino; unlinkat/rmdir/fchmod relative to pinned fds only; marker removed last. | `PinnedRemovalTests`: container swap (r1 deleted the decoy folder), subfolder swap (r1 chmodded the decoy 0o500 -> 0o700) |
| HIGH 3 truncation | Projects listing counted in full against `MAX_ENTRIES_SCANNED`; reader items bounded by `MAX_RECORD_ITEMS`, distinct paths counted after dedupe; any truncation -> "incomplete", nothing removed. | `TruncationTests` probe 1 (r1 returned `set()`), probe 2 (r1 removed the live folder), items cap |
| MED 4 bounds | `MAX_REMOVAL_ENTRIES_PER_PASS`, `MAX_REMOVAL_DEPTH`, deadline checked before every unlink/rmdir; unfinished folder reported `orphan_removal_continues` and resumed; launch folders scanned with a running `MAX_JOURNAL_NAMES` count. | `FolderBoundTests` (r1 removed the whole folder past the deadline/limit), `test_HM2A_7_a_launches_folder_longer...` |
| MED 5 starvation | Rotating positional cursor (`WorkspaceCheck.cursor`), wraps at the end. | `CursorTests` (r1 never reached the leftover) |
| MED 6 absence | Absence only from FileNotFoundError on a stat in the pinned container. | `test_HM2A_1_only_not_found_proves_a_folder_gone` (r1 reported EACCES as fixed) |
| LOW 7 witness | `_listing` witness is now `test_HM2A_6_more_entries_than_one_pass_scans_are_reached_by_later_passes` (1,025 entries, past the real 1,024). | see mutations |

Mutation evidence (each applied alone to `health_workspaces.py`, named test run, file restored): all 13 killed.

| Mutation | Killed by |
| --- | --- |
| M1 container scan cap `examined >= MAX_ENTRIES_SCANNED` removed | `test_HM2A_6_more_entries_than_one_pass_scans_are_reached_by_later_passes` |
| M2 owner proof bypassed (`owner = 'stopped'`) | `OwnerProofTests` (6 failures) |
| M3 any OSError proves absence | `test_HM2A_1_only_not_found_proves_a_folder_gone` |
| M4 cursor removed (`self.cursor = 0`) | `CursorTests` (2 failures) |
| M5 `MAX_REMOVAL_ENTRIES_PER_PASS` removed | `test_HM2A_6_removal_inside_one_folder_stops_at_its_entry_limit_and_resumes` |
| M6 deadline inside a folder removed | `test_HM2A_6_removal_inside_one_folder_stops_when_the_time_share_ends` |
| M7 `MAX_REMOVAL_DEPTH` removed | `test_HM2A_6_a_folder_deeper_than_the_remover_goes_is_reported_and_kept` |
| M8 `MAX_RECORD_ITEMS` removed | `test_HM2A_7_a_reader_handing_back_more_items_than_the_check_reads_is_incomplete` |
| M9 projects scan cap removed | `test_HM2A_7_a_projects_folder_longer_than_the_reader_scans_is_incomplete` |
| M10 `MAX_JOURNAL_NAMES` removed | `test_HM2A_7_a_launches_folder_longer_than_the_reader_takes_is_incomplete` |
| M11 r1's count-before-dedupe (`islice(value, MAX_RECORD_PATHS + 1)`) | `test_HM2A_7_repeated_paths_do_not_hide_a_live_tail` |
| M12 pathname remover (`host_paths.remove_private_directory`) | `PinnedRemovalTests` |
| M13 `MAX_RECORD_PATHS` check removed | `test_HM2A_6_a_reader_that_hands_back_more_paths_than_the_check_takes_removes_nothing` |

Runs (TMPDIR a short new folder under /tmp/claude-501): `scripts/parallel_tests.py --jobs 4 --module
codex:tests.test_health_workspaces --module codex:tests.test_codex_preparation --module codex:tests.test_health_monitor
--module codex:tests.test_lifetime_cap_guard`: 108 tests, `failed_units: []`, `metadata_errors: []`, 26 s wall. Also
`test_host_paths`, `test_codex_verifier_preparation`, `test_memory_holds_lane`, `test_run_driver_retries`: 57 tests
green, 16 s. Not run here (primary, before merge): `tests.test_project_driver` (216 s; `RunDriver.__init__` gained the
`set_owner` call) and the `--affected` run. VERSION and digest still unchanged (runtime files changed:
`health_workspaces.py`, `codex_preparation.py`, `host_paths.py`, `project_driver.py`).

Open for the primary: leftovers of a service that stopped NORMALLY have no lock file (a normal release removes it),
so they are only reported; under decision 309 such folders are pending records that the next driver retries.

## Final fix (confirming check r2c: NOT-READY, 1 HIGH, 3 MED)

Review: `reviews/040-s11-r2c/RESULT-codex.md` in the control checkout. Last round: only the four listed items were
fixed. Principle kept: deletion needs positive proof that the owner is gone; when in doubt nothing is removed. Each
new test was run red on `e9d54732` (the e9d54732 `health_workspaces.py` and `codex_preparation.py` with the new
tests: 6 failures, the 44 earlier tests green) and is green now.

| Item | Change | Red on e9d54732 -> green |
| --- | --- | --- |
| HIGH HM2A-5 chmod by name | `_open_folder` no longer chmods by name. Before owner proof `_owner` uses `_look_folder` (read-only open, else an O_SEARCH open; never a mode change). After owner proof an unlistable level is opened O_SEARCH, O_DIRECTORY, O_NOFOLLOW, verified by fstat (dev, ino, uid, directory), fchmodded through that descriptor, then reopened and verified again. No O_SEARCH (or mode 000): left and reported. | `FinalFixTests.test_HM2A_5_permission_recovery_never_changes_a_folder_swapped_in_before_it` (a 0o100 replacement swapped in before the open: old 0o100 -> 0o700, now unchanged; later passes still remove the real folder), `test_HM2A_1_a_folder_whose_owner_is_not_proven_keeps_its_mode` (no marker, 0o300: old -> 0o700, now unchanged, `owner_unknown`) |
| MED HM2A-6 marker before final check | `_finish`: the last budget/deadline check runs before the marker goes; the marker is read, unlinked and the folder rmdir'd at once with nothing between; a failed rmdir puts the marker back (same bytes, atime/mtime via futimes). | `test_HM2A_6_a_deadline_at_any_removal_step_leaves_the_folder_resumable` (CountedContext 0..N; old failed at calls=15: marker gone, folder never removed), `test_HM2A_6_a_folder_rmdir_failing_after_its_marker_went_is_still_resumed` |
| MED HM2A-6 cursor skipping | The positional cursor (`self.cursor`) is replaced by a kept listing (`self._scan`: container dev/ino and its open scandir). The next pass goes on with it, so nothing is skipped; every entry read counts toward `MAX_ENTRIES_SCANNED`; the time share is checked every `_LISTING_DEADLINE_EVERY` (64) entries (a small container is still listed by an expired pass, as `test_HM2A_6_removal_stops_when_the_time_share_ends` requires). Closed at the end, on an error, when the container path names another folder, by `close()` and `__del__`. | `test_HM2A_6_a_cursor_deep_in_a_huge_folder_stays_within_the_cap_and_progresses` (3,082 entries: old pass read 1,025 then cursor+1,024; now each pass <= 1,024, an expired pass <= 64, orphan removed within 5 passes) |
| MED runtime marker close | `_write_owner_marker` catches OSError from close (and a short write), unlinks the partial marker and returns a fixed reason code (`owner_marker_not_closed` / `owner_marker_not_written`) instead of raising. | `tests.test_codex_preparation...test_HM2A_1_a_marker_that_fails_to_close_leaves_a_report_only_folder` (old: `preparation_failed_at_workspace`; now ready, no marker, the check reports `owner_unknown` and keeps the folder) |

Run (unsandboxed, TMPDIR a short new folder under /tmp/claude-501): `scripts/parallel_tests.py --jobs 4 --module
codex:tests.test_health_workspaces --module codex:tests.test_codex_preparation --module codex:tests.test_health_monitor
--module codex:tests.test_lifetime_cap_guard --module codex:tests.test_host_paths`: 131 tests, `failed_units: []`,
`metadata_errors: []`, 25 s wall. VERSION and digest unchanged. Not run here (primary, before merge): the
`--affected` run and `tests.test_project_driver`.
