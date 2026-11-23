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
