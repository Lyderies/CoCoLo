# T7 lifecycle e2e: assertions changed because the product changed (slice S0)

Rule: no existing assertion is edited to fit code. Each row below is a check that was already in
`scripts/e2e_lifecycle.py` at the e2e-t7-l1 merge parent 342df324 ("T7 L5.3: verifier ready variant"; 87005877
held only the runner skeleton) and that the product has since made untrue on purpose. Every row is a decision about the product's current, designed behaviour, with its source.

| ID | Check | Old assertion | New assertion | Reason |
|----|-------|---------------|---------------|--------|
| L4.2 | edited_route_refused | A route whose `provider` is edited is refused at open (exit 2). | A route whose `provider` is edited is admitted with the installed route in its place: provider, model, effort, role, agent, sandbox and dispatch of the admitted route equal the unedited route's. The other five edits (model, effort, role, sandbox_mode, dispatch) and the late edit at dispatch are still refused with exit 2. | cli.py `learning-open-host-run`: "A changed worker/verifier provider invalidates advertised execution settings. Only the fresh installed route supplies their replacements" (route fallback, RT-ROUTE-EXHAUSTED). The edit has no effect at open: the admitted route's seven fields are asserted equal to the installed route's. The provider-edited run is judged at open only and is not dispatched (the L4.1 launch-argument check dispatches the unedited route). The edit is offered in its own orchestration and assignment, because the admitted run holds the task. |
| L2.2 | replace_scene | Before the replacement the status shows the owner key `fingerprint` (342df324 line 454); after it the `fingerprint` is present and differs from the one before (line 474). | The status shows no `fingerprint`, asserted absent both before and after the replacement; before the replacement `replacements == 0` and `last_replaced_at_ms` is null, after it `last_replaced_at_ms` is an integer and the allowed-signers record holds the new key blob and no longer the old (unchanged). | Owner decision 36: saved or AI-read output (the status command) carries no private value; the status no longer shows the fingerprint. |
| G8 | g8_no_private_value_in_any_output | Every collected output of the L1 world only (its setup screens and command outputs; the owner's tools were already excluded with `record=False`) carries no fingerprint, key material, passphrase or temporary-home path (342df324 lines 1219-1228). | Judged at capture for every World and every step (`e2e_support.capture_breaks`; a break fails the check that captured it and decides G8 failed): commands' outputs (saved or AI-read) carry no private value at all; every owner screen (setup and the owner's tools, `record=False` included) shows no passphrase, no public key blob and no private key material (a private key block or any line of the private key file). The G8 check keeps its L1-world assertions and also judges planted cases. | Owner decision 36 ("screens show what is needed to proceed; saved/AI-read output has no private values"). The 0.8.4x wizard shows the key tool's own fingerprint line and the rule file's path. Whether the fingerprint line is "needed to proceed" is a primary decision (see report). |
| G9 | g9_report_is_content_free_json_with_phases_seconds_reasons | CI runs the lifecycle script with a report and keeps it as an artifact. | Unchanged (the assertion text is the same as at 342df324); one assertion added: the report upload uses `if-no-files-found: error`. It failed because no workflow ran the script (the test reset replaced the CI steps). `.github/workflows/ci.yml` gains the `lifecycle-e2e` job (main, tags, nightly; macOS; `continue-on-error` until a first CI run shows it stable). | The requirement stays; the product (CI) was missing. |

Not an assertion change: `World.cli()` now runs the project's installed CLI by default and raises for any
other program (decision 128d); `tests/test_e2e_strictness.py::InstalledCliTests` fails if a check names the
source-tree CLI. Every-flow requirements with no check yet (G5, G6, G7) are listed on stderr and not judged in
a `--flows` run; `--all` fails them.

Also changed (G8): `len(world.outputs) >= 6` ("the outputs of every step were collected") became
`len(world.outputs) + len(world.screens) >= 6`, because setup and the owner's tools now land in `screens`; the
claim (every step's output was collected) is the same.

## Primary decision 133 (2026-11-24, not owner-gated: follows decided requirements)
All four rows accepted after reading each against its source. L2.2 and G8: owner decision 36 (own screens may show a
fingerprint or file location; anything saved, logged, reported or AI-read carries no private value). L4.2: the RT-RX
re-route at open (96041325, cli.py learning-open-host-run) is decided behaviour; the requirement's intent (an edit
never takes effect) holds, so the L4.2 statement is reworded in tests/e2e/requirements.json and
scripts/test-requirements.json: old "A route edited after learning-route is refused." -> new "A route edited after
learning-route never takes effect: an edited worker/verifier provider is replaced by the fresh installed route at open
(RT-RX re-route at open, decision 133); any other edit is refused." G9: the new lifecycle-e2e job is not a required
check (main ruleset's 9 required checks unchanged).

## Fix round 1 (Codex review t7s0, NOT-READY): added assertions, none removed
- L2.2: added "after the replacement the status still shows no key fingerprint (decision 36)".
- G1, G8: added at-capture judging for every World (capture_breaks) and planted-case assertions on the judge.
- G3, G4: digest equalities now require a nonempty digest (`same_digest`); was a bare `==` that two missing
  digests passed. Stricter, same claim.
- G9: added "a missing report fails its CI step" (`if-no-files-found: error`).
- scripts/test-requirements.json: G8 and L1.2 statements copied from tests/e2e/requirements.json (their
  authority, already changed by the e2e-t7-l1 merge); they had drifted and failed validate_sources.

## Slice S2 (recovery, status, deletion; branch feat/t7-s2): assertions changed
None. No S0 or S1 check, test or existing assertion was edited or removed. Additions only: scripts/e2e_checks_recovery.py
(L8.1, L8.2, L8.3 x2, L14.1, L15.1), tests/test_e2e_recovery.py, `checks` arrays of five IDs in tests/e2e/requirements.json,
and shared-file hunks that are byte-identical to S1's (e2e_support.py: BOOT patch hook and `child_environment`) or
analogous to them (e2e_lifecycle.py: `--fault` wiring for the recovery module; e2e_core.py: GROUP_SECONDS).
Assumptions a decision may reverse (not changes): L15.1 treats `projects`, `tombstones` and the account's provider
observation rows (`account_observation_origins/requests`) as kept by design after a deletion.

## S2 fix round 1 (Codex review t7s2, NOT-READY): assertions changed, old -> new
Primary decision 173 (L8.2) and owner question 174 recommendation (L15.1) apply. Every row is a stricter or reworded assertion;
no assertion was removed without a replacement.

| ID | Old | New | Reason |
|----|-----|-----|--------|
| L8.2 statement (tests/e2e/requirements.json, scripts/test-requirements.json) | "Killing the collector mid-run: the same, once it is back." | "After the collector is killed, the project service survives: the run either completes normally with capacity released once the collector returns, or settles run_lost; never in limbo, never counted twice." | Decision 173: a killed collector does not make the run run_lost (0.8.56 behaviour, finding 4). |
| L8.2 check | `settled(status_after)`: ended with cleanup confirmed, or lost and released (read from the run-status reply) | `persisted_terminal(ledger binding, outcome)` is exactly `run_lost` (abandoned, service_lost, released) or `completed` (terminal, released AND the project history's outcome record for the run reads `execution_state == completed`, read explicitly, never inferred from release or from no run_lost); the run's own status agrees explicitly (`status_agrees`: run_lost reads reason_code run_lost; completed reads ready and terminal_observed true; an empty or running status fails); the project service pid is unchanged and alive after the kill | Review HIGH: `settled()` read a reply, and accepted any ended run. |
| L8.3 (collector scene) | slot read from `scene.slot_before` (the EARLIER run); `wait_running` result discarded; `collector_kept` accepted with no service check | uses `other_slot_before` (this run's slot, unreleased); `is_running(other_running)`; `service_replaced(before, after)`: one service before, a different single one after, the old pid gone; a dispatch that did not fail must complete (the old "or replaced" branch keyed on the collector now keys only on the exit code) | Review HIGH: the scenario (setup replaces the dispatching project's service) was not verified. |
| L8.3 (collector scene, 0.8.56) | (as above) | On 0.8.56 the second project's setup replaces neither the collector nor the dispatching project's service (the L8b incident is fixed), so the scenario cannot be forced to replace the service. Two outcomes, each judged in full and named in the reason code: `service_replaced` (new single process, old gone; the dispatch must be cut short and say so in words) or `service_untouched` (same single live process; the dispatch must complete). Anything else fails. Per primary decision 178 (DECIDED): keep both outcomes (no stimulus forces a replacement). Decision 180: the second dispatch is captured as expected-to-refuse and judged again once the service is observed: an untouched service's dispatch that exits nonzero is a G1 failure (`g1_unexpected_exit`); a replaced service's dispatch may refuse (and the check requires it to). | Review HIGH asked to verify replacement; the product no longer replaces. |
| L8.3 (both checks) | close ok = exit 0 and `ready is not False` | `closed_ok`: exit 0 and `ready is True` | Review MED: a missing or null ready passed. |
| L8.1 | next child completes: `nxt.reply.result.execution_state == completed` | `persisted_terminal(next run's ledger binding, persisted outcome) == completed` (the outcome record read explicitly from the project history, `execution_state == completed`; explicit persisted completion evidence again, as before the first fix round); the running status must be explicit (`terminal_observed is False`, was `in (False, None)`) | Review MED. |
| G1 (all S2 steps) | `refuses=True` on reports, running-status reads, recovery commands, close attempts, deletion, status after the lost run | `refuses=True` only on: the dispatch the kill cuts short, the run-status polls of a run that may not yet run or be lost, and the run status of an opened-not-dispatched run (L14.1 `opened`). A guard test lists every flagged call site. | Review HIGH: expected-success steps were exempt from G1. |
| G8 | `World.cli()` let a timeout escape (partial output unjudged, exception carries arguments and paths) | `World.cli()` captures a timeout (partial output judged, step recorded timed out, returns -1) | Review HIGH. |
| L15.1 | the ledger's `projects`, `tombstones` and both observation tables were exempt wholesale (`KEPT_BY_DESIGN`) | the deleted registration must be a tombstone (deleted, no root path, no root digest, deleted time); retained provider observations hold no project path and no run identifier; only `tombstones`, `projects` (the scrubbed row), and the two observation tables may hold rows | Review HIGH; owner question 174 recommendation. |
| L15.1 | the deleted service's original pid is gone | also: no `project_host.py` for the deleted project's folder in the final process listing (no replacement service) | Review HIGH. |
| L15.1 | surviving project: equal row counts | also: equal record IDs and content digests of the ledger `records` and its own history (a same-count substitution fails) | Review HIGH. |
| L14.1 | migration status judged only at the lost phase; no snapshot after the close attempt; two last assertions skipped when the first failed | migration status judged at the six phases that collect one (orchestration, opened, running, lost, next, closed; `idle` collects the lifecycle status only), with a guard (`migration_phases_judged`) that fails if any one is left out of the judged set; a `closed` phase after the close attempt is judged (orchestration closed, report completed, run counts); `expect_each` judges every last assertion before failing | Review MED. |
| L14.1 | `learning-orchestration-report` and the lost-phase run status passed `refuses=True` | no flag (expected success); only the `opened` phase's run status expects a refusal | G1 above. |

## S2 final fix (decision 180): assertions changed, old -> new
| ID | Old | New | Reason |
|----|-----|-----|--------|
| G1 / L8.3 | the second dispatch passed no `refuses` (a replaced service's required refusal failed G1) | `refuses=True` at capture plus a second judgement once the service is observed (`dispatch_g1_breaks`): untouched + nonzero exit = `g1_unexpected_exit` | New HIGH of the confirming check. |
| L8.2 / L8.1 | completed = ledger terminal and released | also the persisted outcome record `execution_state == completed`; L8.2 status agreement is explicit (`status_agrees`) | Confirming check: completed was inferred. |
| L14.1 | migration phases judged by a literal tuple | `MIGRATION_PHASES` plus `migration_phases_judged` guard | Confirming check. |
| tests (existing, edited per decision 180) | `test_refusals_are_marked...` forbade `refuses=True` on the collector scene's second dispatch; `test_refusal_is_expected_only_at_named_call_sites` listed 4 sites; `test_l8_2_terminal...` called `persisted_terminal(("terminal", None, 1))` expecting completed | the second dispatch must carry `refuses=True` and the late judge; 5 sites (adds `collector_scene dispatch_in_background`); completed needs the outcome argument `"completed"` | Same decision. |
