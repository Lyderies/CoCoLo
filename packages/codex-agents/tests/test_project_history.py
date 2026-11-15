from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.contracts import ContractError
from codex_agents.history_contracts import MAX_SEQUENCE, AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json, validate_learning_record
from codex_agents.project_history import MAX_DELIVERY_PAGE_BYTES, MAX_HISTORY_ROW_BYTES, ProjectHistory
from codex_agents import project_history
from tests.learning_support import event_record, verification_outcome

PROJECT_A = "prj_" + "a" * 32
PROJECT_B = "prj_" + "b" * 32


def binding(number: int = 1, *, project: str = PROJECT_A) -> AttemptBinding:
    return AttemptBinding(project, "session-1", "a" * 64, "supervisor-1", 1,
                          "orc_" + "a" * 48, "hrn_" + f"{number:048x}", f"run-{number}",
                          1, f"claim-{number}", hashlib.sha256(f"claim-{number}".encode()).hexdigest())


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

    def test_supported_run_records_preserve_their_bytes(self) -> None:
        intent = {
            "schema_version": 1, "record_type": "dispatch_intent", "record_id": "intent-fixture",
            "run_id": "run-fixture", "bridge_run_id": "brg_" + "a" * 32,
            "provider": "codex", "model": "gpt-6-astra", "effort": "high",
            "observed_at_ms": 1000, "deadline_at_ms": 2000,
        }
        verification = {
            "schema_version": 1, "record_type": "verification_binding", "record_id": "binding-fixture",
            "run_id": "verifier-fixture", "worker_outcome_digest": "a" * 64, "observed_at_ms": 1000,
        }
        unverified = verification_outcome()
        unverified.update(status='unverified', accepted=False, admission_state='unverified')
        for record in (intent, unverified, verification):
            with self.subTest(kind=record["record_type"]):
                content = canonical_json(record)
                entry = self.store.append_record(content, source="host", ingested_at_ms=1000)
                self.assertEqual(content, entry.content)
                self.assertEqual(entry, self.store.record(record["record_id"]))

    def test_generic_outcome_append_cannot_claim_a_verified_disposition_from_any_source(self):
        from codex_agents.learning_contracts import SOURCES
        for status in ('accepted', 'rejected', 'failed', 'cancelled', 'interrupted'):
            for source in sorted(SOURCES):
                record = verification_outcome('generic-' + status + '-' + source)
                record.update(status=status, accepted=status == 'accepted',
                    admission_state='accepted' if status == 'accepted' else 'rejected',
                    terminal_failure=status in {'failed', 'cancelled', 'interrupted'})
                raw = canonical_json(record)
                self.store._checked_record(raw, source, 1000)  # Valid shape is insufficient authority.
                with self.subTest(status=status, source=source):
                    with self.assertRaisesRegex(HistoryError, '^history_invalid_input$'):
                        self.store.append_record(raw, source=source, ingested_at_ms=1000)
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual(1, self.store._connection.execute('SELECT next_source_sequence FROM history_identity').fetchone()[0])

    def test_generic_unverified_outcome_remains_byte_exact_and_replayable(self):
        record = verification_outcome('generic-unverified')
        record.update(status='unverified', accepted=False, admission_state='unverified')
        raw = canonical_json(record)
        entry = self.store.append_record(raw, source='host', ingested_at_ms=1000)
        self.assertEqual(raw, entry.content)
        self.assertEqual(entry, self.store.append_record(raw, source='host', ingested_at_ms=2000))
        self.assertEqual(entry, self.store.record(record['record_id']))

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

    def test_one_attempt_and_claim_digest_cannot_acquire_a_second_terminal_binding(self) -> None:
        original = self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100)
        other = json.loads(terminal(2))
        other["run_id"] = binding().run_id
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(canonical_json(other), replace(binding(), claim_id="claim-2", claim_digest="b" * 64),
                                       ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(terminal(2), replace(binding(2), claim_digest=binding().claim_digest), ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record("dispatch-2")
        self.assertEqual((original,), self.store.pending_terminals())

    def test_unfenced_completion_keeps_its_unknown_forced_terminal_state(self) -> None:
        record = json.loads(terminal())
        record.update(fenced=False, reviewed=True, terminal_state=None, result_validation_reason_code=None)
        content = canonical_json(record)
        delivery = self.store.commit_terminal(content, binding(), ingested_at_ms=100)
        self.assertEqual((delivery,), self.store.pending_terminals())
        self.assertEqual(content, self.store.record(record["record_id"]).content)
        self.assertIsNone(json.loads(self.store.record(record["record_id"]).content)["terminal_state"])

    def test_corrupted_binding_or_record_returns_fixed_failure_without_chained_input(self) -> None:
        self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100)
        for content in (b"CANARY-corrupted-json", b'{"CANARY-UNKNOWN-FIELD":1}'):
            self.store._connection.execute("UPDATE terminal_outbox SET binding=?", (content,))
            with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
                self.store.pending_terminals()
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
        self.store._connection.execute("UPDATE terminal_outbox SET binding=?", (canonical_json(binding().as_data()),))
        self.store._connection.execute("UPDATE terminal_outbox SET result_digest=?", ("f" * 64,))
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.store.pending_terminals()
        self.store._connection.execute("UPDATE terminal_outbox SET result_digest=?", (json.loads(terminal())["result_digest"],))
        self.store._connection.execute("UPDATE records SET content_digest=?", ("f" * 64,))
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.store.record("dispatch-1")

    def test_integer_and_text_blob_storage_cannot_allocate_or_escape_during_replay(self) -> None:
        content = canonical_json(event_record("event-replay"))
        self.store.append_record(content, source="host", ingested_at_ms=1000)
        self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000)
        for stored in (1 << 62, content.decode("ascii"), b"x" * 65_537):
            self.store._connection.execute("UPDATE records SET content=? WHERE record_id='event-replay'", (stored,))
            for operation in (
                lambda: self.store.record("event-replay"),
                lambda: self.store.append_record(content, source="host", ingested_at_ms=1000),
            ):
                with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
                    operation()
                self.assertIsNone(caught.exception.__context__)
        for stored in (1 << 62, canonical_json(binding().as_data()).decode("ascii"), b"x" * 4097):
            self.store._connection.execute("UPDATE terminal_outbox SET binding=?", (stored,))
            for operation in (
                self.store.pending_terminals,
                lambda: self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000),
                lambda: self.store.acknowledge_terminal("claim-1", hashlib.sha256(terminal()).hexdigest(), "f" * 64, runtime_digest="a" * 64),
            ):
                with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
                    operation()
                self.assertIsNone(caught.exception.__context__)

    def test_sqlite_length_limit_bounds_oversized_corrupt_rows_before_delivery(self) -> None:
        self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000)
        # Another test-only connection simulates a corrupt row that this owner
        # could never have written through its validators or connection limits.
        connection = sqlite3.connect(self.directory / "history.sqlite3")
        connection.execute("UPDATE terminal_outbox SET binding=?", (b"x" * (MAX_HISTORY_ROW_BYTES + 1),))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(HistoryError, "history_unavailable") as caught:
            self.store.pending_terminals()
        self.assertIsNone(caught.exception.__context__)

    def test_missing_or_corrupted_identity_counters_fail_with_fixed_reasons(self) -> None:
        self.store._connection.execute("UPDATE history_identity SET next_source_sequence=0,next_delivery_sequence='CANARY'")
        for operation in (
            lambda: self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000),
            lambda: self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000),
        ):
            with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
                operation()
            self.assertIsNone(caught.exception.__context__)
        self.store._connection.execute("DELETE FROM history_identity")
        for operation in (
            lambda: self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000),
            lambda: self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000),
        ):
            with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
                operation()
            self.assertIsNone(caught.exception.__context__)

    def test_corrupted_observation_aggregate_cannot_escape_or_append(self) -> None:
        self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000)
        self.store._connection.execute("UPDATE records SET observed_at_ms='CANARY'")
        with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
            self.store.append_record(canonical_json(event_record("evt-new")), source="host", ingested_at_ms=1000)
        self.assertIsNone(caught.exception.__context__)
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record("evt-new")

    def test_nested_oversized_record_failure_keeps_the_unavailable_reason(self) -> None:
        self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000)
        connection = sqlite3.connect(self.directory / "history.sqlite3")
        connection.execute("UPDATE records SET content=?", (b"x" * (MAX_HISTORY_ROW_BYTES + 1),))
        connection.commit()
        connection.close()
        for operation in (
            self.store.pending_terminals,
            lambda: self.store.acknowledge_terminal("claim-1", hashlib.sha256(terminal()).hexdigest(), "f" * 64, runtime_digest="a" * 64),
        ):
            with self.assertRaisesRegex(HistoryError, "history_unavailable") as caught:
                operation()
            self.assertIsNone(caught.exception.__context__)

    def test_unsupported_sqlite_limit_binding_is_refused_before_opening_files(self) -> None:
        directory = self.base / "unsupported-bindings"
        directory.mkdir(mode=0o700)
        with mock.patch.object(project_history.sqlite3, "SQLITE_LIMIT_LENGTH", None), \
                mock.patch.object(project_history.os, "open", side_effect=AssertionError("file opened")), \
                self.assertRaisesRegex(HistoryError, "history_incompatible"):
            ProjectHistory(directory, PROJECT_A)

    def test_verified_receipt_ack_does_not_allow_changed_binding_or_receipt(self) -> None:
        delivery = self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64, runtime_digest="b" * 64)
        self.assertEqual((delivery,), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.acknowledge_terminal("claim-1", "f" * 64, "a" * 64, runtime_digest="a" * 64)
        self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64, runtime_digest="a" * 64)
        self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64, runtime_digest="a" * 64)
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(self.store.close)
        self.assertEqual((), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64, runtime_digest="b" * 64)
        self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "a" * 64, runtime_digest="a" * 64)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.acknowledge_terminal("claim-1", delivery.record_content_digest, "b" * 64, runtime_digest="a" * 64)
        self.assertEqual(delivery, self.store.commit_terminal(terminal(), binding(), ingested_at_ms=100))
        self.assertEqual((), self.store.pending_terminals())

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
        for options in ({"limit": True}, {"limit": 101}, {"after_sequence": -1},
                        {"after_sequence": True}, {"after_sequence": MAX_SEQUENCE + 1}):
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

    def test_recycled_numeric_thread_identity_cannot_read_or_close_a_writer(self) -> None:
        owner_ident = threading.get_ident()
        errors = []

        def foreign_thread(operation):
            try:
                operation()
            except HistoryError as exc:
                errors.append(exc.reason_code)
            else:
                errors.append("accepted")

        replacement = SimpleNamespace(get_ident=lambda: owner_ident, current_thread=threading.current_thread)
        with mock.patch.object(project_history, "threading", replacement):
            worker = threading.Thread(target=foreign_thread, args=(self.store.pending_terminals,))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            # A closed owner still refuses a foreign close before its idempotent
            # fast path, without letting this negative fixture release any FD.
            self.store._closed = True
            try:
                worker = threading.Thread(target=foreign_thread, args=(self.store.close,))
                worker.start()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
            finally:
                self.store._closed = False
        self.assertEqual(["history_wrong_owner", "history_wrong_owner"], errors)
        self.assertEqual((), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_writer_busy"):
            ProjectHistory(self.directory, PROJECT_A)

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

    def test_replaceable_ancestor_is_refused_before_sqlite_open(self) -> None:
        parent = self.base / "replaceable-ancestor"
        parent.mkdir()
        parent.chmod(0o777)  # No sticky bit: another user could rename its children.
        self.addCleanup(parent.chmod, 0o700)
        leaf = parent / "protected-leaf"
        leaf.mkdir(mode=0o700)
        with mock.patch.object(project_history.sqlite3, "connect", side_effect=AssertionError("SQLite opened")), \
                self.assertRaisesRegex(HistoryError, "history_unsafe_path"):
            ProjectHistory(leaf, PROJECT_A)
        self.assertEqual([], list(leaf.iterdir()))

    def test_lock_and_sidecar_aliases_are_refused_before_sqlite_open(self) -> None:
        target = self.base / "untouched-canary"
        target.write_bytes(b"CANARY")
        target.chmod(0o600)
        for name in ("history.lock", "history.sqlite3-wal", "history.sqlite3-shm", "history.sqlite3-journal"):
            for alias in ("symlink", "hardlink"):
                directory = self.base / (name + alias)
                directory.mkdir(mode=0o700)
                if alias == "symlink":
                    (directory / name).symlink_to(target)
                else:
                    os.link(target, directory / name)
                with self.subTest(name=name, alias=alias), \
                        mock.patch.object(project_history.sqlite3, "connect", side_effect=AssertionError("SQLite opened")), \
                        self.assertRaises(HistoryError):
                    ProjectHistory(directory, PROJECT_A)
                self.assertEqual(b"CANARY", target.read_bytes())
                (directory / name).unlink()

    def test_incompatible_version_application_and_columns_are_not_opened_as_history(self) -> None:
        for index, mutation in enumerate((f"PRAGMA user_version={project_history._SCHEMA_VERSION + 1}", "PRAGMA application_id=123",
                                         "ALTER TABLE records RENAME COLUMN source TO wrong_source")):
            directory = self.base / f"incompatible-{index}"
            directory.mkdir(mode=0o700)
            other = ProjectHistory(directory, PROJECT_A)
            other.close()
            connection = sqlite3.connect(directory / "history.sqlite3")
            connection.execute(mutation)
            connection.commit()
            connection.close()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(HistoryError, "history_incompatible"):
                ProjectHistory(directory, PROJECT_A)

    def test_sequence_exhaustion_does_not_wrap_or_leave_terminal_rows(self) -> None:
        self.store._connection.execute("UPDATE history_identity SET next_source_sequence=?", (MAX_SEQUENCE,))
        with self.assertRaisesRegex(HistoryError, "history_sequence_exhausted"):
            self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000)
        self.store._connection.execute("UPDATE history_identity SET next_source_sequence=1,next_delivery_sequence=?", (MAX_SEQUENCE,))
        with self.assertRaisesRegex(HistoryError, "history_sequence_exhausted"):
            self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000)
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.store.record("dispatch-1")
        self.assertEqual((), self.store.pending_terminals())

    def test_unsupported_record_is_rejected_before_generic_validation(self) -> None:
        content = canonical_json({"record_type": "provider_headroom", "windows": [{"used_percent": 10**400}]})
        with mock.patch.object(project_history, "validate_learning_record", side_effect=AssertionError("wrong validator")), \
                self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
            self.store.append_record(content, source="provider", ingested_at_ms=1000)
        self.assertIsNone(caught.exception.__context__)

    def test_boolean_float_versions_and_numeric_overflow_are_fixed_rejections(self) -> None:
        legacy = {key: value for key, value in json.loads(terminal()).items()
                  if key not in {"client_diagnostics", "result_validation_reason_code"}}
        legacy["schema_version"] = 1
        for version in (True, 1.0):
            for record in (event_record("evt-invalid-version"), dict(legacy)):
                record["schema_version"] = version
                with self.subTest(version=version, kind=record["record_type"]), \
                        self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                    self.store._checked_record(canonical_json(record), "host", 1000)
        with mock.patch.object(project_history, "validate_learning_record", side_effect=OverflowError("CANARY")), \
                self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
            self.store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000)
        self.assertIsNone(caught.exception.__context__)

        # The shared provider validator also rejects an out-of-range integer
        # before math.isfinite would convert it to an overflowing C double.
        headroom = {
            "schema_version": 1, "record_type": "provider_headroom", "record_id": "headroom-fixture",
            "provider": "codex", "account_fingerprint": "a" * 64,
            "windows": [{"window_minutes": 300, "scope": None, "used_percent": 10**400, "resets_at": 1000}],
            "status": None, "run_id": None, "phase": None, "observed_at_ms": 1000,
        }
        with self.assertRaises(ContractError):
            validate_learning_record(headroom)

    def test_other_process_cannot_acquire_the_same_writer(self) -> None:
        program = """
import json,sys
from pathlib import Path
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
try: ProjectHistory(Path(sys.argv[1]),sys.argv[2])
except HistoryError as exc: print(json.dumps({'reason':exc.reason_code}))
else: raise SystemExit(1)
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
             str(self.directory), PROJECT_A], capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"reason": "history_writer_busy"}, json.loads(completed.stdout))

    @unittest.skipUnless(hasattr(os, "fork"), "process-affinity check requires fork")
    def test_forked_process_cannot_use_or_close_the_inherited_writer(self) -> None:
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            errors = []
            for operation in (self.store.pending_terminals, self.store.close):
                try:
                    operation()
                except HistoryError as exc:
                    errors.append(exc.reason_code)
            os.write(writer, b"1" if errors == ["history_wrong_owner"] * 2 else b"0")
            os.close(writer)
            os._exit(0)
        os.close(writer)
        try:
            if not select.select([reader], [], [], 5)[0]:
                os.kill(child, signal.SIGKILL)
                self.fail("forked fixture did not finish")
            self.assertEqual(b"1", os.read(reader, 1))
        finally:
            os.close(reader)
            os.waitpid(child, 0)
        # The inherited close was refused without releasing this process's lock.
        with self.assertRaisesRegex(HistoryError, "history_writer_busy"):
            ProjectHistory(self.directory, PROJECT_A)

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
                    expected = store.commit_terminal(terminal(), binding(), ingested_at_ms=999)
                    self.assertEqual((expected,), store.pending_terminals())
                    self.assertEqual(binding(), expected.binding)
                    self.assertEqual(hashlib.sha256(terminal()).hexdigest(), expected.record_content_digest)
                    self.assertEqual(json.loads(terminal())["result_digest"], expected.result_digest)
                    self.assertEqual(1, expected.delivery_sequence)
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
