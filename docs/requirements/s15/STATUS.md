# Decision 382 continuation — STOP: expired repeat-probe generation returned again

**INCOMPLETE. Do not merge as finished S15.** No commit, socket, or product
code change was made. The first-record defect is fixed at HEAD `e2ae020d`;
this stop is a separate defect, **S15-O0C-REPEAT-EXPIRY**.

## Product defect and reproducer

`account_billing_admission.py::_reserve_repeat` checks its existing same-key
reservation before `_forget_closed_contained()`. It returns an expired repeat
when its history, context and snapshot still match, without checking either
of that generation's deadlines. The ordinary cleanup path immediately below
would discard the expired, unprepared generation. `prepare()` then refuses the
returned generation because its wall deadline has passed.

The registered witness is
`tests.test_s15_probe_caps.RepeatProbeCapTests.test_expired_repeat_generations_free_the_count_cap_for_the_same_request`.
It uses real owner reserve/commit writers for three stored successful-reuse
fixture rows, real retained provider records and their product-parsed references,
and no SQL inserts or invented receipts. The reuse conclusion is a fixture fact:
this test does **not** prove the signature gate or native enrollment. At reduced
machine/project pending quotas of two, two real repeat generations fill both
quotas and a third is refused `probe_full`. Neither generation has a signature
handle, permit or running job. After **both clocks** pass their deadlines, retrying
the first request returns its expired reservation. The fresh-deadline assertion
is RED for both a 2-ms timeout and the actual **60,000-ms default timeout**:
`10000000102 <= 10000000103`, and `10000060100 <= 10000060101`.

The companion different-request control is GREEN: retrying the third committed
request after the same expiry reaches ordinary cleanup, drops both expired
reservations and their retained inputs, takes one fresh generation, and can
capture its signature inputs. This bounds the finding: it is a same-request
expiry/retry gap, **not** a claim that every new request remains blocked forever.
Authority: decision 382's real past-limit evidence, RS-A F6's expired-probe owner
memory lifetime, and the product's `_forget_closed` comment that a repeat
"ends at its deadline". STOP per the user's instruction; no product fix attempted.

## Registry and binding progress

- All **547 retained entries** now have `kind` (546 new labels plus the existing
  first-record label): 190 count, 230 bytes, 34 time, 65 validation, 21 read_only,
  7 precondition. All retain nonempty `ends`; no exemption kind exists.
- Exactly the six decision-382 stale cap sites were removed. Their functions
  still exist; the **cap-check code sites** are gone. The audit records a grep
  over each complete function body, exit 1/no matches, source span and hash.
  `_reply` delegates to `_build_reply`; `admit` delegates to `_admit`; those
  current bounded sites remain separately registered. No other entry removed.
- **30 formerly missing bindings** were added: reviewed existing tests, seven
  new contract-boundary tests, and the RED repeat-expiry witness. New tests
  cover all six request codecs at an exact reduced frame ceiling and one byte
  beyond, recursive cleaning, exit locations, usage arrays, escalation ladders,
  real proof-directory pruning and independent wire writer budgets.
- The guard rule at HEAD is preserved: every kind requires `ends`;
  count/bytes/time additionally require a real named past-limit test. The old
  -> new rule and decision 382 are recorded in assertions.md. Name existence
  alone is not execution proof. The registry guard still correctly stays RED.
- **197 bounded entries remain unbound**: 34 count, 147 bytes, 16 time.
  Of the original 58 pending-count audit rows, 9 now have bindings (including
  this RED witness); 34 count and 15 byte rows remain unbound. Existing historic
  bindings still marked `bound-review-required` in the audit have not received
  a new blanket certification. `kind`, `current_matches`, `binding_status` and
  binding evidence describe the continuation; original dispositions remain
  historical so the source of every migration is reviewable.
- No existing test assertion changed or was removed. No runtime source changed.
  Both new modules and the new requirement ID are registered in layers, stages,
  timings, requirement source and catalogue. The two modules forbid sockets
  before fixture setup.

## Matrix and verification at STOP

Matrix work did not resume before this cap defect. The earlier 34 positive
store cells / 24 release mutation controls remain the last completed partial
matrix, with their limitations and every unfinished row retained below.
**S27a/S27b/S27c/S27d** remain explicit container carry-ins below; none was
converted to a pass or executable skip. Decisions 132/377 and B0-2's skip remain.

Final named-module runner: **metadata_errors: [], budget_failures: []**;
15 tests in 1.9 s elapsed / 1.851 worker-s, discovery 2.325 s and preflight
7.429 s. New probe module 0.8 s, contract module 0.3 s, guard 0.7 s; both
new modules are below 60 s. Contract caps and the different-request control
are GREEN; only the registry guard and same-request defect witness are RED.
Evidence: `decision-382-repeat-stop-results.json`. No run remains in flight.
Only the three named modules were run; no full/affected suite was selected.

Initial probe draft tried real signature-process preparation and timed out in
this workspace (20.6 s); that is **not** product-defect evidence. Its replacement
uses the real owner commit writer with parsed stored references solely to seed
reuse fixtures, and reaches the cap/expiry branch without native execution.
The final RED is the unchanged fresh-deadline oracle after real cap saturation,
not the earlier fixture timeout or an assertion changed to match product code.

---

# Decision 382 finish attempt — STOP: production first-record byte-cap defect

Current work is **INCOMPLETE; do not merge as finished S15**. No commit was
made, no product code changed, and no socket was opened. Owner decisions
132/377 and the original B0-2 skip remain intact. Container carry-ins
S27a/S27b/S27c/S27d remain listed in the retained status below.

## FIXED: session_guard._first_record accepted bytes past its limit (S15-O0C-FIRST-RECORD)

Root cause: the loop was `while size <= MAX_FIRST_RECORD_BYTES` with a fixed 65,536-byte read. At exactly the limit
it read one more block; a newline in that block broke out before the bytes were counted, so a record of limit+1 bytes
(and any record whose newline arrived in the next block) was accepted. Fix: read only up to MAX+1 bytes in total
(`min(65536, MAX+1-size)`); no newline within MAX+1 bytes -> the existing UNKNOWN_SHAPE refusal. A record of exactly MAX
bytes (newline at byte MAX+1, or end of file) is accepted. Memory stays bounded.
Other readers audited with the same pattern: write_merge `_Seen` hashing (1 MiB blocks, size > limit -> no sha; correct
at the boundary), service_enrollment proof read (reads limit+1, refuses len > limit; correct), core_api `_LineReader`
(limit+1 read, `len(line) > limit` refused; correct). No assertion changed. Witness
`test_first_record_rejects_one_byte_past_its_real_byte_limit` RED -> GREEN; new
`test_first_record_exact_boundary_at_reduced_limit` (limit 200: 199/200 accepted, 201 refused, with newline, newline+more, and
no newline) RED -> GREEN. Remaining RED: S15-O0C-GUARD (Codex finish work: 552 kind migrations, 58 open count caps).

## Decision 382 progress and exact remaining work

- New kind-rule control RED (8 subtest failures) -> GREEN; guard implementation
  now requires a valid kind and ends for every entry, with existing test names
  additionally required for count/bytes/time. No kind named exempt exists.
- Existing malformed-field fixtures explicitly use count; expected diagnostics
  and assertions are unchanged. Old -> new rule is recorded in assertions.md.
- Only the newly failing first-record entry has been assigned kind=bytes and
  bound here. The other **552 entries still need kind migration**. The 58 open
  count caps and remaining bytes/time bindings are not completed. The full
  registry guard is RED; it has not been bypassed or presented as green.
- Six stale entries remain present. No stale-site deletion was performed; each
  still needs current-source grep evidence in the audit before removal.
- Hold-kind × failure-exit matrix and mutations were not extended in this
  attempt before the defect STOP; all previous unfinished rows remain below.

## Verification

Final named-module runner: **metadata_errors: [], budget_failures: []**;
11 tests, 9 passed, 2 failed (first-record defect and incomplete registry).
Elapsed 0.918 s; worker total 0.913 s; discovery 2.307 s; preflight 7.401 s.
Session module 0.177 s, guard module 0.736 s, both below 60 s.
Durable result: `decision-382-stop-results.json` (selected requirements only).
Command: `PY -B scripts/parallel_tests.py --module codex:tests.test_lifetime_cap_guard
--module codex:tests.test_s15_session_caps --jobs 1 --keep`.
No run is left in flight. The runner's unrelated catalogue regeneration was
removed while preserving both S15 registration changes.
The first check found one catalogue-link error: a method-specific requirement
overrides its class requirement. The spurious S15-O0C-BINDINGS link was removed;
the witness stays registered to its own S15-O0C-FIRST-RECORD requirement.
Both affected modules already have layer, stage and timing registrations.

---

# S15 finish pass — INCOMPLETE (2026-11-26, base 28d24d58)

The strict guard is unchanged and remains **RED**. Do not merge this pass as
finished S15. No product file was changed and no commit was made.
Decisions 132/377 remain in force; the original B0-2 service assertion stays skipped.

## Guard binding audit and primary adjudication

- Of the 361 initially missing bindings, **50 are now bound: 44 existing tests
  and 6 new tests**. Each replacement `ends` describes the actual counted
  population/temporary scope and the test stimulus. A binding establishes the
  specific cap exercised, not every branch in a multi-check function.
- **311 entries remain unbound**: **253 listed for primary adjudication** and
  **58 real count-cap entries still needing verified test evidence**. This is
  unfinished work, not a green guard or a blanket exception.
- `cap-binding-audit.json` records every original missing site, exact matching
  source lines, disposition, and bound test/module where available. Listed
  categories: 218 non-count checks, 13 configuration validations, 14 read-only
  counts, 2 consistency preconditions, 6 stale registry sites. No registry entry
  was deleted, marked exempt, or bound to a guessed/static name-existence test.
- Representative primary exceptions: `account_api.py::__init__` line 483 is a
  timeout; `account_api.py::integer` line 108 validates a scalar integer;
  `account_api.py::decode_account_request` line 237 caps frame bytes;
  `project_history.py::runtime_close_status` line 3614 is a read-only status
  count; `_reply`/`admit` in account_billing_admission have no current count-site
  match after refactoring. See the audit for the complete list and evidence.
- The current guard requires `test` even for all those listed sites. Making it
  green requires the primary to reconcile their registry treatment with the
  requested real count-cap evidence. This pass preserves the strict guard.
- New real past-limit tests: four session scans use three real directory/day/
  binding/rollout entries at a reduced limit of two; fair queue fills both
  project and item quotas, refuses both extra populations, drains, and admits
  fresh work; the SQL tracker receives MAX_SQL_READ_CURSORS + 1 actual native
  SQLite cursors in a read scope, closes them all, and a later read succeeds.
- Existing source bindings include socket/child integration tests. Their names
  are evidence bindings, **not claims that they were all executed here**.

## Additional real release paths and controls

`tests.test_s15_release_paths` adds **18 release cells**, each run normally and
with exactly its own reached release suppressed once. The same ordinary row
oracle must raise AssertionError under the mutation; all other release calls
run normally. Each normal row replays and preserves its first full hold snapshot.

| Acquisition / release driver | Cells / individually suppressed release |
| --- | --- |
| Real context lease reserve; unsent withdrawal, armed-unsent withdrawal, unarmed expiry, successor `release_earlier_unarmed_leases` | 8: lease `withdrawn_unsent` and companion custody `lost_with_reservation` separately for each exit |
| Real memory registration/preparation; `memory.release` | 4: driver entry `retired`; preparation `retired`, `submit_refused`, `never_launch_local` |
| Real orchestration open; explicit primary close or due unattended close | 2: orchestration `closed`, `interrupted_unattended` |
| Real account denial/closure call; `settle_denied_result_lease` | 1: lease `denied` |
| Real reserve/arm/bind | 1: lease `bound`, reservation remains held, charge stays one |
| Real bind/publish and retention expiry | 1: custody `retention`, body/context files removed, live counts baseline |
| Real memory registration, sealed lease reserve/arm, recorded open binding and `memory.hand_over` | 1: driver entry `handed_over`; lease/open-binding obligations stay held and transfer to sweep |

Together with the unchanged settlement and recovery modules this is **34 green
store-level matrix cells and 24 independently disabled release controls**
(8 settlement cells + 8 memory-recovery cells + 18 new cells; previous 6 controls
+ 18 new). The separate B4s reservation recovery and lease churn witnesses are
also green. These counts do **not** represent the complete failure-exit matrix.
No new product defect was found by the completed rows.

Still open: full account run-slot/binding/probe exits and mutations; lease fenced
and full driver refusal/dispatch/restart exits; normal reservation account ACK;
open-binding withdrawn/account-final paths; custody orchestration-close; host
worker/result/monitor/execution/probe-worker cleanup; all required boot, clock,
collector and service boundary combinations. Container-only rows remain assigned
to **S27a/S27b/S27c/S27d** in the retained table below. Do not convert them into
passing evidence or executable skips.

## Verification / registration

New modules and IDs are registered in test-layers, test-stages, test-timings,
tests/requirements/s15.json and test-requirements. The final nine named-module
runner report (2.958 s elapsed, 5.841 worker-s) is recorded
in `finish-module-results.json`; metadata_errors and
budget_failures are empty. Its only failure is the unchanged strict guard.
No full/affected suite was run. Every new module is well below 60 seconds.

Additional socket-blocked direct named batch: project_monitor,
signature_input_wire, sql_read_budget, project_cleanup, project_orchestration,
cli_reports, execution_activity, rate_limit_contracts: **153 tests, 1.318 s, OK**.
The SQL module subsequently gained one new test and was rerun in the final runner.
New release paths direct final precursor: 2 tests / 1.173 s, OK (18 subtest rows
per test). Session cap and queue direct modules also passed before registration.

One initial mixed runner mistakenly selected test_open_request_lifetime, whose
module includes DriverHarness service/socket tests. It was interrupted and is
**not completion evidence**; its record reports runner interrupted. Later direct
batches install a socket-denying audit hook, and the scenario release-path module
installs the same denial before fixture setup. The release fixture uses git and
signature subprocesses, hence its honest scenario layer. A first component-layer
attempt refused git during denial-fixture setup; that was a fixture-layer error,
not a product failure. No original assertion was changed to address it.
No run is left in flight at handoff.

Primary socket modules (not proved by this finish pass): test_hold_lifecycle,
test_host_slots, test_open_request_lifetime, test_cleanup_receipt_lifetime,
test_project_driver, test_project_request_queue, test_project_ingress,
test_control_loop. Also execute the remaining bound source modules from the audit
where this pass records only source review. The B0-2 skip must remain intact.

---

# S15 status — P3 fixed (2026-11-26); matrix and strict guard still open

## P3 fix (lease x failed_before_launch past the verification limit)

- Root cause: `ProjectHistory._result_counts()` verified EVERY `result_leases` row (live or finished) and
  refused `history_result_capacity` past `MAX_LEASE_ROWS`. Admission checks before its insert, so it admitted
  row MAX_LEASE_ROWS + 1; finished (withdrawn-unsent) rows stay until the purge (closed orchestration +
  deadline + 1 h grace), so every later capacity read and admission failed until then.
- Requirement: 0.3.4 R1d (DESIGN-0.3.4-result-capacity.md:39-43: the lease-row check "exists to bound the
  work of verifying every lease, not to limit results"; live leases count against the 64 live entries) and
  the decided B6 pattern RS-B-B6-2 / decision 210 (docs/requirements/b6.md:119-123: held leases found by
  verified identities, "bounded by the live leases (MAX_LEASE_ROWS), not by retained history"). No conflict.
- Fix (project_history.py `_result_counts`): verify only live leases: rows in state reserved/claim_pending
  (lease_expiry index) UNION rows named by held lease register rows (project_holds_kind_due), each set bounded
  by MAX_LEASE_ROWS, each row verified by its seal. A live lease relabelled finished keeps its held register
  row, so its seal still refuses it (`history_incompatible`); a held register row with no lease row refuses.
- Tests red -> green: `tests.test_s15_lease_churn` (S15-O0C-P3 witness, unchanged) and new
  `tests.test_result_leases...test_retained_finished_leases_never_bound_live_capacity_reads_or_admission`
  (4 finished rows at limit 2, a 5th admitted and charged, relabel refused).
- Registry: lifetime_caps.json `project_history.py::_result_counts` checks 2 -> 3, ends text updated
  (assertions.md). Strict guard unchanged at 361 missing `test` bindings (pre-existing S15 work).
- Named modules (parallel_tests, jobs 2), metadata_errors [], budget_failures []: batch 1 (s15 x4, guard,
  result_leases, result_persistence, project_history, project_results, hold_lifecycle, b6_unattended_close,
  deadlines_retention) 26.9 s; batch 2 (result_lifetime, history_service_upgrade, record_context,
  open_request_lifetime, project_orchestration, b7b_recovery, b7b_terminal, refusal_steps, holds_report,
  retirement_sweep, rsd_runtime_close, publication_replay) 40.2 s. Only failures: the known strict-guard red
  (test_lifetime_cap_guard and test_holds_report B7_3, both the 361 missing bindings, same as base).
- Runtime file changed: the primary re-records the runtime digest / bumps the version at batch time.

## Earlier stop record (historical)


2026-11-26, branch `feat/s15-fault-matrix`, base `88822538`.
Changes remain uncommitted as requested. No product code changed and no sockets
were opened. Read builder rules, original brief, and owner decisions 132/377.

## New product defect: lease × failed_before_launch, repeated past the cap

`tests.test_s15_lease_churn.LeaseChurnTests.test_failed_before_launch_past_lease_verification_limit_keeps_live_counts_readable`
is **RED**, reproducibly on lifecycle **3** with only `MAX_LEASE_ROWS` reduced
from 4096 to 2 (the small-limit R8 test pattern). Every iteration uses the real
`reserve_result_lease` and `release_unsent_result_lease` writers; identical
release replay succeeds. Before the failing read, all three leases are expired
and all three lease holds are released `withdrawn_unsent`. There are no SQL
writes or invented release receipts in this reproduction.

The third lease is accepted while the historical-row scan sees exactly two
rows. Its release succeeds, but `_result_counts()` subsequently raises
`history_result_capacity` at `project_history.py:1675` because the scan counts
three retained source rows, including finished leases. Thus a valid accepted
lifecycle makes live capacity unreadable even after its local hold has ended.
This is distinct from the intentional retention of tombstones and from refusing
new work *before* a verification budget is exceeded. Production uses the same
comparison at 4096; the production-size case was not run. No claim of a permanent
leak or of failure after the ordinary purge deadline is made.

**STOP per brief/user instruction: no product fix.** Primary/owner must resolve
this row before S15 continues. The strict guard and complete matrix remain open.

## Requirement evidence at stop

| ID | Evidence and limits |
| --- | --- |
| S15-B0-2 / B4s / B0-6 | GREEN: `test_successor_settlement_releases_once_frees_capacity_and_writes_no_terminal`. Real reserve/arm/bind, store reopening, arbitration/outbox, authenticated closure, receipt replay, unchanged settlement snapshot, one settlement row, ordinary expiry, fresh admission. No terminal/outbox/consumption/cleanup receipt/record added. Original B0-2 service assertion remains skipped under 132/377. |
| S15-O0C-GUARD | Strict nonempty `ends`/`test` and test-name existence now apply to **every registry entry**, including stale sites; missing/malformed-field controls pass. Registry guard is RED: 553 entries, **361 still lack `test`** (362 at resume; P3 now registered). Existing 192 bindings have not all been audited for actual past-cap execution. No blanket/fake bindings added. |
| S15-O0C-P3 | RED: exact failed-before-launch churn test above; registered against `project_history.py::_result_counts`. |
| S15-RS8-SETTLEMENT | GREEN for 8 store-boundary cells: unbound lease vs bound reservation × stop before settlement, cut before release, cut after real release inside transaction, lost reply after committed transaction. Companion open-binding/result-custody releases checked, rollback is byte/row snapshot exact, replay survives reopen, no terminal/records, live capacity returns to baseline. Not a full driver/account lifecycle claim. |
| S15-RS8-MEMORY | GREEN for real registration/preparation, retained unlocked instance-inode proof, live-lock control, wall rollback, lost registration reply, cuts before/after real recovery release, successor/replay and unattended orchestration release. The fixture label `before_registration` observes absence before registering normally; it does **not** prove a crash before registration. |
| S15-RS8-MUTATION | GREEN: six individually disabled releases are reached once and their ordinary row oracle fails: lease lost-unbound, reservation lost, open-binding claimed, result-custody lost-with-reservation, driver-entry service-stopped, preparation service-stopped. Other release paths remain unproved. |

## Explicit unfinished matrix work

The passing cells above are **partial coverage**, not the full hold-kind ×
failure-exit cross product. Required axes retained from B0-6 and RS-8:

- Exits: completed, refused open, refused claim, failure before launch, cancel,
  no result, lost/duplicate dispatch reply, service restart mid-run, collector
  restart, probe interrupted by restart; cuts before/after registration, local
  effect, launch, acknowledgement and release; restart without primary; boot
  change; wall forward/back.
- Kinds: account run slot, binding and probe; project lease, reservation,
  open binding, result custody, driver entry, preparation and orchestration;
  host worker/result/monitor/execution entries and probe worker. Each cell needs
  its real acquisition/release driver or explicit evidence that no such hold is
  acquired at that boundary; absence is not an executable skip.
- Remaining mutation controls include normal account/project release, denied and
  fenced leases, unsent withdrawal, open withdrawal/account-final, custody
  retention/orchestration-close, memory retirement/handover, unattended close,
  host worker/result/monitor/preparation cleanup and noncontainer probe paths.
- Remaining guard work: real end descriptions and actual past-limit evidence
  for every entry; a name-existence check alone does not establish execution.

## Explicit container carry-ins (not passing evidence or executable skips)

| Step | Deferred matrix rows and required outcomes |
| --- | --- |
| S27a / RS-H1 | Run slot × dead container with result → completed; run slot × dead container without result → lost; run slot × alive after deadline → alert; launch confirmation retains container identity. |
| S27b / C5 | Probe worker and admitted probe row × restart before launch → `probe_unlaunched_restart`; both × lost launched probe container → `probe_lost`. |
| S27c / B5b | Reservation and run slot × crash with container and cgroup proven gone → crashed settlement; both × absent proof or failed stop → retained `stop_failed` hold. |
| S27d / O0C-U4-3 | Probe capacity × dead probe/reaper replay → exactly one slot release, capacity bounded by probe journal. |

## Registration and final named-module run

New modules are registered in `test-layers`, `test-stages`, `test-timings`;
IDs/statements/checks in `tests/requirements/s15.json` and `test-requirements`.
Restored deferred B0-2 `test-checks` entry with 132/377 reason. No requirement
removed. Assertion history is in `assertions.md`.

```sh
PY=/private/tmp/claude-501/-<user>-Code-temp-orchestrator-child-liveness/bbfe95e0-9740-406f-9285-5f684a8bf0f1/scratchpad/tfvenv/bin/python
TMPDIR=/tmp/claude-501/s15 "$PY" -B scripts/parallel_tests.py \
  --module codex:tests.test_s15_reservation_recovery \
  --module codex:tests.test_s15_settlement_matrix \
  --module codex:tests.test_s15_memory_matrix \
  --module codex:tests.test_s15_lease_churn \
  --module codex:tests.test_lifetime_cap_guard --jobs 1 --keep
```

Final exit **1**, intentionally RED: **`metadata_errors: []`,
`budget_failures: []`**, 11 tests, 5 modules; 9 pass, 2 fail.
Workers 4.528 s total / 4.5 s elapsed (preflight separately 7.793 s;
discovery 2.431 s). Rounded worker times: settlement matrix 1.4 s,
memory matrix 1.2 s, guard 0.8 s, recovery 0.6 s, churn 0.6 s.
Each new module is well below 60 s. Durable result: `named-module-results.json`.
No full-suite, affected-suite, socket, or measurement run; none left running.

Direct preliminary runs: recovery 1 pass / 0.034 s; settlement matrix 2 pass /
0.457 s; memory matrix final 2 pass / 0.310 s; strict guard 5 tests / 1.001 s,
1 fail (362 missing bindings before P3 registration). A draft memory oracle
used a time below the stored due boundary after rolling the clock back; it was
corrected to `due_at_ms + 1`, with release assertions unchanged. Initial churn
discovery accidentally included imported ResultLeaseTests: 24 tests / 2.282 s,
only the P3 test failed; `load_tests` now restricts discovery to its own case.
Two initial direct invocations had import-path errors and ran no tests.
The first final runner attempt had catalogue-generator lifecycle metadata
churn; unrelated generated changes were discarded and the command above then
proved empty metadata errors. No unrelated catalogue edits remain.

Primary socket modules, **not run here**: `tests.test_hold_lifecycle` (B0-2 stays
skipped), `tests.test_host_slots`. They are later integration evidence, not a
substitute for resolving P3 or completing the remaining matrix/guard work.
