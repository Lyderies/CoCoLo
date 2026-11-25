# Assertion changes

No existing test assertions removed or changed. RECOV adds regression assertions; E2c-9 remains unchanged.

## Review correction round on 18929e7e

No assertions present at `18929e7e` were edited or removed. Three new regression tests were added in
`tests.test_recovery_line`; all seven original tests and the existing E2c-9 assertions remain unchanged.

The new tests assert:

- RECOV-2 / E2a-7: after a real execution-hold release commits and settlement publication is lost, the real ledger
  holds `pending`, with a null or no-later-than-effective-due deadline, rather than the acknowledged later B6 retry.
- RECOV-1/3: for each of the seven suppressed maintenance sub-steps, three persistently failing passes still
  complete their requests but retain the last successful check; the warning returns exactly at the threshold.
- RECOV-1/3: a real `reserve_result` transaction (calling real `take_hold`) between passes retains the old floor,
  does not renew it, and permits renewal by the next successful pass.

Two draft-test assertion replacements occurred before runtime implementation, correcting the fixture/protocol
expectations rather than adapting an existing assertion to the fix:

| Draft assertion | Replacement | Reason |
| --- | --- | --- |
| Released/crashed ledger tuple must equal `('pending', None)` | State must equal `pending`; due must be null or at most the effective orchestration due | The requested `wake_before(due_at_ms)` option includes that earlier deadline in `pending`; the ledger preserves the supplied payload. Both requested pending forms are conservative. The corrected test was red before implementation. |
| A second store connection is outside a transaction and sees the new reservation hold as `held` | The existing writer returns `('reserved', target)` from real `reserve_result`, followed by the same floor/threshold/next-pass assertions | A second writer is correctly refused with `history_writer_busy`. Dispatch through the existing writer executes the real store transaction and real hold hooks; no persistence or publication behavior is mocked. |
