# B5a: crashed settlement in process mode

Source: `~/Code/temp/orchestrator-child-liveness/ANALYSIS-0.4.0-rsb-b4-b7.md`
§5 and slice table rows 34–37; D-4 / owner decision 129.4. Base supplied:
1c293c44 (0.8.58), taken from the supplied task without git verification.
The temporary-fixture git deviation is recorded in validation. Container proof
and `stop_failed` remain B5b.

**RS-B-B5a — Crashed settlement in process mode.** Acceptance items RS-B-B5a-1 to RS-B-B5a-6 follow; the tests cite the group id.

- **RS-B-B5a-1 — Positive, recorded process evidence.** A process journal entry
  may advance from `watched` to durable `gone` only after `tree_state` returns
  `gone`: child and watchdog identities (PID plus kernel birth) have exited,
  the watchdog recorded its whole-tree cleanup confirmation, and two kernel
  checks find no group/session members or processes carrying the run marker.
  A live child/descendant, missing confirmation, unreadable/ambiguous evidence,
  `spawning`, `launched`, or a container entry proves nothing. Age never proves
  a project-side crash. `gone` retains the identities and cannot launch again.
- **RS-B-B5a-2 — Account closure.** Reuse `account.lost_run_closure`, with exactly
  its existing reply fields and request. A slot whose recorded account release
  is `crashed` receives a closure of kind `crashed` only with this run's durable
  process `gone` (or already `settled`) journal entry. Match the complete lease,
  run, attempt, claim request and bridge ID. The admitted-unbound exception is
  a durable never-spawned admission (`claimed`, or its `settled` tombstone,
  with no child, watchdog or run marker). Until then the existing
  `claim_cleanup_unavailable` refusal remains, including B4s's live-child test.
  RS-A's existing timed account-slot rule is unchanged; it cannot authorize
  release of a project reservation. No new account operation or op-set pin.
- **RS-B-B5a-3 — Writer evidence gate, V3/V7.** A signed `crashed` closure alone
  cannot release a reservation. Before the first settlement the writer reads
  the matching protected process journal entry and requires recorded `gone` or
  `settled` evidence with child/watchdog identities. Missing, mismatched,
  unreadable, container or pruned-first evidence refuses with every table
  unchanged. Its proof is saved in the released hold's `evidence` in the same
  transaction as the B4s closure and register release. A claim_pending lease
  with the matching never-spawned admission is marked `admitted_unbound` by the
  outbox writer; only that lease can use the exception, never a bound reservation.
  A fence's authenticated `claimed` answer is journaled before arbitration and
  keeps the stopped intent's instance so a failed settlement retries next sweep.
- **RS-B-B5a-4 — Exactly once, shared outbox.** Preserve B4s arbitration and
  request digest fencing; replay the identical request after a stop. Record
  `crashed` once through `record_account_settlement`, then remove the workspace,
  then mark the journal `settled`. An identical retry, including after journal
  pruning or attempt purge, returns the stored receipt with `known: true`.
  Another key/project/run/attempt/lease/claim/request is refused without writes.
- **RS-B-B5a-5 — Custody and readers, X4.** An uncommitted crashed reservation
  retires and releases unfinished custody `lost_with_reservation`; a committed
  result and its custody remain for retention. No terminal, consumption,
  cleanup receipt or outcome is invented. The existing verified-settlement
  readers allow close/purge and never count the run as successful. Existing
  never-spawned admitted claims release their lease through B4s `lost_unbound`,
  including an account slot already marked `crashed`; they are not described
  as an exited child (A5/L6b).
- **RS-B-B5a-6 — Plain status and compatibility.** A lost run with capacity
  still held explains that process-exit confirmation/settlement is pending;
  a released one says its capacity was released, with unknown outcome. Preserve
  status fields and all existing account/history reply field sets. Keep every
  existing assertion unchanged; if any needs changing, stop and record exact
  old → new in `b5a/assertions.md`.

Implementation and test map, red evidence and commands are in
[b5a/validation.md](b5a/validation.md).
