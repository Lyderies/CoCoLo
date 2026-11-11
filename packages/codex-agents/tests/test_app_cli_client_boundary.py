"""Actual App Server CLI calls use client paths and never inspect database files.

The complete CLI still imports legacy owners for other commands. This tests the
two converted command paths, not full CLI import isolation or native enrollment.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from codex_agents.collector_client import CollectorClient
from codex_agents.learning_store import (
    CollectorServer, LearningLedger, LearningState, authorize_collector_runtime,
)


GUARDED_CLI = r'''
import os
import runpy
import sys

def no_database(path):
    if isinstance(path, (str, bytes, os.PathLike)):
        value = os.fsdecode(path)
        if "learning.sqlite3" in value or value.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
            raise AssertionError("App Server CLI inspected a database path")

def audit(event, args):
    if event == "sqlite3.connect":
        raise AssertionError("App Server CLI opened SQLite")
    if event == "open":
        no_database(args[0])

sys.addaudithook(audit)
for name in ("stat", "lstat"):
    original = getattr(os, name)
    def guarded(path, *args, _original=original, **kwargs):
        no_database(path)
        return _original(path, *args, **kwargs)
    setattr(os, name, guarded)

sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class AppCliClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = LearningState(self.base / "state")
        self.project = self.base / "project"
        (self.project / ".git").mkdir(parents=True)
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.message = self.base / "message.json"
        self.message.write_text(json.dumps({
            "method": "turn/started",
            "params": {"threadId": "boundary-thread", "turnId": "boundary-turn",
                       "turn": {"id": "boundary-turn"}, "private": "DO_NOT_STORE_PRIVATE"},
        }))

    def command(self, name):
        arguments = [str(ORCHESTRATION_ROOT / "cli.py"), name,
                     "--state-root", str(self.state.root), "--message", str(self.message),
                     "--client-version", "codex-cli-0.153.0"]
        if name == "learning-ingest-app":
            arguments.extend(["--project-root", str(self.project)])
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", GUARDED_CLI, *arguments],
            capture_output=True, text=True, timeout=15, check=False,
        )

    def test_normalize_reads_only_client_key_and_never_inspects_database(self):
        before = self.state.database.read_bytes()
        run = self.command("learning-normalize-app")
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertTrue(report["ready"])
        self.assertEqual("turn_started", report["record"]["event_code"])
        self.assertNotIn("DO_NOT_STORE_PRIVATE", run.stdout)
        self.assertEqual(before, self.state.database.read_bytes())
        self.assertFalse(self.state.initialized.exists())

    def test_ingest_commits_on_actual_collector_without_client_database_access(self):
        ledger = LearningLedger(self.state)
        self.addCleanup(ledger.close)
        project_id = ledger.register_project(self.project)
        server = CollectorServer(ledger)
        server.start()
        self.addCleanup(server.stop)
        client = CollectorClient(self.state.socket)
        before = client.health()["started_at_ms"]
        run = self.command("learning-ingest-app")
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertTrue(json.loads(run.stdout)["stored"])
        self.assertEqual(before, client.health()["started_at_ms"])
        with ledger.repository(project_id) as repository:
            records = repository.records("event")
        self.assertEqual(1, len(records))
        self.assertEqual("turn_started", records[0]["event_code"])
        self.assertNotIn("DO_NOT_STORE_PRIVATE", json.dumps(records))

    def test_unavailable_collector_has_no_database_fallback(self):
        before = self.state.database.read_bytes()
        run = self.command("learning-ingest-app")
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        self.assertFalse(json.loads(run.stdout)["ready"])
        self.assertIn("collector is unavailable", run.stdout)
        self.assertNotIn("inspected a database", run.stdout + run.stderr)
        self.assertEqual(before, self.state.database.read_bytes())
        self.assertFalse(self.state.initialized.exists())
