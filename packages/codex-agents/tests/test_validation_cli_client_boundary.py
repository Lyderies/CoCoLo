"""Validator rejection metadata uses the collector without client database I/O.

No rejected child output is replayed or repaired. Complete CLI import isolation
and native caller enrollment remain outside these command-path tests.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

from tests.support import ORCHESTRATION_ROOT
from tests import test_app_cli_client_boundary as app_fixtures
from tests import test_contracts as contract_fixtures
from codex_agents.collector_client import CollectorClient
from codex_agents.learning_store import CollectorServer, LearningLedger


GUARDED_VALIDATOR = app_fixtures.GUARDED_CLI.replace(
    'sys.argv = sys.argv[1:]',
    '''
startup = sys.argv.pop(1)
if startup == "deny":
    import subprocess
    original_popen = subprocess.Popen
    def no_collector(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "learning-serve" in command:
            return None
        return original_popen(command, *args, **kwargs)
    subprocess.Popen = no_collector
sys.argv = sys.argv[1:]
''',
)


class ValidationCliClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = app_fixtures.AppCliClientBoundaryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state, self.project = self.fixture.state, self.fixture.project
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "source.txt").write_text("fixture\n")
        contracts = contract_fixtures.ContractTests(methodName="runTest")
        values = {
            "task.json": contracts.task(), "assignment.json": contracts.assignment(),
            "parent.json": contracts.parent_grant(),
            "result.json": {"assignment_id": "assignment-1", "status": "completed",
                "summary": "DO_NOT_STORE_REJECTED_CONTENT", "artifacts": [], "evidence": [],
                "unverified_claims": [], "remaining_risks": []},
        }
        for name, value in values.items():
            (self.fixture.base / name).write_text(json.dumps(value))

    def validate(self, *, startup="allow"):
        arguments = [str(ORCHESTRATION_ROOT / "cli.py"), "validate-result",
                     "--state-root", str(self.state.root), "--project-root", str(self.project)]
        for option, name in (("--task", "task.json"), ("--assignment", "assignment.json"),
                             ("--parent-grant", "parent.json"), ("--result", "result.json")):
            arguments.extend([option, str(self.fixture.base / name)])
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", GUARDED_VALIDATOR, startup, *arguments],
            capture_output=True, text=True, check=False, timeout=15,
        )

    def test_rejection_metadata_reaches_actual_collector_without_client_database_access(self):
        ledger = LearningLedger(self.state)
        self.addCleanup(ledger.close)
        ledger.register_project(self.project)
        server = CollectorServer(ledger)
        server.start()
        self.addCleanup(server.stop)
        client = CollectorClient(self.state.socket)
        before = client.health()["started_at_ms"]
        run = self.validate()
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertFalse(report["ready"])
        self.assertEqual("missing_evidence", report["reason_code"])
        self.assertTrue(report["learning_ready"])
        self.assertRegex(report["repair_receipt"], r"^rpr_[0-9a-f]{48}$")
        self.assertNotIn("DO_NOT_STORE_REJECTED_CONTENT", run.stdout + run.stderr)
        self.assertEqual(before, client.health()["started_at_ms"])

    def test_unavailable_collector_preserves_rejection_and_has_no_database_fallback(self):
        before = self.state.database.read_bytes()
        run = self.validate(startup="deny")
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertFalse(report["ready"])
        self.assertEqual("missing_evidence", report["reason_code"])
        self.assertFalse(report["learning_ready"])
        self.assertIsNone(report["repair_receipt"])
        self.assertNotIn("DO_NOT_STORE_REJECTED_CONTENT", run.stdout + run.stderr)
        self.assertEqual(before, self.state.database.read_bytes())
        self.assertFalse(self.state.initialized.exists())
