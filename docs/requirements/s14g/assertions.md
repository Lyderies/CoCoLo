# S14g assertion ledger

No existing assertion was changed or removed.

Fixture scaffolding in `test_b4t_takeover.RetentionA1Tests.project` previously provided only
`project_holds(kind,identity,state,evidence)`. It now also supplies `history_identity`, the hold's leading
`project_id` (defaulting to the single fixture project), and the composite primary key. The existing INSERT
explicitly names the same original four columns and supplies the same values. This preserves all original
retention/rollback assertions while representing the project identity used by the runtime's keyed lookup.
The new S14g plan tests independently open complete real product schemas; they do not use this simplified fixture.
