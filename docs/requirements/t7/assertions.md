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

## T7 S1 (primary decision 151)
L9.1 statement old: "Two children at once run; a third is refused with claim_busy and the probe with probe_busy."
-> new: "Children run at once up to the installed limits (3 per project, 6 per machine); one over a limit is refused
with its reason (preparation_full in the project, claim_busy across projects) and the probe with probe_busy." Reason: the
installed policy limits are 6/3 (configurable limits are 0.5.0); the check proves the installed numbers.
L6.1 and L6.2 stay as written: their red/inconsistent results are product bugs fixed in their own unit.

## Slice S1, fix round 1 (Codex review t7s1, NOT-READY, 2026-11-24): assertions changed in S1 checks
All in scripts/e2e_checks_run_control.py (added at 8cafd4c1, same slice; none is an S0 check).
| ID | Check | Old assertion | New assertion | Reason |
|----|-------|---------------|---------------|--------|
| L6.1 | l6_1_tool_the_client_denied_is_reported_in_the_result | history holds one dispatch_result with `terminal_state is None` (a missing field also passed) | one dispatch_result; its `terminal_state` is exactly `failed`; the reply outcome is `claude_tool_not_allowed`; the stored result is failed and names `claude_tool_not_allowed` (the history keeps only its keyed digest, so: the returned result says so and recording it against that digest is accepted). This is the outcome fix/l6-findings a876f6f0 produces (coordinator). Report assertions kept. | HIGH L6.1: a completed child the client denied a tool passed. Stricter, same requirement. |
| L6.1 | l6_1_child_calling_a_tool_the_run_does_not_allow_is_stopped... | "the {tool} run opens" (`open_code == 0`) | "opens and its dispatch answers" (`open_code == 0 and code == 0`); added: the stored result (recorded against its digest) names `claude_tool_not_allowed` (proves the predicate on the existing path) | dispatch exits are now judged by the S1 check (see G1 note below). Stricter. |
| L6.2 | l6_2_uncommitted_read_path_refused_before_admission | `admission_created is not True` (missing passed); history/ledger counts sampled BEFORE the open; open not judged; dispatch refused with `preparation_export_failed`; slot counts equal | `admission_created` present and `False`; preflight leaves the admission history (open provenance, run bindings, capacity slots) unchanged; the open REFUSES (exit not 0, not admitted); the admission history re-read AFTER the open is unchanged; the dispatch is refused (exit not 0, not ready, a named reason) and the history after it is unchanged | HIGH L6.2. The dispatch's reason is no longer pinned to `preparation_export_failed`: once the open refuses (the product fix, decision 151) the dispatch has no run to export, so pinning today's reason would keep the check red after the fix. |
| L9.3 | l9_3_two_projects_share_one_collector... | per-project dispatch_result COUNTS 4/3/1 | per project, the sorted dispatch_result run IDs equal the run IDs it dispatched; every run ID named by its dispatch_result/event/host_open_provenance records is one opened for it; no run ID opened for two projects | MED L9.3: counts survive an exchange. Stricter. |
| G5 | g5_after_close_every_claimed_run_released... | probe and eight reads taken BEFORE close (reuse/cooldown patched to 1 s/0.5 s, 1.2 s sleep) | close first (reports: nothing active or pending); then ONE probe of a second, attested, never-probed Claude account (launched); the account ledger holds exactly one `probe_admission` pass for that account made after the close and its `provider_billing` reading; then the eight per-run reads (released) and the ledger table (8 released); the order close <= probe <= reads is asserted | HIGH G5 and HIGH billing (decision 128 scales only silence limits). L9.1's `probe_busy` probe is the same second account's. |

G1 note (fault isolation): S1 dispatches (`Ctl.dispatch`) and the G5 probe pass `refuses=True` to the harness, so an
unexpected exit of one of THOSE steps fails the S1 check that asserts its `code` (every one does), not G1. Every
other S1 step (setup, observe, open, record, status, capacity reads) is still judged for G1 at capture, and timeouts
and bare reasons of every step still are. Without it a capacity fault (slots never released) turns G1 red too. For
the primary to read against G1's intent.

## Slice S1, fix round 2 (primary decision 157, confirming check on 3f190429): assertions changed
All in scripts/e2e_checks_run_control.py (S1 checks only) and tests/test_e2e_run_control.py.
| ID | Check | Old assertion | New assertion | Reason |
|----|-------|---------------|---------------|--------|
| G1 (judging) | `Ctl.dispatch`, G5 probe | every S1 dispatch and the G5 probe passed `refuses=True` (an unexpected exit hid from G1) | `refuses=True` only on the expected refusals (fourth child: preparation_full, third project's child: claim_busy, uncommitted dispatch, the full-machine probe); every other dispatch and the G5 probe are judged by G1 (exit 0) again | NEW HIGH. Under the slot fault G1 now also goes red; it is declared in `FAULT_ALSO_RED`. |
| L9.2 | l9_2_capacity_released... | (no exit assertion for the six first-wave dispatches) | added: each of the six first-wave dispatches exits 0 | NEW HIGH: explicit success. |
| L9.3 | l9_3_two_projects_share_one_collector... | results == run IDs whose `run.code == 0` (a failed dispatch and its missing result vanished together; counts 4/3/1 no longer asserted) | results == run IDs of the runs SCHEDULED to succeed (all except `scene.refused`) AND counts are 4/3/1 AND no None ID | MED L9.3. Stricter. |
| L6.2 | l6_2_uncommitted_read_path_refused_before_admission | open refused = nonzero exit, not admitted, not ready (a crash with an empty reply passed); dispatch refused = nonzero, not ready, ANY reason_code | open refused AND its reply names the read-path reason (reason_code `read_path_not_committed` or a reason saying "not committed"); dispatch refused (nonzero, not ready) with the same reason | HIGH L6.2. Stricter; check stays red until the product fix. |
| fault isolation | `fault_isolated` | changed IDs outside S1 failed; any S1 ID (incl. L9.1) could turn red | a newly red ID outside the fault's declared targets (`FAULT_TARGETS` + `FAULT_ALSO_RED` = G1) fails: L9.1 red under the fault is a failure | HIGH. |
| fault isolation | helper children | E2E_* stripped from workers and product processes only | every subprocess in e2e_support.py (trusted python/git lookups, ssh-keygen, ps) gets `child_environment(os.environ)`; a test asserts every subprocess call in e2e_support.py and e2e_lifecycle.py names `env=` | HIGH. |
Tests changed in tests/test_e2e_run_control.py: `test_a_fault_run_must_turn_exactly_the_s1_ids_red` - old: G1 red -> not isolated, L9.1 red accepted; new: G1 red accepted (declared), any other ID or L9.1 red -> not isolated (fixture fault keeps L9.1 green). `uncommitted_scene` fixture: open reply now carries reason_code `read_path_not_committed`; `L93Tests.scene` fixture: adds `refused` (a4, c1).
