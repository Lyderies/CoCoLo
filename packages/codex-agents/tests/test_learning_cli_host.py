"""Learning CLI tests, part host (CI round 6 split of test_learning_cli, test alignment 126; tests moved verbatim)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT

from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_store import (
    CollectorMaintenance,
    LearningLedger,
    LearningState,
)
from tests.learning_support import (
    DIGEST_A,
    assignment,
    event_record,
    full_usage,
    result,
    routing_fields,
)
from tests.learning_cli_support import LearningCliBase  # noqa: F401


class LearningCliTests(LearningCliBase):
    def test_installed_host_result_derives_provenance_and_runs_learning_cycle(self) -> None:
        task = {
            "id": "task-1",
            "objective": "Read one fixture",
            "family": "research",
            "complexity": "medium",
            "risk": "low",
            **routing_fields(),
            "meaningful": True,
            "deterministic_validation": True,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
            "owned_paths": [],
            "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent_grant = {
            "tools": ["shell"],
            "filesystem": ["source.txt:read"],
            "network": False,
            "mcps": [],
            "external_effects": False,
        }
        def admit_run(session_id: str, status: str, observed_at_ms: int) -> str:
            state = LearningState(self.state_root, create=False)
            maintenance = CollectorMaintenance.acquire(
                state,
                ORCHESTRATION_ROOT / "cli.py",
                self.project,
            )
            ledger = LearningLedger(state)
            try:
                _, _, run_id = ledger.bind_session(
                    self.project,
                    session_id,
                    "app_server",
                    "codex-cli-0.153.0",
                    now_ms=observed_at_ms,
                )
                terminal = event_record(f"terminal-{session_id}", observed_at_ms=observed_at_ms)
                terminal.update(
                    run_id=run_id,
                    event_code="turn_completed" if status == "completed" else "turn_failed",
                    status=status,
                    stop_reason=status,
                    coverage="root_turn_complete",
                    item_digest=DIGEST_A,
                )
                usage = event_record(f"usage-{session_id}", observed_at_ms=observed_at_ms)
                usage.update(
                    run_id=run_id,
                    event_code="usage_snapshot",
                    status="observed",
                    stop_reason=None,
                    coverage="root_turn_complete",
                    usage=full_usage(),
                )
                ledger.append(self.project_id, terminal, producer="app_server", now_ms=observed_at_ms)
                ledger.append(self.project_id, usage, producer="app_server", now_ms=observed_at_ms)
                return run_id
            finally:
                ledger.close()
                maintenance.resume()

        run_id = admit_run("host-result-session", "completed", 1_000)
        task_path = self._write("host-task.json", task)
        assignment_path = self._write("host-assignment.json", assigned)
        parent_path = self._write("host-parent.json", parent_grant)
        invalid_result = result()
        invalid_result["summary"] = "TOP_SECRET rejected result"
        invalid_result["evidence"] = []
        rejection = self._run(
            "validate-result",
            "--task",
            str(task_path),
            "--assignment",
            str(assignment_path),
            "--parent-grant",
            str(parent_path),
            "--result",
            str(self._write("invalid-host-result.json", invalid_result)),
            expected=2,
        )
        self.assertEqual("missing_evidence", rejection["reason_code"])
        self.assertTrue(rejection["learning_ready"])
        self.assertRegex(rejection["repair_receipt"], r"^rpr_[0-9a-f]{48}$")
        observation = {
            "run_id": run_id,
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": rejection["repair_receipt"],
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        host_result_path = self._write("host-result.json", result())
        observation_path = self._write("host-observation.json", observation)
        report = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(observation_path),
        )
        self.assertEqual(run_id, report["outcome"]["run_id"])
        self.assertEqual("compact-learning-cycle-v1", report["learning_cycle"]["format"])
        self.assertEqual("available", report["learning_cycle"]["summary_state"])
        self.assertEqual("unverified", report["outcome"]["status"])
        self.assertFalse(report["outcome"]["accepted"])
        self.assertFalse(report["outcome"]["deterministic_pass"])
        self.assertEqual("root_turn_complete", report["outcome"]["coverage"])
        self.assertEqual(1, report["outcome"]["repairs"])
        self.assertEqual(
            ["missing_evidence"], report["outcome"]["repair_reason_codes"]
        )
        for field, value in report["outcome"]["usage"].items():
            self.assertEqual(
                "client_observed" if value is not None else "unavailable",
                report["outcome"]["usage_sources"][field]["source"],
            )
        replay = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(observation_path),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", replay["error"])

        forged_observation = {
            "run_id": admit_run("host-forged-session", "completed", 1_001),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": "rpr_" + "f" * 48,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        forged = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(
                self._write("forged-host-observation.json", forged_observation)
            ),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", forged["error"])

        second_rejection = self._run(
            "validate-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("second-invalid-result.json", invalid_result)),
            expected=2,
        )
        other_task = dict(task)
        other_task["id"] = "task-2"
        other_assigned = assignment()
        other_assigned.update(assignment_id="assignment-2", task_id="task-2")
        other_result = result()
        other_result["assignment_id"] = "assignment-2"
        wrong_assignment_observation = {
            "run_id": admit_run("host-wrong-assignment", "completed", 1_002),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": second_rejection["repair_receipt"],
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        wrong_assignment = self._run(
            "learning-record-host-result",
            "--task", str(self._write("other-task.json", other_task)),
            "--assignment", str(self._write("other-assignment.json", other_assigned)),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("other-result.json", other_result)),
            "--observation", str(
                self._write(
                    "wrong-assignment-observation.json",
                    wrong_assignment_observation,
                )
            ),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", wrong_assignment["error"])
        state = LearningState(self.state_root, create=False)
        maintenance = CollectorMaintenance.acquire(
            state,
            ORCHESTRATION_ROOT / "cli.py",
            self.project,
        )
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        try:
            with ledger.repository(self.project_id) as repository:
                persisted_cases = repository.records("evaluation_case")
                mined_sources = {
                    record["source_outcome_id"]
                    for record in persisted_cases
                }
            self.assertEqual(len(persisted_cases), report["learning_cycle"]["mined_case_count"])
            self.assertNotIn(report["outcome"]["record_id"], mined_sources)
        finally:
            ledger.close()

            maintenance.resume()
        database_bytes = self.state_root.joinpath("learning.sqlite3").read_bytes()
        for secret in (b"sensitive result text", b"private/path", b"secret command"):
            self.assertNotIn(secret, database_bytes)

        failed_result = result()
        failed_result.update(
            status="failed",
            semantic_verdict="not-applicable",
            summary="failed secret summary",
            evidence=[],
            remaining_risks=["private failure detail"],
        )
        mismatched_observation = {
            "run_id": admit_run("host-mismatch-session", "completed", 1_003),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        mismatched = self._run(
            "learning-record-host-result",
            "--task", str(self._write("mismatch-task.json", task)),
            "--assignment", str(self._write("mismatch-assignment.json", assigned)),
            "--parent-grant", str(self._write("mismatch-parent.json", parent_grant)),
            "--result", str(self._write("mismatch-result.json", failed_result)),
            "--observation", str(self._write("mismatch-observation.json", mismatched_observation)),
            expected=2,
        )
        self.assertEqual(
            "host result conflicts with the trusted terminal event",
            mismatched["error"],
        )
        failed_observation = {
            "run_id": admit_run("host-failed-session", "failed", 1_004),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        failed = self._run(
            "learning-record-host-result",
            "--full-report",
            "--task", str(self._write("failed-task.json", task)),
            "--assignment", str(self._write("failed-assignment.json", assigned)),
            "--parent-grant", str(self._write("failed-parent.json", parent_grant)),
            "--result", str(self._write("failed-result.json", failed_result)),
            "--observation", str(self._write("failed-observation.json", failed_observation)),
        )
        self.assertEqual("failed", failed["outcome"]["status"])
        self.assertEqual("turn_failed", failed["event"]["event_code"])
        self.assertTrue(failed["outcome"]["terminal_failure"])
        self.assertIn("slices", failed["learning_cycle"]["analysis"])
        self.assertIn("mined_case_ids", failed["learning_cycle"])

    def test_generate_candidate_requires_qa_and_selects_exact_paired_cohorts(self) -> None:
        spec, qa_rows = self._seed_candidate_cli_cohort()
        spec_path = self._write("paired-candidate.json", spec)

        missing_qa = self._run(
            "learning-generate-candidate",
            "--spec",
            str(spec_path),
            expected=2,
        )
        self.assertEqual(
            "candidate evidence fails a verified eligibility gate",
            missing_qa["error"],
        )

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            with ledger._connection:
                ledger._connection.executemany(
                    "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                    qa_rows,
                )
        finally:
            ledger.close()
        generated = self._run(
            "learning-generate-candidate", "--spec", str(spec_path)
        )
        self.assertTrue(generated["ready"])
        self.assertEqual(40, len(generated["record"]["evidence_digests"]))
        self.assertEqual(
            20,
            generated["record"]["eligibility_receipt"]["independent_case_count"],
        )
        self.assertEqual(
            spec["champion_configuration"],
            generated["record"]["champion_configuration"],
        )

    def test_legacy_fallback_is_refused_for_bridged_children(self) -> None:
        # The fallback answers a native host's model rejection. Every Codex
        # child is bridged, so a recovery-unavailable run gets no receipt.
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        primary_assignment = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        task_path = self._write("fallback-task.json", task)
        primary_assignment_path = self._write(
            "fallback-primary-assignment.json", primary_assignment
        )
        parent_path = self._write("fallback-parent.json", parent)
        primary_route = self._run("learning-route", "--task", str(task_path))
        self.assertTrue(primary_assignment_path.exists() and parent_path.exists())
        orchestration, primary_run = self._legacy_run(task, primary_assignment, primary_route)
        self.assertEqual("bridge", primary_route["dispatch"])
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            ledger.set_orchestration_paused(
                self.project_id, orchestration_handle=orchestration["orchestration_handle"],
                paused=True,
            )
            ledger.classify_host_run_abandoned(
                self.project_id, run_handle=primary_run["run_handle"],
                reason="recovery_unavailable",
            )
        finally:
            ledger.close()
        refused = self._run(
            "learning-route",
            "--task", str(task_path),
            "--fallback-from-run-handle", primary_run["run_handle"],
            expected=2,
        )
        self.assertEqual("input validation failed", refused["error"])
        self.assertNotIn("fallback", refused)

    def test_runtime_rejects_test_fixture_client_identity(self) -> None:
        clients = json.loads(
            (ORCHESTRATION_ROOT / "config" / "learning-clients.json").read_text()
        )["clients"]
        self.assertNotIn("codex-eval-fixture-1", clients)

    def test_cli_rejections_are_bounded_and_do_not_echo_sensitive_paths(self) -> None:
        sensitive = Path(self.temporary.name) / "TOP_SECRET_customer_token"
        missing = self._run_raw(
            "learning-validate", "--record", str(sensitive), expected=2
        )
        self.assertEqual("input validation failed", missing["error"])
        self.assertNotIn("TOP_SECRET", json.dumps(missing))

        duplicate = Path(self.temporary.name) / "duplicate.json"
        duplicate.write_text('{"schema_version":1,"schema_version":1}')
        duplicate_report = self._run_raw(
            "learning-validate", "--record", str(duplicate), expected=2
        )
        self.assertEqual("input validation failed", duplicate_report["error"])

        oversized = Path(self.temporary.name) / "oversized.json"
        oversized.write_bytes(b"[" + b"0," * 40_000 + b"0]")
        oversized_report = self._run_raw(
            "learning-validate", "--record", str(oversized), expected=2
        )
        self.assertEqual("input validation failed", oversized_report["error"])

        rejected = self._write(
            "rejected-secret.json", {"TOP_SECRET_customer_token": "TOP_SECRET_value"}
        )
        rejected_report = self._run_raw(
            "learning-validate", "--record", str(rejected), expected=2
        )
        self.assertEqual("input validation failed", rejected_report["error"])
        self.assertNotIn("TOP_SECRET", json.dumps(rejected_report))


if __name__ == "__main__":
    unittest.main()
