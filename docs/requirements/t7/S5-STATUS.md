# T7 S5 status: update safety and rollback (L11.2, L12.2, L12.3, L12.4, L12.5, G6)

Branch feat/t7-s5, base cbc757d5 (0.8.60). Module scripts/e2e_checks_update_safety.py; guard tests tests/test_e2e_update_safety.py
(red first: ModuleNotFoundError, 1 error; now 20 green; `parallel_tests.py --module root:test_e2e_update_safety` shows
`"metadata_errors": []`). Nothing pushed, installed or merged; no product code, VERSION or digest changed.

## What an update is here
`./setup.sh --update PROJECT` of this checkout, on a project installed by an EARLIER build. The earlier build is a copy of this
checkout's tracked files (`git ls-files`, so a shallow clone is enough) with VERSION = the release before and one comment line in a
runtime file (its own runtime digest: the collector AND the project service are really another version, as the update replaces both).
It is installed through its own `setup.sh --answers`. It is the same code, not older code: real earlier tags are S4's. A VERSION-only
copy was tried first and rejected: the runtime digest ignores VERSION, so the project service was kept and no other-version path ran.

## Scenes (four Worlds, so four parallel processes under --jobs; every World is removed at the end of its process)
- upd_main (about 110-155 s): two projects on the earlier build; project A has a run opened and admitted, never dispatched. Update A,
  then update B, then a child runs to its end on each. L12.2, L12.5, G6.
- upd_child (about 70-95 s): a child really running (gated fake client) when the update is run; the refusal; the child finishes and is
  recorded as the primary does; then the update again. L12.3 (three checks).
- upd_dirty (about 55-70 s): a tracked managed file edited, not committed; the update; discard; the update again. L12.4.
- upd_fail (about 55-75 s): the Claude package's update step is forced to fail after the Codex step was applied (a test-only source
  rewrite of `claude_agents.packaging`, the stimulus, not the fault); then the next attempt without it. L11.2.

## How values are read (never the update's own screen)
Process table (`ps`): which collector and project service run, from which tree (the earlier clone or this checkout), the old pid gone,
exactly one new collector, the child's own process still alive right after the refusal. The collector's own `learning-status` (version,
ready, stale, earlier_version_runs). The snapshot of every file digest in the project and the home, Git HEAD, commit count and
uncommitted listing, the collector's counts by kind and the ledger's row counts per table (read-only sqlite). The account ledger's
capacity table and binding count; the project history's outcome record (`persisted_outcome`); the account service's per-run read.
The screen is read only for words (is the refusal plain: `child_refusal_is_plain`, `dirty_refusal_is_plain`) and the step summary.
The harness never stops, kills or signals a process (a guard test forbids it): an update that needs a hand stop fails its check.

## ID -> check -> evidence
Normal run: `python3 -I -B scripts/e2e_lifecycle.py --flows L11,L12 --jobs 4 --report R` (3m10 to 4m15 wall under load from other units;
reports kept outside the repo). Result of the last run: L11.2, L12.2, L12.4, L12.5, G6 green; L12.3 RED (finding 1); G1 RED only
because of that finding (a step that must exit 0 did not); G2-G5, G8, G9, L11.1 green; L12.1, L12.6, L12.7 no check (S4).
| ID | Check | Evidence |
|----|-------|----------|
| L12.2 | l12_2_one_update_with_an_admitted_never_launched_run_needs_no_manual_stop | green. Before: run opened and admitted, never dispatched, its binding in the ledger; collector and service run from the earlier clone, version = previous. One `setup_update` step, exit 0, no FAIL; earlier collector pid alive before and gone after, exactly one new collector from this checkout, service replaced, status ready, this version, not stale, earlier_version_runs holding 0. NOT covered: the 0.2.x leftover clause (finding 3). |
| L12.3 | l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on | green. Child running (service live status), update exits non-zero at step 5, text says "a child run is active; nothing was changed, try again when none is running"; collector, service and the child's own process are the same live pids right after; the dispatch ends 0 and the slot is released. |
| L12.3 | l12_3_refused_update_with_a_running_child_changes_nothing | RED (product finding 1). Reasons in the report: changed_files, changed_commits, changed_head, changed_records. |
| L12.3 | l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended | RED (product finding 2): step 5 fails with project_service_other_version. |
| L12.4 | l12_4_update_with_uncommitted_managed_files_is_refused_and_changes_nothing | green. Exit non-zero, `managed_files_uncommitted ... Nothing was changed.`; snapshots equal (files, HEAD, commits, the uncommitted listing, records, ledger); the edit's sha256 unchanged; the same collector and service pids on the earlier build; after `git checkout` of the file the same update exits 0 and replaces the collector (so the refusal was about the file). |
| L12.5 | l12_5_second_project_updated_after_the_first_works_and_the_first_still_works | green. Second update: one step, exit 0, no FAIL, never "stopped working"; its service replaced, one collector from this checkout; project A's service is the same live pid after B's update and ready; a child runs to completed (outcome record, slot released) on each project afterwards. |
| L11.2 | l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds | green (intent; the literal `learning-rollback` clause is not exercised, finding 4). Forced failure: exit non-zero, steps 1-3 OK, step 4 FAIL; the Codex step's file was rewritten (mtime changed) and put back (digests equal); files, HEAD, commits, uncommitted listing, records by kind and ledger row counts equal; collector and service same live pids on the earlier build. Next attempt: exit 0, collector replaced, ready on this version, a new commit and a clean tree. |
| G6 | g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build | green over both updates of upd_main: each exit 0 and one command; ends with exactly one collector from this checkout, ready, this version, not stale; the earlier collector was alive right before the first update and gone after it with no stop between; the same healthy collector after both. |

## Injected fault (`--fault update_handover_broken`)
The earlier collector refuses every `shutdown_if_drained` (as if a child always ran): the update can only finish by a hand stop.
Patch applies to `codex_agents.learning_store` exactly once (a test guards it). Run: `--flows L11,L12 --jobs 4 --fault update_handover_broken`
(3m10): L11.2, L12.2, L12.4, L12.5, G6 turn from green to red; L12.3 is red in both runs (its refusal check stays green, the other two
red as before); `fault_isolated(normal, fault)` = `{'isolated': True, 'changed': ['G6','L11.2','L12.2','L12.4','L12.5'], 'changed_outside_targets': [], 'targets_not_red': [], 'ids_differ': []}`.
A first fault run was not isolated only because G5 (S1's capacity scene) timed out under load (`g1_step_timed_out`, 259 s vs 142-192 s);
the rerun with the machine quieter was clean. G1 may also go red under the fault (declared in FAULT_ALSO_RED).

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
   write into the product's ledger was refused by the harness' permission check (as S3's). S4 (L12.6) owns historical ledgers.
4. L11.2 names `learning-rollback`; that command (cli.py) records a candidate's failback to the champion and has no connection to an
   upgrade. The rollback of a failed upgrade is the installer's own, automatic (install.py `_roll_back`). The statement needs rewording
   or a decision; the check judges the described behaviour.
5. After a refused update `learning-status` and `learning-run-status` through the (new) installed CLI refuse (stale collector), so the
   child's liveness cannot be read through the product; the check reads the child's own process instead.

## Not provable here
Real older code (S4), 0.2.x ledgers, a real second user, launchd/login start, sleeping machines, real Codex/Claude clients.

## Time
Whole run `--flows L11,L12 --jobs 4`: 3m10 (fault) to 4m15 (normal, with other units loading the machine); the S5 scenes alone are
about 110-155 s (upd_main), the others 55-95 s each, in parallel. Unit tests: 20 in 0.3 s.
