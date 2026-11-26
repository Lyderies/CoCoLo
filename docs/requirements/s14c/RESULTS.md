# S14c small large-ledger growth profile (measurement only)

Script: `scripts/measure_s14c.py`, run only through `scripts/run_measurement.py` with defaults (300 s, 1 GiB, 3 GiB RSS; no --approve-decision),
one size per run. Raw output: `size-<N>.json` and `results.json` in this folder. No product code changed.

## How the ledgers were built (and what that means)
One real open + claim through the product path (signature worker included) is taken from the admission test fixture; its rows in 11 tables are
cloned with fresh run-specific ids (admission_evidence and run_holds fill from the product's own triggers), 774 runs/day spacing, all older than
the 7-day open-request window, until the database reaches the size. Clones are claimed, unreleased runs (the only state a single real run
leaves). Density came out at 48,232 runs/GiB (about 22 KB per run), the same as the stopped 4 GiB build (~188k runs), so "size" and "runs" convert
with that figure. The 1024 MiB size was NOT run: the 1 GiB generated-data cap includes the file log, and the largest size that fits with headroom was 512 MiB
(the upgrade copy needs a second copy, so it ran up to 256 MiB only). Timings are one run each on a shared machine.

## Table (seconds unless noted; Size = database MiB)
| MiB | runs | build | Q1 active_run_capacity | Q2 custody (holds held) | Q2 with kind (candidate) | Q2 (holds released) | product open | 17->18 upgrade | idle account retention | idle run retention | status read | run total |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 34 | 1,607 | 0.8 | 0.0006 | 0.0418 | 0.0014 | 0.162 | 0.073 | 0.22 | 0.045 | 0.0 | 0.0052 | 2.8 |
| 87 | 4,101 | 2.1 | 0.0012 | 0.2618 | 0.0041 | 1.0743 | 0.334 | 1.064 | 0.272 | 0.0 | 0.0144 | 7.1 |
| 128 | 6,061 | 3.5 | 0.0017 | 0.5761 | 0.0067 | 2.5003 | 0.687 | 2.31 | 0.589 | 0.0 | 0.0224 | 12.9 |
| 259 | 12,221 | 7.5 | 0.0033 | 2.4267 | 0.0178 | 13.685 | 2.618 | 11.078 | 2.432 | 0.0 | 0.0514 | 46.5 |
| 514 | 24,252 | 16.5 | 0.0065 | 12.7467 | 0.043 | 45.0034+ (stopped at cap) | 13.777 | not run | 12.84 | 0.0 | 0.112 | 129.6 |

Per-run lookups (lost-run lookup, withdrawal, denied-open maintenance) stay at 0.0001-0.0074 s at every size (all `SEARCH ... USING INDEX` on the
hold primary key with kind and identity). Retention (run) idle pass 0 s. Status read (`account_holds_status`) 1.4 ms -> 112 ms at 512 MiB (grows; unprofiled, see step 5).

## Fitted curves (power law t = a * runs^b; log-log fit over the five sizes) and extrapolation at 48,232 runs/GiB
| Path | exponent b | 4 GiB | 8 GiB | 16 GiB |
|---|---|---|---|---|
| Q2 custody, holds held (s) | 2.09 | 867.5 | 3,704.7 | 15,820.5 |
| Q2 candidate with kind (s) | 1.27 | 0.6 | 1.4 | 3.4 |
| product open (s) | 1.92 | 598.9 | 2,263.7 | 8,555.6 |
| Q1 active_run_capacity (s) | 0.88 | 0.0 | 0.1 | 0.1 |
| status read (s) | 1.14 | 1.2 | 2.6 | 5.6 |
| Q2 holds released (s), 4 sizes (512 MiB hit the 45 s cap) | 2.18 | 5,079.6 | 22,970.9 | 103,879 |
| consented 17->18 upgrade (s), 4 sizes (no copy fits at 512) | 1.92 | 1,968.0 | 7,454.3 | 28,235.6 |

An exponent near 2 is quadratic; near 1 is linear. The held-holds Q2 and the product open (which contains it) are quadratic; the same query with the
kind in the lookup is linear and small (0.04 s at 24k runs; fitted exponent 1.27, about 3.4 s at 16 GiB). Quadratic at ~190k runs (4 GiB): about 14 min with holds held (open ~10 min, upgrade ~33 min), about 85 min once holds are released;
this matches the stopped run (run-lineage lookup over 60 s at 4 GiB; open over 12 min at 8 GiB). The stopped run's 46 s upgrade at 8 GiB is NOT reproduced here (mine is quadratic too); its data had a different state mix.

## What breaks first, and when
The open path, at about 1-3 GiB (5-minute phase budget crossed at ~2.4 GiB held, ~2.8 GiB for the whole open, ~1.1 GiB once holds are released): it is the product open, the retention sweeps (`sweep_account_retention` runs the same custody purge) and the 17->18 upgrade all
running `live_ledger_custody` Q2. Open already takes 2.6 s at 259 MiB and 13.8 s at 514 MiB; the 5-minute phase budget is passed near 2.3 GiB (held) or ~1 GiB (released holds,
whose Q2 hit the 45 s cap at 514 MiB); at 4 GiB expect 10-85 min per open and per maintenance pass; at 16 GiB 2.4-29 hours. Nothing else measured grows past milliseconds below 512 MiB except the status read.

## Root causes
1. `packages/codex-agents/native-package/.codex/orchestration/codex_agents/runtime_lineage.py:143-147` (`live_ledger_custody`, 2nd query): `EXISTS(SELECT 1 FROM run_holds r WHERE r.project_id=i.project_id AND r.identity=i.handle_digest AND r.state<>'released')`
   omits `r.kind`. Plan: `SEARCH r USING INDEX sqlite_autoindex_run_holds_1 (project_id=?)`: a prefix of the primary key (project_id, kind, identity), so every launch input whose binding has expired scans
   all of the project's run holds: runs x 2*runs rows (quadratic). With `r.kind='binding'` (and a second branch for run_slot) the plan is `(project_id=? AND kind=? AND identity=?)`, 0.04 s at 24k runs. Q1 (`active_run_capacity`) is a plain SCAN, linear, 6 ms at 24k runs.
   Called at: `learning_store.py:3579-3582` (`_purge_runtime_lineage`), from every ledger open (`:3575`), from `sweep_account_retention` (`:2705`), and inside the 17->18 upgrade.
2. "Run-lineage lookup" of the stopped run: no separate query reproduced as slow; every per-run keyed lookup is indexed. The slow path matching its symptom and its 4 GiB size is the same custody query (point 1). If the stopped step named another function, it needs naming (it left no log).

## Proposed plan steps (one line each; findings only, no fixes made)
1. S14e: fix `live_ledger_custody` Q2 to use the hold primary key (kind + identity), with a test that fails if its plan has `(project_id=?)` only and an open-time budget test at 24k runs (>=12 s today).
2. S14e: cache or incrementally maintain custody digests so open, each retention sweep and the upgrade do not recompute the full history (the sweep runs it every tick).
3. S14f: add plan-inventory test coverage for every query on `run_holds` filtering by identity without `kind` (DENIED_OPENS use `kind IN`, fine; audit the rest).
4. S14g: Q1 `active_run_capacity` scan (linear, 6 ms at 24k): add a partial index on `released_at_ms IS NULL` or accept; revisit past 4 GiB.
5. S14h: profile `account_holds_status` (1.4 ms to 112 ms from 1.6k to 24k runs, ~linear-plus) before 4 GiB.
6. S14i: after fix, rerun `scripts/measure_s14c.py` at 256 and 512 MiB; the 17->18 upgrade at 512 MiB needs a 2 GiB cap approval or a streamed copy.

## After S14e (decision 380; `live_ledger_custody` uses `r.kind IN ('run_slot','binding')`, the full hold key)
Same script, same runner defaults, one size per run (raw: `after-s14e-259.json`, `after-s14e-514.json`). The "Q2" rows of the script still time the OLD
query text verbatim (kept as the baseline probe), so they stay slow; the product function is the row `live_ledger_custody`.
| MiB | runs | custody before (s) | custody after (s) | open before (s) | open after (s) | idle account retention before -> after (s) | 17->18 upgrade before -> after (s) | run wall |
|---|---|---|---|---|---|---|---|---|
| 259 | 12,356 | 2.427 | 0.024 | 2.618 | 0.236 | 2.432 -> 0.043 | 11.078 -> 1.185 | 27.9 s |
| 514 | 24,353 | 12.747 | 0.056 | 13.777 | 0.485 | 12.84 -> 0.098 | not run | 78.1 s |
Custody and open now grow about linearly (259 -> 514 MiB: custody x2.3, open x2.1); the remaining open cost is not the custody query. Extrapolated to 4 GiB
(x8 from 514 MiB): custody about 0.5 s, open about 4 s (before: 867 s and 599 s). The remaining quadratic suspects (`account_holds_status`, Q1) are S14g/S14h.

## After S14f (decision 388; the owner-thread sweep computes custody in bounded passes, `CustodyScan`, 256 rows per pass)
Same script and runner defaults (raw: `after-s14f-256.json`, `after-s14f-514.json`; the script now also times every pass of one paged cycle with one lineage edge on a used runtime).
Before = whole-history `live_ledger_custody` on the owner thread (the S14e figure, best of 3); after = the worst single pass the owner is held for.
| MiB | runs | whole custody per sweep before (s) | paged: passes | worst pass after (s) | whole cycle after (s) | run wall |
|---|---|---|---|---|---|---|
| 259 | 12,356 | 0.024 | 96 | 0.0005 | 0.026 | 28.6 s |
| 514 | 24,353 | 0.053 | 190 | 0.0006 | 0.059 | 78.6 s |
The per-pass owner time is constant (about 0.5 ms; bounded by the row budget, not history size) and the cycle total is linear, as before. Open and the 17->18 upgrade keep the whole computation (S14e).

### After S14f fix round (r1 NOT-READY: clock, total work, invalidation; the sweep now runs the whole prune cycle in bounded passes, `CustodyScan.advance`)
Same script and runner defaults (raw: `after-s14f-256.json`, `after-s14f-512.json`, overwritten). Each pass now does at most 256 units of ALL its work
(lineage edges read by key page, history rows, protected-path walk steps, deletions); the timed cycle is the full owner cycle including walk and deletes.
| MiB | runs | whole custody per sweep (s) | paged: passes | worst pass (s) | whole cycle (s) | run wall |
|---|---|---|---|---|---|---|
| 259 | 12,204 | 0.025 | 96 | 0.0007 | 0.028 | 30.9 s |
| 515 | 24,260 | 0.095 | 190 | 0.0012 | 0.101 | 85.6 s |
The worst pass stays about 1 ms (bounded by the unit budget, not history size); the cycle total is linear. The change watch (TEMP triggers) adds no
measurable pass cost. 17->18 upgrade at 512 MiB not run (disk cap, as before).

## After 0.8.73 (S14e/S14f/S14g; S14i re-measure, tree feat/s14i-0874 at 0.8.73 be6f17a6 + S14G-4)
Same script, runner defaults (300 s, 1 GiB, 3 GiB RSS, no approval), one size per run; raw `after-s14i-256.json`, `after-s14i-514.json`. Wall 27.2 s + 77.4 s = 104.6 s in total;
peak generated data 735 MB (file log included, deleted after each run); peak RSS 217 MiB. 17->18 upgrade at 514 MiB again not run (copy would pass the 1 GiB cap). The "Q2 ... old text" probe rows still time the pre-S14e query on purpose.
Original S14c values are from the table above (259 / 514 MiB runs; 12,221 / 24,252 runs); now 12,213 / 24,351 runs.
| Measure (s) | 256 MiB before | 256 MiB after | 514 MiB before | 514 MiB after |
|---|---|---|---|---|
| open (product) | 2.618 | 0.228 | 13.777 | 0.469 |
| custody, whole history (`live_ledger_custody`) | 2.427 (Q2) | 0.023 | 12.747 (Q2) | 0.053 |
| custody, paged worst pass the owner thread is held (S14f) | n/a (whole 2.427) | 0.0005 | n/a (whole 12.747) | 0.0007 |
| 17->18 upgrade | 11.078 | 1.131 | not run | not run |
| idle account retention pass | 2.432 | 0.017 | 12.84 | 0.043 |
| idle run retention pass | 0.0 | 0.0 | 0.0 | 0.0 |
| status read (`account_holds_status`) | 0.0514 | 0.0511 | 0.112 | 0.1106 |
| per-run lookups (lost run, withdrawal) | <=0.0074 | <=0.0001 | <=0.0074 | <=0.0001 |
Open, custody, upgrade and idle retention are linear now (256 -> 514 MiB: open x2.06, whole custody x2.3, retention x2.5; fitted exponents about 1.0-1.3). The worst owner pass is about 1 ms at both sizes (bounded by rows, not history).
The status read is unchanged and linear (x2.2): S14h has not touched it.

Extrapolation, linear from 514 MiB (x8, x16, x32 for 4, 8, 16 GiB; 48,232 runs/GiB):
| Path | 4 GiB | 8 GiB | 16 GiB | before (4 / 16 GiB) |
|---|---|---|---|---|
| open (s) | 3.8 | 7.5 | 15 | 599 / 8,556 |
| whole custody (s; open, upgrade only) | 0.4 | 0.8 | 1.7 | 868 / 15,820 |
| paged worst owner pass (s) | 0.001 | 0.001 | 0.001 | n/a |
| 17->18 upgrade (s, from 256 MiB x16/32/64) | 18 | 36 | 72 | 1,968 / 28,236 |
| idle account retention pass (s) | 0.3 | 0.7 | 1.4 | 1,968 (open-like) / 15,820 |
| status read (s) | 0.9 | 1.8 | 3.5 | 1.2 / 5.6 |

What breaks first now: nothing crosses the 5-minute phase budget up to 16 GiB. The first thing that gets slow is the status read (about 1 s at 4 GiB, 3.5 s at 16 GiB, linear and unprofiled, on a user-facing read path), then the 17->18 upgrade (72 s at 16 GiB) and its need for a second full copy of the database on disk (the disk cap stopped it at 514 MiB here).
Proposed steps (one line each):
1. S14h: profile `account_holds_status` and bound it (cache or page), since it is the only measured path still growing with history on a read the user waits for.
2. Make the 17->18 upgrade stream or batch its copy so it needs no second full-size copy and has a visible progress and budget at 8-16 GiB.
3. Re-measure at 1 GiB with an owner-approved cap (or skip) only after step 1; everything else is linear and below 20 s at 16 GiB.

## S14h: `account_holds_status` profile (no product change)
Added `status_profile()` to `scripts/measure_s14c.py` (cProfile of one status call plus held-row counts). Runner defaults, `--no-upgrade`, one size per run:
64 MiB 5.6 s wall / 100 MB data / 162 MiB RSS; 256 MiB 25.4 s / 318 MB / 173 MiB; 514 MiB 77.6 s / 736 MB / 216 MiB (data deleted after each; 109 s total).
| MiB | runs | held run_slot / binding rows | run_holds rows | status read (s, mean of 5) | profiled single call (s) |
|---|---|---|---|---|---|
| 64 | 4,101 | 4,102 / 4,103 | 8,205 | 0.0142 | 0.0166 |
| 256 | 12,214 | 12,215 / 12,216 | 24,431 | 0.0508 | 0.0562 |
| 514 | 24,346 | 24,347 / 24,348 | 48,695 | 0.1089 | 0.1235 |
Where the time goes: all three statements already read only held rows (`run_holds INDEXED BY run_holds_due ... state='held' AND kind=... AND project_id=?`, pinned by
`test_s14g_hold_queries.py` and `test_run_holds.py`); SQL fetch is about 9 ms of 56 ms at 256 MiB, the rest is the Python loop over each HELD slot (`_slot_eligible`, deadline/due
min/sum, about 4.5 us per row). Time is linear in the number of HELD holds, not in released history. The fixture is the worst case: every cloned run is claimed and never released
(24k held slots), which a real ledger never has (held slots are bounded by concurrent runs and the open-request/binding caps; settled runs are released and not read).
Conclusion: no query scans history; nothing to bound or index with the same result. No product code, requirement or test changed. The 16 GiB figure (3.5 s) is the all-runs-held
fixture extrapolation (linear in held rows, about 2.2 ms per 1,000 held); with real held counts (tens) the read is sub-millisecond at any history size.
Q1 `active_run_capacity` (plain scan, 6.5 ms at 514 MiB, 0.1 s at 16 GiB) stays accepted; it too sits under every budget.
