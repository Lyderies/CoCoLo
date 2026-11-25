# UPDFIX: an update that cannot go ahead changes nothing; a second setup changes nothing

Unit UPDFIX, branch fix/update-refusal, base 0.8.60 (cbc757d5). Two product bugs found by real lifecycle runs.
Requirement ids served are the lifecycle test's own (tests/e2e/requirements.json): L12.3 and L1.2.

## Findings (cause)

1. L12.3. `./setup.sh --update` with a child run still running: step 4 (install) wrote and committed the new files, then step 5
   (`learning-register`) asked the collector for a drained stop (`CollectorMaintenance.acquire`, learning_store.py) and was refused
   with "a child run is active; nothing was changed". The wizard (scripts/setup_wizard/flow.py `step_install` before
   `step_collector`) had no check before step 4, so the project's manifest, HEAD and CLI were already the new version while the
   collector and the project's service stayed on the earlier one: the primary could no longer record the child's result or close
   its orchestration, and the sentence "nothing was changed" was false for the project.
2. L1.2. `learning-register` on an already running compatible collector sends the collector's `register` op; the collector
   re-issued the project's wake key (`issue_wake_capability`, learning_store.py `_handle` register branch, service_enrollment.py
   `_issue_key`) every time: a new random key file `client-runtimes/<digest>.wake.cap` replaced the old one. A second setup, or
   an update with nothing to update, therefore changed a file (its digest) in the state folder.

## Requirements

- UPDFIX-1 (L12.3): Every refusal that depends on the collector's or the project service's state (a child is running while the
  collector or the project's service must be replaced) is decided in a read-only check BEFORE the update writes, installs or
  commits anything. A refused update changes no file of the project, no Git commit or index entry, no ledger record, no
  remembered-project entry (`setup-projects`), and leaves every process running. (Reading a ledger kept in write-ahead mode
  can make SQLite create or touch its own `-wal`/`-shm` files in the state folder; the ledger's contents are what stays
  unchanged, and the check cannot be made file-silent without ignoring the write-ahead log, which would miss a just-claimed
  child.)
- UPDFIX-2 (L12.3): The check is a read-only owner command, `learning-update-check --project-root P`. It reports
  `children_running` (the same rule as the collector's drained stop, evaluated with the same SQL functions the collector
  registers, including for an unreleased earlier-version slot: an unreleased claim that is not long crashed; a run admitted
  but never claimed does not count), `would_replace` (the running collector is not this build, or the project's service is
  another version's), `unreadable`, and `refused` (`unreadable`, or both of the first two) with a `reason`
  (`child_run_active` or `ledger_unreadable`) and a plain `message`. It never stops, starts, writes or changes anything.
  Decision 265, it FAILS CLOSED: only a state folder with no ledger at all (no learning database) says `refused: false`
  and the later steps decide as before. A ledger that exists but cannot be opened, read or evaluated (damaged or not a
  database, refused folder, an evaluation error) says `unreadable: true`, `refused: true`, reason `ledger_unreadable`,
  and its message gives the next step: close any running child, check the state folder is readable, try again. The wizard
  also fails closed on any answer that is not a readable `refused` boolean (command failed, not JSON).
- UPDFIX-3 (L12.3): An update (and a setup run again on an installed project) whose check says `refused` stops in step 2 with
  "a child run is active; nothing was changed, try again when none is running" (the same words as the collector's refusal;
  for an unreadable ledger, the check's own plain sentence with its next step), exit non-zero, before the installer, the
  commit, registration or attestation run, and before the project is remembered (an interactive open of an installed
  project remembers it only once accepted, for both entry paths). The check is asked again immediately before the installer's
  `--apply` (step 4's first write): a child that appeared after step 2 is refused there, before any write. The update after the child has ended goes through.
  A child running with a collector that is already this build, and a project with no collector, are not refused.
- UPDFIX-4 (L12.3): A child that starts after the check and is refused at step 5 anyway is told the truth: the step says that
  setup already updated this project's files and to run the update again when no child is running; the collector's sentence
  "nothing was changed" is replaced by that, never shown for the project in that case.
- UPDFIX-5 (L1.2): Registering a project on a running compatible collector keeps the project's existing valid wake key (the file,
  its bytes and its mode are unchanged and the collector's verifier knows the same key); only a missing or invalid key is issued.
  A collector that starts still issues a fresh key per active project (unchanged).
- UPDFIX-6 (L1.2, L12.3): The lifecycle checks pass: `l1_setup_again_with_answers_changes_nothing`,
  `l1_update_on_installed_project_changes_nothing`, `l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on`,
  `l12_3_refused_update_with_a_running_child_changes_nothing`,
  `l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended`.

## Tests

| ID | Test |
|----|------|
| UPDFIX-2 | packages/codex-agents/tests/test_updfix.py `UpdateCheckTests` |
| UPDFIX-5 | packages/codex-agents/tests/test_updfix.py `WakeKeyKeptTests` |
| UPDFIX-3, UPDFIX-4 | tests/test_setup_update.py `test_UPDFIX_*` |
| UPDFIX-1, UPDFIX-6 | the lifecycle checks above (scripts/e2e_lifecycle.py --execute ...) |

## Status (2026-11-25) and evidence

Done and committed on fix/update-refusal; nothing pushed, installed or merged; VERSION and runtime-releases.json untouched.
- Code: cli.py `_update_check_report` and `learning-update-check`; service_enrollment.py `keep_wake_capability` used by the
  collector's `register` op (learning_store.py); scripts/setup_wizard/flow.py `update_refusal` (step 2 and again before step 4's `--apply`) and the step 5
  line `CHILD_STARTED_LATER`; docs/troubleshooting.md.
- Red -> green: test_updfix.py 4 failures + 10 errors (command and function missing) -> 8 OK; tests/test_setup_update.py 3
  failures (`test_UPDFIX_3_*` x2, `test_UPDFIX_4_*`) with flow.py as before -> 21 OK (161 s). Lifecycle checks, run with
  `scripts/e2e_lifecycle.py --execute <names>`: before the fix l1_setup_again_with_answers_changes_nothing and
  l1_update_on_installed_project_changes_nothing failed on one file (`client-runtimes/<digest>.wake.cap`, re-issued by every
  `register`), and with the wizard as before l12_3_refused_update..._changes_nothing (changed_files/head/commits/records) and
  l12_3_the_update_after_..._has_ended (step_5_failed) failed; after the fix all of them pass.
- Known not run: the full suite (the primary's job). `codex:tests.test_runtime_release` now fails by design until the primary
  refreshes the 0.8.60/next runtime digest (a runtime file changed: cli.py, learning_store.py, service_enrollment.py).
- Residual (documented, not closed in this unit; decision 265): a child claimed in the window between the re-check before
  `--apply` and step 5's registration (the installer, commit and verify take seconds) is still refused at step 5 after step 4
  updated the project. UPDFIX-4 says so truthfully and the update is run again. Closing it fully needs a drained-maintenance
  hold kept from the check through registration, or a rollback of files, routing, index and HEAD; neither is built here.

## Fix round 1 (review 040-updfix-r1, NOT-READY, decision 265)

- HIGH `_drained` on the check's read-only connection lacked the legacy slot SQL functions and swallowed the error: now
  `register_legacy_functions` (legacy_slots.py, also used by the collector's connection) is registered, and any evaluation
  error fails closed. Test `test_UPDFIX_2_a_live_child_is_seen_even_when_an_unreleased_earlier_version_slot_exists`.
- MED re-check right before `--apply`: `test_UPDFIX_3_a_child_that_appears_before_the_install_is_refused_before_any_write`.
- MED the displayed reason of a step-5 refusal after step 4 wrote is replaced (`failure_detail(why=...)`): the negative
  wording assertions in `test_UPDFIX_4_*`.
- MED interactive open remembers the project only after acceptance:
  `test_UPDFIX_3_an_interactive_refused_update_leaves_the_remembered_projects_alone`.
- LOW the check opens SQLite `mode=ro`: the ledger's contents are unchanged, but SQLite may create or touch `-wal`/`-shm`
  files; stated above rather than claimed away (UPDFIX-1).
- Test gap: the lifecycle check also asserts the refusal is reported as the update stopping with `child_run_active` and that
  step 2 is not OK.
