"""Learning CLI tests: status, collector and registration commands (CI round 6 split; the other parts are test_learning_cli_host, _records and _keys_upgrade)."""
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
    def test_committed_ledger_operation_reports_restart_failure_explicitly(self) -> None:
        ledger = mock.Mock()
        maintenance = mock.Mock()
        maintenance.resume.side_effect = CollectorError("restart unavailable")
        managed = _RestartingLedger(ledger, maintenance)

        with self.assertRaisesRegex(
            CollectorRestartError,
            "ledger operation completed; collector restart failed",
        ):
            managed.close(operation_completed=True)

        ledger.close.assert_called_once_with()
        maintenance.resume.assert_called_once_with()

    def test_operation_error_is_not_masked_by_restart_failure(self) -> None:
        ledger = mock.Mock()
        maintenance = mock.Mock()
        maintenance.resume.side_effect = CollectorError("restart unavailable")
        managed = _RestartingLedger(ledger, maintenance)

        with self.assertRaisesRegex(ContractError, "operation failed"):
            try:
                raise ContractError("operation failed")
            finally:
                _close_ledger(managed)

    def test_missing_billing_attestation_names_the_wizard_option(self) -> None:
        for failure, field, reason in (
            (AdmissionDenied("billing_attestation_missing"),
             "reason_code", "billing_attestation_missing"),
            (BridgeRefused("launch denied: billing_attestation_missing"),
             "reason", "launch denied: billing_attestation_missing"),
        ):
            # The refusal comes back from the account service; the hint is the CLI's.
            with self.subTest(field=field), mock.patch(
                "cli.observe_provider_client", side_effect=failure
            ):
                report = self._observe("claude", expected=2)
                self.assertEqual(reason, report[field])
                self.assertIn('"Re-run the billing attestation only"', report["hint"])

    def test_status_allows_time_for_owner_signature_verification(self) -> None:
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        server = CollectorServer(ledger)
        original = ledger.provider_attestation_status

        def slow_report(project_id):
            time.sleep(0.35)  # Longer than the hook client's short delivery timeout.
            return original(project_id)

        server.start()
        try:
            with mock.patch.object(ledger, "provider_attestation_status", side_effect=slow_report):
                # This bare collector serves no account API, so the owner-key part
                # (and with it the exit status) is unavailable; the slow part is not.
                report = self._run("learning-status", expected=1)
            self.assertEqual("status_account_unavailable", report["reason_code"])
            self.assertEqual("present", report["provider_attestations"]["codex"]["state"])
        finally:
            server.stop()
            ledger.close()

    def test_unavailable_collector_does_not_claim_hooks_have_never_delivered(self) -> None:
        stdout = io.StringIO()
        arguments = ["cli.py", "learning-status", "--state-root", str(self.state_root),
                     "--project-root", str(self.project)]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), \
                mock.patch("cli.ensure_collector", side_effect=CollectorError(
                    "unavailable", code="transport_unavailable")):
            self.assertEqual(1, cli_main())
        report = json.loads(stdout.getvalue())
        self.assertEqual("unavailable", report["ingestion"]["ingestion_state"])
        self.assertEqual("unavailable", report["ingestion"]["freshness"]["state"])
        self.assertNotIn("/hooks", report["ingestion"]["freshness"]["message"])
        self.assertEqual("unavailable", report["provider_attestations"]["codex"]["state"])

    def test_status_recovers_collector_and_direct_command_restarts_it(self) -> None:
        try:
            first = self._run("learning-status")
            self.assertTrue(first["ready"])
            self.assertTrue(first["active"])
            self.assertEqual("present", first["provider_attestations"]["codex"]["state"])
            self.assertEqual("identity_missing", first["provider_attestations"]["claude"]["state"])
            self.assertEqual(
                "awaiting_hook_review_or_first_event",
                first["ingestion"]["freshness"]["state"],
            )
            self.assertEqual("active", first["ingestion"]["registered_project_state"])

            self._run(
                "learning-analyze",
                "--as-of-ms",
                "1000",
                "--max-age-ms",
                "1000",
            )
            after_maintenance = self._run("learning-status")
            self.assertTrue(after_maintenance["active"])

            self._stop_collector()
            recovered = self._run("learning-status")
            self.assertTrue(recovered["ready"])
            self.assertTrue(recovered["active"])
        finally:
            self._stop_collector()

    def test_only_learning_register_creates_missing_runtime_authorization(self) -> None:
        state = LearningState(self.state_root, create=False)
        ledger = LearningLedger(state)
        try:
            orchestration = ledger.open_orchestration(
                self.project_id,
                client_version="codex-host-v1",
                now_ms=2,
            )
        finally:
            ledger.close()
        self.state_root.joinpath("collector-authorization.json").unlink()
        missing = self._run("learning-status", expected=1)
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["active"])
        registered = self._run("learning-register")
        self.assertTrue(registered["ready"])
        self.assertEqual(
            registered["collector_runtime_version"],
            authorized_collector_runtime(
                LearningState(self.state_root, create=False)
            )["runtime_version"],
        )
        status = self._run("learning-status")
        # s2fix round 2 (L14.1): the orchestrations are the project service's; with none running the count is
        # unknown, never this ledger's older table (assertions.md).
        managed = status["ingestion"]["managed_lifecycle"]
        self.assertEqual((None, None, None), tuple(managed["orchestrations_" + name]
                                                   for name in ("active", "paused", "closed")))
        self.assertIn("service", managed["orchestrations_unknown_reason"])
        self.assertEqual("active", orchestration["status"])

    def test_status_reports_unregistered_project_without_identifiers(self) -> None:
        unregistered = Path(self.temporary.name) / "unregistered"
        unregistered.mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                "learning-status",
                "--state-root", str(self.state_root),
                "--project-root", str(unregistered),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            "unregistered", report["ingestion"]["registered_project_state"]
        )
        self.assertEqual("inactive", report["ingestion"]["ingestion_state"])
        encoded = json.dumps(report["ingestion"])
        self.assertNotIn(str(unregistered), encoded)
        self.assertNotIn("project_id", encoded)

    def test_concurrent_direct_commands_serialize_and_leave_collector_active(self) -> None:
        try:
            self.assertTrue(self._run("learning-status")["active"])
            command = [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                "learning-analyze",
                "--state-root",
                str(self.state_root),
                "--project-root",
                str(self.project),
                "--as-of-ms",
                "1000",
                "--max-age-ms",
                "1000",
            ]
            processes = [
                subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)
            ]
            completed = [process.communicate(timeout=10) for process in processes]
            for process, (stdout, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(0, process.returncode, stderr or stdout)
            self.assertTrue(self._run("learning-status")["active"])
        finally:
            self._stop_collector()


if __name__ == "__main__":
    unittest.main()
