# Unit 3.8: account ledger evidence

Base: `230c2fc0` (0.8.62 candidate). Scope is the account ledger; no
project-history, hold-schema, VERSION or runtime digest changes are intended.
Authority: Phase 3.8 inventory slices 1–3 and PLAN-0.4.0 E1a-T2/T3/T4.

## E1a-T2

After earlier phases succeed, a different thread must actually hold the ledger
lock during claim/open completion, stale-input recapture, initial open
authorization and both provenance adapters. The decoded reply must give exactly
`account_busy` (or `open_account_busy` for open), with the exact plain next step
“The account's records were busy: try again in a moment.” Contention must leave
the lane unfaulted with no failure note. Releasing the holder must allow a
successful retry. Enrollment can be synthetic, but the initial-open authorizer
and the contended ledger operations must be real.

## E1a-T3

Real `AccountService.handle` and `begin` entries give successive waits one
request budget. A deferred open publication exhausts its own budget; the next
request and owner pump must observe a fresh budget. A collector frame received
slowly starts its ledger budget only after receipt, and the next frame gets a
fresh budget. A frame whose dispatch raises must also restore the request
deadline on the same thread, allowing the next frame a fresh budget. A controlled
clock may advance time without sleeping; actual ledger operations remain wired in.

## E1a-T4

Use events/barriers, rather than a timed load loop, to coordinate simultaneous
refusals through the held, write-unit, account-transaction, read-call, reader
entry, slot-health, sweep and project-health acquisition paths. Assert the exact
whole per-label count dictionary while ledger-free health reads run concurrently.
All workers must acknowledge completion, and release must permit ordinary work.

Each new test is mapped by its requirement decorator in
`tests.test_e1a_evidence` and the requirement registry. For already-green
behavior, record an in-memory broken-wiring red control and its matching green
run in [STATUS](e1a-evidence/STATUS.md). Do not relax existing assertions.
