# S14g assertion ledger

No existing assertion was changed or removed.

Fixture scaffolding in `test_b4t_takeover.RetentionA1Tests.project` previously provided only
`project_holds(kind,identity,state,evidence)`. It now also supplies `history_identity`, the hold's leading
`project_id` (defaulting to the single fixture project), and the composite primary key. The existing INSERT
explicitly names the same original four columns and supplies the same values. This preserves all original
retention/rollback assertions while representing the project identity used by the runtime's keyed lookup.
The new S14g plan tests independently open complete real product schemas; they do not use this simplified fixture.

## S14G-4 (S14i follow-up, 0.8.73 -> feat/s14i-0874)
Added: `LiveIndexTests` (3 tests): takeover finished-custody scan and `next_hold_due` reads carry `state<>'released'` and plan through `project_holds_kind_due` (the open-binding retry through `project_holds_due`); answers unchanged over held/releasing/released rows. Red first (3 failures), then green.
Changed: no existing test assertion. Pinned data `docs/requirements/s14g/plans.json` regenerated for 4 changed SQL texts (same 147 queries; the plans now name the live index: custody scan `SCAN` -> `SEARCH ... project_holds_kind_due (kind=? AND state=?)`; the three `next_hold_due` reads `SEARCH project_holds` -> `USING (COVERING) INDEX project_holds_kind_due` / `project_holds_due (state=?)`). `state='held'`/`'releasing'` already implies `<>'released'`, so the selected rows are the same.
