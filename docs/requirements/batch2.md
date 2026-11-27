# Batch 0.8.57b integration (merge/batch-0857b: 0.8.56 + B4s + T7 S1)

Status: see the bottom of this file.

## 1. Merge of feat/rsb-b4s-settlement (conflicts)
- `orchestration_store.py` `_unfinished`: both sides kept. B4s's `_settled(binding)` helper and its skip
  (RS-B-B4-5: a reservation with a verified settlement row is finished, checked before the consumption check)
  plus RS-D S2's `unrecorded=None` keyword and its append on the verified-account-release branch (RS-D-2-N1/N2).
  A settled reservation is not counted as unrecorded (it never reaches the release branch).
- `scripts/test-requirements.json`: union (RS-B-B4m-1..3, RS-D-2-N1/N2 from HEAD; RS-B-B4 from B4s).

## 2. B4s x B4m x RS-F S1
- `reply_shapes.REPLY_FIELDS` gains `account.lost_run_closure` = exactly `lost_run_closure._FIELDS` (no optional).
  Without it B4m's shape guard refused every real closure (red: test_account_settlement_wire
  test_B4_1_a_matching_closure_decodes and the new table test errored; green after).
- `test_reply_shape_live.DRIVERS` (outcomes/lanes) gains `test_account_settlement_driver` so the op is produced live.
- `account.lost_run_closure` is in `ACCOUNT_OPERATIONS` (not a read op), so RS-F S1's runtime gate covers it.
  New module `tests/test_batch2_b4s_integration.py` (scenario/integration; same store fixture as test_rsf_s1_ingress):
  table row == producer fields, settle_lost_run row unchanged, missing/wrong runtime refused before the authorizer
  with zero rows changed.

## Assertion changes (old -> new)
- tests/test_rsf_s1_ingress.py `test_RSF_S1_2_every_account_mutation_refuses_missing_or_wrong_runtime_before_authorizer`:
  `assertEqual(15, len(mutations))` -> `assertEqual(16, len(mutations))`. Reason: B4s adds the account mutation
  `account.lost_run_closure` (RS-B-B4-1, a new operation by B4m rule); RS-F S1 requires every account mutation
  gated, so the inventory grows 23 -> 24 (docs/requirements/rsf-s1.md updated). The loop body is unchanged and now
  also covers the new op.
- tests/test_reply_shape_live.py DRIVERS: one module added (no assertion removed).

## 3. Merge of feat/t7-s1
- Clean merge (ee1f4f1d). New root module tests/test_e2e_run_control.py registered as root:test_e2e_run_control,
  stage integration, layer scenario (it starts a stand-in Python process). L6.1/L6.2 e2e checks are expected red
  until fix/l6-findings merges.

## Status
- Commits: 6520e2fd (B4s merge), b2b5d68c (B4s x B4m x RS-F S1), ee1f4f1d (T7 S1 merge), then the registration/run commit.
- Runs (named modules, clean env): settlement trio + lost_run_settlement + batch2 + rsd_runtime_close + reply_shape_compat
  + rsf_s1 x2: 119 tests 116 s OK; journals/container/claim_denial/container_stop/holds_schema/result_lifetime/cap_guard/
  admission_inputs/signature_inputs/signature_job_intake: 315 tests 150 s OK; open_withdrawal/b2_service_outcome/
  holds_report: 127 tests 46 s OK; root test_test_stages/e2e_requirements/e2e_strictness/e2e_run_control: 68 tests 4 s OK.
- test_project_driver alone: 93 tests 225 s OK. test_reply_shape_live alone (now replays test_account_settlement_driver
  too): 5 tests 596 s OK (over the 5-min single-run guide; it is one module replaying 31 drivers).

## 4. Decision 167: dispatch-preflight refusals say admission_created false (L6.2)
- Requirement: every refusal reply of `cli.py dispatch-preflight` (BridgeRefused, e.g. "assignment read path is not
  committed", and ContractError input refusals) carries `admission_created: false`. Product fix in cli.py main(); the
  e2e check is unchanged.
- Test: tests/test_dispatch_preflight.py test_cli_refusal_of_an_uncommitted_read_path_says_it_admitted_nothing
  (red KeyError 'admission_created', then green).
- Assertions changed: none. reply_shapes.py has no table for the preflight refusal (checked); no other test pins that reply.
- runtime-releases.json: only the 0.8.57 entry refreshed (ffc6ac85... -> 3f66f3ae...); 0.8.55/0.8.56 untouched.

## 5. Full-run 0.8.57 failures (three)
1. codex:tests.test_route_exhausted_open (BridgeRefused "project repository cannot be trusted for export" at open).
   Cause: the L6.2 open check (cli.py learning-open-host-run) ran the WHOLE committed-export inspection, so open
   also refused repository-trust conditions it never refused before and that dispatch-preflight refuses later.
   Product fix: `_refuse_uncommitted_read_paths` runs the same inspection at the same three points but leaves
   `REPOSITORY_TRUST_REFUSALS` ("project repository cannot be trusted for export", "project root is not the top of
   a git repository") to dispatch-preflight's full check, which is unchanged. Every read-path refusal (not committed,
   not granted, symlink, ...) still refuses at open before host start/admission, so the existing L6.2 driver tests
   (test_project_driver -k test_l6_2, which use an uncommitted AND ungranted path) stay green unchanged.
   Narrowing to the "not committed" reason alone was tried and broke those two tests, so it was not kept.
   Test (new): test_dispatch_preflight test_open_time_read_path_check_leaves_repository_trust_to_dispatch
   (red: no such helper; green).
2. codex:tests.test_learning_cli test_trusted_host_lifecycle_opens_and_records_exact_assignment (KeyError
   reason_code). Cause: the fixture project is `git init` with NO commit, so the assignment's read path source.txt is
   genuinely uncommitted; L6.2 (open refuses an uncommitted read path before host start, any backend call or
   admission) now refuses it before the API-key admission check the test exercises, and the later successful open
   in the same test could never pass. The product is right per L6.2. Setup fix only (no assertion changed): the test
   writes and commits source.txt before its first open.
   Product fix for the missing code: a BridgeRefused reply for "assignment read path is not committed" now carries
   `reason_code: read_path_not_committed` (open and dispatch-preflight share the handler; the T7 L6.2 check accepts
   that code). Test (new): test_dispatch_preflight test_cli_refusal_of_an_uncommitted_read_path_names_its_reason_code
   (red KeyError 'reason_code'; green). Other BridgeRefused reasons (about 60 fixed strings) still have no
   reason_code: mapping them all is a separate decision (one test pins the exact refusal dict:
   test_dispatch_delivery_cli line 174).
3. codex:tests.test_reply_shape_live (10-minute worker timeout). Decision 175: it is a release-gate proof.
   Mechanism used: the existing `soak` layer (scripts/test-layers.json scenario -> soak). Default ci/dev profiles,
   so every local full run and --affected run, skip it; `--profile soak --module codex:tests.test_reply_shape_live`
   runs it. Runner: under --profile soak the worker timeout defaults to the soak budget (900 s) instead of 600 s
   (COCOLO_WORKER_TIMEOUT still overrides). CI: new job `release-replay` (main, tags, nightly; not
   continue-on-error). Documented in TESTING-framework.md (Release-gate replays) and CONTRIBUTING.md (release gate).
   Drivers: observed() now wraps the whole replay in trusted_tools(fake_provider_tools(...)) (the same setup the
   drivers' BridgeTestCase uses), so an unpatched driver resolves only the fake clients; a real client binary
   cannot be found. The hang in the log was inside a driver's fake-client observation at the 600 s mark, i.e. the
   replay's total time, not a real binary (BridgeTestCase already patches the trusted path).

### Assertion / setup changes (old -> new)
- tests/test_learning_cli.py test_trusted_host_lifecycle_opens_and_records_exact_assignment: SETUP only.
  old: project is an empty `git init` (read path source.txt never committed) -> new: the test writes source.txt and
  commits it before the first open. No assertion changed.
- tests/test_reply_shape_live.py LiveReplyShapeTests.observed: SETUP only. old: drivers replayed with the default
  trusted search path -> new: replayed inside trusted_tools(fake clients). No assertion changed.
- scripts/test-layers.json codex:tests.test_reply_shape_live: scenario -> soak (selection, decision 175). Its
  RS-B-B4m-2 checks are unselected (not passing) in ci-profile runs; the other RS-B-B4m-2 checks still run there.
- runtime-releases.json: only the 0.8.57 entry refreshed (3f66f3ae... -> f287792a...); 0.8.55/0.8.56 untouched.

### Runs (clean env, named modules)
- test_dispatch_preflight + test_route_exhausted_open + test_learning_cli: 60 tests 65 s OK (after final narrowing:
  dispatch_preflight + route_exhausted_open + the learning_cli test 31 tests 10 s OK; test_project_driver -k test_l6_2 4 OK).
- test_bridge + test_runtime_release: 171 tests 206 s OK. Root test_test_stages + test_e2e_run_control + test_ci_stages:
  49 tests 3 s OK.
- `parallel_tests.py --profile soak --module codex:tests.test_reply_shape_live`: 5 tests, unit 636 s OK, no requirement
  failures. Runner exit 1 only from 50 pre-existing METADATA errors ("RS-B-B4 does not list its check",
  test_account_settlement*/driver), the same 50 as in the 0.8.57 full-run log: not from this change; for the primary.
