# 0.8.70: a hold the update left unverified (decision 366)

Authority: OWNER-DECISIONS-0.4.0.md decision 366; DESIGN-0.4.0-B4-takeover.md Amendment A1 (an unverifiable
predecessor record stays unchanged; never force-released).

Live finding (0.8.68): the run the 0.8.67 -> 0.8.68 update ended had records that could not be re-verified, so
adoption skipped it (`refused_unverifiable`) and wrote no `takeover_obligations` row. Its project hold stays
`releasing`, the driver retried its settlement for ever (`history_conflict`), and the holds check reported
`hold_overdue` every pass with advice that led nowhere.

Marker (decision 367, replacing 366's inferred marker after review 040-uh-r1 NOT-READY): a durable fact written at
adoption. When `project_takeover.adopt_project` refuses a `releasing` lease/reservation hold as `refused_unverifiable`
and it has no obligation row yet, the same adoption transaction inserts a `takeover_obligations` row for it whose
`original_request_digest` is `unverified` (`project_takeover.UNVERIFIED`; never a digest, so it authorizes nothing:
`successor_settlement` refuses it). No schema change and no new table: an existing B4t table at schema step 23. The row
is written once by the first takeover that refuses the hold and is never changed (a later takeover skips it). It
survives journal completion, maintenance, restarts and copies of the store, and is retained and pruned with the other
obligation rows under A1.10 (kept while its hold is not released; its `takeover_record` is kept by k3; it is excluded
from the k4 live-obligation chain walk). Nothing is inferred from a missing obligation row any more.

- **UH-1:** a hold the takeover recorded as unverified is not reported as `hold_overdue`.
- **UH-2:** it is reported as its own plain finding, `hold_unverified_takeover` ("a run ended by the last update is still
  counted because its records could not be checked; harmless, takes no run slot, will not clear by itself"), with
  what to do (nothing is needed). One finding per project, never one per hold.
- **UH-3:** the driver stops retrying that run's settlement, on both paths (the lost-run sweep and container
  recovery, `RunDriver._settle_recovered`): after a `history_conflict` it asks the writer (`takeover_unverified`, which
  reads the durable record); when it is unverified the run is remembered in a memory capped at
  `MAX_UNVERIFIED_KNOWN` (64, oldest out) and not attempted again; after a restart or an eviction one more refused,
  local attempt (no account request, no change) learns it again from the record. No release and no account change is
  made for it. A conflict for any run not recorded unverified is still retried.
  Correction (decision 370): with more than 64 recurring unverified run ids, the oldest are forgotten from
  that memory, so each forgotten run is retried locally on every sweep (one refused local attempt, no account
  calls, no change). That is accepted; the memory is a cap on work, not on correctness.
- **UH-4:** `docs/troubleshooting.md` "Update hand-over" explains this case in plain words.
- **UH-5:** verified takeover rows, a run still `held` at adoption that later goes `releasing` (through the
  successor's own arbitration and outbox), and genuinely overdue holds of any other kind, behave as before.
- **UH-6:** the unverified fact is written in the adoption transaction and survives journal completion, maintenance,
  restart and a copied store (review r1 HIGH 1).
- **UH-7:** the health finding's query reads live holds only (`INDEXED BY project_holds_kind_due`, live predicate):
  work at 100k released rows stays within 2x of 1k and within the health deadline (review r1 MED).

## Assertions
See `docs/requirements/unverified-hold/assertions.md` (one changed assertion, decision 367).

## STATUS
0.8.70 r2 (decision 367): durable marker in `project_takeover.adopt_project` (`UNVERIFIED`), `unverified_hold_condition`
reads it, `ProjectHistory.takeover_unverified` (a settlement step), `RunDriver._takeover_unverified` /
`_settle_recovered` / `MAX_UNVERIFIED_KNOWN`, `health_holds` (`hold_unverified_takeover`, index-bound; overdue
excludes it). No schema change. Limit: an obligation row written by an earlier takeover that a later takeover refuses
(chain) is never rewritten (H3b), so it is not marked unverified and still reads as `hold_overdue` when late.
