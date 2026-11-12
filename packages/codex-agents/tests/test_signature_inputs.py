from __future__ import annotations

import os
import hashlib
import sqlite3
import time
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import attest_provider, generate_test_owner_key
from tests.test_owner_key_contracts import public_value

from codex_agents.account_api import ACCOUNT_OPERATIONS, AccountError
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_contracts import MAX_RECORD_BYTES
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import (
    OWNER_SIGNERS_MAX_BYTES, LearningLedger, LearningState, LedgerError, StateError, authorize_collector_runtime,
)
from codex_agents.signature_inputs import (
    MAX_SIGNATURE_CAPTURE_BYTES, MAX_SIGNATURE_CAPTURE_ROWS,
    SignatureCaptureError, SignatureCaptureLimits, signature_input_size,
)
from codex_agents.sql_read_budget import SQLReadLimits


class _CaptureConnectionTap:
    """Observe fetched preliminary values without hiding assertion failures."""

    def __init__(self, connection):
        self.connection = connection
        self.headers = []
        self.owner_headers = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        cursor = self.connection.execute(sql, parameters)
        owner_metadata = sql.startswith("SELECT CASE WHEN typeof(sequence)")
        if not owner_metadata and not sql.startswith("SELECT typeof(r.record_id)"):
            return cursor
        headers = self.headers
        owner_headers = self.owner_headers

        class Cursor:
            def fetchone(self):
                row = cursor.fetchone()
                owner_headers.append(None if row is None else tuple(row))
                return row

            def fetchall(self):
                rows = cursor.fetchall()
                headers.extend(tuple(row) for row in rows)
                return rows

        return Cursor()


class SignatureCaptureLimitTests(unittest.TestCase):
    def test_limits_are_explicit_exact_integers_with_finite_ceilings(self):
        for rows, size in ((True, 1), (1, False), (0, 1), (1, 0), (-1, 1), (1, -1),
                           (1.0, 1), (1, "CANARY"), (MAX_SIGNATURE_CAPTURE_ROWS + 1, 1),
                           (1, MAX_SIGNATURE_CAPTURE_BYTES + 1)):
            with self.subTest(rows=rows, size=size), self.assertRaises(SignatureCaptureError) as caught:
                SignatureCaptureLimits(rows, size)
            self.assertEqual("signature_capture_invalid_request", str(caught.exception))
        self.assertEqual(1, SignatureCaptureLimits(1, 1).max_bytes)
        limits = SignatureCaptureLimits(MAX_SIGNATURE_CAPTURE_ROWS, MAX_SIGNATURE_CAPTURE_BYTES)
        with self.assertRaises(FrozenInstanceError):
            limits.max_rows = 1

    def test_unknown_error_text_never_becomes_a_reason(self):
        for value in ("CANARY-PRIVATE", {}, True, None):
            self.assertEqual("signature_capture_invalid_request", str(SignatureCaptureError(value)))


class SignatureInputOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = LearningState(self.base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.key, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.roots = [self.base / name for name in ("project-a", "project-b")]
        for root in self.roots:
            root.mkdir()
        self.projects = [self.ledger.register_project(root, now_ms=i + 1) for i, root in enumerate(self.roots)]
        self.record = attest_provider(self.ledger, self.projects[0], self.key, "codex", "a" * 64)
        self.views = AccountOwnerViews(self.ledger)
        self.limits = SignatureCaptureLimits(8, 65_536)
        self.work_limits = SQLReadLimits(10_000, 1000, MAX_SIGNATURE_CAPTURE_BYTES)

    def capture(self, *, project=None, provider="codex", limits=None, work_limits=None):
        return self.views.capture_signature_inputs(
            self.projects[0] if project is None else project, provider,
            self.limits if limits is None else limits,
            self.work_limits if work_limits is None else work_limits,
        )

    def assert_capture_error(self, reason, action):
        with self.assertRaises(SignatureCaptureError) as caught:
            action()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def add_unrelated_history(self):
        connection = self.ledger._connection
        original = dict(connection.execute("SELECT * FROM records WHERE project_id=?",
                                           (self.projects[0],)).fetchone())
        rows = [{**original, "record_id": "CANARY-event-" + str(i), "record_type": "event", "source_sequence": i}
                for i in range(100, 5100)]
        connection.executemany("INSERT INTO records(" + ",".join(original) + ") VALUES(" +
                               ",".join("?" for _ in original) + ")", [tuple(row.values()) for row in rows])

    def test_capture_matches_legacy_inputs_without_crypto_connections_or_writes(self):
        rows = self.ledger._attestation_rows_locked(self.projects[0], "codex")
        owner = self.ledger._owner_generation_locked()
        signer = self.state.allowed_signers_path.read_bytes()
        before = self.ledger._connection.total_changes
        history = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")]
        with mock.patch("codex_agents.learning_store.sqlite3.connect", side_effect=AssertionError("second connection")), \
                mock.patch.object(self.state, "digest_key", side_effect=AssertionError("secret key")), \
                mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            captured = self.capture()
        self.assertIs(self.ledger._attestation_origin, captured.origin)
        self.assertEqual((self.projects[0], "codex", owner, signer, rows),
                         (captured.project_id, captured.provider, captured.owner, captured.signer, captured.rows))
        self.assertEqual(signature_input_size(self.projects[0], "codex", owner, signer, rows), captured.retained_bytes)
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(history, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")])
        self.assertEqual(signer, self.state.allowed_signers_path.read_bytes())
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual((), self.capture(project=self.projects[1]).rows)
        self.assertEqual({"account.health", "account.project", "account.owner_key", "account.owner_public_key",
                          "account.open_host_run", "account.claim_launch", "account.capacity_status", "account.register_execution", "account.submit_terminal", "account.release_capacity", "account.close_denied_attempt", "account.run_provenance", "account.open_provenance", "account.observe_provider", "account.record_observation_unavailable", "account.billing_probe"},
                         ACCOUNT_OPERATIONS)

    def test_inputs_are_immutable_hidden_and_never_a_prepared_signature_proof(self):
        captured = self.capture()
        with self.assertRaises(FrozenInstanceError):
            captured.provider = "claude"
        self.assertNotIn(captured.rows[0][0], repr(captured))
        self.assertFalse(hasattr(captured, "verified"))
        with mock.patch.object(self.ledger, "_owner_generation_locked", side_effect=AssertionError("proof backend")):
            with self.assertRaisesRegex(LedgerError, "preparation is stale"):
                self.ledger._prepared_attestations_locked(captured, self.projects[0], "codex")

    def test_exact_field_byte_boundary_and_one_byte_overflow_before_body_copy(self):
        captured = self.capture()
        exact = SignatureCaptureLimits(1, captured.retained_bytes)
        self.assertEqual(captured.rows, self.capture(limits=exact).rows)
        with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=AssertionError("body copy")) as copy:
            self.assert_capture_error("signature_capture_limit", lambda: self.capture(
                limits=SignatureCaptureLimits(1, exact.max_bytes - 1)))
        copy.assert_not_called()

    def test_row_overflow_never_truncates_or_calls_body_copy(self):
        attest_provider(self.ledger, self.projects[0], self.key, "codex", "a" * 64, issued_at=2)
        with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=AssertionError("body copy")) as copy:
            self.assert_capture_error("signature_capture_limit", lambda: self.capture(
                limits=SignatureCaptureLimits(1, 65_536)))
        copy.assert_not_called()
        captured = self.capture(limits=SignatureCaptureLimits(2, 65_536))
        self.assertEqual(2, len(captured.rows))
        self.assertEqual(self.ledger._attestation_rows_locked(self.projects[0], "codex"), captured.rows)

    def test_other_provider_history_is_charged_before_filtering(self):
        attest_provider(self.ledger, self.projects[0], self.key, "claude", "b" * 64, issued_at=2)
        self.assert_capture_error("signature_capture_limit", lambda: self.capture(
            limits=SignatureCaptureLimits(1, 65_536)))
        captured = self.capture(limits=SignatureCaptureLimits(2, 65_536))
        self.assertEqual(1, len(captured.rows))
        self.assertEqual(self.record["record_id"], captured.rows[0][0])
        self.assertNotEqual(captured.rows, self.capture(provider="claude").rows)

    def test_utf8_and_nul_in_metadata_cannot_undercharge_before_body_copy(self):
        captured = self.capture()
        self.ledger._connection.execute("UPDATE records SET source=? WHERE project_id=? AND record_id=?",
                                        ("owner\0" + "\U0001f984" * 512, self.projects[0], self.record["record_id"]))
        with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=AssertionError("body copy")) as copy:
            self.assert_capture_error("signature_capture_limit", lambda: self.capture(
                limits=SignatureCaptureLimits(1, captured.retained_bytes + 10)))
        copy.assert_not_called()

    def test_invalid_stored_body_and_signature_types_or_sizes_refuse_before_copy(self):
        connection = self.ledger._connection
        original_content = connection.execute("SELECT content FROM records WHERE project_id=? AND record_id=?",
                                              (self.projects[0], self.record["record_id"])).fetchone()[0]
        original_signature = connection.execute(
            "SELECT signature FROM owner_attestations WHERE project_id=? AND subject_record_id=?",
            (self.projects[0], self.record["record_id"]),
        ).fetchone()[0]
        variants = (("content", b"x" * (MAX_RECORD_BYTES + 1)), ("content", "CANARY-TEXT"),
                    ("signature", b"x" * 16_385), ("signature", "CANARY-TEXT"))
        for field, value in variants:
            with self.subTest(field=field, kind=type(value).__name__):
                if field == "content":
                    connection.execute("UPDATE records SET content=? WHERE project_id=? AND record_id=?",
                                       (value, self.projects[0], self.record["record_id"]))
                else:
                    connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                       (value, self.projects[0], self.record["record_id"]))
                with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=AssertionError("body copy")) as copy:
                    self.assert_capture_error("signature_capture_incompatible", self.capture)
                copy.assert_not_called()
                connection.execute("UPDATE records SET content=? WHERE project_id=? AND record_id=?",
                                   (original_content, self.projects[0], self.record["record_id"]))
                connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                   (original_signature, self.projects[0], self.record["record_id"]))

    def test_invalid_digest_and_backend_exceptions_are_fixed_without_context(self):
        with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=LedgerError("CANARY-CONTENT")):
            self.assert_capture_error("signature_capture_incompatible", self.capture)
        with mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=OSError("CANARY-PATH")):
            self.assert_capture_error("signature_capture_unavailable", self.capture)
        self.ledger._connection.execute("UPDATE records SET content_digest=? WHERE project_id=? AND record_id=?",
                                        ("0" * 64, self.projects[0], self.record["record_id"]))
        self.assert_capture_error("signature_capture_incompatible", self.capture)

    def test_malformed_sequence_never_returns_variable_data_from_preliminary_cursor(self):
        connection = self.ledger._connection
        for value in (b"CANARY" * 11_000, "CANARY" * 11_000, 1.5, 0, -1):
            with self.subTest(kind=type(value).__name__):
                connection.execute("UPDATE records SET source_sequence=? WHERE project_id=? AND record_id=?",
                                   (value, self.projects[0], self.record["record_id"]))
                tap = _CaptureConnectionTap(connection)
                with mock.patch.object(self.ledger, "_connection", tap), mock.patch.object(
                        self.ledger, "_attestation_rows_locked", side_effect=AssertionError("body copy")) as copy:
                    self.assert_capture_error("signature_capture_incompatible", self.capture)
                copy.assert_not_called()
                self.assertEqual(1, len(tap.headers))
                if type(value) is int:
                    self.assertEqual(value, tap.headers[0][8])
                else:
                    self.assertIsNone(tap.headers[0][8])

    def test_invalid_owner_history_refuses_before_fetching_signer_bytes(self):
        connection = self.ledger._connection
        for sequence, (line, timestamp) in enumerate((
                (b"CANARY" * 11_000, 42), ("CANARY-TEXT", 42), (b"", 42),
                (b"CANARY", "CANARY" * 11_000)), start=2):
            with self.subTest(sequence=sequence):
                connection.execute(
                    "INSERT INTO owner_key_history(sequence,signer_line,reason,recorded_at_ms) "
                    "VALUES(?,?,'replaced',?)", (sequence, line, timestamp))
                tap = _CaptureConnectionTap(connection)
                with mock.patch.object(self.ledger, "_connection", tap), \
                        mock.patch.object(self.ledger, "_owner_generation_locked") as owner, mock.patch.object(
                        self.ledger, "_current_signer_bytes") as signer:
                    self.assert_capture_error("signature_capture_incompatible", self.capture)
                owner.assert_not_called()
                signer.assert_not_called()
                self.assertEqual(1, len(tap.owner_headers))
                self.assertEqual(5, len(tap.owner_headers[0]))
                self.assertTrue(all(type(value) is not bytes and (type(value) is not str or len(value) < 16)
                                    for value in tap.owner_headers[0]))
        self.assertGreater(66_000, OWNER_SIGNERS_MAX_BYTES)

    def test_maximal_raw_owner_signer_is_captured_without_canonicalization(self):
        line = b"CANARY".ljust(OWNER_SIGNERS_MAX_BYTES, b"x")
        self.ledger._connection.execute(
            "INSERT INTO owner_key_history(sequence,signer_line,reason,recorded_at_ms) VALUES(2,?,'replaced',42)",
            (line,))
        self.assertEqual(line, self.capture().owner[1])

    def test_nontext_record_metadata_refuses_before_copy(self):
        connection = self.ledger._connection
        original = dict(connection.execute("SELECT * FROM records WHERE project_id=?",
                                           (self.projects[0],)).fetchone())
        for sequence, column in enumerate(("record_id", "content_digest", "source"), start=100):
            with self.subTest(column=column):
                row = {**original, "record_id": "CANARY-" + column, "source_sequence": sequence, column: b"CANARY"}
                connection.execute("INSERT INTO records(" + ",".join(row) + ") VALUES(" +
                                   ",".join("?" for _ in row) + ")", tuple(row.values()))
                try:
                    with mock.patch.object(self.ledger, "_attestation_rows_locked") as copy:
                        self.assert_capture_error("signature_capture_incompatible", self.capture)
                    copy.assert_not_called()
                finally:
                    connection.execute("DELETE FROM records WHERE project_id=? AND record_id=?",
                                       (self.projects[0], row["record_id"]))

    def test_owner_base_quota_refuses_before_fetching_signer_bytes(self):
        with mock.patch.object(self.ledger, "_owner_generation_locked") as owner, mock.patch.object(
                self.ledger, "_current_signer_bytes") as signer:
            self.assert_capture_error("signature_capture_limit", lambda: self.capture(
                limits=SignatureCaptureLimits(1, 1)))
        owner.assert_not_called()
        signer.assert_not_called()

    def test_exact_maximal_body_is_captured_without_crypto(self):
        prefix, suffix = b'{"provider":"codex","padding":"', b'"}'
        body = prefix + b"x" * (MAX_RECORD_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(MAX_RECORD_BYTES, len(body))
        self.ledger._connection.execute(
            "UPDATE records SET content=?,content_digest=? WHERE project_id=? AND record_id=?",
            (body, hashlib.sha256(body).hexdigest(), self.projects[0], self.record["record_id"]))
        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            captured = self.capture(limits=SignatureCaptureLimits(1, MAX_SIGNATURE_CAPTURE_BYTES))
        self.assertEqual(body, captured.rows[0][2])

    def test_other_provider_bytes_are_charged_independently_of_row_limit(self):
        attest_provider(self.ledger, self.projects[0], self.key, "claude", "b" * 64, issued_at=2)
        captured = self.capture()
        other = self.ledger._attestation_rows_locked(self.projects[0], "claude")
        total = signature_input_size(self.projects[0], "codex", captured.owner, captured.signer, captured.rows + other)
        self.assertEqual(captured.rows, self.capture(limits=SignatureCaptureLimits(2, total)).rows)
        with mock.patch.object(self.ledger, "_attestation_rows_locked") as copy:
            self.assert_capture_error("signature_capture_limit", lambda: self.capture(
                limits=SignatureCaptureLimits(2, total - 1)))
        copy.assert_not_called()

    def test_postcopy_underestimate_and_extra_rows_are_rejected(self):
        row = self.capture().rows[0]
        bigger = (*row[:4], row[4] + "CANARY", row[5])
        for rows in ((bigger,), (row, row)):
            with self.subTest(count=len(rows)), mock.patch.object(
                    self.ledger, "_attestation_rows_locked", return_value=rows):
                self.assert_capture_error("signature_capture_incompatible", lambda: self.capture(
                    limits=SignatureCaptureLimits(1, 65_536)))

    def test_unsafe_database_hardening_refuses_before_owner_access(self):
        with mock.patch.object(self.state, "harden_database_files", side_effect=StateError("CANARY-PATH")), \
                mock.patch.object(self.ledger, "_owner_generation_locked") as owner:
            self.assert_capture_error("signature_capture_unavailable", self.capture)
        owner.assert_not_called()

    def test_unrelated_history_scan_exhausts_work_before_body_copy_then_recovers(self):
        captured = self.capture()
        connection = self.ledger._connection
        self.add_unrelated_history()
        before = connection.total_changes
        with mock.patch.object(self.ledger, "_attestation_rows_locked") as copy:
            self.assert_capture_error("signature_capture_work_limit", lambda: self.capture(
                work_limits=SQLReadLimits(4, 1000, MAX_SIGNATURE_CAPTURE_BYTES)))
        copy.assert_not_called()
        self.assertEqual(captured.rows, self.capture().rows)
        self.assertEqual(before, connection.total_changes)
        self.assertEqual(5000, connection.execute("PRAGMA busy_timeout").fetchone()[0])

    def test_capture_deadline_refuses_during_scan_and_restores_the_owner(self):
        captured = self.capture()
        self.add_unrelated_history()
        connection = self.ledger._connection
        observed = []

        def clock():
            scope = connection._sql_scope
            if scope is not None and scope.callbacks:
                observed.append(scope.callbacks)
                return 1_000_000_000
            return 0

        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=clock), \
                mock.patch.object(self.ledger, "_attestation_rows_locked") as copy:
            self.assert_capture_error("signature_capture_work_limit", self.capture)
        copy.assert_not_called()
        self.assertTrue(observed)
        self.assertIsNone(connection._sql_scope)
        self.assertFalse(connection._sql_unusable)
        self.assertEqual(captured.rows, self.capture().rows)

    def test_capture_busy_refusal_is_bounded_and_restores_the_previous_timeout(self):
        connection = self.ledger._connection
        # A private rollback-journal fixture makes an exclusive lock observable
        # to a reader; this is not installed WAL concurrency/latency evidence.
        self.assertEqual("delete", connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        locker = sqlite3.connect(self.state.database, isolation_level=None)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.assert_capture_error("signature_capture_work_limit", self.capture)
            elapsed = time.monotonic() - started
        finally:
            locker.rollback()
            locker.close()
        self.assertLess(elapsed, 2)
        self.assertEqual(5000, connection.execute("PRAGMA busy_timeout").fetchone()[0])
        self.assertEqual(1, len(self.capture().rows))

    def test_capture_poisoning_overrides_a_domain_field_quota_refusal(self):
        connection = self.ledger._connection
        original = connection._pragma_integer
        reached = []

        def pragma(name, value=None):
            if name == "busy_timeout" and value == 5000:
                reached.append(name)
                raise sqlite3.OperationalError("CANARY-RESTORE")
            return original(name, value)

        with mock.patch.object(connection, "_pragma_integer", side_effect=pragma):
            self.assert_capture_error("signature_capture_unavailable", lambda: self.capture(
                limits=SignatureCaptureLimits(1, 1)))
        self.assertEqual(["busy_timeout"], reached)
        self.assertTrue(connection._sql_unusable)
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(connection, "SELECT 1")

    def test_capture_reports_unavailable_when_deadline_and_actual_restore_step_fail(self):
        connection = self.ledger._connection
        original_harden, original_pragma = self.state.harden_database_files, connection._pragma_integer
        hardened, reached = [], []

        def harden():
            original_harden()
            hardened.append(True)

        def pragma(name, value=None):
            if name == "busy_timeout" and value == 5000:
                reached.append(name)
                raise sqlite3.OperationalError("CANARY-RESTORE")
            return original_pragma(name, value)

        with mock.patch.object(self.state, "harden_database_files", side_effect=harden), \
                mock.patch.object(connection, "_pragma_integer", side_effect=pragma), \
                mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=lambda: 1_000_000_000 if hardened else 0):
            self.assert_capture_error("signature_capture_unavailable", self.capture)
        self.assertEqual([True], hardened)
        self.assertEqual(["busy_timeout"], reached)
        self.assertTrue(connection._sql_unusable)
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(connection, "SELECT 1")

    def test_foreign_progress_handler_is_preserved_before_any_capture_read(self):
        connection = self.ledger._connection
        observed = []
        connection.set_progress_handler(lambda: observed.append(1) or 0, 1)
        try:
            with mock.patch.object(self.ledger, "_owner_generation_locked") as owner:
                self.assert_capture_error("signature_capture_unavailable", self.capture)
            owner.assert_not_called()
            connection.execute("SELECT 1").fetchone()
            self.assertGreater(len(observed), 0)
        finally:
            connection.set_progress_handler(None, 0)
        self.assertEqual(1, len(self.capture().rows))

    def test_missing_or_empty_signer_is_not_recreated_or_implicitly_verified(self):
        path = self.state.allowed_signers_path
        line = path.read_bytes()
        before = self.ledger._connection.total_changes
        try:
            path.unlink()
            captured = self.capture()
            self.assertIsNone(captured.signer)
            self.assertEqual(line, captured.owner[1])
            self.assertFalse(path.exists())
            path.write_bytes(b"")
            path.chmod(0o600)
            self.assertIsNone(self.capture().signer)
            self.assertEqual(b"", path.read_bytes())
        finally:
            path.write_bytes(line)
            path.chmod(0o600)
        self.assertEqual(before, self.ledger._connection.total_changes)

    def test_repeated_capture_observes_new_generation_even_with_identical_key(self):
        first = self.capture()
        # Synthetic history recurrence, not permission to re-enroll a retired key.
        self.ledger._connection.execute(
            "INSERT INTO owner_key_history(sequence,signer_line,reason,recorded_at_ms) VALUES(2,?,'replaced',42)",
            (first.owner[1],),
        )
        second = self.capture()
        self.assertEqual(first.owner[1], second.owner[1])
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(2, second.owner[0])
        self.assertEqual(1, first.owner[0])
        self.assertIsNot(first, second)

    def test_legacy_signer_encoding_is_captured_exactly_without_rewriting(self):
        padded = public_value("ssh-rsa", b"\x00\x01\x00\x01", b"\x00\x01" + b"\x55" * 127)
        line = ("codex-agents-owner " + padded + "\n").encode("ascii")
        self.state.allowed_signers_path.write_bytes(line)
        self.ledger._connection.execute(
            "INSERT INTO owner_key_history(sequence,signer_line,reason,recorded_at_ms) VALUES(2,?,'replaced',42)", (line,))
        before = self.ledger._connection.total_changes
        captured = self.capture()
        self.assertEqual(line, captured.owner[1])
        self.assertEqual(line, captured.signer)
        self.assertEqual(line, self.state.allowed_signers_path.read_bytes())
        self.assertEqual(before, self.ledger._connection.total_changes)

    def test_invalid_requests_and_inactive_project_refuse_before_capture(self):
        with mock.patch.object(self.ledger, "_owner_generation_locked", side_effect=AssertionError("owner access")) as owner:
            for provider in (None, True, "CANARY", {}):
                self.assert_capture_error("signature_capture_invalid_request", lambda: self.capture(provider=provider))
            self.assert_capture_error("signature_capture_invalid_request", lambda: self.views.capture_signature_inputs(
                self.projects[0], "codex", {}, self.work_limits))
            self.assert_capture_error("signature_capture_invalid_request", lambda: self.capture(work_limits={}))
            self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.projects[0],))
            with self.assertRaisesRegex(AccountError, "account_project_not_found"):
                self.capture()
        owner.assert_not_called()

    def test_outer_transaction_refuses_without_body_or_signer_access(self):
        self.ledger._connection.execute("BEGIN")
        try:
            with mock.patch.object(self.ledger, "_current_signer_bytes", side_effect=AssertionError("signer")) as signer:
                self.assert_capture_error("signature_capture_unavailable", self.capture)
            signer.assert_not_called()
        finally:
            self.ledger._connection.rollback()

    def test_maximal_unverified_signature_and_missing_signature_keep_exact_bytes(self):
        signature = b"CANARY-UNVERIFIED".ljust(16_384, b"x")
        self.ledger._connection.execute(
            "UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
            (signature, self.projects[0], self.record["record_id"]),
        )
        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            captured = self.capture()
            self.assertEqual(signature, captured.rows[0][-1])
            self.ledger._connection.execute("DELETE FROM owner_attestations WHERE project_id=?", (self.projects[0],))
            self.assertIsNone(self.capture().rows[0][-1])

    def test_actual_replacement_is_fresh_and_does_not_reverify_old_inputs(self):
        first = self.capture()
        directory = self.base / "new-owner"
        directory.mkdir()
        _, public = generate_test_owner_key(directory)
        self.ledger.replace_owner_key(public, now_ms=42)
        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            second = self.capture()
        self.assertEqual(first.owner[0] + 1, second.owner[0])
        self.assertNotEqual(first.signer, second.signer)
        self.assertEqual(second.owner[1], second.signer)
        self.assertEqual(first.rows, second.rows)

    def test_unconfigured_owner_is_observed_without_enrollment(self):
        state = LearningState(self.base / "unconfigured")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(state)
        self.addCleanup(ledger.close)
        project = ledger.register_project(self.roots[0], now_ms=1)
        views = AccountOwnerViews(ledger)
        state.allowed_signers_path.unlink()
        before = ledger._connection.total_changes
        captured = views.capture_signature_inputs(project, "codex", self.limits, self.work_limits)
        self.assertIsNone(captured.owner)
        self.assertIsNone(captured.signer)
        self.assertEqual((), captured.rows)
        self.assertFalse(state.allowed_signers_path.exists())
        self.assertEqual(before, ledger._connection.total_changes)
        self.assertEqual(0, ledger._connection.execute("SELECT COUNT(*) FROM owner_key_history").fetchone()[0])

    def test_wrong_process_or_thread_never_uses_the_backend(self):
        with mock.patch.object(self.ledger, "_owner_generation_locked", side_effect=AssertionError("owner access")) as owner:
            with mock.patch("codex_agents.account_service.os.getpid", return_value=os.getpid() + 1):
                with self.assertRaisesRegex(AccountError, "account_wrong_owner"):
                    self.capture()
            errors = []

            def attempt():
                try:
                    self.capture()
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        owner.assert_not_called()
        self.assertEqual(1, len(errors))
        self.assertIs(type(errors[0]), AccountError)
        self.assertEqual("account_wrong_owner", errors[0].reason_code)
