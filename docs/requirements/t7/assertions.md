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
