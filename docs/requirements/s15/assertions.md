# S15 assertion history

## Original B0-2 service test: skip → active → skip

- Base: `@unittest.skip("gates the run supervisor: the owner narrowed B0 on
  2026-11-20 (stranded reservations)")`.
- Earlier S15 session: skip removed and `S15-B0-2` added, with original service
  assertions intact. Split store regression expected a non-NULL lost terminal,
  and failed. This was the stopped state at resume.
- Decision 377 (reaffirming 132): restore the skip, now
  `Owner decisions 132/377: B4s writes no terminal record by specification;
  S15 tests settlement and release separately.` Remove the S15 ID from the
  incompatible original service test, retain its legacy ID, restore its deferred
  metadata and update the authority description/reason. The original
  `assertIsNotNone(self.reservation_terminal(run_handle))` and wait for
  `self.reservation_terminal(run_handle)[1]` are **unchanged**; no service
  assertion was weakened or removed.

## Split reservation recovery test: approved B4s re-aim

- Old test `test_B0_2_successor_settlement_gives_the_stranded_reservation_its_lost_terminal`
  → `test_successor_settlement_releases_once_frees_capacity_and_writes_no_terminal`.
- Old `assertIsNotNone(row['terminal_record_id'])` →
  `assertIsNone(row['terminal_record_id'])`, explicitly authorized by 132/377;
  B4s must not invent a terminal/outbox/record. Keep S15-B0-2 as the re-aimed
  requirement ID, with its updated statement in the source registry.
- Retain `retiring`, released/lost hold, first receipt and consistency checks.
  Add stable receipt replay after reopen, unchanged full settlement snapshot,
  exactly one settlement row, unchanged terminal/consumption/cleanup/record
  counts, expiry returning live entries to baseline, and fresh admission.
- Earlier draft observer correction retained: side-effecting
  `self.reservation(1).state` → read-only `SELECT state, terminal_record_id`;
  the accessor lazily retires the reservation. This was already recorded before
  decision 377; expected `retiring` is unchanged.

## Existing lifetime guard assertions

- Old permissive runtime registry check → require nonempty string `ends` and
  `test` on **all** entries and reject a supplied name absent from test sources,
  while retaining new-site and increased-check detection. The real registry
  assertion `assertEqual([], problems(...))` is unchanged and intentionally RED.
- `test_a_listed_function_gaining_a_limit_is_caught`: old diagnostic lookup
  `problems(...)[0]` → `problems(...)[-1]`. Its exact expected text
  `2 limit checks, 1 listed` is unchanged. New missing-field diagnostics precede
  the same increased-check diagnostic; the minimal fixture lacks both fields.
- No other existing test assertion changed. New malformed-field controls cover
  absent, null, empty, whitespace-only and non-string values, including stale
  registry sites. No claim that text name existence proves a past-cap execution.
- `_result_counts` registry evidence: vague audited/BOUNDED text without a test
  → specific release/purge description and the real past-limit **failing** P3
  test. This registers the defect; it does not pretend the cap is proved safe.

## New matrix drafts and controls

- New memory draft clock old `now += ORCHESTRATION_IDLE_MS + 1` after setting
  `now=0` → stored orchestration `due_at_ms + 1`. The old draft remained below
  the original due boundary by 999 ms; this fixes the stimulus, not the expected
  close count, release kinds or state assertions. All eight cells then pass.
- Mutation controls disable exactly one release kind, assert it was reached
  once, and require the unchanged positive row oracle to raise AssertionError.
  They are sensitivity evidence for six covered releases only.
- New P3 test remains RED at the live-capacity read after the third successful
  reserve/release/replay cycle. No expected error was substituted for its
  baseline-count assertion. No product fix was made.

## P3 fix: lifetime_caps.json registry entry (not a test assertion)

- `project_history.py::_result_counts` `"checks": 2` -> `"checks": 3`: the function now has two bounded reads
  (live leases, held lease register rows) and their MAX_LEASE_ROWS check, replacing one all-rows read and its check.
- Its `ends` text "S15-P3 exposes ... admits MAX_LEASE_ROWS + 1, then refuses live-capacity reads" -> the fixed
  behaviour (only live leases verified). `test` binding unchanged. No test assertion changed or removed; the S15
  churn witness is unchanged and now green.
