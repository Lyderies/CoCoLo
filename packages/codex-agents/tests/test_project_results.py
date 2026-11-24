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
from tests.learning_support import assignment, result
from tests.test_project_history import PROJECT_A, PROJECT_B, binding, terminal

from codex_agents import project_history
from codex_agents.bridge import _fenced_result
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity


KEY = b"r" * 32  # Disposable synthetic digest key, never a real owner credential.


class ProjectResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "history"
        self.directory.mkdir(mode=0o700)
        self.now = 1000
        self.capacity = ResultCapacity(16, 2 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.store = self.open()
        self.addCleanup(lambda: self.store.close())

    def open(self, **changes):
        # Isolated legacy result fixture; managed execution is tested separately.
        options = {"result_key": KEY, "result_capacity": self.capacity, "result_clock_ms": lambda: self.now,
                   "allow_legacy_orchestration_reference": True}
        options.update(changes)
        return ProjectHistory(self.directory, PROJECT_A, **options)

    def reserve(self, number=1, **changes):
        return self.store.reserve_result(binding(number), assignment(), hard_deadline_at_ms=changes.get("deadline", 2000))

    def payload(self, number=1, *, kind="legacy", marker="PRIVATE-RESULT-CANARY"):
        value = result()
        value["summary"] = marker
        if kind == "negative":
            value.update(execution_state="completed", semantic_verdict="not-ready", admission_state="unverified",
                         retry={"attempt": 1, "retry_of_assignment_id": None})
            value["evidence"][0]["outcome"] = "fail"
        elif kind == "fenced":
            value = _fenced_result(assignment(), "timed_out", "timed_out")
        body = canonical_json(value)
        record = json.loads(terminal(number))
        record.update(result_digest=keyed_digest(KEY, body), fenced=kind == "fenced", reviewed=True,
                      terminal_state="timed_out" if kind == "fenced" else None,
                      result_validation_reason_code=None)
        return body, canonical_json(record)

    def commit(self, number=1, **options):
        body, content = self.payload(number, **options)
        return self.store.commit_result(content, binding(number), assignment(), body)

    def files(self):
        return sorted(self.directory.glob(".result-*"))

    def test_feature_is_closed_without_explicit_capacity_and_private_digest_key(self):
        self.store.close()
        self.store = ProjectHistory(self.directory, PROJECT_A)
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.reserve()
        self.assertEqual([], self.files())
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])

    def test_reservation_is_durable_exact_and_does_not_refresh_or_grant_a_launch(self):
        first = self.reserve()
        self.assertEqual((1000, 2000, 61_000, 61_000, "reserved"),
                         (first.created_at_ms, first.hard_deadline_at_ms, first.reserved_until_ms,
                          first.expires_at_ms, first.state))
        self.now = 1500
        self.assertEqual(first, self.reserve())
        self.store.close()
        self.store = self.open()
        self.assertEqual(first, self.reserve())
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual([], self.files())
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.store.read_result(binding())

    def test_deadline_must_fit_reservation_without_extending_original_deadline(self):
        for deadline in (True, 1.5, "2000", None):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.reserve(deadline=deadline)
        for deadline in (999, 1000, 61_000, 99_999):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(HistoryError, "history_result_capacity"):
                self.reserve(deadline=deadline)
        first = self.reserve()
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.reserve(deadline=2001)
        self.assertEqual(first, self.store.result_reservation(binding()))

    def test_full_supported_size_is_reserved_before_any_result_exists(self):
        self.reserve()
        self.reserve(2)
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.reserve(3)
        self.assertEqual([], self.files())
        self.assertEqual((), self.store.pending_terminals())

    def test_exact_one_mib_validated_body_survives_private_storage_and_reopen(self):
        self.reserve()
        value = result()
        value["summary"] = ""
        value["summary"] = "x" * (MAX_RESULT_BODY_BYTES - len(canonical_json(value)))
        body = canonical_json(value)
        self.assertEqual(MAX_RESULT_BODY_BYTES, len(body))
        record = json.loads(terminal())
        record.update(result_digest=keyed_digest(KEY, body), fenced=False, reviewed=True,
                      terminal_state=None, result_validation_reason_code=None)
        content = canonical_json(record)
        self.store.commit_result(content, binding(), assignment(), body)
        self.store.close()
        self.store = self.open()
        self.assertEqual(body, self.store.read_result(binding()))
        self.assertEqual(MAX_RESULT_BODY_BYTES, self.store.result_reservation(binding()).body_size)

    def test_capacity_values_reject_boolean_float_overflow_and_unbounded_defaults(self):
        for field in ("max_entries", "max_reserved_bytes", "reservation_ms", "retention_ms"):
            for invalid in (True, 1.5, None, "2", 0, -1, 1 << 100):
                values = vars(self.capacity).copy()
                values[field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                    ResultCapacity(**values)

    def test_all_binding_fields_and_assignment_are_immutable(self):
        original = self.reserve()
        for name, value in vars(binding()).items():
            changed = (value + 1 if type(value) is int else
                       PROJECT_B if name == "project_id" else
                       "b" * 64 if name.endswith("digest") else
                       "orc_" + "b" * 48 if name == "orchestration_handle" else
                       "hrn_" + "b" * 48 if name == "run_handle" else "other-id")
            altered = replace(binding(), **{name: changed})
            with self.subTest(field=name), self.assertRaises(HistoryError):
                self.store.reserve_result(altered, assignment(), hard_deadline_at_ms=2000)
        changed_assignment = assignment()
        changed_assignment["objective"] += " changed"
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.reserve_result(binding(), changed_assignment, hard_deadline_at_ms=2000)
        self.assertEqual(original, self.store.result_reservation(binding()))

    def test_legacy_negative_and_host_fenced_results_survive_reopen_exactly(self):
        for number, kind in enumerate(("legacy", "negative", "fenced"), 1):
            if number == 3:
                self.store.retire_result(binding())
            self.reserve(number)
            body, content = self.payload(number, kind=kind)
            delivery = self.store.commit_result(content, binding(number), assignment(), body)
            self.assertEqual(body, self.store.read_result(binding(number)))
            self.assertEqual(content, self.store.record(delivery.record_id).content)
            before = self.store.result_reservation(binding(number))
            self.now += 1
            self.assertEqual(delivery, self.store.commit_result(content, binding(number), assignment(), body))
            self.assertEqual(before, self.store.result_reservation(binding(number)))
            self.store.close()
            self.store = self.open()
            self.assertEqual(body, self.store.read_result(binding(number)))

    def test_committed_retention_starts_after_execution_not_at_reservation(self):
        reserved = self.reserve()
        self.now = 50_000  # Persistence may follow an earlier actual terminal fact.
        self.commit()
        committed = self.store.result_reservation(binding())
        self.assertEqual((reserved.reserved_until_ms, 50_000, 170_000),
                         (committed.reserved_until_ms, committed.committed_at_ms, committed.expires_at_ms))
        self.now = 70_000
        self.assertEqual(committed, self.reserve())
        self.commit()
        self.assertEqual(committed, self.store.result_reservation(binding()))

    def test_no_result_or_assignment_text_enters_sqlite(self):
        assigned = assignment()
        assigned["objective"] = "ASSIGNMENT-PRIVATE-CANARY"
        self.store.reserve_result(binding(), assigned, hard_deadline_at_ms=2000)
        body, content = self.payload()
        self.store.commit_result(content, binding(), assigned, body)
        dumped = "\n".join(self.store._connection.iterdump())
        for text in ("ASSIGNMENT-PRIVATE-CANARY", "PRIVATE-RESULT-CANARY"):
            self.assertNotIn(text, dumped)
            self.assertNotIn(text.encode().hex(), dumped)
        files = self.files()
        self.assertEqual(1, len(files))
        self.assertEqual(body, files[0].read_bytes())
        self.assertEqual(0o600, files[0].stat().st_mode & 0o777)

    def test_invalid_oversized_noncanonical_or_wrong_digest_bodies_leave_no_files(self):
        self.reserve()
        body, content = self.payload()
        for invalid in (b"REJECTED-PRIVATE-CANARY", b"x" * (MAX_RESULT_BODY_BYTES + 1),
                        b" " + body, body.replace(b"PRIVATE-RESULT-CANARY", b"changed")):
            with self.subTest(size=len(invalid)), self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
                self.store.commit_result(content, binding(), assignment(), invalid)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
            self.assertEqual([], self.files())
            self.assertEqual((), self.store.pending_terminals())

    def test_unreserved_or_changed_assignment_cannot_write_a_body(self):
        body, content = self.payload()
        with self.assertRaisesRegex(HistoryError, "history_result_not_reserved"):
            self.store.commit_result(content, binding(), assignment(), body)
        self.reserve()
        altered = assignment()
        altered["objective"] += " changed"
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_result(content, binding(), altered, body)
        self.assertEqual([], self.files())

    def test_metadata_only_commit_cannot_bypass_a_reserved_result(self):
        self.reserve()
        body, content = self.payload()
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.store.commit_terminal(content, binding(), ingested_at_ms=self.now)
        delivery = self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(delivery, self.store.commit_terminal(content, binding(), ingested_at_ms=self.now))

    def assert_metadata_alias_is_refused(self, **changes):
        self.reserve()
        body, content = self.payload()
        alias = replace(binding(), **changes)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(content, alias, ingested_at_ms=self.now)
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual([], self.files())
        delivery = self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(1, delivery.delivery_sequence)
        self.assertEqual(1, self.store.record(delivery.record_id).source_sequence)
        self.assertEqual(body, self.store.read_result(binding()))

    def test_changed_claim_id_cannot_bypass_reserved_run_and_claim_digest(self):
        self.assert_metadata_alias_is_refused(claim_id="other-claim")

    def test_changed_claim_and_digest_cannot_bypass_reserved_run_attempt(self):
        self.assert_metadata_alias_is_refused(claim_id="other-claim", claim_digest="b" * 64)

    def test_changed_claim_and_run_cannot_bypass_reserved_claim_digest(self):
        self.assert_metadata_alias_is_refused(claim_id="other-claim", run_handle="hrn_" + "b" * 48)

    def test_request_matching_two_reservations_cannot_consume_either_identity(self):
        self.reserve()
        self.reserve(2)
        _, content = self.payload()
        alias = replace(binding(), run_handle=binding(2).run_handle)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(content, alias, ingested_at_ms=self.now)
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual((1, 2), (self.commit().delivery_sequence, self.commit(2).delivery_sequence))

    def test_expired_reservation_still_blocks_metadata_identity_reuse(self):
        self.reserve()
        self.now = 61_000
        self.assertEqual(1, self.store.expire_results())
        _, content = self.payload()
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(content, replace(binding(), claim_id="other-claim"), ingested_at_ms=self.now)
        self.assertEqual((), self.store.pending_terminals())

    def test_retired_body_keeps_exact_metadata_replay_but_not_an_alias(self):
        self.reserve()
        delivery = self.commit()
        _, content = self.payload()
        self.store.retire_result(binding())
        self.assertEqual(delivery, self.store.commit_terminal(content, binding(), ingested_at_ms=self.now))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_terminal(content, replace(binding(), claim_id="other-claim"), ingested_at_ms=self.now)
        self.assertEqual((delivery,), self.store.pending_terminals())
        self.assertEqual([], self.files())

    def test_boolean_assignment_attempt_cannot_reserve_or_write_a_body(self):
        assigned = assignment()
        assigned["attempt"] = True
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.store.reserve_result(binding(), assigned, hard_deadline_at_ms=2000)
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])
        self.assertEqual([], self.files())

    def test_atomic_terminal_failure_retains_exact_prepared_retry_without_consuming_sequence(self):
        self.reserve()
        body, content = self.payload()
        with mock.patch.object(self.store, "_insert_terminal", side_effect=RuntimeError("fixture cut")):
            with self.assertRaisesRegex(RuntimeError, "fixture cut"):
                self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual("reserved", self.store.result_reservation(binding()).state)
        self.assertEqual(body, self.files()[0].read_bytes())
        changed, changed_record = self.payload(marker="changed valid result")
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.commit_result(changed_record, binding(), assignment(), changed)
        self.store.close()
        self.store = self.open()
        delivery = self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(1, delivery.delivery_sequence)
        self.assertEqual(1, self.store.record(delivery.record_id).source_sequence)
        self.assertEqual(body, self.store.read_result(binding()))

    def test_failed_metadata_commit_cannot_restart_existing_body_retention(self):
        self.store.close()
        self.capacity = ResultCapacity(16, 2 * MAX_RESULT_BODY_BYTES, 60_000, 5000)
        self.store = self.open()
        self.reserve()
        body, content = self.payload()
        with mock.patch.object(self.store, "_insert_terminal", side_effect=RuntimeError("metadata cut")):
            with self.assertRaisesRegex(RuntimeError, "metadata cut"):
                self.store.commit_result(content, binding(), assignment(), body)
        prepared = self.store.result_reservation(binding())
        self.assertEqual((1000, 6000, 6000, None), (prepared.prepared_at_ms, prepared.retained_until_ms,
                                                   prepared.expires_at_ms, prepared.committed_at_ms))
        self.assertEqual(1, len(self.files()))
        self.now = 7000
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual([], self.files())
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual("expired", self.store.result_reservation(binding()).state)

    def test_changed_policy_and_later_success_keep_original_prepared_retention(self):
        self.store.close()
        self.capacity = ResultCapacity(16, 2 * MAX_RESULT_BODY_BYTES, 60_000, 5000)
        self.store = self.open()
        self.reserve()
        body, content = self.payload()
        with mock.patch.object(self.store, "_insert_terminal", side_effect=RuntimeError("metadata cut")):
            with self.assertRaises(RuntimeError):
                self.store.commit_result(content, binding(), assignment(), body)
        self.now = 1500
        self.store.close()
        self.capacity = replace(self.capacity, retention_ms=120_000)
        self.store = self.open()
        self.store.commit_result(content, binding(), assignment(), body)
        committed = self.store.result_reservation(binding())
        self.assertEqual((1000, 1500, 6000, 6000),
                         (committed.prepared_at_ms, committed.committed_at_ms,
                          committed.retained_until_ms, committed.expires_at_ms))
        self.assertEqual(body, self.store.read_result(binding()))
        self.now = 6000
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.store.read_result(binding())
        self.assertEqual([], self.files())

    def test_interrupted_partial_write_is_bounded_and_exact_retry_recovers(self):
        self.reserve()
        body, content = self.payload()
        with mock.patch.object(project_history.os, "link", side_effect=OSError("CANARY-link-cut")):
            with self.assertRaisesRegex(HistoryError, "history_unavailable") as caught:
                self.store.commit_result(content, binding(), assignment(), body)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(1, len(self.files()))
        self.assertTrue(self.files()[0].name.endswith(".partial"))
        self.store.close()
        self.store = self.open()
        self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(body, self.store.read_result(binding()))
        self.assertEqual(1, len(self.files()))
        self.assertTrue(self.files()[0].name.endswith(".json"))

    def test_interrupted_two_link_publication_recovers_only_the_exact_pair(self):
        self.reserve()
        body, content = self.payload()
        original = os.unlink
        def cut(name, *args, **kwargs):
            if str(name).endswith(".partial"):
                raise OSError("publication cut")
            return original(name, *args, **kwargs)
        with mock.patch.object(project_history.os, "unlink", side_effect=cut):
            with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(2, len(self.files()))
        self.assertEqual(2, self.files()[0].stat().st_nlink)
        self.store.close()
        self.store = self.open()
        self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual(body, self.store.read_result(binding()))
        self.assertEqual(1, len(self.files()))
        self.assertEqual(1, self.files()[0].stat().st_nlink)

    def test_expiry_after_artifact_io_never_commits_or_returns_expired_bytes(self):
        self.reserve()
        body, content = self.payload()
        write = self.store._write_result_file
        def slow_write(*args):
            write(*args)
            self.now = 61_000
        with mock.patch.object(self.store, "_write_result_file", side_effect=slow_write):
            with self.assertRaisesRegex(HistoryError, "history_result_expired"):
                self.store.commit_result(content, binding(), assignment(), body)
        self.assertEqual([], self.files())
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual("expired", self.store.result_reservation(binding()).state)

    def test_expiry_after_read_does_not_emit_the_already_read_private_bytes(self):
        self.reserve()
        self.commit()
        read = self.store._read_result_file
        def slow_read(*args):
            body = read(*args)
            self.now = 121_000
            return body
        with mock.patch.object(self.store, "_read_result_file", side_effect=slow_read):
            with self.assertRaisesRegex(HistoryError, "history_result_expired"):
                self.store.read_result(binding())
        self.assertEqual([], self.files())

    def test_failed_delete_keeps_capacity_and_retired_state_until_real_cleanup(self):
        self.store.close()
        self.capacity = ResultCapacity(2, MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.store = self.open()
        self.reserve()
        self.commit()
        with mock.patch.object(project_history.os, "unlink", side_effect=PermissionError("CANARY-delete")):
            with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                self.store.retire_result(binding())
        self.assertEqual("retiring", self.store._connection.execute("SELECT state FROM result_reservations").fetchone()[0])
        # Corrected for 0.3.4 R8: a reservation now runs the cleanup when the store looks full, so the
        # delete must still fail during it for the charge to be held, as the test intends.
        with mock.patch.object(project_history.os, "unlink", side_effect=PermissionError("CANARY-delete")):
            with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
                self.reserve(2)
        self.assertEqual(1, self.store.expire_results())
        self.reserve(2)
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.reserve()  # The retired attempt's tombstone still refuses it.
        self.store.retire_result(binding(2))
        # Corrected for 0.3.4 R1 (owner, 2026-11-18): retired tombstones no longer take entries, so the
        # third fits. 0.8.29 refused it here ("retained replay tombstones are never evicted").
        self.reserve(3)

    def test_expired_orchestration_body_is_deleted_but_terminal_outbox_and_replay_survive(self):
        self.reserve()
        delivery = self.commit()
        self.store.retire_result(binding())  # Trusted lifecycle fixture, not native close proof.
        self.assertEqual([], self.files())
        self.assertEqual((delivery,), self.store.pending_terminals())
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.store.read_result(binding())
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.reserve()

    def prepare_expired_results(self, count=2):
        if count > 2:
            self.store.close()
            self.capacity = replace(self.capacity, max_reserved_bytes=count * MAX_RESULT_BODY_BYTES)
            self.store = self.open()
        for number in range(1, count + 1):
            self.reserve(number)
            self.commit(number)
        self.now = 121_000
        unlink = os.unlink
        def fail_for(*numbers):
            blocked = {name for number in numbers for name in self.store._result_names(binding(number))}
            def guarded(name, *args, **kwargs):
                if name in blocked:
                    raise PermissionError("CANARY-private-expiry-detail")
                return unlink(name, *args, **kwargs)
            return guarded
        return fail_for

    def test_failed_expiry_does_not_skip_healthy_rows_in_the_same_batch(self):
        fail_for = self.prepare_expired_results()
        with mock.patch.object(project_history.os, "unlink", side_effect=fail_for(1)):
            with self.assertRaisesRegex(HistoryError, "history_unavailable") as caught:
                self.store.expire_results(limit=2)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))
        rows = self.store._connection.execute("SELECT claim_id,state FROM result_reservations ORDER BY claim_id").fetchall()
        self.assertEqual([("claim-1", "retiring"), ("claim-2", "expired")], [tuple(row) for row in rows])
        self.assertEqual(1, len(self.files()))

    def test_limit_one_expiry_rotates_then_wraps_and_retries_the_failed_row(self):
        fail_for = self.prepare_expired_results()
        with mock.patch.object(project_history.os, "unlink", side_effect=fail_for(1)):
            with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                self.store.expire_results(limit=1)
            self.assertEqual(1, self.store.expire_results(limit=1))
            with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                self.store.expire_results(limit=1)
        self.assertEqual(1, len(self.files()))
        self.assertEqual(1, self.store.expire_results(limit=1))
        self.assertEqual(0, self.store.expire_results(limit=1))
        self.assertEqual([], self.files())
        self.assertEqual(2, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations WHERE state='expired'").fetchone()[0])

    def test_multiple_bad_expirations_cannot_starve_a_later_candidate(self):
        fail_for = self.prepare_expired_results(3)
        with mock.patch.object(project_history.os, "unlink", side_effect=fail_for(1, 2)):
            for _ in range(2):
                with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                    self.store.expire_results(limit=1)
            self.assertEqual(1, self.store.expire_results(limit=1))
        rows = self.store._connection.execute("SELECT claim_id,state FROM result_reservations ORDER BY claim_id").fetchall()
        self.assertEqual([("claim-1", "retiring"), ("claim-2", "retiring"), ("claim-3", "expired")],
                         [tuple(row) for row in rows])
        self.assertEqual(2, len(self.files()))

    def test_corrupt_expiry_metadata_keeps_its_charge_without_blocking_healthy_cleanup(self):
        self.prepare_expired_results()
        self.store._connection.execute("UPDATE result_reservations SET binding=? WHERE claim_id='claim-1'", (1_000_000_000,))
        with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
            self.store.expire_results(limit=2)
        self.assertIsNone(caught.exception.__context__)
        rows = self.store._connection.execute("SELECT claim_id,state FROM result_reservations ORDER BY claim_id").fetchall()
        self.assertEqual([("claim-1", "committed"), ("claim-2", "expired")], [tuple(row) for row in rows])
        self.assertEqual(1, len(self.files()))

    def test_oversized_row_cannot_prevent_bounded_candidate_selection_and_other_cleanup(self):
        self.prepare_expired_results()
        original = self.store._connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        self.store._connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, original * 4)
        self.store._connection.execute("UPDATE result_reservations SET binding=? WHERE claim_id='claim-1'", (b"x" * (original * 2),))
        self.store._connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, original)
        with self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.store.expire_results(limit=2)
        rows = self.store._connection.execute("SELECT claim_id,state FROM result_reservations ORDER BY claim_id").fetchall()
        self.assertEqual([("claim-1", "committed"), ("claim-2", "expired")], [tuple(row) for row in rows])
        self.assertEqual(1, len(self.files()))

    def test_expiry_cursor_covers_the_full_sqlite_signed_row_id_domain(self):
        fail_for = self.prepare_expired_results()
        self.store._connection.execute("UPDATE result_reservations SET rowid=? WHERE claim_id='claim-1'", (-(1 << 63),))
        with mock.patch.object(project_history.os, "unlink", side_effect=fail_for(1)):
            with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                self.store.expire_results(limit=1)
            self.assertEqual(1, self.store.expire_results(limit=1))
        self.assertEqual(1, self.store.expire_results(limit=1))
        self.assertEqual(0, self.store.expire_results(limit=1))
        self.assertEqual([], self.files())

    def test_wrong_key_and_backward_clock_fail_closed_across_reopen(self):
        self.reserve()
        self.commit()
        self.now = 2000
        self.store.read_result(binding())
        self.store.close()
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.open(result_key=b"z" * 32)
        self.store = self.open()
        self.now = 1500
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.store.read_result(binding())
        self.now = 121_000
        self.assertEqual(1, self.store.expire_results())
        self.store.close()
        self.store = self.open()
        self.now = 1000
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.store.read_result(binding())

    def test_corrupt_changed_or_aliased_files_never_return_private_content(self):
        self.reserve()
        body, _ = self.payload()
        self.commit()
        path = self.files()[0]
        path.write_bytes(b"CHANGED-PRIVATE-CANARY")
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.store.read_result(binding())
        path.write_bytes(body)
        alias = self.directory / "unrelated-link"
        os.link(path, alias)
        with self.assertRaisesRegex(HistoryError, "history_unsafe_path"):
            self.store.read_result(binding())
        alias.unlink()
        path.unlink()
        path.symlink_to(self.directory / "history.sqlite3")
        with self.assertRaises(HistoryError):
            self.store.read_result(binding())

    def test_corrupt_metadata_is_not_coerced_or_delivered(self):
        self.reserve()
        self.commit()
        self.store._connection.execute("UPDATE result_reservations SET binding=?", (1_000_000_000,))
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.store.read_result(binding())
        self.assertEqual(1, len(self.files()))

    def test_invalid_or_failed_trusted_clock_has_only_a_fixed_failure(self):
        self.reserve()
        for invalid in (True, 1.5, "1000", None, -1, 1 << 100):
            with self.subTest(invalid=invalid), mock.patch.object(self.store, "_result_clock", return_value=invalid), \
                    self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
                self.store.result_reservation(binding())
        with mock.patch.object(self.store, "_result_clock", side_effect=RuntimeError("CANARY-private-clock")):
            with self.assertRaisesRegex(HistoryError, "history_result_unavailable") as caught:
                self.store.result_reservation(binding())
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual([], self.files())

    def test_owner_thread_guard_precedes_validation_clock_and_file_work(self):
        observed = []
        def foreign():
            for operation in (lambda: self.store.reserve_result(None, None, hard_deadline_at_ms=None),
                              lambda: self.store.commit_result(None, None, None, None),
                              lambda: self.store.read_result(None), lambda: self.store.result_reservation(None),
                              lambda: self.store.retire_result(None), lambda: self.store.expire_results(limit=None)):
                try:
                    operation()
                except HistoryError as exc:
                    observed.append(exc.reason_code)
        with mock.patch.object(self.store, "_result_clock", side_effect=AssertionError("clock called")):
            thread = threading.Thread(target=foreign)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(["history_wrong_owner"] * 6, observed)

    def test_actual_process_crashes_preserve_prepared_file_and_terminal_reconciliation(self):
        program = """
import json,os,sys
from pathlib import Path
from codex_agents import project_history
from codex_agents.history_contracts import AttemptBinding
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
cut=sys.argv[2]
class Cut(ProjectHistory):
    def _write_result_file(self,*args):
        if cut=='before-file': os._exit(71)
        super()._write_result_file(*args)
        if cut=='after-file': os._exit(72)
    def _insert_terminal(self,*args):
        super()._insert_terminal(*args)
        if cut=='before-terminal-commit': os._exit(73)
    def _recover_result_link(self,*args):
        if getattr(self,'cut_retire',False): os._exit(76)
        return super()._recover_result_link(*args)
original=project_history.os.unlink
def unlink(name,*args,**kwargs):
    if cut=='after-link' and str(name).endswith('.partial'): os._exit(75)
    return original(name,*args,**kwargs)
project_history.os.unlink=unlink
request=json.loads(sys.stdin.read())
target=AttemptBinding(**request['binding'])
store=Cut(Path(sys.argv[1]),target.project_id,result_key=b'r'*32,allow_legacy_orchestration_reference=True,
 result_capacity=ResultCapacity(16,2*MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1000)
store.reserve_result(target,request['assignment'],hard_deadline_at_ms=2000)
store.commit_result(request['content'].encode('ascii'),target,request['assignment'],request['body'].encode('ascii'))
if cut=='retiring':
    store.cut_retire=True
    store.retire_result(target)
os._exit(74)
"""
        body, content = self.payload()
        request = json.dumps({"binding": binding().as_data(), "assignment": assignment(),
                              "content": content.decode("ascii"), "body": body.decode("ascii")})
        for cut, exit_code in (("before-file", 71), ("after-file", 72), ("before-terminal-commit", 73),
                               ("after-commit", 74), ("after-link", 75), ("retiring", 76)):
            with self.subTest(cut=cut):
                directory = Path(self.temp.name) / cut
                directory.mkdir(mode=0o700)
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", "-S", "-c",
                     f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
                     str(directory), cut], input=request, text=True, capture_output=True,
                    timeout=15, env={"PATH": os.defpath},
                )
                self.assertEqual(exit_code, completed.returncode, completed.stderr)
                store = ProjectHistory(directory, PROJECT_A, result_key=KEY, result_capacity=self.capacity,
                                       allow_legacy_orchestration_reference=True,
                                       result_clock_ms=lambda: 1001)
                try:
                    if cut == "retiring":
                        self.assertEqual("retiring", store._connection.execute(
                            "SELECT state FROM result_reservations").fetchone()[0])
                        self.assertEqual(1, len(list(directory.glob(".result-*"))))
                        self.assertEqual("expired", store.result_reservation(binding()).state)
                        self.assertEqual([], list(directory.glob(".result-*")))
                        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
                            store.read_result(binding())
                        self.assertEqual(1, len(store.pending_terminals()))
                        continue
                    reserved = store.result_reservation(binding())
                    self.assertEqual("committed" if cut == "after-commit" else "reserved", reserved.state)
                    if cut != "after-commit":
                        self.assertEqual((), store.pending_terminals())
                        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                            store.read_result(binding())
                    delivery = store.commit_result(content, binding(), assignment(), body)
                    self.assertEqual(1, delivery.delivery_sequence)
                    self.assertEqual(1, store.record(delivery.record_id).source_sequence)
                    self.assertEqual(body, store.read_result(binding()))
                    self.assertEqual((delivery,), store.pending_terminals())
                    self.assertEqual(1, len(list(directory.glob(".result-*"))))
                finally:
                    store.close()


class ResultSchemaMigrationTests(unittest.TestCase):
    def test_exact_schema1_migration_preserves_every_existing_byte_and_sequence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary)
            path = directory / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(project_history._SCHEMA_V1)
            connection.execute("INSERT INTO history_identity VALUES(?,7,3)", (PROJECT_A,))
            connection.execute(f"PRAGMA application_id={project_history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            # A real typed canonical record, not reconstructed legacy input.
            content = terminal()
            record = json.loads(content)
            connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (PROJECT_A, record["record_id"], "dispatch_result", "host", 6,
                                1, 10, content, hashlib.sha256(content).hexdigest(), 0))
            target = binding()
            connection.execute("INSERT INTO terminal_outbox VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                               (PROJECT_A, target.claim_id, 2, target.run_handle, target.attempt,
                                target.claim_digest, canonical_json(target.as_data()), record["record_id"],
                                hashlib.sha256(content).hexdigest(), record["result_digest"]))
            connection.commit()
            connection.close()
            path.chmod(0o600)
            store = ProjectHistory(directory, PROJECT_A, allow_schema_upgrade=True)
            try:
                self.assertEqual(content, store.record(record["record_id"]).content)
                self.assertEqual((7, 3), tuple(store._connection.execute(
                    "SELECT next_source_sequence,next_delivery_sequence FROM history_identity").fetchone()))
                self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)
                self.assertEqual(0, store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])
                delivery, = store.pending_terminals()
                self.assertEqual((target, 2, record["record_id"], record["result_digest"]),
                                 (delivery.binding, delivery.delivery_sequence, delivery.record_id, delivery.result_digest))
                self.assertEqual((6, 10), (store.record(delivery.record_id).source_sequence,
                                          store.record(delivery.record_id).ingested_at_ms))
            finally:
                store.close()

    def test_altered_schema1_is_refused_without_running_its_trigger_or_migration(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary)
            path = directory / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(project_history._SCHEMA_V1)
            connection.execute("INSERT INTO history_identity VALUES(?,1,1)", (PROJECT_A,))
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON records BEGIN DELETE FROM history_identity; END")
            connection.execute(f"PRAGMA application_id={project_history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()
            path.chmod(0o600)
            with self.assertRaisesRegex(HistoryError, "history_incompatible"):
                ProjectHistory(directory, PROJECT_A, allow_schema_upgrade=True)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM history_identity").fetchone()[0])
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='result_reservations'").fetchone())
            finally:
                connection.close()

    def test_actual_process_death_during_schema_upgrade_rolls_back_and_can_restart(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary)
            path = directory / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(project_history._SCHEMA_V1)
            connection.execute("INSERT INTO history_identity VALUES(?,7,3)", (PROJECT_A,))
            connection.execute(f"PRAGMA application_id={project_history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()
            path.chmod(0o600)
            program = """
import os,sys
from pathlib import Path
from codex_agents.project_history import ProjectHistory
class Cut(ProjectHistory):
    def _initialize(self):
        def trace(sql):
            if sql.lstrip().startswith('CREATE TABLE result_reservations'): os._exit(79)
        self._connection.set_trace_callback(trace)
        super()._initialize()
Cut(Path(sys.argv[1]),sys.argv[2],allow_schema_upgrade=True)
"""
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-S", "-c",
                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
                 str(directory), PROJECT_A], capture_output=True, timeout=15, env={"PATH": os.defpath},
            )
            self.assertEqual(79, completed.returncode, completed.stderr)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='result_identity'").fetchone())
                self.assertEqual((7, 3), connection.execute(
                    "SELECT next_source_sequence,next_delivery_sequence FROM history_identity").fetchone())
            finally:
                connection.close()
            store = ProjectHistory(directory, PROJECT_A, allow_schema_upgrade=True)
            try:
                self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)
                self.assertEqual((7, 3), tuple(store._connection.execute(
                    "SELECT next_source_sequence,next_delivery_sequence FROM history_identity").fetchone()))
            finally:
                store.close()

    def test_normal_open_never_upgrades_schema1_even_with_result_key_configuration(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary)
            path = directory / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(project_history._SCHEMA_V1)
            connection.execute("INSERT INTO history_identity VALUES(?,7,3)", (PROJECT_A,))
            connection.execute(f"PRAGMA application_id={project_history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()
            path.chmod(0o600)
            for options in ({}, {"allow_schema_upgrade": False},
                            {"result_key": KEY, "result_capacity": ResultCapacity(2, MAX_RESULT_BODY_BYTES, 60_000, 120_000)}):
                with self.subTest(options=list(options)), self.assertRaisesRegex(HistoryError, "history_incompatible"):
                    ProjectHistory(directory, PROJECT_A, **options)
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='result_identity'").fetchone())
                    self.assertEqual((7, 3), connection.execute(
                        "SELECT next_source_sequence,next_delivery_sequence FROM history_identity").fetchone())
                finally:
                    connection.close()

    def test_upgrade_option_rejects_non_boolean_values_before_opening_files(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            for invalid in (None, 0, 1, "true", 1.0):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                    ProjectHistory(Path(temporary), PROJECT_A, allow_schema_upgrade=invalid)
            self.assertEqual([], list(Path(temporary).iterdir()))


if __name__ == "__main__":
    unittest.main()
