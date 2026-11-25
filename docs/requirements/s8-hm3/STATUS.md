# S8 HM-3 builder handoff

Base ce81bd96 (0.8.67, IP-A), branch feat/s8-hm3. Changes remain in the worktree; no commit because this
is a sandboxed builder. VERSION and runtime-releases.json are unchanged. The implementation is ready for
primary review, subject to the integration and master-plan gates below; this step is not marked DONE.

| Requirement | Test evidence in tests.test_health_holds | Result |
| --- | --- | --- |
| S8-HM3-1 | test_account_orphan_is_reported; test_project_orphan_is_reported; test_missing_hold_is_reported | Initial missing-module red -> green; both registers checked, no release. |
| S8-HM3-2 | test_account_due_binding_is_reported_without_release; test_overdue_boundary_and_backoff; test_clock_floor | Missing overdue report red -> green; below/equal/above sweep bound, backoff and floor covered. |
| S8-HM3-3 | test_faulted_service_reported_then_stopped; test_service_stop_failure; test_healthy_service | Missing-module red -> green; report precedes close, no settlement, stop failures remain reported. Real D13 startup test blocked by sockets. |
| S8-HM3-4 | test_custody_drift; test_publication_material_is_checked; test_context_byte_budget_across_pages; test_context_byte_budget_does_not_combine_changed_snapshots; test_healthy_store | Missing-module and mixed-generation sum red -> green; context relationships, hashes, bytes and healthy state covered. |
| S8-HM3-5 | test_past_page_limit; test_account_projects_past_page_limit; test_past_legacy_evidence_limit; test_query_plans_and_read_only; test_unreadable_account_is_plain_machine_finding; test_deadline | Missing-module/helper and missing-account exception red -> green; actual run crosses project page bound; live rows cross source page bound; evidence crosses two-row limit; cap guard green. |
| S8-HM3-6 | test_scoping_and_private_text; test_registration; test_findings_survive_other_project_pass_and_say_when_stale | Missing registration and lost cross-project finding red -> green; project scope, fixed wording and stale readings covered. |

Requirements and the first failing tests preceded the new runtime module. No behaviour assertion from the
new tests was removed. Fixture corrections and the three existing registry expectation replacements are
mapped in [assertions.md](assertions.md). The original two checks remain in order; replacements require
[self_check, containers, holds] and the corresponding three healthy states.

Final green named run: tests.test_health_holds (23 tests, 1.172 s), tests.test_lifetime_cap_guard (4 tests,
0.776 s), tests.test_health_containers (19 tests, 1.118 s; one real-podman test skipped by its existing gate).
Parallel worker phase: 1.2 s; discovery: 3.226 s; preflight: 7.567 s. All six S8 requirement results are
passed, metadata_errors is [], budget_failures is [], failed_units is []. Sanitized structured evidence is
in [evidence.json](evidence.json); no raw logs or machine home paths are retained here.

The broader named run also included tests.test_health_monitor, tests.test_project_service_startup and
tests.test_runtime_release. Health monitor: 22 passed, seven socket setup errors. Project service startup:
all 13 stopped at socket setup (Operation not permitted), including the existing D13 successor-settlement
proof. Runtime release: two passed, only the current-version digest pin failed as expected. No test
assertion was weakened to bypass these failures. No PTY is needed by the new check. Linux/container,
unsandboxed affected, lifecycle and full-suite runs were not attempted by this builder.

The collector registers HM-3 through the one-line health_monitor.py extension; learning_store.py supplies
the state and keeps the container check's position. Runtime membership includes the new module. No table,
column, persisted record shape or account operation changed; the new reason strings are regex-validated
monitor findings, not new stored-record enum values. Copier/backup/migration gates are therefore not
triggered by a schema or record-shape change.

The collector has no project-lane fault endpoint. The service-side part emits the plain finding before
D13's close, preserves the existing writer_fault exit report, and leaves settlement to the successor.
The primary should confirm this reporting boundary during review and run the existing D13 integration
proof unsandboxed. Byte aggregates restart if the source changes; large totals need a stable interval to
complete. Findings persist while a clean recheck is still paging; stale observations explicitly say so.

Changed runtime files (under the orchestration codex_agents directory): health_holds.py (new),
health_monitor.py (one line), learning_store.py (wiring), project_service.py (D13 hook), runtime_files.py
(package membership). Changed tests: test_health_holds.py (new), test_health_monitor.py,
test_health_containers.py, lifetime_caps.json. Metadata: scripts/test-layers.json, test-stages.json,
test-timings.json, test-requirements.json; tests/requirements/s8-hm3.json (new). Documentation:
docs/requirements/s8-hm3.md and this directory's STATUS.md, assertions.md and evidence.json.

Primary reruns (with the test environment's Python, from repository root):

```text
python -B scripts/parallel_tests.py --module codex:tests.test_health_holds --module codex:tests.test_lifetime_cap_guard --module codex:tests.test_health_containers
python -B scripts/parallel_tests.py --module codex:tests.test_health_monitor --module codex:tests.test_project_service_startup --module codex:tests.test_runtime_release
```

Remaining per-step gates: primary unsandboxed affected run against ce81bd96, affected lifecycle flows on
macOS and Linux, other-provider review and confirmation, merge after S5 with the registration wiring,
and quiet full suites on the merged batch. The primary owns the release version and digest update.
