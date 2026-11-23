# D2 "Deadlines and retention": requirement -> test -> code site

Base f52cb746 (B3 head). Code sites are in packages/codex-agents/native-package/.codex/orchestration/codex_agents/
(PH = project_history.py, PRC = project_result_contexts.py). Tests: packages/codex-agents/tests/test_deadlines_retention.py.
Sources: reviews/040-rs-bf-a1/requirements.numbered.txt:384-388 (slice D2), DESIGN-0.4.0-RS-D.md (draft 2 s1, s8, s9; draft 3 s8,
rows ~:660-700; draft 4 s2, a-HIGH 2 row :821), DESIGN-0.4.0-RS-B.md X6-X9, DESIGN-0.4.0-RS-BD-shared.md SH-3/SH-4.
Owner numbers (decision 90): 90.3 A 256 MiB contexts, 90.4 A 1 h verifier margin, 90.5 A 7-day retention, 90.8 A readable until expiry.

| Row | Requirement | Test (class.method) | Code site today -> change |
|---|---|---|---|
| D1 | RS-D-3: `retained_until_ms` set once at first preparation, unchanged by retry / changed policy; context row gets it at commit | PreCommitDeadlineTests.test_D1_retained_until_is_set_once_and_the_context_takes_it_at_commit | PH `publish_result` prepare UPDATE (~2233-2240), PRC.commit (233-247). Already true; regression |
| D2' | SH-4: reserved row past its lease deadline, obligation held: not retired, still charged, still publishable, expire pass leaves it | PreCommitDeadlineTests.test_D2prime_* (4 tests: expire pass, result_reservation/_live_result, publication, charges) | PH `_live_result` (949-953), `result_reservation` (2325-2334), `expire_results` selector + loop (2386-2425), prepare UPDATE `expires_at_ms=MIN(...)` (2233-2240). Change: new `_pre_commit_kept` rule, selector gains the reserved clause |
| X8 (RS-B) | = D2' (RS-B draft 4 changes X8: past deadline, obligation held, publication commits, read refused (not committed), files and charge kept) | PreCommitDeadlineTests.test_X8_* | same |
| X9 (RS-B) | obligation released `lost` -> retired at next pass, charge released, custody `lost_with_reservation` with the cause | PreCommitDeadlineTests.test_X9_* | PH `expire_results` selector, `_retire_result` (2336-2360) kind, PRC.begin_retirement evidence |
| X6 (RS-B) | a bound attempt (reservation live, committed, recorded): the context is not swept; context, body, charge kept | ContextSweepTests.test_X6_* | PH `expire_results`; there is no context-only sweep in D1 (PRC.reconcile at open only) so the selector must not select a bound or committed context by itself. Regression pin |
| X7 (RS-B) | lease released withdrawn_unsent with no reservation: context retired, `ended` | ContextSweepTests.test_X7_* | PH `release_earlier_unarmed_leases` (~1960-2000), PRC.release_unfinished/finish (282-312). Already true; regression |
| D3 | RS-D-3/4: after commit `max(wall, floor)` below / at / above `retained_until_ms`; no verifier ever: released `retention` exactly at equality | CommittedRetentionTests.test_D3_* | PH `_live_result`, `expire_results`, `_retire_result` kind. Already true at equality (`now >= expires_at_ms`); regression |
| D4 | RS-D-7 / draft 2 s8: floor persisted past a deadline, wall moved back: the next expire pass still retires; nothing revives, work not blocked | ClockFloorTests.test_D4_* | PH `_result_now` (855-875) raises `history_result_unavailable` when wall < floor; `expire_results` (2376) calls it. Change: `_expiry_now` = max(wall, floor) used by `expire_results` and `retire_result` |
| C1 | RS-D-4: primary closes (closed_for) -> committed custody released `orchestration_closed` at the next pass | OrchestrationCloseTests.test_C1_* | PH `expire_results` loop (2418-2424), `_retire_result` (2340-2343). Already true; regression |
| C2 | RS-D-4 s8, decision 90.8: `interrupted_unattended` close does not end custody: kept to its deadline, read and record work, verifier open refused | OrchestrationCloseTests.test_C2_* | PH `read_result` (2314-2316) refuses any closed orchestration; `expire_results` early branch; `_retire_result` kind. Change: `_unattended_close` (orchestration register row released `interrupted_unattended`, project_holds_schema.py:32) narrows all three. No orchestration register row (pre-18, and every row until B6 writes them) keeps today's behaviour |
| C3 | RS-D-4: close or expiry racing a context read and an admission: after `retiring` body and context reads both refused | OrchestrationCloseTests.test_C3_* | PRC `_readable` (392-401) `_GONE`; PH `read_result`. Already true; regression |
| V1 | RS-D-5 / draft 2 s9: verifier open with the worker's deadline below / equal / above (lease deadline + 1 h margin): refused `history_worker_result_expiring`, accepted, accepted | VerifierMarginTests.test_V1_* | none today (grep: no `history_worker_result_expiring`, no QA_MARGIN_MS). Add `QA_MARGIN_MS = 3_600_000` in PRC and the check in PH `_reserve_result_lease_locked` (1129-1155) when the record context task names `verification_for_outcome` |
| V2 | RS-D-5: expiry during the verifier's preparation: refused `history_result_expired`, nothing launched | VerifierMarginTests.test_V2_* | PH `verification_worker_reference` -> project_worker_result.py `verification_worker_reference` -> `read_result`. Already true; regression |
| V3 | draft 2 s9: worker expires while the verifier runs: verifier custody live, orchestration active, verifier records; worker reference refused | VerifierMarginTests.test_V3_* | per-attempt custody independence; PH `_retire_result`. Already true; regression |
| V4 | RS-D-5/7: forward jump past both deadlines: both released; the verifier's record refused `history_result_expired` | VerifierMarginTests.test_V4_* | PH `admit_host_result` (~2707 `now >= reservation.expires_at_ms`), `expire_results`. Already true; regression |
| SH-3 release kinds | custody released `retention`, `orchestration_closed` or `lost_with_reservation`; never by `bound`, `interrupted_unattended`, or a pre-commit deadline | covered by D2', X9, C1, C2, D3 | PRC `_release_custody`, `begin_retirement`; project_holds_schema.py:36-37, `_CUSTODY_INCONSISTENT` joint check (:225-232): kept consistent (SH-7) |
| RS-4 (project store) | no deadline authorizes loss of a body whose obligation is held | D2', X8 | same as D2' |
| 90.3 | 256 MiB live-context budget | already `MAX_LIVE_CONTEXT_BYTES` (PRC:31); covered by D1's LeaseContextTests O5 | none |
| 90.5 | 7-day retention | service value project_service.py:85 (7 * 86_400_000); tests pass it explicitly | none |
| SH-7 | both validators stay consistent | the lease-backed X/D/V tests and the V3/V4/C3 tests end with `custody_consistent` and `register_consistent` (`consistent()`); C1, C2 and V2 do not | project_holds_schema.py:225-262 unchanged |

## Scope decision (reported)
SH-4's "no pre-commit deadline retires" is applied to reservations born from a lease (a `result_leases` row for the claim).
The product path is always lease-born: the driver calls `prepare_lease` (project_driver.py), never the non-lease
`ResultPersistence.prepare(binding, handle, hard_deadline_at_ms=...)`, whose branch (`reserve_result`, no lease, no context)
has no product caller. `NativeProductCallerTests` (test_deadlines_retention.py) pins that by AST over the native package: only
tests may call it. That branch keeps today's deadline rules because ~10 existing assertions in test_project_results.py pin
them (e.g. `test_expired_reservation_still_blocks_metadata_identity_reuse`: reserve, now=61 000, `expire_results() == 1`);
editing those needs owner approval. A lease-born reserved row never lacks its obligation hold (taken in the claim's own
transaction; `register_consistent` refuses a store without it). If one did, `_obligation_state` returns `missing`: kept and
charged, never retired by a deadline, and reported by the register validator; the expire selector agrees.

## Not in D2 (later slices)
Replay (RS-D R3', D4 slice), arbitration (B4), `record_account_settlement` (B4: a `lost` release is planted directly in tests),
orchestration register rows (B6: `interrupted_unattended` is planted directly in tests), the context sweep (D3).

## As built (D2 production)
- PH `_retire_due`, `_obligation_state`, `_unattended_close`, `_custody_ended_by_close`, `_expiry_now` (new, after `_live_result`); `_live_result`,
  `result_reservation`, `read_result`, prepare UPDATE, `_retire_result` (kind `lost_with_reservation` + cause for a reserved row whose obligation
  was released lost/crashed/released_unacknowledged), `retire_result`, `expire_results` (selector CASE + loop) changed.
- PRC `QA_MARGIN_MS`, `check_verifier_margin` (called from PH `_reserve_result_lease_locked`), `begin_retirement(evidence)`.
- history_contracts.py: new fixed reason `history_worker_result_expiring`.
- result_contracts.py `ResultReservation.__post_init__`: the lease deadline no longer bounds `prepared_at_ms` / `committed_at_ms`, and a prepared
  unpublished row may keep `expires_at_ms == reserved_until_ms` (SH-4; the old contract made a late publication `history_incompatible`).
  Only adds accepted states; no existing assertion changed.
- No schema step (18 -> 19 stays), no new account operation.
- Test fixes after the red commit (new tests only): X9 plants the `project_account_settlements` row the register validator requires (as B4 will);
  X8's last step asserts the late commit is `committed` and due at the next pass (RS-D draft 4 s2: "commits, and is due at the next pass").
- Known limits: V3/V4 are store-level (two committed lease-born attempts) not a driven verifier run; C2's verifier refusal is OrchestrationOwner.active_for
  (existing, not mocked here); `interrupted_unattended` register rows are planted (B6 writes them); replay (R3') is the D4 slice.
