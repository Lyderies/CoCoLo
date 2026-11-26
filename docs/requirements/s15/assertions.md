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

## S15-O0C-REPEAT-EXPIRY fix
No existing assertion changed or removed. Added test (new): an expired repeat generation that cleanup must keep is refused `probe_conflict`, never returned.

## Decision 382 continuation 2 at 4bc4dbbd

- **No existing test assertion changed or was removed. No runtime source or
  lifetime guard implementation/assertion changed.** Decision **382** remains
  the authority for old **all entries require ends+test** → new **all entries
  require kind+ends; count/bytes/time additionally require a real past-limit
  test; validation/read_only/precondition require ends only**. No exemption.
- Registry old: 197 absent tests (34 count / 147 bytes / 16 time) → new: 197
  concrete existing/new past-limit bindings and specific end/stimulus text.
  Strict guard RED at HEAD → GREEN, 547 retained sites, zero missing bounded
  bindings. No entry deleted in this continuation; the six earlier authorized
  stale removals have fresh complete-function grep/no-match evidence in the
  audit. Original audit dispositions remain history, not blanket recertification.
- Added tests use real reduced populations, buffers, files, clocks, store writers
  and queue/worker owners. Controlled frontend/signature/metadata seams isolate
  the stated cap; they do not establish native authorization. The worker-result
  reader uses actual writer bytes/digest with a metadata adapter. The QA cap
  uses one genuine pair above a reduced ceiling of zero; a draft second-pair
  stimulus conflicted with the one-QA-per-worker rule and never reached the cap.
  Account ingress uses actual grouping with controlled unfinished jobs, no frontend
  thread/socket and no claim that synthetic groups establish real job custody.
- Added matrix positives and 24 new release-suppression controls change no prior
  oracle: each reached release/deletion is disabled independently and its same
  positive release/baseline oracle must fail. Account controls disable the actual
  SQLite trigger; host controls disable the exact dictionary deletion; project
  controls suppress only their target release kind. There are 48 controls total.
- New draft corrections (not changes to existing test assertions): wrong guessed
  input error `worker_failed` → documented `worker_input_invalid`; guessed clock
  reason `clock_unavailable` → fixed `monitor_unavailable`; direct fence return
  → its actual `closure` envelope member; nonexistent runtime `_workers` → the
  preparation owner's actual `_pool`. The release, zero-charge and deletion
  expectations are unchanged. Account fault injection now targets the outer
  transaction that actually changes the hold; earlier drafts cut a nested savepoint
  or a read-only authorization transaction, before the intended release commit.
- The matrix explicitly preserves intentional retention and no-acquisition rows,
  the original B0-2 skip under 132/377, and S27a/b/c/d native container carry-ins.
  Full-service source references are named separately from this no-socket run.

## Decision 383 fix round (base 82cc5c48; no commit)

These are test/registry changes authorized by decisions 382/383. No runtime
behavior, original B0-2 assertion, or 132/377 skip changed.

| Existing pin / oracle | Old → new | Reason and evidence |
| --- | --- | --- |
| Publication proof registry dictionaries | `{checks, ends, test}` → the same fields plus `kind: count` on both D3 entries. Reservation-row `test_R2_a_reservation_only_directory_is_listed_before_earlier_retrying_contexts` → `test_reaudited_reservation_only_repairs_page_past_their_own_row_limit`. Context-row test/ends stay pinned. | Decision 382 requires kind; decision 383's audit found the old reservation test has one reservation-only row and does not fill that query. New test pages three actual reservation-only directories at limit one. |
| Health unrelated-cap preservation | Historical `ce81bd96` values, except two older S14a changes and one S5 rename → exact accepted `95ebe3a5` decision-382 shapes, plus 72 explicit decision-383 old/new deltas. | The old base predates the authorized kind/ends/test migration and six evidenced stale removals. No comparison against live JSON is used to manufacture expectations. Every other accepted entry is still pinned; the former two broad exceptions are removed. The full 72 per-entry **old → new** values are in `packages/codex-agents/tests/s15-cap-shape-updates.json` (including all 22 check-count changes and one evidenced stale cap removal). |
| Health past-page lease admission | Existing `reserved` expectation → **unchanged**. Fixture deletes synthetic leases only → deletes their synthetic held-lease register rows too. | RED `history_incompatible` came from `_result_counts` finding an SH-7 held identity with no source lease. Both rows were inserted by this test. Product validation stays intact; paired fixture cleanup restores its intended healthy admission. |
| Lifetime guard | Fail only when `actual > checks` over scanned sites → fail whenever `actual != checks` for every listed entry, including actual zero. Existing increased/new-site oracles → unchanged. | New over-list/zero control was RED for both actual zero and one with listed two; exact guard is GREEN after 22 registry count corrections. Source-proven zero regex sites are classified separately from genuinely absent cap code. |
| Guard field/kind fixtures | Noncurrent fixtures used `checks: 1` with an empty source counter → `checks: 0` with the **same empty counter**. | Decision 383's exact count is now an independent contract. Existing missing-field, invalid-kind and ends-only/past-limit diagnostics and assertions are unchanged; these fixtures continue testing entries absent from the scan. |
| `OwnerCapTests.byte_cap` | Exact-bound success, `assertRaises(error)` at size−1, restored success → those same oracles **plus** trace evidence of the particular module/constant's refusing comparison, deferred `invalid` comparison, or delegated `max_bytes` call. | Public exception class alone could accept a different failure. The helper checks the reached cap site; no old success/refusal expectation was removed. Cases using deferred validation and delegated bounded JSON retain their original product paths. |
| Registry metadata | 547 entries, 454 bounded; 227 inherited bindings pending review → 546 entries, 438 bounded; all 227 individually audited (178 confirmed, 33 rebound, 15 ends-only reclassifications, one stale cap removed). | Per-site original/resolved test references, source hashes, concrete stimulus and current disposition are in `cap-binding-audit.json`. Mixed count/byte/time functions state which independent bound is actually demonstrated. No blanket execution claim or exemption kind. |

Additional tests add oracles only: 21 owner-cap repair methods, one wall-only
repeat-expiry method and one over-listed-count guard method. Draft corrections
were fixture/API mistakes, not changed product expectations: use module-level
`takeover_journal.validate`; obtain body-size constant from the result fixture;
create enough real byte capacity for three leases; for the retained-evidence
byte cap use a genuine crashed closure **plus** product journal proof (plain
lost closure only reaches the earlier verifier). A second genuinely registered
project and its own context drive `max_projects`, before any second reserve.

The wall-only no-permit row records the accepted behavior precisely: no expired
original is returned; `probe_conflict` preserves it until monotonic expiry,
then ordinary cleanup removes its custody and starts a fresh generation.
This is not an assertion that wall time alone frees the generation.

RS-8's eight missing combinations are explicitly **pending**, assigned to
S27a/c/d with the real service/native boundary reason and independent reached
release mutation required for each positive release. They add zero executed
mutation claims; historical 48 controls remain historical evidence only here.

## Last corrections: lifetime-cap binding changes

- `user_settings.py::load` (bytes): rebound from
  `test_shared_settings_typed_bounds_and_no_import_io` (whose oversize input was
  also invalid JSON) to the new
  `test_shared_settings_byte_cap_rejects_valid_json_one_byte_over`: valid JSON of
  exactly SETTINGS_BYTES is accepted, SETTINGS_BYTES+1 is refused. Verified to
  fail with the `len(raw) > SETTINGS_BYTES` check disabled, then restored.
- `project_driver.py::_release_failed` (kind now `time`): rebound from the
  MAX_RETIRED_RUNS test to
  `test_W5_a_failed_run_is_kept_for_a_late_prepare_for_a_bounded_time_and_then_released`
  (injected clock past FAILED_VIEW_SECONDS). The second FAILED_VIEW_SECONDS site
  (view window) has the new `test_W5_a_refused_run_view_expires_after_the_view_window`;
  the MAX_RETIRED_RUNS test stays named in the entry's `ends`. The registry key is
  per function, so one entry cannot hold two tests. No assertion was removed.
