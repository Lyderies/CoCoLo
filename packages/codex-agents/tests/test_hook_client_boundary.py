"""Fresh hook processes must not import persistence or touch database paths."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.collector_client import CollectorClient
from codex_agents.learning_store import (
    CollectorServer,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    client_health_snapshot,
)


GUARDED_HOOK = r'''
import importlib.abc
import os
import runpy
import sys

class NoPersistence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"sqlite3", "_sqlite3"} or fullname.startswith((
            "codex_agents.learning_store", "codex_agents.project_history",
            "codex_agents.history_service", "codex_agents.account_service",
            "codex_agents.orchestration_store", "codex_agents.orchestration_service",
        )):
            raise AssertionError("hook imported persistence owner: " + fullname)

def no_database(path):
    if isinstance(path, (str, bytes, os.PathLike)):
        value = os.fsdecode(path)
        if "learning.sqlite3" in value or value.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
            raise AssertionError("hook inspected a database path")

def audit(event, args):
    if event == "sqlite3.connect":
        raise AssertionError("hook opened SQLite")
    if event == "open":
        no_database(args[0])

sys.meta_path.insert(0, NoPersistence())
sys.addaudithook(audit)
for name in ("stat", "lstat"):
    original = getattr(os, name)
    def guarded(path, *args, _original=original, **kwargs):
        no_database(path)
        return _original(path, *args, **kwargs)
    setattr(os, name, guarded)

hook, state, project, startup = sys.argv[1:]
if startup == "deny":
    import subprocess
    def unavailable(*args, **kwargs):
        # Record no process at all; exercise the real bounded health timeout.
        return None
    subprocess.Popen = unavailable
sys.argv = [hook, "--state-root", state, "--checkout", project,
            "--client-version", "codex-hooks-v1"]
runpy.run_path(hook, run_name="__main__")
'''


class HookClientBoundaryTests(unittest.TestCase):
    def run_hook(self, state, project, *, startup="allow"):
        payload = {
            "hook_event_name": "PreToolUse", "session_id": "boundary-session",
            "turn_id": "boundary-turn", "tool_use_id": "boundary-tool",
            "tool_name": "shell", "cwd": str(project),
            "tool_input": {"command": "TEST_SECRET_MUST_NOT_REACH_STORAGE"},
        }
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", GUARDED_HOOK,
             str(ORCHESTRATION_ROOT / "learning_hook.py"), str(state.root),
             str(project), startup],
            input=json.dumps(payload), text=True, capture_output=True,
            check=False, timeout=15,
        )

    def fixture(self, directory):
        base = Path(directory).resolve()
        state = LearningState(base / "state")
        project = base / "project"
        (project / ".git").mkdir(parents=True)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        return state, project

    def test_actual_hook_ingests_without_persistence_imports_or_database_access(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state, project = self.fixture(directory)
            ledger = LearningLedger(state)
            project_id = ledger.register_project(project)
            server = CollectorServer(ledger)
            server.start()
            try:
                client = CollectorClient(state.socket)
                before = client.health()["started_at_ms"]
                result = self.run_hook(state, project)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                snapshot = ledger.ingestion_snapshot(project_id)
                self.assertEqual(snapshot["hook_event_counts"]["tool_started"], 1)
                self.assertEqual(client.health()["started_at_ms"], before)
                with ledger.repository(project_id) as repository:
                    records = repository.records()
                self.assertNotIn("TEST_SECRET", json.dumps(records))
            finally:
                server.stop()
                ledger.close()

    def test_unavailable_service_records_metadata_without_database_fallback(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state, project = self.fixture(directory)
            result = self.run_hook(state, project, startup="deny")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            # Only local metadata is available; there is no fallback ledger open.
            self.assertFalse(state.initialized.exists())
            health = client_health_snapshot(state, project)
            self.assertEqual(health["collector_start_failed"]["count"], 1)
            self.assertEqual(health["collector_unavailable"]["count"], 1)

    def test_unregistered_service_does_not_recreate_project_health(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state, project = self.fixture(directory)
            ledger = LearningLedger(state)
            server = CollectorServer(ledger)
            server.start()
            try:
                result = self.run_hook(state, project)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                self.assertEqual(client_health_snapshot(state, project), {})
            finally:
                server.stop()
                ledger.close()


if __name__ == "__main__":
    unittest.main()
