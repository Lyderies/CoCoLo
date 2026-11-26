# S5 B4t build status — Final fix

## 0.8.69 install refusal (decision 372, plan step 1) — read first

Branch `fix/0869-takeover-proof`, base `77bca2e5` (0.8.69 candidate). The owner's `./setup.sh --update` refused with
`predecessor_unproven` ("running 0 child runs ... When that run ends") against the installed, idle 0.8.68 service.

- **Root cause (A1.4 I3, the entry of the argument check).** The installed service runs from its protected launch copy:
  `ps` shows `Python -I -B -S -X pycache_prefix=/dev/null ~/.codex/agent-orchestration/cdccdfce300a/28435b17.../.codex/
  orchestration/project_host.py --state-root ~/.local/state/codex-agents --project-root <project>`. Peer pid, uid,
  owner-only directory, interpreter flags, state and project roots all match. `read_plan` tried the candidate trees in
  order (project root, then the recorded `launch_copy`) and stopped at the FIRST trusted tree: the project root, also
  0.8.68 with the same digest. Its entry is `<project>/.codex/orchestration/project_host.py`, so `_trusted` failed on
  the entry path, and because 0.8.68 has `service.stop_for_update` (`stop_op`) the failure raised
  `predecessor_unproven` even with 0 children; the launch copy was never tried. A1.4 I3 names the entry as "the
  H2a-verified install or launch copy", so the copy is in A1. The S5 native witnesses launched their archived service
  from the project tree, never from a launch copy, so they could not see it.
- **Why idle did not take the signal-free path.** The `hint` fallback (A1.5 row 4) is only for pre-B4t services;
  for a `stop_op` service any I3 failure was a refusal (row 7 keeps I3 "as built"). Row 7 is unchanged: with the
  correct entry the idle 0.8.68 service is stopped by its own `service.stop_for_update`, no signal.
- **Fix.** `takeover_preflight.read_plan`: a live candidate whose identity check fails moves on to the next trusted
  tree; only when none matches does the old outcome apply to the first trusted tree (busy or `stop_op`: refuse
  write-free; idle pre-B4t: `hint`). No new process is read: the same one candidate pid, the same I1-I4.
  `update_takeover.TakeoverError` gains `pid`; an idle (0 runs) `predecessor_unproven` no longer says "when that run
  ends" and names `ps -o pid,lstart,command -p <pid>` and `kill <pid>` as the step (A1_IDLE_UNPROVEN). The busy text
  (A1.9) is unchanged. docs/troubleshooting.md "Update hand-over" says so.
- **Tests.** New `tests.test_b4t_native_p68` (vendored `tests/fixtures/b4t/0.8.68-native-package.tar.gz`, sha256
  `e05a6860...b765`): real 0.8.68 collector whose starter launches the recorded copy; red output in
  [evidence/0869-refusal/red.txt](evidence/0869-refusal/red.txt) (the owner's exact sentence), green after the fix.
  No existing assertion changed. Runtime files changed, so the runtime digest moves: VERSION and digests are left for
  the primary (no bump here).

Branch `feat/b4t-takeover`, review head `44f16529` / product `146955fc`, base
`ce81bd96` (0.8.67). Accepted Amendment A1, including A1.15 (decision 337), is
the authority for this one fix round. Changes are left in the worktree: no
commit, install, live-service mutation, VERSION bump or catalogue edit.

**Native acceptance remains open; do not install this worktree.** The focused
product/archive/kernel tests are green, including the real candidate-only Mac
pin and pipe-only exec residual. The new socket acceptance module is explicitly
skipped here. The exact reruns and the two failed native fixture attempts are
listed in [fix-round-rerun.md](fix-round-rerun.md). A skipped native check is not
a passing requirement or evidence that its fixture is correct.

## Regression-only fix (S5, gate exception) — read first

Head before: `84890ba5`, a WIP that was interrupted. Every hunk was reviewed and all were kept: a pin hunk adds
only a design op, and the product hunks are listed below. It fixes the 22 regressions in 13 modules listed in
[evidence/final-fix/affected.txt](evidence/final-fix/affected.txt). Pairs are in
[assertions.md](assertions.md), "S5 regression-only fix".

- **Op-set pins** (test_b2_service_outcome, test_account_host, test_holds_report_socket/test_b7b_terminal,
  test_admission_inputs, test_signature_inputs, test_signature_job_intake): the pins gain only
  `history.takeover_report`, `account.takeover_report` and `service.stop_for_update` (D2.8, D2.9, H4b,
  A1.5 row 7).
- **Downgraded-schema fixtures** (test_cleanup_receipt_lifetime P1_4 x3, test_rsf_s7_account x3): this was a
  product defect, and the product is fixed. `_migrate` refused any pre-18 ledger that carried
  `runtime_floor`/`runtime_lineage` ("predecessor ledger schema is incompatible"). Now such a ledger upgrades
  as on 0.8.67 (owner 129.3) when both tables have the exact B4t layout. They are kept, not re-created, and the
  floor still binds through `raise_floor` (`check_floor`). A wrong layout is still refused.
- **test_cli_status_client_boundary** (4 cases): learning-status always made an `account.takeover_report`
  exchange, even when no project report existed for it to complete (`takeover_lines` uses the account pages only
  with a project report). It now reads them only then, so the status account exchange is the same as on ce81bd96.
- **test_updfix UPDFIX_2** (4 cases): the product's behaviour is the design's. SW-3/B4t-0 removed the
  `child_run_active` refusal, so the pin of `refused` follows that. Under A1.5 row 10, the fixture's own open
  ledger is a held `collector.lock` that does not answer, so the "no collector runs here" case now closes it.
  Red with the unchanged test: [evidence/regression-fix/updfix-red-unchanged-test.txt](evidence/regression-fix/updfix-red-unchanged-test.txt).
- **test_host_slots M1** (product defect, red in isolation: [m1-red-before-fix.txt](evidence/regression-fix/m1-red-before-fix.txt)).
  The new `account_takeover_report_client` is learning-status's last call, and it left the process-wide
  `client` enrollment for the account endpoint installed. `service_transport._preamble` takes the first matching
  owner. So the in-process host's lost-run settlement then went out as role `account` with the terminal's
  label, the account refused it (`account_not_authorized`), and the slot was never freed. On ce81bd96,
  learning-status's last enrollment was history-only. Fix: that read ends its enrollment (`finally:
  install_enrollment(None)`). M1 is green.
- **AP3 (resolved, decision 348, `eddd2d00`).** `test_candidate_evidence_lookup` AP3 is re-aimed, not loosened: it
  compares against the newest previous release with the SAME ledger schema. While none exists (0.8.68 is the first
  schema-18 build) it asserts that the current schema is newer than every released one and that released 0.8.61
  refuses the store with zero writes (the T-5 behaviour), never a skip. Old -> new is mapped in
  [assertions.md](assertions.md). Before that change the test opened the current ledger with 0.8.61 and failed.

Affected run after the regression fix and the AP3 re-aim:
[evidence/regression-fix/affected.txt](evidence/regression-fix/affected.txt) (a full-fallback `--affected` run, no
coverage map). The earlier named run was 85 s with `metadata_errors: []` and no budget failures:
[evidence/regression-fix/named-run.txt](evidence/regression-fix/named-run.txt). The primary also ran the affected
set, so "no affected run exists" no longer holds.

## Final fix (S5, last round) — read first

Head before this round: `094f73d1` (fix round `ff35ecdd` plus the primary's native-run evidence). Scope: exactly
the three items of the primary's final brief. VERSION and the catalogue are unchanged.

**Root cause of "archived host did not serve: None".** The archived 0.8.66 host did serve (hint, sockets and
locks were on disk). The fixture polled `_hinted_host(self.state, …)` with a `LearningState`, and
`runtime_hint_path` accepts only the exact `CollectorPaths` type, so every read was `dispatch_unavailable`.
Fixing the fixture exposed the same mistake in the product (below), then three more fixture faults.

**Product defects found and fixed (each red on the real archived host first, `evidence/final-fix/red-product-hint-type.txt`):**
1. `takeover_preflight.read_plan`, `OwnerOperations.withdraw_hint` and the T4 straggler re-read passed the owner
   command's `LearningState` to the hint readers. Every hint read failed, so a live pre-B4t service was never seen:
   T0 waited 5 s on its held locks and refused every such update as `project_store_busy` (B4t-0/-1 blocked
   outright; A1 rows 3-6 unreachable). New `takeover_preflight.client_paths(state)` hands them `CollectorPaths`.
2. `run_owner_takeover` opened `collector-runtime.lock` without `O_CREAT`, so a state root whose collector was
   never auto-started crashed with `FileNotFoundError` instead of taking a free lock. It now creates it 0600 as
   every other taker does.
3. No behaviour change: the T3 self-stop wait (A1.5 rows 3/4) is the named constant
   `takeover_owner.SERVICE_SELF_STOP_SECONDS = 30.0`, pinned by the stub tests.

**Fixture faults fixed (no assertion loosened, no new skip; pairs in [assertion-audit.md](assertion-audit.md)):**
the `CollectorPaths` type in the native tests; the P66 audit stopped its World service through
`pin_predecessor`, which rightly refuses the World's stand-in interpreter launch line (now: exact peer pid after
an argument check); the audited service ran on the real interpreter and so the real account home (now the
World's stand-in); the V34 audit used 0.8.6x wizard answers (now the L12.1 `set_up_release` path, with V34's
Claude tool set in the fake init frame).

**E2c-7 verdict: the fixture (its oracle) was wrong, the product is right.** D2.8's RS-5 line is
"recovery: needs update hand-over: <B4m command>", and B4m gives the local command whenever an install binding
exists. Both tests record the launch of a bound copy (`install_copy` binds the project as its own clone), so the
correct literal is the command form `cd <root> && ./setup.sh <root>`, not the "record unavailable" guidance. The
OtherVersion fixture also deleted a ledger this build had opened (leaving the `initialized` marker, which every
build refuses); the earlier runtime now originates the ledger, as before a real update, and no floor is lowered.

**Budget.** `test_b4t_native` was 372 s. Each stub case lost 30 s to the self-stop wait of a stub that never stops
by itself (now 1 s in those cases only, value pinned) and the module held two archived Worlds. Split, all
registered (scenario/integration, timings, RS-B-B4t-3/-9 checks), 60 s ceiling unchanged:
`test_b4t_native` (5 tests, ~19 s), `test_b4t_native_kill` (TERM-ignoring stub, real 30 s TERM bound, now
asserted ≥ 29.9 s, ~35 s), `test_b4t_native_audit_p66` (~39-43 s), `test_b4t_native_audit_v34` (~25-28 s).

Two more faults found by the affected run and fixed here: the C stub closed each accepted connection at once,
so macOS `LOCAL_PEERPID` sometimes reported no peer and the plan fell back to `hint` under load (reproduced in
isolation; the stub now holds connections as a real service does); and the L12.5 e2e fault rewrite named the
replaced `runtime_hint_path(self.state, …)` line (now `paths`).

**Runs (unsandboxed, short TMPDIR; [run1](evidence/final-fix/run1-named.txt), [run2](evidence/final-fix/run2-named.txt), after every fix).**
Named modules (the brief's four plus the three split modules), twice: 239 tests, exit 0, `metadata_errors: []`,
no budget failures, no failed units or requirement failures; all eight native A1 tests, both E2c-7 cases, E2a-1
and the wizard wording passed (150 s and 160 s). Only `test_project_start` and `test_setup_wizard_direct` are over
60 s, both under their existing budget-debt ceilings.

**Affected run ([evidence/final-fix/affected.txt](evidence/final-fix/affected.txt)): NOT clean.** No coverage map
here, so it ran in full: 7906 tests, 1366 s, `metadata_errors: []`. Besides the expected `test_runtime_release`
and the two faults above (fixed, reruns green), 22 tests in 13 modules fail. Each fails identically on 094f73d1
and passes on ce81bd96: regressions of the earlier B4t rounds, outside this round's brief, left for the primary.
They are op-set pins missing `history.takeover_report` / `account.takeover_report` (and the captured family
sets), downgraded-schema fixtures now refused as `predecessor ledger schema is incompatible`, and
`test_cli_status_client_boundary`, `test_updfix` UPDFIX_2 and `test_host_slots` M1. The four full-run budget
failures are load-induced (named reruns within debt; ce81bd96 itself fails one).

## Fix round: finding → test → evidence

| Finding / requirement | Product witness and result | Evidence / remaining limit |
|---|---|---|
| HIGH-1 / A1.4–A1.9 / B4t-0,3,9 | `AmendmentA1Tests.test_B4t_A1_no_other_process_is_inspected`; idle EPERM T-37, busy T-38, held-lock T-39, hint T-40; `NativeProcessProofTests.test_B4t_A1_native_candidate_pin_reads_no_other_process_and_stops_only_it` | Untouched 146955fc behavior refuses the idle EPERM fixture (`a1-red.txt`); candidate-only native kernel pin now passes. Genuine P66 socket T-36/37 remains for primary. No process census or descriptor read remains. |
| HIGH-2 / L11.2 | `RegistryTests.test_failed_handover_rejects_added_business_rows_and_changed_schema` | `high2-red.txt`: invented `outcome:2` passed old helper. Green now: exact business rows, epoch key set, only four new authority-table key prefixes allowed, separately pinned 22→23/23 schema. Full L11 native flow unrun. |
| MED-1 / T-4,12b,15,17,30 / B4t-2,3,4 | Real archived publication/current authority; real owner stop calls; bound/unbound/pending open paths; real P adoption/Q claim/child; actual killed predecessor with missing inode | Five named mutations all red, unmutated witnesses green (`mutations.txt`, replay script alongside it). No fake result/other-child constants remain. Full native lifecycle matrix remains owed. |
| MED-2 / registration consent | `test_registration_without_owner_consent_never_enables_records_upgrade` | Original `allow_schema_upgrade=True` behavior red in `consent-red.txt`; final call enables it only when a whole-ledger copy/consented `behind` exists. Additive 16/17 owner behavior retained. |
| MED-3 / A1.10 / T-41 | `RetentionA1Tests.test_B4t_A1_takeover_tables_have_retention_and_caps`; refused-obligation rollback; 65-branch/18-link protection | `retention-red.txt` before new owner purges; green 50-row atomic backlog drain to 17, protected rows kept, obligations and orphan run annotations drain. Owner-open/periodic paths use the persistence owners and independent post-adoption transactions. Cap guard green. Native 17/50-update and all-settlement campaigns remain acceptance work. |
| MED-4 / A1.11 / T-42,44 | `test_B4t_A1_journal_stays_bounded_on_a_busy_project`; maximum path/fields test; `StoreTests.test_B4t_A1_overflow_count_survives_the_journal` | Old J4a oversized failure red (`a1-red.txt`). Green: 1,000 tombstones + 70 unsettled → 64/6; all path encodings ≤4095 bytes round-trip; maximum inputs retain at least 54 runs; final counts survive actual adoption, J14 journal removal and annotation purge. Widest tested fixed form is 8,014 bytes; the complete serialized byte rule is authoritative. Full native crash cuts still owed. |
| MED-5 / shallow CI | `RegistryTests.test_predecessor_archive_needs_no_git_history_and_checks_its_digest` | `vendor-red.txt` → green with all Git/subprocess calls forbidden; vendored unmodified P66 archive and digest checked before extraction. |
| MED-6 / audit | Literal expected strings in both E2c-7 cases; complete round pairs and omitted fixture/target changes | [assertion-audit.md](assertion-audit.md) and [assertions.md](assertions.md). Neither expected string calls `compatibility_message`. Existing base business-row/schema checks remain exact. |
| HIGH-P1 / owner 129.3 | `test_released_0859_owner_upgrade_preserves_project_local_enumeration`; copier/backup regressions | Released 0.8.59 source creates a real ledger; current owner upgrades to 18. Project-local table enumeration stays exactly project-scoped; authority shapes move to the separately checked extra-table set. Standalone owner backup shape equality caught the remaining script copy and is green after its product fix. Full E2a_1 socket case unrun. |
| MED-P2 | `OwnerIntegrationTests.test_records_upgrade_approval_keeps_the_other_projects_warning_literal` | Green real-Wizard body includes “until you run this setup for each of them”. Full root PTY case unrun. |
| MED-P3 | Earlier runtime's own owner now originates E2c-7 fixture ledger before its collector starts | All host/no-spawn/collector assertions retained, literal oracle restored. No published floor is lowered. Exact socket case must be rerun; no green native result claimed. |
| LOW-1..4 / I4 / T-19 | R10 docstring corrected; exit-at-I4 races count as exited; all five modules guarded; Linux census removed | `exit-race-red.txt` → green. Native exec test caught NOTE_EXEC incorrectly ending the exit wait: corrected and green. Unknown custody remains held by design. |
| A1.7 item 7 / T-43 | `NativeExecA1Tests` real exec-before/after-I4 and archived process-boundary static checks | Both green. P66 uses `create_child`; the actual V34 archive uses `subprocess.Popen` for the separate signature worker. Both exec sites are explicitly bounded by their process entries; this is not an import-closure claim. Per-service-pid dynamic audit tests are written in `test_b4t_native` and unrun. |

## Fix round: per-requirement evidence

All names below are in `tests.test_b4t_takeover`; full native acceptance is
separate. Initial red evidence and the previous build's unchanged cases remain
in the historical section below.

| ID | Passing named witness in this round |
|---|---|
| RS-B-B4t-0 | `AmendmentA1Tests.test_B4t_A1_idle_predecessor_updates_without_process_proof`; busy refusal snapshot; exact registration consent; released 0.8.59 offline upgrade |
| RS-B-B4t-1 | `JournalTests.test_B4t_0_update_with_a_running_child_goes_ahead`; real owner collector-stop protocol witnesses |
| RS-B-B4t-2 | `StoreTests.test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store`; retained committed-result/revocation tests |
| RS-B-B4t-3 | Candidate-only native pin, real inherited flock holders, I4 exit races, real exec-before/after-last-check; stop-op and collector guards |
| RS-B-B4t-4 | `StoreTests.test_B4t_4_open_bindings_end_by_their_own_rules`; `AccountLineageTests.test_B4t_4_another_projects_child_keeps_running`; `MemoryTakeoverTests.test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds` |
| RS-B-B4t-5 | Existing real archived original-custody settlement/chained outbox replay tests; lineage retention past 64/16 verification bounds |
| RS-B-B4t-6 | `StoreTests.test_B4t_6_primary_recheck_idempotent_across_takeover` |
| RS-B-B4t-7 | Existing B4m recovery/other-project command witnesses; literal existing E2c socket oracles await primary |
| RS-B-B4t-8 | T-42/T-44 durable counts, real account/history producers, over-cap line, increasing/bounded page loop |
| RS-B-B4t-9 | Candidate proof negatives; busy write-free snapshot; strict row/schema negatives; original custody/corruption and current-authority cases |
| RS-B-B4t-10 | Retention rollback, widest journal fields, final-store overflow persistence, existing phase/archive migration cuts and U1/chained replay |

## Fix round: named runs and remaining gate

- Red-first focused outputs: `a1-red.txt`, `high2-red.txt`, `vendor-red.txt`,
  `retention-red.txt`, `idle-proven-red.txt`, `width-red.txt`,
  `later-width-red.txt`, `ascii-width-red.txt`, `endpoint-red.txt`, `exit-race-red.txt`.
  `consent-red.txt` explicitly replays the original registration behavior after
  correcting an initial test harness setup error; it is not presented as the
  first harness attempt succeeding at its assertion.
- Copier batch: 215 tests in 41.6 s, metadata clean; one standalone-script
  authority-definition mismatch. The existing equality assertion was retained;
  after the product script fix, its full named module passed in the 25.8 s
  owner/orchestration/release batch. The only other failures in that batch were
  the expected release digest and a subsequently corrected cap registry count.
- Green registered B4t/kernel/cap/harness checkpoint: 181 reported tests in
  23.9 s, `metadata_errors: []`, no failed units or requirement failures; eight
  native socket tests explicitly skipped. The final delivery run is recorded
  in `evidence/fix-round/final-proof-named.txt`: 183 reported tests, eight explicit native skips, no failures, 22.6 s; real nonempty history/count and optional account-lineage reply producers are included. The final strengthened exec-exit/record-completion checks plus cap guard pass 7/7 in 6.715 s (`final-focused.txt`).
- Offline learning-migration classes: 15/15 pass in 4.871 s; the two CLI/socket cases are explicitly listed for primary.
- Five MED-1 product mutations all detected; native exec and sole-candidate
  kernel checks run with pipes only. No full suite or `--affected` run used.
- Every new table, count, byte limit, page loop and stop/lock/probe wait is in
  `lifetime_caps.json`, with a past-limit witness. Protected lineage has no
  fixed/per-digest cap; its lifetime is the oldest live/replayable custody.
- All changed existing assertions and expected-value/fixture changes are paired
  in both audit documents. VERSION/catalogue are unchanged; the release-digest
  mismatch remains expected until primary release bookkeeping.
- [Exact socket/PTY/native reruns](fix-round-rerun.md); no acceptance credit
  for skipped checks. Native owner-command SIGKILL/start-path and loaded-service
  gates remain as described there. No user approval is requested.

[Changed files](files.md). All builder-launched test sessions are reaped before
handoff; no account/project service was touched outside disposable fixtures.

---

## Historical S5 build evidence before the fix round

The text below records the reviewed build's results, including the obsolete
sole-holder failure. Current implementation/evidence is above; the old census
is intentionally removed by accepted A1.


Base `ce81bd96` (0.8.67), branch `feat/b4t-takeover`. Scope: accepted Draft 2,
its fix round and final fix (decision 326), S5/SW-3 and decisions 129.2 A, 261,
272 and 323. VERSION and the runtime digest catalogue are unchanged. No commit,
install, live-service mutation or full-suite run was attempted.

Implementation and named-module work are present. **S5 is not through its
per-step gate and this worktree must not be installed.** Native macOS/Linux
lifecycle acceptance, other-provider review, security review and the primary's
batch gates remain. The source is reviewable here; the builder cannot supply
those external gates from this sandbox.

## Ordered work

1. Read the accepted requirements and builder rules. Added the initial 43-method
   T-1–T-35 scaffold and its scenario/integration/requirement registrations before
   changing product code. Its named run against the untouched product was red:
   2 failures and 55 error reports, including subtests, in 2.7 seconds;
   `metadata_errors: []`. Existing start-barrier guards were already green.
   These initial component tests were not complete native acceptance witnesses.
2. Built the bounded, owner-only atomic takeover journal and phase coordinator,
   including exact authorization undo before J5, truthful post-intent failures,
   J4a and J6a entry reconciliation and crash/resume postconditions.
3. Reused the existing quiesce start gate; added authenticated collector shutdown,
   owner-enrolled service stop, verified legacy process pinning, bounded sole-lock
   ownership proof, one TERM and same-pin escalation, and bounded straggler retries.
4. Added read-only successor custody at the six approved account functions. The
   four `earlier_runtime=` call sites are pinned by an AST witness. Admission,
   storage evidence, execution and provenance retain their strict runtime gates.
5. Added ledger 18/project 23 authority tables and floor checks under S2/S3.
   Pending old 17/22 migrations finish their original target before stage B.
   Genuine archived SIGKILL cuts exercise both stages, frozen clocks, preserved
   before-copies, missing-copy refusal and uninterrupted-result equivalence.
6. Adopted immutable original project custody and outbox obligations; kept open
   and paused orchestrations. Replaced the interim runtime-only close assertions.
   Current recording still requires its own takeover evidence and current host
   class; revocation during body I/O rolls the real handler's write back.
7. Wired the owner CLI and wizard before installer writes, moved records consent
   before stopping, added installed/complete checkpoints and reset-failed before
   systemd refresh. Only the running-child preflight refusal is removed.
8. Added the two new read operations and fixed outcome/phase wording, per-run
   lost annotations, B4m commands and troubleshooting text. Existing wire reply
   fields remain unchanged. The owner command reads other-project diagnostics
   after the bounded account-service start, without requiring a project service.
9. Strengthened the initial scaffolds with authentic archived claims/outboxes,
   A→B→C replay on both stores, memory-hold and removal recovery, and U1 cuts
   inside/after the old transaction. These stronger tests were added later;
   they must not be misreported as having all been committed red before code.
   Every subsequent product fix was preceded by its focused failing witness.
10. Ran the named copier/backup/move/enumeration and custody regressions below.
    Paired all 143 removed/changed existing assertion expressions in
    [assertion-audit.md](assertion-audit.md), with stimulus/fixture explanations
    in [assertions.md](assertions.md). The account target pins changed 17→18;
    original byte, receipt, rollback and manifest comparisons remain.

## Requirement evidence

All IDs have the initial red component run above. PASS below means the named
component/archive witness passed; it does not complete the native gate.

| ID | Passing named witness | Acceptance still owed |
|---|---|---|
| RS-B-B4t-0 | `JournalTests.test_B4t_0_update_with_a_running_child_goes_ahead`; `test_B4t_0_every_write_free_refusal_still_writes_nothing`; `OwnerIntegrationTests.test_update_check_replaces_only_the_running_child_refusal` | Full wizard, L12.3 and no-half-install native flow |
| RS-B-B4t-1 | `OwnerIntegrationTests.test_wizard_runs_the_takeover_after_approval_and_before_installer_apply`; collector-stop tests | Actual loaded collector/service stop durations and IP-B |
| RS-B-B4t-2 | `StoreTests.test_B4t_2_committed_results_stay_recordable`; `test_B4t_2_revoked_enrollment_refuses_recording`; own-takeover-authority negative | Live enrolled service/primary flow |
| RS-B-B4t-3 | `FloorTests.test_archived_0866_and_0832_cannot_reopen_either_published_store`; `ProcessProofTests`; `ServiceStopTests`; ledger/project straggler tests | Real kernel sole-holder witness FAILS safely here; native start-path matrix and legacy command exclusion |
| RS-B-B4t-4 | `StoreTests.test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once`; `MemoryTakeoverTests`; `RolloverTakeoverTests`; J4a/J6a tests | Real running P/Q children, orphan/deadline and complete service startup recovery |
| RS-B-B4t-5 | `ChainedOutboxTests.test_two_archived_outboxes_survive_A_B_C_with_one_effect_and_immutable_originals`; `StoreTests.test_B4t_5_no_double_record_in_any_order`; `AccountLineageTests` | Live transport retries and service crash at the native command boundary |
| RS-B-B4t-6 | `StoreTests.test_B4t_6_primary_recheck_idempotent_across_takeover` | Primary lifecycle recheck on both operating systems |
| RS-B-B4t-7 | `OwnerIntegrationTests.test_another_projects_unfinished_takeover_names_its_verified_B4m_command`; `test_recovery_handover_names_the_B4m_update_command`; other-project account reads | Installed-old-client lifecycle flow |
| RS-B-B4t-8 | `StoreTests.test_status_wording_uses_fixed_outcomes_and_other_project_commands`; `test_post_stop_count_includes_an_unsettled_orphan_but_excludes_a_result_committed_before_stop`; real new read producers; reply-shape compatibility | Full terminal/run-status/wizard native rendering and live producer soak |
| RS-B-B4t-9 | lineage/catalogue negatives; revoked-host real handler; tampered takeover-run and outbox read tests; unchanged-custody assertions | Native spoof/reuse races and both-platform review |
| RS-B-B4t-10 | `JournalTests.test_B4t_10_crash_at_each_phase_then_resume`; `MigrationCutsTests`; `RolloverTakeoverTests`; chained immutable replay | Whole owner command SIGKILL at every phase; authorization replace/directory-fsync cuts; native login restart/start races |

The journal phase tests use an operations port. They are not a substitute for
killing the real native owner command. T-6b, T-8's process census, T-9/T-10,
T-12/T-13 under real load, T-17 and the archived T-20–T-24 start matrix still
need that acceptance. T-15's archived row-preservation check is narrower than
the complete bound/unbound/in-flight open lifecycle matrix. The actual chained
fixture accepts B's second send and loses its closure reply; it verifies missing
project/account edges refuse without effects, but the alternative where that
send never reached the account still needs the native flow.

## Named runs and limits

No full run or `--affected` was used. Completed batches stayed below five minutes;
one extended account run was interrupted at 301.9 seconds as described below.
Module fragments below are selected by the normal named-module runner.

| Run | Result | Elapsed |
|---|---|---|
| First untouched-product B4t scaffold | 43 methods; expected red; metadata clean | 2.7 s |
| Boundary/API/U1/root checks before final diagnostics | 265 tests; B4t 99/100 pass; four history socket-listener failures; one metadata link subsequently fixed | B4t 32.122 s |
| Copier/migration run, 14 named modules / 17 fragments | 215/217 pass; two existing CLI migration failures | 86.7 s |
| Exact two CLI migration cases on untouched `ce81bd96` archive | Same two failures reproduce, same fixed product errors | 16.247 s |
| Custody/schema/runtime-close run | 342 tests; 327 pass; 11 additive target-pin failures, three account socket failures, one expected release-digest failure | 46.7 s |
| Target-pin corrections + cap guard + root login/e2e metadata | 131/131 pass; `metadata_errors: []`, `requirement_failures: []` | 15.6 s |
| Registered B4t + cap proof before last diagnostics | 103/104 pass (100 B4t + four cap tests); only native sole-holder proof refuses; `metadata_errors: []` | 22.6 s |
| Two new full wizard cases | Both blocked during fixture setup, before the wizard starts | 0.034 s |

The copier run covered `test_learning_source_image`, `test_learning_source_image_legacy`,
`test_learning_source_transfer`, `test_backup_installed_project`,
`test_account_observation_backup`, `test_large_project_backups`,
`test_owner_backup_streamed`, `test_backup_reply_private`, `test_learning_migration`,
`test_launch_receipts_layout`, `test_project_holds_schema`, `test_rsf_s2_fence`,
`test_rsf_s2_regressions` and `test_rsf_s3_resumable`. Additional account schema and
standalone restore coverage ran in the custody batch and target-pin rerun.
The runner reported existing per-module budget debt for large backups, owner
backup and S3; their thresholds were not changed.

Additional evidence after that checkpoint:

- Registered diagnostic/U1/S6/root run: 234/235 pass in 23.2 s; only the native
  sole-holder proof refuses; `metadata_errors: []`.
- Registered current-owner/lineage security run: 120/121 pass in 17.6 s; only
  that same native proof refuses; `metadata_errors: []`.
- Extended project schema/enumeration run: 397 tests, 128.7 s, metadata clean.
  It exposed additional target pins plus existing native listener failures.
- Extended account enumeration run: 404 tests completed across 14 fragments;
  the last billing fragment was still waiting for signing results. Interrupted
  at 301.9 s through its execution session; the runner's finally block reaped
  its owned worker group. Its interruption is recorded, not counted as a pass
  or as metadata validation.
- Focused post-fix schema/fixture/copy cases: 66/68 pass in 35.299 s. The two
  blocked cases are the socket-based collector test and the billing-signature
  fixture. The latter fails identically on untouched `ce81bd96` (20.549 s).
  The exact durable selection and rerun command are in
  [named-regressions.md](named-regressions.md).
- Missing/incomplete hook-aggregate recovery and historical 15/1 fixtures:
  five focused cases pass in 1.474 s after the existing failing tests exposed
  the omitted schema-18 additive repair.

Final owner/migration regression after the hook repair: 204/205 pass in
53.7 seconds; `metadata_errors: []`. The only failing case is the real native
sole-holder proof, so `requirement_failures` names RS-B-B4t-3. This run includes
the 106-method B4t module, cap guard, S3, S2 regressions, account observation
backup, installed-project backup, source transfer/image and account cleanup
release. All schema/copy/settlement checks in that selection pass.

All launched test sessions have finished or were explicitly interrupted and
reaped. No full run or test process is left in flight.

## Environment and remaining gate

- The billing signing-worker backup fixture times out awaiting its original
  signature and during cleanup on this tree and the untouched unit base; its
  genuine-sealed backup assertion remains for the primary.
- Unix socket bind is denied (`EPERM`), including the existing account/history
  listener fixtures. The two learning-migration CLI failures reproduce on the
  untouched unit base in this environment.
- The real macOS pin test gets a valid kernel pin and process identity, then
  refuses sole-holder proof because `proc_pidfdinfo` denies inspection of a
  same-user opener. The test remains failing, not skipped or weakened. The new
  bounded KERN_ARGMAX read fixed an earlier real argument-read error.
- The wizard fixture cannot create its trusted runtime cache outside writable
  roots. Its two new full-flow cases therefore do not reach the product.
- Linux, PTY/lifecycle flows and live IP-B were not run. The reply producer soak
  pulls in a transitive batch exceeding this builder's allowed run budget and
  remains for the primary. `test_project_start`'s native process scenarios also
  remain unrun here; the new B4m recovery-line component witness passes.
- `test_runtime_release` has the one expected current-digest mismatch; its file
  set and unique/increasing catalogue checks pass. The primary must assign the
  release version and catalogue entry at merge, as instructed.

The primary can rerun named modules with `$PY -B scripts/parallel_tests.py
--module codex:tests.test_b4t_takeover` and the modules listed above. For native
acceptance use the S5/SW-3 affected lifecycle flows on both systems, then the
review and quiet batch gates in plan section 1. No approval is requested here.


## Final implementation notes

The current-owner predicate requires the exact applicable takeover edge and its
prefix from the immutable original binding. A valid chain elsewhere in the
store is insufficient. Status validates the run mapping and its takeover edge,
checks releasing outboxes and deduplicates bridge IDs across successive updates.
The wizard's post-stop count includes a verified unfinished orphan, while a
result committed before the stop keeps its own outcome; run-status annotation
and capacity release still wait for the ordinary lost-settlement evidence.

The extended schema fixtures and all changed assertion expressions are in the
143-pair audit. The new helper restores historical layouts by dropping local
authority, never by changing an original receipt or result. The restored
current-ledger additive repair is an existing owner-open behavior, not a new
migration/admission permission.


[Changed-file manifest](files.md). `git diff --check` passes. No path in these
requirement documents names an absolute home directory; no raw logs are copied
here. No VERSION or digest-catalogue file appears in the changed-file list.
