from __future__ import annotations

import os
import sqlite3
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest import mock

import tests.test_signature_inputs as capture_fixture

from codex_agents.account_api import ACCOUNT_OPERATIONS, AccountError
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import StateError
from codex_agents.signature_inputs import SignatureCaptureError, SignatureInputSnapshot, signature_input_size
from codex_agents.signature_job_intake import (
    MAX_SIGNATURE_JOB_BYTES, MAX_SIGNATURE_JOB_ROWS, SignatureIntakeLimits, SignatureJobError,
    SignatureJobIntake,
)


class SignatureIntakeLimitTests(unittest.TestCase):
    def test_exact_integer_limits_and_aggregate_boundaries(self):
        valid = (2, 2, 2048, MAX_SIGNATURE_JOB_BYTES // 4, 2)
        limits = SignatureIntakeLimits(*valid)
        self.assertEqual(MAX_SIGNATURE_JOB_ROWS, limits.max_projects * limits.max_jobs_per_project * limits.rows_per_job)
        self.assertEqual(MAX_SIGNATURE_JOB_BYTES, limits.max_projects * limits.max_jobs_per_project * limits.bytes_per_job)
        with self.assertRaises(FrozenInstanceError):
            limits.max_selected = 1
        for values in ((True, *valid[1:]), (2, False, *valid[2:]), (2, 2, 0, valid[3], 2),
                       (2, 2, 2049, valid[3], 2), (2, 2, 2048, valid[3] + 1, 2),
                       (2, 2, 2048, valid[3], 3), (257, 1, 1, 1, 1), (256, 17, 1, 1, 1),
                       (2, 2, 1.0, 1, 1), (2, 2, 1, "CANARY", 1), (2, 2, 1, 1, True)):
            with self.subTest(values=values), self.assertRaises(SignatureJobError) as caught:
                SignatureIntakeLimits(*values)
            self.assertEqual("signature_job_invalid_limits", caught.exception.reason_code)
        self.assertEqual(4096, SignatureIntakeLimits(256, 16, 1, 1, 256).max_projects * 16)
        self.assertEqual("signature_job_invalid_request", str(SignatureJobError("CANARY-PRIVATE")))


class SignatureJobIntakeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups(), "owned fixture cleanup failed"))
        self.views, self.ledger = self.fixture.views, self.fixture.ledger
        self.a, self.b = self.fixture.projects
        self.limits = SignatureIntakeLimits(2, 2, 8, 65_536, 2)
        self.intake = SignatureJobIntake(self.views, self.limits)

    def capture(self, project=None, provider="codex", intake=None):
        return (self.intake if intake is None else intake).capture(
            self.a if project is None else project, provider, self.fixture.work_limits)

    def another_owner_intake(self, limits):
        fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "owned fixture cleanup failed"))
        return SignatureJobIntake(fixture.views, limits)

    def assert_job_error(self, reason, operation):
        with self.assertRaises(SignatureJobError) as caught:
            operation()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_capacity_is_reserved_before_capture_without_extra_database_secret_or_crypto(self):
        observed = []
        original = self.views.capture_signature_inputs
        before = self.ledger._connection.total_changes

        def capture(*args):
            observed.append(self.intake.snapshot())
            return original(*args)

        with mock.patch.object(self.views, "capture_signature_inputs", side_effect=capture), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("second connection")), \
                mock.patch.object(self.fixture.state, "digest_key", side_effect=AssertionError("secret key")), \
                mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto")):
            handle = self.capture()
        self.assertIs(type(handle), object)
        self.assertEqual(1, len(observed))
        self.assertEqual((1, 1, 0, 8, 65_536), (observed[0].jobs, observed[0].reserved, observed[0].queued,
                                                  observed[0].reserved_rows, observed[0].reserved_bytes))
        status = self.intake.snapshot()
        self.assertEqual((1, 0, 1, 8, 65_536), (status.jobs, status.reserved, status.queued,
                                              status.reserved_rows, status.reserved_bytes))
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual({"account.health", "account.project", "account.owner_key", "account.owner_public_key"},
                         ACCOUNT_OPERATIONS)
        self.assertNotIn("CANARY", repr(status))

    def test_project_overload_never_reads_and_other_projects_keep_their_share(self):
        self.capture()
        self.capture()
        with mock.patch.object(self.views, "capture_signature_inputs", side_effect=AssertionError("capture read")) as read:
            self.assert_job_error("signature_job_project_full", self.capture)
        read.assert_not_called()
        self.capture(self.b)
        self.capture(self.b)
        status = self.intake.snapshot()
        self.assertEqual((2, 4, 32, 262_144), (status.projects, status.jobs, status.reserved_rows, status.reserved_bytes))

    def test_project_entry_capacity_and_reuse_are_bounded(self):
        a, b = self.capture(), self.capture(self.b)
        root = self.fixture.base / "project-c"
        root.mkdir()
        c = self.ledger.register_project(root, now_ms=3)
        with mock.patch.object(self.views, "capture_signature_inputs", side_effect=AssertionError("capture read")) as read:
            self.assert_job_error("signature_job_project_limit", lambda: self.capture(c))
        read.assert_not_called()
        self.assertEqual("released", self.intake.cancel(a))
        self.capture(c)
        self.assertEqual(2, self.intake.snapshot().projects)
        self.assertEqual("released", self.intake.cancel(b))

    def test_capture_failure_and_interruption_release_the_unstarted_reservation(self):
        for error in (SignatureCaptureError("signature_capture_work_limit"), KeyboardInterrupt(), AssertionError("CANARY")):
            with self.subTest(kind=type(error).__name__), mock.patch.object(
                    self.views, "capture_signature_inputs", side_effect=error):
                with self.assertRaises(type(error)):
                    self.capture()
            status = self.intake.snapshot()
            self.assertEqual((0, 0, 0, 0), (status.projects, status.jobs, status.reserved_rows, status.reserved_bytes))
        self.capture()

    def test_failure_after_publication_removes_pending_index_and_capacity(self):
        original = self.intake._publish

        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt()

        with mock.patch.object(self.intake, "_publish", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.capture()
        self.assertEqual(0, self.intake.snapshot().jobs)
        self.assertEqual(0, self.intake.snapshot().projects)
        self.assertIsNone(self.intake.select_next())
        handle = self.capture(self.b)
        self.assertIs(handle, self.intake.select_next().handle)

    def test_trusted_internal_faults_propagate_after_capture_and_intake_cleanup(self):
        connection = self.ledger._connection

        def settings():
            return (connection.execute("PRAGMA query_only").fetchone()[0],
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), connection.in_transaction)

        baseline = settings()
        for boundary in ("capture_body", "intake_size"):
            for error_type in (AssertionError, MemoryError, RuntimeError, TypeError, ValueError, AttributeError):
                with self.subTest(boundary=boundary, error=error_type.__name__):
                    error = error_type("CANARY-TRUSTED-FAULT")
                    patch = (mock.patch.object(self.ledger, "_attestation_rows_locked", side_effect=error)
                             if boundary == "capture_body" else mock.patch(
                                 "codex_agents.signature_job_intake.signature_input_size", side_effect=error))
                    with patch, self.assertRaises(error_type) as caught:
                        self.capture()
                    self.assertIs(error, caught.exception)
                    self.assertEqual((0, 0), (self.intake.snapshot().jobs, self.intake.snapshot().reserved_bytes))
                    self.assertIsNone(self.intake.select_next())
                    self.assertEqual(baseline, settings())
                    self.assertIsNot(True, getattr(connection, "_sql_unusable", False))
        self.intake.cancel(self.capture())

    def test_expected_storage_errors_remain_fixed_after_actual_capture_cleanup(self):
        for error in (StateError("CANARY-PATH"), OSError("CANARY-PATH")):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                    self.fixture.state, "harden_database_files", side_effect=error):
                with self.assertRaises(SignatureCaptureError) as caught:
                    self.capture()
            self.assertEqual("signature_capture_unavailable", caught.exception.reason_code)
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
            self.assertEqual((0, 0), (self.intake.snapshot().jobs, self.intake.snapshot().reserved_bytes))
            self.assertIsNone(self.intake.select_next())
        self.intake.cancel(self.capture())

    def test_snapshot_origin_fields_and_actual_byte_count_cannot_be_forged_by_a_capture_bug(self):
        inputs = self.fixture.capture()
        row = inputs.rows[0]
        variants = (object(), replace(inputs, origin=object()), replace(inputs, project_id=self.b),
                    replace(inputs, provider="claude"), replace(inputs, retained_bytes=inputs.retained_bytes - 1),
                    replace(inputs, retained_bytes=True), replace(inputs, rows=list(inputs.rows)),
                    replace(inputs, rows=(row,) * 9), replace(inputs, owner=(1, b"x", "CANARY", 1)),
                    replace(inputs, rows=((*row[:4], "\ud800", row[5]),)))
        for value in variants:
            with self.subTest(kind=type(value).__name__), mock.patch.object(
                    self.views, "capture_signature_inputs", return_value=value):
                self.assert_job_error("signature_job_capture_incompatible", self.capture)
            self.assertEqual(0, self.intake.snapshot().jobs)
            self.assertIsNone(self.intake.select_next())

    def test_round_robin_skips_busy_project_without_reordering_its_fifo(self):
        a1, a2 = self.capture(), self.capture(provider="claude")
        first = self.intake.select_next()
        self.assertIs(a1, first.handle)
        self.assertEqual("codex", first.provider)
        del first
        self.assertIsNone(self.intake.select_next())
        b1, b2 = self.capture(self.b), self.capture(self.b, "claude")
        second = self.intake.select_next()
        self.assertIs(b1, second.handle)
        del second
        self.assertIsNone(self.intake.select_next())
        self.assertFalse(self.intake.confirm_worker_stopped(a1))
        next_a = self.intake.select_next()
        self.assertIs(a2, next_a.handle)
        self.assertEqual("claude", next_a.provider)
        del next_a
        self.assertFalse(self.intake.confirm_worker_stopped(b1))
        next_b = self.intake.select_next()
        self.assertIs(b2, next_b.handle)
        del next_b
        self.assertEqual((2, 2, 4), (self.intake.snapshot().selected, self.intake.snapshot().returned,
                                    self.intake.snapshot().jobs))

    def test_selected_cancellation_retains_every_reservation_until_owner_confirmation(self):
        first, pending = self.capture(), self.capture()
        item = self.intake.select_next()
        self.assertIs(first, item.handle)
        self.assertFalse(hasattr(item, "verified"))
        self.assertNotIn(item.inputs.rows[0][0], repr(item))
        del item
        self.assertEqual("pending_confirmation", self.intake.cancel(first))
        self.assertEqual("pending_confirmation", self.intake.cancel(first))
        status = self.intake.snapshot()
        self.assertEqual((2, 1, 1, 16, 131_072), (status.jobs, status.selected, status.cancel_requested,
                                                 status.reserved_rows, status.reserved_bytes))
        self.assert_job_error("signature_job_project_full", self.capture)
        self.assert_job_error("signature_job_invalid_transition", lambda: self.intake.discard_returned(first))
        self.assertTrue(self.intake.confirm_worker_stopped(first))
        self.assertEqual((1, 0, 0), (self.intake.snapshot().jobs, self.intake.snapshot().selected,
                                    self.intake.snapshot().cancel_requested))
        self.capture()
        self.assertIs(pending, self.intake.select_next().handle)

    def test_returned_inputs_keep_capacity_until_final_owner_consumption(self):
        handle = self.capture()
        self.assert_job_error("signature_job_invalid_transition", lambda: self.intake.returned_inputs(handle))
        selected = self.intake.select_next()
        self.assertIs(handle, selected.handle)
        del selected
        self.assertFalse(self.intake.confirm_worker_stopped(handle))
        status = self.intake.snapshot()
        self.assertEqual((1, 0, 1, 8, 65_536), (status.jobs, status.selected, status.returned,
                                               status.reserved_rows, status.reserved_bytes))
        inputs = self.intake.returned_inputs(handle)
        self.assertEqual((self.a, "codex"), (inputs.project_id, inputs.provider))
        self.assertFalse(hasattr(inputs, "verified"))
        del inputs
        self.intake.discard_returned(handle)
        self.assertEqual(0, self.intake.snapshot().jobs)
        self.assert_job_error("signature_job_invalid_handle", lambda: self.intake.discard_returned(handle))
        self.assert_job_error("signature_job_invalid_handle", lambda: self.intake.returned_inputs(handle))

    def test_owner_confirmed_no_start_is_separate_from_selection_and_cancel(self):
        handle = self.capture()
        self.assert_job_error("signature_job_invalid_transition", lambda: self.intake.confirm_no_worker_started(handle))
        item = self.intake.select_next()
        self.assertIs(handle, item.handle)
        del item
        self.assertEqual("pending_confirmation", self.intake.cancel(handle))
        self.intake.confirm_no_worker_started(handle)
        self.assertEqual(0, self.intake.snapshot().jobs)
        self.assertIsNone(self.intake.select_next())

    def test_foreign_forged_and_duplicate_handles_cannot_release_another_job(self):
        handle = self.capture()
        other = self.another_owner_intake(self.limits)

        class HashTrap:
            def __hash__(self):
                raise AssertionError("caller callback")

        for value in (object(), None, True, [], {}, "CANARY", HashTrap()):
            with self.subTest(kind=type(value).__name__):
                self.assert_job_error("signature_job_invalid_handle", lambda: self.intake.cancel(value))
        self.assert_job_error("signature_job_invalid_handle", lambda: other.cancel(handle))
        self.assertEqual(1, self.intake.snapshot().jobs)
        self.assertEqual("released", self.intake.cancel(handle))
        self.assert_job_error("signature_job_invalid_handle", lambda: self.intake.cancel(handle))
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_limits_are_copied_and_invalid_requests_never_capture(self):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("constructor connection")):
            self.assert_job_error("signature_job_unavailable", lambda: SignatureJobIntake(self.views, self.limits))
            self.assert_job_error("signature_job_invalid_request", lambda: SignatureJobIntake({}, self.limits))
            self.assert_job_error("signature_job_invalid_limits", lambda: SignatureJobIntake(self.views, {}))
        object.__setattr__(self.limits, "max_jobs_per_project", 4096)
        self.capture()
        self.capture()
        with mock.patch.object(self.views, "capture_signature_inputs", side_effect=AssertionError("capture")) as read:
            self.assert_job_error("signature_job_project_full", self.capture)
            for project, provider, work in (("CANARY", "codex", self.fixture.work_limits),
                                             (self.a, "CANARY", self.fixture.work_limits), (self.a, "codex", {})):
                self.assert_job_error("signature_job_invalid_request", lambda: self.intake.capture(project, provider, work))
        read.assert_not_called()

    def test_metadata_slot_ceiling_and_project_churn_with_controlled_owner_capture(self):
        # A controlled owner capture isolates metadata limits. These synthetic
        # project labels are not actual registrations or native authorizations.
        intake = self.another_owner_intake(SignatureIntakeLimits(256, 16, 1, 128, 256))
        handles = []

        def captured(project_id, provider, limits, work_limits):
            return SignatureInputSnapshot(
                intake._views._ledger._attestation_origin, project_id, provider, None, None, (),
                signature_input_size(project_id, provider, None, None, ()),
            )

        with mock.patch.object(intake._views, "capture_signature_inputs", side_effect=captured):
            for project in range(256):
                for _ in range(16):
                    handles.append(self.capture("prj_" + f"{project:032x}", intake=intake))
        status = intake.snapshot()
        self.assertEqual((256, 4096, 4096, 524_288),
                         (status.projects, status.jobs, status.reserved_rows, status.reserved_bytes))
        with mock.patch.object(intake._views, "capture_signature_inputs", side_effect=AssertionError("overload read")) as read:
            self.assert_job_error("signature_job_project_full", lambda: self.capture("prj_" + "0" * 32, intake=intake))
            self.assert_job_error("signature_job_project_limit", lambda: self.capture("prj_" + "f" * 32, intake=intake))
        read.assert_not_called()
        for handle in handles:
            self.assertEqual("released", intake.cancel(handle))
        self.assertEqual((0, 0, 0), (intake.snapshot().projects, intake.snapshot().jobs, intake.snapshot().reserved_bytes))
        self.assertIsNone(intake.select_next())

    def test_one_intake_is_bound_to_the_actual_ledger_lifetime(self):
        self.assert_job_error("signature_job_unavailable", lambda: SignatureJobIntake(self.views, self.limits))
        another_view = AccountOwnerViews(self.ledger)
        self.assert_job_error("signature_job_unavailable", lambda: SignatureJobIntake(another_view, self.limits))
        self.assertIs(self.intake, self.ledger._signature_job_intake)
        self.assertEqual(0, self.intake.snapshot().jobs)
        self.capture()
        self.assertEqual(1, self.intake.snapshot().jobs)

    def test_poisoned_capture_releases_only_the_new_unstarted_reservation(self):
        handle = self.capture()
        connection = self.ledger._connection
        original = connection._pragma_integer

        def pragma(name, value=None):
            if name == "busy_timeout" and value == 5000:
                raise RuntimeError("CANARY-RESTORE")
            return original(name, value)

        with mock.patch.object(connection, "_pragma_integer", side_effect=pragma):
            with self.assertRaises(SignatureCaptureError) as caught:
                self.capture(self.b)
        self.assertEqual("signature_capture_unavailable", caught.exception.reason_code)
        self.assertEqual(1, self.intake.snapshot().jobs)
        self.assertTrue(connection._sql_unusable)
        before = self.intake.snapshot()
        with mock.patch.object(self.views, "_capture_signature_inputs_locked",
                               side_effect=AssertionError("poisoned backend work")) as capture:
            for caller, operation in (("intake", lambda: self.capture(self.b)), ("owner", self.fixture.capture)):
                with self.subTest(caller=caller):
                    with self.assertRaises(SignatureCaptureError) as refused:
                        operation()
                    self.assertEqual("signature_capture_unavailable", refused.exception.reason_code)
                    self.assertIsNone(refused.exception.__cause__)
                    self.assertIsNone(refused.exception.__context__)
                    self.assertEqual(before, self.intake.snapshot())
        capture.assert_not_called()
        self.assert_job_error("signature_job_unavailable", self.intake.select_next)
        self.assertEqual("released", self.intake.cancel(handle))
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_wrong_process_and_thread_refuse_before_metadata_or_backend_access(self):
        handle = self.capture()
        operations = (self.intake.snapshot, self.intake.select_next, self.capture,
                      lambda: self.intake.cancel(handle), lambda: self.intake.confirm_worker_stopped(handle),
                      lambda: self.intake.confirm_no_worker_started(handle), lambda: self.intake.discard_returned(handle),
                      lambda: self.intake.returned_inputs(handle))
        with mock.patch("codex_agents.account_service.os.getpid", return_value=os.getpid() + 1):
            for operation in operations:
                with self.assertRaisesRegex(AccountError, "account_wrong_owner"):
                    operation()
        failures = []

        def foreign():
            for operation in operations:
                try:
                    operation()
                except Exception as error:
                    failures.append(error)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(operations), len(failures))
        self.assertTrue(all(type(error) is AccountError and error.reason_code == "account_wrong_owner" for error in failures))
        self.assertEqual(1, self.intake.snapshot().jobs)
