# B3 `account.withdraw_open` from the project: summary

Requirement RS-B-5 (DESIGN-0.4.0-RS-B.md draft 3 §4, §7; rows W1-W5, M7). Stage: integration (real sockets).

- The wire contract (`open_withdrawal.py`): closed payload (handle, attempt, run id, inputs digest, generation or none,
  fixed reason), reply check (a reply for another handle is rejected), fixed request id per attempt, custody naming the run.
- Account: `account.withdraw_open` is allowed from the project service and from the host that opened the run, each
  authenticated by custody that equals the payload. The ledger authorizes by the open's own sealed receipt (caller scope
  of the host class, run id, inputs digest), answers an exact replay only to the caller scope that made the first
  request (evidence kept for good in `binding-withdrawal-v2`), and takes a new transition only for a pending binding at
  its current generation. Refusals other than plain stale/unavailable/generation-required read `account_not_authorized`
  with no receipt.
- Project: an `open_binding` register row is taken before registration (so a refused registration keeps the duty),
  marked `releasing` with its complete request before the driver drops the run, retried with the stored request after a
  lost reply, released `withdrawn`/`account_final` by the answer or `claimed`/`fenced` by the claim's own end. The driver
  withdraws never-leased cancellations, failures before preparation, idle registered runs (one hour, decision 90.7) and
  (next service start) a stopped service's runs. One memory withdrawal is sent only when the writer cannot record the duty.
- Account claim closure: a claim refused because its open was withdrawn can now be closed (settlement record) so the lease
  releases `fenced`; before, `claim_denial_unavailable` left it armed until expiry.
- Raw test logs are kept outside the tree.

## Fixes after the two security reviews (branch fix/b3-withdraw-2)

- Outbox fairness (MED-1): each `releasing` row has its own persisted schedule (`next_check_ms`, `failures` in its
  evidence, backoff base `WITHDRAW_RETRY_SECONDS` doubling to 300 s). `open_withdrawals` lists only due rows, earliest
  due first; the driver tries each listed row in turn and defers a failing one (`defer_open_withdrawal`) instead of
  stopping on it. Only `withdraw_stale` is final (`account_final`); authorization, unavailable, transport and
  decode/envelope failures are retried, never finalized.
- Bounded work (MED-2, decision 108 A): project-history schema 19 adds three indexes (the run id and the instance of
  an `open_binding` row's evidence, over only the live or held rows, and the run id of `project_registered_runs`).
  Registration checks both run-id owners with one index search each; the successor pass reads one window of at most
  `limit` held rows in index order (split around the live service's own, so those are never read), moves the window
  on after every call over unmarkable rows too, and starts again at the end. No scan, no sort; the complete ownership
  check and the lease check (`mark_open_withdrawing`) are unchanged. Tests measure SQLite steps and query plans.
- Deferral failure (LOW): a deferral that cannot be submitted keeps the rest of the batch; the failed row stays
  `releasing` with its old schedule.
- Evidence (L1): the owner-path withdrawal evidence is `{"d":"w3","q","g","c","r","i","a","w"}` (at most about 400
  bytes); `withdraw_open` reads v1, v2 and v3 records through `_stored_withdrawal`. The receipt is still the digest of
  the stored evidence.
- Conditional concern (a run retained when marking conflicts, `_retire_unclaimed`): not changed. The path is entered
  only for a failed run with no persistence, receipt, binding, claim sent or live future, so this service holds no
  lease for it; a mark can then conflict only if its row is already released or its lease belongs to another service.
  I found no real-driver sequence that gets there (the row is taken by this run, released only by this run's own
  withdrawal, a bind or a fence, none of which has happened), so I could not write a failing test. The unconditional
  `_release_failed` the worker proposed would hide a conflict whose cause is unknown, so the run stays visible.
