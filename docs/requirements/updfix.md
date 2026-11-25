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
  commits anything. A refused update changes no file, no Git commit or index entry, no record, and leaves every process running.
- UPDFIX-2 (L12.3): The check is a read-only owner command, `learning-update-check --project-root P`. It reports
  `children_running` (the same rule as the collector's drained stop: an unreleased claim that is not long crashed; a run admitted
  but never claimed does not count), `would_replace` (the running collector is not this build, or the project's service is another
  version's), and `refused` (both). It never stops, starts, writes or changes anything. When it cannot tell (no ledger, no
  collector, an unreadable or older ledger) it says `refused: false` and the later steps decide as before.
- UPDFIX-3 (L12.3): An update (and a setup run again on an installed project) whose check says `refused` stops in step 2 with
  "a child run is active; nothing was changed, try again when none is running" (the same words as the collector's refusal), exit
  non-zero, before the installer, the commit, registration or attestation run. The update after the child has ended goes through.
  A child running with a collector that is already this build, and a project with no collector, are not refused.
- UPDFIX-4 (L12.3): A child that starts after the check and is refused at step 5 anyway is told the truth: the step says that
  setup already updated this project's files and to run the update again when no child is running; the sentence "nothing was
  changed" is never shown for the project in that case.
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
