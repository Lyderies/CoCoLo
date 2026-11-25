# T7 S5 status: update safety and rollback (L11.2, L12.3, L12.4, L12.5, G6; L12.2 unchecked)

Branch feat/t7-s5, base cbc757d5 (0.8.60). Module scripts/e2e_checks_update_safety.py; guard tests tests/test_e2e_update_safety.py
(`parallel_tests.py --module root:test_e2e_update_safety --module root:test_e2e_requirements` shows `"metadata_errors": []`).
Nothing pushed, installed or merged; no product code, VERSION or digest changed.

## Fix round after review t7s5-r1 (decisions 259, 260; one round)
1. L12.2 (HIGH): no 0.2.x leftovers are planted, so the ID is UNCHECKED (`checks: []`, `deferred`, reason "0.2.x leftovers need a
   historical ledger: slice S4 / L12.6"); its check is removed. Its admitted-never-launched half is judged inside G6's first update.
   `tests/test_e2e_requirements.py` accepts `deferred`/`reason` (cherry-picked from feat/t7-s6 f30c3222, only that test change).
2. G6 (HIGH): judges EVERY update of the slice that must succeed: both main-scene updates, the retry after the refused update with
   a running child, the update after the uncommitted file was discarded, the attempt after the forced failure. Each update's verdict is
   a reason code (`g6_first_update_ok`, `g6_retry_after_refusal_failed`, ...). G6 is RED in a normal run until UPDFIX lands (the retry
   after the refused update still needs a hand stop): that is correct. All checks of the slice now share one scene group `upd_all`
   (G6 reads every scene; the slice runs in one process, about 5 min wall instead of 2).
3. L11.2 (HIGH): the scene first runs a real child through the earlier build's installed CLI (recorded, orchestration closed, waited
   until the services' own working files are gone: `quiet()`), reads the project history store read-only (every record as
   type:sequence -> content sha256 plus `pragma integrity_check`), and requires: exactly equal after the forced failure, every earlier
   record unchanged after the next successful update, and the seeded child still readable (its outcome record, the account's per-run
   read). Statement reworded (decision 260), also in scripts/test-requirements.json.
4. Fault comparators (MED): `fault_isolated` needs a real green-to-red transition per targeted ID (red in both is rejected). Faults:
   `failed_update_leaves_files` -> L11.2 (the failing step also leaves a file the part-applied update did not report, so the
   rollback cannot restore the project); `update_ignores_uncommitted_files` -> L12.4 (setup_wizard.flow does not stop at dirty
   managed files); `update_stops_other_projects` -> L12.5 (the second project's service-hint removal takes every project's hint, so
   the first project's service stops); `update_handover_broken` -> G6 (may also turn G1, G8, L11.2, L12.4, L12.5 red: their updates
   need the handover). G6 is red in a normal run today, so its proof is per update: an update `_ok` in the normal run and `_failed`
   under the fault. L12.3 has no fault: it is red by a product bug, so no green-to-red transition can be shown.

## What an update is here
`./setup.sh --update PROJECT` of this checkout, on a project installed by an EARLIER build. The earlier build is a copy of this
checkout's tracked files (`git ls-files`, so a shallow clone is enough) with VERSION = the release before and one comment line in a
runtime file (its own runtime digest: the collector AND the project service are really another version, as the update replaces both).
It is installed through its own `setup.sh --answers`. It is the same code, not older code: real earlier tags are S4's. A VERSION-only
copy was tried first and rejected: the runtime digest ignores VERSION, so the project service was kept and no other-version path ran.

## Scenes (four Worlds, one process `upd_all`; every World is removed at the end of the process)
- upd_main (about 105-155 s): two projects on the earlier build; project A has a run opened and admitted, never dispatched. Update A,
  then update B, then a child runs to its end on each. L12.5, G6.
- upd_child (about 65-95 s): a child really running (gated fake client) when the update is run; the refusal; the child finishes and is
  recorded as the primary does; then the update again. L12.3 (three checks), and the retry judged by G6.
- upd_dirty (about 50-70 s): a tracked managed file edited, not committed; the update; discard; the update again. L12.4, G6.
- upd_fail (about 70-95 s): a real child is run and recorded first (history seed, then `quiet()`), then the Claude package's update step
  is forced to fail after the Codex step was applied (a test-only source rewrite of `claude_agents.packaging`, the stimulus, not the
  fault); then the next attempt without it. L11.2, G6.

## How values are read (never the update's own screen)
Process table (`ps`): which collector and project service run, from which tree (the earlier clone or this checkout), the old pid gone,
exactly one new collector, the child's own process still alive right after the refusal. The collector's own `learning-status` (version,
ready, stale, earlier_version_runs). The snapshot of every file digest in the project and the home, Git HEAD, commit count and
uncommitted listing, the collector's counts by kind and the ledger's row counts per table (read-only sqlite). The account ledger's
capacity table and binding count; the project history's outcome record (`persisted_outcome`); the account service's per-run read.
The screen is read only for words (is the refusal plain: `child_refusal_is_plain`, `dirty_refusal_is_plain`) and the step summary.
The harness never stops, kills or signals a process (a guard test forbids it): an update that needs a hand stop fails its check.

## ID -> check -> evidence
Normal run (`--execute <all S5 check names>`, 5m10 wall, one process, machine loaded by other units): L11.2, L12.4, L12.5 green;
L12.3 RED (three checks, finding 1 and 2), G6 RED (the retry after the refused update, finding 2: `g6_first_update_ok`,
`g6_second_update_ok`, `g6_retry_after_refusal_failed`, `g6_after_discarding_the_file_ok`, `g6_after_the_forced_failure_ok`); G1 red
only because of finding 1 (`g1_unexpected_exit` in this check); L12.2 unchecked (`no_check`, deferred).
| ID | Check | Evidence |
|----|-------|----------|
| L12.2 | none (unchecked, deferred) | needs 0.2.x leftovers (a historical ledger): S4 / L12.6. The admitted-never-launched half runs inside G6's first update. |
| L12.3 | l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on | its expectations hold (plain refusal, child unaffected, slot released); G1 goes red with it only because a step that must exit 0 after the refusal did not (a consequence of findings 1 and 2). |
| L12.3 | l12_3_refused_update_with_a_running_child_changes_nothing | RED (product finding 1). Reasons: changed_files, changed_commits, changed_head, changed_records. No fault proof: red in a normal run by a product bug being fixed in another unit (UPDFIX). |
| L12.3 | l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended | RED (product finding 2): step 5 fails with project_service_other_version. |
| L12.4 | l12_4_update_with_uncommitted_managed_files_is_refused_and_changes_nothing | green. Exit non-zero, `managed_files_uncommitted ... Nothing was changed.`; snapshots equal; the edit's sha256 unchanged; same collector and service pids on the earlier build; after `git checkout` the same update exits 0 and replaces the collector. Own red proof: `update_ignores_uncommitted_files`. |
| L12.5 | l12_5_second_project_updated_after_the_first_works_and_the_first_still_works | green. Second update: one step, exit 0, no FAIL, never "stopped working"; its service replaced, one collector from this checkout; project A's service is the same live pid after B's update and ready; a child runs to completed on each project afterwards. Own red proof: `update_stops_other_projects`. |
| L11.2 | l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds | green. A real child ran and was recorded through the installed CLI of the earlier build before the failure (history store: 7 records among them `outcome:`, integrity ok). Forced failure: exit non-zero, steps 1-3 OK, step 4 FAIL; the Codex step's file rewritten (mtime) and put back (digests equal); files, HEAD, commits, uncommitted listing, collector records, ledger row counts equal; history store EXACTLY equal (every record's sha256, integrity ok); collector and service same live pids. Next attempt: exit 0, replaced, ready, a new commit, clean tree; every earlier history record unchanged; the seeded child's outcome still `completed` and the account's per-run read `released`. Own red proof: `failed_update_leaves_files`. |
| G6 | g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build | RED until UPDFIX (finding 2): every update judged separately (reason codes above); only the retry after the refusal fails. Proof of sensitivity: `update_handover_broken`. |

## Injected faults (`--fault NAME`, chosen only by the runner's argument)
Each fault run (`--execute <the targeted check names> --fault NAME`) is compared with the normal run by `fault_isolated`
(green -> red per targeted ID; no other ID may change except the named collateral ones). Results (this round, all `isolated: True`):
| Fault | Target (green -> red) | Other IDs that changed | Run time |
|-------|----------------------|------------------------|----------|
| failed_update_leaves_files | L11.2 (files, dirty) | none | 91 s |
| update_ignores_uncommitted_files | L12.4 (plain refusal) | none | 60 s |
| update_stops_other_projects | L12.5 (first project's service stopped by the second update) | none | 122 s |
| update_handover_broken | G6 (`g6_*_ok` -> `_failed` for every update; red in both runs, so by reason code) | L11.2, L12.4, L12.5 and G1, G8 (their updates need the handover; G8: the failed update's text carries the temporary path) | all checks, about 5 min |
The L12.4 fault run was made before the final G6 rewrite; its scene, patch and check are unchanged since.

## Product findings (no product code changed)
1. L12.3 "changes nothing" is not met. With a child running the update refuses at step 5 (collector registration), AFTER step 4 installed
   the new files and committed them: `.codex/orchestration/install-manifest.json` changed, HEAD moved, one more commit, and the project's
   installed CLI is now the new one while the collector and service are the earlier ones. The refusal text says "nothing was changed",
   which is false for the project. Plan S1-U6 ("stops plainly and changes nothing") is not met.
2. Consequence: after that refusal the primary that started the child cannot record the child's result (`installed collector differs
   from the owner-authorized runtime; run learning-register`) nor close its orchestration (`Another CoCoLo version ... holds the project
   state`), and the next update fails at step 5 with `project_service_other_version` after the child has ended and its slot is released:
   it needs the hand stop docs/troubleshooting.md describes. Control: with no refused update before it, an update after a finished,
   recorded, closed child goes through. Fix direction for the primary: check the running-child condition before step 4 writes anything.
3. L12.2 "0.2.x leftovers" is not covered: a `legacy_unknown` slot only exists after a real 0.2.x ledger upgrade; planting one with a
   write into the product's ledger was refused by the harness' permission check (as S3's). The ID is unchecked (decision 259); S4 (L12.6)
   owns historical ledgers.
4. L11.2 named `learning-rollback` (a candidate's failback, unrelated to an upgrade); reworded by decision 260 to the installer's
   automatic rollback of a failed update (install.py `_roll_back`).
5. After a refused update `learning-status` and `learning-run-status` through the (new) installed CLI refuse (stale collector), so the
   child's liveness cannot be read through the product; the check reads the child's own process instead.

## Not provable here
Real older code (S4), 0.2.x ledgers, a real second user, launchd/login start, sleeping machines, real Codex/Claude clients.

## Time
Normal run of the five checked IDs plus L12.3's three checks, one process: 5m10 (scenes: 67 + 50 + 106 + 94 s of checks, the rest scene
setup). Fault runs 60-122 s each, the handover fault about 5 min. Unit tests: 32 in 0.03 s (`test_e2e_update_safety`) plus
`test_e2e_requirements`; `parallel_tests.py --module root:test_e2e_update_safety --module root:test_e2e_requirements`: 12 s,
`"metadata_errors": []`, `"failed_units": []`.
