# S13G assertions changed (decision 389)

No budget or debt ceiling was raised. Every change is old -> new with its reason.

## tests/test_run_retention_scale.py (S14b-4)
| Old | New | Reason |
|---|---|---|
| One build of 365,000 summaries (2,000 product appends + 363,000 batch rows) and 50,000 detail rows, measured once | Two sizes, 20,000 and 80,000 summaries (a year and four years at ~200 batches/day), 10,000 detail rows (same ~1:8 share); each size runs the same measurements | Cause: 20 of 45 s were SQLite `executemany` inserts into the records table and its indexes (365k rows), plus 730k member rows written twice (batch insert, then dropped and rebuilt by `rebuild_history_index`). Insert time grew with load (37 s alone in a 12-unit run, 57 s in the load run, 68-85 s in full runs). |
| `assertLessEqual(index_bytes/(N+1), 40)` once at 365,001 | same assertion at both sizes | unchanged property |
| Old JSON walk raises `SQLReadError` under the 25 ms read budget at 365,001 | same assertion at 80,001 only (the walk takes ~120 ms there, 4.7x the budget; at 20,000 it takes ~30 ms, near the budget, so it would prove nothing and flake) | the oracle needs a size clearly over the budget; 80,000 keeps a 4x margin |
| Three lookups (found, wrong type, typo) once | the same three lookups at both sizes | unchanged |
| (none) | NEW growth claim: bytes per entry at 80,000 <= 1.15 x at 20,000; rebuild pages at 80,000 within 3.5x..4.5x of the pages at 20,000 (linear in the history, not quadratic) | the scale claim now holds by growth ratio over a 4x step, as S14E-3 does, instead of by one large size |
| `bulk_summaries` also inserted `retention_members` rows | no member insert in the bulk helper | the following `rebuild_history_index` drops the table and rebuilds it from the stored summaries before anything reads it, so the batch rows were never observed (no assertion depended on them) |

## tests/test_project_start.py
| Old | New | Reason |
|---|---|---|
| `StarterBase.setUpClass` ran `compileall` of the runtime into a per-class cache (15 classes) | one cache per test process, compiled once (`shared_pycache`), removed at exit | 0.5 s alone each, several seconds under load; the S13C-1 property (compiled before any test clock starts) is kept |
| `StarterBase.CONSTANTS` `STARTER_CHECK_SECONDS` 0.2, `START_RECHECK_SECONDS` 1.0 | 0.1 and 0.5 | named test intervals; each wait for a start or a recheck is shorter, and every window below holds at least as many starter passes and rechecks as before |
| `time.sleep(1.5)` "rechecked, still not replaced" (E2c-7 twice) and "a serving recheck never replaces an active probation" | `time.sleep(1.0)` | 1.0 s now holds two rechecks (0.5 s) and ten claim passes (was 1.5 rechecks / 7.5 passes) |
All other assertions in both files are unchanged.

## Decision 390 rewrite (tests/test_project_start.py, S13G-3)
New StarterBase helpers `start_clock`, `advance`, `clock_ms`, `sync_clock_to_due` drive the launcher's existing `clock` option (only the starter's policy clock moves).
| Test | Old | New | Reason |
|---|---|---|---|
| ProbationTests.test_E2c_4_a_probation_is_kept_across_a_collector_restart | 10 s probation; real `sleep` of 6 s before 'dies' is recorded and ~4.5 s before the restart | 60 s probation on the policy clock; 'dies' recorded 36 000 ms after 'lasts' started, restart at 'lasts' probation end + 500 ms (clock advanced, no sleep); `streak_first_ms` is clock-60 000 | same order, same margins (restart > end+0.5 s of 'lasts', and `assertLess(now, dies.probation_until - 2000)` now on the policy clock); same states and counts asserted. After `record_launch` the clock steps 1 s because a pre-record claim of 'dies' backs off one RECHECK on the frozen clock (a race that real time hid). The long probation keeps the first collector's own monotonic watch from ending it. ~12.8 s -> ~2.7 s |
| ProbationDeadlineTests.test_E2c_4_a_host_that_died_early_... | 3 s probation, real `sleep` until probation_until+1 s | 60 s probation, clock advanced to probation_until+1000 ms | same states (probation_attempt None, failures 1, `service_ended_early`). ~5.9 s -> ~1.7 s |
| ProductionIntervalTests.test_E2c_3_1000_due_projects... | 4 served + 16 changed-install projects + bulk rows to 1 000 | 4 served + 6 changed + bulk rows to 1 000 | still more projects than START_SLOTS (2), so the queue is still deeper than the slots; every changed project still asserts `install_changed`; production intervals and the 60 s drain bound unchanged (the one real-time case). Each changed project costs ~0.35 s of install |
Kept real-time (one case per behaviour): ProbationTests serving-recheck (real recheck wait), BackoffTests early-exit (real own-child probation expiry), ProductionIntervalTests (product 5 s intervals). BackoffTests crash-loop already runs on the policy clock; its time is the five real host spawns that fail, which are what it proves.
