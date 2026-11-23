# D4 "Publication material and replay": requirement -> test -> code site

Base ef08e42c (D3 head, on D2 on B3; all unmerged). Code: PH = project_history.py, PRC = project_result_contexts.py,
RP = result_persistence.py, PD = project_driver.py, all in packages/codex-agents/native-package/.codex/orchestration/codex_agents/.
Tests: packages/codex-agents/tests/test_publication_replay.py (22, store level) and test_publication_replay_driver.py (7, through the
real driver and writer lane), requirement RS-D-D4 (REQUIREMENTS-publication-replay.md). Sources: reviews/040-rs-bf-a1/requirements.numbered.txt:393-396
(slice D4) and :507, DESIGN-0.4.0-RS-D.md draft 3 s1-s3, s6, draft 4 s2-s4 and rows :674-683, :943-955, DESIGN-0.4.0-RS-BD-shared.md S-2/S-3,
ANALYSIS-0.4.0-rs-bf-audit.md (D4 rows; the verifier's list expands "R2+" to this catalogue plus the production retry).
Owner decisions: 90.1 A (CoCoLo stopping just before a result is saved: the run is marked lost, never half-saved), 90.2 A (measure, no promised number).

Already there before D4 (checked, kept): the production retry (PD `_retry_publication` 835-860 calls `RP.retry`, backs off 1 s doubling to 60 s,
ends at the run's hard deadline: pinned by test_publication_retry, unchanged); `_write_result_file` reuses a verified final file; the
prepare transaction writes `prepared_digest` and `retained_until_ms` once (D2); no pre-commit deadline retires a held obligation (D2, SH-4);
the `project_result_publications` table (step 18, no schema step here). Retirement (D3) already removes files; D4 adds the material row to it.

| Row | Requirement | Test | Code site (before -> now) |
|---|---|---|---|
| P1 | a failure at create, write, fsync, link, partial unlink or directory fsync with the service live is retried from the kept packet; one terminal, outbox and usage record; digest and retention unchanged; then released normally | KeptPacketRetryTests.test_P1_*; driver LiveRetryLifecycleTests (L1, L2) | PH `commit_result` (~2240-2320): the prepare transaction now also stores the material (`_store_material` 2318); the live path keeps the packet (RP `pending.selected` until commit) and PD retries it (exists) |
| P2' | stop before the directory fsync: (a) final link survived and verifies -> recovered and committed; (b) only a partial -> lost by settlement, files and material cleaned | StopBeforeAndAfterTheHandoffTests.test_P2_*, test_P2prime_a_*, test_P2prime_b_* | PH `_verified_publication` 2354 (verification is the test, not the fsync), `arbitrate_publication` 2484 (`unrecoverable` -> obligation `releasing`), `_retire_result` 2622 deletes the material with `retiring` |
| P3' | stop after the handoff, before the commit: the successor's start-up, expiry and lost sweeps each run first, in turn: committed once, never lost | test_P3_*, test_P3prime_* (three orders) ; driver StoppedInstanceOrderTests | PH `replay_publications` 2434, `arbitrate_publication`; `expire_results` unchanged (selector keeps a held reserved row) |
| R1 | envelope and final file, reservation `retiring`: cleanup only | ReplayEligibilityTests.test_R1_* | PH `_retire_result` deletes the material in the `retiring` transaction (SH-7: a material exists only for `reserved`); `_replay_locked` refuses a non-`reserved` row |
| R2 | reservation released lost / crashed / completed: cleanup only, no terminal | test_R2_* (three) | PH `_replay_locked` 2406 (obligation must be `held`); D2's selector retires |
| R3' | past `expires_at_ms` with the obligation held: replayed, nothing deleted | test_R3prime_* | PH `_replay_locked` (no deadline condition) |
| R4' | live publication racing a settlement, both commit orders; the real driver step order and the reverse, stopped earlier instance: the body is committed before any `settle_lost_run` | test_R4prime_a_live_publication_racing_*; driver test_R4prime_* (two orders) and test_the_exiting_service_* | PH `_require_publishable` 2323 (`releasing`/`released` or a settlement row refuses), `_commit_publication_locked` 2331 (compare-and-set in the transaction); PD `_arbitrate_publication` 1876 called first in `_settle_entry` (used by the lost sweep `_settle_lost`, the own-ended settle and container recovery); RP `arbitrate_publication` 1066 |
| R5 | context not `bound`: replay refused, cleanup only | test_R5_* | PH `_replay_locked` (PRC `stored_state` 234); `PRC.commit` already refuses a non-`bound` context inside the commit transaction |
| R6 | altered binding, assignment digest, terminal record, body, usage record (present against absent, and altered): refused `history_conflict`, kept charged | test_R6_* (four tests, eight alterations) | PH `_verified_publication` 2354: digest, `canonical_json` round trip, binding, assignment digest, record run id and result digest, usage record id/run/phase/provider/time, body keyed digest |
| X1 | arbitration on a verifiable `reserved` publication: recovered; settlement then leaves the body; custody `committed`; recordable | ArbitrationTests.test_X1_* | PH `arbitrate_publication`; the settlement write itself is B4's (`record_account_settlement`): the test plants the two writes as D2's X9 does |
| X2 | no material or a body that does not verify: obligation `releasing` before the account call; a later live or replayed commit refused | test_X2_* | PH `arbitrate_publication` (one transaction on the held row, request id stored) ; `_require_publishable` |
| X3 | a settlement row or `releasing` row already present: `settling`, nothing committed, cleanup follows | test_X3_* | PH `arbitrate_publication`; replay refuses a settlement row too (`_require_publishable`) |
| L1 | live: failure after the directory fsync (commit transaction fails): retry reuses the final file; lifecycle completes | driver test_L1_* | PH `commit_result` (unchanged branch), `_write_result_file` verified-final reuse (exists) |
| L2 | live: failure after the terminal commit, before delivery install: retry takes the committed branch; lifecycle completes | driver test_L2_* | PH `commit_result` committed branch (exists); RP `_publish` (exists) |
| L3 | replay or arbitration commits a claim with a live job: `adopt_delivery` installs it once; unequal delivery refused | driver test_L3_* (live run; unequal-delivery unit) | RP `_install_delivery` 1097, `_adopt_committed` 1110, `expire` request (replays, ~1200) and `arbitrate` request 1211; PH `committed_delivery` 2473 |
| RS-D-12 (narrowed) | a result is lost only when the service stops before a verifiable final body exists; a failure without a stop is retried; a verifiable body is committed before any settlement of its attempt | P1, P2', P3', R4' | as above; no numeric bound (90.2 A) |
| SH-7 | legal recovery states pass both validators | `consistent()` at the end of the store tests (material exists only for `reserved`, hashing to `prepared_digest`; a `releasing` obligation is legal) | project_holds_schema.py unchanged |

## Needs B4 (not invented here)
- The release of the `releasing` obligation to `released` `lost`/`crashed`/`released_unacknowledged` with its settlement row (`record_account_settlement`), and
  `arbitrate_publication`'s use in B4's own settlement transaction (the SH-7 joint release check). D4 stops at `releasing`; a held-then-`releasing` row stays
  until B4. The tests plant the settlement the way D2's X9 does.
- RS-C `run_slot_state` crashed-release arbitration (S-3's second caller) and X4 (a crashed release with committed or uncommitted body: custody kept / `lost_with_reservation`).
- Takeover of a stopped service's live jobs by a successor lane (B4): D4 gives the successor the store replay and the arbitration only.

## Choices to flag
- Settlement at the service's own exit: the writer has stopped, so `RP.arbitrate_after_stop` opens a store for the call (the exiting thread) and arbitrates before
  `settle_lost_run`; if it cannot (writer fault, store unreadable) the settlement is skipped and asked again by the next host (the faulted path already settles nothing).
- The driver's live retry still ends at the run's hard deadline (pinned by test_publication_retry `test_D4_the_runs_deadline_ends_the_retries...`); after that the
  stranded body is recovered by replay or arbitration, not by the live lane.
- `replay_publications` completes at most `REPLAY_PASS_LIMIT` = 4 stored publications per pass (each one body of at most 1 MiB): registered in lifetime_caps.json.
