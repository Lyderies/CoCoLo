# S12 assertion changes

Port of off-branch `2ef61423`:

- The M10 never-clearing-tree test now also patches `ProcessTree.ended_after_reap` to false.
  Its existing assertions (one admitted row, held probe worker, restart waits) are retained.
  Replacement premise: both initial cleanup and subsequent proof remain inconclusive;
  the new N2 test separately requires eventual clearing and settlement.
- The reaped signature test retains the original non-null returncode and unguarded assertions,
  saves the process reference before proof, and adds closed-pipe assertions.
- N1's direct `_end_children` call supplies the base's required `right_after=False` argument.
  The off-branch safety assertions are unchanged.

- The new M8 collector fixture handles the immediate byte reply on claim replay before
  decoding it, and reopens with its original fake client capabilities. The unchanged
  requirement remains one claimed capacity row after two identical requests.
- Fresh-session test doubles use the real helper's boolean answer (`False` / `True`)
  instead of empty/nonempty lists; the wait/release assertions are unchanged.

- Exact retained-root assertion mapping:
  `assertIsNotNone(slot.retained.process.returncode)` ->
  `assertIsNotNone(process.returncode)`, where `process = slot.retained.process` is
  captured before proof clears the context. The predicate is identical; the
  retained reference also permits the added closed-stream assertion.
- The fresh-proof fixture now seeds the root cleanup identity exactly as
  `ProcessTree.identify` does. No assertion was removed; a new negative control
  requires known live descendants and irrecoverable tracking loss to stay held
  even when the marker, group and session appear empty.
- The ported N1/N4 process waits and cleanup waits now have five-second bounds.
  Their assertions remain unchanged.

- No assertion changed for the M9/M10 collector scenarios. `publication.assert_not_called()` stayed as written
  (Q3(b): a lost probe's records commit without the caller's publication callback; `account_billing_execution.complete_ended`
  documents "not the caller's publication callback"). It was red because the product asked that callback anyway: `complete_ended`
  -> `_completion` / records loop -> `account_billing_admission._execution` -> `_original` -> `_authorize(reservation.authorize)`,
  and the refusing callback turned the records into a denial. Fixed in the product (`lost=` flag, context still checked, callback not asked).

No other existing assertion is removed or weakened.
