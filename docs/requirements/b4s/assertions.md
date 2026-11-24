# B4s: existing assertions changed (old -> new, with reason)

Base 8d4a7ce3. No assertion of an existing test was weakened or removed.

## Exact op-set pins (primary decision 132: a decided new account operation)
`account.lost_run_closure` is RS-B-B4-1's new operation (finding 4: `settle_lost_run`'s reply shape stays exact), so the
three exact `ACCOUNT_OPERATIONS` pins gain it. Nothing else in the sets changes.

| Test | Old | New |
|---|---|---|
| tests/test_admission_inputs.py:139 (test in AdmissionInputs, `assertEqual({...}, ACCOUNT_OPERATIONS)`) | 21 ops, `... "account.settle_lost_run", "account.run_provenance", ...` | 22 ops, `... "account.settle_lost_run", "account.lost_run_closure", "account.run_provenance", ...` |
| tests/test_signature_inputs.py:146 | same 21-op set | same set + `account.lost_run_closure` |
| tests/test_signature_job_intake.py:96 | same 21-op set | same set + `account.lost_run_closure` |

## Setup change (no assertion change): test_A2 (tests/test_account_settlement_driver.py, B4s's own red test)
- Old setup: `self.lose('binding')`. At driver state `binding` the store's bind has already committed, so the lease is
  bound and the run settles as a reservation (kind lost, claim id set); the test's premise (a claim admitted, lease never
  bound) did not hold.
- New setup: the same `self.lose('binding')` under a patch of `ProjectHistory.bind_result_lease` that fails once
  (`history_service_unavailable`), so the account holds the claim while the lease stays `claim_pending`.
- Assertions unchanged: settlement row `(run_handle, 0, 'lost')`; lease `expired`, lease hold `released lost_unbound`.

## Setup change (no assertion change): tests/test_lost_run_settlement.py test_a_lost_reply_and_repeated_restarts_settle_the_run_exactly_once
- Old setup: restart the host twice right after `len(replies) >= 2`.
- New setup: also wait until the journal entry is `settled` (or pruned) before those restarts.
- Reason: B4s adds steps after the account's reply (closure fetched, recorded, workspace removed, then journal
  `settled`); a restart inside that window correctly replays the identical request (RS-B-B4-4), which produced a
  third, `known` reply (failed 2 of 4 runs). "Settled runs are never sent again" is unchanged, and is now measured
  from the point where the run is settled. Assertions unchanged: `[False, True]` known, one receipt digest,
  `2 == len(replies)`, 0 counted, binding `abandoned service_lost`. Passed 5 of 5 after the change.

## Skip reason updated (still skipped): RC-3 (tests/test_result_lifetime.py:105)
- Old reason: "the account operation that calls withdraw_open and the driver calling it are RS-B (not built)".
- New reason (measured in B4s with the skip removed): a run refused at its lease reservation has a writer job but no
  lease, so `withdraw_unsent` refuses (`history_result_not_ready`), `run.persistence` is never cleared and
  `_retire_unclaimed` never marks its open binding withdrawing; the `open_binding` hold stays `held` (24 h).
  That is B3's open-withdrawal wiring for a lease-refused run, not lost-run settlement; left for the primary.
- Test body and assertions unchanged.

## Stays skipped by spec
- B0-2 (tests/test_hold_lifecycle.py:131) expects a lost terminal; B4s writes none by design (oracle S3).

## Review r1 fix round (Codex security review NOT-READY on e57f84a5)
No existing assertion was weakened or removed. Changes, old -> new:

| Test | Old | New | Reason |
|---|---|---|---|
| tests/test_account_settlement_driver.py test_A2 (B4s's own) | lease `expired`, lease hold `released lost_unbound` | the same, plus the run's `open_binding` hold `released claimed` | added assertion: review HIGH 2 (A2 missed the open-binding leak) |
| tests/test_account_settlement_driver.py test_B4_4_the_container_recovery_writes_its_journal_only_after_the_settlement_returned | setup: `remove_workspace` mock returns None | the mock returns True (the workspace is gone) | setup only: MED 2 makes `remove_workspace`'s answer gate the journal; assertions unchanged (`'settled'`, order settled -> workspace -> journal) |
| tests/test_account_settlement_driver.py `call_as` | a helper of ClosureAuthorityTests | moved unchanged to SettlementHarness | helper move, so the r1 MED 1 test can use it; no test body changed |
| tests/test_result_lifetime.py RC-3 (RS-B-B4-9) | `@unittest.skip(...)` | skip removed, test body unchanged, passes | B4-9 requires it (the primary's earlier split reversed): the lease-refused job is retired so the open is withdrawn |
| tests/test_project_holds_schema.py test_SB_a_copy_of_an_18_store_carries_the_new_tables_and_a_restore_opens_validated | setup: settlement row `lost` for claim 2 while claim 2's reservation row stays `held` | setup also writes claim 2's reservation row `released lost` (replaces the held row; the `1 + 5` row count is unchanged) | already failing at e57f84a5 (history_incompatible at open): RS-B-B4-7 refuses a held reservation with a settlement row; assertions unchanged |
| tests/test_project_holds_schema.py test_X5_M3_a_settlement_row_is_written_and_read_back | setup: settlement row for claim 2, its reservation row `held` | the same transaction releases claim 2's reservation row `lost` | as above (already failing at e57f84a5); assertion (row read back after a validated reopen) unchanged |

New tests (red first, docs/requirements/b4s/red.md): tests/test_account_settlement.py ReviewRoundOneStoreTests (5),
tests/test_account_settlement_driver.py ReviewRoundOneDriverTests (9; the process-mode HIGH 3 test stays skipped with
its cause). test_r1_H1_a_recorded_settlement... was edited before it went green (its own, new in this round): the
account's tombstone evidence is cleared after the purge (a run settled before the tombstone kept it), and its
"no account call after the purge" check became "the stopping service settled it at the account first": RS-B-B4-4's
replay (StepBoundaryTests: every later sweep re-sends the identical request, answer `known`) is kept, and the local
receipt finishes the journal when the account can no longer answer.

## Pending primary (HIGH 3 process mode, decision 146): NOT changed
tests/test_container_launch_journal.py IntentBeforeClaimTests.test_without_the_switch_no_intent_is_written_and_the_claim_goes_as_today
- old: `self.assertEqual([], value._journal.entries())` after the claim is sent with containers off
- proposed new: one entry, the process intent (`version 3`, state `intent`, the run's claim_request_id), and still
  `run.launch_token is None` (no container intent). Reason: decision 146 requires an intent written before the
  claim in process mode, which this assertion forbids. Left failing; the primary decides.

## Setup change (HIGH 3 process mode round), no assertion value changed
tests/test_account_settlement_driver.py test_r1_H3_a_claim_admitted_before_its_first_journal_write_is_settled_from_the_store:
its wait read `self.ledger._connection` (the collector thread's connection) from the test thread, which raised
sql_read_wrong_owner when the collector was mid-read (seen in a combined run, then cascading into 17 other
failures). Now `account_claims()` reads the same COUNT(*) FROM launch_claims on a read-only connection of its own.
APPROVED by the primary (decision 153): applied as one version-3 `intent` entry (state, version) + launch_token None;
test renamed to test_without_the_switch_only_the_process_intent_is_written_and_the_claim_goes_as_today (the old name
stated the forbidden behaviour). The claim-once and claim_sent assertions are unchanged.
