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


## Finish pass at 28d24d58

- **No existing test assertion was changed or removed.** No guard code or
  assertion was changed; the strict registry assertion remains red with 311
  unbound entries. The original B0-2 service assertion/skip is untouched.
- lifetime_caps.json: 50 absent `test` fields -> 44 existing past-limit test
  names and 6 new real past-limit test names; their vague `ends` descriptions
  -> concrete population/scope and stimulus descriptions. No exemption,
  deletion, fabricated test name or product change. Source evidence and the
  253 listed exceptions / 58 pending count entries are in cap-binding-audit.json.
- New session/fair-queue/native-SQL-tracker tests add assertions only. The SQL
  tracker test deliberately exercises `_track` with actual SQLite native cursors
  because the ordinary execute path has an earlier capacity check; it does not
  claim the existing execute-cap test reaches that independent defence.
- New release-path positives use real acquisition/release writers and assert
  exact released kind, companion retention where applicable and stable replay.
  Their 18 independent disabled-release controls require exactly one hit and
  failure of the unchanged positive oracle. They cover only their listed cells.
- Draft fixture-layer correction: component guard refused git during the real
  account-denial fixture setup -> scenario metadata plus a socket-denying audit
  hook before setup. No expected product state or release assertion changed.
- The audit lists non-count/configuration/read-only/consistency/stale sites for
  primary review; it does not weaken the registry guard or claim those rows
  have past-limit tests. This pass is explicitly incomplete.


## Decision 382 finish attempt — stopped on first-record byte defect

- Guard rule old: every entry requires `ends` and `test`, with no kind check
  -> new: every entry requires one of count/bytes/time/validation/read_only/
  precondition and nonempty `ends`; count/bytes/time additionally require an
  existing `test`. Decision **382** expressly authorizes this rule change.
  The new control was RED (8 subtest failures) before implementation and GREEN
  afterward. The live registry equality assertion is unchanged and remains RED;
  the registry-wide kind migration was not completed before the required STOP.
- Existing malformed-field and missing-test fixtures old: implicitly untyped
  entries -> new: explicit `kind: count`; their expected diagnostics and all
  assertions are unchanged. Validation/read_only/precondition now get the
  decided ends-only treatment. No existing test assertion was removed.
- `session_guard.py::_first_record`: generic audited/BOUNDED ends, no kind/test
  -> `kind: bytes`, concrete temporary scope and registered failing production
  boundary witness. This records the defect, not a passing cap binding.
- New witness adds assertions only: exact 8,388,608-byte JSON is accepted;
  8,388,609-byte JSON followed by newline must raise SessionRefused. The latter
  assertion fails with the actual parser and real file. Product code untouched;
  STOP per the user brief. Six stale entries remain present and unmodified.


## Decision 382 continuation at e2ae020d

- Existing assertions/code in `test_lifetime_cap_guard.py`: unchanged from HEAD.
  The decision-382 rule remains old **all entries need ends+test** -> new **all
  entries need kind+ends; count/bytes/time also need a real past-limit test;
  validation/read_only/precondition need ends only**. No exemption was added.
- Registry: 546 absent kind fields -> explicit kinds on all 547 retained rows.
  Six stale cap entries -> removed, only with full-function grep evidence in
  cap-binding-audit.json (decision **382**). The functions remain; their cap
  expressions are gone. Original `ends` remain except for concretely bound
  entries; 30 absent test bindings -> existing or newly registered real tests,
  including one failing product witness, which is expressly not a green claim.
- **No existing test assertion changed or was removed.** Added contract tests
  and repeat-probe witness/control add assertions only. The existing first-record
  tests and all matrix tests remain unchanged.
- New probe draft fixture: real fixed signature execution timed out before the
  count stimulus -> product owner commit writer plus genuinely parsed retained
  provider references seeds the reuse conclusion as a fixture fact. This makes
  no signature/native-authority claim. The fresh-deadline oracle is unchanged
  and is RED at both 2 ms and the 60,000-ms production timeout. Its final capture
  check is scoped to accepting a fresh signature-input capture; it does not
  claim a completed signature worker or a new probe admission.
- New wire draft referenced nonexistent `SignatureInputError` -> the product's
  actual `SignatureWireError`; the exact-limit/one-more-byte expectations are
  unchanged. No product behavior or baseline assertion was adjusted.
