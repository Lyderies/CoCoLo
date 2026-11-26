"""Shared fixture and helpers of the learning CLI test modules (split out of test_learning_cli: CI round 6)."""
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


class LearningCliBase(unittest.TestCase):
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
