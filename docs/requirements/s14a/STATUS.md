# S14a builder status

Base `ce81bd96` (0.8.67), branch `feat/s14a-capture`. Changes are uncommitted because this builder is sandboxed. VERSION and the digest catalogue are unchanged. Builder checks are complete; primary integration, review and merge gates remain open.

## Requirement evidence

All short test names below are methods of `test_admission_capture_bytes.AdmissionCaptureBytesTests`, except the explicitly named anchored claim test. The complete requirement links are registered in `tests/requirements/s14a.json` and `scripts/test-requirements.json`.

| Requirement | Test | Evidence |
| --- | --- | --- |
| S14A-1 | `test_doubled_peak_four_accounts_and_sixteen_windows_fit_with_twenty_percent_margin` | RED before runtime changes: 4,737,875 bytes > 1,677,721 target. Strengthened to SHA-256 account fingerprints: initial packing alone was RED at 1,764,392. GREEN after packing and actual metadata sharing: 1,542,869 bytes, 554,283 bytes spare (26.4%), 3,768 rows, 328 spare. Both providers and anchored captures pass the same margin check and independent oracle. |
| S14A-2 | `test_packed_content_is_lossless_bounded_and_rejects_corruption`, `test_every_stored_byte_and_decision_input_survives_packing` | Codec test initially RED for absent implementation. Every captured original content byte equals its stored counterpart after reconstruction; detached decoded records and all prepared decision inputs are equal. Truncation, trailing/concatenated streams, over-expansion and wrong metadata dictionaries are refused. Metadata identity was RED before pooling, then GREEN; separately allocated equal copies are refused rather than undercharged. |
| S14A-2 | `test_expanded_byte_bound_is_checked_before_json`, `test_open_and_probe_appended_record_recaptures_remain_exact` | Aggregate expansion test RED before guard, GREEN afterward. Per-record expansion stays within 65,536 bytes; all original content within one snapshot is bounded by the existing 64-MiB capture maximum before any JSON decode. Original open/probe appended rows compare exactly in the packed representation and changed expectations refuse as stale/conflicting. |
| S14A-3 | `test_U2_1_the_tool_equals_the_real_jobs_charge_and_follows_a_product_change`, `test_U2_3_records_exactly_at_each_cutoff_and_older_evidence_in_effect_are_charged_alike`, `test_real_two_mib_boundary_still_refuses_one_byte_over` | GREEN. Real intake charge equals sizing tool and independent oracle. Exact bytes/rows are accepted; one byte or row less is refused. Signature omitted/doubled mutations still cause the intended parity failure. Inclusive cutoffs, old live headroom, first Claude pass and older Claude readings remain covered. The actual 2-MiB boundary fixture also passes the full closed-record decoder. |
| S14A-3 | `test_admission_capture_claim.AdmissionCaptureClaimTests.test_U2_4_a_claim_anchored_at_its_open_is_charged_alike` | Ported unchanged from accepted S14 into its own scenario module. Primary must rerun: the pre-change helper path timed out at its existing 10-second ceiling here. No timeout assertion was relaxed. |

## Final sourced envelopes

The S14 cadence, counts and 387-run peak are retained. Account fingerprints now use deterministic SHA-256 values so low-entropy identifiers cannot inflate compression savings. These are synthetic captures, not completed native launch flows. The table reports Codex intake; the combined test additionally checks Claude and anchored captures. Every measured envelope agrees with the real intake and independent oracle. Limits remain 4,096 rows and 2,097,152 bytes; 24 slots still reserve 48 MiB.

| Envelope | Rows | Retained bytes | Byte margin |
| --- | ---: | ---: | ---: |
| base | 1892 | 742,553 | 1,354,599 |
| no_probes | 1250 | 525,008 | 1,572,144 |
| four_accounts | 2978 | 1,216,755 | 880,397 |
| largest_payload | 1892 | 773,621 | 1,323,531 |
| combined_double_peak | 3768 | 1,542,869 | 554,283 |
| double_peak | 2682 | 1,006,270 | 1,090,882 |

Full numeric evidence: [measurement-admission.json](measurement-admission.json). The first binding limit is now rows in the measured brackets; the measurement script reports `job_limit_binds`. Original content byte totals in the sizing tool are diagnostic, not a lower bound on packed retained bytes.

## Reader audit and representation

No candidate, field, window, provenance component, timestamp, signature, duplicate evidence occurrence, digest, order column or citation is dropped or summarized. Thus no field is being claimed unused. The change is lossless storage of owner-local copies; persistent records and public/wire record shapes are unchanged.

- `account_service._capture_admission_rows_locked`: unchanged SQL candidate scope and ordering. Headers validate original storage lengths and charge actual packed lengths plus one copy of shared project/type/source strings. SQL windows operate on the already bounded candidate set. Original expanded bytes are independently bounded. The packer sees at most one original record at a time and never parses JSON. Actual captured strings share object identity within that snapshot, with no global intern table.
- `admission_input_decode.decode_admission_inputs`: verifies actual sharing and the whole retained-byte budget, then bounded reconstruction, original SHA-256, canonical encoding, closed record shape and metadata binding. Returned `ParsedAdmissionRow.raw` remains the exact captured tuple for worker custody checks.
- `claim_admission.prepare_claim_admission`: receives identical decoded records; billing, headroom, attestations, identities, admissions and reviewed versions compare equal to the uncompressed model. `admission_metadata` classification and window-start outputs compare equal across clocks. No decision reader needs an altered field.
- `claim_admission.admission_signature_rows`: attestations are deliberately raw; signature input and its second charge are unchanged.
- `account_original_open._fresh_admission` and `account_billing_admission.AccountBillingAdmission._fresh`: compare newly appended canonical records through the same deterministic lossless packing. Full stale-input row comparison in `same_admission_inputs` is unchanged.
- `signature_worker_pool`: raw tuple identity and the original worker-result custody checks are preserved. No new authority, reason code or account operation is introduced.

Reader search used `rg --hidden` for `AdmissionInputSnapshot`, `snapshot.rows`, `admission_input_size`, raw content index readers, and provider billing/headroom consumers across the runtime. All raw-content consumers are addressed above. Assertion and fixture changes are mapped in [assertions.md](assertions.md).

## Named verification

- Final capture module: 8 tests PASS, 19.6 s, `metadata_errors: []`, no budget failure. Global metadata validation includes both new scenario modules, their requirement sources, stages and layer registrations.
- Affected named run: 94 tests, 42.3 s; all 91 tests in the other seven modules PASS (`test_admission_capture_bytes`, `test_admission_inputs`, `test_admission_input_decode`, `test_admission_metadata`, `test_signature_job_intake`, `test_lifetime_cap_guard`, `test_read_growth`). `test_runtime_release` has two passing checks (including exact sorted runtime file set) and only the expected unchanged-version digest-pin failure.
- Compatibility named run: 135 tests, 80.8 s. `test_owner_backup_streamed`, `test_learning_source_image`, `test_learning_source_image_legacy`, `test_backup_installed_project`, `test_large_project_backups`, `test_account_observation_backup` and `test_backup_reply_private` pass their functional checks. The migration module has the two CLI failures below. Owner streamed backup totals 65.9 s over two fragments, exceeding its 60-s budget; that performance issue was already assigned to S13c in the accepted S14 handoff. No timing assertion or budget was relaxed.
- No full suite, affected expansion, socket/PTY lifecycle run, install or live-service mutation was performed. The migration CLI module attempted unavailable service paths; those cases are unresolved and require primary rerun. No baseline equivalence is claimed for those failures. `ruff` is absent from the supplied test environment; syntax compilation and `git diff --check` are used locally.

Durable structured summaries, without raw logs or private paths: [test-evidence.json](test-evidence.json).

## Primary reruns and gates

Use the supplied test interpreter as `$PY`, an unsandboxed environment and a short clean temporary directory for socket tests. Run named groups separately; do not run tests while this worktree is being edited.

1. `codex:tests.test_admission_capture_claim` and `codex:tests.test_admission_history_growth`: anchored real claim and all original C1 open/claim boundary and restriction tests. The new byte-boundary fixture is valid under the full decoder and keeps every original boundary assertion.
2. `codex:tests.test_account_original_open`, `codex:tests.test_account_claim_jobs`, `codex:tests.test_account_billing_admission`, `codex:tests.test_account_billing_jobs`, `codex:tests.test_billing_rejudge`: actual worker publication, exact recapture and rejudge flows.
3. `codex:tests.test_signature_worker_pool`, `codex:tests.test_signature_inputs`, `codex:tests.test_account_host`: worker custody, helper execution and real socket integration. No PTY-specific test was run here.
4. Rerun `tests.test_learning_migration.MigrationCliTests.test_first_start_of_the_project_service_moves_the_history_by_itself` (returned `orchestration_client_unavailable`) and `test_command_moves_reports_refuses_and_rolls_back` (returned the moved-project-store error). Their offline sibling cases passed. Rerun the owner-backup budget on the primary's quiet batch as part of S13c.
5. Primary runs `scripts/parallel_tests.py --affected ce81bd96` outside the sandbox, affected macOS/Linux lifecycle flows, other-provider admission-security review, and the batch macOS/Linux full-suite gate. The primary refreshes VERSION/catalogue at merge; only then can the release pin turn green.

Example named invocation: `$PY -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_admission_capture_claim --module codex:tests.test_admission_history_growth`.

## Files changed

- `docs/requirements/s14a.md`
- `docs/requirements/s14a/STATUS.md`
- `docs/requirements/s14a/assertions.md`
- `docs/requirements/s14a/measurement-admission.json`
- `docs/requirements/s14a/test-evidence.json`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/account_billing_admission.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/account_original_open.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/account_service.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/admission_capture_content.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/admission_input_decode.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/admission_inputs.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/learning_store.py`
- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/runtime_files.py`
- `packages/codex-agents/scripts/admission_capture_size.py`
- `packages/codex-agents/scripts/measure_s14_admission_day.py`
- `packages/codex-agents/tests/lifetime_caps.json`
- `packages/codex-agents/tests/test_admission_capture_bytes.py`
- `packages/codex-agents/tests/test_admission_capture_claim.py`
- `packages/codex-agents/tests/test_admission_history_growth.py`
- `packages/codex-agents/tests/test_admission_inputs.py`
- `scripts/test-layers.json`
- `scripts/test-requirements.json`
- `scripts/test-stages.json`
- `scripts/test-timings.json`
- `tests/requirements/s14a.json`
