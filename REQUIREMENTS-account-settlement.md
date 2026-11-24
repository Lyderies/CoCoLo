# B4s lost-run settlement through the register outbox

**RS-B-B4 — Lost-run settlement reaches the project store.**
A run whose service was lost after its claim was admitted is settled at the account (`account.settle_lost_run`, reply
shape unchanged) and its settlement then reaches the project store as a closure the project writer verifies with its own
result key: a NEW account operation, `account.lost_run_closure`, answers a settled (`lost`) or released
(`released_unacknowledged`) run with a closure bound to the project, run handle, attempt, lease digest, assignment digest,
bridge run id (the claim id), release kind, the account's release time and the outbox request's digest (RS-B-B4-1).
Before any account call the attempt's register row (its reservation hold, or its lease hold when the claim was never bound)
is `releasing` with the request id and request digest; after any crash the identical request is re-sent until the closure
is stored, and a closure naming another request digest is refused with nothing written; account calls stay one at a time
on the driver's helper thread (RS-B-B4-2). `record_account_settlement(lease_binding, closure)` verifies the closure and, in
one writer transaction, inserts the settlement row, expires a `claim_pending` lease and releases its hold `lost_unbound`,
moves a `reserved` reservation to `retiring` (a committed body is left to retention), releases the reservation hold with
the closure's kind and receipt, and releases unfinished custody `lost_with_reservation`; it writes nothing to the terminal
outbox, execution consumptions, cleanup receipts or admission records, and its two register writes are the ones D2/D4
planted (RS-B-B4-3). At every driver caller the order is: account reply, settlement committed, workspace removed, journal
`settled`; a stop between steps leaves the journal entry unsettled and the next sweep replays the identical request
(RS-B-B4-4). A reservation with a verified settlement row is finished for `_unfinished` (checked before the consumption
check) and released for the purge (`_attempt_released`); no reader counts it as a successful run (RS-B-B4-5). An
identical retry after settlement returns the stored receipt, also from the register tombstone after the purge; another
project, lease digest, claim id or attempt is refused with every table unchanged; a purged identity can never be taken
again (RS-B-B4-6). The register validator refuses a settlement row whose hold is not released with the same kind, and a
held reservation with a settlement row; `releasing` without one is legal (RS-B-B4-7). With every bound set to 2, three
runs lost after their claim are settled, their orchestrations close, their rows purge, and a fourth run dispatches,
records and closes (RS-B-B4-8). RC-3 runs un-skipped (RS-B-B4-9).
Prerequisite (§9): the account's digest key and the project writer's result key are the same file
(`<state root>/digest.key`) in every install path, and a run can only be claimed after the account verified the
project's sealed lease with its key, so every closure is verifiable at the project.
Source: ANALYSIS-0.4.0-rsb-b4-b7.md §0, §2, §9; DESIGN-0.4.0-RS-B.md draft 2 §4, draft 3 §6, §14; DESIGN-0.4.0-RS-BD-shared.md SH-3, SH-5, SH-7; docs/requirements/b4s.md.
