# U4 build status — U4-1/2/4 pass unsandboxed; U4-3 deferred (C5)

## Fix round (primary, unsandboxed)

- Scope: O0C-U4-3 is **deferred, unchecked**: C5 probe journal/reaper parked with the
  container phase (decision 110). Recorded in `u4.md`, `tests/requirements/u4.json` and
  `scripts/test-requirements.json` as `deferred: true` with that reason, no checks.
  The older "Missing 3.3 dependency" and "Resume" sections below are historical;
  step 1 there is now this deferral.
- Cause of the unsandboxed failure of `test_completed_history_past_the_cap_still_replays`
  (`'reserved' != 'committed'` in the borrowed fixture's `prepare`): a TEST defect, not a
  product defect. The shared `row()` helper runs `SELECT * ... fetchone()` with no filter,
  so from the second lifecycle it returned the first, committed row. The new test now sets
  a local `case.row` that returns the not-yet-committed (else newest) row. Nothing in the
  fixture module or in any assertion was edited or weakened; no runtime change, no digest
  re-record needed.
- The earlier signature/socket failures were sandbox-only: unsandboxed the live and
  product modules pass too.
- Run (`--jobs 6`, short TMPDIR, unsandboxed): the four U4 modules plus
  test_account_billing_admission, test_account_billing_execution, test_rsf_s5_probe,
  test_hold_lifecycle: 8 units, failed_units [], metadata_errors [], budget_failures [],
  requirement_failures []. Module seconds: hold_lifecycle 31.9, billing_admission 26.7,
  billing_execution 16.4, u4 product 9.3, u4 execution 6.4, u4 live 3.4.

Base verified: `8b959668`, branch `feat/u4-probe-capacity`; clean before work.
VERSION, runtime digest catalogue and all runtime files remain unchanged.
No commit is attempted in the sandbox. No installation, live-service operation,
merge or push. No test run remains in flight.

The requirement map preceded tests. There is **no semantic red-to-green claim**:
the new expiry check already passes on the base; signature and socket failures
prevent the other new tests from reaching their acceptance assertions.
No production change is justified by those environment failures.

## Requirement → test → evidence

All names below are in `tests`; module names are relative to that package.

| ID | Test | Evidence on the unchanged runtime |
|---|---|---|
| O0C-U4-1, expired reservations | `test_u4_probe_capacity.ProbeCapacityTests.test_expired_reservations_past_the_cap_release_once_with_a_live_control` | PASS twice, 1.03 s and 0.98 s module time. Three expired reservations against cap two; admitted-row control stays charged; exact-limit refusal; origin/history retained; fixed replay across reopen; repeat sweeps leave hold generations and release times unchanged. The control is explicitly a store row, not a running child. |
| O0C-U4-1, denied history | `test_u4_probe_execution_capacity.DeniedProbeCapacityTests.test_denials_past_the_cap_keep_the_admitted_control_and_replay` | HISTORICAL (sandbox round, superseded by the Fix round above; green unsandboxed): original signature return cannot complete in this sandbox. This two-test module exceeded the explicit 40 s worker cutoff during signature cleanup, so its result has no completed-test outcomes. No acceptance pass claimed. |
| O0C-U4-1, completed history | `test_u4_probe_execution_capacity.CompletedProbeCapacityTests.test_completed_history_past_the_cap_still_replays` | HISTORICAL (superseded by the Fix round above; green unsandboxed, now also replays the oldest and a middle identity): BLOCKED by the same signature execution restriction. Draft drives three real inert model completions, advances wall time across unchanged cooldown/reuse windows, checks current-input replay without another launch and unchanged sealed request history. Needs an unrestricted run. |
| O0C-U4-2 | `test_u4_probe_live_capacity.LiveProbeCapacityTests.test_a_running_model_stays_charged_while_reservations_expire` | HISTORICAL (sandbox round; superseded by the Fix round above): FAILED before launch: timed out waiting for the original signature return, 22.05 s including fixture cleanup. Running-child and settlement assertions were not reached. The final wait also requires the execution owner's child-ended proof before settlement; that small wait refinement is syntax-checked only. |
| O0C-U4-3 | No test yet: accepted 3.3 journal/reaper dependency absent | NOT RUN / NOT IMPLEMENTED. See dependency below. Existing journal-less P6 compatibility passes, but is not journal/reaper acceptance. |
| O0C-U4-4 | `test_u4_probe_product_capacity.ProductProbeCapacityTests.test_history_past_the_cap_allows_a_normal_claim_and_probe` | HISTORICAL (sandbox round; superseded by the Fix round above): FAILED in setup: collector endpoint could not be created, 1.94 s module time. No CLI acceptance assertion reached. The final draft additionally advances wall time after the claim and requires a second actual model launch, rather than reused billing; that refinement is syntax-checked only. |

The harmless platform preflight `sandbox-exec` running `true` exits 71 because
macOS refuses sandbox application with Operation not permitted. Signature-worker
threads retain unknown custody rather than returning a usable result. The real
collector fixture independently fails to create its Unix socket. These are
execution blockers, not evidence of a probe-capacity implementation defect.
No PTY check was needed or attempted.

## Named runs

- Initial baseline `codex:tests.test_account_billing_admission`: interrupted at
  60.60 s after no unit completed. Its signature fixtures cannot finish here.
  It is not a successful baseline or a product red.
- First new combined module: failed at explicit 50 s worker timeout. Its initial
  missing `why_not_scenario` metadata was repaired, and coverage was split into
  four short modules. No assertion was weakened to accommodate that failure.
- Four new modules, `--jobs 4`, explicit 40 s worker cutoff: **metadata_errors
  []** and budget_failures []; store test passes, product and live tests fail
  before their assertions, execution module times out. Worker wall 40.16 s,
  preflight 12.86 s. No successful under-load timing certification is claimed.
- Store gate: `codex:tests.test_u4_probe_capacity`,
  `codex:tests.test_rsf_s5_probe`, `codex:tests.test_binding_lookup`, `--jobs 3`:
  **13 tests PASS**, failed_units [], metadata_errors [], budget_failures [],
  requirement_failures []; worker wall 2.49 s, preflight 12.30 s.
  Module times respectively 0.98 s (one test), 1.78 s (seven), 2.49 s (five).
  This preserves the decided P6 boundary and existing indexed cap accounting.
- Final four new files parse successfully; `git diff --check` passes. No full
  suite, affected-set run or release-catalogue update was performed.

## Missing 3.3 dependency

The supplied base contains only the in-memory `OwnedExecutionRegistry` for
probe custody and P6's `close_orphaned_admitted` sweep. No probe launch journal
or cross-restart probe reaper exists in its tracked source. PLAN's C5 entry
mentions earlier off-branch work, but does not identify an accepted implementation
in this base. The owner was asked which accepted 3.3 commit U4 should consume.

`docs/requirements/rsf-s5.md`, decision 188, explicitly preserves journal-less
P6 release by deadline plus grace and forbids changing those assertions.
The requested 3.3 evidence path must therefore be supplied before U4 can test
restarted admitted lifecycles and evidence-based exactly-once release together.
Do not fabricate a second journal, silently change P6 or call the compatibility
sweep evidence of a stopped child.

## Resume

1. Supply the accepted 3.3 dependency in this worktree under the owner's merge
   policy. Read its journal/reaper interfaces and requirements before writing the
   missing O0C-U4-3 test. Exercise more restarted admissions than cap two, with
   one real running probe retained, stopping evidence and repeated/reopened
   settlement, then the normal CLI claim and fresh probe in the same campaign.
2. Run the existing draft process checks where macOS sandbox execution and local
   sockets work. Fix any fixture errors without weakening acceptance assertions.
   Obtain semantic red evidence before any necessary runtime change.
3. Run only the four named U4 modules and affected compatibility modules. For
   example, with `PY` set to the designated test interpreter:

   ```sh
   "$PY" -B scripts/parallel_tests.py --jobs 4 \
     --module codex:tests.test_u4_probe_capacity \
     --module codex:tests.test_u4_probe_execution_capacity \
     --module codex:tests.test_u4_probe_live_capacity \
     --module codex:tests.test_u4_probe_product_capacity
   ```

4. Record actual passing module timings well below 60 s. The four new timing
   entries are scheduling estimates, not passing measurements. If runtime
   changes become necessary, preserve VERSION/digests and expand named checks
   according to builder rules; any schema/shape change needs the full copier,
   backup, move and enumeration module set.

## Files and assertion accounting

Added `docs/requirements/u4.md`, this status and `u4/assertions.md`; four new
`test_u4_probe_*capacity.py` modules; `tests/requirements/u4.json`.
Updated only test registration in `scripts/test-layers.json`,
`test-stages.json`, `test-timings.json`, `test-requirements.json`.
No existing test assertion changed or removed. No schema, index, record shape,
release reason or operation-set change; copier/backup/move tests are therefore
not required by these edits. No absolute home paths or raw test logs are saved
in the documentation.
