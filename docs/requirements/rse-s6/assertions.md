# rse-s6 review fix (040-rse-s6 r1): existing assertions changed, old -> new

Every one is listed with the requirement that decides it. No assertion was removed.

1. `WiringTests.test_E1_7_an_update_never_asks_and_an_answers_file_can_answer` (from before S6)
   - old: an update with answers `{"login_start": true}` and no recorded answer creates the entry
     (`assertTrue(entry_path(...).exists())`).
   - new: nothing is run and no entry is created (`assertEqual([], runner.calls)`,
     `assertFalse(entry_path(...).exists())`).
   - reason: E1-9 "with no recorded yes, or with no entry, nothing is written" and E1-4/E1-7 "an update never asks":
     an answers-file value in an update is an implied answer, not a recorded yes. E1-9 is the later, specific rule;
     the old assertion pinned the gap the RS-E closure analysis listed (ANALYSIS-0.4.0-rsd-rse-closure.md line 58).
     The test name is kept so the catalogue keeps its check id.
2. `InstallTests.test_E1_6_an_unreadable_old_entry_is_refused_not_replaced` (from before S6)
   - old: the unreadable entry is injected by patching `Path.read_bytes` for the entry path.
   - new: injected by patching `os.open` for the entry's name opened below its checked folder descriptor.
   - reason: E1-6/E1-8 now read the entry through folder descriptors without following links (review HIGH 2), so
     `Path.read_bytes` is no longer the read. The assertions themselves are unchanged (`not ok`, "could not be read",
     tree unchanged, no runner call).
3. `UpgradeTests` (S6's own tests, commit 1da80aa4): the "new authorized runtime" input changes from the stand-in
   `DIGEST_B` / `DIGEST_A` to `ATTESTED` (this setup's `COLLECTOR_RUNTIME_DIGEST`, with `PACKAGE_VERSION`):
   - `test_E1_9_an_update_that_moves_the_collector...`: `authorize(DIGEST_A)`, `write_entry(..., DIGEST_A)`,
     `entry_status(..., DIGEST_A)` -> `ATTESTED` in all three.
   - `test_E1_9_an_update_that_changes_the_runtime_digest...`: `authorize(DIGEST_B)` -> `authorize(ATTESTED)`;
     `assertEqual(DIGEST_B, read_record(...))` -> `assertEqual(ATTESTED, read_record(...))`.
   - `test_E1_9_an_entry_that_cannot_be_rewritten...`: `authorize(DIGEST_B)` -> `authorize(ATTESTED)`;
     `assertNotIn(DIGEST_B, screen)` -> `assertNotIn(ATTESTED, screen)`.
   - `test_E1_9_a_current_entry_a_missing_entry_and_no_consent...`: second `authorize(DIGEST_B)` -> `authorize(ATTESTED)`.
   - `RuntimeHome.authorize` default version `"0.8.55"` -> `PACKAGE_VERSION`; `RuntimeHome.setUp` makes the state
     folder owner-only (0700), as the collector's own state check requires.
   - reason: E1-9 as clarified by review HIGH 1 and E1-10: the runtime an entry is rewritten for must pass the
     collector's own active-identity check against the attested runtime; an arbitrary digest is now (correctly)
     refused, which `ReviewFixTests.test_E1_9_a_rewrite_needs_the_collectors_active_identity_for_the_attested_runtime`
     pins. What each test checks (rewrite without asking, record kept, no rewrite when current, stale with the earlier
     entry kept, no private value shown) is unchanged.
