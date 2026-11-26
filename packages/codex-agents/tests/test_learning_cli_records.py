"""Learning CLI tests, part records (CI round 6 split of test_learning_cli, test alignment 126; tests moved verbatim)."""
from __future__ import annotations

import contextlib
import io
import json
import base64
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import learning_store
from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import (
    candidate_base_digest,
    canonical_json,
    content_digest,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_store import (
    CollectorClient,
    CollectorServer,
    CollectorError,
    CollectorMaintenance,
    CollectorRestartError,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    authorized_collector_runtime,
)
from cli import _RestartingLedger, _close_ledger
from cli import main as cli_main
from codex_agents.learning_engine import generate_candidate, runtime_fixture_digest
from codex_agents.providers import AdmissionDenied
from codex_agents.bridge import BridgeRefused
from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    GRADER_CONFIG,
    assignment,
    champion_configuration,
    event_record,
    full_usage,
    admit_provider,
    fake_provider_tools,
    generate_test_owner_key,
    OWNER_SIGNATURE_NAMESPACE,
    trusted_tools,
    sign_owner_payload,
    result,
    routing_fields,
    verification_outcome,
)
from tests.learning_cli_support import LearningCliBase  # noqa: F401


class LearningCliTests(LearningCliBase):
    def test_detached_owner_grant_workflow_never_reads_private_key(self) -> None:
        request_report = self._run(
            "learning-create-grant-request",
            "--kind", "canary",
            "--candidate-digest", DIGEST_A,
            "--champion-digest", DIGEST_B,
            "--review-digest", DIGEST_C,
            "--bounds-digest", "d" * 64,
            "--issued-at-ms", "10",
            "--expires-at-ms", "100",
        )
        payload = Path(self.temporary.name) / "owner-request-payload"
        payload.write_bytes(base64.b64decode(request_report["signing_payload_b64"]))
        signed = subprocess.run(
            [
                "ssh-keygen", "-Y", "sign", "-f", str(self.owner_private_key),
                "-n", request_report["namespace"], str(payload),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, signed.returncode, signed.stderr)
        attached = self._run_state(
            "learning-attach-grant-signature",
            "--request", str(self._write("grant-request.json", request_report["request"])),
            "--signature", str(Path(str(payload) + ".sig")),
        )
        self.assertEqual("owner_grant", attached["record"]["record_type"])
        self.assertNotIn(str(self.owner_private_key), json.dumps(attached))

    def test_installed_l3_l4_commands_persist_closed_records(self) -> None:
        feedback_arguments = (
            "--run-id",
            self.outcome["run_id"],
            "--label",
            "accepted",
            "--reason-code",
            "quality",
            "--observed-at-ms",
            "10",
        )
        prepared_feedback = self._run(
            "learning-record-feedback",
            *feedback_arguments,
        )
        feedback_signature = self._sign_prepared(
            prepared_feedback, "feedback-payload"
        )
        feedback = self._run(
            "learning-record-feedback",
            *feedback_arguments,
            "--signature",
            str(feedback_signature),
        )
        feedback_signature.unlink(missing_ok=True)
        self.assertEqual(self.outcome["run_id"], feedback["record"]["run_id"])
        mined = self._run(
            "learning-mine-case",
            "--source-outcome-id",
            self.outcome["record_id"],
            "--task",
            str(self._write("mine-task.json", self.fixture_task)),
            "--criteria-digest",
            DIGEST_B,
            "--lineage-digest",
            DIGEST_C,
        )
        reviewed = self._run(
            "learning-review-case",
            "--case-id",
            mined["record"]["record_id"],
            "--review-digest",
            DIGEST_C,
            "--similarity-group-digest",
            "d" * 64,
            "--destination",
            "promotion_holdout",
            "--freeze",
        )
        self.assertTrue(reviewed["record"]["frozen"])
        analysis = self._run(
            "learning-analyze", "--as-of-ms", "1000", "--max-age-ms", "1000"
        )
        self.assertIn("verification-learning", analysis["analysis"]["slices"])

        before = {"model": self.outcome["model"], "effort": self.outcome["effort"]}
        after = {"model": "gpt-6-sol", "effort": "high"}
        champion = {
            key: self.outcome[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest",
                "package_digest", "policy_digest", "agent_digest", "skill_digests",
                "prompt_digest", "client_digest",
            )
        }
        proposed = generate_candidate(
            digest_key=b"c" * 32,
            scope="general",
            task_slice=self.outcome["task_slice"],
            target="model_effort",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(before),
                "after_digest": content_digest(after),
                "before_value": before,
                "after_value": after,
            }],
            evidence_digests=[content_digest(self.outcome)],
        )
        spec = {
            key: proposed[key]
            for key in (
                "scope", "task_slice", "target", "effect", "base_digest",
                "change_manifest", "champion_configuration",
            )
        }
        generated = self._run(
            "learning-generate-candidate",
            "--spec",
            str(self._write("candidate.json", spec)),
            expected=2,
        )
        self.assertEqual("input validation failed", generated["error"])

        labels = [{"owner": "a", "grader": "a", "swapped": "b"} for _ in range(30)]
        calibration_arguments = (
            "--labels",
            str(self._write("labels.json", labels)),
            "--configuration",
            str(self._write("grader.json", GRADER_CONFIG)),
            "--observed-at-ms",
            "100",
        )
        prepared_calibration = self._run(
            "learning-calibrate-grader",
            *calibration_arguments,
        )
        calibration_signature = self._sign_prepared(
            prepared_calibration, "calibration-payload"
        )
        calibrated = self._run(
            "learning-calibrate-grader",
            *calibration_arguments,
            "--signature",
            str(calibration_signature),
        )
        calibration_signature.unlink(missing_ok=True)
        self.assertTrue(calibrated["record"]["ready"])
        self.assertEqual(content_digest(labels), calibrated["record"]["labels_digest"])

        # The owner commands that ask where the project's learning lives leave the
        # shared collector running; it owns the ledger until it is stopped.
        self._stop_collector()
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            with ledger.repository(self.project_id) as repository:
                self.assertEqual(1, len(repository.records("grader_calibration")))
                self.assertEqual(1, len(repository.records("evaluation_case")))
        finally:
            ledger.close()

    def test_provider_observation_and_billing_attestation_commands(self) -> None:
        # The fake clients answer from recorded fixtures; nothing is spent. The
        # command and the account services run in-process so the trusted search
        # path can name them.
        with self._account_services():
            self._provider_observation_and_billing_attestation_commands()

    def test_trusted_host_lifecycle_opens_and_records_exact_assignment(self) -> None:
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        # Setup (L6.2): the assignment reads source.txt; open refuses an uncommitted read path before
        # admission, so the fixture commits it as a real project would.
        (self.project / "source.txt").write_text("fixture\n")
        git = ["git", "-C", str(self.project), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "add", "source.txt"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)
        task_path = self._write("trusted-task.json", task)
        assignment_path = self._write("trusted-assignment.json", assigned)
        parent_path = self._write("trusted-parent.json", parent)
        route = self._run("learning-route", "--task", str(task_path))
        route_path = self._write("trusted-route.json", route)
        orchestration = self._run("learning-open-orchestration")
        checkpoint = self._run(
            "learning-checkpoint-orchestration",
            "--orchestration-handle", orchestration["orchestration_handle"],
            "--goal-tokens-used", "101",
            "--goal-time-used-seconds", "3",
        )
        self.assertEqual("active", checkpoint["status"])
        self.assertEqual(101, checkpoint["usage"]["goal_total_tokens"])
        changed_route = dict(route)
        changed_route["model"] = "gpt-6-astra"
        rejected_route = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(self._write("changed-trusted-route.json", changed_route)),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
            expected=2,
        )
        self.assertEqual("input validation failed", rejected_route["error"])
        open_arguments = (
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(route_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        denied = self._run(
            *open_arguments, expected=2, env={"OPENAI_API_KEY": "test-only"}
        )
        self.assertEqual(
            ("provider admission denied", "provider_api_key_present"),
            (denied["error"], denied["reason_code"]),
        )
        opened = self._run(*open_arguments)
        self.assertRegex(opened["run_handle"], r"^hrn_[0-9a-f]{48}$")
        self.assertEqual("admitted", opened["admission"])
        # A Codex child is bridged: only its own dispatch binds it, and only that
        # child's recorded result can finish the run (see test_bridge).
        self.assertEqual("bridge", route["dispatch"])
        refused = self._run(
            "learning-bind-host-agent",
            "--run-handle", opened["run_handle"],
            "--agent-id", "native-child-1",
            expected=2,
        )
        self.assertIn("binds only its dispatched child", refused["error"])
        observation = {
            "run_handle": opened["run_handle"],
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        arguments = (
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("trusted-result.json", result())),
            "--observation", str(self._write("trusted-observation.json", observation)),
        )
        recorded = self._run("learning-record-host-result", *arguments, expected=2)
        self.assertIn("must be dispatched", recorded["error"])
        self.assertNotIn(
            opened["run_handle"].encode(),
            self.state_root.joinpath("learning.sqlite3").read_bytes(),
        )

    def test_upgrade_reconciliation_cli_requires_owner_signature(self) -> None:
        task = {
            "id": "task-1", "objective": "Inspect one fixture",
            "family": "research", "complexity": "medium", "risk": "low",
            "meaningful": True, **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        task_path = self._write("upgrade-task.json", task)
        assigned_path = self._write("upgrade-assignment.json", assigned)
        parent_path = self._write("upgrade-parent.json", parent)
        route = self._run("learning-route", "--task", str(task_path))
        self.assertTrue(assigned_path.exists() and parent_path.exists())
        orchestration, opened = self._legacy_run(task, assigned, route)
        prepared = self._run(
            "learning-prepare-upgrade-reconciliation",
            "--orchestration-id", orchestration["orchestration_id"],
            "--run-handle", opened["run_handle"],
        )
        request_path = self._write("upgrade-request.json", prepared["request"])
        invalid_signature = Path(self.temporary.name) / "invalid-upgrade.sig"
        invalid_signature.write_bytes(b"invalid")
        rejected = self._run(
            "learning-commit-upgrade-reconciliation",
            "--request", str(request_path),
            "--signature", str(invalid_signature), expected=2,
        )
        self.assertIn("signature", rejected["error"])
        signature_path = sign_owner_payload(
            self.owner_private_key,
            base64.b64decode(prepared["signing_payload_b64"]),
            "upgrade-cli-payload",
        )
        committed = self._run(
            "learning-commit-upgrade-reconciliation",
            "--request", str(request_path),
            "--signature", str(signature_path),
        )
        self.assertEqual("interrupted", committed["status"])
        self.assertEqual(1, committed["runs"]["abandoned"])
        self.assertEqual(0, committed["runs"]["pending"])


if __name__ == "__main__":
    unittest.main()
