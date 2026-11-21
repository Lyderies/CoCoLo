# LOW items from CoCoLo review rev040journalr1 (launch journal reader, fix/container-stop-flake)

Fixed in the round-1 fix commit
- MED: `_entry_or_unknown` (feeds the entries window) now catches JournalError and keeps the old unknown result
  ({'state': None}); test_an_entry_that_keeps_changing_is_unknown_in_the_entries_window (red before, green after).
- Docstring of `_read_entry`: _REREADS = 3 opens in all, i.e. two reopens.
- The times=2 test now also checks the entry content (attempt 9).
- `_read_entry_or_none` keeps None for status; its docstring says why. unresolved() already lists such an entry
  with state None (unknown, not missing); container_holds() leaves it out of its counts (no schema change).
  Its only caller is client_runtime's status report: display only, never a stop decision.

Notes
- `container_entry` now raises JournalError (via `_read`) when a container entry kept changing; deliberate, a
  caller must not treat "kept changing" as "no such entry".
- Of the 4 tests added in 0.8.43, 2 are guards that pass on the old code by design (two-links refused, missing
  entry is None).
- Status-reader test added: test_status_lists_an_entry_that_keeps_changing_as_unknown_not_missing. It is also a
  guard (passes on old code); it pins the behaviour of `_read_entry_or_none`.
- The claimed "line 994" site does not exist as such. The other st_nlink/fstat readers are the lock-file sweep
  (launch_journal.py ~643, opens a lock file and skips nlink != 1: a skip is safe, the lock is only a temp
  file), `_read_recovery` (fstat, no st_nlink check; a replaced report reads fine from the open descriptor)
  and the watchdog marker reader (~1013, nlink != 1 returns False). None of them reads an entry file by path
  that a writer replaces, and none has the replaced-while-open race.
