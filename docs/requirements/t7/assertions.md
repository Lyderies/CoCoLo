# T7 lifecycle e2e: assertions changed because the product changed (slice S0)

Rule: no existing assertion is edited to fit code. Each row below is a check that was already in
`scripts/e2e_lifecycle.py` on branch e2e-t7-l1 (87005877) and that the product has since made untrue on
purpose. Every row is a decision about the product's current, designed behaviour, with its source.

| ID | Check | Old assertion | New assertion | Reason |
|----|-------|---------------|---------------|--------|
| L4.2 | edited_route_refused | A route whose `provider` is edited is refused at open (exit 2). | A route whose `provider` is edited is admitted with the installed route in its place: provider, model, effort, role, agent, sandbox and dispatch of the admitted route equal the unedited route's. The other five edits (model, effort, role, sandbox_mode, dispatch) and the late edit at dispatch are still refused with exit 2. | cli.py `learning-open-host-run`: "A changed worker/verifier provider invalidates advertised execution settings. Only the fresh installed route supplies their replacements" (route fallback, RT-ROUTE-EXHAUSTED). The edit has no effect; the L4.1 launch-argument check proves the child still runs the policy's route. The edit is offered in its own orchestration and assignment, because the admitted run holds the task. |
| L2.2 | replace_scene | The status shows the owner key `fingerprint`, and it changes after the replacement. | The status shows no `fingerprint` (asserted absent); before the replacement `replacements == 0` and `last_replaced_at_ms` is null, after it `last_replaced_at_ms` is an integer and the allowed-signers record holds the new key blob and no longer the old (unchanged). | Owner decision 36: saved or AI-read output (the status command) carries no private value; the status no longer shows the fingerprint. |
| G8 | g8_no_private_value_in_any_output | Every output of every step, setup screens included, carries no fingerprint, key material, passphrase or temporary-home path. | Commands' outputs (saved or AI-read) keep that rule unchanged. The owner's terminal screens (setup, the owner's tools) are collected apart (`World.screens`) and must show no passphrase and no key material; they may show what the owner needs to proceed or troubleshoot. | Owner decision 36 ("screens show what is needed to proceed; saved/AI-read output has no private values"). The 0.8.4x wizard shows the key tool's own fingerprint line and the rule file's path. Whether the fingerprint line is "needed to proceed" is a primary decision (see report). |
| G9 | g9_report_is_content_free_json_with_phases_seconds_reasons | CI runs the lifecycle script with a report and keeps it as an artifact. | Unchanged. It failed because no workflow ran the script (the test reset replaced the CI steps). `.github/workflows/ci.yml` gains the `lifecycle-e2e` job (main, tags, nightly; macOS; `continue-on-error` until a first CI run shows it stable). | The requirement stays; the product (CI) was missing. |

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
