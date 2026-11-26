# Batch 0.8.73 (install point IP-G, branch merge/batch-0873)

Base 6edd6a79 (accepted 0.8.72, decision 393, not installed). `version/0.4.0` not moved, no push.

## Merges
- `feat/s14g-0873` @81afbbf2 (decision 395) -> fast merge, no conflicts.
- `feat/gen-0873` @6a777ed4 (395) -> conflict only in scripts/test-requirements.json (generator normalises ordering); resolved by
  taking the branch copy and regenerating (4f7937cf).
- `feat/prepfind-0873` @1bb3b3b2 (396) -> ab15dd5e, registry JSONs merged automatically.
- scripts/test-requirements.json then REGENERATED with `scripts/generate_test_metadata.py --catalogue` (the generator's own
  output, includes S14g and PREP entries); running it again gives no diff (idempotent).

## Bump 0.8.73
VERSION 0.8.73; runtime digest b4c07d73... in runtime-releases.json; CHANGELOG; previous-release record for 0.8.72 (source
6edd6a79, Claude package compiled with `packaging.compile_bundle` from `git archive 6edd6a79`, bundle digest eb34209d...
unchanged; PINNED comment extended); catalogue 32 -> 33. Floor 0.8.73 (0.8.68..0.8.72 refused).
Assertions changed (old -> new), reason: floor and version move with the release:
- `test_b4t_takeover.FloorTests.test_a_store_written_by_this_build_refuses_0868_0869_0870_and_0871` renamed `..._0870_0871_and_0872` (also in scripts/test-requirements.json); stored floor '0.8.72' -> '0.8.73'; refused builds add '0.8.72'.
- `test_b4t_native_p69`: every 0.8.72 -> 0.8.73 (proves 0.8.69 -> 0.8.73 natively); tests/fixtures/b4t/README.md text.
- `test_previous_release_catalogue`: 32 -> 33 (twice).
Green through the runner (metadata_errors [], failed_units [], 15.4 s): test_runtime_release, test_previous_release_catalogue,
test_previous_release_recorded, test_b4t_takeover, test_b4t_native_p69, test_candidate_evidence_lookup, test_lifetime_cap_guard.
`python -m unittest tests.test_generate_test_metadata` passes (3 tests, 6 s) when run directly.

## Full run (HEAD before this note: bump commit, `--base 6edd6a79`, real sockets; redacted /HOME/ and <user>)
Output docs/batch-0873-full-run.txt, timings docs/batch-0873-full-run-timings.json (+ .run.json).
exit 1; units 574; elapsed 609.9 s; preflight 8.11 s; metadata_errors []; budget_failures []; failed_units
[root:test_generate_test_metadata]. test_project_start 145.9 s (ceiling 183.4 s, debt), test_run_retention_scale 14.9 s.
Over-budget modules (billing_gate, bridge, host_commands, learning_cli, lost_run_settlement, project_driver, project_start, ...)
are recorded budget debt, all inside their ceilings.

## Failure classification (not fixed, per instructions)
`root:test_generate_test_metadata` FAILS through the runner, alone too (reproduced with `--module root:test_generate_test_metadata`):
"component test root:test_generate_test_metadata: import must not start a process (subprocess.Popen)". The runner's component
layer forbids process starts, and the new test runs the generator with `subprocess.run` (tests/test_generate_test_metadata.py
`generate`). It passes under plain unittest, so it is a runner-layer/test-design problem introduced by feat/gen-0873, not a
product regression. The test does not exist at base 6edd6a79, so there is no base comparison. Needs a fix on the gen branch
(call the generator in-process, or register the module in a layer that may start processes) before install.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
- IP-G generator test fix: root:test_generate_test_metadata moved to the scenario layer (it starts processes for discovery); product (native-package) files unchanged since the full run.
