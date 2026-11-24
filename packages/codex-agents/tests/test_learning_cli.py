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


class LearningCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        self.project = base / "project"
        self.project.mkdir()
        # Routing needs a repository to prove no provider configuration is committed.
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        state = LearningState(self.state_root)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, owner_public_key = generate_test_owner_key(base)
        state.install_owner_public_key(owner_public_key)
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        self.project_id = ledger.register_project(self.project, now_ms=1)
        # Host runs are admitted only with a fresh identity and an owner attestation.
        self.tools = base / "fake-tools"
        fake_provider_tools(self.tools)
        # Collector sessions here bind on a small fixed clock and host runs on the
        # real clock, so the identity is observed at both.
        for observed_at_ms in (1, time.time_ns() // 1_000_000):
            admit_provider(
                ledger, self.project_id, self.owner_private_key, tools=self.tools,
                observed_at_ms=observed_at_ms,
            )
        self.fixture_task = {
            "id": "cli-fixture", "objective": "Verify one fixed fixture",
            "family": "verification", "complexity": "low", "risk": "low",
            **routing_fields(operation="review"),
            "meaningful": True, "deterministic_validation": True,
            "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0,
            "failure_events": [],
        }
        self.outcome = verification_outcome("cli_outcome")
        self.outcome["task_slice"] = (
            "verification-low-low-review-known-repository-executable"
        )
        self.outcome["fixture_digest"] = runtime_fixture_digest(
            state.digest_key(), self.fixture_task
        )
        event = event_record("cli_event")
        event["run_id"] = self.outcome["run_id"]
        event["assignment_id"] = self.outcome["assignment_id"]
        event["model"] = self.outcome["model"]
        event["effort"] = self.outcome["effort"]
        event["role"] = self.outcome["role"]
        event["task_slice"] = self.outcome["task_slice"]
        event["topology"] = self.outcome["topology"]
        event["configuration_digest"] = self.outcome["configuration_digest"]
        event["coverage"] = self.outcome["coverage"]
        event["usage"] = self.outcome["usage"]
        event["event_code"] = "item_completed"
        event["status"] = "completed"
        event["item_digest"] = self.outcome["evidence_digests"][0]
        self._admit_fixture_outcome(
            ledger, event=event, outcome=self.outcome, producer="evaluator", now_ms=1
        )
        verifier = verification_outcome("cli_verifier")
        verifier_event = event_record("cli_verifier_event")
        verifier_event["run_id"] = verifier["run_id"]
        verifier_event["assignment_id"] = verifier["assignment_id"]
        verifier_event["model"] = verifier["model"]
        verifier_event["effort"] = verifier["effort"]
        verifier_event["role"] = verifier["role"]
        verifier_event["task_slice"] = verifier["task_slice"]
        verifier_event["topology"] = verifier["topology"]
        verifier_event["configuration_digest"] = verifier["configuration_digest"]
        verifier_event["coverage"] = verifier["coverage"]
        verifier_event["usage"] = verifier["usage"]
        verifier_event["event_code"] = "item_completed"
        verifier_event["status"] = "completed"
        verifier_event["item_digest"] = verifier["evidence_digests"][0]
        self._admit_fixture_outcome(
            # Admitted now: the ledger's cycle reads the retention window of outcomes (O0c P8), and
            # the commands here run on the real clock, so a 1970 outcome would never be mined.
            ledger, event=verifier_event, outcome=verifier, producer="verifier",
            now_ms=time.time_ns() // 1_000_000,
        )
        ledger.close()
        self.verifier = verifier

    def _legacy_run(self, task: dict, assigned: dict, route: dict) -> tuple[dict, dict]:
        """An orchestration and run as an earlier version opened them, in the ledger.

        Every run is now opened by the project's own service; the ledger commands
        tested with this still serve what an earlier version left behind.
        """
        self._stop_collector()
        ledger = LearningLedger(
            LearningState(self.state_root, create=False),
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        try:
            orchestration = ledger.open_orchestration(
                self.project_id, client_version="codex-host-v1"
            )
            opened = ledger.open_host_run(
                self.project_id, assignment=assigned, task=task, route=route,
                execution_configuration=route["execution_configuration"],
                client_version="codex-host-v1",
                orchestration_handle=orchestration["orchestration_handle"],
            )
        finally:
            ledger.close()
        return orchestration, opened

    def _admit_fixture_outcome(
        self,
        ledger: LearningLedger,
        *,
        event: dict,
        outcome: dict,
        producer: str,
        now_ms: int,
    ) -> None:
        with ledger._lock, ledger._connection:
            ledger._append_checked_locked(
                self.project_id, event, producer="host", timestamp=now_ms
            )
            ledger._validate_evidence_links_locked(self.project_id, outcome)
            ledger._append_checked_locked(
                self.project_id, outcome, producer=producer, timestamp=now_ms
            )

    def _seed_candidate_cli_cohort(self) -> tuple[dict, list[tuple]]:
        champion = champion_configuration()
        before = {"model": champion["model"], "effort": champion["effort"]}
        after = {"model": "gpt-6-luna", "effort": "high"}
        spec = {
            "scope": "project",
            "task_slice": "coding-medium-low",
            "target": "model_effort",
            "effect": "optimize",
            "base_digest": candidate_base_digest(champion),
            "change_manifest": [{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(before),
                "after_digest": content_digest(after),
                "before_value": before,
                "after_value": after,
            }],
            "champion_configuration": champion,
        }
        provisional = generate_candidate(
            digest_key=b"c" * 32,
            **spec,
            evidence_digests=[DIGEST_A],
        )
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        qa_rows: list[tuple] = []
        try:
            for index in range(20):
                fixture = f"{index + 3000:064x}"
                for cohort, configuration in (
                    ("champion", provisional["champion_configuration"]),
                    ("candidate", provisional["candidate_configuration"]),
                ):
                    identifier = f"cli-cohort-{index}-{cohort}"
                    qa_digest = content_digest({"qa": identifier})
                    outcome = verification_outcome(identifier)
                    outcome.update(
                        **configuration,
                        task_slice=spec["task_slice"],
                        fixture_digest=fixture,
                        observed_at_ms=100 + index,
                        evidence_digests=[DIGEST_A, qa_digest],
                    )
                    outcome["configuration_digest"] = content_digest(configuration)
                    event = event_record(f"event-{identifier}")
                    event.update(
                        run_id=outcome["run_id"],
                        assignment_id=outcome["assignment_id"],
                        model=outcome["model"],
                        effort=outcome["effort"],
                        role=outcome["role"],
                        task_slice=outcome["task_slice"],
                        topology=outcome["topology"],
                        configuration_digest=outcome["configuration_digest"],
                        coverage=outcome["coverage"],
                        usage=outcome["usage"],
                        event_code="item_completed",
                        status="completed",
                        item_digest=DIGEST_A,
                    )
                    self._admit_fixture_outcome(
                        ledger,
                        event=event,
                        outcome=outcome,
                        producer="verifier",
                        now_ms=100 + index,
                    )
                    qa_rows.append(
                        (
                            self.project_id,
                            f"qa:{identifier}",
                            qa_digest,
                            content_digest(outcome),
                            DIGEST_B,
                            1,
                            100 + index,
                            canonical_json({"accepted": True}),
                        )
                    )
        finally:
            ledger.close()
        return spec, qa_rows

    def tearDown(self) -> None:
        try:
            if self.state_root.exists():
                self._stop_collector()
        finally:
            self.temporary.cleanup()

    def _write(self, name: str, value: object) -> Path:
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps(value))
        return path

    def _run(
        self,
        command: str,
        *arguments: str,
        expected: int = 0,
        env: dict[str, str] | None = None,
    ) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                command,
                "--state-root",
                str(self.state_root),
                "--project-root",
                str(self.project),
                *arguments,
            ],
            text=True,
            capture_output=True,
            env=self._environment(**(env or {})),
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _observe(
        self,
        provider: str,
        *,
        expected: int = 0,
        env: dict[str, str] | None = None,
        raw_output: bool = False,
    ):
        """Run provider-observe in-process with the fake clients as trusted ones."""

        stdout = io.StringIO()
        arguments = [
            "cli.py", "provider-observe", "--provider", provider,
            "--state-root", str(self.state_root), "--project-root", str(self.project),
        ]
        with trusted_tools(self.tools), mock.patch.object(sys, "argv", arguments), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(stdout):
            code = cli_main()
        self.assertEqual(expected, code, stdout.getvalue())
        return stdout.getvalue() if raw_output else json.loads(stdout.getvalue())

    @staticmethod
    def _environment(**overrides: str) -> dict[str, str]:
        # Admission denies API-key environments, so tests never inherit them.
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
        }
        environment.update(overrides)
        return environment

    def _run_state(self, command: str, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                command,
                "--state-root",
                str(self.state_root),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _run_raw(self, command: str, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), command, *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _sign_prepared(self, report: dict, name: str) -> Path:
        return sign_owner_payload(
            self.owner_private_key,
            base64.b64decode(report["signing_payload_b64"], validate=True),
            name,
        )

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

    def _stop_collector(self) -> None:
        client = CollectorClient(self.state_root / "collector.sock", timeout=0.1)
        try:
            client.shutdown()
        except CollectorError:
            maintenance = CollectorMaintenance.acquire(
                LearningState(self.state_root, create=False),
                ORCHESTRATION_ROOT / "cli.py",
                self.project,
            )
            maintenance.resume()
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                client.health()
            except CollectorError:
                maintenance = CollectorMaintenance.acquire(
                    LearningState(self.state_root, create=False),
                    ORCHESTRATION_ROOT / "cli.py",
                    self.project,
                )
                maintenance.resume()
                return
            time.sleep(0.02)
        self.fail("collector did not stop")

    def test_replacing_the_owner_key_through_the_cli(self) -> None:
        try:
            before = self._run("learning-status")["owner_key"]
            self.assertEqual((before["state"], before["replacements"]), ("ok", 0))
            directory = Path(self.temporary.name) / "replacement"
            directory.mkdir()
            key, public = generate_test_owner_key(directory)
            self.assertNotIn("fingerprint", before)  # D36: status shows no key fingerprint
            self.assertIsNone(before["last_replaced_at_ms"])
            statement = b"owner statement"
            allowed = self.state_root / "owner-allowed-signers"

            def accepted(private: Path, name: str) -> bool:
                signature = sign_owner_payload(private, statement, name)
                try:
                    return subprocess.run(
                        ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "codex-agents-owner",
                         "-n", OWNER_SIGNATURE_NAMESPACE, "-s", str(signature)],
                        input=statement, capture_output=True, check=False).returncode == 0
                finally:
                    signature.unlink(missing_ok=True)

            self.assertTrue(accepted(self.owner_private_key, "old-before"))
            replaced = self._run(
                "learning-replace-owner-key", "--public-key", f"{key}.pub"
            )["owner_key"]
            self.assertEqual((replaced["state"], replaced["replacements"]), ("ok", 1))
            self.assertNotIn("fingerprint", replaced)
            # L2.2: the replacement really changed the key: the old key's signature is refused, the new one's accepted.
            self.assertIsInstance(replaced["last_replaced_at_ms"], int)
            self.assertTrue(accepted(key, "new-after"))
            self.assertFalse(accepted(self.owner_private_key, "old-after"))
            after = self._run("learning-status")["owner_key"]
            self.assertEqual(after, replaced)
            fields = public.split()
            self.assertEqual(
                (self.state_root / "owner-allowed-signers").read_text(),
                f"codex-agents-owner {fields[0]} {fields[1]}\n",
            )
        finally:
            self._stop_collector()

    def test_status_and_key_replacement_carry_no_private_value_anywhere(self) -> None:
        # Review 040-screens-r1 MED-1 (owner decision 36): a sentinel scan over the whole output (stdout and
        # stderr, not one field) of status and the replacement, and over every saved file that is not the
        # ledger or the signer file (which hold the public key by design): no fingerprint, key text or home path.
        try:
            directory = Path(self.temporary.name) / "replacement"
            directory.mkdir()
            key, public = generate_test_owner_key(directory)
            sentinels = {"home path": str(Path.home())}
            for name, pub in (("old", self.owner_private_key.with_suffix(".pub")), ("new", Path(f"{key}.pub"))):
                listed = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True).stdout
                fingerprint = listed.split()[1]
                self.assertTrue(fingerprint.startswith("SHA256:"), listed)
                sentinels[f"{name} fingerprint"] = fingerprint
                sentinels[f"{name} fingerprint body"] = fingerprint.split(":", 1)[1]
                sentinels[f"{name} key text"] = pub.read_text().split()[1]

            def output(*command: str) -> str:
                completed = subprocess.run(
                    [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), command[0], "--state-root", str(self.state_root),
                     "--project-root", str(self.project), *command[1:]],
                    text=True, capture_output=True, env=self._environment(), check=False)
                self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
                return completed.stdout + "\n--stderr--\n" + completed.stderr

            reports = {"status before": output("learning-status"),
                       "replacement": output("learning-replace-owner-key", "--public-key", f"{key}.pub"),
                       "status after": output("learning-status")}
            for report, text in reports.items():
                for name, value in sentinels.items():
                    with self.subTest(report=report, sentinel=name):
                        self.assertNotIn(value, text)
                        self.assertNotIn("fingerprint", json.loads(text.split("\n--stderr--\n")[0]).get("owner_key", {}))
            saved = [path for path in self.state_root.rglob("*")
                     if path.is_file() and path.name != "owner-allowed-signers"
                     and not path.name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"))
                     and "sqlite" not in path.name]
            self.assertTrue(saved)
            for path in saved:
                data = path.read_bytes()
                for name, value in sentinels.items():
                    with self.subTest(saved=path.name, sentinel=name):
                        self.assertNotIn(value.encode(), data)
            # The owner's own terminal output still names the key where it matters: status says whether a key is
            # set and how often it was replaced, which is what the owner needs to proceed or troubleshoot.
            owner_key = json.loads(reports["status after"].split("\n--stderr--\n")[0])["owner_key"]
            self.assertEqual(("ok", 1), (owner_key["state"], owner_key["replacements"]))
            self.assertIsInstance(owner_key["last_replaced_at_ms"], int)
        finally:
            self._stop_collector()

    def _replace_key_report(self, backend_report: dict) -> tuple[int, str]:
        """learning-replace-owner-key in this process against a backend that answers with backend_report."""

        class Backend:
            def replace_owner_key(self, public_key, **_ignored):
                return dict(backend_report)

            def close(self, *_ignored, **_also_ignored):
                pass

        public = Path(self.temporary.name) / "replacement-key.pub"
        public.write_text("ssh-ed25519 AAAAREPLACEMENTKEY test-only\n")
        stdout = io.StringIO()
        arguments = ["cli.py", "learning-replace-owner-key", "--state-root", str(self.state_root),
                     "--project-root", str(self.project), "--public-key", str(public)]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), \
                mock.patch("cli._learning_ledger", return_value=(Backend(), "project")):
            code = cli_main()
        return code, stdout.getvalue()

    def test_replace_key_report_has_exactly_the_three_allowed_fields(self) -> None:
        # Review 040-screens-r2 MED-2 (owner decision 36): the report is a whitelist, so a backend that grows or
        # leaks a field (a fingerprint, key text, a path, a passphrase) never reaches the owner's terminal.
        sentinels = ("SHA256:SENTINELFINGERPRINT0123", "AAAASENTINELKEYTEXT", "sentinel-passphrase-value",
                     "/srv/sentinel/owner-key", "-----BEGIN OPENSSH PRIVATE KEY-----SENTINEL")
        extra = {"fingerprint": sentinels[0], "public_key": sentinels[1], "passphrase": sentinels[2],
                 "path": sentinels[3], "private_key": sentinels[4], "signer_line": sentinels[1]}
        code, text = self._replace_key_report(
            {"state": "ok", "replacements": 2, "last_replaced_at_ms": 1234, **extra})
        self.assertEqual(0, code, text)
        report = json.loads(text)
        self.assertEqual({"ready", "owner_key"}, set(report))
        self.assertEqual({"state", "replacements", "last_replaced_at_ms"}, set(report["owner_key"]))
        self.assertEqual({"state": "ok", "replacements": 2, "last_replaced_at_ms": 1234}, report["owner_key"])
        for value in sentinels:
            self.assertNotIn(value, text)

    def test_replace_key_report_missing_a_field_is_a_clean_error_not_a_traceback(self) -> None:
        # Review 040-screens-r2 LOW: a backend that omits a whitelisted field gives the CLI's error report.
        code, text = self._replace_key_report({"state": "ok", "replacements": 1})
        self.assertEqual(2, code, text)
        report = json.loads(text)
        self.assertIs(False, report["ready"])
        self.assertIn("error", report)
        self.assertNotIn("Traceback", text)

    def test_key_replacement_output_carries_no_private_key_text_or_passphrase(self) -> None:
        # Review 040-screens-r2 LOW: the whole-output scan also seeds the private key's text and a passphrase
        # (in the environment the CLI runs in); neither may appear in the replacement's stdout or stderr.
        try:
            directory = Path(self.temporary.name) / "private-scan"
            directory.mkdir()
            key, _public = generate_test_owner_key(directory)
            private_body = "".join(key.read_text().splitlines()[1:-1])
            self.assertGreater(len(private_body), 40)
            passphrase = "sentinel-passphrase-value-r2"
            completed = subprocess.run(
                [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), "learning-replace-owner-key",
                 "--state-root", str(self.state_root), "--project-root", str(self.project),
                 "--public-key", f"{key}.pub"],
                text=True, capture_output=True,
                env=self._environment(SSH_KEY_PASSPHRASE=passphrase, ORCH_OWNER_PASSPHRASE=passphrase),
                check=False)
            self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
            output = completed.stdout + completed.stderr
            for value in (private_body, passphrase, str(key)):
                self.assertNotIn(value, output)
        finally:
            self._stop_collector()

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

    def test_registration_upgrades_an_earlier_versions_ledger_only_when_approved(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        records = ledger._connection.execute("SELECT count(*) FROM records").fetchone()[0]
        downgrade_ledger_to_six(ledger)
        ledger.close()
        try:
            # Asked first: nothing is stopped, copied or changed until the owner approves.
            refused = self._run("learning-register", expected=2)
            self.assertEqual(("ledger_upgrade_required", 6, False), (
                refused["reason_code"], refused["ledger_schema_version"], refused["ready"]))
            self.assertIn("--upgrade-ledger", refused["error"])
            self.assertFalse((self.state_root / "ledger-before-upgrade").exists())
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            upgraded = self._run("learning-register", "--upgrade-ledger")
            self.assertEqual((True, 6, learning_store.LEDGER_SCHEMA_VERSION, self.project_id), (
                upgraded["ready"], upgraded["ledger_upgraded_from"], upgraded["collector_schema_version"],
                upgraded["project_id"]))
            copy = Path(upgraded["ledger_copy"])
            self.assertEqual(self.state_root.resolve() / "ledger-before-upgrade", copy.parent)
            kept = sqlite3.connect(copy)
            try:
                self.assertEqual((6, records), (kept.execute("PRAGMA user_version").fetchone()[0],
                                                kept.execute("SELECT count(*) FROM records").fetchone()[0]))
            finally:
                kept.close()
            self.assertEqual(1, len(list((self.state_root / "backups" / self.project_id).glob("upgrade_schema6_*.sqlite3"))))
            # Approving again when there is nothing left to upgrade changes nothing.
            again = self._run("learning-register", "--upgrade-ledger")
            self.assertEqual((True, None, None), (again["ready"], again["ledger_upgraded_from"], again["ledger_copy"]))
            self.assertEqual([copy], list(copy.parent.iterdir()))
            self.assertTrue(self._run("learning-status")["active"])
        finally:
            self._stop_collector()

    def _register_here(self, *arguments: str, expected: int = 0) -> dict:
        """learning-register in this process, so a test's patches apply to it."""
        stdout = io.StringIO()
        argv = ["cli.py", "learning-register", "--state-root", str(self.state_root),
                "--project-root", str(self.project), *arguments]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
            code = cli_main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def _upgrade_backups(self) -> list[Path]:
        return sorted((self.state_root / "backups").glob("*/upgrade_schema6_*"))

    def test_a_project_backup_is_streamed_so_its_size_never_meets_a_memory_bound(self) -> None:
        # Release 0.3.0 built each project's upgrade backup as one document in memory
        # and refused any over 128 MiB, so a busy project could never be upgraded. The
        # backup is now written and checked row by row: building that document at all
        # is a regression.
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        records = ledger._connection.execute("SELECT count(*) FROM records").fetchone()[0]
        downgrade_ledger_to_six(ledger)
        ledger.close()
        whole = mock.patch.object(learning_store, "_snapshot_project_partition",
                                  side_effect=AssertionError("a whole partition was built in memory"))
        try:
            with whole:
                upgraded = self._register_here("--upgrade-ledger")
            self.assertEqual((True, 6), (upgraded["ready"], upgraded["ledger_upgraded_from"]))
            [backup] = self._upgrade_backups()
            self.assertEqual(".sqlite3", backup.suffix)
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            self._stop_collector()
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            try:
                with whole:
                    verified = ledger.verify_sanitized_backup(self.project_id, file_id=backup.stem)
            finally:
                ledger.close()
            self.assertEqual((True, 6, records), (
                verified["verified"], verified["ledger_schema_version"], verified["counts"]["records"]))
        finally:
            self._stop_collector()

    def _later_large_project(self, ledger) -> tuple[str, str]:
        """Two projects, the one backed up later made the larger: (earlier, later)."""
        from tests.test_large_project_backups import add_events

        root = self.project.parent / "second-project"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        other = ledger.register_project(root, now_ms=1)
        earlier, later = sorted((self.project_id, other))
        add_events(ledger, later, 400)
        return earlier, later

    def test_an_upgrade_that_cannot_fit_refuses_before_writing_any_backup(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            earlier, later = self._later_large_project(ledger)
            downgrade_ledger_to_six(ledger)
            # The earlier project fits; the later one is made too large by a limit
            # between their sizes, so an upgrade that sized projects one at a time
            # would already have written the earlier one's backup.
            active = [row[0] for row in ledger._connection.execute(
                "SELECT project_id FROM projects WHERE status='active' ORDER BY project_id")]
            sizes = {project: learning_store.project_backup_bytes(ledger._connection, project, 6)
                     for project in active}
        finally:
            ledger.close()
        self.assertTrue(all(sizes[later] > size for project, size in sizes.items() if project != later))
        self.assertLess(active.index(earlier), active.index(later))
        try:
            for patch, expected in (
                    # The later project alone is over the per-project limit.
                    (mock.patch.object(learning_store, "MAX_PROJECT_BACKUP_BYTES", sizes[later] - 1),
                     {"project_number": active.index(later) + 1, "projects": len(active),
                      "needed_bytes": sizes[later], "limit_bytes": sizes[later] - 1}),
                    # Every project fits, but not all of them on this disk.
                    (mock.patch.object(learning_store, "_free_backup_bytes", return_value=1),
                     {"project_number": None, "projects": len(active), "available_bytes": 1})):
                with self.subTest(expected=sorted(expected)), patch:
                    refused = self._register_here("--upgrade-ledger", expected=2)
                self.assertEqual(("ledger_upgrade_capacity", False), (refused["reason_code"], refused["ready"]))
                for name, value in expected.items():
                    self.assertEqual(value, refused["capacity"][name], name)
                self.assertIn("nothing was changed", refused["error"])
                self.assertEqual([], self._upgrade_backups())
                self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
                kept = sqlite3.connect(self.state_root / "learning.sqlite3")
                try:
                    self.assertEqual(0, kept.execute(
                        "SELECT count(*) FROM stored_files WHERE file_id LIKE 'upgrade_schema6_%'").fetchone()[0])
                finally:
                    kept.close()
            # With room, the same records upgrade, every project backed up.
            upgraded = self._register_here("--upgrade-ledger")
            self.assertEqual((True, 6), (upgraded["ready"], upgraded["ledger_upgraded_from"]))
            self.assertEqual(len(active), len(self._upgrade_backups()))
        finally:
            self._stop_collector()

    def test_a_failed_upgrade_names_its_cause_only_in_plain_fixed_words(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        try:
            for message, shown in (("project backup cannot represent records exactly",
                                    "project backup cannot represent records exactly"),
                                   # A path, or anything that could identify a project or account, is never shown.
                                   ("cannot read /srv/records/private/ledger", None),
                                   ("project prj_0123456789abcdef0123456789abcdef is not active", None)):
                ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
                if learning_store.ledger_schema_behind(ledger.state) is None:
                    downgrade_ledger_to_six(ledger)
                ledger.close()
                with self.subTest(message=message), \
                        mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True,
                                          side_effect=learning_store.LedgerError(message)):
                    report = self._register_here("--upgrade-ledger", expected=2)
                self.assertEqual(("ledger_upgrade_failed", shown), (report["reason_code"], report["cause"]))
                self.assertNotIn("/srv/records", json.dumps(report))
                self.assertNotIn("prj_0123456789abcdef", json.dumps(report))
        finally:
            self._stop_collector()

    def test_a_failure_after_the_upgrade_is_not_reported_as_records_left_unchanged(self) -> None:
        import cli
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        def register(patched, effect):
            ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
            downgrade_ledger_to_six(ledger)
            ledger.close()
            stdout = io.StringIO()
            arguments = ["cli.py", "learning-register", "--state-root", str(self.state_root),
                         "--project-root", str(self.project), "--upgrade-ledger"]
            with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, patched, side_effect=effect):
                code = cli_main()
            self.assertEqual(2, code, stdout.getvalue())
            return json.loads(stdout.getvalue())

        try:
            # The schema change is committed, then authorising the runtime fails.
            after = register("authorize_collector_runtime", OSError("disk"))
            self.assertEqual(("registration_incomplete_after_upgrade", 6), (
                after["reason_code"], after["ledger_schema_version"]))
            self.assertIn("were upgraded", after["error"])
            self.assertNotIn("as the earlier version left them", after["error"])
            self.assertTrue(Path(after["ledger_copy"]).is_file())
            self.assertIsNone(learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            # The copy itself cannot be made: nothing was upgraded, and the report says so plainly.
            stdout = io.StringIO()
            ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
            downgrade_ledger_to_six(ledger)
            ledger.close()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "copy_ledger_before_upgrade",
                                      side_effect=learning_store.LedgerError("the ledger could not be copied before its upgrade")):
                self.assertEqual(2, cli_main())
            uncopied = json.loads(stdout.getvalue())
            self.assertEqual(("ledger_upgrade_failed", None, None), (
                uncopied["reason_code"], uncopied["ledger_copy"], uncopied["state_moved"]))
            self.assertNotIn("copy", uncopied["error"])  # There is none to keep.
            # The upgrade and the registration went through and the collector then did not start:
            # still one report, with the copy, never a bare error.
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "ensure_collector", side_effect=cli.CollectorError("did not become healthy")):
                self.assertEqual(2, cli_main())
            unstarted = json.loads(stdout.getvalue())
            # Registration completed: it is not described as unfinished, and the cause is kept.
            self.assertEqual(("collector_not_started_after_registration", "did not become healthy"), (
                unstarted["reason_code"], unstarted["cause"]))
            self.assertIn("were upgraded and this project is registered", unstarted["error"])
            self.assertTrue(Path(unstarted["ledger_copy"]).is_file())
            # SQLite itself failing inside the migration is reported the same way as any other failure there.
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            downgrade_ledger_to_six(ledger)
            ledger.close()
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True,
                                      side_effect=sqlite3.OperationalError("disk I/O error")):
                self.assertEqual(2, cli_main())
            self.assertEqual("ledger_upgrade_failed", json.loads(stdout.getvalue())["reason_code"])
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            # The upgrade went through, registration failed, and the ledger cannot then be read.
            readings = iter([6, 6])
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "authorize_collector_runtime", side_effect=OSError("disk")), \
                    mock.patch.object(cli, "ledger_schema_behind", side_effect=lambda state: next(
                        readings, None) or (_ for _ in ()).throw(learning_store.LedgerError("unreadable"))):
                self.assertEqual(2, cli_main())
            unknown = json.loads(stdout.getvalue())
            self.assertEqual("ledger_upgrade_unknown", unknown["reason_code"])
            self.assertIn("do not delete anything; keep the copy", unknown["error"])
            self.assertTrue(Path(unknown["ledger_copy"]).is_file())
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            downgrade_ledger_to_six(ledger)
            ledger.close()
            # The migration fails inside its transaction: the earlier records are as they were.
            initialize = LearningLedger._initialize_launch_claims_locked

            def fail(ledger, **arguments):
                initialize(ledger, **arguments)
                raise learning_store.LedgerError("fixture migration failure")

            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True, side_effect=fail):
                self.assertEqual(2, cli_main())
            before = json.loads(stdout.getvalue())
            self.assertEqual("ledger_upgrade_failed", before["reason_code"])
            self.assertIn("as the earlier version left them", before["error"])
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
        finally:
            self._stop_collector()

    def test_a_ledger_is_found_and_copied_by_its_own_path_whatever_characters_that_holds(self) -> None:
        base = Path(self.temporary.name)
        for folder, decoy in (("st%41te", "stAte"), ("st?mode=rwc", "st"), ("st#x", "st")):
            root = base / folder
            state = LearningState(root)
            LearningLedger(state).close()
            connection = sqlite3.connect(state.database)
            connection.execute("PRAGMA user_version=6")
            connection.close()
            # What the path would name if it were read as a URI by hand: a current ledger.
            (base / decoy).mkdir(mode=0o700, exist_ok=True)
            other = sqlite3.connect(base / decoy / "learning.sqlite3")
            other.execute(f"PRAGMA user_version={learning_store.LEDGER_SCHEMA_VERSION}")
            other.execute("CREATE TABLE IF NOT EXISTS decoy(x)")
            other.commit()
            other.close()
            with self.subTest(folder=folder):
                self.assertEqual(6, learning_store.ledger_schema_behind(state))
                copy = learning_store.copy_ledger_before_upgrade(state, 6)
                self.assertEqual(root.resolve() / "ledger-before-upgrade", copy.parent)
                kept = sqlite3.connect(copy)
                try:
                    self.assertEqual(6, kept.execute("PRAGMA user_version").fetchone()[0])
                    self.assertIsNone(kept.execute("SELECT 1 FROM sqlite_master WHERE name='decoy'").fetchone())
                finally:
                    kept.close()
        # A ledger that is there but cannot be read is an error, never taken for a current one.
        broken = LearningState(base / "broken")
        LearningLedger(broken).close()
        broken.database.write_bytes(b"not a database, and long enough to be read as one" * 100)
        with self.assertRaisesRegex(learning_store.LedgerError, "cannot be read"):
            learning_store.ledger_schema_behind(broken)

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

    @contextlib.contextmanager
    def _account_services(self):
        """The collector and account services as threads of this process, as `collector_host.py` runs them.

        The account services run the observation, so they must see the fake clients as the
        trusted ones; a detached collector would resolve whatever client this machine has.
        """
        from codex_agents.account_host import AccountServiceHost
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.service_enrollment import capability_path

        stop, failures, ready = threading.Event(), [], threading.Event()

        def serve():
            ledger = LearningLedger(
                LearningState(self.state_root, create=False),
                client_capabilities=load_client_capabilities(
                    ORCHESTRATION_ROOT / "config" / "learning-clients.json"),
            )
            server, account = CollectorServer(ledger), None
            try:
                server.start()
                account = AccountServiceHost(ledger, CollectorPaths(self.state_root))
                ready.set()
                while not stop.is_set() and not server.wait(0.002):
                    account.serve_once()
            except BaseException as error:
                failures.append(repr(error))
            finally:
                ready.set()
                try:
                    if account is not None:
                        account.close()
                        end = time.monotonic() + 10
                        while not account.drained() and time.monotonic() < end:
                            time.sleep(0.01)
                finally:
                    server.stop()
                    ledger.close()

        with trusted_tools(self.tools):
            thread = threading.Thread(target=serve, name="test-collector")
            thread.start()
            try:
                self.assertTrue(ready.wait(30), "account services did not start")
                self.assertEqual([], failures)
                self.assertTrue(capability_path(CollectorPaths(self.state_root), None, "account").exists())
                yield
            finally:
                stop.set()
                thread.join(30)
            self.assertFalse(thread.is_alive(), "collector thread did not stop")
            self.assertEqual([], failures)

    def test_provider_observation_and_billing_attestation_commands(self) -> None:
        # The fake clients answer from recorded fixtures; nothing is spent. The
        # command and the account services run in-process so the trusted search
        # path can name them.
        with self._account_services():
            self._provider_observation_and_billing_attestation_commands()

    def _provider_observation_and_billing_attestation_commands(self) -> None:
        tools = self.tools
        observed = self._observe("claude")
        identity = observed["records"][0]
        self.assertEqual("provider_identity", identity["record_type"])
        self.assertNotIn("example.invalid", json.dumps(observed))
        self.assertNotIn("client_path", identity)
        self.assertNotIn(str(tools), json.dumps(observed))
        # The account service observes, and reuses its recent observation; raw
        # provider identifiers never reach its records or this output. (That an
        # identifier placed in a label field is dropped is tested on the observer
        # itself, in test_providers.)
        status = json.loads((tools / "claude-status.json").read_text())
        codex = json.loads((tools / "codex-result.json").read_text())
        for provider, raw in (("claude", status["orgId"]), ("codex", codex["accountId"])):
            self.assertNotIn(raw, self._observe(provider, raw_output=True))
        refused = self._observe(
            "claude", expected=2, env={"ANTHROPIC_API_KEY": "test-only"}
        )
        self.assertEqual("input validation failed", refused["error"])

        grant = self._write("billing-grant.json", {
            "version": 1, "provider": "claude",
            "account_fingerprint": identity["account_fingerprint"],
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 7,
        })
        request = self._run(
            "learning-record-billing-attestation",
            "--provider", "claude", "--grant", str(grant),
        )
        self.assertTrue(request["requires_signature"])
        mismatched = self._run(
            "learning-record-billing-attestation",
            "--provider", "codex", "--grant", str(grant), expected=2,
        )
        self.assertEqual("input validation failed", mismatched["error"])
        signature = sign_owner_payload(
            self.owner_private_key,
            base64.b64decode(request["signing_payload_b64"]),
            "billing-attestation-payload",
        )
        stored = self._run(
            "learning-record-billing-attestation",
            "--provider", "claude", "--grant", str(grant),
            "--signature", str(signature),
        )
        self.assertTrue(stored["stored"])
        self.assertEqual("billing_attestation", stored["record"]["record_type"])

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
