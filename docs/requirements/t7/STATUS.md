# T7 S0 status (WIP checkpoint, branch feat/t7-s0, base 8d4a7ce3 = 0.8.55; nothing pushed)

Done: e2e-t7-l1 merged (requirements.json resolved); World.cli() runs the INSTALLED cli.py, raises for any other
program, guard tests in tests/test_e2e_strictness.py (InstalledCliTests; red if a check names the source CLI);
checks split into scripts/e2e_checks_{setup,owner_key,billing,routing,run,general}.py + scripts/e2e_core.py;
--jobs N (one scene group per process; L1-L5 parallel 4 jobs = 1m47 vs 5m36 serial before the split);
declared_checks() and scripts/parallel_tests.py scan the modules; G2/G3/G4 added on the L5 scenes, read from the
project history sqlite (read-only) and the verifier's reserved input; CI job lifecycle-e2e added (ci.yml).
Last full parallel run (par1, before the last commit): all green except G3, G4, G8 (fixed in the last commit,
not yet re-verified by a full run). G5, G6, G7 have no check yet (other slices): listed on stderr, not judged
in --flows runs.
Forced assertion changes (old -> new, reason): docs/requirements/t7/assertions.md (L4.2 provider edit, L2.2
status fingerprint, G8 owner screens, G9 CI job).
Product findings for the primary: (1) L4.2 provider edit is admitted with the installed route, not refused
(cli.py learning-open-host-run, designed); (2) setup's key screen shows ssh-keygen's fingerprint and home paths:
owner decision needed whether screens may show them (G8 allows it on screens, forbids passphrase/key material).
Verified (2026-11-24): L1-L5 --jobs 4 full run green (report passed=true; G1-G4, G8, G9 and all L checks pass; 5m12 wall),
tests.test_e2e_requirements/strictness/ci_stages (19) and test_test_framework ReviewLifecycleTests (4) green.
Pending primary decisions: the 4 changes in assertions.md (L4.2, L2.2, G8, G9) are untouched.
Next: first CI run of lifecycle-e2e decides if continue-on-error goes; G5-G7 checks belong to other slices.

## Fix round 1 (Codex review NOT-READY, 2026-11-24)
Fixed: (1) lifecycle links validated against tests/e2e/requirements.json by parallel_tests.lifecycle_link_errors()
(not fed into the shared catalogue); G8/L1.2 statements synced into scripts/test-requirements.json; metadata()
35 errors -> 0. (2) local _SCENES removed from e2e_checks_setup.py; SceneCleanupTests guard. (4) accept_worker():
exactly one entry per name and exit 0, else every check of the group fails. (5) same_digest() in G3/G4. (6) G1/G8
judged at capture for every World (e2e_support.capture_breaks, CAPTURE_BREAKS; _execute fails the capturing check
and decides G1/G8); screens incl. record=False checked for passphrase, public blob and private key material.
(7) redact_entries(): a leaking worker's entries replaced whole. (8) assertions.md baseline 342df324, rows fixed,
L2.2 asserts no fingerprint after replacement. (9) lifecycle report upload if-no-files-found: error (G9 asserts it).
Tests: tests.test_e2e_strictness (23, 15 new: red first, then green), test_e2e_requirements, test_ci_stages (34, 0.8 s).
Verified: L1-L5 --jobs 4 run r2 passed=true, 33/33 checks, 6m13 wall; no ~/.cache/e2e-* left by the run (3 older ones from
22:25-22:32 predate the _SCENES fix, left in place).
