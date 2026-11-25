# S12 assertion changes

## Fix round (`040-s12-r1`)

No existing test assertion was changed or removed; there is no old -> new assertion
replacement in this round. Six tests were added to the existing scenario module
`tests.test_s12_account_lanes` and linked to existing requirement IDs:

- M10 / H1c-1: `S12RestartStatusTests.test_probe_kill_hint_requires_not_reaped_root`
  requires no `kill` or `you can stop` wording for `tree_not_confirmed`, retains
  the group check, and requires `kill 4242` for `not_reaped`.
  `test_probe_kill_hint_skips_reaped_root_before_owned_unreaped_root` requires
  selection of the unreaped root even when the reaped root comes first.
- S12-N3: `S12SettlementLimitTests.test_raw_sqlite_busy_and_locked_remain_retryable_past_record_failure_limit`
  checks code-less `OperationalError('database is locked')`, code-less table locks,
  BUSY, LOCKED and extended LOCKED codes: five failures followed by success,
  zero record-budget consumption and no denial after ten passes.
  `test_other_sqlite_operational_errors_still_exhaust_record_budget` retains
  the three-attempt denial bound for a non-transient operational error.
- H1c-3 / H1c-1: `S12SignatureBirthTests.test_missing_signature_process_record_waits_boundedly_and_names_missing_evidence`
  checks ten inconclusive `wait(timeout=0)` calls, retained custody, named missing
  birth/parent process-record evidence, no signal or kill instruction, then one
  release and pipe closure after a successful wait with the existing guarded proof.
  `test_missing_signature_record_and_reaped_root_still_need_descendant_proof`
  requires a nonempty group to retain custody even after that successful root wait.

The existing `unchecked` display reason is reused, with more useful signature
wording; no new reason or record shape was added. The wait uses the existing
zero-timeout bound; no count-based limit was added.

## Original build

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
