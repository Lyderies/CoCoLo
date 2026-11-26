# S15 status — STOP: P3 lease verification overflow

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
