# B6 assertion ledger (old -> new, each with its reason)

Not owner-gated (analysis section 8, decisions 109/114 precedent, memory "not owner-gated"): every change below follows
the decided requirement 131 (schema step 20 -> 21) or RS-B-B6-1 (the open takes the orchestration row). No assertion
about behaviour was weakened; each line is a format pin, a copy-name pin, or a fixture line that builds an older layout.

## A. Format pins 20 -> 21 (33 lines; reason: decision 131, the store format is now 21)
- `test_project_hold_register.py:357`: `self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])` -> `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`
- `test_project_host_open.py:308`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_b2_service_outcome.py:784`: `self.assertEqual(20, project_history._SCHEMA_VERSION)` -> `self.assertEqual(21, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)`
- `test_b2_service_outcome.py:803`: `self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])` -> `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`
- `test_b2_service_outcome.py:807`: `self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-20.*'))))  # the copy made for it` -> `self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-21.*'))))  # the copy made for it`
- `test_b2_service_outcome.py:821`: `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-20.*')))` -> `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-21.*')))  # B6: schema 21 (decision 131)`
- `test_b2_service_outcome.py:823`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`
- `test_project_orchestration.py:214`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_learning_chunk_upgrade.py:94`: `self.assertEqual(20, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_open_withdrawal.py:498`: `self.assertEqual(20, project_history._SCHEMA_VERSION)` -> `self.assertEqual(21, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)`
- `test_open_withdrawal.py:516`: `self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])` -> `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`
- `test_project_qa_commit.py:294`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_learning_qa_upgrade.py:80`: `self.assertEqual(20, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_execution_grant.py:285`: `self.assertEqual(20, self.fixture.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.fixture.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:139`: `self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])` -> `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:326`: `self.assertTrue(copy.name.startswith('history.sqlite3.before-20.'))` -> `self.assertTrue(copy.name.startswith('history.sqlite3.before-21.'))  # B6: schema 21 (decision 131)`
- `test_project_results.py:733`: `self.assertEqual(20, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_results.py:806`: `self.assertEqual(20, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_result_leases.py:414`: `self.assertEqual(20, self.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_result_leases.py:457`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_learning_corpus.py:289`: `self.assertEqual(20, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_qa_check_receipts.py:243`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_history_service_upgrade.py:42`: `return sorted(path for path in self.directory.glob('history.sqlite3.before-20.*')` -> `return sorted(path for path in self.directory.glob('history.sqlite3.before-21.*')  # B6: schema 21 (decision 131)`
- `test_history_service_upgrade.py:55`: `self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_history_service_upgrade.py:115`: `impostor = self.directory / 'history.sqlite3.before-20.1'  # (requirement) SB: format 20` -> `impostor = self.directory / 'history.sqlite3.before-21.1'  # (requirement) B6: format 21 (decision 131)`
- `test_history_service_upgrade.py:167`: `existing = self.directory / f'history.sqlite3.before-20.{fixed}'  # (requirement) SB: format 20` -> `existing = self.directory / f'history.sqlite3.before-21.{fixed}'  # (requirement) B6: format 21 (decision 131)`
- `test_learning_migration.py:415`: `self.assertEqual(20, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_learning_outcome_pages.py:167`: `self.assertEqual(20, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_release_ack.py:39`: `self.assertEqual(20, db.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20` -> `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:249`: `self.assertEqual(20, self.version())` -> `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:268`: `self.assertEqual(20, self.version())` -> `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:291`: `self.assertEqual(20, self.version())` -> `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)`
- `test_project_holds_schema.py:323`: `self.assertEqual(20, self.version())` -> `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)`

## B. Fixture lines (layout builders and setup; no assertion text changed)
- `project_schema_fixtures.py`: new `legacy_orchestration_state_layout(store)` (drops the three step-21 indexes and the
  `project_orchestrations.state` column; keeps B6's held orchestration rows, which the step skips, so a fixture's rows
  come back unchanged); `legacy_project_holds_layout` calls it first and then deletes held orchestration rows (they go
  with the dropped table; its existing "only lease/reservation rows" guard is unchanged). Reason: every older-layout
  fixture is built from a format 21 store (as the schema19/20 lines did, decisions 108/114.1).
- `project_schema_fixtures.assert_migrated_rows`: new branch for `project_orchestrations` only: `before == after` ->
  `before == [row[:-1] for row in after]` plus `state in ('open','closed')` (as its existing `result_leases` branch does
  for that table's added column). Reason: step 21 adds one column to that table; every other table compares as before.
- `test_project_holds_schema.py` D111 (format 18 store builder): + drop the three step-21 indexes and the state column.
- `test_project_holds_schema.py` `OrchestrationHoldTests.holds`: `INSERT INTO project_holds` -> `INSERT OR IGNORE`.
  Reason: B6-1, the open now takes that row itself; the "no orchestration" case still inserts and is still refused.
- `test_open_withdrawal.py` D108 (format 18 builder) and `test_b2_service_outcome.py` two format 19 builders:
  + `legacy_orchestration_state_layout(self.store)` (and its import).
- `test_deadlines_retention.py` `OrchestrationCloseTests.unattended`: take the orchestration row only when the open did
  not (it now always does). Reason: B6-1; the helper's release `interrupted_unattended` is unchanged.
- `test_rsd_runtime_close.py:155`: `CREATE TEMP TRIGGER refuse_update_close BEFORE INSERT ON project_holds` ->
  `BEFORE UPDATE`. Reason: B6-1, the row exists from the open, so the update close's write is its release (an UPDATE);
  the trigger still cuts exactly that write and every assertion around it is unchanged.

## D. Behaviour changed by a decided B6 requirement (primary: please read these against the original)
- `test_result_lifetime.py:354` `test_O0c_P5_the_service_ends_idle_orchestrations_by_itself`:
  `self.assertEqual(10, calls[0][1])` -> `self.assertEqual(4, calls[0][1])`. Reason: RS-B-B6-4 (analysis section 6:
  "select by next_check_ms (4 per pass)"); the service now passes UNATTENDED_CLOSE_BATCH = 4.
- `test_b2_service_outcome_paths.py` `RealFixture.idle_end` (fixture, no assertion text changed): before the idle
  pass, the reservation's register row is released `completed`, standing in for the account release acknowledgement
  next to the existing `_account_released` patch. Reason: RS-B-B6-2, the unattended close now also requires no held
  reservation of the orchestration; without it the 4 idle subtests (`way='idle'`, both entries) saw no close
  (`1 != 0`). The primary close path of the same tests is unchanged.
- `test_lost_run_settlement.py` C3e (fixture): when the test deletes an orchestration to simulate an unreadable one, it
  now deletes its register row too. Reason: RS-B-B6-1, a held orchestration row without its orchestration is refused
  at open (the store would not open at all; the test's point, one unreadable lease never stops the others, is kept).

## C. Brought in unchanged
`tests/test_rsd_budget.py` (RS-D S3, 9a3c6860): no line changed; its two red retirement tests are green on step 21.

## E. Review round 1 (Codex security review of 700d41f8; primary: read these against the original)
- `test_b6_unattended_close.py` `test_B6_4_at_most_four_close_per_pass_and_the_service_asks_for_four` (B6's own test,
  new in 700d41f8, not yet released): `self.assertIn('end_idle(history._result_now(), UNATTENDED_CLOSE_BATCH)', source)`
  -> `self.assertIn('end_idle(None, UNATTENDED_CLOSE_BATCH)', source)`. Reason: review MED clock (RS-B-B6-2 round 1):
  the old text pinned the defect itself (`_result_now()` raises below the floor before the close starts, and moves
  the floor); the owner now reads its own effective time. The service-path behaviour is proven by the new
  ReviewClockTests. NOT covered by decision 195: listed for the primary.
- `test_b6_unattended_close.py` process-death test: assertions ADDED only (exactly one copy, format 20; no marker or
  partial file left); no existing line changed.
- Fixture lines: `project_schema_fixtures.legacy_orchestration_state_layout` and `test_project_holds_schema.py` D111
  also drop the two new step-21 indexes (`result_reservations_orchestration`, `result_leases_orchestration`).
  Reason: they build older layouts from a format 21 store (section B).
- `tests/fixtures/history_step20/make_store.py`: `--leases` variant added (the default output path and operations are
  unchanged; `store.sql` was NOT regenerated); new `store_leases.sql` generated once with the 6a1ea9da runtime.
- `tests/lifetime_caps.json`: two sites registered (`orchestration_store.py::<module>`, `::_open_count`); the
  end_earlier and runtime_close_status `ends` texts corrected (hold tombstones are never purged; decision 131's
  indexes purge nothing).

## F. Final fix (decision 210; no existing assertion changed or removed)
- Assertions: none changed, none removed. All new assertions are in the new Final* classes of
  `test_b6_unattended_close.py`.
- Fixture lines (B6's own, unreleased): `test_b6_unattended_close.retain`: the retained consumptions' grant
  `x'00'` -> a JSON grant naming its owner orchestration. Reason: item 2's index reads the grant's orchestration; with
  real-shaped grants the equal-work test measures the new index seek against 100,000 other orchestrations' rows
  (with `x'00'` they would all index as NULL). The test's assertions are unchanged.
- Fixture lines: `project_schema_fixtures.legacy_orchestration_state_layout` and `test_project_holds_schema.py` D111
  also drop `project_execution_consumptions_orchestration` (step 21's sixth index). Reason: they build older
  layouts from a format 21 store (sections B, E).
- `tests/lifetime_caps.json` (registry, no test text): `orchestration_store.py::<module>` checks 1 -> 3 (the two
  item-1 selects `_HELD_LEASES`, `_HELD_LEASE_ROWS`, LIMIT over live leases only) and new
  `orchestration_store.py::_held_leases` (checks 1); both name the new cap test.

## G. Decision 212 (no existing assertion changed or removed)
- New tests only (FinalReservationSelectionTests x2, FinalStalePartialTests x1). `tests/lifetime_caps.json`:
  `<module>` checks 3 -> 4 (`_HELD_RESERVATION_ROWS`) and new `_verify_held_reservations` (checks 1).
