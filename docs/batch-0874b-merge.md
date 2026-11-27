# Batch 0.8.74 re-cut (branch merge/batch-0874b)

Base d47ab6b7 (0.8.74 batch, never installed). VERSION stays 0.8.74; the runtime digest is refreshed in place
(decision by the primary: 0.8.74 was never installed). The 0.8.73 record, floor and VERSION are unchanged.

## Merged (CI triage reviews/040-ci-0874-triage/TRIAGE.md, decision 405)
- fix/0874-ci-env: G1 ruff (non-runtime files), G2 private XDG_RUNTIME_DIR step in the Linux CI jobs, G3 e2e harness probes a trusted python3.
- fix/0874-ci-speed: G5 RS-10-DUE batch cost, G6 s13a lease pass (verification once per unchanged row, 8.1 s -> 0.6 s), G7 setup_wizard_source and routing golden modules (routing moved to the integration stage); no budget raised (decision 405).
- fix/0874-ci-prod: G4 RecursionError in every json loader (user_settings.load first), G8 C3 settling retry, G9 api13 diagnostics (test side only); ruff runtime lint (G1 part) changed the shipped runtime.

## Digest refresh
The ci-prod runtime edits changed the collector runtime digest, so `runtime-releases.json` 0.8.74:
f86147a3... -> 11fd46b264b830919b4b967fcefc00f554cf811f0b8cd3ab1d356fca5ee5f32c (the only place that pins it;
`test_runtime_release` proves it). `scripts/generate_test_metadata.py --catalogue` ran twice: no change, byte-identical; no entries removed.

## Test-side fix found by the full run
G2 added a `sudo install` step before "Select trusted review base", so two tests that took the FIRST `run: |` block of a CI job
executed sudo. Both now select the step named "Select trusted review base" (same requirement TF-1/TF-5, same assertions):
`test_test_framework_r3.test_p05_CI_selects_trusted_bases_for_supported_events`,
`test_test_framework_guards.test_B5_missing_base_fails_and_advanced_PR_base_uses_merge_base`. Re-run of both modules: green, metadata_errors [].

## Full run (`--base d47ab6b7`, real sockets; docs/batch-0874b-full-run.txt, redacted)
Units 574; the run failed only the two modules above (before the fix); budget_failures []; metadata_errors [].
