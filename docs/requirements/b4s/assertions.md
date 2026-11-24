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
