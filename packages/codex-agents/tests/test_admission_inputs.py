from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import attest_provider, generate_test_owner_key, provider_fixture
from codex_agents.account_api import ACCOUNT_OPERATIONS, AccountError
from codex_agents.account_service import AccountOwnerViews
from codex_agents.admission_inputs import (
    ADMISSION_CAPTURE_RECORD_TYPES, MAX_ADMISSION_CAPTURE_BYTES, MAX_ADMISSION_CAPTURE_ROWS,
    AdmissionCaptureError, AdmissionCaptureLimits, AdmissionInputPolicy, admission_input_size,
)
from codex_agents.learning_contracts import content_digest
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.providers import claude_rate_limit_records, parse_codex_rate_limits
from codex_agents.sql_read_budget import SQLReadLimits

NOW = 10_000_000_000
RAW_PREFIX = "SELECT r.rowid,r.project_id,r.record_id"
HEADER_PREFIX = "SELECT r.rowid,typeof(r.project_id)"


class AdmissionInputLimitTests(unittest.TestCase):
    def test_limits_and_policy_are_bounded_exact_and_immutable(self):
        for rows, size in ((True, 1), (1, False), (0, 1), (1, 0), (1.0, 1),
                           (MAX_ADMISSION_CAPTURE_ROWS + 1, 1), (1, MAX_ADMISSION_CAPTURE_BYTES + 1)):
            with self.subTest(rows=rows, size=size), self.assertRaises(AdmissionCaptureError):
                AdmissionCaptureLimits(rows, size)
        limits = AdmissionCaptureLimits(MAX_ADMISSION_CAPTURE_ROWS, MAX_ADMISSION_CAPTURE_BYTES)
        with self.assertRaises(FrozenInstanceError):
            limits.max_rows = 1
        for minimum, maximum, cap in ((None, "1.0.0", 1), ("0.1.0", "0.1.0", 1),
                                      ("1.0.0", "0.1.0", 1), ("0.1.0", "1.0.0", True),
                                      ("0.1.0", "1.0.0", 0), ("0.1.0", "1.0.0", 2 ** 63),
                                      ("0.1.0" * 100, "1.0.0", 1)):
            with self.subTest(minimum=minimum, maximum=maximum, cap=cap), self.assertRaises(AdmissionCaptureError):
                AdmissionInputPolicy(minimum, maximum, cap)
        self.assertIsNone(AdmissionInputPolicy("0.1.0", "1.0.0", None).max_admissions_per_window)
        self.assertEqual("admission_capture_invalid_request", str(AdmissionCaptureError("CANARY-private")))


class AdmissionInputOwnerTests(unittest.TestCase):
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
        self.projects = []
        for name in ("project-a", "project-b"):
            root = self.base / name
            root.mkdir()
            self.projects.append(self.ledger.register_project(root, now_ms=1))
        self.record = attest_provider(self.ledger, self.projects[0], self.key, "codex", "a" * 64)
        self.views = AccountOwnerViews(self.ledger)
        self.policy = AdmissionInputPolicy("0.164.0", "0.164.1", 60)
        self.limits = AdmissionCaptureLimits(64, 1_048_576)
        self.work = SQLReadLimits(10_000, 1000, MAX_ADMISSION_CAPTURE_BYTES)

    def capture(self, *, limits=None, work=None, project=None, provider="codex", policy=None):
        with mock.patch.object(self.ledger, "_clock_ms", return_value=NOW):
            return self.views.capture_admission_inputs(
                self.projects[0] if project is None else project, provider, self.policy if policy is None else policy,
                self.limits if limits is None else limits, self.work if work is None else work,
            )

    def denied(self, reason, operation):
        with self.assertRaises(AdmissionCaptureError) as caught:
            operation()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def observe_codex(self, project):
        records = parse_codex_rate_limits(
            json.loads(provider_fixture("codex-rate-limits.json")), digest_key=self.state.digest_key(),
            observed_at_ms=NOW, client=("/usr/bin/fixture-codex", "f" * 64), client_version="0.164.0",
        )
        self.ledger._store_provider_observations(project, records, timestamp=NOW)
        return records

    def test_exact_six_family_raw_capture_without_crypto_connections_or_writes(self):
        local = self.observe_codex(self.projects[0])
        foreign = self.observe_codex(self.projects[1])
        self.assertEqual(local, foreign)
        # Synthetic stored decisions exercise raw run/probe history capture;
        # they do not represent admitted native callers or launched children.
        decision = {"provider": "claude", "decision": "pass", "reason_code": "admitted",
                    "admission_basis": "bootstrap", "account_fingerprint": "b" * 64,
                    "evidence_digests": [content_digest(local[0])], "observed_at_ms": NOW + 1}
        self.ledger._store_admission_locked(self.projects[1], decision, run_id="fixture-run", timestamp=NOW + 1)
        self.ledger._store_probe_admission_locked(self.projects[1], decision, timestamp=NOW + 1)
        # A foreign identity is excluded; identical foreign billing/headroom
        # digests and every source/order field are retained for citation logic.
        expected = [tuple(row) for row in self.ledger._connection.execute(
            "SELECT r.rowid,r.project_id,r.record_id,r.source_sequence,r.record_type,r.content,"
            "r.content_digest,r.source,r.observed_at_ms,r.ingested_at_ms,a.signature "
            "FROM records r LEFT JOIN owner_attestations a ON a.project_id=r.project_id "
            "AND a.subject_record_id=r.record_id WHERE r.record_type!='event' "
            "AND NOT(r.project_id=? AND r.record_type='provider_identity') ORDER BY r.rowid",
            (self.projects[1],),
        )]
        changes = self.ledger._connection.total_changes
        with mock.patch("sqlite3.connect", side_effect=AssertionError("new connection")), \
                mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            captured = self.capture()
        from codex_agents.admission_capture_content import unpack_content
        expanded = [(*row[:5], unpack_content(row[5], row[2], row[8]), *row[6:]) for row in captured.rows]
        self.assertEqual(expected, expanded)
        self.assertLess(sum(len(row[5]) for row in captured.rows), sum(len(row[5]) for row in expected))
        self.assertEqual(ADMISSION_CAPTURE_RECORD_TYPES, {row[4] for row in captured.rows})
        self.assertEqual(NOW, captured.captured_at_ms)
        self.assertEqual(1, captured.project_created_at_ms)
        self.assertIs(captured.origin, self.views._admission_capture_origin)
        self.assertEqual(self.ledger._owner_generation_locked(), captured.owner)
        self.assertEqual(self.state.allowed_signers_path.read_bytes(), captured.signer)
        self.assertIsNot(self.policy, captured.policy)
        object.__setattr__(self.policy, "max_admissions_per_window", 1)
        self.assertEqual(60, captured.policy.max_admissions_per_window)
        self.assertEqual(changes, self.ledger._connection.total_changes)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(captured.retained_bytes, admission_input_size(
            captured.project_id, captured.provider, captured.runtime_digest, captured.policy,
            captured.owner, captured.signer, captured.rows))
        with self.assertRaises(FrozenInstanceError):
            captured.rows = ()
        self.assertEqual({"account.health", "account.project", "account.owner_key", "account.owner_public_key",
                          "account.open_host_run", "account.claim_launch", "account.capacity_status", "account.register_execution", "account.submit_terminal", "account.release_capacity", "account.close_denied_attempt", "account.fence_claim", "account.settle_lost_run", "account.lost_run_closure", "account.run_provenance", "account.open_provenance", "account.observe_provider", "account.record_observation_unavailable", "account.record_rate_limit_event", "account.billing_probe", "account.withdraw_open", "account.provider_state_snapshot",
                          "account.publish_wake"}, ACCOUNT_OPERATIONS)  # E2a; Approved: decision 170

    def test_exact_field_and_row_quotas_refuse_before_raw_fetch(self):
        baseline = self.capture()
        self.assertEqual(1, len(baseline.rows))
        exact = AdmissionCaptureLimits(1, baseline.retained_bytes)
        self.assertEqual(baseline.rows, self.capture(limits=exact).rows)
        original = self.ledger._connection.execute
        raw = []

        def execute(sql, parameters=()):
            if sql.startswith(RAW_PREFIX):
                raw.append(True)
                raise AssertionError("raw values fetched after quota refusal")
            return original(sql, parameters)

        with mock.patch.object(self.ledger._connection, "execute", side_effect=execute):
            self.denied("admission_capture_limit", lambda: self.capture(
                limits=AdmissionCaptureLimits(1, baseline.retained_bytes - 1)))
        self.assertEqual([], raw)
        self.observe_codex(self.projects[1])
        with mock.patch.object(self.ledger._connection, "execute", side_effect=execute):
            self.denied("admission_capture_limit", lambda: self.capture(limits=exact))
        self.assertEqual([], raw)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertIsNotNone(self.capture())

    def test_headers_return_no_variable_value_even_for_non_strict_corruption(self):
        original = self.ledger._connection.execute
        raw = []
        headers = []

        def execute(sql, parameters=()):
            if sql.startswith(RAW_PREFIX):
                raw.append(True)
                raise AssertionError("corrupt raw value fetched")
            cursor = original(sql, parameters)
            if not sql.startswith(HEADER_PREFIX):
                return cursor

            class HeaderView:
                def fetchall(self):
                    result = cursor.fetchall()
                    headers.extend(tuple(row) for row in result)
                    return result

            return HeaderView()

        saved = dict(self.ledger._connection.execute("SELECT * FROM records WHERE record_id=?",
                                                    (self.record["record_id"],)).fetchone())
        for field, value in (("source_sequence", b"CANARY" * 20_000),
                             ("observed_at_ms", "CANARY" * 20_000),
                             ("content", b"CANARY" * 20_000)):
            with self.subTest(field=field):
                self.ledger._connection.execute(f"UPDATE records SET {field}=? WHERE record_id=?",
                                                (value, self.record["record_id"]))
                try:
                    with mock.patch.object(self.ledger._connection, "execute", side_effect=execute):
                        self.denied("admission_capture_incompatible", self.capture)
                finally:
                    self.ledger._connection.execute(f"UPDATE records SET {field}=? WHERE record_id=?",
                                                    (saved[field], self.record["record_id"]))
        self.assertEqual([], raw)
        self.assertTrue(headers)
        self.assertTrue(all(type(value) in (str, int, type(None)) for header in headers for value in header))
        self.assertTrue(all(type(value) is not str or len(value) <= 7 for header in headers for value in header))
        self.assertIsNotNone(self.capture())

    def test_complete_row_and_byte_preflight_precedes_owner_history_values(self):
        self.observe_codex(self.projects[1])
        baseline = self.capture()
        rows = len(baseline.rows)
        for mode, limits in (
            ("bytes", AdmissionCaptureLimits(rows, baseline.retained_bytes - 1)),
            ("rows", AdmissionCaptureLimits(rows - 1, baseline.retained_bytes)),
        ):
            with self.subTest(mode=mode), mock.patch.object(
                self.ledger, "_owner_generation_locked", side_effect=AssertionError("owner value before full preflight")
            ):
                self.denied("admission_capture_limit", lambda: self.capture(limits=limits))
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertIsNotNone(self.capture())

    def test_digest_corruption_returns_fixed_error_and_releases_the_read_snapshot(self):
        self.ledger._connection.execute("UPDATE records SET content_digest=? WHERE record_id=?",
                                        ("0" * 64, self.record["record_id"]))
        self.denied("admission_capture_incompatible", self.capture)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertFalse(self.ledger._connection._sql_unusable)

    def test_real_second_writer_commit_cannot_mix_headers_and_values(self):
        baseline = self.capture()
        original = self.ledger._connection.execute
        committed = []

        def execute(sql, parameters=()):
            cursor = original(sql, parameters)
            if not sql.startswith(HEADER_PREFIX):
                return cursor

            class HeaderView:
                def fetchall(inner_self):
                    result = cursor.fetchall()
                    writer = sqlite3.connect(self.state.database, isolation_level=None, timeout=.1)
                    try:
                        self.assertEqual("wal", writer.execute("PRAGMA journal_mode").fetchone()[0])
                        writer.execute("UPDATE owner_attestations SET signature=? WHERE project_id=?",
                                       (b"synthetic-new-signature", self.projects[0]))
                        committed.append(True)
                    finally:
                        writer.close()
                    return result

            return HeaderView()

        with mock.patch.object(self.ledger._connection, "execute", side_effect=execute):
            captured = self.capture()
        self.assertEqual([True], committed)
        self.assertEqual(baseline.rows, captured.rows)
        newer = self.capture()
        self.assertNotEqual(captured.rows[0][-1], newer.rows[0][-1])
        self.assertEqual(b"synthetic-new-signature", newer.rows[0][-1])

    def test_outer_transaction_and_wrong_thread_fail_before_filesystem_work(self):
        self.ledger._connection.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
                self.denied("admission_capture_unavailable", self.capture)
            self.assertTrue(self.ledger._connection.in_transaction)
        finally:
            self.ledger._connection.rollback()
        received = []

        def other_thread():
            try:
                self.capture()
            except AccountError as error:
                received.append(error.reason_code)

        with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["account_wrong_owner"], received)

    def test_domain_faults_and_internal_baseexceptions_roll_back_without_positive_capture(self):
        connection = self.ledger._connection
        settings = (connection._pragma_integer("query_only"), connection._pragma_integer("busy_timeout"),
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        for failure in (MemoryError("synthetic"), OSError("synthetic"), KeyboardInterrupt("synthetic"), SystemExit("synthetic")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(self.views, "_capture_admission_rows_locked", side_effect=failure):
                    with self.assertRaises(BaseException) as caught:
                        self.capture()
                self.assertIs(failure, caught.exception)
                self.assertFalse(connection.in_transaction)
                self.assertIsNone(connection._sql_scope)
                self.assertFalse(connection._sql_unusable)
                self.assertEqual(settings, (connection._pragma_integer("query_only"), connection._pragma_integer("busy_timeout"),
                                            connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)))
        self.assertIsNotNone(self.capture())

    def test_deadline_failure_is_fixed_and_does_not_leave_an_outer_transaction(self):
        from codex_agents import sql_read_budget
        with mock.patch.object(sql_read_budget, "_clock_ns", side_effect=[100, 100, 1_000_000_100]):
            self.denied("admission_capture_work_limit", self.capture)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertIsNotNone(self.capture())

    def test_rollback_fault_poisons_and_refuses_reuse_without_database_work(self):
        connection = self.ledger._connection
        with mock.patch.object(connection, "rollback", side_effect=sqlite3.OperationalError("CANARY cleanup")):
            self.denied("admission_capture_unavailable", self.capture)
        self.assertTrue(connection._sql_unusable)
        with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
            self.denied("admission_capture_unavailable", self.capture)

    def test_capture_does_not_read_unrelated_raw_result_content(self):
        row = dict(self.ledger._connection.execute("SELECT * FROM records WHERE record_id=?",
                                                  (self.record["record_id"],)).fetchone())
        row.update(record_id="fixture-unrelated", source_sequence=99, record_type="event",
                   content=b"CANARY-private-rejected-content" * 100_000)
        self.ledger._connection.execute("INSERT INTO records(" + ",".join(row) + ") VALUES(" +
                                       ",".join("?" for _ in row) + ")", tuple(row.values()))
        captured = self.capture()
        self.assertEqual([self.record["record_id"]], [value[2] for value in captured.rows])

    def test_empty_unconfigured_capture_does_not_enroll_or_create_a_signer(self):
        state = LearningState(self.base / "empty-state")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(state)
        try:
            root = self.base / "empty-project"
            root.mkdir()
            project = ledger.register_project(root, now_ms=1)
            for missing in (False, True):
                if missing:
                    state.allowed_signers_path.unlink()
                before = ledger._connection.total_changes
                captured = AccountOwnerViews(ledger).capture_admission_inputs(project, "codex", self.policy, self.limits, self.work)
                self.assertIsNone(captured.owner)
                self.assertIsNone(captured.signer)
                self.assertEqual((), captured.rows)
                self.assertEqual(before, ledger._connection.total_changes)
                self.assertEqual(not missing, state.allowed_signers_path.exists())
                if not missing:
                    self.assertEqual(b"", state.allowed_signers_path.read_bytes())
        finally:
            ledger.close()

    def test_candidates_include_other_provider_future_restrictions_before_filtering(self):
        foreign = claude_rate_limit_records({
            "status": "rejected", "unifiedWindows": {}, "isUsingOverage": False,
            "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
        }, account_fingerprint="b" * 64, observed_at_ms=NOW + 1)
        self.ledger._store_provider_observations(self.projects[1], foreign, timestamp=NOW + 1)
        foreign_attestation = attest_provider(self.ledger, self.projects[1], self.key, "claude", "b" * 64)
        captured = self.capture()
        by_id = {row[2]: row for row in captured.rows}
        self.assertEqual({self.record["record_id"], *(item["record_id"] for item in foreign)}, set(by_id))
        self.assertNotIn(foreign_attestation["record_id"], by_id)
        self.assertEqual(NOW + 1, by_id[foreign[0]["record_id"]][8])
        self.denied("admission_capture_limit", lambda: self.capture(limits=AdmissionCaptureLimits(2, 1_048_576)))

    def test_owner_header_checks_precede_owner_blob_fetch(self):
        connection = self.ledger._connection
        saved = tuple(connection.execute("SELECT signer_line,recorded_at_ms FROM owner_key_history").fetchone())
        for field, value in (("signer_line", b"CANARY" * 20_000), ("recorded_at_ms", b"CANARY" * 20_000)):
            with self.subTest(field=field):
                # Preserve append-only triggers. A new synthetic generation is
                # deliberately corrupt; no production owner key is replaced.
                connection.execute("INSERT INTO owner_key_history(signer_line,reason,recorded_at_ms) VALUES(?,'replaced',?)",
                                   (value if field == "signer_line" else saved[0],
                                    value if field == "recorded_at_ms" else saved[1]))
                with mock.patch.object(self.ledger, "_owner_generation_locked", side_effect=AssertionError("owner blob")):
                    self.denied("admission_capture_incompatible", self.capture)

    def test_invalid_input_and_clock_cannot_create_a_capture(self):
        with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
            for options in ({"project": "CANARY"}, {"provider": "unknown"}, {"policy": {}}, {"limits": {}}):
                with self.subTest(options=options):
                    self.denied("admission_capture_invalid_request", lambda: self.capture(**options))
        for timestamp in (True, -1, None, 2 ** 63):
            with self.subTest(timestamp=timestamp), mock.patch.object(self.ledger, "_clock_ms", return_value=timestamp):
                self.denied("admission_capture_unavailable", lambda: self.views.capture_admission_inputs(
                    self.projects[0], "codex", self.policy, self.limits, self.work))
        self.assertFalse(self.ledger._connection.in_transaction)

    def test_foreign_progress_handler_is_preserved_on_refused_capture(self):
        connection = self.ledger._connection
        seen = []
        connection.set_progress_handler(lambda: seen.append(True) or 0, 1)
        try:
            with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
                self.denied("admission_capture_unavailable", self.capture)
            self.assertIsNone(connection._sql_scope)
            self.assertFalse(connection._sql_unusable)
            connection.execute("SELECT count(*) FROM records").fetchone()
            self.assertTrue(seen)
        finally:
            connection.set_progress_handler(None, 0)

    def test_sql_work_limit_refuses_without_fetching_raw_values(self):
        connection = self.ledger._connection
        original_row = dict(connection.execute("SELECT * FROM records WHERE record_id=?",
                                               (self.record["record_id"],)).fetchone())
        # Raw capture does not decode JSON. These repeated canonical bytes are
        # synthetic scan-work fixtures, not verified admission evidence.
        rows = [{**original_row, "record_id": "fixture-scan-" + str(i), "source_sequence": i,
                 "record_type": "provider_headroom", "source": "provider"} for i in range(100, 2100)]
        connection.executemany("INSERT INTO records(" + ",".join(original_row) + ") VALUES(" +
                               ",".join("?" for _ in original_row) + ")", [tuple(row.values()) for row in rows])
        original = connection.execute

        def execute(sql, parameters=()):
            if sql.startswith(RAW_PREFIX):
                raise AssertionError("raw values fetched after work refusal")
            return original(sql, parameters)

        with mock.patch.object(connection, "execute", side_effect=execute):
            self.denied("admission_capture_work_limit", lambda: self.capture(
                limits=AdmissionCaptureLimits(4096, MAX_ADMISSION_CAPTURE_BYTES),
                work=SQLReadLimits(1, 1000, MAX_ADMISSION_CAPTURE_BYTES)))
        self.assertFalse(connection.in_transaction)

    def test_restoration_fault_discards_capture_and_poison_prevents_reuse(self):
        connection = self.ledger._connection
        with mock.patch.object(connection, "_restore_read_settings", side_effect=OSError("CANARY restore")):
            self.denied("admission_capture_unavailable", self.capture)
        self.assertTrue(connection._sql_unusable)
        with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
            self.denied("admission_capture_unavailable", self.capture)

    def test_capture_uses_no_json_parsing_in_sql_or_python(self):
        connection = self.ledger._connection
        functions = []

        def authorizer(action, first, second, *rest):
            if action == sqlite3.SQLITE_FUNCTION:
                name = second or first or ""
                functions.append(name)
                if name.startswith("json"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            with mock.patch("json.loads", side_effect=AssertionError("JSON before decoder")):
                captured = self.capture()
            self.assertEqual(self.record["record_id"], captured.rows[0][2])
            self.assertTrue(functions)
            self.assertFalse(any(name.startswith("json") for name in functions))
        finally:
            connection.set_authorizer(None)

    def test_closed_owner_refuses_before_filesystem_work(self):
        self.ledger.close()
        with mock.patch.object(self.state, "harden_database_files", side_effect=AssertionError("filesystem")):
            self.denied("admission_capture_unavailable", self.capture)

    def test_project_metadata_is_preflighted_without_legacy_unbounded_reads(self):
        with mock.patch.object(self.views, "_project_locked", side_effect=AssertionError("legacy project fetch")), \
                mock.patch.object(self.ledger, "_account_records_locked", side_effect=AssertionError("legacy fetchall")), \
                mock.patch.object(self.ledger, "_provider_records_locked", side_effect=AssertionError("legacy JSON")), \
                mock.patch.object(self.ledger, "_admission_settings", side_effect=AssertionError("policy file")):
            self.assertEqual(1, self.capture().project_created_at_ms)
            self.ledger._connection.execute("UPDATE projects SET created_at_ms=? WHERE project_id=?",
                                            (b"CANARY" * 20_000, self.projects[0]))
            self.denied("admission_capture_incompatible", self.capture)

    def test_ambiguous_begin_failure_cannot_leave_a_reusable_open_transaction(self):
        connection = self.ledger._connection
        original = connection.execute
        failure = MemoryError("synthetic after BEGIN")

        def execute(sql, parameters=()):
            result = original(sql, parameters)
            if sql == "BEGIN":
                raise failure
            return result

        with mock.patch.object(connection, "execute", side_effect=execute):
            with self.assertRaises(MemoryError) as caught:
                self.capture()
        self.assertIs(failure, caught.exception)
        self.assertTrue(connection._sql_unusable)
        self.assertIsNone(connection._sql_scope)
        self.denied("admission_capture_unavailable", self.capture)

    def test_success_restores_scope_settings_and_closes_all_cursors(self):
        connection = self.ledger._connection
        settings = (connection._pragma_integer("query_only"), connection._pragma_integer("busy_timeout"),
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        original = connection.execute
        views = []

        def execute(sql, parameters=()):
            result = original(sql, parameters)
            views.append(result)
            return result

        with mock.patch.object(connection, "execute", side_effect=execute):
            self.capture()
        self.assertIsNone(connection._sql_scope)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(settings, (connection._pragma_integer("query_only"), connection._pragma_integer("busy_timeout"),
                                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)))
        from codex_agents.sql_read_budget import SQLReadError
        self.assertTrue(views)
        for view in views:
            with self.assertRaises(SQLReadError):
                view.fetchone()

    def test_missing_and_inactive_projects_keep_the_account_error_contract(self):
        missing = "prj_" + "0" * 32
        self.assertNotIn(missing, self.projects)
        connection = self.ledger._connection
        for project in (missing, self.projects[0]):
            with self.subTest(project=project):
                if project == self.projects[0]:
                    connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (project,))
                before = connection.total_changes
                with self.assertRaises(AccountError) as caught:
                    self.capture(project=project)
                self.assertEqual("account_project_not_found", caught.exception.reason_code)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(before, connection.total_changes)
                self.assertFalse(connection.in_transaction)
                self.assertIsNone(connection._sql_scope)
                self.assertFalse(connection._sql_unusable)

    def test_malformed_signature_values_are_refused_before_any_owner_or_record_value(self):
        connection = self.ledger._connection
        original = connection.execute
        saved = connection.execute("SELECT signature FROM owner_attestations WHERE project_id=?",
                                    (self.projects[0],)).fetchone()[0]

        def execute(sql, parameters=()):
            if sql.startswith(RAW_PREFIX):
                raise AssertionError("malformed signature fetched")
            return original(sql, parameters)

        for signature in ("CANARY" * 20_000, b"CANARY" * 3000):
            with self.subTest(kind=type(signature).__name__):
                connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=?",
                                   (signature, self.projects[0]))
                try:
                    with mock.patch.object(connection, "execute", side_effect=execute), mock.patch.object(
                        self.ledger, "_owner_generation_locked", side_effect=AssertionError("owner before signature preflight")
                    ):
                        self.denied("admission_capture_incompatible", self.capture)
                finally:
                    connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=?",
                                       (saved, self.projects[0]))
        self.assertEqual(saved, self.capture().rows[0][-1])
