"""The exact files that make up the orchestration runtime, decided by this package.

The owner's Codex command rule runs a copy of these files, and nothing else, from
an owner-only directory, and their digest is the runtime identity the collector
authorizes and `runtime-releases.json` records. The list is a constant of the
code doing the work, never read from a project's install manifest, which a
session with write access to the project could extend. A test keeps it equal to
the package's own files.

Paths are relative to `.codex/orchestration/`. Signed release data
(`config/learning-releases.json`) and the hook script are package files but not
runtime code: a protected copy reads release data from its project, and Codex
runs the project's own `learning_hook.py` (named absolutely by the checkout's
rendered `.codex/hooks.json`), never the copy's. That file sits in the project's
`.codex`, which Codex keeps read-only for a session opened on the project and the
session guard refuses for one opened above it, like `hooks.json` itself; a process
already writing the project before the install (when `.codex` did not exist) is the
exception, which is why the installer refuses extra files there and the owner closes
every Codex session first.
`hook_config.py` renders and checks that file; validation imports it, so it is
runtime code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

ORCHESTRATION_PREFIX = ".codex/orchestration/"
RUNTIME_FILES = (
    "cli.py",
    "codex_agents/__init__.py",
    "codex_agents/account_api.py",
    "codex_agents/account_billing_admission.py",
    "codex_agents/account_billing_execution.py",
    "codex_agents/account_billing_jobs.py",
    "codex_agents/account_claim_contracts.py",
    "codex_agents/account_claim_jobs.py",
    "codex_agents/account_cleanup.py",
    "codex_agents/account_cleanup_schema.py",
    "codex_agents/account_host.py",
    "codex_agents/account_host_open.py",
    "codex_agents/account_ingress.py",
    "codex_agents/account_lost_run.py",
    "codex_agents/account_observation.py",
    "codex_agents/account_observation_failure.py",
    "codex_agents/account_observation_jobs.py",
    "codex_agents/account_observation_schema.py",
    "codex_agents/account_open_admission.py",
    "codex_agents/account_open_contracts.py",
    "codex_agents/account_open_schema.py",
    "codex_agents/account_original_open.py",
    "codex_agents/account_restart.py",
    "codex_agents/account_run_provenance.py",
    "codex_agents/account_service.py",
    "codex_agents/active_run_capacity.py",
    "codex_agents/admission_evidence_schema.py",
    "codex_agents/admission_input_decode.py",
    "codex_agents/admission_inputs.py",
    "codex_agents/admission_metadata.py",
    "codex_agents/atomic_rename.py",
    "codex_agents/billing_probe_contracts.py",
    "codex_agents/billing_probe_schema.py",
    "codex_agents/billing_probe_worker.py",
    "codex_agents/bridge.py",
    "codex_agents/bridge_liveness.py",
    "codex_agents/capacity_status_contracts.py",
    "codex_agents/catalog.py",
    "codex_agents/claim_admission.py",
    "codex_agents/claim_denial.py",
    "codex_agents/claim_storage.py",
    "codex_agents/claude_activity.py",
    "codex_agents/claude_host.py",
    "codex_agents/cleanup_contracts.py",
    "codex_agents/cli_reports.py",
    "codex_agents/client_runtime.py",
    "codex_agents/codex_activity.py",
    "codex_agents/codex_preparation.py",
    "codex_agents/codex_worker.py",
    "codex_agents/collector_client.py",
    "codex_agents/collector_contracts.py",
    "codex_agents/collector_exit.py",
    "codex_agents/collector_paths.py",
    "codex_agents/collector_runtime.py",
    "codex_agents/compatibility_message.py",
    "codex_agents/container_launch.py",
    "codex_agents/container_recovery.py",
    "codex_agents/container_runtime.py",
    "codex_agents/contracts.py",
    "codex_agents/control_api.py",
    "codex_agents/control_loop.py",
    "codex_agents/control_service.py",
    "codex_agents/core_api.py",
    "codex_agents/dispatch_api.py",
    "codex_agents/egress_proxy.py",
    "codex_agents/evaluation.py",
    "codex_agents/event_shapes.py",
    "codex_agents/execution_activity.py",
    "codex_agents/execution_grant.py",
    "codex_agents/gate_policy.py",
    "codex_agents/git_plumbing.py",
    "codex_agents/hardened_git.py",
    "codex_agents/health_containers.py",
    "codex_agents/health_monitor.py",
    "codex_agents/history_api.py",
    "codex_agents/history_contracts.py",
    "codex_agents/history_service.py",
    "codex_agents/holds_report_contracts.py",
    "codex_agents/hook_config.py",
    "codex_agents/host_commands.py",
    "codex_agents/host_open_contracts.py",
    "codex_agents/host_paths.py",
    "codex_agents/host_result_contracts.py",
    "codex_agents/hosts.py",
    "codex_agents/lane_health.py",
    "codex_agents/launch_journal.py",
    "codex_agents/learning_adapters.py",
    "codex_agents/learning_analysis.py",
    "codex_agents/learning_contracts.py",
    "codex_agents/learning_cycle_core.py",
    "codex_agents/learning_eligibility.py",
    "codex_agents/learning_engine.py",
    "codex_agents/learning_migration.py",
    "codex_agents/learning_source_image.py",
    "codex_agents/learning_source_transfer.py",
    "codex_agents/learning_store.py",
    "codex_agents/ledger_waits.py",
    "codex_agents/legacy_slots.py",
    "codex_agents/liveness_clock.py",
    "codex_agents/lost_run_closure.py",
    "codex_agents/lost_run_contracts.py",
    "codex_agents/merge_library.py",
    "codex_agents/observation_failure_contracts.py",
    "codex_agents/open_scope_contracts.py",
    "codex_agents/open_withdrawal.py",
    "codex_agents/operational_qa_contracts.py",
    "codex_agents/orchestration_api.py",
    "codex_agents/orchestration_schema.py",
    "codex_agents/orchestration_service.py",
    "codex_agents/orchestration_store.py",
    "codex_agents/owned_execution.py",
    "codex_agents/owner_key_contracts.py",
    "codex_agents/owner_record_payload.py",
    "codex_agents/phase_report.py",
    "codex_agents/preparation_reasons.py",
    "codex_agents/prepared_dispatch.py",
    "codex_agents/process_tree.py",
    "codex_agents/project_backup.py",
    "codex_agents/project_capacity_report.py",
    "codex_agents/project_cleanup.py",
    "codex_agents/project_driver.py",
    "codex_agents/project_history.py",
    "codex_agents/project_holds_report.py",
    "codex_agents/project_holds_schema.py",
    "codex_agents/project_host_client.py",
    "codex_agents/project_host_open.py",
    "codex_agents/project_ingress.py",
    "codex_agents/project_learning_activation.py",
    "codex_agents/project_learning_chunks.py",
    "codex_agents/project_learning_corpus.py",
    "codex_agents/project_learning_identity.py",
    "codex_agents/project_learning_live.py",
    "codex_agents/project_learning_owner.py",
    "codex_agents/project_learning_qa.py",
    "codex_agents/project_learning_source.py",
    "codex_agents/project_monitor.py",
    "codex_agents/project_open_scope.py",
    "codex_agents/project_operational_qa.py",
    "codex_agents/project_orchestration_report.py",
    "codex_agents/project_qa_checks.py",
    "codex_agents/project_qa_commit.py",
    "codex_agents/project_release_ack.py",
    "codex_agents/project_request_queue.py",
    "codex_agents/project_result_contexts.py",
    "codex_agents/project_routing_evidence.py",
    "codex_agents/project_runtime.py",
    "codex_agents/project_service.py",
    "codex_agents/project_usage_report.py",
    "codex_agents/project_wake.py",
    "codex_agents/project_worker_result.py",
    "codex_agents/provider_observation_contracts.py",
    "codex_agents/provider_restrictions.py",
    "codex_agents/providers.py",
    "codex_agents/rate_limit_contracts.py",
    "codex_agents/refusal_steps.py",
    "codex_agents/release_contracts.py",
    "codex_agents/reply_shapes.py",
    "codex_agents/result_contracts.py",
    "codex_agents/result_persistence.py",
    "codex_agents/result_read_api.py",
    "codex_agents/result_read_service.py",
    "codex_agents/review_runner.py",
    "codex_agents/routing.py",
    "codex_agents/run_failure_reasons.py",
    "codex_agents/run_holds_schema.py",
    "codex_agents/run_network.py",
    "codex_agents/run_provenance.py",
    "codex_agents/runtime_files.py",
    "codex_agents/service_enrollment.py",
    "codex_agents/service_host.py",
    "codex_agents/service_protocol.py",
    "codex_agents/service_queue.py",
    "codex_agents/service_record.py",
    "codex_agents/service_transport.py",
    "codex_agents/session_guard.py",
    "codex_agents/shutdown_watchdog.py",
    "codex_agents/signature_execution.py",
    "codex_agents/signature_input_wire.py",
    "codex_agents/signature_inputs.py",
    "codex_agents/signature_job_intake.py",
    "codex_agents/signature_payloads.py",
    "codex_agents/signature_process.py",
    "codex_agents/signature_sandbox.py",
    "codex_agents/signature_worker_pool.py",
    "codex_agents/sql_read_budget.py",
    "codex_agents/supervision.py",
    "codex_agents/terminal_delivery.py",
    "codex_agents/validation.py",
    "codex_agents/wake_contracts.py",
    "codex_agents/write_merge.py",
    "collector_host.py",
    "config/discovery-policy.json",
    "config/evaluation-summary.json",
    "config/learning-clients.json",
    "config/learning-overhead.json",
    "config/learning-producers.json",
    "config/retention-policy.json",
    "config/routing-policy.json",
    "project_host.py",
    "schemas/agent-state.schema.json",
    "schemas/assignment.schema.json",
    "schemas/evaluation-record.schema.json",
    "schemas/learning-canary-report.schema.json",
    "schemas/learning-candidate.schema.json",
    "schemas/learning-comparison.schema.json",
    "schemas/learning-evaluation-assignment.schema.json",
    "schemas/learning-evaluation-case.schema.json",
    "schemas/learning-evaluation-trial.schema.json",
    "schemas/learning-event.schema.json",
    "schemas/learning-evidence-review.schema.json",
    "schemas/learning-export.schema.json",
    "schemas/learning-feedback.schema.json",
    "schemas/learning-grader-calibration.schema.json",
    "schemas/learning-host-observation.schema.json",
    "schemas/learning-outcome.schema.json",
    "schemas/learning-owner-grant.schema.json",
    "schemas/learning-promotion.schema.json",
    "schemas/learning-qa-evidence.schema.json",
    "schemas/operational-checks.schema.json",
    "schemas/parent-grant.schema.json",
    "schemas/project-agent-routing.schema.json",
    "schemas/result.schema.json",
    "schemas/task.schema.json",
)
# Package files under `.codex/orchestration/` that are not runtime code.
PACKAGE_DATA_FILES = ("config/learning-releases.json", "learning_hook.py")
# Files the installer or setup writes there for the project itself.
# `local-install.json` is this checkout's own (the interpreter its hooks name), never
# committed.
PROJECT_FILES = (
    "install-manifest.json",
    "local-install.json",
    "operational-checks.json",
    "project-agent-routing.json",
    "provider-routing.json",
)
RELEASES_FILE = "config/learning-releases.json"


def runtime_digest(contents: Mapping[str, bytes]) -> str:
    """The digest of exactly the runtime files, each length-prefixed with its path.

    `contents` maps every name in RUNTIME_FILES (and no other) to its bytes.
    """

    if set(contents) != set(RUNTIME_FILES):
        raise ValueError("the runtime file set is incomplete or has extra files")
    hasher = hashlib.sha256()
    for relative in RUNTIME_FILES:
        name = relative.encode("utf-8")
        content = contents[relative]
        hasher.update(len(name).to_bytes(4, "big"))
        hasher.update(name)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()
