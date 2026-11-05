from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import MAX_DELIVERY_PAGE_BYTES, ProjectHistory
from tests.learning_support import event_record

PROJECT_A = "prj_" + "a" * 32
PROJECT_B = "prj_" + "b" * 32


def binding(number: int = 1, *, project: str = PROJECT_A) -> AttemptBinding:
    return AttemptBinding(project, "session-1", "a" * 64, "supervisor-1", 1,
                          "orc_" + "a" * 48, "hrn_" + f"{number:048x}", f"run-{number}",
                          1, f"claim-{number}", f"{number:064x}")


def terminal(number: int = 1) -> bytes:
    return canonical_json({
        "schema_version": 3, "record_type": "dispatch_result", "record_id": f"dispatch-{number}",
        "run_id": f"run-{number}", "bridge_run_id": "brg_" + f"{number:032x}",
        "result_digest": f"{number:064x}", "fenced": True, "reviewed": False,
        "terminal_state": "failed", "observed_at_ms": number,
        "result_validation_reason_code": "invalid_shape", "client_diagnostics": None,
    })


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base / "history-a"
        self.directory.mkdir(mode=0o700)
        self.store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(self.store.close)

    def test_canonical_bytes_and_first_ingest_metadata_survive_retry_and_reopen(self) -> None:
        first = canonical_json(event_record("evt-first", observed_at_ms=100))
        a = self.store.append_record(first, source="host", ingested_at_ms=200)
        late = canonical_json(event_record("evt-late", observed_at_ms=50))
        b = self.store.append_record(late, source="hook", ingested_at_ms=201)
        self.assertEqual((1, 2), (a.source_sequence, b.source_sequence))
        self.assertFalse(a.late)
        self.assertTrue(b.late)
        self.assertEqual(hashlib.sha256(first).hexdigest(), a.content_digest)
        self.assertEqual(first, a.content)
        self.assertEqual(a, self.store.append_record(first, source="host", ingested_at_ms=999))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.append_record(first, source="hook", ingested_at_ms=200)
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(self.store.close)
        self.assertEqual(a, self.store.record("evt-first"))
        self.assertEqual(b, self.store.record("evt-late"))

    def test_invalid_noncanonical_and_unsupported_records_create_no_rows(self) -> None:
        record = event_record("evt-invalid")
        for content in (b"CANARY-private-content", b'{"record_type":"event","record_type":"outcome"}',
                        json.dumps(record, indent=2).encode(), b"x" * 65_537, canonical_json({**record, "extra": 1})):
            with self.subTest(size=len(content)), self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
                self.store.append_record(content, source="host", ingested_at_ms=1000)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record(record["record_id"])
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.store.append_record(terminal(), source="host", ingested_at_ms=1000)

    def test_terminal_record_and_outbox_are_atomic_and_exactly_idempotent(self) -> None:
        content = terminal()
        with mock.patch.object(self.store, "_insert_terminal", side_effect=RuntimeError("fixture cut")):
            with self.assertRaisesRegex(RuntimeError, "fixture cut"):
                self.store.commit_terminal(content, binding(), ingested_at_ms=100)
        self.assertEqual((), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record("dispatch-1")
        delivery = self.store.commit_terminal(content, binding(), ingested_at_ms=101)
        self.assertEqual(1, self.store.record("dispatch-1").source_sequence)
        self.assertEqual(content, self.store.record("dispatch-1").content)
        self.assertEqual(delivery, self.store.commit_terminal(content, binding(), ingested_at_ms=999))
        self.assertEqual((delivery,), self.store.pending_terminals())
        self.assertEqual("invalid_shape", json.loads(self.store.record("dispatch-1").content)["result_validation_reason_code"])
        changed = json.loads(content)
        changed["reviewed"] = True
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(canonical_json(changed), binding(), ingested_at_ms=101)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(content, replace(binding(), primary_session_id="other"), ingested_at_ms=101)

    def test_wrong_project_or_run_and_reused_record_cannot_create_another_delivery(self) -> None:
        for wrong in (binding(project=PROJECT_B), replace(binding(), run_id="other")):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.store.commit_terminal(terminal(), wrong, ingested_at_ms=100)
        original = self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(terminal(), replace(binding(), claim_id="other-claim"), ingested_at_ms=100)
        self.assertEqual((original,), self.store.pending_terminals())

        # A conflicting claim cannot leave a second terminal record or consume a sequence.
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(terminal(2), replace(binding(2), claim_id="claim-1"), ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record("dispatch-2")
        second = self.store.commit_terminal(terminal(2), binding(2), ingested_at_ms=100)
        self.assertEqual(2, second.delivery_sequence)
        self.assertEqual(2, self.store.record("dispatch-2").source_sequence)

    def test_verified_receipt_ack_does_not_allow_changed_binding_or_receipt(self) -> None:
        delivery = self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.acknowledge_terminal("claim-1", "f" * 64, "a" * 64)
        self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64)
        self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64)
        self.assertEqual((), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "b" * 64)
        self.assertEqual(delivery, self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100))

    def test_delivery_pages_bound_encoded_bytes_and_allow_continuation(self) -> None:
        for index in range(1, 41):
            self.store.commit_terminal(terminal(index), binding(index), ingested_at_ms=100)
        seen = []
        offset = 0
        while True:
            page = self.store.pending_terminals(after_sequence=offset, limit=100)
            if not page:
                break
            self.assertLessEqual(len(canonical_json([item.as_data() for item in page])) + 1, MAX_DELIVERY_PAGE_BYTES)
            seen.extend(item.delivery_sequence for item in page)
            offset = page[-1].delivery_sequence
        self.assertEqual(list(range(1, 41)), seen)
        for options in ({"limit": True}, {"limit": 101}, {"after_sequence": -1}):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.store.pending_terminals(**options)

    def test_owner_affinity_and_lifetime_writer_lock(self) -> None:
        with self.assertRaisesRegex(HistoryError, "history_writer_busy"):
            ProjectHistory(self.directory, PROJECT_A)
        errors = []

        def other_thread():
            try:
                self.store.pending_terminals()
            except HistoryError as exc:
                errors.append(exc.reason_code)
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(timeout=5)
        self.assertEqual(["history_wrong_owner"], errors)
        self.store.close()
        with self.assertRaisesRegex(HistoryError, "history_wrong_owner"):
            self.store.pending_terminals()

    def test_wrong_project_and_unsafe_database_aliases_fail_closed(self) -> None:
        self.store.close()
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            ProjectHistory(self.directory, PROJECT_B)
        for alias in ("symlink", "hardlink"):
            path = self.base / alias
            path.mkdir(mode=0o700)
            target = path / "history.sqlite3"
            if alias == "symlink":
                target.symlink_to(self.directory / "history.sqlite3")
            else:
                os.link(self.directory / "history.sqlite3", target)
            with self.assertRaises(HistoryError):
                ProjectHistory(path, PROJECT_A)
        world = self.base / "world"
        world.mkdir(mode=0o755)
        with self.assertRaisesRegex(HistoryError, "history_unsafe_path"):
            ProjectHistory(world, PROJECT_A)

    def test_unexpected_schema_is_refused_without_running_its_triggers(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.directory / "history.sqlite3")
        connection.execute("CREATE TRIGGER hostile AFTER INSERT ON records BEGIN DELETE FROM records; END")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            ProjectHistory(self.directory, PROJECT_A)

    def test_database_failure_returns_only_a_fixed_code_without_exception_context(self) -> None:
        self.store._connection.execute("PRAGMA query_only=ON")
        with self.assertRaisesRegex(HistoryError, "history_unavailable") as caught:
            self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000)
        self.assertIsNone(caught.exception.__context__)

    def test_process_death_rolls_back_uncommitted_terminal_and_preserves_committed_outbox(self) -> None:
        self.store.close()
        program = """
import json,os,sys
from pathlib import Path
from codex_agents.history_contracts import AttemptBinding
from codex_agents.project_history import ProjectHistory
class Cut(ProjectHistory):
    def _insert_terminal(self, delivery, encoded):
        super()._insert_terminal(delivery, encoded)
        if sys.argv[4]=='before-commit': os._exit(71)
store=Cut(Path(sys.argv[1]),sys.argv[2])
request=json.loads(sys.argv[3])
store.commit_terminal(request['content'].encode('ascii'),AttemptBinding(**request['binding']),ingested_at_ms=100)
os._exit(72)
"""
        payload = json.dumps({"content": terminal().decode("ascii"), "binding": binding().as_data()})
        for cut in ("before-commit", "after-commit"):
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-S", "-c",
                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
                 str(self.directory), PROJECT_A, payload, cut],
                capture_output=True, timeout=15,
            )
            self.assertEqual(71 if cut == "before-commit" else 72, completed.returncode, completed.stderr)
            store = ProjectHistory(self.directory, PROJECT_A)
            try:
                if cut == "before-commit":
                    self.assertEqual((), store.pending_terminals())
                    with self.assertRaisesRegex(HistoryError, "history_not_found"):
                        store.record("dispatch-1")
                else:
                    self.assertEqual(terminal(), store.record("dispatch-1").content)
                    self.assertEqual(1, len(store.pending_terminals()))
            finally:
                store.close()

    def test_another_project_commits_during_project_a_writer_stall(self) -> None:
        directory = self.base / "history-b"
        directory.mkdir(mode=0o700)
        program = """
import json,sys
from pathlib import Path
from codex_agents.project_history import ProjectHistory
store=ProjectHistory(Path(sys.argv[1]),sys.argv[2])
value=json.loads(sys.stdin.readline())
entry=store.append_record(value['content'].encode('ascii'),source='host',ingested_at_ms=1000)
print(json.dumps({'source_sequence':entry.source_sequence}),flush=True)
store.close()
"""
        with self.store._transaction():
            # A real held write transaction in A; B uses the actual owner method.
            self.store._connection.execute("UPDATE history_identity SET next_source_sequence=next_source_sequence")
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-S", "-c",
                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
                 str(directory), PROJECT_B],
                input=json.dumps({"content": canonical_json(event_record()).decode("ascii")}) + "\n",
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual({"source_sequence": 1}, json.loads(completed.stdout))
        # A history database never creates a disconnected account/digest key.
        self.assertEqual({"history.lock", "history.sqlite3"}, {p.name for p in directory.iterdir()})


if __name__ == "__main__":
    unittest.main()
